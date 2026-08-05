from __future__ import annotations

from kb import __version__
from kb.cli import _cli_version
from kb.server import app


def test_cli_and_server_use_the_package_source_version() -> None:
    assert __version__ == "0.5.0"
    assert _cli_version() == __version__
    assert app.version == __version__
