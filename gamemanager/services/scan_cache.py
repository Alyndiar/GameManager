from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class DirectorySizeCache:
    def __init__(self, cache_path: Path, max_entries: int = 200_000):
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_entries = max(1_000, int(max_entries))
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, int]] = {}
        self._loaded = False

    def _norm_key(self, path: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    def set_max_entries(self, value: int) -> None:
        with self._lock:
            self._max_entries = max(1_000, int(value))

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                return
            rows: dict[str, dict[str, int]] = {}
            for key, entry in payload.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                try:
                    mtime_ns = int(entry.get("m", -1))
                    size = int(entry.get("s", -1))
                    access = int(entry.get("a", 0))
                except (TypeError, ValueError):
                    continue
                if mtime_ns < 0 or size < 0:
                    continue
                rows[key] = {"m": mtime_ns, "s": size, "a": max(0, access)}
            self._rows = rows
            self._prune_locked()

    def get(self, path: str | Path, mtime_ns: int) -> int | None:
        self.load()
        key = self._norm_key(path)
        with self._lock:
            entry = self._rows.get(key)
            if entry is None:
                return None
            if int(entry.get("m", -1)) != int(mtime_ns):
                return None
            entry["a"] = int(time.time())
            return int(entry.get("s", 0))

    def put(self, path: str | Path, mtime_ns: int, size_bytes: int) -> None:
        self.load()
        key = self._norm_key(path)
        with self._lock:
            self._rows[key] = {
                "m": int(mtime_ns),
                "s": max(0, int(size_bytes)),
                "a": int(time.time()),
            }
            if len(self._rows) > self._max_entries:
                self._prune_locked()

    def save(self) -> None:
        self.load()
        with self._lock:
            self._prune_locked()
            payload = dict(self._rows)
        try:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            return

    def _prune_locked(self) -> None:
        if len(self._rows) <= self._max_entries:
            return
        sorted_keys = sorted(
            self._rows.keys(),
            key=lambda k: int(self._rows[k].get("a", 0)),
        )
        drop = len(self._rows) - self._max_entries
        for key in sorted_keys[:drop]:
            self._rows.pop(key, None)

