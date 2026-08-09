from __future__ import annotations

import logging
import os
import re

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
COGNEE_LOG_LEVEL_ENV = "CITADEL_COGNEE_LOG_LEVEL"
DEFAULT_COGNEE_LOG_LEVEL = "WARNING"

# Cognee 1.2.x and its task/retriever helpers emit high-volume INFO records.
# Keep the names explicit because some helpers log outside the ``cognee.*``
# namespace. WARNING and above still pass through to the hosted log sink.
COGNEE_LOGGER_NAMES = (
    "cognee",
    "cognee.shared.logging_utils",
    "run_tasks_base",
    "ChunksRetriever",
    "OntologyAdapter",
)

# Everything left in C0/C1 once the three that have readable escapes are handled.
# \x09 (tab) and \x0a (newline) are excluded from the range because the explicit
# replacements below have already consumed them.
_REMAINING_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Long enough for a dataset name, a path or a source id; short enough that no one
# value can push the rest of a line out of a log viewer.
DEFAULT_LOG_VALUE_LIMIT = 200


def safe_log_value(value: object, *, limit: int = DEFAULT_LOG_VALUE_LIMIT) -> str:
    """Render an untrusted value as one line of a log record.

    A log line is a record this project reads back as evidence. A value carrying
    ``\\n`` writes a second line that looks exactly like a real one, and a value
    carrying ``\\r`` can overwrite the first on a terminal. Callers reach this with
    request-supplied strings: dataset names, tags, paths, source ids.

    Escapes rather than strips, so ``"a\\nb"`` and ``"ab"`` stay distinguishable in
    the record, and bounds the result so a large value cannot bury the fields
    logged after it.
    """
    text = str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    text = text.replace("\t", "\\t")
    text = _REMAINING_CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)
    if len(text) > limit:
        return f"{text[:limit]}...(+{len(text) - limit} more characters)"
    return text


def resolve_log_level(value: str | None = None) -> str:
    level = (value or os.getenv("CITADEL_LOG_LEVEL") or "INFO").strip().upper()
    return level if level in VALID_LEVELS else "INFO"


def resolve_cognee_log_level(value: str | None = None) -> str:
    level = (value or os.getenv(COGNEE_LOG_LEVEL_ENV) or DEFAULT_COGNEE_LOG_LEVEL).strip().upper()
    return level if level in VALID_LEVELS else DEFAULT_COGNEE_LOG_LEVEL


def configure_cognee_logging(level: str | None = None) -> None:
    """Bound Cognee's noisy loggers without changing Citadel's root level.

    Cognee installs its structlog handler during import, so this is called after
    import and may safely run again for each client operation. An explicit
    ``CITADEL_COGNEE_LOG_LEVEL`` can restore INFO or DEBUG for diagnosis.
    """
    resolved = resolve_cognee_log_level(level)
    numeric_level = logging.getLevelNamesMapping()[resolved]
    for name in COGNEE_LOGGER_NAMES:
        logging.getLogger(name).setLevel(numeric_level)


def configure_logging(level: str | None = None) -> None:
    """Configure stdlib logging once at startup.

    Level comes from ``CITADEL_LOG_LEVEL`` (default INFO). Safe to call more than
    once: an already-configured root logger is left untouched except for level.
    """
    resolved = resolve_log_level(level)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=resolved, format=LOG_FORMAT)
    root.setLevel(resolved)
