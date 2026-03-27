from __future__ import annotations

UPSCALE_METHOD_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Qt Smooth (fast)", "qt_smooth"),
    ("Pillow Bicubic", "bicubic"),
    ("Pillow Lanczos", "lanczos"),
    ("Pillow Lanczos + Unsharp", "lanczos_unsharp"),
)


def shader_tone_label(mode: str) -> str:
    return "Lightness" if mode == "hsl" else "Value"


def shader_swatch_css(rgb: tuple[int, int, int]) -> str:
    red, green, blue = rgb
    return (
        "QPushButton {"
        f" background-color: rgb({red}, {green}, {blue});"
        " border: 1px solid #555;"
        " min-width: 28px;"
        " min-height: 18px;"
        " }"
    )


def normalize_upscale_method(method: str | None) -> str:
    value = str(method or "qt_smooth").strip().casefold()
    valid = {item[1] for item in UPSCALE_METHOD_OPTIONS}
    return value if value in valid else "qt_smooth"
