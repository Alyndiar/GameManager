from __future__ import annotations

import argparse
import json
import sys


def _gpu_status_short_label(status: str) -> str:
    token = status.strip()
    lowered = token.casefold()
    if not token:
        return "N/A"
    if "disabled" in lowered:
        return "Off"
    if "gpu" in lowered or "cuda" in lowered:
        return "GPU"
    if "cpu" in lowered:
        return "CPU"
    if "unavailable" in lowered or "missing" in lowered or "error" in lowered:
        return "Off"
    return token


def _torch_cuda_status() -> tuple[str, str]:
    try:
        import torch
    except Exception as exc:
        return ("Unavailable", f"torch: unavailable ({exc})")
    try:
        has_cuda = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if has_cuda else 0
        short = "GPU" if has_cuda and count > 0 else "CPU"
        detail = (
            f"torch {torch.__version__}: {short}"
            f"{f' ({count} device)' if count == 1 else f' ({count} devices)' if count > 1 else ''}"
        )
        return (short, detail)
    except Exception as exc:
        return ("Unavailable", f"torch: status error ({exc})")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--bg-engine", default="none")
    return parser.parse_args(argv)


def _worker_main(bg_engine: str) -> int:
    from gamemanager.services.background_removal import background_removal_device_status
    from gamemanager.services.icon_pipeline import text_extraction_device_status

    cutout_status = background_removal_device_status(bg_engine)
    paddle_status = text_extraction_device_status("paddleocr")
    opencv_status = text_extraction_device_status("opencv_db")
    torch_short, torch_detail = _torch_cuda_status()
    bg_label_map = {
        "none": "Disabled",
        "rembg": "rembg",
        "bria_rmbg": "BRIA",
    }
    bg_label = bg_label_map.get(bg_engine.strip().casefold(), bg_engine.strip() or "none")
    summary = (
        "| GPU: "
        f"Cutout[{bg_label}]={_gpu_status_short_label(cutout_status)} | "
        f"Paddle={_gpu_status_short_label(paddle_status)} | "
        f"OpenCV-DB={_gpu_status_short_label(opencv_status)} | "
        f"Torch={_gpu_status_short_label(torch_short)}"
    )
    tooltip = "\n".join(
        [
            f"Cutout ({bg_label}): {cutout_status}",
            f"Text extraction (PaddleOCR): {paddle_status}",
            f"Text extraction (OpenCV DB): {opencv_status}",
            torch_detail,
        ]
    )
    payload = {
        "type": "gpu_status",
        "summary": summary,
        "tooltip": tooltip,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.worker:
        return 2
    try:
        return _worker_main(str(args.bg_engine or "none"))
    except Exception as exc:
        print(
            json.dumps(
                {"type": "error", "message": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

