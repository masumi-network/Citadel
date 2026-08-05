"""Tests for the packaged retrieval benchmark harness.

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
        assert row["answer_pass_at_1"] is True
        assert row["answer_pass_at_5"] is True
        # One question, one pass: recall over [row] + probe must be 1.0, not
        # inflated by the 10 duplicate slots.
        probe = sb.score_question(question("p01", expect=["masumi-network/x/gone.md"], recall=0), [], {})
        summary = sb.summarize([row, probe], [])
        assert summary["quality"]["answer_recall_at_5"] == 1.0
        assert summary["quality"]["answer_recall_at_1"] == 1.0

    def test_duplication_tax_raw_page_misses_collapsed_hits(self):
        # First 5 raw slots are one non-answering file; the answer sits at slot
        # 6. Collapsed, the answer has effective_rank 2; the raw page misses.
        span = "the unique answer sentence lives here"
        hits = [repo_hit(PATH_OTHER, BLOB_B, "filler body")] * 5
        hits.append(repo_hit(PATH_DOC, BLOB_A, span))
        row = sb.score_question(question(spans=[span]), hits, {})
        assert row["raw_page_pass_at_5"] is False
        assert row["answer_rank"] == 2
        assert row["answer_pass_at_1"] is False
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

    def test_cmd_lint_accepts_explicit_ground_truth_path(self, tmp_path, capsys):
        path, gt = self._write_golden(tmp_path, [self._q()])
        assert sb.main(
            ["lint", "--questions", str(path), "--ground-truth", str(gt)]
        ) == 0
        assert "lint OK: 1 question(s)" in capsys.readouterr().out

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


class TestRunExitStatus:
    def test_cmd_run_fails_when_every_search_attempt_errors(self, tmp_path, monkeypatch, capsys):
        questions_path = tmp_path / "questions.json"
        questions_path.write_text(
            json.dumps(
                {"questions": [question(spans=["x" * 20]), question("p01", recall=0)]}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
        monkeypatch.setattr(
            sb,
            "make_http_searcher",
            lambda *args: lambda _query, _top_k: (None, 2.0, "connection refused"),
        )
        monkeypatch.setattr(
            sb,
            "build_fingerprint",
            lambda *args: {
                "content": {"sha256": "a" * 64, "files": 1},
                "api": {"documents_tracked": 1, "node_version": "test"},
                "harness_git_sha": "test",
                "questions_sha256": "q" * 64,
            },
        )

        exit_code = sb.main(
            ["run", "--questions", str(questions_path), "--node-url", "https://node.example"]
        )

        assert exit_code == 1
        assert "all 2 benchmark search attempts failed" in capsys.readouterr().err


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
    questions_pin="022b6b4f66c73af1" + "0" * 48,
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
                "answer_recall_at_1": 0.6410,
                "answer_recall_at_5": 0.8974,
                "raw_page_recall_at_5": 0.7692,
                "doc_recall_at_1": 0.7213,
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
    if questions_pin is not None:
        run["fingerprint"]["questions_pin"] = questions_pin
    if census is not None:
        run["fingerprint"]["census"] = census
    return run


class TestReportNamesItsFrozenSet:
    """The runbook told an operator to confirm the pin before reading any
    delta, and named `report` as a surface that prints it. It did not. There
    was no printed surface at all: `compare` gated on the file hash and the
    markdown table named no question set."""

    def test_the_report_prints_the_pin(self):
        markdown = sb.build_markdown_report(make_run())
        assert "questions_pin" in markdown
        assert "022b6b4f66c73af1" in markdown

    def test_a_run_without_a_pin_says_it_is_not_determinable(self):
        markdown = sb.build_markdown_report(make_run(questions_pin=None))
        assert "NOT RECORDED" in markdown
        assert "not determinable" in markdown


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

    def test_the_answer_slot_really_strips_the_sync_header(self):
        """The test above never exercised the stripping: its span occurs in no
        header, so `split_header_body` could be deleted and it stayed green.

        This is the header-credit incident in miniature. A span whose text also
        appears in the sync header must bind the answer slot to the hit whose
        BODY carries it, not to whichever hit ranked highest carrying that
        header. Without the stripping the rank-1 header carrier becomes the
        answer, its low coverage becomes `answer_coverage`, and the inversion
        for that question silently disappears -- rank_inversion_rate falls and
        reads as a ranking improvement.
        """
        span = f"repository: {PATH_DOC.split('/')[0]}/{PATH_DOC.split('/')[1]}"
        header_carrier = scored_hit(PATH_OTHER, BLOB_B, "unrelated filler prose", 0.05)
        body_carrier = scored_hit(PATH_DOC, BLOB_A, f"prose {span} prose", 1.0)
        # The header carrier's FULL text really does contain the span; only the
        # stripping keeps it out of the answer slot.
        assert sb.normalize(span) in sb.normalize(sb.hit_text(header_carrier))
        assert sb.normalize(span) not in sb.normalize(
            sb.split_header_body(sb.hit_text(header_carrier))[1]
        )
        row = sb.score_question(
            question(spans=[span]), [header_carrier, body_carrier], {}
        )
        assert row["answer_slot"] == 2
        assert row["answer_term_coverage"] == 1.0
        assert row["outranked_by_coverage"] == 0.05

    def test_term_coverage_is_the_nodes_number_and_a_path_can_move_it(self):
        """The published claim used to be that a path-header match "can neither
        manufacture nor hide an inversion". It can do both.

        The answer SLOT is header-immune. `term_coverage` is not: the node
        computes it over a haystack that includes each hit's own path, source
        url, provenance and the sync header still sitting in the chunk text
        (kb/search_format.py `_hit_text`), and every hit carries a DIFFERENT
        path. Executed both directions here so the real behaviour is pinned and
        the immunity claim cannot come back.
        """
        # HIDING: a decoy whose body has zero query overlap, but whose path
        # supplies coverage above the answer's, stops being an inversion.
        answer = scored_hit(PATH_DOC, BLOB_A, f"prose {self.SPAN} prose", 0.40)
        decoy_high = scored_hit(PATH_OTHER, BLOB_B, "filler", 0.60)
        hidden = sb.score_question(
            question(spans=[self.SPAN]), [decoy_high, answer], {}
        )
        assert hidden["answer_slot"] == 2
        assert hidden["outranked_by_coverage"] is None
        # Same page, same bodies, same order: only the decoy's reported
        # coverage drops to what its BODY alone would earn.
        decoy_low = scored_hit(PATH_OTHER, BLOB_B, "filler", 0.0)
        revealed = sb.score_question(
            question(spans=[self.SPAN]), [decoy_low, answer], {}
        )
        assert revealed["answer_slot"] == 2
        assert revealed["outranked_by_coverage"] == 0.0

        # MANUFACTURING: the same decoy at a fixed 0.60 becomes an inversion
        # purely because the ANSWER's own filename lifts its coverage to 1.0.
        answer_path_inflated = scored_hit(PATH_DOC, BLOB_A, f"prose {self.SPAN} prose", 1.0)
        manufactured = sb.score_question(
            question(spans=[self.SPAN]), [decoy_high, answer_path_inflated], {}
        )
        assert manufactured["outranked_by_coverage"] == 0.60

        # And the published definition must say so, because the report table
        # copies it verbatim next to the number.
        definition = sb.METRIC_DEFINITIONS["rank_inversion_rate"]
        assert "cannot manufacture or hide" not in definition
        assert "header-immune" in definition
        assert "path" in definition

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

    def test_a_dangling_half_pair_moves_no_published_window_rate(self):
        """head_recall_at_5, tail_recall_at_5 and window_penalty are published
        with `pairs_complete` as their sample count, so all three have to be
        computed over complete pairs.

        They used to be `rate()` over ALL head rows and ALL tail rows. One
        dangling side turned window_penalty into a comparison across two
        different document populations -- the one thing the metric exists to
        avoid -- while `pairs_complete` stayed honest and the test named for
        this kept passing. The report then printed a rate over 3 heads next to
        n = 2.
        """
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        complete = [plain, probe] + self._pair_rows(True, False, "w01")
        baseline = sb.summarize(complete, [])
        assert baseline["window"]["pairs_complete"] == 1
        assert baseline["window"]["head_recall_at_5"] == 1.0
        assert baseline["window"]["tail_recall_at_5"] == 0.0
        assert baseline["window"]["window_penalty"] == 1.0

        # A head with no tail: it is not a within-document comparison, so it
        # must not move the rate, the penalty, or the count.
        dangling_head = [row for row in self._pair_rows(False, False, "w02") if row["window_role"] == "head"]
        with_head = sb.summarize(complete + dangling_head, [])
        assert with_head["window"]["pairs_complete"] == 1
        assert with_head["window"]["head_recall_at_5"] == 1.0
        assert with_head["window"]["window_penalty"] == 1.0

        # And a tail with no head, the other direction.
        dangling_tail = [row for row in self._pair_rows(True, True, "w03") if row["window_role"] == "tail"]
        with_tail = sb.summarize(complete + dangling_tail, [])
        assert with_tail["window"]["pairs_complete"] == 1
        assert with_tail["window"]["tail_recall_at_5"] == 0.0
        assert with_tail["window"]["window_penalty"] == 1.0

    def test_window_documents_counts_documents_not_pair_ids(self):
        """`documents` said how many documents the window measurement covers.
        It counted distinct window_pair ids, so two pairs quoting one document
        reported 2 -- the name-versus-what-it-attests failure the harness
        exists to catch."""
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        # Both pairs name PATH_DOC (see `question`'s default expect_any).
        rows = [plain, probe] + self._pair_rows(True, False, "w01") + self._pair_rows(True, False, "w02")
        summary = sb.summarize(rows, [])
        assert summary["window"]["pairs_complete"] == 2
        assert summary["window"]["documents"] == 1

    def test_rank_inversion_rate_is_reported_split_by_window(self):
        """The headline rate blends 36 verbatim-sentence window queries with
        the real questions, while answer_recall_at_5 deliberately keeps them
        apart. Publish the split so a movement can be attributed."""
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        rows = [plain, probe] + self._pair_rows(True, True, "w01")
        summary = sb.summarize(rows, [])
        ranking = summary["ranking"]
        assert ranking["answers_ranked"] == 3
        assert ranking["answers_ranked_excluding_window"] == 1
        assert ranking["answers_ranked_window_only"] == 2
        assert ranking["rank_inversion_rate_excluding_window"] == 0.0
        assert ranking["rank_inversion_rate_window_only"] == 0.0

    def test_only_the_span_scored_metrics_survive_the_v5_boundary(self):
        """The exclusion protects `span_rows` and nothing else. Naming the
        metrics that DO move stops someone reading a v5 doc_recall_at_5 drop as
        the node degrading; `compare` refuses the cross-set comparison outright
        because the pin differs."""
        probe = sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )
        plain = sb.score_question(
            question("q1", spans=[self.SPAN]),
            [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
            {},
        )
        before = sb.summarize([plain, probe], [])
        after = sb.summarize([plain, probe] + self._pair_rows(True, False, "w01"), [])
        # Unmoved: computed from span_rows.
        assert before["quality"]["answer_recall_at_5"] == after["quality"]["answer_recall_at_5"]
        assert before["quality"]["mrr_body"] == after["quality"]["mrr_body"]
        assert before["quality"]["header_credit_rate"] == after["quality"]["header_credit_rate"]
        # Moved: computed from positives / rows. Both are in REPORT_METRICS.
        assert after["quality"]["doc_recall_at_5"] != before["quality"]["doc_recall_at_5"]
        assert {
            key for _, key, _ in sb.REPORT_METRICS
        } & {"doc_recall_at_5", "duplicate_blob_rate_at_10"} == {
            "doc_recall_at_5",
            "duplicate_blob_rate_at_10",
        }

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

    # A quote that really sits early in the document. `alpha beta gamma` is
    # inside HEAD_TEXT, so body.find puts it at offset 13.
    SHALLOW_QUOTE = "alpha beta gamma delta epsilon alpha beta gamma delta"

    def test_tail_quote_too_shallow_fails(self, tmp_path):
        """A tail quote that really sits in the head window is rejected, and
        the message says BELOW the threshold. It used to read 'above the 0.4
        threshold' while firing on depth < 0.4, sending an author fixing the
        pair in the wrong direction."""
        shallow = self._tail_q(
            question=self.SHALLOW_QUOTE,
            answer_spans=[self.SHALLOW_QUOTE],
            source_offset_chars=self.BODY.find(self.SHALLOW_QUOTE),
        )
        path, gt = self._golden(tmp_path, shallow)
        problems, _ = sb.lint_questions(path, gt)
        assert any("too shallow to test the window" in p for p in problems)
        assert any("BELOW" in p for p in problems)
        assert not any("above the" in p for p in problems), problems

    def test_a_declared_offset_cannot_smuggle_a_shallow_quote_into_the_tail(
        self, tmp_path
    ):
        """`source_offset_chars` is a claim; body.find(quote) is the fact.

        Trusting the claim let a quote inside the first 2000 characters ship
        declared at 50% depth. lint returned no problems, the row landed in
        tail_recall_at_5 and in the tail_recall_given_head_at_5 denominator,
        and every artifact said it had tested a quote past 40% depth.
        """
        forged = self._tail_q(
            question=self.SHALLOW_QUOTE,
            answer_spans=[self.SHALLOW_QUOTE],
            source_offset_chars=int(len(self.BODY) * 0.60),
            depth_fraction=0.60,
        )
        real_offset = self.BODY.find(self.SHALLOW_QUOTE)
        assert real_offset / len(self.BODY) < sb.TAIL_MIN_DEPTH
        path, gt = self._golden(tmp_path, forged)
        problems, notes = sb.lint_questions(path, gt)
        assert any("too shallow to test the window" in p for p in problems), problems
        # The disagreement itself is reported, so a stale fixture is visible
        # rather than silently overridden.
        assert any(str(real_offset) in note for note in notes), notes

    def test_a_drifted_declared_offset_is_a_note_not_a_failure(self, tmp_path):
        """ground_truth/ is refetched from an unpinned upstream HEAD, so a few
        characters of whitespace drift move the real offset. That must not fail
        lint on every machine that fetched on a different day -- the measured
        offset is what the thresholds use, so a stale claim cannot change a
        verdict."""
        drifted = self._tail_q(source_offset_chars=self.BODY.find(self.TAIL_TEXT) - 3)
        path, gt = self._golden(tmp_path, drifted)
        problems, notes = sb.lint_questions(path, gt)
        assert problems == []
        assert any("drifted from the fixture" in note for note in notes), notes

    def test_novel_terms_must_be_terms_of_the_quote(self, tmp_path):
        """Three arbitrary words absent from the head satisfied the control
        while saying nothing about this sentence. A word the quote does not
        contain cannot make the quote novel."""
        path, gt = self._golden(
            tmp_path,
            self._tail_q(
                novel_terms_absent_from_head=["absolute", "meridian", "portcullis"]
            ),
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("are not terms of the quote" in p for p in problems), problems

    def test_a_quote_absent_from_the_body_fails(self, tmp_path):
        """The window-specific verbatim check is case- and whitespace-exact,
        and stricter than the generic normalize()-based span check. Nothing
        exercised it, so it could be deleted with the suite still green."""
        missing = "this exact sentence is nowhere in the cached body at all"
        path, gt = self._golden(
            tmp_path, self._tail_q(question=missing, answer_spans=[missing])
        )
        problems, _ = sb.lint_questions(path, gt)
        assert any("not present verbatim in the cached body" in p for p in problems)

    def test_a_window_role_without_the_category_is_still_linted(self, tmp_path):
        """lint used to decide 'is this a window question' from `category`
        while summarize buckets on `window_role`. A question carrying only
        window_role was invisible to every window check and still entered
        head/tail recall and the tail_recall_given_head_at_5 arithmetic."""
        sneaky = self._tail_q(
            question=self.SHALLOW_QUOTE,
            answer_spans=[self.SHALLOW_QUOTE],
            source_offset_chars=self.BODY.find(self.SHALLOW_QUOTE),
        )
        sneaky.pop("category")
        assert sb.is_window_question(sneaky) is True
        path, gt = self._golden(tmp_path, sneaky)
        problems, _ = sb.lint_questions(path, gt)
        assert any("needs category 'window_tail'" in p for p in problems), problems
        assert any("too shallow to test the window" in p for p in problems), problems
        # And summarize really would have bucketed it, which is why lint has to.
        row = sb.score_question(sneaky, [], {})
        assert row["window_role"] == "tail"

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
            # The two surfaces have to agree. lint keys on category, summarize
            # buckets on window_role; a set where they disagree is one where a
            # row is scored without ever being validated. This runs in CI,
            # where ground_truth/ does not exist and `lint` cannot.
            expected_category = (
                "window_head" if q["window_role"] == "head" else "window_tail"
            )
            assert q["category"] == expected_category, q["id"]
            if q["window_role"] == "tail":
                assert q["depth_fraction"] >= sb.TAIL_MIN_DEPTH, q["id"]
                novel = q["novel_terms_absent_from_head"]
                assert len(novel) >= sb.TAIL_MIN_NOVEL_TERMS, q["id"]
                # Declared novel terms must be terms of the quote itself. This
                # one IS checkable offline: it needs the quote, not the body.
                quote_terms = sb.distinctive_terms(q["question"])
                assert set(map(str, novel)) <= quote_terms, q["id"]
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


class TestAnswerProvenanceIsRecorded:
    """`answer_pass_at_5` scores the first served identity whose body contains
    a span, and never consults `expect_any`. Span uniqueness is enforced by
    lint only across the ~49 files in the gitignored ground-truth cache, never
    across the corpus, so a second render of a file, an org digest or any
    uncached document quoting the sentence scores the pass. That is a real
    blind spot of the method; counting it is what makes it visible."""

    SPAN = "the subsystem persists attempted charges with a null transaction id"

    def _probe(self):
        return sb.score_question(
            {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
        )

    def test_an_answer_off_an_unexpected_document_is_counted(self):
        q = question("q1", spans=[self.SPAN], expect=[PATH_OTHER])
        row = sb.score_question(q, [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)], {})
        # The pass still counts: answer_recall asks whether the answer TEXT
        # came back, doc_recall asks whether the named document did.
        assert row["answer_pass_at_5"] is True
        assert row["doc_rank"] is None
        assert row["answer_from_expected_document"] is False
        summary = sb.summarize([row, self._probe()], [])
        assert summary["quality"]["answers_from_unexpected_documents"] == 1

    def test_an_answer_off_the_expected_document_is_not_counted(self):
        q = question("q1", spans=[self.SPAN])
        row = sb.score_question(q, [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)], {})
        assert row["answer_from_expected_document"] is True
        summary = sb.summarize([row, self._probe()], [])
        assert summary["quality"]["answers_from_unexpected_documents"] == 0

    def test_a_shingle_matched_later_chunk_still_counts_as_expected(self):
        """A later chunk carries no sync header, so identity falls back to the
        document id. The cached-body shingle test is what recognises it, and
        the answer-provenance check has to use the SAME definition doc_rank
        uses or the two disagree about what 'the expected document' means."""
        body = " ".join(f"word{i}" for i in range(60)) + " " + self.SPAN
        hit = repo_hit(PATH_DOC, BLOB_A, body, first_chunk=False)
        hit["_citadel"]["relevance"] = {"term_coverage": 1.0, "matched_terms": []}
        gt = {PATH_DOC: sb.shingles(body)}
        row = sb.score_question(question("q1", spans=[self.SPAN]), [hit], gt)
        assert row["answer_pass_at_5"] is True
        assert row["doc_rank"] == 1
        assert row["answer_from_expected_document"] is True


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

    def test_the_same_chunk_at_a_DIFFERENT_sha_is_not_instability(self):
        """The key is (result_id, content_sha256) and both halves are load
        bearing. Every fixture shared sha="deadbeef", so the sha half was
        untested: degrading the key to result_id alone kept the suite green.

        A chunk whose CONTENT legitimately changed between two requests, and
        whose trust_tier legitimately changed with it, is not per-request
        metadata instability. Counting it turns this metric into a false
        positive generator on any corpus that is being re-indexed -- which is
        exactly when it will be read.
        """
        span = "the subsystem persists attempted charges with a null transaction id"
        a = scored_hit(PATH_DOC, BLOB_A, span, 1.0, tier="reference-only", sha="1111")
        b = scored_hit(PATH_DOC, BLOB_A, span, 1.0, tier="unattested", sha="2222")
        assert a["_citadel"]["result_id"] == b["_citadel"]["result_id"]
        rows = [
            sb.score_question(question("q1", spans=[span]), [a], {}),
            sb.score_question(question("q2", spans=[span]), [b], {}),
            sb.score_question(
                {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0}, [], {}
            ),
        ]
        meta = sb.summarize(rows, [])["metadata_stability"]
        assert meta["chunks_observed"] == 2
        assert meta["chunks_with_unstable_trust_tier"] == 0

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
                    "tail_recall_given_head_at_5": 0.0, "window_penalty": 0.667,
                    "pairs_complete": 18, "pairs_head_reachable": 11,
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


class TestTailRecallIsConditionedOnAReachableDocument:
    """The number that survives the obvious objection: a document whose head
    quote ALSO missed may never have been indexed, so its tail miss says
    nothing about where the text sits."""

    SPAN = "the subsystem persists attempted charges with a null transaction id"

    def _rows(self, pairs):
        rows = [
            sb.score_question(
                {"id": "p1", "question": "probe", "expect_any": [], "expected_recall": 0},
                [],
                {},
            ),
            sb.score_question(
                question("q1", spans=[self.SPAN]),
                [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)],
                {},
            ),
        ]
        for pair, (head_pass, tail_pass) in pairs.items():
            for role, passed in (("head", head_pass), ("tail", tail_pass)):
                q = question(f"{pair}{role[0]}", spans=[self.SPAN])
                q.update({"category": f"window_{role}", "window_role": role,
                          "window_pair": pair})
                hits = (
                    [scored_hit(PATH_DOC, BLOB_A, self.SPAN, 1.0)]
                    if passed
                    else [scored_hit(PATH_OTHER, BLOB_B, "unrelated filler", 0.1)]
                )
                rows.append(sb.score_question(q, hits, {}))
        return rows

    def test_unreachable_documents_are_excluded_from_the_conditional(self):
        # w01 reachable, tail missed. w02 unreachable on both sides.
        summary = sb.summarize(
            self._rows({"w01": (True, False), "w02": (False, False)}), []
        )
        window = summary["window"]
        assert window["pairs_complete"] == 2
        assert window["pairs_neither"] == 1
        assert window["tail_recall_at_5"] == 0.0          # 0 of 2, pessimistic
        assert window["pairs_head_reachable"] == 1        # only w01 is evidence
        assert window["tail_recall_given_head_at_5"] == 0.0

    def test_conditional_counts_only_proven_reachable_documents(self):
        summary = sb.summarize(
            self._rows(
                {"w01": (True, True), "w02": (True, False), "w03": (False, False)}
            ),
            [],
        )
        window = summary["window"]
        assert window["pairs_head_reachable"] == 2
        assert window["tail_recall_given_head_at_5"] == 0.5   # 1 of 2
        assert window["tail_recall_at_5"] == round(1 / 3, 4)  # 1 of 3, pessimistic

    def test_conditional_is_none_when_no_head_ever_retrieved(self):
        summary = sb.summarize(self._rows({"w01": (False, False)}), [])
        assert summary["window"]["tail_recall_given_head_at_5"] is None


class TestRunRecordsWhichFrozenSetItAnswered:
    def test_fingerprint_carries_the_pin_not_just_the_file_hash(self, tmp_path):
        questions = [{"id": "q1", "question": "text", "expected_recall": 1}]
        path = tmp_path / "golden.json"
        path.write_text(json.dumps({"version": 5, "questions": questions}))
        reformatted = tmp_path / "golden2.json"
        reformatted.write_text(json.dumps({"questions": questions, "version": 5}, indent=4))
        # Reformatting moves the file hash but must NOT move the pin: the run
        # answered the same questions.
        assert hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(
            reformatted.read_bytes()
        ).hexdigest()
        assert sb.questions_pin(sb.load_questions(path)) == sb.questions_pin(
            sb.load_questions(reformatted)
        )

    def _fingerprint(self, tmp_path, monkeypatch, questions=None, gt_dir=None):
        """build_fingerprint with only the two network calls stubbed.

        The test above never called build_fingerprint, so the key that actually
        records which frozen set a run answered was untested: renaming it left
        every run JSON silently pinless with the suite still green.
        """
        questions = questions or [{"id": "q1", "question": "text", "expected_recall": 1}]
        path = tmp_path / "golden.json"
        path.write_text(json.dumps({"version": 5, "questions": questions}))
        monkeypatch.setattr(sb, "api_fingerprint", lambda *a, **k: {"node_version": "x"})
        monkeypatch.setattr(sb, "corpus_census", lambda *a, **k: {"documents_total": 1})
        monkeypatch.setattr(sb, "make_corpus_fetcher", lambda *a, **k: None)
        if gt_dir is not None:
            monkeypatch.setattr(sb, "GROUND_TRUTH", gt_dir)
        return sb.build_fingerprint("http://node", "tok", 1.0, path, None), path

    def test_build_fingerprint_records_the_pin(self, tmp_path, monkeypatch):
        fingerprint, path = self._fingerprint(tmp_path, monkeypatch)
        assert "questions_pin" in fingerprint
        assert fingerprint["questions_pin"] == sb.questions_pin(sb.load_questions(path))

    def test_build_fingerprint_records_the_ground_truth_cache(
        self, tmp_path, monkeypatch
    ):
        """ground_truth/ is gitignored, refetched from an unpinned upstream
        HEAD, and feeds doc_rank's shingle fallback and legacy_rank. Two runs
        at the same pin against the same node can therefore score differently.
        The fingerprint has to say which cache was used."""
        gt = tmp_path / "gt"
        gt.mkdir()
        (gt / "org__repo__a.md").write_text("first body")
        before, _ = self._fingerprint(tmp_path, monkeypatch, gt_dir=gt)
        assert before["ground_truth"]["files"] == 1
        assert before["ground_truth"]["sha256"]
        (gt / "org__repo__a.md").write_text("upstream edited this body")
        after, _ = self._fingerprint(tmp_path, monkeypatch, gt_dir=gt)
        assert after["ground_truth"]["sha256"] != before["ground_truth"]["sha256"]

    def test_an_absent_ground_truth_cache_says_so(self, tmp_path, monkeypatch):
        fingerprint, _ = self._fingerprint(
            tmp_path, monkeypatch, gt_dir=tmp_path / "nope"
        )
        assert fingerprint["ground_truth"]["sha256"] is None
        assert "NOT" in fingerprint["ground_truth"]["reason"]


class TestCompareGatesOnThePinNotTheFileBytes:
    """`questions_pin` says which frozen set was answered. The file hash says
    whether the file was touched. Gating on the second withheld the entire
    delta in precisely the case the pin was introduced to fix."""

    def _fingerprint(self, *, pin="p" * 64, file_sha="q" * 64, gt="g" * 64):
        fingerprint = {
            "content": {"sha256": "f" * 64},
            "questions_sha256": file_sha,
            "harness_git_sha": "deadbeef",
            "ground_truth": {"sha256": gt, "files": 3},
        }
        if pin is not None:
            fingerprint["questions_pin"] = pin
        return fingerprint

    def test_same_pin_different_file_bytes_stays_comparable(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(file_sha="a" * 64), self._fingerprint(file_sha="b" * 64)
        )
        assert comparable is True
        assert any("questions_pin is identical" in line for line in verdicts)
        assert not any("QUESTIONS CHANGED" in line for line in verdicts)

    def test_a_different_pin_is_not_comparable(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(pin="1" * 64), self._fingerprint(pin="2" * 64)
        )
        assert comparable is False
        assert any("QUESTIONS CHANGED" in line for line in verdicts)

    def test_a_run_without_a_pin_falls_back_to_the_file_hash(self):
        """An artifact that cannot say which set it answered is not comparable
        on anybody's word, so the older, blunter gate still applies there."""
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(pin=None, file_sha="a" * 64),
            self._fingerprint(pin=None, file_sha="b" * 64),
        )
        assert comparable is False
        assert any("predates questions_pin" in line for line in verdicts)

    def test_a_moved_ground_truth_cache_is_noted_but_not_a_gate(self):
        comparable, verdicts = sb.compare_fingerprints(
            self._fingerprint(gt="1" * 64), self._fingerprint(gt="2" * 64)
        )
        assert comparable is True
        assert any("ground-truth cache differs" in line for line in verdicts)

    def test_a_missing_ground_truth_fingerprint_is_noted(self):
        current = self._fingerprint()
        current.pop("ground_truth")
        comparable, verdicts = sb.compare_fingerprints(current, self._fingerprint())
        assert comparable is True
        assert any("ground-truth cache fingerprint unavailable" in line for line in verdicts)
