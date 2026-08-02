"""Tests for the retrieval benchmark harness (scripts/bench/search_bench.py).

Fixtures mirror the REAL /search hit shape: repo-content hits carry a
``# org/repo/path`` first line plus a Repository/Source/Commit/Blob header
terminated by ``---``, an ``id`` distinct from ``document_id``, a ``_citadel``
envelope, and NO top-level ``dataset`` key.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "bench"))

import search_bench as sb  # noqa: E402

BLOB_A = "a" * 40
BLOB_B = "b" * 40
PATH_DOC = "masumi-network/sokosumi/docs/example.md"
PATH_OTHER = "masumi-network/sokosumi/docs/other.md"


def repo_hit(
    path: str,
    blob: str,
    body: str,
    *,
    legacy: bool = False,
    first_chunk: bool = True,
    document_id: str | None = None,
) -> dict:
    org, repo, sub = path.split("/", 2)
    header = [
        f"# {path}",
        "",
        f"Repository: {org}/{repo}",
        f"Source: https://github.com/{org}/{repo}/blob/main/{sub}",
        "Commit: 1234567890abcdef1234567890abcdef12345678",
        f"Blob: {blob}",
    ]
    if legacy:
        header.append("Retrieved: 2026-07-30T00:00:00+00:00")
    text = "\n".join(header + ["", "---", "", body]) if first_chunk else body
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return {
        "id": f"chunk:{digest}",
        "document_id": document_id or f"doc-{hashlib.sha256((path + blob).encode()).hexdigest()[:8]}",
        "text": text,
        "_citadel": {
            "rank": 1,
            "dataset": "masumi-network",
            "result_id": f"chunk:{digest}",
            "provenance": {},
            "retrieval": {"untrusted_context": True, "citation_required": True},
        },
    }


def linear_hit(identifier: str, title: str, body: str) -> dict:
    text = f"# Linear {identifier}: {title}\n\n{body}"
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return {
        "id": f"chunk:{digest}",
        "document_id": f"doc-{identifier}",
        "text": text,
        "_citadel": {"rank": 1, "dataset": "linear", "provenance": {}},
    }


def question(qid: str = "t01", *, spans: list[str] | None = None, expect=None, recall=1) -> dict:
    return {
        "id": qid,
        "question": "How does the example subsystem work?",
        "expect_any": expect if expect is not None else [PATH_DOC],
        "category": "test",
        "expected_recall": recall,
        "answer_spans": spans if spans is not None else [],
    }


# --------------------------------------------------------------------------
# Identity and collapsing
# --------------------------------------------------------------------------


class TestIdentity:
    def test_repo_identity_is_path_plus_blob(self):
        hit = repo_hit(PATH_DOC, BLOB_A, "body text")
        identity = sb.parse_identity(hit)
        assert identity == sb.Identity("repo", PATH_DOC, BLOB_A)

    def test_linear_identity(self):
        hit = linear_hit("SOK-658", "Turn dedup", "Redis fallback details.")
        assert sb.parse_identity(hit) == sb.Identity("linear", "linear:SOK-658")

    def test_headerless_chunk_falls_back_to_document_id(self):
        hit = repo_hit(PATH_DOC, BLOB_A, "later chunk body", first_chunk=False)
        identity = sb.parse_identity(hit)
        assert identity.kind == "doc"
        assert identity.source == hit["document_id"]

    def test_quoted_header_mid_body_does_not_become_identity(self):
        # The old scorer's defect: a doc QUOTING another doc's header scored as
        # that doc. Identity parsing is anchored at chunk start, so the quoting
        # hit keeps its own document id.
        body = f"see the note about\n# {PATH_OTHER}\nwhich covers this."
        hit = repo_hit(PATH_DOC, BLOB_A, body, first_chunk=False)
        identity = sb.parse_identity(hit)
        assert identity.kind == "doc"
        # ... while the legacy scorer still credits it, which is exactly the
        # gap header_credit_rate exists to expose.
        assert sb.legacy_rank([hit], [PATH_OTHER], {}) == 1

    def test_collapse_ten_slots_one_file_two_blobs(self):
        hits = [repo_hit(PATH_DOC, BLOB_A, "the payload answer sentence here")] * 6
        hits += [repo_hit(PATH_DOC, BLOB_B, "the payload answer sentence here")] * 4
        collapsed = sb.collapse_hits(hits)
        assert len(collapsed) == 2
        assert [entry["effective_rank"] for entry in collapsed] == [1, 2]
        assert collapsed[0]["identity"].blob == BLOB_A
        assert collapsed[1]["identity"].blob == BLOB_B
        assert collapsed[1]["first_slot"] == 7

    def test_collapse_keeps_first_occurrence_order(self):
        hits = [
            repo_hit(PATH_OTHER, BLOB_B, "other body"),
            repo_hit(PATH_DOC, BLOB_A, "doc body"),
            repo_hit(PATH_OTHER, BLOB_B, "other body"),
        ]
        collapsed = sb.collapse_hits(hits)
        assert [entry["identity"].source for entry in collapsed] == [PATH_OTHER, PATH_DOC]


class TestPathAndBlobDiversityBothSurvive:
    """The two diversity families answer different questions (PR #183 vs the
    identity rewrite) and neither may be dropped as redundant."""

    def test_path_and_blob_views_disagree_on_one_file_under_two_blobs(self):
        span = "the unique answer sentence lives here"
        hits = [repo_hit(PATH_DOC, BLOB_A, span)] * 6 + [
            repo_hit(PATH_DOC, BLOB_B, span)
        ] * 4
        row = sb.score_question(question(spans=[span]), hits, {})
        # Blob view: two distinct (path, blob) identities in ten slots.
        assert row["duplicate_blob_rate_at_10"] == pytest.approx(0.8)
        # Path view: ONE source file, so the ratio is 1/10, not 2/10.
        assert row["distinct_files_at_10"] == 1
        assert row["distinct_source_ratio"] == pytest.approx(0.1)
        # If these ever agree on this fixture, one view has been collapsed into
        # the other and a real signal has been lost.
        assert row["distinct_source_ratio"] != pytest.approx(
            1 - row["duplicate_blob_rate_at_10"]
        )

    def test_unresolvable_sources_are_counted_and_split_out(self):
        span = "the unique answer sentence lives here"
        # Five resolvable slots across two files, five with no parseable source.
        hits = (
            [repo_hit(PATH_DOC, BLOB_A, span)] * 3
            + [repo_hit(PATH_OTHER, BLOB_B, span)] * 2
            + [
                {"id": f"chunk:none{i}", "document_id": f"doc-{i}", "text": span}
                for i in range(5)
            ]
        )
        row = sb.score_question(question(spans=[span]), hits, {})
        assert row["hits_with_unresolvable_source"] == 5
        # Pessimistic ratio divides by all ten slots; resolvable-only divides by
        # the five attributable ones, so they must differ.
        assert row["distinct_source_ratio"] == pytest.approx(2 / 10)
        assert row["distinct_source_ratio_resolvable_only"] == pytest.approx(2 / 5)


class TestDuplicationMetrics:
    def test_two_blob_page_scores_once_with_dup_rate(self):
        span = "the unique answer sentence lives here"
        hits = [repo_hit(PATH_DOC, BLOB_A, span)] * 6 + [
            repo_hit(PATH_DOC, BLOB_B, span)
        ] * 4
        row = sb.score_question(question(spans=[span]), hits, {})
        assert row["duplicate_blob_rate_at_10"] == pytest.approx(0.8)
        assert row["distinct_files_at_10"] == 1
        assert row["answer_rank"] == 1
        assert row["answer_pass_at_5"] is True
        # One question, one pass: recall over [row] + probe must be 1.0, not
        # inflated by the 10 duplicate slots.
        probe = sb.score_question(question("p01", expect=["masumi-network/x/gone.md"], recall=0), [], {})
        summary = sb.summarize([row, probe], [])
        assert summary["quality"]["answer_recall_at_5"] == 1.0

    def test_duplication_tax_raw_page_misses_collapsed_hits(self):
        # First 5 raw slots are one non-answering file; the answer sits at slot
        # 6. Collapsed, the answer has effective_rank 2; the raw page misses.
        span = "the unique answer sentence lives here"
        hits = [repo_hit(PATH_OTHER, BLOB_B, "filler body")] * 5
        hits.append(repo_hit(PATH_DOC, BLOB_A, span))
        row = sb.score_question(question(spans=[span]), hits, {})
        assert row["raw_page_pass_at_5"] is False
        assert row["answer_rank"] == 2
        assert row["answer_pass_at_5"] is True


# --------------------------------------------------------------------------
# Span matching and header stripping
# --------------------------------------------------------------------------


class TestSpanMatching:
    def test_normalization_survives_backticks_emphasis_whitespace(self):
        body = "Use the **`create_coworker_task`** tool,\n  with `status=\"READY\"` set."
        span = 'Use the create_coworker_task tool, with status="READY" set.'
        assert sb.normalize(span) in sb.normalize(body)

    def test_span_in_header_does_not_match_after_stripping(self):
        # The span is the source path: present in the header, absent from the
        # body. The old scorer would credit this; the body scorer must not.
        hit = repo_hit(PATH_DOC, BLOB_A, "unrelated body content entirely")
        row = sb.score_question(question(spans=[PATH_DOC]), [hit], {})
        assert row["answer_rank"] is None
        assert row["answer_pass_at_5"] is False

    def test_legacy_retrieved_line_is_stripped_with_header(self):
        hit = repo_hit(PATH_DOC, BLOB_A, "real body", legacy=True)
        header, body = sb.split_header_body(hit["text"])
        assert "Retrieved:" in header
        assert "Retrieved:" not in body
        assert body.strip() == "real body"

    def test_linear_leading_line_is_stripped(self):
        hit = linear_hit("SOK-658", "Turn dedup hardening", "Persist a dedup key in Postgres.")
        header, body = sb.split_header_body(hit["text"])
        assert header.startswith("# Linear SOK-658")
        assert "Persist a dedup key" in body
        row = sb.score_question(
            question(spans=["Turn dedup hardening"], expect=["linear:SOK-658"]),
            [hit],
            {},
        )
        # The span only appears in the stripped title line -> no credit.
        assert row["answer_pass_at_5"] is False

    def test_quoted_header_stays_in_body_text(self):
        body = f"as documented in\n# {PATH_OTHER}\nthe flow is different."
        hit = repo_hit(PATH_DOC, BLOB_A, body)
        _, stripped = sb.split_header_body(hit["text"])
        assert PATH_OTHER in stripped

    def test_header_credit_rate_counts_old_scorer_passes_body_rejects(self):
        # Quoting hit: legacy path-match passes, body scorer fails.
        body = f"see\n# {PATH_OTHER}\nfor details."
        hit = repo_hit(PATH_DOC, BLOB_A, body, first_chunk=False)
        row = sb.score_question(
            question(spans=["a span that matches nothing"], expect=[PATH_OTHER]),
            [hit],
            {},
        )
        assert row["legacy_rank"] == 1
        assert row["answer_pass_at_5"] is False
        probe = sb.score_question(question("p01", recall=0), [], {})
        summary = sb.summarize([row, probe], [])
        assert summary["quality"]["header_credit_rate"] == 1.0


# --------------------------------------------------------------------------
# Probes and negatives: never a fake pass
# --------------------------------------------------------------------------


class TestHonestProbes:
    def test_summarize_errors_on_empty_probe_list(self):
        row = sb.score_question(question(spans=["x" * 20]), [], {})
        with pytest.raises(sb.BenchError, match="probe"):
            sb.summarize([row], [])

    def test_execute_benchmark_refuses_before_searching(self):
        calls = []

        def searcher(query, top_k):
            calls.append(query)
            return {"results": []}, 1.0, None

        with pytest.raises(sb.BenchError, match="probe"):
            sb.execute_benchmark([question(spans=["y" * 20])], searcher)
        assert calls == []  # refused before issuing any search

    def test_execute_benchmark_refuses_without_any_spans(self):
        questions = [question(), question("p01", recall=0)]
        with pytest.raises(sb.BenchError, match="answer_spans"):
            sb.execute_benchmark(questions, lambda q, k: ({"results": []}, 1.0, None))

    def test_negative_hit_rate_counts_probe_target_appearing(self):
        probe_q = question("p01", expect=[PATH_DOC], recall=0)
        hit_row = sb.score_question(probe_q, [repo_hit(PATH_DOC, BLOB_A, "leaked")], {})
        span_row = sb.score_question(
            question(spans=["needle sentence of the answer"]),
            [repo_hit(PATH_DOC, BLOB_A, "needle sentence of the answer")],
            {},
        )
        summary = sb.summarize([span_row, hit_row], [])
        assert summary["quality"]["negative_hit_rate"] == 1.0
        miss_row = sb.score_question(probe_q, [repo_hit(PATH_OTHER, BLOB_B, "clean")], {})
        summary = sb.summarize([span_row, miss_row], [])
        assert summary["quality"]["negative_hit_rate"] == 0.0


# --------------------------------------------------------------------------
# Blocked-pattern probes: derived from the live scanner
# --------------------------------------------------------------------------


class TestBlockedPatternProbes:
    # Synthetic, assembled at runtime so no credential-shaped literal ships in
    # this file (same convention as tests/test_security_scan.py fixtures).
    SYNTHETIC_CTDL = "ctdl_" + "Ab1" * 8

    def _probe(self, qid: str = "p90", category: str = "citadel_access_token") -> dict:
        return {
            "id": qid,
            "question": "Where is the vault access token value?",
            "expect_any": [],
            "category": "blocked_probe",
            "expected_recall": 0,
            "blocked_pattern": category,
        }

    def test_load_scanner_patterns_excludes_secret_assignment(self):
        patterns = sb.load_scanner_patterns()
        assert "citadel_access_token" in patterns
        # Carve-outs (_is_credential_like) make the bare regex over-match, so
        # it must never be probe-eligible.
        assert "secret_assignment" not in patterns

    def test_pattern_probe_hit_and_no_secret_in_row(self):
        pattern = sb.load_scanner_patterns()["citadel_access_token"]
        hit = repo_hit(PATH_DOC, BLOB_A, f"config sets token {self.SYNTHETIC_CTDL} here")
        row = sb.score_question(self._probe(), [hit], {}, pattern)
        assert row["doc_rank"] == 1
        assert row["probe_pattern_hit_slot"] == 1
        # The leaked value must never propagate into benchmark artifacts.
        assert self.SYNTHETIC_CTDL not in json.dumps(row)

    def test_pattern_probe_secret_in_header_still_hits(self):
        # A secret in the sync header block is as much a leak as one in the
        # body, so probes scan RAW chunk text, not the stripped body.
        pattern = sb.load_scanner_patterns()["citadel_access_token"]
        hit = repo_hit(PATH_DOC, BLOB_A, "clean body")
        hit["text"] = hit["text"].replace("Commit: ", f"Commit: {self.SYNTHETIC_CTDL} ", 1)
        row = sb.score_question(self._probe(), [hit], {}, pattern)
        assert row["probe_pattern_hit_slot"] == 1

    def test_prefix_mention_without_token_does_not_hit(self):
        pattern = sb.load_scanner_patterns()["citadel_access_token"]
        hit = repo_hit(PATH_DOC, BLOB_A, "docs: tokens use the ctdl_ prefix, minted per seat")
        row = sb.score_question(self._probe(), [hit], {}, pattern)
        assert row["doc_rank"] is None
        assert row["probe_pattern_hit_slot"] is None

    def test_deleted_rule_refuses_before_any_search(self):
        calls = []

        def searcher(query, top_k):
            calls.append(query)
            return {"results": []}, 1.0, None

        qs = [question(spans=["z" * 20]), self._probe(category="rule_that_never_existed")]
        with pytest.raises(sb.BenchError, match="rule_that_never_existed"):
            sb.execute_benchmark(qs, searcher)
        assert calls == []

    def test_secret_assignment_is_not_probe_eligible(self):
        qs = [question(spans=["z" * 20]), self._probe(category="secret_assignment")]
        with pytest.raises(sb.BenchError, match="secret_assignment"):
            sb.execute_benchmark(qs, lambda q, k: ({"results": []}, 1.0, None))

    def test_probe_with_neither_pattern_nor_target_refuses(self):
        probe = {"id": "p91", "question": "anything", "expect_any": [], "expected_recall": 0}
        qs = [question(spans=["z" * 20]), probe]
        with pytest.raises(sb.BenchError, match="never register"):
            sb.execute_benchmark(qs, lambda q, k: ({"results": []}, 1.0, None))

    def test_end_to_end_pattern_hit_feeds_negative_hit_rate(self):
        span = "the unique answer sentence lives here"
        qs = [question(spans=[span]), self._probe()]
        pages = [
            [repo_hit(PATH_DOC, BLOB_A, span)],
            [repo_hit(PATH_OTHER, BLOB_B, f"leaked {self.SYNTHETIC_CTDL} value")],
        ]
        state = {"i": 0}

        def searcher(query, top_k):
            page = pages[min(state["i"], len(pages) - 1)]
            state["i"] += 1
            return {"results": page}, 1.0, None

        result = sb.execute_benchmark(qs, searcher, quiet=True)
        assert result["summary"]["quality"]["negative_hit_rate"] == 1.0


# --------------------------------------------------------------------------
# Repeats: quality from attempt 1 only
# --------------------------------------------------------------------------


class TestRepeats:
    def _searcher(self, pages):
        state = {"i": 0}

        def searcher(query, top_k):
            page = pages[min(state["i"], len(pages) - 1)]
            state["i"] += 1
            return {"results": page}, 5.0, None

        return searcher

    def test_flaky_hit_on_second_attempt_does_not_score(self):
        span = "the stable answer sentence for stability"
        q = [question(spans=[span]), question("p01", recall=0)]
        miss_page = [repo_hit(PATH_OTHER, BLOB_B, "filler")]
        hit_page = [repo_hit(PATH_DOC, BLOB_A, span)]
        # attempt 1 misses, attempt 2 hits; probe pages empty afterwards
        pages = [miss_page, hit_page, [], []]
        result = sb.execute_benchmark(q, self._searcher(pages), repeats=2, quiet=True)
        assert result["summary"]["quality"]["answer_recall_at_5"] == 0.0
        assert result["summary"]["stability"]["hit_stability"] == pytest.approx(0.75)

    def test_first_attempt_hit_scores_even_if_repeat_misses(self):
        span = "the stable answer sentence for stability"
        q = [question(spans=[span]), question("p01", recall=0)]
        hit_page = [repo_hit(PATH_DOC, BLOB_A, span)]
        miss_page = [repo_hit(PATH_OTHER, BLOB_B, "filler")]
        pages = [hit_page, miss_page, [], []]
        result = sb.execute_benchmark(q, self._searcher(pages), repeats=2, quiet=True)
        assert result["summary"]["quality"]["answer_recall_at_5"] == 1.0
        assert result["summary"]["stability"]["hit_stability"] == pytest.approx(0.75)
        # Latency saw every attempt.
        assert result["summary"]["latency"]["samples"] == 4


# --------------------------------------------------------------------------
# Fingerprints and comparability
# --------------------------------------------------------------------------


class TestFingerprints:
    def _state(self, tmp_path, entries, name="state.json"):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "last_checked_at": "2026-08-01T00:00:00+00:00",
                    "files": {
                        key: {
                            "sha": sha,
                            "content_hash": "c" * 16,
                            "last_ingested_at": "2026-08-01T00:00:00+00:00",
                            "cognee_data_ids": ["11111111-1111-1111-1111-111111111111"],
                        }
                        for key, sha in entries
                    },
                }
            )
        )
        return path

    def test_content_fingerprint_is_order_independent(self, tmp_path):
        entries = [(PATH_DOC, BLOB_A), (PATH_OTHER, BLOB_B)]
        one = sb.content_fingerprint(self._state(tmp_path, entries, "a.json"))
        two = sb.content_fingerprint(self._state(tmp_path, list(reversed(entries)), "b.json"))
        assert one["sha256"] == two["sha256"]
        assert one["files"] == 2

    def test_content_fingerprint_moves_when_a_blob_changes(self, tmp_path):
        before = sb.content_fingerprint(
            self._state(tmp_path, [(PATH_DOC, BLOB_A)], "a.json")
        )
        after = sb.content_fingerprint(
            self._state(tmp_path, [(PATH_DOC, BLOB_B)], "b.json")
        )
        assert before["sha256"] != after["sha256"]

    def _fingerprint(self, sha, questions="q" * 64):
        return {
            "content": {"sha256": sha},
            "questions_sha256": questions,
            "harness_git_sha": "deadbeef",
        }

    def test_compare_same_corpus_is_comparable(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint("f" * 64), self._fingerprint("f" * 64)
        )
        assert comparable is True
        assert any("COMPARABLE" in line for line in verdicts)

    def test_compare_moved_corpus_prints_corpus_moved(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint("f" * 64), self._fingerprint("0" * 64)
        )
        assert comparable is False
        assert any("CORPUS MOVED" in line for line in verdicts)

    def test_compare_missing_fingerprint_is_not_comparable(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(None), self._fingerprint("f" * 64)
        )
        assert comparable is False
        assert any("NOT COMPARABLE" in line for line in verdicts)

    def test_compare_changed_questions_is_not_comparable(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint("f" * 64, questions="a" * 64),
            self._fingerprint("f" * 64, questions="b" * 64),
        )
        assert comparable is False
        assert any("QUESTIONS CHANGED" in line for line in verdicts)


# --------------------------------------------------------------------------
# Lint
# --------------------------------------------------------------------------


class TestLint:
    BODY = (
        "# Example Doc Title\n\n"
        "The subsystem persists attempted charges with a null transaction id.\n"
        "Another paragraph with completely different content for padding.\n"
    )
    OTHER_BODY = (
        "# Other Doc Title\n\n"
        "Entirely unrelated prose that shares no sentences with the example.\n"
    )

    def _write_golden(self, tmp_path, questions):
        gt = tmp_path / "ground_truth"
        gt.mkdir(exist_ok=True)
        (gt / PATH_DOC.replace("/", "__")).write_text(self.BODY)
        (gt / PATH_OTHER.replace("/", "__")).write_text(self.OTHER_BODY)
        path = tmp_path / "golden.json"
        path.write_text(json.dumps({"version": 3, "questions": questions}))
        return path, gt

    def _q(self, **overrides):
        base = {
            "id": "t01",
            "question": "How are attempted charges stored?",
            "expect_any": [PATH_DOC],
            "category": "test",
            "expected_recall": 1,
            "answer_spans": [
                "persists attempted charges with a null transaction id"
            ],
        }
        base.update(overrides)
        return base

    def test_valid_span_passes(self, tmp_path):
        path, gt = self._write_golden(tmp_path, [self._q()])
        problems, _ = sb.lint_questions(path, gt)
        assert problems == []

    def test_span_absent_from_body_fails(self, tmp_path):
        path, gt = self._write_golden(
            tmp_path, [self._q(answer_spans=["this sentence exists nowhere at all"])]
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("not found in any cached ground-truth body" in p for p in problems)

    def test_span_matching_first_line_fails(self, tmp_path):
        path, gt = self._write_golden(
            tmp_path, [self._q(answer_spans=["Example Doc Title"])]
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("first line" in p for p in problems)

    def test_span_matching_sync_header_fails(self, tmp_path):
        # The source path answers nothing; it is exactly what the old scorer
        # matched on. It also is not in the body, so expect both complaints.
        path, gt = self._write_golden(tmp_path, [self._q(answer_spans=[PATH_DOC])])
        problems, _ = sb.lint_questions(path, gt)
        assert any("sync header" in p for p in problems)

    def test_span_in_question_text_fails(self, tmp_path):
        q = self._q(
            question="Why does it say it persists attempted charges with a null transaction id?",
        )
        path, gt = self._write_golden(tmp_path, [q])
        problems, _ = sb.lint_questions(path, gt)
        assert any("question text" in p for p in problems)

    def test_span_not_unique_across_other_bodies_fails(self, tmp_path):
        shared = "a shared boilerplate sentence living in two documents"
        path, gt = self._write_golden(tmp_path, [self._q(answer_spans=[shared])])
        (gt / PATH_DOC.replace("/", "__")).write_text(self.BODY + shared + "\n")
        (gt / PATH_OTHER.replace("/", "__")).write_text(self.OTHER_BODY + shared + "\n")
        problems, _ = sb.lint_questions(path, gt)
        assert any("not unique" in p for p in problems)

    def test_unconverted_without_marker_fails(self, tmp_path):
        path, gt = self._write_golden(tmp_path, [self._q(answer_spans=[])])
        problems, _ = sb.lint_questions(path, gt)
        assert any("unconverted" in p or "marker" in p for p in problems)

    def test_unconverted_with_marker_passes(self, tmp_path):
        q = self._q(answer_spans=[])
        q[sb.UNCONVERTED_KEY] = "linear ground truth not cached"
        path, gt = self._write_golden(tmp_path, [q])
        problems, _ = sb.lint_questions(path, gt)
        assert problems == []

    def test_spans_and_marker_together_fail(self, tmp_path):
        q = self._q()
        q[sb.UNCONVERTED_KEY] = "should not also have spans"
        path, gt = self._write_golden(tmp_path, [q])
        problems, _ = sb.lint_questions(path, gt)
        assert any("both" in p for p in problems)

    def test_probe_with_spans_fails(self, tmp_path):
        q = self._q(expected_recall=0)
        path, gt = self._write_golden(tmp_path, [q])
        problems, _ = sb.lint_questions(path, gt)
        assert any("probe" in p for p in problems)

    def test_short_span_fails(self, tmp_path):
        path, gt = self._write_golden(tmp_path, [self._q(answer_spans=["null id"])])
        problems, _ = sb.lint_questions(path, gt)
        assert any("too short" in p for p in problems)

    def test_empty_probe_list_is_noted(self, tmp_path):
        path, gt = self._write_golden(tmp_path, [self._q()])
        _, notes = sb.lint_questions(path, gt)
        assert any("probe" in note for note in notes)

    def test_probe_question_matching_its_own_pattern_fails(self, tmp_path):
        leaky = {
            "id": "p90",
            "question": f"is {TestBlockedPatternProbes.SYNTHETIC_CTDL} still valid?",
            "expect_any": [],
            "expected_recall": 0,
            "blocked_pattern": "citadel_access_token",
        }
        path, gt = self._write_golden(tmp_path, [self._q(), leaky])
        problems, _ = sb.lint_questions(path, gt)
        assert any("matches the" in p for p in problems)

    def test_probe_with_unknown_rule_fails_lint(self, tmp_path):
        bad = {
            "id": "p90",
            "question": "where is the credential?",
            "expect_any": [],
            "expected_recall": 0,
            "blocked_pattern": "rule_that_never_existed",
        }
        path, gt = self._write_golden(tmp_path, [self._q(), bad])
        problems, _ = sb.lint_questions(path, gt)
        assert any("not a blocking rule" in p for p in problems)

    def test_probe_with_neither_pattern_nor_target_fails_lint(self, tmp_path):
        bare = {
            "id": "p90",
            "question": "where is anything?",
            "expect_any": [],
            "expected_recall": 0,
        }
        path, gt = self._write_golden(tmp_path, [self._q(), bare])
        problems, _ = sb.lint_questions(path, gt)
        assert any("never register" in p for p in problems)

    def test_valid_pattern_probe_passes_lint(self, tmp_path):
        probe = {
            "id": "p90",
            "question": "where is the vault access token value?",
            "expect_any": [],
            "expected_recall": 0,
            "blocked_pattern": "citadel_access_token",
        }
        path, gt = self._write_golden(tmp_path, [self._q(), probe])
        problems, _ = sb.lint_questions(path, gt)
        assert problems == []


# --------------------------------------------------------------------------
# The shipped golden set stays valid
# --------------------------------------------------------------------------


class TestShippedGoldenSet:
    def test_every_question_has_spans_or_explicit_marker(self):
        data = json.loads(
            (Path(sb.HERE) / "golden_questions.json").read_text(encoding="utf-8")
        )
        for q in data["questions"]:
            if q.get("expected_recall", 1) == 0:
                continue
            has_spans = bool(q.get("answer_spans"))
            has_marker = bool(q.get(sb.UNCONVERTED_KEY))
            assert has_spans != has_marker, (
                f"{q['id']}: must have exactly one of answer_spans / "
                f"{sb.UNCONVERTED_KEY}"
            )

    def test_probes_exist_and_resolve_against_live_scanner(self):
        """`run` refuses on zero probes, so the shipped set must carry them,
        and every blocked_pattern must still be a rule the scanner enforces."""
        data = json.loads(
            (Path(sb.HERE) / "golden_questions.json").read_text(encoding="utf-8")
        )
        probes = [q for q in data["questions"] if q.get("expected_recall", 1) == 0]
        pattern_probes = [q for q in probes if q.get("blocked_pattern")]
        controls = [q for q in probes if not q.get("blocked_pattern")]
        assert len(pattern_probes) >= 3
        assert len(controls) >= 3
        patterns = sb.load_scanner_patterns()
        for probe in pattern_probes:
            assert probe["blocked_pattern"] in patterns, probe["id"]
            assert not patterns[probe["blocked_pattern"]].search(probe["question"]), probe["id"]
            assert not probe.get("answer_spans"), probe["id"]
        for control in controls:
            assert control.get("expect_any"), control["id"]
