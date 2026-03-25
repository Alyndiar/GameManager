import sys
import types
from io import BytesIO

from PIL import Image

from gamemanager.services.background_removal import (
    _preferred_onnx_providers,
    _rembg_session,
    normalize_background_removal_engine,
    normalize_background_removal_params,
    remove_background_bytes,
)


def _png_bytes(color: tuple[int, int, int, int] = (20, 40, 60, 255)) -> bytes:
    image = Image.new("RGBA", (4, 4), color)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_normalize_background_removal_engine() -> None:
    assert normalize_background_removal_engine("rembg") == "rembg"
    assert normalize_background_removal_engine("BRIA_RMBG") == "bria_rmbg"
    assert normalize_background_removal_engine("pick_colors") == "pick_colors"
    assert normalize_background_removal_engine("none") == "none"
    assert normalize_background_removal_engine("invalid") == "none"


def test_remove_background_noop_when_disabled() -> None:
    payload = b"abc123"
    assert remove_background_bytes(payload, "none") == payload


def test_pick_colors_defaults_use_flat_falloff() -> None:
    cfg = normalize_background_removal_params({})
    assert cfg["pick_colors_falloff"] == "flat"
    assert cfg["pick_colors_curve_strength"] == 50


def test_remove_background_pick_colors_transparentizes_selected_background() -> None:
    image = Image.new("RGBA", (8, 8), (32, 80, 200, 255))
    for y in range(2, 6):
        for x in range(2, 6):
            image.putpixel((x, y), (220, 40, 40, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    payload = buf.getvalue()

    out = remove_background_bytes(
        payload,
        "pick_colors",
        params={
            "picked_colors": [
                {"color": [32, 80, 200], "tolerance": 10},
            ],
        },
    )
    with Image.open(BytesIO(out)) as loaded:
        loaded.load()
        rgba = loaded.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((4, 4))[3] == 255


def test_remove_background_pick_colors_is_global_not_contiguous() -> None:
    image = Image.new("RGBA", (7, 7), (180, 30, 30, 255))
    image.putpixel((0, 0), (15, 120, 230, 255))
    image.putpixel((3, 3), (15, 120, 230, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")

    out = remove_background_bytes(
        buf.getvalue(),
        "pick_colors",
        params={
            "picked_colors": [{"color": [15, 120, 230], "tolerance": 10}],
        },
    )
    with Image.open(BytesIO(out)) as loaded:
        loaded.load()
        rgba = loaded.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((3, 3))[3] == 0
        assert rgba.getpixel((1, 1))[3] == 255


def test_remove_background_pick_colors_linear_falloff_changes_alpha_gradually() -> None:
    image = Image.new("RGBA", (3, 1), (0, 0, 0, 255))
    image.putpixel((1, 0), (5, 0, 0, 255))
    image.putpixel((2, 0), (12, 0, 0, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    out = remove_background_bytes(
        buf.getvalue(),
        "pick_colors",
        params={
            "picked_colors": [{"color": [0, 0, 0], "tolerance": 10, "falloff": "lin"}],
            "pick_colors_use_hsv": False,
            "pick_colors_tolerance_mode": "max",
        },
    )
    with Image.open(BytesIO(out)) as loaded:
        loaded.load()
        rgba = loaded.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert 90 <= rgba.getpixel((1, 0))[3] <= 150
        assert rgba.getpixel((2, 0))[3] == 255


def test_remove_background_pick_colors_contig_supports_add_remove_seeds() -> None:
    image = Image.new("RGBA", (7, 7), (180, 180, 180, 255))
    for y in range(2, 5):
        for x in range(2, 5):
            image.putpixel((x, y), (20, 100, 220, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    payload = buf.getvalue()

    no_seed = remove_background_bytes(
        payload,
        "pick_colors",
        params={
            "picked_colors": [
                {
                    "color": [20, 100, 220],
                    "tolerance": 10,
                    "scope": "contig",
                    "falloff": "flat",
                }
            ]
        },
    )
    with Image.open(BytesIO(no_seed)) as loaded:
        loaded.load()
        rgba = loaded.convert("RGBA")
        assert rgba.getpixel((3, 3))[3] == 255

    add_seed = remove_background_bytes(
        payload,
        "pick_colors",
        params={
            "picked_colors": [
                {
                    "color": [20, 100, 220],
                    "tolerance": 10,
                    "scope": "contig",
                    "falloff": "flat",
                    "include_seeds": [[0.5, 0.5]],
                }
            ]
        },
    )
    with Image.open(BytesIO(add_seed)) as loaded:
        loaded.load()
        rgba = loaded.convert("RGBA")
        assert rgba.getpixel((3, 3))[3] == 0


def test_remove_background_rembg_ignores_pick_color_keys(monkeypatch) -> None:
    payload = _png_bytes()
    calls: list[dict[str, object]] = []

    def _fake_remove(image_bytes, *, session=None, force_return_bytes=True, **kwargs):
        calls.append(dict(kwargs))
        return image_bytes

    monkeypatch.setitem(sys.modules, "rembg", types.SimpleNamespace(remove=_fake_remove))
    monkeypatch.setattr(
        "gamemanager.services.background_removal._rembg_session",
        lambda _model_name: object(),
    )
    out = remove_background_bytes(
        payload,
        "rembg",
        params={
            "alpha_matting": True,
            "picked_colors": [{"color": [0, 0, 0], "tolerance": 10}],
            "pick_colors_use_hsv": True,
        },
    )
    assert out
    assert calls
    kwargs = calls[0]
    assert "picked_colors" not in kwargs
    assert "pick_colors_use_hsv" not in kwargs
    assert "pick_colors_falloff" not in kwargs
    assert "pick_colors_curve_strength" not in kwargs


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
