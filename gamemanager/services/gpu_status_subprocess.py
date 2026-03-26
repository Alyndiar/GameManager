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


def _torch_vram_summary() -> tuple[str, str]:
    try:
        import torch
    except Exception:
        return ("", "VRAM unavailable (torch missing)")
    try:
        if not bool(torch.cuda.is_available()) or int(torch.cuda.device_count()) <= 0:
            return ("", "VRAM unavailable (CUDA not active)")
        props = torch.cuda.get_device_properties(0)
        total_bytes = int(getattr(props, "total_memory", 0) or 0)
        free_bytes = 0
        try:
            free_bytes, _total_bytes = torch.cuda.mem_get_info(0)
        except Exception:
            free_bytes = 0
        if total_bytes <= 0:
            return ("", "VRAM unavailable")
        total_gb = total_bytes / (1024**3)
        if free_bytes > 0:
            free_gb = free_bytes / (1024**3)
            used_gb = max(0.0, total_gb - free_gb)
            short = f"VRAM {used_gb:.1f}/{total_gb:.1f} GB"
            detail = (
                f"CUDA device 0 VRAM: used={used_gb:.2f} GB, "
                f"free={free_gb:.2f} GB, total={total_gb:.2f} GB"
            )
            return (short, detail)
        short = f"VRAM total {total_gb:.1f} GB"
        detail = f"CUDA device 0 VRAM total: {total_gb:.2f} GB"
        return (short, detail)
    except Exception as exc:
        return ("", f"VRAM status error ({exc})")


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
    vram_short, vram_detail = _torch_vram_summary()
    bg_label_map = {
        "none": "Disabled",
        "rembg": "rembg",
        "bria_rmbg": "BRIA",
    }
    bg_label = bg_label_map.get(bg_engine.strip().casefold(), bg_engine.strip() or "none")
    summary = (
        "GPU: "
        f"Cutout[{bg_label}]={_gpu_status_short_label(cutout_status)}  "
        f"Paddle={_gpu_status_short_label(paddle_status)}  "
        f"OpenCV-DB={_gpu_status_short_label(opencv_status)}  "
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
    if vram_detail:
        tooltip += f"\n{vram_detail}"
    payload = {
        "type": "gpu_status",
        "summary": summary,
        "vram_summary": vram_short,
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

