"""Minimal ``.env`` reader used by the local launcher.

The project intentionally avoids a third-party dotenv dependency. Values already
present in the process environment win over the file unless ``override=True`` is
requested, which matches how deployment environments usually override a local
developer file.
"""

from __future__ import annotations

import os
from pathlib import Path

_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse ``path`` and return its key/value pairs.

    Missing or unreadable files yield an empty mapping. Malformed lines are
    skipped instead of failing startup.
    """

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _is_valid_key(key):
            continue
        values[key] = _unquote(value.strip())
    return values


def load_env_file(
    path: str | Path,
    *,
    override: bool = False,
) -> dict[str, str]:
    """Load ``path`` into ``os.environ`` and return the parsed values.

    ``override=False`` keeps any value that is already exported in the process
    environment.
    """

    values = parse_env_file(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def _is_valid_key(key: str) -> bool:
    return bool(key) and key[0] not in "0123456789" and set(key) <= _KEY_CHARS


def _unquote(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {"'", '"'}:
        return value
    quote = value[0]
    inner = value[1:-1]
    if quote == "'":
        return inner
    return (
        inner.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
