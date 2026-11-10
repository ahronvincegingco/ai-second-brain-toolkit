"""Python 3.11 portability helpers and polyfills."""
from __future__ import annotations

import os
from pathlib import Path

# Polyfill Path.is_junction on Python < 3.12 (standardized in Python 3.12)
if not hasattr(Path, "is_junction"):
    def _is_junction(self: Path) -> bool:
        try:
            st = os.lstat(self)
            tag = getattr(st, "st_reparse_tag", 0)
            return bool(tag == 0xA0000003 or (tag & 0x20000000))
        except (OSError, ValueError):
            return False

    Path.is_junction = _is_junction  # type: ignore[attr-defined]

if not hasattr(os.path, "isjunction"):
    def _os_isjunction(path: str | bytes | os.PathLike) -> bool:
        try:
            st = os.lstat(path)
            tag = getattr(st, "st_reparse_tag", 0)
            return bool(tag == 0xA0000003 or (tag & 0x20000000))
        except (OSError, ValueError):
            return False

    os.path.isjunction = _os_isjunction  # type: ignore[attr-defined]
