from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from gamemanager.services.icon_origin import (
    detect_sgdb_origin_by_visual,
    icon_fingerprint256_from_ico,
)
from gamemanager.services.icon_sources import (
    DEFAULT_STEAMGRIDDB_API_BASE,
    IconSearchSettings,
)
from gamemanager.services.steamgriddb_targeting import resolve_target_candidates


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _error_result(message: str) -> dict[str, object]:
    return {
        "status": "error",
        "error": str(message or "unknown error"),
    }


def _ok_result() -> dict[str, object]:
    return {
        "status": "ok",
        "source_kind": "web",
        "source_provider": "Internet",
        "source_confidence": 0.0,
        "source_note": "fallback",
        "source_fingerprint256": "",
    }


def probe_icon_source_in_subprocess(
    *,
    folder_path: str,
    icon_path: str,
    cleaned_name: str,
    full_name: str,
    source_game_id: str,
    sgdb: dict[str, object],
    threshold: float,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    payload = {
        "folder_path": str(folder_path or "").strip(),
        "icon_path": str(icon_path or "").strip(),
        "cleaned_name": str(cleaned_name or "").strip(),
        "full_name": str(full_name or "").strip(),
        "source_game_id": str(source_game_id or "").strip(),
        "sgdb": dict(sgdb or {}),
        "threshold": float(threshold),
    }
    cmd = [
        sys.executable,
        "-m",
        "gamemanager.services.icon_source_probe_subprocess",
        "--worker",
        "--payload-json",
        json.dumps(payload, ensure_ascii=False),
    ]
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
        )
    except subprocess.TimeoutExpired as exc:
        return _error_result(f"timed out after {exc.timeout} seconds")
    except OSError as exc:
        return _error_result(f"could not launch worker: {exc}")

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        for line in reversed(stdout.splitlines()):
            token = line.strip()
            if not token:
                continue
            try:
                parsed = json.loads(token)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return _error_result(stderr or stdout or f"worker exit code {proc.returncode}")


def _worker_probe(payload: dict[str, object]) -> dict[str, object]:
    result = _ok_result()
    folder_path = str(payload.get("folder_path") or "").strip()
    icon_path = str(payload.get("icon_path") or "").strip()
    cleaned_name = str(payload.get("cleaned_name") or "").strip()
    full_name = str(payload.get("full_name") or "").strip()
    source_game_id = str(payload.get("source_game_id") or "").strip()
    try:
        threshold = max(0.5, min(1.0, float(payload.get("threshold", 0.95))))
    except Exception:
        threshold = 0.95

    icon_file = Path(icon_path)
    if not folder_path:
        return _error_result("missing folder path")
    if not icon_path:
        return _error_result("missing icon path")
    if not icon_file.exists():
        return _error_result("icon file does not exist")
    if icon_file.suffix.casefold() != ".ico":
        return _error_result("icon is not .ico")

    try:
        result["source_fingerprint256"] = str(icon_fingerprint256_from_ico(icon_file))
    except Exception:
        result["source_fingerprint256"] = ""

    sgdb_payload = payload.get("sgdb")
    sgdb = dict(sgdb_payload) if isinstance(sgdb_payload, dict) else {}
    sgdb_enabled = bool(sgdb.get("enabled"))
    sgdb_api_key = str(sgdb.get("api_key") or "").strip()
    sgdb_api_base = str(sgdb.get("api_base") or "").strip()
    if not (sgdb_enabled and sgdb_api_key):
        return result

    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_base=sgdb_api_base or DEFAULT_STEAMGRIDDB_API_BASE,
        steamgriddb_api_key=sgdb_api_key,
    )

    game_id = int(source_game_id) if source_game_id.isdigit() else 0
    try:
        if game_id <= 0:
            candidates, _variants, exact_appid_game_id = resolve_target_candidates(
                settings,
                folder_path=folder_path,
                cleaned_name=cleaned_name,
                full_name=full_name,
            )
            if exact_appid_game_id is not None and int(exact_appid_game_id) > 0:
                game_id = int(exact_appid_game_id)
            elif candidates:
                game_id = int(candidates[0].game_id)
        if game_id <= 0:
            result["source_note"] = "fallback-no-game-id"
            return result
        visual = detect_sgdb_origin_by_visual(
            local_icon_path=icon_path,
            game_id=int(game_id),
            settings=settings,
            threshold=float(threshold),
        )
        confidence = float(getattr(visual, "confidence", 0.0) or 0.0)
        if confidence >= float(threshold):
            result["source_kind"] = "sgdb_raw"
            result["source_provider"] = "SteamGridDB"
            result["source_confidence"] = confidence
            result["source_note"] = "sgdb_visual_match"
            return result
        result["source_note"] = "fallback-sgdb-no-match"
        return result
    except Exception as exc:
        result["source_note"] = f"fallback-sgdb-error:{exc}"
        return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--payload-json", default="{}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.worker:
        return 2
    try:
        payload = json.loads(args.payload_json or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    result = _worker_probe(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
