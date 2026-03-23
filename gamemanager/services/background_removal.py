from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import filecmp
from functools import lru_cache
import gc
from io import BytesIO, StringIO
import os
from pathlib import Path
import shutil
import threading
import warnings

from PIL import Image, ImageFilter, ImageOps


BACKGROUND_REMOVAL_OPTIONS: list[tuple[str, str]] = [
    ("Disabled", "none"),
    ("rembg (U2Net)", "rembg"),
    ("BRIA RMBG-2.0", "bria_rmbg"),
]

DEFAULT_BG_REMOVAL_PARAMS: dict[str, object] = {
    "alpha_matting": False,
    "alpha_matting_foreground_threshold": 220,
    "alpha_matting_background_threshold": 8,
    "alpha_matting_erode_size": 1,
    "alpha_edge_feather": 0,
    "post_process_mask": False,
}


def _project_data_dir() -> Path:
    override = os.environ.get("GAMEMANAGER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    project_root = Path(__file__).resolve().parents[2]
    return project_root / ".gamemanager_data"


def _remove_if_empty(path: Path) -> None:
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        return


def _merge_move_dir(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        src_path = src / entry.name
        dst_path = dst / entry.name
        if src_path.is_dir():
            _merge_move_dir(src_path, dst_path)
            _remove_if_empty(src_path)
            continue
        if dst_path.exists():
            try:
                if filecmp.cmp(src_path, dst_path, shallow=False):
                    src_path.unlink()
            except OSError:
                pass
            continue
        try:
            shutil.move(str(src_path), str(dst_path))
        except OSError:
            continue
    _remove_if_empty(src)


def _configure_local_model_cache() -> None:
    model_root = _project_data_dir() / "models"
    model_root.mkdir(parents=True, exist_ok=True)

    u2net_home = model_root / "u2net"
    hf_home = model_root / "hf"
    torch_home = model_root / "torch"
    xdg_home = model_root / "xdg"
    transformers_home = hf_home / "transformers"
    u2net_home.mkdir(parents=True, exist_ok=True)
    hf_home.mkdir(parents=True, exist_ok=True)
    torch_home.mkdir(parents=True, exist_ok=True)
    xdg_home.mkdir(parents=True, exist_ok=True)
    transformers_home.mkdir(parents=True, exist_ok=True)

    # Migrate common legacy caches from user-profile locations.
    home = Path.home()
    _merge_move_dir(home / ".u2net", u2net_home)
    _merge_move_dir(home / ".cache" / "huggingface", hf_home)
    _merge_move_dir(home / ".cache" / "torch", torch_home)

    os.environ.setdefault("U2NET_HOME", str(u2net_home))
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HF_HUB_CACHE", str(hf_home / "hub"))
    os.environ.setdefault("TORCH_HOME", str(torch_home))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(transformers_home))
    os.environ.setdefault("XDG_CACHE_HOME", str(xdg_home))


_configure_local_model_cache()

_BACKGROUND_MODEL_LOCK = threading.Lock()
_ACTIVE_BACKGROUND_MODELS: set[str] = set()
_PARKED_BACKGROUND_MODELS: dict[str, object] = {}


def normalize_background_removal_engine(engine: str | None) -> str:
    value = (engine or "").strip().casefold()
    if value in {"none", "rembg", "bria_rmbg"}:
        return value
    return "none"


def background_removal_device_status(engine: str | None) -> str:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return "Disabled"
    preferred = _preferred_onnx_providers()
    if "CUDAExecutionProvider" in preferred:
        return "GPU (CUDA)"
    if preferred:
        return "CPU"
    return "Unavailable"


def normalize_background_removal_params(
    params: dict[str, object] | None,
) -> dict[str, object]:
    raw = dict(DEFAULT_BG_REMOVAL_PARAMS)
    if isinstance(params, dict):
        raw.update(params)
    return {
        "alpha_matting": bool(raw.get("alpha_matting", False)),
        "alpha_matting_foreground_threshold": max(
            1, min(255, int(raw.get("alpha_matting_foreground_threshold", 220) or 220))
        ),
        "alpha_matting_background_threshold": max(
            0, min(254, int(raw.get("alpha_matting_background_threshold", 8) or 8))
        ),
        "alpha_matting_erode_size": max(
            0, min(64, int(raw.get("alpha_matting_erode_size", 1) or 1))
        ),
        "alpha_edge_feather": max(0, min(24, int(raw.get("alpha_edge_feather", 0) or 0))),
        "post_process_mask": bool(raw.get("post_process_mask", False)),
    }


def _tune_cutout_alpha(
    cutout_bytes: bytes,
    params: dict[str, object] | None,
) -> bytes:
    cfg = normalize_background_removal_params(params)
    try:
        with Image.open(BytesIO(cutout_bytes)) as loaded:
            loaded.load()
            image = ImageOps.exif_transpose(loaded).convert("RGBA")
    except Exception:
        return cutout_bytes

    alpha = image.getchannel("A")
    fg = int(cfg.get("alpha_matting_foreground_threshold", 220) or 220)
    bg = int(cfg.get("alpha_matting_background_threshold", 8) or 8)
    if fg <= bg:
        fg = min(255, bg + 1)

    alpha = alpha.point(
        lambda value: (
            0
            if int(value) <= bg
            else (
                255
                if int(value) >= fg
                else int(round(((int(value) - bg) * 255.0) / max(1.0, float(fg - bg))))
            )
        ),
        mode="L",
    )

    erode = int(cfg.get("alpha_matting_erode_size", 0) or 0)
    post = bool(cfg.get("post_process_mask", False))
    if erode > 0 or post:
        try:
            import cv2
            import numpy as np

            arr = np.array(alpha, dtype=np.uint8)
            if erode > 0:
                k = max(1, (min(64, erode) * 2) + 1)
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                arr = cv2.erode(arr, kernel, iterations=1)
            if post:
                k2 = max(3, (max(1, min(12, erode)) * 2) + 1)
                kernel2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2))
                arr = cv2.morphologyEx(arr, cv2.MORPH_OPEN, kernel2, iterations=1)
                arr = cv2.morphologyEx(arr, cv2.MORPH_CLOSE, kernel2, iterations=1)
            alpha = Image.fromarray(arr, mode="L")
        except Exception:
            if erode > 0:
                min_size = max(3, (min(16, erode) * 2) + 1)
                alpha = alpha.filter(ImageFilter.MinFilter(size=min_size))
            if post:
                alpha = alpha.filter(ImageFilter.MinFilter(size=3))
                alpha = alpha.filter(ImageFilter.MaxFilter(size=3))

    feather = int(cfg.get("alpha_edge_feather", 0) or 0)
    if feather > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=float(feather)))

    image.putalpha(alpha)
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=1)
def _preferred_onnx_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except Exception:
        return ["CPUExecutionProvider"]

    # Prevent cuDNN DLL version conflicts between torch and onnxruntime on Windows.
    # If torch is imported first, onnxruntime.preload_dlls() keeps torch's CUDA/cuDNN
    # set and skips loading potentially incompatible nvidia-* wheel DLLs.
    try:
        import torch  # noqa: F401
    except Exception:
        pass

    try:
        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            try:
                with _suppress_noisy_runtime_output():
                    preload(directory="")
            except TypeError:
                with _suppress_noisy_runtime_output():
                    preload()
            except Exception:
                pass
    except Exception:
        pass

    try:
        available = set(ort.get_available_providers() or [])
    except Exception:
        available = set()

    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@lru_cache(maxsize=4)
def _rembg_session(model_name: str):
    _configure_local_model_cache()
    try:
        from rembg import new_session
    except ImportError as exc:  # pragma: no cover - handled by callers in runtime only
        raise RuntimeError(
            "Background removal requires rembg. Install it in your GameManager env."
        ) from exc
    preferred = _preferred_onnx_providers()
    with _BACKGROUND_MODEL_LOCK:
        parked = _PARKED_BACKGROUND_MODELS.get(str(model_name))
    if parked is not None and "CUDAExecutionProvider" not in preferred:
        session = parked
    else:
        try:
            session = new_session(model_name, providers=preferred)
        except Exception:
            if preferred != ["CPUExecutionProvider"]:
                with _BACKGROUND_MODEL_LOCK:
                    parked = _PARKED_BACKGROUND_MODELS.get(str(model_name))
                if parked is not None:
                    session = parked
                else:
                    session = new_session(model_name, providers=["CPUExecutionProvider"])
            else:
                raise
    with _BACKGROUND_MODEL_LOCK:
        _ACTIVE_BACKGROUND_MODELS.add(str(model_name))
    return session


def _normalize_input_image_bytes(image_bytes: bytes) -> bytes:
    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGBA")
        out = BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return image_bytes


@contextmanager
def _filtered_removal_warnings():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=(
                "Palette images with Transparency expressed in bytes "
                "should be converted to RGBA images"
            ),
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=(
                r"invalid value encountered in scalar divide|"
                r"divide by zero encountered in scalar divide|"
                r"invalid value encountered in scalar multiply"
            ),
            category=RuntimeWarning,
            module=r"pymatting\.solver\.cg",
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Thresholded incomplete Cholesky decomposition failed.*",
            category=UserWarning,
            module=r".*pymatting.*",
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Thresholded incomplete Cholesky decomposition failed.*",
            category=RuntimeWarning,
            module=r".*pymatting.*",
        )
        yield


@contextmanager
def _suppress_noisy_runtime_output():
    buf_out = StringIO()
    buf_err = StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        yield


def remove_background_bytes(
    image_bytes: bytes,
    engine: str | None,
    *,
    params: dict[str, object] | None = None,
) -> bytes:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return image_bytes
    model = "u2net" if mode == "rembg" else "bria-rmbg"
    try:
        from rembg import remove
    except ImportError as exc:  # pragma: no cover - handled by callers in runtime only
        raise RuntimeError(
            "Background removal requires rembg. Install it in your GameManager env."
        ) from exc
    session = _rembg_session(model)
    remove_kwargs = normalize_background_removal_params(params)
    normalized_input = _normalize_input_image_bytes(image_bytes)
    try:
        with _filtered_removal_warnings():
            with _suppress_noisy_runtime_output():
                removed = remove(
                    normalized_input,
                    session=session,
                    force_return_bytes=True,
                    **remove_kwargs,
                )
            return _tune_cutout_alpha(removed, remove_kwargs)
    except TypeError:
        # Compatibility for older rembg signatures.
        with _filtered_removal_warnings():
            with _suppress_noisy_runtime_output():
                removed = remove(normalized_input, session=session, **remove_kwargs)
            return _tune_cutout_alpha(removed, remove_kwargs)


def preload_background_models() -> dict[str, str]:
    report: dict[str, str] = {}
    try:
        _preferred_onnx_providers()
        report["providers"] = "ok"
    except Exception as exc:
        report["providers"] = f"error: {exc}"
    for model_name in ("u2net", "bria-rmbg"):
        try:
            _rembg_session(model_name)
            report[model_name] = "ok"
        except Exception as exc:
            report[model_name] = f"error: {exc}"
    return report


def preload_background_engine(engine: str | None) -> str:
    mode = normalize_background_removal_engine(engine)
    if mode == "none":
        return "disabled"
    model_name = "u2net" if mode == "rembg" else "bria-rmbg"
    _rembg_session(model_name)
    return "ok"


def _park_background_model_to_ram(model_name: str) -> bool:
    model_token = str(model_name).strip()
    if not model_token:
        return False
    with _BACKGROUND_MODEL_LOCK:
        if model_token in _PARKED_BACKGROUND_MODELS:
            return True
    try:
        from rembg import new_session
    except Exception:
        return False
    try:
        session = new_session(model_token, providers=["CPUExecutionProvider"])
    except Exception:
        return False
    with _BACKGROUND_MODEL_LOCK:
        _PARKED_BACKGROUND_MODELS[model_token] = session
    return True


def clear_parked_background_models() -> int:
    with _BACKGROUND_MODEL_LOCK:
        count = len(_PARKED_BACKGROUND_MODELS)
        _PARKED_BACKGROUND_MODELS.clear()
    gc.collect()
    return count


def background_model_memory_state() -> dict[str, object]:
    providers = _preferred_onnx_providers()
    cache_info = _rembg_session.cache_info()
    with _BACKGROUND_MODEL_LOCK:
        loaded = sorted(_ACTIVE_BACKGROUND_MODELS)
        parked = sorted(_PARKED_BACKGROUND_MODELS.keys())
    return {
        "loaded_models": loaded,
        "parked_models": parked,
        "session_cache_currsize": int(cache_info.currsize),
        "session_cache_maxsize": int(cache_info.maxsize),
        "providers": providers,
        "cuda_preferred": "CUDAExecutionProvider" in providers,
    }


def release_background_models(
    *,
    clear_provider_cache: bool = False,
    aggressive: bool = False,
    park_in_ram: bool = True,
    drop_parked: bool = False,
) -> dict[str, object]:
    with _BACKGROUND_MODEL_LOCK:
        released_models = sorted(_ACTIVE_BACKGROUND_MODELS)
        _ACTIVE_BACKGROUND_MODELS.clear()
    parked_now: list[str] = []
    if park_in_ram:
        for model_name in released_models:
            if _park_background_model_to_ram(model_name):
                parked_now.append(str(model_name))
    _rembg_session.cache_clear()
    if drop_parked:
        clear_parked_background_models()
    if clear_provider_cache or aggressive:
        _preferred_onnx_providers.cache_clear()
    if aggressive:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    gc.collect()
    return {
        "released_models": released_models,
        "released_count": len(released_models),
        "parked_models": sorted(set(parked_now)),
        "parked_count": len(set(parked_now)),
        "park_in_ram": bool(park_in_ram),
        "drop_parked": bool(drop_parked),
        "aggressive": bool(aggressive),
    }
