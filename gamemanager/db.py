from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gamemanager.models import RootFolder, TagRule


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS root_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tag_rules (
                    canonical_tag TEXT PRIMARY KEY,
                    display_tag TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('approved', 'non_tag')),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ui_prefs (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tag_candidates (
                    canonical_tag TEXT PRIMARY KEY,
                    observed_tag TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    example_name TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                """
            )

    def list_roots(self) -> list[RootFolder]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path, enabled, added_at FROM root_folders ORDER BY added_at ASC"
            ).fetchall()
        return [
            RootFolder(
                id=row["id"],
                path=row["path"],
                enabled=bool(row["enabled"]),
                added_at=row["added_at"],
            )
            for row in rows
        ]

    def add_root(self, path: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO root_folders(path, enabled, added_at)
                VALUES (?, 1, ?)
                """,
                (path, utc_now_iso()),
            )
        return cursor.rowcount > 0

    def remove_root(self, root_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM root_folders WHERE id = ?", (root_id,))

    def list_tag_rules(self, status: str | None = None) -> list[TagRule]:
        query = "SELECT canonical_tag, display_tag, status, updated_at FROM tag_rules"
        args: tuple[str, ...] = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        query += " ORDER BY canonical_tag ASC"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            TagRule(
                canonical_tag=row["canonical_tag"],
                display_tag=row["display_tag"],
                status=row["status"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def upsert_tag_rule(self, canonical_tag: str, display_tag: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tag_rules(canonical_tag, display_tag, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(canonical_tag) DO UPDATE SET
                  display_tag=excluded.display_tag,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (canonical_tag, display_tag, status, utc_now_iso()),
            )

    def get_ui_pref(self, key: str, default: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM ui_prefs WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_ui_pref(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ui_prefs(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def replace_tag_candidates(
        self, rows: list[tuple[str, str, int, str]]
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM tag_candidates")
            conn.executemany(
                """
                INSERT INTO tag_candidates(
                    canonical_tag, observed_tag, count, example_name, last_seen
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [(c, o, n, e, now) for c, o, n, e in rows],
            )
