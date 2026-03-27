from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path


def _remove_if_empty(path: Path) -> None:
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        return


def _merge_move_dir(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        src_path = src / entry.name
        dst_path = dst / entry.name
        if src_path.is_dir():
            _merge_move_dir(src_path, dst_path)
            _remove_if_empty(src_path)
            continue
        if dst_path.exists():
            try:
                if filecmp.cmp(src_path, dst_path, shallow=False):
                    src_path.unlink()
            except OSError:
                pass
            continue
        try:
            shutil.move(str(src_path), str(dst_path))
        except OSError:
            continue
    _remove_if_empty(src)


def configure_local_ml_model_cache(model_root: Path) -> None:
    model_root.mkdir(parents=True, exist_ok=True)
    paddleocr_root = model_root / "paddleocr"
    paddle_root = model_root / "paddle"
    xdg_root = model_root / "xdg"
    paddleocr_root.mkdir(parents=True, exist_ok=True)
    paddle_root.mkdir(parents=True, exist_ok=True)
    xdg_root.mkdir(parents=True, exist_ok=True)

    # Migrate legacy user-profile model stores into project-local data.
    home = Path.home()
    _merge_move_dir(home / ".paddleocr", paddleocr_root)
    _merge_move_dir(home / ".cache" / "paddle", paddle_root)

    os.environ.setdefault("PADDLE_OCR_BASE_DIR", str(paddleocr_root))
    os.environ.setdefault("PADDLE_HOME", str(paddle_root))
    os.environ.setdefault("XDG_CACHE_HOME", str(xdg_root))

