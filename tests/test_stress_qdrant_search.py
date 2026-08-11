from types import SimpleNamespace

import scripts.stress_qdrant_search as stress
from scripts.stress_qdrant_search import RequestResult, summarize


def test_summarize_reports_latency_statuses_and_exact_hits() -> None:
    report = summarize(
        [
            RequestResult(status=200, latency_ms=10.0, exact_hit=True),
            RequestResult(status=200, latency_ms=30.0, exact_hit=True),
            RequestResult(
                status=429,
                latency_ms=20.0,
                exact_hit=False,
                retry_after="1",
            ),
        ]
    )

    assert report == {
        "requests": 3,
        "statuses": {"200": 2, "429": 1},
        "exact_hits": 2,
        "errors": [],
        "retry_after_missing": 0,
        "latency_ms": {"min": 10.0, "p50": 20.0, "p95": 30.0, "max": 30.0},
    }


def test_summarize_flags_transport_errors_and_429_without_retry_after() -> None:
    report = summarize(
        [
            RequestResult(
                status=0,
                latency_ms=5.0,
                exact_hit=False,
                error="TimeoutError",
            ),
            RequestResult(status=429, latency_ms=6.0, exact_hit=False),
        ]
    )

    assert report["errors"] == ["TimeoutError"]
    assert report["retry_after_missing"] == 1


def _run_main(monkeypatch, results: list[RequestResult]) -> int:
    class Future:
        def __init__(self, result: RequestResult) -> None:
            self.result_value = result

        def result(self) -> RequestResult:
            return self.result_value

    class Executor:
        pending_results = iter(results)

        def __init__(self, *, max_workers: int) -> None:
            self.max_workers = max_workers

        def __enter__(self) -> "Executor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def submit(self, *args: object, **kwargs: object) -> Future:
            return Future(next(self.pending_results))

    args = SimpleNamespace(
        url="http://example.test",
        query="query",
        dataset="dataset",
        expected_text="expected",
        requests=2,
        concurrency=2,
        timeout=1.0,
        require_all_200=False,
    )
    monkeypatch.setattr(
        stress,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: args),
    )
    monkeypatch.setattr(stress, "ThreadPoolExecutor", Executor)
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "test-token")

    return stress.main()


def test_main_fails_when_all_requests_are_rate_limited(monkeypatch) -> None:
    assert (
        _run_main(
            monkeypatch,
            [
                RequestResult(
                    status=429,
                    latency_ms=1.0,
                    exact_hit=False,
                    retry_after="1",
                ),
                RequestResult(
                    status=429,
                    latency_ms=2.0,
                    exact_hit=False,
                    retry_after="1",
                ),
            ],
        )
        == 1
    )


def test_main_accepts_mixed_successes_and_rate_limits(monkeypatch) -> None:
    assert (
        _run_main(
            monkeypatch,
            [
                RequestResult(status=200, latency_ms=1.0, exact_hit=True),
                RequestResult(
                    status=429,
                    latency_ms=2.0,
                    exact_hit=False,
                    retry_after="1",
                ),
            ],
        )
        == 0
    )
