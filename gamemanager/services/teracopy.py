from __future__ import annotations

import os
import shutil
from pathlib import Path


DEFAULT_TERACOPY_PATH = r"C:\Program Files\TeraCopy\TeraCopy.exe"
FALLBACK_TERACOPY_PATHS = [
    DEFAULT_TERACOPY_PATH,
    r"C:\Program Files (x86)\TeraCopy\TeraCopy.exe",
]


def resolve_teracopy_path(preferred_path: str | None = None) -> str | None:
    candidates: list[Path] = []
    if preferred_path and preferred_path.strip():
        candidates.append(Path(preferred_path.strip()))
    for candidate in FALLBACK_TERACOPY_PATHS:
        path = Path(candidate)
        if path not in candidates:
            candidates.append(path)

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return os.path.normpath(str(path))
        except OSError:
            continue

    for executable in ("TeraCopy.exe", "teracopy.exe"):
        found = shutil.which(executable)
        if found:
            return os.path.normpath(found)
    return None

