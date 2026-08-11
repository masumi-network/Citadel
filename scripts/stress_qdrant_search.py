#!/usr/bin/env python3
"""Bounded concurrent search gate for a running Citadel Qdrant node."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class RequestResult:
    status: int
    latency_ms: float
    exact_hit: bool
    retry_after: str | None = None
    error: str | None = None


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return round(ordered[index], 1)


def summarize(results: list[RequestResult]) -> dict[str, Any]:
    latencies = [result.latency_ms for result in results]
    statuses = Counter(str(result.status) for result in results)
    return {
        "requests": len(results),
        "statuses": dict(sorted(statuses.items())),
        "exact_hits": sum(result.exact_hit for result in results),
        "errors": sorted(result.error for result in results if result.error),
        "retry_after_missing": sum(
            result.status == 429 and not result.retry_after for result in results
        ),
        "latency_ms": {
            "min": round(min(latencies), 1) if latencies else 0.0,
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
    }


def _search_once(
    *,
    url: str,
    token: str,
    query: str,
    dataset: str,
    expected_text: str,
    timeout_seconds: float,
) -> RequestResult:
    payload = json.dumps(
        {"query": query, "dataset": dataset, "top_k": 10}
    ).encode("utf-8")
    request = Request(
        f"{url.rstrip('/')}/search",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
            texts = {
                str(item.get("text"))
                for item in body.get("results", [])
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            }
            return RequestResult(
                status=response.status,
                latency_ms=(time.perf_counter() - started) * 1000,
                exact_hit=expected_text in texts,
                retry_after=response.headers.get("Retry-After"),
            )
    except HTTPError as error:
        return RequestResult(
            status=error.code,
            latency_ms=(time.perf_counter() - started) * 1000,
            exact_hit=False,
            retry_after=error.headers.get("Retry-After"),
        )
    except Exception as error:  # noqa: BLE001 - transport failures belong in the report
        return RequestResult(
            status=0,
            latency_ms=(time.perf_counter() - started) * 1000,
            exact_hit=False,
            error=error.__class__.__name__,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--query", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--expected-text", required=True)
    parser.add_argument("--requests", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--require-all-200", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    token = os.getenv("CITADEL_MCP_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("set CITADEL_MCP_ACCESS_TOKEN")
    if args.requests < 1 or args.concurrency < 1:
        raise SystemExit("requests and concurrency must be positive")
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                _search_once,
                url=args.url,
                token=token,
                query=args.query,
                dataset=args.dataset,
                expected_text=args.expected_text,
                timeout_seconds=args.timeout,
            )
            for _ in range(args.requests)
        ]
        results = [future.result() for future in futures]
    report = {
        "dataset": args.dataset,
        "query_sha256": hashlib.sha256(args.query.encode("utf-8")).hexdigest(),
        "concurrency": args.concurrency,
        **summarize(results),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    successful = int(report["statuses"].get("200", 0))
    allowed_statuses = {"200"} if args.require_all_200 else {"200", "429"}
    return int(
        bool(report["errors"])
        or not set(report["statuses"]).issubset(allowed_statuses)
        or successful == 0
        or report["exact_hits"] != successful
        or report["retry_after_missing"] != 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
