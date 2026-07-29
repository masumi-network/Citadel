"""Durable storage for public /contact enquiries (ADR-0013, amended).

Deliberately NOT the vault. ADR-0013 rejected landing enquiries as Structured
Knowledge and that rejection stands: unauthenticated public text must never
reach the substrate agents read as authority. This is a plain capped JSON file
on the state volume, sitting beside the access store, readable only by an admin.

It exists because the Google Chat gateway is unconfigured, so every enquiry was
answering 503 and vanishing. ADR-0013's own rule is that an enquiry is never
accepted into a void; a file on disk is not a void, and a 503 shown to a partner
who then gives up is a worse outcome than one that fails to reach Chat.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Enough that nothing is lost between someone reading the queue, small enough
# that an unauthenticated endpoint cannot grow the state volume without bound.
# The per-IP and global rate limits in kb/server.py are the first cap; this is
# the backstop if those are defeated by a spread of source addresses.
MAX_STORED_ENQUIRIES = 500


class ContactStore:
    """Append-only, capped, newest-last store of partner enquiries."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt file must not take the public endpoint down with it.
            logger.exception("Contact store unreadable at %s; treating as empty", self.path)
            return []
        entries = raw.get("enquiries") if isinstance(raw, dict) else None
        return entries if isinstance(entries, list) else []

    def append(self, entry: dict[str, Any]) -> None:
        """Persist one enquiry. Raises on write failure so the caller can 503."""
        entries = self._load()
        entries.append(entry)
        if len(entries) > MAX_STORED_ENQUIRIES:
            entries = entries[-MAX_STORED_ENQUIRIES:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump({"enquiries": entries}, handle, indent=2)
            handle.write("\n")
        temp_path.replace(self.path)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, for the admin view."""
        entries = self._load()
        return list(reversed(entries[-max(1, limit) :]))

    def count(self) -> int:
        return len(self._load())
