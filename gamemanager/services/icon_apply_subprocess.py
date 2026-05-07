from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from gamemanager.models import IconApplyResult


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def apply_folder_icon_in_subprocess(
    folder_path: Path,
    source_image: bytes,
    icon_name_hint: str,
    info_tip: str | None,
    icon_style: str,
    bg_removal_engine: str,
    bg_removal_params: dict[str, object] | None,
    text_preserve_config: dict[str, object] | None,
    border_shader: dict[str, object] | None,
    background_fill_mode: str,
    background_fill_params: dict[str, object] | None,
    size_improvements: dict[int, dict[str, object]] | None,
    temp_dir: Path,
    timeout_seconds: int = 180,
) -> IconApplyResult:
    temp_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    source_path = temp_dir / f"icon_apply_src_{token}.img"
    try:
        source_path.write_bytes(source_image)
    except OSError as exc:
        return IconApplyResult(
            folder_path=str(folder_path),
            status="failed",
            message=f"Could not prepare source image for icon apply: {exc}",
        )

    cmd = [
        sys.executable,
        "-m",
        "gamemanager.services.icon_apply_subprocess",
        "--worker",
        "--folder-path",
        str(folder_path),
        "--source-path",
        str(source_path),
        "--icon-name-hint",
        icon_name_hint,
        "--icon-style",
        icon_style or "none",
        "--bg-removal-engine",
        bg_removal_engine or "none",
        "--bg-removal-params-json",
        json.dumps(bg_removal_params or {}, ensure_ascii=False),
        "--text-preserve-config-json",
        json.dumps(text_preserve_config or {}, ensure_ascii=False),
        "--border-shader-json",
        json.dumps(border_shader or {}, ensure_ascii=False),
        "--background-fill-mode",
        str(background_fill_mode or "black"),
        "--background-fill-params-json",
        json.dumps(background_fill_params or {}, ensure_ascii=False),
        "--size-improvements-json",
        json.dumps(size_improvements or {}, ensure_ascii=False),
    ]
    if info_tip and info_tip.strip():
        cmd.extend(["--info-tip", info_tip.strip()])

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
            timeout=max(10, int(timeout_seconds)),
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        return IconApplyResult(
            folder_path=str(folder_path),
            status="failed",
            message=f"Icon apply timed out after {exc.timeout} seconds.",
        )
    except OSError as exc:
        return IconApplyResult(
            folder_path=str(folder_path),
            status="failed",
            message=f"Could not launch icon apply worker: {exc}",
        )
    finally:
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            pass

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
            return IconApplyResult(
                folder_path=str(parsed.get("folder_path") or folder_path),
                status=str(parsed.get("status") or "failed"),
                message=str(parsed.get("message") or "Icon apply worker returned no message."),
                ico_path=parsed.get("ico_path"),
                desktop_ini_path=parsed.get("desktop_ini_path"),
            )

    detail = stderr or stdout or f"worker exit code {proc.returncode}"
    return IconApplyResult(
        folder_path=str(folder_path),
        status="failed",
        message=f"Icon apply worker failed: {detail}",
    )


def _worker_main(args: argparse.Namespace) -> int:
    from gamemanager.services.folder_icons import apply_folder_icon
    from gamemanager.services.icon_pipeline import build_multi_size_ico

    folder = Path(args.folder_path)
    source = Path(args.source_path)

    try:
        source_bytes = source.read_bytes()
        ico_payload = build_multi_size_ico(
            source_bytes,
            icon_style=args.icon_style,
            bg_removal_engine=args.bg_removal_engine,
            bg_removal_params=json.loads(args.bg_removal_params_json or "{}"),
            text_preserve_config=json.loads(args.text_preserve_config_json or "{}"),
            border_shader=json.loads(args.border_shader_json or "{}"),
            background_fill_mode=args.background_fill_mode,
            background_fill_params=json.loads(args.background_fill_params_json or "{}"),
            size_improvements=json.loads(args.size_improvements_json or "{}"),
        )
        result = apply_folder_icon(
            folder_path=folder,
            icon_bytes=ico_payload,
            icon_name_hint=args.icon_name_hint,
            info_tip=args.info_tip,
        )
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    except Exception as exc:
        failed = IconApplyResult(
            folder_path=str(folder),
            status="failed",
            message=str(exc),
        )
        print(json.dumps(asdict(failed), ensure_ascii=False))
        return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--folder-path", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--icon-name-hint", default="")
    parser.add_argument("--icon-style", default="none")
    parser.add_argument("--bg-removal-engine", default="none")
    parser.add_argument("--bg-removal-params-json", default="{}")
    parser.add_argument("--text-preserve-config-json", default="{}")
    parser.add_argument("--border-shader-json", default="{}")
    parser.add_argument("--background-fill-mode", default="black")
    parser.add_argument("--background-fill-params-json", default="{}")
    parser.add_argument("--size-improvements-json", default="{}")
    parser.add_argument("--info-tip", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.worker:
        return 2
    return _worker_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
