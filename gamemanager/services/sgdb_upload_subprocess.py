from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from gamemanager.services.icon_origin import normalized_icon_png_bytes
from gamemanager.services.icon_sources import (
    DEFAULT_STEAMGRIDDB_API_BASE,
    IconSearchSettings,
)
from gamemanager.services.steamgriddb_upload import upload_icon


_SGDB_API_KEY_ENV = "GAMEMANAGER_SGDB_API_KEY"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def upload_icon_to_sgdb_in_subprocess(
    settings: IconSearchSettings,
    game_id: int,
    icon_path: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    api_key = str(settings.steamgriddb_api_key or "").strip()
    if not api_key:
        return {"success": False, "error": "SteamGridDB API key is not configured."}
    cmd = [
        sys.executable,
        "-m",
        "gamemanager.services.sgdb_upload_subprocess",
        "--worker",
        "--icon-path",
        str(icon_path or "").strip(),
        "--game-id",
        str(int(game_id)),
        "--api-base",
        str(settings.steamgriddb_api_base or DEFAULT_STEAMGRIDDB_API_BASE).strip(),
        "--timeout-seconds",
        str(float(settings.timeout_seconds)),
    ]
    env = os.environ.copy()
    env[_SGDB_API_KEY_ENV] = api_key
    try:
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = int(subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(_project_root()),
            timeout=max(15, int(timeout_seconds)),
            creationflags=creationflags,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {"success": False, "error": f"Upload timed out after {exc.timeout} seconds."}
    except OSError as exc:
        return {"success": False, "error": f"Could not launch upload worker: {exc}"}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        for line in reversed(stdout.splitlines()):
            payload = line.strip()
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    detail = stderr or stdout or f"worker exit code {proc.returncode}"
    return {"success": False, "error": f"Upload worker failed: {detail}"}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--icon-path", default="")
    parser.add_argument("--game-id", default="0")
    parser.add_argument("--api-base", default=DEFAULT_STEAMGRIDDB_API_BASE)
    parser.add_argument("--timeout-seconds", default="15")
    return parser.parse_args(argv)


def _worker_main(args: argparse.Namespace) -> int:
    icon_path = str(args.icon_path or "").strip()
    game_id = int(str(args.game_id or "0"))
    api_key = str(os.environ.get(_SGDB_API_KEY_ENV, "")).strip()
    if not icon_path:
        print(json.dumps({"success": False, "error": "Missing icon path."}, ensure_ascii=False))
        return 0
    if game_id <= 0:
        print(json.dumps({"success": False, "error": "Invalid game id."}, ensure_ascii=False))
        return 0
    if not api_key:
        print(json.dumps({"success": False, "error": "Missing API key."}, ensure_ascii=False))
        return 0
    try:
        timeout_value = float(args.timeout_seconds)
    except Exception:
        timeout_value = 15.0
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key=api_key,
        steamgriddb_api_base=str(args.api_base or DEFAULT_STEAMGRIDDB_API_BASE).strip(),
        timeout_seconds=max(3.0, timeout_value),
    )
    try:
        payload = normalized_icon_png_bytes(icon_path, size=256)
        upload_icon(settings, game_id, payload)
        print(json.dumps({"success": True}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.worker:
        return 2
    return _worker_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
