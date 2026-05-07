from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from gamemanager.models import RootFolder, StoreAccount, StoreOwnedGame, TagRule


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

                CREATE TABLE IF NOT EXISTS game_infotips (
                    cleaned_name_key TEXT PRIMARY KEY,
                    cleaned_name TEXT NOT NULL,
                    info_tip TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sgdb_game_bindings (
                    folder_path TEXT PRIMARY KEY,
                    game_id INTEGER NOT NULL,
                    game_name TEXT NOT NULL,
                    last_confidence REAL NOT NULL DEFAULT 0.0,
                    evidence_json TEXT NOT NULL DEFAULT '',
                    confirmed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sgdb_upload_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder_path TEXT NOT NULL,
                    game_id INTEGER NOT NULL,
                    icon_fingerprint256 TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_sgdb_upload_history_folder
                    ON sgdb_upload_history(folder_path);
                CREATE INDEX IF NOT EXISTS idx_sgdb_upload_history_lookup
                    ON sgdb_upload_history(folder_path, game_id, icon_fingerprint256, status);

                CREATE TABLE IF NOT EXISTS folder_metadata (
                    folder_path TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(folder_path, key)
                );
                CREATE INDEX IF NOT EXISTS idx_folder_metadata_path
                    ON folder_metadata(folder_path);

                CREATE TABLE IF NOT EXISTS store_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    auth_kind TEXT NOT NULL DEFAULT 'unknown',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_sync_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE(store_name, account_id)
                );

                CREATE TABLE IF NOT EXISTS store_tokens_meta (
                    store_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    expires_utc TEXT NOT NULL DEFAULT '',
                    scopes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(store_name, account_id)
                );

                CREATE TABLE IF NOT EXISTS store_owned_games (
                    store_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    entitlement_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    store_game_id TEXT NOT NULL DEFAULT '',
                    manifest_id TEXT NOT NULL DEFAULT '',
                    launch_uri TEXT NOT NULL DEFAULT '',
                    install_path TEXT NOT NULL DEFAULT '',
                    is_installed INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '',
                    last_seen_utc TEXT NOT NULL,
                    PRIMARY KEY(store_name, account_id, entitlement_id)
                );

                CREATE TABLE IF NOT EXISTS store_links (
                    inventory_path TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    entitlement_id TEXT NOT NULL,
                    match_method TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    verified INTEGER NOT NULL DEFAULT 1,
                    last_verified_utc TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(inventory_path, store_name, account_id, entitlement_id)
                );

                CREATE TABLE IF NOT EXISTS store_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    started_utc TEXT NOT NULL,
                    completed_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    imported_count INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS store_link_rebuild_state (
                    inventory_path TEXT PRIMARY KEY,
                    name_sig TEXT NOT NULL DEFAULT '',
                    store_ids_sig_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_store_accounts_enabled
                    ON store_accounts(enabled, store_name);
                CREATE INDEX IF NOT EXISTS idx_store_owned_games_title
                    ON store_owned_games(store_name, title);
                CREATE INDEX IF NOT EXISTS idx_store_owned_games_ids
                    ON store_owned_games(store_name, store_game_id, manifest_id);
                CREATE INDEX IF NOT EXISTS idx_store_links_inventory
                    ON store_links(inventory_path, verified);
                CREATE INDEX IF NOT EXISTS idx_store_link_rebuild_state_updated
                    ON store_link_rebuild_state(updated_at);
                CREATE INDEX IF NOT EXISTS idx_store_sync_runs_store
                    ON store_sync_runs(store_name, account_id, id DESC);
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

    def get_game_infotip(self, cleaned_name: str) -> tuple[str, str] | None:
        key = cleaned_name.strip().casefold()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT info_tip, source
                FROM game_infotips
                WHERE cleaned_name_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["info_tip"] or ""), str(row["source"] or "")

    def upsert_game_infotip(self, cleaned_name: str, info_tip: str, source: str) -> None:
        key = cleaned_name.strip().casefold()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO game_infotips(
                    cleaned_name_key, cleaned_name, info_tip, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cleaned_name_key) DO UPDATE SET
                  cleaned_name=excluded.cleaned_name,
                  info_tip=excluded.info_tip,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (
                    key,
                    cleaned_name.strip(),
                    info_tip.strip(),
                    source.strip() or "unknown",
                    utc_now_iso(),
                ),
            )

    def get_sgdb_binding(self, folder_path: str) -> sqlite3.Row | None:
        folder = folder_path.strip()
        if not folder:
            return None
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT folder_path, game_id, game_name, last_confidence,
                       evidence_json, confirmed_at, updated_at
                FROM sgdb_game_bindings
                WHERE folder_path = ?
                """,
                (folder,),
            ).fetchone()

    def upsert_sgdb_binding(
        self,
        folder_path: str,
        game_id: int,
        game_name: str,
        last_confidence: float,
        evidence_json: str,
    ) -> None:
        folder = folder_path.strip()
        if not folder:
            return
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sgdb_game_bindings(
                    folder_path, game_id, game_name, last_confidence,
                    evidence_json, confirmed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(folder_path) DO UPDATE SET
                    game_id=excluded.game_id,
                    game_name=excluded.game_name,
                    last_confidence=excluded.last_confidence,
                    evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (
                    folder,
                    int(game_id),
                    game_name.strip(),
                    float(last_confidence),
                    evidence_json.strip(),
                    now,
                    now,
                ),
            )

    def add_sgdb_upload_history(
        self,
        folder_path: str,
        game_id: int,
        icon_fingerprint256: str,
        status: str,
        note: str = "",
    ) -> None:
        folder = folder_path.strip()
        fingerprint = icon_fingerprint256.strip().casefold()
        if not folder or not fingerprint:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sgdb_upload_history(
                    folder_path, game_id, icon_fingerprint256, uploaded_at, status, note
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    folder,
                    int(game_id),
                    fingerprint,
                    utc_now_iso(),
                    status.strip() or "unknown",
                    note.strip(),
                ),
            )

    def was_sgdb_icon_uploaded(
        self,
        folder_path: str,
        game_id: int,
        icon_fingerprint256: str,
    ) -> bool:
        folder = folder_path.strip()
        fingerprint = icon_fingerprint256.strip().casefold()
        if not folder or not fingerprint:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM sgdb_upload_history
                WHERE folder_path = ?
                  AND game_id = ?
                  AND icon_fingerprint256 = ?
                  AND status = 'uploaded'
                LIMIT 1
                """,
                (folder, int(game_id), fingerprint),
            ).fetchone()
        return row is not None

    def latest_sgdb_upload_for_folder(self, folder_path: str) -> sqlite3.Row | None:
        folder = folder_path.strip()
        if not folder:
            return None
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, folder_path, game_id, icon_fingerprint256, uploaded_at, status, note
                FROM sgdb_upload_history
                WHERE folder_path = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (folder,),
            ).fetchone()

    @staticmethod
    def _norm_inventory_path(path: str) -> str:
        token = str(path or "").strip()
        if not token:
            return ""
        return os.path.normcase(os.path.normpath(os.path.abspath(token)))

    def list_store_accounts(self, *, enabled_only: bool = False) -> list[StoreAccount]:
        query = (
            "SELECT store_name, account_id, display_name, auth_kind, enabled, "
            "created_at, updated_at, last_sync_utc FROM store_accounts"
        )
        args: tuple[object, ...] = ()
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY store_name ASC, account_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            StoreAccount(
                store_name=str(row["store_name"] or ""),
                account_id=str(row["account_id"] or ""),
                display_name=str(row["display_name"] or ""),
                auth_kind=str(row["auth_kind"] or "unknown"),
                enabled=bool(row["enabled"]),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                last_sync_utc=str(row["last_sync_utc"] or ""),
            )
            for row in rows
        ]

    def upsert_store_account(
        self,
        store_name: str,
        account_id: str,
        display_name: str,
        auth_kind: str,
        *,
        enabled: bool = True,
        last_sync_utc: str = "",
    ) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO store_accounts(
                    store_name, account_id, display_name, auth_kind,
                    enabled, created_at, updated_at, last_sync_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_name, account_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    auth_kind=excluded.auth_kind,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at,
                    last_sync_utc=excluded.last_sync_utc
                """,
                (
                    store,
                    account,
                    str(display_name or "").strip(),
                    str(auth_kind or "unknown").strip() or "unknown",
                    1 if enabled else 0,
                    now,
                    now,
                    str(last_sync_utc or "").strip(),
                ),
            )

    def read_folder_metadata(self, folder_path: str) -> dict[str, str]:
        normalized = self._norm_inventory_path(folder_path)
        if not normalized:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT key, value
                FROM folder_metadata
                WHERE folder_path = ?
                ORDER BY key ASC
                """,
                (normalized,),
            ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            key = str(row["key"] or "").strip()
            value = str(row["value"] or "").strip()
            if key and value:
                out[key] = value
        return out

    def replace_folder_metadata(
        self,
        folder_path: str,
        metadata: dict[str, str],
    ) -> None:
        normalized = self._norm_inventory_path(folder_path)
        if not normalized:
            return
        clean: dict[str, str] = {}
        for key, value in dict(metadata or {}).items():
            key_token = str(key or "").strip()
            value_token = str(value or "").strip()
            if key_token and value_token:
                clean[key_token] = value_token
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM folder_metadata WHERE folder_path = ?",
                (normalized,),
            )
            if clean:
                conn.executemany(
                    """
                    INSERT INTO folder_metadata(folder_path, key, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (normalized, key, value, now)
                        for key, value in sorted(clean.items())
                    ],
                )

    def set_store_account_enabled(
        self,
        store_name: str,
        account_id: str,
        enabled: bool,
    ) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE store_accounts
                SET enabled = ?, updated_at = ?
                WHERE store_name = ? AND account_id = ?
                """,
                (1 if enabled else 0, utc_now_iso(), store, account),
            )

    def delete_store_account(self, store_name: str, account_id: str) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM store_tokens_meta WHERE store_name = ? AND account_id = ?",
                (store, account),
            )
            conn.execute(
                "DELETE FROM store_owned_games WHERE store_name = ? AND account_id = ?",
                (store, account),
            )
            conn.execute(
                "DELETE FROM store_links WHERE store_name = ? AND account_id = ?",
                (store, account),
            )
            conn.execute(
                "DELETE FROM store_accounts WHERE store_name = ? AND account_id = ?",
                (store, account),
            )

    def upsert_store_token_meta(
        self,
        store_name: str,
        account_id: str,
        *,
        expires_utc: str = "",
        scopes: str = "",
        status: str = "",
    ) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO store_tokens_meta(
                    store_name, account_id, expires_utc, scopes, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_name, account_id) DO UPDATE SET
                    expires_utc=excluded.expires_utc,
                    scopes=excluded.scopes,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    store,
                    account,
                    str(expires_utc or "").strip(),
                    str(scopes or "").strip(),
                    str(status or "").strip(),
                    utc_now_iso(),
                ),
            )

    def replace_store_owned_games_for_account(
        self,
        store_name: str,
        account_id: str,
        rows: list[StoreOwnedGame],
    ) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM store_owned_games WHERE store_name = ? AND account_id = ?",
                (store, account),
            )
            conn.executemany(
                """
                INSERT INTO store_owned_games(
                    store_name, account_id, entitlement_id, title, store_game_id,
                    manifest_id, launch_uri, install_path, is_installed,
                    metadata_json, last_seen_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        store,
                        account,
                        str(row.entitlement_id or "").strip(),
                        str(row.title or "").strip(),
                        str(row.store_game_id or "").strip(),
                        str(row.manifest_id or "").strip(),
                        str(row.launch_uri or "").strip(),
                        str(row.install_path or "").strip(),
                        1 if bool(row.is_installed) else 0,
                        str(row.metadata_json or "").strip(),
                        str(row.last_seen_utc or "").strip() or now,
                    )
                    for row in rows
                    if str(row.entitlement_id or "").strip() and str(row.title or "").strip()
                ],
            )
            conn.execute(
                """
                UPDATE store_accounts
                SET last_sync_utc = ?, updated_at = ?
                WHERE store_name = ? AND account_id = ?
                """,
                (now, now, store, account),
            )

    def list_store_owned_games(
        self,
        store_name: str,
        account_id: str,
    ) -> list[StoreOwnedGame]:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT store_name, account_id, entitlement_id, title, store_game_id,
                       manifest_id, launch_uri, install_path, is_installed,
                       metadata_json, last_seen_utc
                FROM store_owned_games
                WHERE store_name = ? AND account_id = ?
                ORDER BY title ASC
                """,
                (store, account),
            ).fetchall()
        return [
            StoreOwnedGame(
                store_name=str(row["store_name"] or ""),
                account_id=str(row["account_id"] or ""),
                entitlement_id=str(row["entitlement_id"] or ""),
                title=str(row["title"] or ""),
                store_game_id=str(row["store_game_id"] or ""),
                manifest_id=str(row["manifest_id"] or ""),
                launch_uri=str(row["launch_uri"] or ""),
                install_path=str(row["install_path"] or ""),
                is_installed=bool(row["is_installed"]),
                metadata_json=str(row["metadata_json"] or ""),
                last_seen_utc=str(row["last_seen_utc"] or ""),
            )
            for row in rows
        ]

    def upsert_store_link(
        self,
        *,
        inventory_path: str,
        store_name: str,
        account_id: str,
        entitlement_id: str,
        match_method: str,
        confidence: float,
        verified: bool,
        notes: str = "",
    ) -> None:
        path = self._norm_inventory_path(inventory_path)
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        entitlement = str(entitlement_id or "").strip()
        if not path or not store or not account or not entitlement:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO store_links(
                    inventory_path, store_name, account_id, entitlement_id,
                    match_method, confidence, verified, last_verified_utc, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inventory_path, store_name, account_id, entitlement_id)
                DO UPDATE SET
                    match_method=excluded.match_method,
                    confidence=excluded.confidence,
                    verified=excluded.verified,
                    last_verified_utc=excluded.last_verified_utc,
                    notes=excluded.notes
                """,
                (
                    path,
                    store,
                    account,
                    entitlement,
                    str(match_method or "unknown").strip() or "unknown",
                    float(confidence),
                    1 if verified else 0,
                    utc_now_iso(),
                    str(notes or "").strip(),
                ),
            )

    def delete_store_links_for_inventory_path(
        self,
        inventory_path: str,
        *,
        include_manual: bool = True,
    ) -> int:
        path = self._norm_inventory_path(inventory_path)
        if not path:
            return 0
        with self._connect() as conn:
            if include_manual:
                cursor = conn.execute(
                    "DELETE FROM store_links WHERE inventory_path = ?",
                    (path,),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM store_links WHERE inventory_path = ? AND account_id <> '__manual__'",
                    (path,),
                )
        return int(cursor.rowcount or 0)

    def delete_store_links_for_inventory_store(
        self,
        inventory_path: str,
        store_name: str,
        *,
        include_manual: bool = True,
    ) -> int:
        path = self._norm_inventory_path(inventory_path)
        store = str(store_name or "").strip()
        if not path or not store:
            return 0
        with self._connect() as conn:
            if include_manual:
                cursor = conn.execute(
                    """
                    DELETE FROM store_links
                    WHERE inventory_path = ? AND store_name = ?
                    """,
                    (path, store),
                )
            else:
                cursor = conn.execute(
                    """
                    DELETE FROM store_links
                    WHERE inventory_path = ? AND store_name = ? AND account_id <> '__manual__'
                    """,
                    (path, store),
                )
        return int(cursor.rowcount or 0)

    def delete_store_link(
        self,
        *,
        inventory_path: str,
        store_name: str,
        account_id: str,
        entitlement_id: str,
    ) -> int:
        path = self._norm_inventory_path(inventory_path)
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        entitlement = str(entitlement_id or "").strip()
        if not path or not store or not account or not entitlement:
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM store_links
                WHERE inventory_path = ?
                  AND store_name = ?
                  AND account_id = ?
                  AND entitlement_id = ?
                """,
                (path, store, account, entitlement),
            )
        return int(cursor.rowcount or 0)

    def list_store_links_for_paths(
        self,
        paths: list[str],
        *,
        verified_only: bool = True,
    ) -> dict[str, list[str]]:
        normalized = [self._norm_inventory_path(path) for path in paths]
        normalized = [value for value in normalized if value]
        if not normalized:
            return {}
        out: dict[str, set[str]] = {path: set() for path in normalized}
        chunks: list[list[str]] = [
            normalized[idx : idx + 400] for idx in range(0, len(normalized), 400)
        ]
        with self._connect() as conn:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    "SELECT inventory_path, store_name FROM store_links "
                    f"WHERE inventory_path IN ({placeholders})"
                )
                args: list[object] = list(chunk)
                if verified_only:
                    query += " AND verified = 1"
                rows = conn.execute(query, tuple(args)).fetchall()
                for row in rows:
                    path = str(row["inventory_path"] or "")
                    store = str(row["store_name"] or "").strip()
                    if path and store:
                        out.setdefault(path, set()).add(store)
        return {path: sorted(values) for path, values in out.items() if values}

    def list_store_link_rows_for_paths(
        self,
        paths: list[str],
        *,
        include_manual: bool = False,
    ) -> dict[str, list[dict[str, object]]]:
        normalized = [self._norm_inventory_path(path) for path in paths]
        normalized = [value for value in normalized if value]
        if not normalized:
            return {}
        out: dict[str, list[dict[str, object]]] = {path: [] for path in normalized}
        chunks: list[list[str]] = [
            normalized[idx : idx + 300] for idx in range(0, len(normalized), 300)
        ]
        with self._connect() as conn:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    "SELECT inventory_path, store_name, account_id, entitlement_id, "
                    "match_method, confidence, verified, notes "
                    "FROM store_links "
                    f"WHERE inventory_path IN ({placeholders})"
                )
                args: list[object] = list(chunk)
                if not include_manual:
                    query += " AND account_id <> '__manual__'"
                rows = conn.execute(query, tuple(args)).fetchall()
                for row in rows:
                    path = str(row["inventory_path"] or "")
                    if not path:
                        continue
                    out.setdefault(path, []).append(
                        {
                            "inventory_path": path,
                            "store_name": str(row["store_name"] or "").strip(),
                            "account_id": str(row["account_id"] or "").strip(),
                            "entitlement_id": str(row["entitlement_id"] or "").strip(),
                            "match_method": str(row["match_method"] or "").strip(),
                            "confidence": float(row["confidence"] or 0.0),
                            "verified": bool(row["verified"]),
                            "notes": str(row["notes"] or "").strip(),
                        }
                    )
        return out

    def list_store_link_rebuild_state_for_paths(
        self,
        paths: list[str],
    ) -> dict[str, dict[str, str]]:
        normalized = [self._norm_inventory_path(path) for path in paths]
        normalized = [value for value in normalized if value]
        if not normalized:
            return {}
        out: dict[str, dict[str, str]] = {}
        chunks: list[list[str]] = [
            normalized[idx : idx + 400] for idx in range(0, len(normalized), 400)
        ]
        with self._connect() as conn:
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    (
                        "SELECT inventory_path, name_sig, store_ids_sig_json "
                        "FROM store_link_rebuild_state "
                        f"WHERE inventory_path IN ({placeholders})"
                    ),
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    path = str(row["inventory_path"] or "")
                    if not path:
                        continue
                    out[path] = {
                        "name_sig": str(row["name_sig"] or ""),
                        "store_ids_sig_json": str(row["store_ids_sig_json"] or "{}"),
                    }
        return out

    def upsert_store_link_rebuild_state(
        self,
        *,
        inventory_path: str,
        name_sig: str,
        store_ids_sig_json: str,
    ) -> None:
        path = self._norm_inventory_path(inventory_path)
        if not path:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO store_link_rebuild_state(
                    inventory_path, name_sig, store_ids_sig_json, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(inventory_path) DO UPDATE SET
                    name_sig=excluded.name_sig,
                    store_ids_sig_json=excluded.store_ids_sig_json,
                    updated_at=excluded.updated_at
                """,
                (
                    path,
                    str(name_sig or "").strip(),
                    str(store_ids_sig_json or "{}").strip() or "{}",
                    utc_now_iso(),
                ),
            )

    def first_store_link_target(
        self,
        inventory_path: str,
        *,
        store_name: str = "",
    ) -> dict[str, str] | None:
        path = self._norm_inventory_path(inventory_path)
        if not path:
            return None
        store = str(store_name or "").strip()
        query = (
            "SELECT l.store_name, l.account_id, l.entitlement_id, "
            "og.store_game_id, og.title "
            "FROM store_links l "
            "LEFT JOIN store_owned_games og "
            "ON og.store_name = l.store_name "
            "AND og.account_id = l.account_id "
            "AND og.entitlement_id = l.entitlement_id "
            "WHERE l.inventory_path = ? AND l.verified = 1"
        )
        args: list[object] = [path]
        if store:
            query += " AND l.store_name = ?"
            args.append(store)
        query += (
            " ORDER BY CASE WHEN l.account_id = '__manual__' THEN 1 ELSE 0 END ASC, "
            "l.last_verified_utc DESC LIMIT 1"
        )
        with self._connect() as conn:
            row = conn.execute(query, tuple(args)).fetchone()
        if row is None:
            return None
        return {
            "store_name": str(row["store_name"] or "").strip(),
            "account_id": str(row["account_id"] or "").strip(),
            "entitlement_id": str(row["entitlement_id"] or "").strip(),
            "store_game_id": str(row["store_game_id"] or "").strip(),
            "title": str(row["title"] or "").strip(),
        }

    def list_store_link_targets_for_inventory(
        self,
        inventory_path: str,
        *,
        verified_only: bool = True,
    ) -> list[dict[str, str]]:
        path = self._norm_inventory_path(inventory_path)
        if not path:
            return []
        query = (
            "SELECT l.store_name, l.account_id, l.entitlement_id, "
            "og.store_game_id, og.title "
            "FROM store_links l "
            "LEFT JOIN store_owned_games og "
            "ON og.store_name = l.store_name "
            "AND og.account_id = l.account_id "
            "AND og.entitlement_id = l.entitlement_id "
            "WHERE l.inventory_path = ?"
        )
        args: list[object] = [path]
        if verified_only:
            query += " AND l.verified = 1"
        query += (
            " ORDER BY CASE WHEN l.account_id = '__manual__' THEN 1 ELSE 0 END ASC, "
            "l.last_verified_utc DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, tuple(args)).fetchall()
        out: list[dict[str, str]] = []
        for row in rows:
            out.append(
                {
                    "store_name": str(row["store_name"] or "").strip(),
                    "account_id": str(row["account_id"] or "").strip(),
                    "entitlement_id": str(row["entitlement_id"] or "").strip(),
                    "store_game_id": str(row["store_game_id"] or "").strip(),
                    "title": str(row["title"] or "").strip(),
                }
            )
        return out

    def add_store_sync_run(
        self,
        *,
        store_name: str,
        account_id: str,
        started_utc: str,
        completed_utc: str,
        status: str,
        duration_ms: int,
        imported_count: int,
        error_summary: str = "",
    ) -> None:
        store = str(store_name or "").strip()
        account = str(account_id or "").strip()
        if not store or not account:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO store_sync_runs(
                    store_name, account_id, started_utc, completed_utc,
                    status, duration_ms, imported_count, error_summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    store,
                    account,
                    str(started_utc or "").strip(),
                    str(completed_utc or "").strip(),
                    str(status or "unknown").strip() or "unknown",
                    max(0, int(duration_ms)),
                    max(0, int(imported_count)),
                    str(error_summary or "").strip(),
                ),
            )
