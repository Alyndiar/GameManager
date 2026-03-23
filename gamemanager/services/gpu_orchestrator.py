from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import gc
import inspect
import subprocess
import threading
from typing import Any, Callable


WarmFn = Callable[[], object]
ReleaseFn = Callable[..., object]
StatusFn = Callable[[], str]
StateFn = Callable[[], dict[str, object]]


@dataclass(frozen=True, slots=True)
class GpuResourceSpec:
    resource_id: str
    label: str
    warm_fn: WarmFn
    release_fn: ReleaseFn
    status_fn: StatusFn
    state_fn: StateFn | None = None
    estimated_vram_mb: int = 0
    release_priority: int = 50
    supports_ram_parking: bool = True


@dataclass(frozen=True, slots=True)
class ExternalGpuJobPlan:
    created_at_utc: str
    required_free_mb: int | None
    released_resource_ids: tuple[str, ...]
    park_in_ram: bool
    aggressive: bool


def _call_with_supported_kwargs(func: Callable[..., object], **kwargs: object) -> object:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return func(**kwargs)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
        return func(**kwargs)
    accepted = {
        key: value
        for key, value in kwargs.items()
        if key in sig.parameters
    }
    return func(**accepted)


class GpuMemoryOrchestrator:
    def __init__(self) -> None:
        self._resources: dict[str, GpuResourceSpec] = {}
        self._lock = threading.RLock()

    def register_resource(self, spec: GpuResourceSpec, *, replace: bool = False) -> None:
        with self._lock:
            if not replace and spec.resource_id in self._resources:
                raise ValueError(f"GPU resource already registered: {spec.resource_id}")
            self._resources[spec.resource_id] = spec

    def list_resources(self) -> list[GpuResourceSpec]:
        with self._lock:
            specs = list(self._resources.values())
        specs.sort(
            key=lambda spec: (-int(spec.release_priority), spec.resource_id.casefold())
        )
        return specs

    def _resolve_resource_ids(self, resource_ids: list[str] | tuple[str, ...] | None) -> list[str]:
        if resource_ids is None:
            return [spec.resource_id for spec in self.list_resources()]
        normalized: list[str] = []
        seen: set[str] = set()
        with self._lock:
            valid = set(self._resources.keys())
        for token in resource_ids:
            key = str(token).strip()
            if not key or key in seen:
                continue
            if key not in valid:
                raise KeyError(f"Unknown GPU resource id: {key}")
            seen.add(key)
            normalized.append(key)
        return normalized

    @staticmethod
    def query_gpu_memory_mb() -> dict[str, int | None]:
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.used,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
            )
        except Exception:
            return {"total_mb": None, "used_mb": None, "free_mb": None}
        for line in out.splitlines():
            token = line.strip()
            if not token:
                continue
            parts = [part.strip() for part in token.split(",")]
            if len(parts) != 3:
                continue
            try:
                total, used, free = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            return {"total_mb": total, "used_mb": used, "free_mb": free}
        return {"total_mb": None, "used_mb": None, "free_mb": None}

    def status_snapshot(
        self,
        *,
        include_state: bool = True,
        include_gpu_memory: bool = True,
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        if include_gpu_memory:
            result["gpu_memory_mb"] = self.query_gpu_memory_mb()
        resources: dict[str, object] = {}
        for spec in self.list_resources():
            state: dict[str, object] = {
                "label": spec.label,
                "estimated_vram_mb": int(spec.estimated_vram_mb),
                "supports_ram_parking": bool(spec.supports_ram_parking),
            }
            try:
                state["status"] = str(spec.status_fn())
            except Exception as exc:
                state["status"] = f"error: {exc}"
            if include_state and spec.state_fn is not None:
                try:
                    state["state"] = spec.state_fn()
                except Exception as exc:
                    state["state"] = {"error": str(exc)}
            resources[spec.resource_id] = state
        result["resources"] = resources
        return result

    def warm(self, resource_ids: list[str] | tuple[str, ...] | None = None) -> dict[str, object]:
        ids = self._resolve_resource_ids(resource_ids)
        result: dict[str, object] = {"warmed": {}, "errors": {}}
        for resource_id in ids:
            spec = self._resources[resource_id]
            try:
                result["warmed"][resource_id] = spec.warm_fn()
            except Exception as exc:
                result["errors"][resource_id] = str(exc)
        return result

    def release(
        self,
        resource_ids: list[str] | tuple[str, ...] | None = None,
        *,
        park_in_ram: bool = True,
        aggressive: bool = False,
        drop_parked: bool = False,
    ) -> dict[str, object]:
        ids = self._resolve_resource_ids(resource_ids)
        result: dict[str, object] = {"released": {}, "errors": {}}
        for resource_id in ids:
            spec = self._resources[resource_id]
            kwargs = {
                "park_in_ram": bool(park_in_ram and spec.supports_ram_parking),
                "aggressive": bool(aggressive),
                "drop_parked": bool(drop_parked),
            }
            try:
                result["released"][resource_id] = _call_with_supported_kwargs(
                    spec.release_fn,
                    **kwargs,
                )
            except Exception as exc:
                result["errors"][resource_id] = str(exc)
        if aggressive:
            gc.collect()
        return result

    def ensure_free_vram(
        self,
        required_free_mb: int,
        *,
        park_in_ram: bool = True,
        aggressive: bool = True,
    ) -> dict[str, object]:
        required = max(0, int(required_free_mb))
        before = self.query_gpu_memory_mb()
        free_before = before.get("free_mb")
        released_ids: list[str] = []
        release_results: dict[str, object] = {}
        if free_before is not None and int(free_before) >= required:
            return {
                "required_free_mb": required,
                "free_before_mb": free_before,
                "free_after_mb": free_before,
                "released_resource_ids": released_ids,
                "release_results": release_results,
                "satisfied": True,
            }

        ordered = [spec.resource_id for spec in self.list_resources()]
        for resource_id in ordered:
            response = self.release(
                [resource_id],
                park_in_ram=park_in_ram,
                aggressive=aggressive,
                drop_parked=False,
            )
            release_results[resource_id] = response
            released_ids.append(resource_id)
            now = self.query_gpu_memory_mb()
            free_now = now.get("free_mb")
            if free_now is not None and int(free_now) >= required:
                return {
                    "required_free_mb": required,
                    "free_before_mb": free_before,
                    "free_after_mb": free_now,
                    "released_resource_ids": released_ids,
                    "release_results": release_results,
                    "satisfied": True,
                }

        after = self.query_gpu_memory_mb()
        free_after = after.get("free_mb")
        return {
            "required_free_mb": required,
            "free_before_mb": free_before,
            "free_after_mb": free_after,
            "released_resource_ids": released_ids,
            "release_results": release_results,
            "satisfied": bool(free_after is not None and int(free_after) >= required),
        }

    def prepare_for_external_gpu_job(
        self,
        *,
        required_free_mb: int | None = None,
        park_in_ram: bool = True,
        aggressive: bool = True,
    ) -> ExternalGpuJobPlan:
        released_ids: list[str]
        if required_free_mb is None:
            release_result = self.release(
                None,
                park_in_ram=park_in_ram,
                aggressive=aggressive,
                drop_parked=False,
            )
            released_ids = list((release_result.get("released") or {}).keys())
        else:
            ensure = self.ensure_free_vram(
                int(required_free_mb),
                park_in_ram=park_in_ram,
                aggressive=aggressive,
            )
            released_ids = list(ensure.get("released_resource_ids") or [])
        return ExternalGpuJobPlan(
            created_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            required_free_mb=None if required_free_mb is None else int(required_free_mb),
            released_resource_ids=tuple(released_ids),
            park_in_ram=bool(park_in_ram),
            aggressive=bool(aggressive),
        )

    def restore_after_external_gpu_job(
        self,
        plan: ExternalGpuJobPlan,
        *,
        warm_released: bool = True,
    ) -> dict[str, object]:
        if not warm_released:
            return {"restored": {}, "errors": {}}
        ids = list(plan.released_resource_ids)
        if not ids:
            return {"restored": {}, "errors": {}}
        warmed = self.warm(ids)
        return {
            "restored": warmed.get("warmed", {}),
            "errors": warmed.get("errors", {}),
        }

    @contextmanager
    def external_gpu_job(
        self,
        *,
        required_free_mb: int | None = None,
        park_in_ram: bool = True,
        aggressive: bool = True,
        warm_on_exit: bool = True,
    ):
        plan = self.prepare_for_external_gpu_job(
            required_free_mb=required_free_mb,
            park_in_ram=park_in_ram,
            aggressive=aggressive,
        )
        try:
            yield plan
        finally:
            if warm_on_exit:
                self.restore_after_external_gpu_job(plan, warm_released=True)


def _torch_status() -> str:
    try:
        import torch
    except Exception as exc:
        return f"Unavailable ({exc})"
    try:
        if torch.cuda.is_available():
            count = int(torch.cuda.device_count())
            return f"GPU ({count})"
        return "CPU"
    except Exception as exc:
        return f"Unknown ({exc})"


def _torch_state() -> dict[str, object]:
    try:
        import torch
    except Exception as exc:
        return {"error": str(exc)}
    info: dict[str, object] = {
        "cuda_available": bool(torch.cuda.is_available()),
        "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
    }
    if torch.cuda.is_available():
        try:
            info["memory_allocated_mb"] = int(torch.cuda.memory_allocated() / (1024 * 1024))
            info["memory_reserved_mb"] = int(torch.cuda.memory_reserved() / (1024 * 1024))
        except Exception:
            pass
    return info


def _warm_torch_runtime() -> str:
    try:
        import torch
    except Exception as exc:
        return f"error: {exc}"
    return "ok" if torch.cuda.is_available() else "cpu"


def _release_torch_runtime(
    *,
    aggressive: bool = False,
    **_unused: object,
) -> dict[str, object]:
    try:
        import torch
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if aggressive:
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    gc.collect()
    return {"status": "ok", "aggressive": bool(aggressive)}


def _background_status() -> str:
    from gamemanager.services.background_removal import background_removal_device_status

    rembg = background_removal_device_status("rembg")
    bria = background_removal_device_status("bria_rmbg")
    return f"rembg={rembg}; bria={bria}"


def _text_status() -> str:
    from gamemanager.services.icon_pipeline import text_extraction_device_status

    paddle = text_extraction_device_status("paddleocr")
    opencv = text_extraction_device_status("opencv_db")
    return f"paddle={paddle}; opencv={opencv}"


_GLOBAL_ORCHESTRATOR: GpuMemoryOrchestrator | None = None
_GLOBAL_ORCHESTRATOR_LOCK = threading.Lock()


def get_gpu_memory_orchestrator() -> GpuMemoryOrchestrator:
    global _GLOBAL_ORCHESTRATOR
    with _GLOBAL_ORCHESTRATOR_LOCK:
        if _GLOBAL_ORCHESTRATOR is not None:
            return _GLOBAL_ORCHESTRATOR
        orchestrator = GpuMemoryOrchestrator()

        from gamemanager.services.background_removal import (
            background_model_memory_state,
            preload_background_engine,
            preload_background_models,
            release_background_models,
        )
        from gamemanager.services.icon_pipeline import (
            preload_text_model,
            preload_text_models,
            release_text_models,
            text_model_memory_state,
        )

        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="background_rembg",
                label="Background Removal (rembg)",
                warm_fn=lambda: preload_background_engine("rembg"),
                release_fn=release_background_models,
                status_fn=_background_status,
                state_fn=background_model_memory_state,
                estimated_vram_mb=3200,
                release_priority=95,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="background_bria",
                label="Background Removal (BRIA)",
                warm_fn=lambda: preload_background_engine("bria_rmbg"),
                release_fn=release_background_models,
                status_fn=_background_status,
                state_fn=background_model_memory_state,
                estimated_vram_mb=3600,
                release_priority=94,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="background_stack",
                label="Background Removal Stack",
                warm_fn=preload_background_models,
                release_fn=release_background_models,
                status_fn=_background_status,
                state_fn=background_model_memory_state,
                estimated_vram_mb=6500,
                release_priority=90,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="text_paddle",
                label="Text Extraction (PaddleOCR)",
                warm_fn=lambda: preload_text_model("paddleocr"),
                release_fn=release_text_models,
                status_fn=_text_status,
                state_fn=text_model_memory_state,
                estimated_vram_mb=1200,
                release_priority=75,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="text_opencv",
                label="Text Extraction (OpenCV DB)",
                warm_fn=lambda: preload_text_model("opencv_db"),
                release_fn=release_text_models,
                status_fn=_text_status,
                state_fn=text_model_memory_state,
                estimated_vram_mb=500,
                release_priority=74,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="text_stack",
                label="Text Extraction Stack",
                warm_fn=preload_text_models,
                release_fn=release_text_models,
                status_fn=_text_status,
                state_fn=text_model_memory_state,
                estimated_vram_mb=1800,
                release_priority=70,
                supports_ram_parking=True,
            )
        )
        orchestrator.register_resource(
            GpuResourceSpec(
                resource_id="torch_runtime",
                label="Torch CUDA Runtime Cache",
                warm_fn=_warm_torch_runtime,
                release_fn=_release_torch_runtime,
                status_fn=_torch_status,
                state_fn=_torch_state,
                estimated_vram_mb=256,
                release_priority=20,
                supports_ram_parking=False,
            )
        )
        _GLOBAL_ORCHESTRATOR = orchestrator
        return orchestrator


def reset_gpu_memory_orchestrator_for_tests() -> None:
    global _GLOBAL_ORCHESTRATOR
    with _GLOBAL_ORCHESTRATOR_LOCK:
        _GLOBAL_ORCHESTRATOR = None
