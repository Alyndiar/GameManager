from io import BytesIO

from PIL import Image

from gamemanager.services.icon_pipeline import ICO_SIZES, build_multi_size_ico, build_preview_png


def _sample_png_bytes() -> bytes:
    img = Image.new("RGBA", (400, 280), (40, 120, 220, 255))
    b = BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


def test_build_multi_size_ico_generates_valid_ico() -> None:
    ico_data = build_multi_size_ico(_sample_png_bytes(), circular_ring=True)
    assert ico_data[:4] == b"\x00\x00\x01\x00"
    image = Image.open(BytesIO(ico_data))
    assert image.format == "ICO"
    # Pillow exposes at least one frame and icon_sizes metadata for ICO files.
    sizes = image.info.get("sizes") or set()
    assert (256, 256) in sizes or image.size == (256, 256)


def test_build_preview_png_outputs_png_bytes() -> None:
    preview = build_preview_png(_sample_png_bytes(), size=96, circular_ring=False)
    opened = Image.open(BytesIO(preview))
    assert opened.format == "PNG"
    assert opened.size == (96, 96)
    assert len(ICO_SIZES) >= 5
