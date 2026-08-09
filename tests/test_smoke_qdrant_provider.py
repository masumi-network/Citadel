from __future__ import annotations

from pathlib import Path
import sqlite3

from scripts.smoke_qdrant_provider import _backup_sqlite


def test_online_sqlite_backup_restores_lifecycle_database(tmp_path: Path) -> None:
    source_path = tmp_path / "lifecycle.sqlite3"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE source_revisions (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO source_revisions VALUES ('source-1')")
        connection.execute("PRAGMA user_version = 1")
    backup_root = tmp_path / "backup"
    backup_root.mkdir()

    result = _backup_sqlite(
        source_path,
        backup_root,
        name="lifecycle",
    )

    assert result["integrity"] == "ok"
    assert result["row_counts"] == {"source_revisions": 1}
    assert Path(result["backup"]).name == "lifecycle.sqlite"
    assert Path(result["restored"]).name == "lifecycle-restored.sqlite"
