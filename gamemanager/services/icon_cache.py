from __future__ import annotations

import hashlib
import os
from pathlib import Path


class DiskImageCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str, extension: str = ".bin") -> Path:
        safe_key = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{safe_key}{extension}"

    def read(self, key: str, extension: str = ".bin") -> bytes | None:
        path = self._path_for(key, extension)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def write(self, key: str, payload: bytes, extension: str = ".bin") -> Path:
        path = self._path_for(key, extension)
        path.write_bytes(payload)
        return path

    def remove(self, key: str, extension: str = ".bin") -> None:
        path = self._path_for(key, extension)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def clear(self) -> None:
        try:
            for child in self.cache_dir.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
        except OSError:
            return


def icon_cache_key(icon_path: str, size: int) -> str:
    try:
        mtime = os.path.getmtime(icon_path)
    except OSError:
        mtime = 0.0
    return f"folder-icon:{os.path.normcase(icon_path)}:{mtime}:{size}"
