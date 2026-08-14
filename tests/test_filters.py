from kb.filters import PreIngestFilter


def test_rejects_empty_input() -> None:
    decision = PreIngestFilter().check("   ")

    assert not decision.accepted
    assert decision.reason == "empty"


def test_rejects_tiny_non_path_input() -> None:
    decision = PreIngestFilter(min_chars=3).check("ok")

    assert not decision.accepted
    assert decision.reason == "too_short"


def test_accepts_file_like_input_even_when_short() -> None:
    decision = PreIngestFilter(min_chars=20).check("./x.md")

    assert decision.accepted


def test_rejects_excluded_path() -> None:
    decision = PreIngestFilter(exclude_patterns=("private/*",)).check("private/note.md")

    assert not decision.accepted
    assert decision.reason == "excluded_path"
