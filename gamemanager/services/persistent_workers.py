from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
import threading
import time
from typing import Any


_RUNTIME_LOCK = threading.Lock()
_RUNTIME_POOL: ThreadPoolExecutor | None = None
_RUNTIME_FUTURES: list[Future[Any]] = []
_RUNTIME_NODE_COUNT = 0
_RUNTIME_STARTED_AT = 0.0
_RUNTIME_STARTING = False


def _status_unlocked() -> dict[str, object]:
    running = _RUNTIME_POOL is not None
    uptime_s = max(0.0, time.time() - _RUNTIME_STARTED_AT) if running else 0.0
    return {
        "running": running,
        "node_count": int(_RUNTIME_NODE_COUNT if running else 0),
        "uptime_s": float(uptime_s),
        "starting": bool(_RUNTIME_STARTING and not running),
    }


def _warm_icon_core_caches() -> dict[str, object]:
    # Keep warm-up lightweight and deterministic; model-heavy warm-up remains opt-in.
    from gamemanager.services.background_removal import normalize_background_removal_params
    from gamemanager.services.icon_pipeline import icon_style_options, text_preserve_to_dict

    template_count = len(icon_style_options())
    _ = normalize_background_removal_params(None)
    _ = text_preserve_to_dict(None)
    return {
        "template_count": template_count,
        "status": "warmed",
    }


def ensure_persistent_icon_workers(worker_count: int = 2) -> dict[str, object]:
    if os.environ.get("GAMEMANAGER_DISABLE_ICON_WORKERS", "").strip() in {"1", "true", "yes", "on"}:
        return {
            "running": False,
            "node_count": 0,
            "uptime_s": 0.0,
            "disabled": True,
        }
    count = max(1, int(worker_count))
    with _RUNTIME_LOCK:
        global _RUNTIME_POOL, _RUNTIME_FUTURES, _RUNTIME_NODE_COUNT, _RUNTIME_STARTED_AT, _RUNTIME_STARTING
        if _RUNTIME_POOL is not None:
            return _status_unlocked()
        _RUNTIME_STARTING = True
        _RUNTIME_POOL = ThreadPoolExecutor(
            max_workers=count,
            thread_name_prefix="icon-worker",
        )
        _RUNTIME_FUTURES = []
        _RUNTIME_NODE_COUNT = count
        _RUNTIME_STARTED_AT = time.time()
        try:
            # Keep one lightweight prewarm task, do not pin worker threads permanently.
            _RUNTIME_FUTURES.append(_RUNTIME_POOL.submit(_warm_icon_core_caches))
        finally:
            _RUNTIME_STARTING = False
        return _status_unlocked()


def ensure_persistent_icon_workers_async(worker_count: int = 2) -> dict[str, object]:
    with _RUNTIME_LOCK:
        global _RUNTIME_STARTING
        if _RUNTIME_POOL is not None:
            return _status_unlocked()
        if _RUNTIME_STARTING:
            return {
                "running": False,
                "node_count": max(1, int(worker_count)),
                "uptime_s": 0.0,
                "starting": True,
            }
        _RUNTIME_STARTING = True

    def _runner() -> None:
        try:
            ensure_persistent_icon_workers(worker_count=worker_count)
        except Exception:
            with _RUNTIME_LOCK:
                global _RUNTIME_STARTING
                _RUNTIME_STARTING = False

    thread = threading.Thread(
        target=_runner,
        name="icon-worker-bootstrap",
        daemon=True,
    )
    thread.start()
    return {
        "running": False,
        "node_count": max(1, int(worker_count)),
        "uptime_s": 0.0,
        "starting": True,
    }


def persistent_icon_workers_status() -> dict[str, object]:
    with _RUNTIME_LOCK:
        return _status_unlocked()


def submit_persistent_icon_task(fn, *args, **kwargs) -> Future[Any]:
    with _RUNTIME_LOCK:
        pool = _RUNTIME_POOL
    if pool is None:
        ensure_persistent_icon_workers()
        with _RUNTIME_LOCK:
            pool = _RUNTIME_POOL
    if pool is None:
        raise RuntimeError("Persistent icon workers are unavailable.")
    return pool.submit(fn, *args, **kwargs)


def shutdown_persistent_icon_workers(wait_seconds: float = 1.5) -> None:
    with _RUNTIME_LOCK:
        global _RUNTIME_POOL, _RUNTIME_FUTURES, _RUNTIME_NODE_COUNT, _RUNTIME_STARTED_AT, _RUNTIME_STARTING
        pool = _RUNTIME_POOL
        if pool is None:
            _RUNTIME_STARTING = False
            return
        _RUNTIME_POOL = None
        _RUNTIME_NODE_COUNT = 0
        _RUNTIME_STARTED_AT = 0.0
        _RUNTIME_STARTING = False
    try:
        pool.shutdown(wait=True, cancel_futures=True)
    except Exception:
        pass
    finally:
        _RUNTIME_FUTURES.clear()
