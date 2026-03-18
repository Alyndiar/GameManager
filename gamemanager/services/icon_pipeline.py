from __future__ import annotations

from io import BytesIO
from typing import Final

from PIL import Image, ImageDraw, ImageEnhance, ImageOps


ICO_SIZES: Final[list[int]] = [256, 128, 64, 48, 32, 24, 16]


def _crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _apply_size_profile(image: Image.Image, size: int) -> Image.Image:
    # Stronger local punch for tiny sizes where details collapse.
    if size <= 24:
        contrast, saturation, sharpness, brightness = 1.28, 1.12, 1.45, 1.05
    elif size <= 48:
        contrast, saturation, sharpness, brightness = 1.18, 1.08, 1.25, 1.02
    else:
        contrast, saturation, sharpness, brightness = 1.08, 1.04, 1.12, 1.0
    image = ImageEnhance.Contrast(image).enhance(contrast)
    image = ImageEnhance.Color(image).enhance(saturation)
    image = ImageEnhance.Sharpness(image).enhance(sharpness)
    image = ImageEnhance.Brightness(image).enhance(brightness)
    return image


def _apply_circle_and_ring(image: Image.Image, size: int) -> Image.Image:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = 1 if size <= 24 else 2
    draw.ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=255)
    image.putalpha(mask)

    ring_thickness = 0
    if size >= 128:
        ring_thickness = 12
    elif size >= 64:
        ring_thickness = 5
    elif size >= 32:
        ring_thickness = 2
    elif size >= 24:
        ring_thickness = 1
    if ring_thickness <= 0:
        return image

    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring)
    outer = max(inset, ring_thickness // 2)
    ring_color = (245, 245, 245, 230)
    ring_draw.ellipse(
        (outer, outer, size - outer - 1, size - outer - 1),
        outline=ring_color,
        width=ring_thickness,
    )
    return Image.alpha_composite(image, ring)


def _build_master(image_bytes: bytes) -> Image.Image:
    image = Image.open(BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA")
    return _crop_square(image)


def build_multi_size_ico(
    image_bytes: bytes,
    circular_ring: bool = True,
) -> bytes:
    master = _build_master(image_bytes)
    # Pre-shape at high resolution first, then delegate ICO frame generation.
    base = master.resize((512, 512), Image.Resampling.LANCZOS)
    base = _apply_size_profile(base, 256)
    if circular_ring:
        base = _apply_circle_and_ring(base, 512)

    out = BytesIO()
    base.save(out, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return out.getvalue()


def build_preview_png(
    image_bytes: bytes,
    size: int = 96,
    circular_ring: bool = True,
) -> bytes:
    master = _build_master(image_bytes)
    image = master.resize((size, size), Image.Resampling.LANCZOS)
    image = _apply_size_profile(image, size)
    if circular_ring:
        image = _apply_circle_and_ring(image, size)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
