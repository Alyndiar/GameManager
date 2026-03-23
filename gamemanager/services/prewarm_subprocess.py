from __future__ import annotations

import argparse
import json
import sys


RESOURCE_LABELS: dict[str, str] = {
    "torch_runtime": "Torch runtime",
    "background_stack": "Background models",
    "text_stack": "Text models",
    "background_rembg": "Background model (rembg)",
    "background_bria": "Background model (BRIA)",
    "text_paddle": "Text model (PaddleOCR)",
    "text_opencv": "Text model (OpenCV DB)",
}


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--resources-json", default="[]")
    return parser.parse_args(argv)


def _normalize_resources(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _worker_main(resources: list[str]) -> int:
    from gamemanager.services.gpu_orchestrator import get_gpu_memory_orchestrator

    orchestrator = get_gpu_memory_orchestrator()
    total = max(1, len(resources))
    for idx, key in enumerate(resources, start=1):
        label = RESOURCE_LABELS.get(key, key)
        _emit(
            {
                "type": "progress",
                "stage": f"Preload: {label}",
                "current": idx - 1,
                "total": total,
            }
        )
        response = orchestrator.warm([key])
        errors = response.get("errors") if isinstance(response, dict) else {}
        if isinstance(errors, dict) and errors.get(key):
            _emit(
                {
                    "type": "warning",
                    "message": f"{label}: {errors.get(key)}",
                }
            )
        _emit(
            {
                "type": "progress",
                "stage": f"Preload: {label}",
                "current": idx,
                "total": total,
            }
        )
    _emit({"type": "done", "message": "Preload complete"})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.worker:
        return 2
    try:
        resources_raw = json.loads(args.resources_json or "[]")
    except json.JSONDecodeError:
        resources_raw = []
    resources = _normalize_resources(resources_raw)
    if not resources:
        _emit({"type": "done", "message": "Preload skipped"})
        return 0
    try:
        return _worker_main(resources)
    except Exception as exc:
        _emit({"type": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

