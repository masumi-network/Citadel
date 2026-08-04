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
        # A frozen block with a matching pin is now part of a valid questions
        # file, so the shared helper writes one; TestFrozenFixtureSet covers
        # what happens when it is missing or stale.
        path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "questions": questions,
                    "frozen": {"questions_sha256": sb.questions_pin(questions)},
                }
            )
        )
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
# Corpus census (/api/corpus walk)
# --------------------------------------------------------------------------


class TestCorpusCensus:
    def _page(self, rows, next_cursor=None, total=None):
        return {
            "ok": True,
            "documents": rows,
            "documents_returned": len(rows),
            "documents_total": total,
            "next_cursor": next_cursor,
            "totals": {"documents": total},
            "notes": [],
        }

    def test_census_walks_pages_and_counts_zero_chunk_documents(self):
        pages = [
            self._page(
                [
                    {"id": "d1", "chunk_count": 0},
                    {"id": "d2", "chunk_count": 3},
                    {"id": "d3", "chunk_count": None},
                ],
                next_cursor="c1",
                total=5,
            ),
            self._page(
                [{"id": "d4", "chunk_count": 0}, {"id": "d5", "chunk_count": 7}],
                next_cursor=None,
                total=5,
            ),
        ]
        calls = []

        def fetch(cursor):
            calls.append(cursor)
            return pages[len(calls) - 1]

        census = sb.corpus_census(fetch)
        assert calls == [None, "c1"]
        assert census["documents_total"] == 5
        assert census["documents_walked"] == 5
        assert census["chunk_count_zero"] == 2
        assert census["chunk_count_unmeasured"] == 1
        # Denominator is every walked document, matching the published
        # "892 of 2867 (31.1%)" definition; unmeasured rows make it a floor.
        assert census["chunk_count_zero_ratio"] == pytest.approx(2 / 5)
        assert census["pages"] == 2
        assert census["truncated"] is False

    def test_census_http_error_degrades_to_error_dict(self):
        import urllib.error

        def fetch(cursor):
            raise urllib.error.HTTPError("u", 403, "forbidden", None, None)

        census = sb.corpus_census(fetch)
        assert census["error"] == "HTTP 403"
        assert "reason" in census

    def test_census_stuck_cursor_stops_instead_of_looping(self):
        def fetch(cursor):
            return self._page([{"id": "d1", "chunk_count": 1}], next_cursor="same", total=1)

        census = sb.corpus_census(fetch, max_pages=50)
        assert census["truncated"] is True
        assert census["pages"] == 2  # stopped when the cursor failed to advance

    def test_census_page_cap_truncates_and_says_so(self):
        def fetch(cursor):
            return self._page(
                [{"id": f"d{cursor}", "chunk_count": 0}],
                next_cursor=f"c{len(str(cursor))}{cursor}",
                total=None,
            )

        census = sb.corpus_census(fetch, max_pages=3)
        assert census["truncated"] is True
        assert census["pages"] == 3


# --------------------------------------------------------------------------
# Run JSON records when it ran
# --------------------------------------------------------------------------


class TestRunTimestamp:
    def test_execute_benchmark_stamps_run_at_utc(self):
        from datetime import datetime

        span = "the unique answer sentence lives here"
        qs = [question(spans=[span]), question("p01", recall=0)]
        result = sb.execute_benchmark(
            qs, lambda q, k: ({"results": []}, 1.0, None), quiet=True
        )
        stamped = datetime.fromisoformat(result["run_at"])
        assert stamped.tzinfo is not None


# --------------------------------------------------------------------------
# Empty file map must not fingerprint as a real corpus
# --------------------------------------------------------------------------


class TestEmptyFileMapFingerprint:
    def _empty_state(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"version": 1, "files": {}}))
        return path

    def test_empty_files_map_yields_no_sha(self, tmp_path):
        fingerprint = sb.content_fingerprint(self._empty_state(tmp_path))
        assert fingerprint["sha256"] is None
        assert fingerprint["files"] == 0
        assert "attests nothing" in fingerprint["reason"]

    def test_two_empty_map_runs_are_not_comparable(self, tmp_path):
        # Before the fix both runs recorded sha256("") and compared as
        # COMPARABLE while attesting nothing about the corpus.
        content = sb.content_fingerprint(self._empty_state(tmp_path))
        fingerprint = {
            "content": content,
            "questions_sha256": "q" * 64,
            "harness_git_sha": "deadbeef",
        }
        comparable, verdicts = sb.compare_fingerprints(fingerprint, dict(fingerprint))
        assert comparable is False
        assert any("NOT COMPARABLE" in line for line in verdicts)

    def test_legacy_run_jsons_with_empty_string_sha_do_not_compare(self):
        # Run JSONs written BEFORE the fail-closed fix (e.g. the 2026-08-03
        # baseline) already carry sha256("") with files 0. compare must treat
        # that recorded value as unavailable, not as a matching corpus.
        empty_sha = hashlib.sha256(b"").hexdigest()
        fingerprint = {
            "content": {"sha256": empty_sha, "files": 0},
            "questions_sha256": "q" * 64,
            "harness_git_sha": "deadbeef",
        }
        comparable, verdicts = sb.compare_fingerprints(fingerprint, dict(fingerprint))
        assert comparable is False
        assert any("NOT COMPARABLE" in line for line in verdicts)
        assert any("empty" in line.lower() for line in verdicts)


# --------------------------------------------------------------------------
# Compare notes whole-corpus census drift
# --------------------------------------------------------------------------


class TestCompareCensusNote:
    def _fingerprint(self, census=None):
        fingerprint = {
            "content": {"sha256": "f" * 64},
            "questions_sha256": "q" * 64,
            "harness_git_sha": "deadbeef",
        }
        if census is not None:
            fingerprint["census"] = census
        return fingerprint

    def test_census_total_drift_is_noted_but_not_a_gate(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(census={"documents_total": 3100}),
            self._fingerprint(census={"documents_total": 2867}),
        )
        # Repo-content fingerprint still matches, so the gate stays open, but
        # whole-corpus movement must be said out loud.
        assert comparable is True
        assert any("census" in line and "2867" in line and "3100" in line for line in verdicts)

    def test_missing_census_adds_no_note(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(), self._fingerprint()
        )
        assert comparable is True
        assert not any("census" in line for line in verdicts)


# --------------------------------------------------------------------------
# Markdown report emission
# --------------------------------------------------------------------------


def make_run(
    *,
    run_at="2026-08-03T16:25:00+00:00",
    content_sha="f" * 64,
    census="default",
    rows_count=69,
):
    if census == "default":
        census = {
            "documents_total": 2867,
            "documents_walked": 2867,
            "chunk_count_zero": 892,
            "chunk_count_unmeasured": 0,
            "chunk_count_zero_ratio": 0.3111,
            "pages": 3,
            "truncated": False,
        }
    run = {
        "run_at": run_at,
        "summary": {
            "quality": {
                "answer_recall_at_5": 0.8974,
                "raw_page_recall_at_5": 0.7692,
                "doc_recall_at_5": 0.9508,
                "mrr_body": 0.7521,
                "header_credit_rate": 0.0256,
                "negative_hit_rate": 0.0,
            },
            "duplication": {
                "duplicate_blob_rate_at_10": 0.45,
                "distinct_files_at_10": 4.2,
                "distinct_source_ratio_mean": 0.61,
                "distinct_source_ratio_resolvable_only": 0.65,
                "hits_with_unresolvable_source": 15,
            },
            "stability": {"hit_stability": None},
            "counts": {
                "questions_total": 69,
                "questions_positive": 61,
                "questions_with_spans": 39,
                "questions_excluded_from_answer_recall": 22,
                "questions_blocked_probe": 8,
            },
            "latency": {
                "p50_ms": 504.0,
                "p95_ms": 905.0,
                "mean_ms": 540.0,
                "samples": 69,
                "errors": 0,
            },
            "repeats": 1,
        },
        "rows": [{"duplicate_blob_rate_at_10": 0.5}] * rows_count,
        "fingerprint": {
            "harness_git_sha": "a66cba8" + "0" * 33,
            "questions_sha256": "q" * 64,
            "content": (
                {"sha256": content_sha, "files": 317}
                if content_sha
                else {"sha256": None, "reason": "state file missing"}
            ),
            "api": {"documents_tracked": 2867, "node_version": "9.9.9"},
        },
    }
    if census is not None:
        run["fingerprint"]["census"] = census
    return run


class TestMarkdownReport:
    def test_every_table_metric_carries_its_definition(self):
        markdown = sb.build_markdown_report(make_run())
        for _section, key, _n_source in sb.REPORT_METRICS:
            assert key in markdown
            assert sb.METRIC_DEFINITIONS[key] in markdown

    def test_each_row_is_self_contained_with_date_commit_and_n(self):
        markdown = sb.build_markdown_report(make_run())
        row = next(
            line
            for line in markdown.splitlines()
            if "answer_recall_at_5" in line and "|" in line
        )
        # A row copied out of the table alone must still carry its value, the
        # date, the commit, the sample count, and what it matched on.
        assert "0.8974" in row
        assert "2026-08-03" in row
        assert "a66cba8" in row
        assert "39" in row
        assert sb.METRIC_DEFINITIONS["answer_recall_at_5"] in row

    def test_report_refuses_a_metric_without_a_definition(self, monkeypatch):
        trimmed = {
            key: value
            for key, value in sb.METRIC_DEFINITIONS.items()
            if key != "mrr_body"
        }
        monkeypatch.setattr(sb, "METRIC_DEFINITIONS", trimmed)
        with pytest.raises(sb.BenchError, match="mrr_body"):
            sb.build_markdown_report(make_run())

    def test_report_refuses_run_without_summary(self):
        with pytest.raises(sb.BenchError, match="summary"):
            sb.build_markdown_report({"rows": []})

    def test_census_note_states_never_indexed_share(self):
        markdown = sb.build_markdown_report(make_run())
        assert "2867" in markdown
        assert "892" in markdown
        assert "31.1%" in markdown
        assert "never vector-indexed" in markdown
        assert "cannot" in markdown  # plain-language reachability consequence

    def test_census_unavailable_is_stated_not_silently_dropped(self):
        markdown = sb.build_markdown_report(
            make_run(census={"error": "HTTP 403", "reason": "needs audit:read"})
        )
        assert "census unavailable" in markdown.lower()
        assert "HTTP 403" in markdown

    def test_missing_content_fingerprint_flags_not_comparable(self):
        markdown = sb.build_markdown_report(make_run(content_sha=None))
        assert "NOT comparable on content" in markdown

    def test_latency_is_labelled_client_round_trip(self):
        markdown = sb.build_markdown_report(make_run())
        assert "round-trip" in markdown
        assert "504.0" in markdown

    def test_cmd_report_writes_markdown_file(self, tmp_path, capsys):
        run_path = tmp_path / "run.json"
        run_path.write_text(json.dumps(make_run()), encoding="utf-8")
        out_path = tmp_path / "block.md"
        exit_code = sb.main(
            ["report", str(run_path), "--markdown", "--out", str(out_path)]
        )
        assert exit_code == 0
        written = out_path.read_text(encoding="utf-8")
        assert "answer_recall_at_5" in written
        assert sb.METRIC_DEFINITIONS["answer_recall_at_5"] in written

    def test_cmd_report_prints_to_stdout_without_out(self, tmp_path, capsys):
        run_path = tmp_path / "run.json"
        run_path.write_text(json.dumps(make_run()), encoding="utf-8")
        assert sb.main(["report", str(run_path)]) == 0
        printed = capsys.readouterr().out
        assert "answer_recall_at_5" in printed


# --------------------------------------------------------------------------
# Published paths and prose stay honest
# --------------------------------------------------------------------------


class TestPublishedPathsAndProse:
    def test_report_footer_documents_the_gitignored_out_path(self):
        # The footer is copy-pasted from the repo root, where a bare
        # runs/latest.json resolves to REPO_ROOT/runs/. Only
        # scripts/bench/runs/ is gitignored, and a run JSON enumerates every
        # served hit identity, so the documented path must be the ignored one.
        markdown = sb.build_markdown_report(make_run())
        assert "scripts/bench/runs/latest.json" in markdown
        assert " runs/latest.json" not in markdown

    def test_report_warns_on_legacy_empty_map_fingerprint(self):
        # compare_fingerprints treats EMPTY_MAP_SHA256 as unavailable; the
        # report must apply the same judgement or the two tools disagree
        # about the same run JSON.
        markdown = sb.build_markdown_report(
            make_run(content_sha=sb.EMPTY_MAP_SHA256)
        )
        assert "NOT comparable on content" in markdown
        assert "empty file map" in markdown

    def test_published_mrr_wording_matches_the_harness_definition(self):
        # docs/performance.md restates mrr_body in prose; pin it to METRIC_DEFINITIONS
        # so the two cannot drift apart. The first character is skipped only
        # because the README sentence starts uppercase.
        readme = (Path(__file__).resolve().parent.parent / "docs" / "performance.md").read_text(
            encoding="utf-8"
        )
        assert sb.METRIC_DEFINITIONS["mrr_body"][1:] in readme

    def test_published_quality_rows_state_their_denominators(self):
        # answer_recall@5 and mrr_body are computed over the 39 span-bearing
        # questions, doc_recall@5 over the 61 positives. A row without its n
        # reads as "over all 69 questions", the head-line-0.95 failure again.
        readme = (Path(__file__).resolve().parent.parent / "docs" / "performance.md").read_text(
            encoding="utf-8"
        )
        # Assert on the VALUE cell, not the whole row: the definition cell also
        # says "the 39 span-bearing questions", so a row-wide search passes even
        # when the value loses its n, which is the drift this guards against.
        values = {
            line.split("|")[1].strip(): line.split("|")[2].strip()
            for line in readme.splitlines()
            if line.startswith("| `") and line.count("|") >= 4
        }
        assert "n=39" in values["`answer_recall@5`"]
        assert "n=61" in values["`doc_recall@5`"]
        assert "n=39" in values["`mrr_body`"]


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


def scored_hit(path: str, blob: str, body: str, coverage: float | None,
               *, tier: str = "unattested", sha: str | None = None) -> dict:
    """A repo hit carrying the node's own reported term_coverage and trust tier."""
    hit = repo_hit(path, blob, body)
    hit["_citadel"]["relevance"] = (
        {} if coverage is None else {"term_coverage": coverage, "matched_terms": []}
    )
    hit["_citadel"]["trust_tier"] = tier
    hit["_citadel"]["content_sha256"] = sha or hashlib.sha256(body.encode()).hexdigest()
    return hit


class TestRankingIsMeasuredSeparatelyFromRetrieval:
    """Retrieval asks whether the answer came back; ranking asks whether the node
    put it in the right place. A single recall number blends the two."""

    SPAN = "the subsystem persists attempted charges with a null transaction id"

    def test_exact_answer_below_a_lower_coverage_hit_is_an_inversion(self):
        hits = [
            scored_hit(PATH_OTHER, BLOB_B, "unrelated filler prose", 0.167),
            scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0),
        ]
        row = sb.score_question(question(spans=[self.SPAN]), hits, {})
        assert row["answer_slot"] == 2
        assert row["answer_term_coverage"] == 1.0
        assert row["outranked_by_coverage"] == 0.167
        assert row["outranked_by_slot"] == 1

    def test_answer_ranked_first_is_not_an_inversion(self):
        hits = [
            scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0),
            scored_hit(PATH_OTHER, BLOB_B, "unrelated filler prose", 0.167),
        ]
        row = sb.score_question(question(spans=[self.SPAN]), hits, {})
        assert row["answer_slot"] == 1
        assert row["outranked_by_coverage"] is None

    def test_a_higher_coverage_hit_above_the_answer_is_not_an_inversion(self):
        """Being outranked by something the node scored HIGHER is ordinary
        ranking, not the defect. Only a strictly lower-coverage hit counts."""
        hits = [
            scored_hit(PATH_OTHER, BLOB_B, "unrelated filler prose", 1.0),
            scored_hit(PATH_DOC, BLOB_A, self.SPAN, 0.5),
        ]
        row = sb.score_question(question(spans=[self.SPAN]), hits, {})
        assert row["answer_slot"] == 2
        assert row["outranked_by_coverage"] is None

    def test_a_path_header_match_cannot_manufacture_an_inversion(self):
        """The answer slot is fixed by a BODY span match with the header
        stripped, so a hit whose only overlap is the path header never becomes
        the answer and never creates an inversion."""
        header_only = scored_hit(PATH_DOC, BLOB_B, "no answer text in this body", 1.0)
        hits = [header_only, scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)]
        row = sb.score_question(question(spans=[self.SPAN]), hits, {})
        assert row["answer_slot"] == 2
        assert row["outranked_by_coverage"] is None

    def test_missing_coverage_is_not_counted_as_an_inversion(self):
        hits = [
            scored_hit(PATH_OTHER, BLOB_B, "unrelated filler prose", None),
            scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0),
        ]
        row = sb.score_question(question(spans=[self.SPAN]), hits, {})
        assert row["outranked_by_coverage"] is None

    def test_summary_rate_counts_only_answers_that_were_ranked(self):
        span = self.SPAN
        inverted = sb.score_question(
            question("a1", spans=[span]),
            [
                scored_hit(PATH_OTHER, BLOB_B, "filler", 0.0),
                scored_hit(PATH_DOC, BLOB_A, span, 1.0),
            ],
            {},
        )
        clean = sb.score_question(
            question("a2", spans=[span]),
            [scored_hit(PATH_DOC, BLOB_A, span, 1.0)],
            {},
        )
        never_served = sb.score_question(
            question("a3", spans=[span]),
            [scored_hit(PATH_OTHER, BLOB_B, "filler", 0.2)],
            {},
        )
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0},
            [],
            {},
        )
        summary = sb.summarize([inverted, clean, never_served, probe], [])
        assert summary["ranking"]["answers_ranked"] == 2
        assert summary["ranking"]["rank_inversion_rate"] == 0.5
        assert summary["ranking"]["inversions_by_zero_coverage"] == 1
        assert summary["ranking"]["answer_worst_slot"] == 2


class TestEmbeddingWindowPairs:
    SPAN = "the subsystem persists attempted charges with a null transaction id"

    def _pair_rows(self, head_pass: bool, tail_pass: bool, pair: str = "w01"):
        def side(role, passed):
            q = question(f"{pair}{role[0]}", spans=[self.SPAN])
            q["category"] = f"window_{role}"
            q["window_role"] = role
            q["window_pair"] = pair
            hits = (
                [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)]
                if passed
                else [scored_hit(PATH_OTHER, BLOB_B, "unrelated filler", 0.1)]
            )
            return sb.score_question(q, hits, {})

        return [side("head", head_pass), side("tail", tail_pass)]

    def test_window_rows_are_excluded_from_the_headline(self):
        """v5 added pairs that fail by design. Folding them into
        answer_recall_at_5 would move the headline for a reason that has nothing
        to do with the node changing."""
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        rows = [plain, probe] + self._pair_rows(head_pass=True, tail_pass=False)
        summary = sb.summarize(rows, [])
        assert summary["counts"]["questions_with_spans"] == 1
        assert summary["quality"]["answer_recall_at_5"] == 1.0
        assert summary["counts"]["questions_window"] == 2

    def test_penalty_is_head_recall_minus_tail_recall(self):
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        rows = [plain, probe]
        rows += self._pair_rows(True, False, "w01")
        rows += self._pair_rows(True, True, "w02")
        summary = sb.summarize(rows, [])
        window = summary["window"]
        assert window["head_recall_at_5"] == 1.0
        assert window["tail_recall_at_5"] == 0.5
        assert window["window_penalty"] == 0.5
        assert window["pairs_complete"] == 2
        assert window["pairs_head_only"] == 1
        assert window["pairs_both"] == 1

    def test_a_half_pair_does_not_contribute_to_the_penalty(self):
        """Only pairs whose BOTH sides ran are a within-document comparison."""
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        rows = [plain, probe] + self._pair_rows(True, False, "w01")
        rows = [r for r in rows if r["window_role"] != "tail"]
        summary = sb.summarize(rows, [])
        assert summary["window"]["pairs_complete"] == 0


class TestWindowFixtureContract:
    """The rules that make a failing tail evidence rather than a failing question."""

    HEAD_TEXT = "alpha beta gamma delta epsilon " * 40           # ~1200 chars
    TAIL_TEXT = "zeta thermodynamic eigenvector quaternion sentence here to quote."
    BODY = "# Doc Title\n\n" + HEAD_TEXT + ("filler padding words. " * 200) + TAIL_TEXT

    def _golden(self, tmp_path, question_obj):
        gt = tmp_path / "ground_truth"
        gt.mkdir(exist_ok=True)
        (gt / PATH_DOC.replace("/", "__")).write_text(self.BODY)
        path = tmp_path / "golden.json"
        questions = [question_obj]
        path.write_text(
            json.dumps(
                {
                    "version": 5,
                    "questions": questions,
                    "frozen": {"questions_sha256": sb.questions_pin(questions)},
                }
            )
        )
        return path, gt

    def _tail_q(self, **overrides):
        base = {
            "id": "w01t",
            "question": self.TAIL_TEXT,
            "expect_any": [PATH_DOC],
            "category": "window_tail",
            "expected_recall": 1,
            "answer_spans": [self.TAIL_TEXT],
            "window_pair": "w01",
            "window_role": "tail",
            "source_offset_chars": self.BODY.find(self.TAIL_TEXT),
            "novel_terms_absent_from_head": [
                "thermodynamic", "eigenvector", "quaternion",
            ],
        }
        base.update(overrides)
        return base

    def test_valid_tail_fixture_passes(self, tmp_path):
        path, gt = self._golden(tmp_path, self._tail_q())
        problems, _ = sb.lint_questions(path, gt)
        assert problems == []

    def test_the_quote_being_the_query_is_allowed_only_for_window_questions(self, tmp_path):
        """Every other question rejects a span that appears in its own query.
        A window question IS its verbatim quote by design."""
        plain = self._tail_q(category="repo_overview")
        plain.pop("window_role")
        plain.pop("window_pair")
        path, gt = self._golden(tmp_path, plain)
        problems, _ = sb.lint_questions(path, gt)
        assert any("appears in the question text" in p for p in problems)

    def test_tail_quote_too_shallow_fails(self, tmp_path):
        path, gt = self._golden(tmp_path, self._tail_q(source_offset_chars=10))
        problems, _ = sb.lint_questions(path, gt)
        assert any("too shallow to test the window" in p for p in problems)

    def test_tail_without_enough_novel_terms_fails(self, tmp_path):
        path, gt = self._golden(
            tmp_path, self._tail_q(novel_terms_absent_from_head=["quaternion"])
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("novel_terms_absent_from_head" in p for p in problems)

    def test_a_term_that_actually_occurs_in_the_head_voids_the_control(self, tmp_path):
        """This is the control the whole comparison rests on: if the tail's
        terms are already in the head, the head embedding can answer the query
        and a pass proves nothing about reach."""
        path, gt = self._golden(
            tmp_path,
            self._tail_q(
                novel_terms_absent_from_head=["thermodynamic", "eigenvector", "alpha"]
            ),
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("the control is void" in p for p in problems)
        assert any("alpha" in p for p in problems)

    def test_head_fixture_outside_the_window_fails(self, tmp_path):
        head = self._tail_q(
            id="w01h", category="window_head", window_role="head",
            question=self.TAIL_TEXT, answer_spans=[self.TAIL_TEXT],
        )
        head.pop("novel_terms_absent_from_head")
        path, gt = self._golden(tmp_path, head)
        problems, _ = sb.lint_questions(path, gt)
        assert any("not a head control" in p for p in problems)

    def test_shipped_window_pairs_satisfy_the_contract(self):
        """The committed set, not a fixture: every pair has both sides, both
        quotes are unique to one document, and every tail clears the controls."""
        data = json.loads(
            (Path(sb.HERE) / "golden_questions.json").read_text(encoding="utf-8")
        )
        window = [q for q in data["questions"] if sb.is_window_question(q)]
        assert len(window) >= 20
        by_pair = {}
        for q in window:
            by_pair.setdefault(q["window_pair"], set()).add(q["window_role"])
        assert all(roles == {"head", "tail"} for roles in by_pair.values()), by_pair
        for q in window:
            assert q["answer_spans"] == [q["question"]], q["id"]
            assert len(q["expect_any"]) == 1, q["id"]
            if q["window_role"] == "tail":
                assert q["depth_fraction"] >= sb.TAIL_MIN_DEPTH, q["id"]
                assert (
                    len(q["novel_terms_absent_from_head"]) >= sb.TAIL_MIN_NOVEL_TERMS
                ), q["id"]
            else:
                assert q["source_offset_chars"] < sb.HEAD_WINDOW_CHARS, q["id"]


class TestFrozenFixtureSet:
    """Frozen has to mean something. The pin is what enforces it."""

    def _file(self, tmp_path, questions, frozen=...):
        payload = {"version": 5, "questions": questions}
        if frozen is not ...:
            payload["frozen"] = frozen
        path = tmp_path / "golden.json"
        path.write_text(json.dumps(payload))
        return path

    def test_pin_is_stable_under_key_order_and_formatting(self):
        a = [{"id": "q1", "question": "text", "expected_recall": 1}]
        b = [{"expected_recall": 1, "question": "text", "id": "q1"}]
        assert sb.questions_pin(a) == sb.questions_pin(b)

    def test_pin_moves_when_a_question_changes(self):
        a = [{"id": "q1", "question": "text"}]
        b = [{"id": "q1", "question": "text edited"}]
        assert sb.questions_pin(a) != sb.questions_pin(b)

    def test_edited_question_fails_lint_against_a_stale_pin(self, tmp_path):
        original = [{"id": "q1", "question": "original text"}]
        pin = sb.questions_pin(original)
        edited = [{"id": "q1", "question": "quietly edited text"}]
        path = self._file(tmp_path, edited, {"questions_sha256": pin})
        problems, _ = sb.lint_questions(path, tmp_path / "missing_gt")
        assert any("FROZEN SET CHANGED" in p for p in problems)

    def test_missing_frozen_block_fails(self, tmp_path):
        path = self._file(tmp_path, [{"id": "q1", "question": "text"}])
        problems, _ = sb.lint_questions(path, tmp_path / "missing_gt")
        assert any("no `frozen` block" in p for p in problems)

    def test_shipped_questions_file_matches_its_own_pin(self):
        data = json.loads(
            (Path(sb.HERE) / "golden_questions.json").read_text(encoding="utf-8")
        )
        assert data["frozen"]["questions_sha256"] == sb.questions_pin(data["questions"])


class TestPerRequestMetadataStability:
    def test_same_chunk_same_sha_different_tier_is_flagged(self):
        span = "the subsystem persists attempted charges with a null transaction id"
        a = scored_hit(PATH_DOC, BLOB_A, span, 1.0, tier="reference-only", sha="deadbeef")
        b = scored_hit(PATH_DOC, BLOB_A, span, 1.0, tier="unattested", sha="deadbeef")
        assert a["_citadel"]["result_id"] == b["_citadel"]["result_id"]
        rows = [
            sb.score_question(question("q1", spans=[span]), [a], {}),
            sb.score_question(question("q2", spans=[span]), [b], {}),
            sb.score_question(
                {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
            ),
        ]
        summary = sb.summarize(rows, [])
        meta = summary["metadata_stability"]
        assert meta["chunks_observed"] == 1
        assert meta["chunks_with_unstable_trust_tier"] == 1
        assert meta["unstable_examples"][0]["tiers"] == ["reference-only", "unattested"]

    def test_a_consistent_tier_is_not_flagged(self):
        span = "the subsystem persists attempted charges with a null transaction id"
        hit = scored_hit(PATH_DOC, BLOB_A, span, 1.0, tier="unattested", sha="deadbeef")
        rows = [
            sb.score_question(question("q1", spans=[span]), [hit], {}),
            sb.score_question(question("q2", spans=[span]), [hit], {}),
            sb.score_question(
                {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
            ),
        ]
        summary = sb.summarize(rows, [])
        assert summary["metadata_stability"]["chunks_with_unstable_trust_tier"] == 0


class TestReportCoversTheNewMetrics:
    def _run(self, summary_extra):
        return {
            "run_at": "2026-08-04T00:00:00+00:00",
            "summary": {
                "quality": {
                    "answer_recall_at_5": 0.9, "raw_page_recall_at_5": 0.8,
                    "doc_recall_at_5": 0.9, "mrr_body": 0.7,
                    "header_credit_rate": 0.0, "negative_hit_rate": 0.0,
                },
                "duplication": {"duplicate_blob_rate_at_10": 0.3},
                "counts": {
                    "questions_with_spans": 39, "questions_positive": 61,
                    "questions_blocked_probe": 8,
                },
                "latency": {"p50_ms": 600.0, "p95_ms": 900.0, "samples": 105},
                **summary_extra,
            },
            "rows": [],
            "fingerprint": {"harness_git_sha": "abc1234", "content": {"sha256": "x"}},
        }

    def test_window_and_ranking_rows_carry_their_definitions(self):
        run = self._run(
            {
                "window": {
                    "head_recall_at_5": 0.667, "tail_recall_at_5": 0.0,
                    "window_penalty": 0.667, "pairs_complete": 18,
                },
                "ranking": {"rank_inversion_rate": 0.63, "answers_ranked": 19},
            }
        )
        markdown = sb.build_markdown_report(run)
        for key in ("head_recall_at_5", "tail_recall_at_5", "window_penalty",
                    "rank_inversion_rate"):
            assert key in markdown
            assert sb.METRIC_DEFINITIONS[key][:40] in markdown
        assert "| 18 |" in markdown
        assert "| 19 |" in markdown

    def test_a_run_predating_these_metrics_says_so_instead_of_guessing(self):
        markdown = sb.build_markdown_report(self._run({}))
        assert "not measured in this run" in markdown
        assert "n/a (not measured)" not in markdown.split("head_recall_at_5")[1][:80]
