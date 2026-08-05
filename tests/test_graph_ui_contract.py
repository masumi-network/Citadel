"""Static dashboard graph controls stay connected to both graph APIs."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "kb" / "static"


def test_graph_retry_control_retries_the_active_data_source() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    retry_handler = script[script.index('document.getElementById("meshRetryButton")') :]

    assert 'if (state.graphMode === "knowledge")' in retry_handler
    assert "loadKnowledgeGraph(true)" in retry_handler
    assert "loadMesh()" in retry_handler


def test_knowledge_graph_failures_surface_the_inline_retry_state() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    loader = script[script.index("async function loadKnowledgeGraph") : script.index("function setGraphMode")]

    assert "meshAlert.hidden = false" in loader
    assert "meshAlertText.textContent" in loader
    assert "Knowledge Mesh is busy. Retry in a moment." in loader


def test_index_ui_does_not_render_unmeasured_counts_as_zero() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "function formatIndexRecords(index)" in script
    assert 'return "Not measured"' in script
    assert 'knowledgeRecordCount.textContent = allRecordsMeasured ? String(recordCount) : "Not measured"' in script
