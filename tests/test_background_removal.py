import sys
import types

from gamemanager.services.background_removal import (
    _preferred_onnx_providers,
    _rembg_session,
    normalize_background_removal_engine,
    remove_background_bytes,
)


def test_normalize_background_removal_engine() -> None:
    assert normalize_background_removal_engine("rembg") == "rembg"
    assert normalize_background_removal_engine("BRIA_RMBG") == "bria_rmbg"
    assert normalize_background_removal_engine("none") == "none"
    assert normalize_background_removal_engine("invalid") == "none"


def test_remove_background_noop_when_disabled() -> None:
    payload = b"abc123"
    assert remove_background_bytes(payload, "none") == payload


def test_preferred_providers_prioritize_cuda_when_available(monkeypatch) -> None:
    class _Ort:
        @staticmethod
        def preload_dlls(**_kwargs):
            return None

        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setitem(sys.modules, "onnxruntime", _Ort)
    _preferred_onnx_providers.cache_clear()
    assert _preferred_onnx_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_rembg_session_falls_back_to_cpu_if_cuda_session_fails(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    class _Ort:
        @staticmethod
        def preload_dlls(**_kwargs):
            return None

        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def _new_session(_model_name, providers=None):
        providers = list(providers or [])
        calls.append(providers)
        if providers and providers[0] == "CUDAExecutionProvider":
            raise RuntimeError("CUDA init failed")
        return {"providers": providers}

    rembg_module = types.SimpleNamespace(new_session=_new_session)
    monkeypatch.setitem(sys.modules, "onnxruntime", _Ort)
    monkeypatch.setitem(sys.modules, "rembg", rembg_module)
    monkeypatch.setenv("GAMEMANAGER_DATA_DIR", str(tmp_path / ".gamemanager_data"))
    _preferred_onnx_providers.cache_clear()
    _rembg_session.cache_clear()

    session = _rembg_session("u2net")
    assert session == {"providers": ["CPUExecutionProvider"]}
    assert calls == [
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CPUExecutionProvider"],
    ]
