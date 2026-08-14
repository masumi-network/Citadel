from __future__ import annotations

from pathlib import Path
from typing import Iterable

from kb.capture_policy import path_is_denied
from kb.models import IngestDecision


class PreIngestFilter:
    def __init__(self, *, min_chars: int = 3, exclude_patterns: Iterable[str] = ()) -> None:
        self.min_chars = min_chars
        self.exclude_patterns = tuple(exclude_patterns)

    def check(self, data: str) -> IngestDecision:
        value = data.strip()
        if not value:
            return IngestDecision(False, "empty")

        if self._is_excluded_path(value):
            return IngestDecision(False, "excluded_path")

        if not self._looks_like_path(value) and len(value) < self.min_chars:
            return IngestDecision(False, "too_short")

        return IngestDecision(True)

    def _is_excluded_path(self, value: str) -> bool:
        return path_is_denied(value, self.exclude_patterns)

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        if value.startswith(("file://", "/", "./", "../", "~")):
            return True
        suffix = Path(value).suffix
        return bool(suffix and " " not in value)
