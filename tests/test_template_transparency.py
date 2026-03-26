from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from gamemanager.services.template_transparency import (
    TemplateTransparencyOptions,
    make_background_transparent,
    process_template_file,
)


def _png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _read_alpha(payload: bytes) -> Image.Image:
    return Image.open(BytesIO(payload)).convert("RGBA").getchannel("A")


def test_edge_flood_fill_keeps_same_color_island_not_connected_to_edge() -> None:
    image = Image.new("RGBA", (20, 20), (0, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 13, 13), fill=(255, 0, 0, 255))
    draw.rectangle((9, 9, 10, 10), fill=(0, 0, 0, 255))

    payload = make_background_transparent(
        _png_bytes(image),
        options=TemplateTransparencyOptions(threshold=10, use_edge_flood_fill=True),
    )
    alpha = _read_alpha(payload)
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((9, 9)) == 255


def test_global_mode_removes_matching_color_anywhere() -> None:
    image = Image.new("RGBA", (20, 20), (0, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 13, 13), fill=(255, 0, 0, 255))
    draw.rectangle((9, 9, 10, 10), fill=(0, 0, 0, 255))

    payload = make_background_transparent(
        _png_bytes(image),
        options=TemplateTransparencyOptions(threshold=10, use_edge_flood_fill=False),
    )
    alpha = _read_alpha(payload)
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((9, 9)) == 0


def test_center_pass_removes_center_connected_background() -> None:
    image = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((7, 7, 12, 12), fill=(0, 0, 0, 255))

    payload = make_background_transparent(
        _png_bytes(image),
        options=TemplateTransparencyOptions(
            threshold=10,
            use_edge_flood_fill=True,
            use_center_flood_fill=True,
        ),
        background_color=(0, 0, 0),
    )
    alpha = _read_alpha(payload)
    assert alpha.getpixel((9, 9)) == 0
    assert alpha.getpixel((2, 2)) == 255


def test_process_template_file_writes_png_output(tmp_path: Path) -> None:
    input_path = tmp_path / "template.png"
    output_path = tmp_path / "out" / "template.png"

    image = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
    ImageDraw.Draw(image).rectangle((2, 2, 5, 5), fill=(200, 40, 40, 255))
    input_path.write_bytes(_png_bytes(image))

    process_template_file(
        str(input_path),
        str(output_path),
        options=TemplateTransparencyOptions(threshold=8),
    )

    assert output_path.exists()
    out = Image.open(output_path).convert("RGBA")
    assert out.getchannel("A").getpixel((0, 0)) == 0
    assert out.getchannel("A").getpixel((3, 3)) == 255


def test_falloff_lin_applies_partial_alpha_within_tolerance_band() -> None:
    image = Image.new("RGBA", (3, 1), (0, 0, 0, 255))
    px = image.load()
    px[0, 0] = (0, 0, 0, 255)
    px[1, 0] = (15, 15, 15, 255)
    px[2, 0] = (40, 40, 40, 255)

    payload = make_background_transparent(
        _png_bytes(image),
        options=TemplateTransparencyOptions(
            threshold=20,
            falloff_mode="lin",
            use_edge_flood_fill=False,
            use_center_flood_fill=False,
        ),
        background_color=(0, 0, 0),
    )
    alpha = _read_alpha(payload)
    assert alpha.getpixel((0, 0)) == 0
    assert 0 < alpha.getpixel((1, 0)) < 255
    assert alpha.getpixel((2, 0)) == 255
