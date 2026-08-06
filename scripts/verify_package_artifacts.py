"""Verify the files that make a built Citadel distribution usable."""

from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path
import tarfile


def verify(dist_dir: Path) -> None:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError(
            f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
        )

    webui = files("kb").joinpath("webui")
    assert webui.joinpath("index.html").is_file()
    assert webui.joinpath("404.html").is_file()
    assert webui.joinpath("_next").is_dir()
    assert files("kb").joinpath("retrieval_eval.py").is_file()
    tokenizer_dir = files("kb").joinpath("data", "tiktoken-cache")
    tokenizer_files = [path for path in tokenizer_dir.iterdir() if path.is_file()]
    assert len(tokenizer_files) == 1
    assert tokenizer_files[0].name.endswith(".gz")

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = set(archive.getnames())
    assert any(name.endswith("/kb/webui/index.html") for name in names)
    assert any(name.endswith("/kb/retrieval_eval.py") for name in names)
    tokenizer_prefix = "/kb/data/tiktoken-cache/"
    assert any(
        tokenizer_prefix in name and name.endswith(".gz") for name in names
    )
    print("release artifact webui, benchmark, and tokenizer payload verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    verify(args.dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
