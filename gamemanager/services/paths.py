from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def project_data_dir() -> Path:
    override = os.environ.get("GAMEMANAGER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return project_root() / ".gamemanager_data"

