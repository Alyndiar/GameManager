from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from gamemanager.services.image_prep import (
    ImagePrepOptions,
    SUPPORTED_IMAGE_EXTENSIONS,
    apply_background_color_transparency,
    apply_min_black_transparency,
    detect_content_bbox,
    icon_templates_dir,
    normalize_to_square_png,
    prepare_images_to_template_folder,
    prepare_images_to_512_png,
    resolve_background_removal_config,
)


def _png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_detect_content_bbox_uses_alpha_and_min_padding() -> None:
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 79, 79), fill=(255, 0, 0, 255))
    bbox = detect_content_bbox(
        image,
        alpha_threshold=8,
        border_threshold=16,
        padding_ratio=0.0,
        min_padding_pixels=1,
    )
    assert bbox == (19, 19, 81, 81)


def test_detect_content_bbox_handles_opaque_border() -> None:
    image = Image.new("RGBA", (120, 100), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 12, 109, 87), fill=(40, 70, 220, 255))
    bbox = detect_content_bbox(
        image,
        alpha_threshold=8,
        border_threshold=10,
        padding_ratio=0.0,
        min_padding_pixels=0,
    )
    assert bbox[0] >= 9
    assert bbox[1] >= 11
    assert bbox[2] <= 111
    assert bbox[3] <= 89


def test_normalize_to_square_png_outputs_512_rgba_png() -> None:
    image = Image.new("RGBA", (300, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 20, 270, 160), fill=(240, 190, 20, 255))
    payload = normalize_to_square_png(
        _png_bytes(image),
        output_size=512,
        padding_ratio=0.0,
        min_padding_pixels=1,
    )
    out = Image.open(BytesIO(payload))
    assert out.format == "PNG"
    assert out.size == (512, 512)
    assert out.mode == "RGBA"
    assert out.getbbox() is not None


def test_prepare_images_to_512_png_writes_outputs(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((8, 8, 55, 55), fill=(20, 220, 90, 255))
    (source_dir / "sample.png").write_bytes(_png_bytes(image))

    out_dir = tmp_path / "out"
    report = prepare_images_to_512_png(
        input_paths=[str(source_dir)],
        output_dir=str(out_dir),
        options=ImagePrepOptions(output_size=512, recursive=False),
    )
    assert report.attempted == 1
    assert report.succeeded == 1
    assert report.failed == 0
    assert (out_dir / "sample.png").exists()


def test_prepare_images_to_template_folder_uses_incremented_template_names(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (80, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 8, 70, 56), fill=(220, 40, 40, 255))
    (source_dir / "one.png").write_bytes(_png_bytes(image))
    (source_dir / "two.png").write_bytes(_png_bytes(image))

    template_dir = tmp_path / "IconTemplates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "template001.png").write_bytes(_png_bytes(image))
    (template_dir / "template007.png").write_bytes(_png_bytes(image))

    report = prepare_images_to_template_folder(
        input_paths=[str(source_dir)],
        options=ImagePrepOptions(output_size=512, recursive=False),
        output_dir=str(template_dir),
    )
    assert report.attempted == 2
    assert report.succeeded == 2
    assert (template_dir / "template008.png").exists()
    assert (template_dir / "template009.png").exists()


def test_icon_templates_dir_points_to_project_folder() -> None:
    path = icon_templates_dir()
    assert path.name == "IconTemplates"


def test_apply_min_black_transparency_makes_black_border_transparent() -> None:
    image = Image.new("RGBA", (12, 12), (0, 0, 0, 255))
    ImageDraw.Draw(image).rectangle((3, 3, 8, 8), fill=(220, 60, 40, 255))
    payload = apply_min_black_transparency(_png_bytes(image), min_black_level=10)
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((5, 5)) == 255


def test_apply_min_black_transparency_clamps_to_30() -> None:
    image = Image.new("RGBA", (8, 8), (40, 40, 40, 255))
    payload = apply_min_black_transparency(_png_bytes(image), min_black_level=255)
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 255


def test_resolve_background_removal_config_custom_non_bw_halves_tolerance_hsv() -> None:
    base, effective, space = resolve_background_removal_config(
        mode="custom",
        tolerance=20,
        custom_color_rgb=(30, 90, 210),
        use_hsv_for_custom=True,
    )
    assert base == (30, 90, 210)
    assert effective == 10
    assert space == "hsv"


def test_apply_background_color_transparency_white_mode() -> None:
    image = Image.new("RGBA", (16, 16), (255, 255, 255, 255))
    ImageDraw.Draw(image).ellipse((4, 4, 11, 11), fill=(40, 110, 210, 255))
    payload = apply_background_color_transparency(
        _png_bytes(image),
        mode="white",
        tolerance=10,
        custom_color_rgb=(255, 255, 255),
    )
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((8, 8)) == 255


def test_apply_background_color_transparency_center_flood_fill_can_remove_center_island() -> None:
    image = Image.new("RGBA", (21, 21), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 20, 20), outline=(0, 0, 0, 255), width=2)
    draw.rectangle((9, 9, 11, 11), fill=(0, 0, 0, 255))

    edge_only = apply_background_color_transparency(
        _png_bytes(image),
        mode="black",
        tolerance=12,
        custom_color_rgb=(0, 0, 0),
        use_hsv_for_custom=False,
        use_center_flood_fill=False,
    )
    with_center = apply_background_color_transparency(
        _png_bytes(image),
        mode="black",
        tolerance=12,
        custom_color_rgb=(0, 0, 0),
        use_hsv_for_custom=False,
        use_center_flood_fill=True,
    )

    edge_alpha = Image.open(BytesIO(edge_only)).convert("RGBA").getchannel("A")
    center_alpha = Image.open(BytesIO(with_center)).convert("RGBA").getchannel("A")

    assert edge_alpha.getpixel((1, 1)) == 0
    assert edge_alpha.getpixel((10, 10)) == 255
    assert center_alpha.getpixel((1, 1)) == 0
    assert center_alpha.getpixel((10, 10)) == 0


def test_apply_background_color_transparency_supports_falloff() -> None:
    image = Image.new("RGBA", (3, 1), (0, 0, 0, 255))
    px = image.load()
    px[0, 0] = (0, 0, 0, 255)
    px[1, 0] = (15, 15, 15, 255)
    px[2, 0] = (40, 40, 40, 255)
    payload = apply_background_color_transparency(
        _png_bytes(image),
        mode="black",
        tolerance=20,
        custom_color_rgb=(0, 0, 0),
        use_hsv_for_custom=False,
        falloff_mode="lin",
        curve_strength=50,
        use_center_flood_fill=False,
    )
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert 0 < alpha.getpixel((1, 0)) < 255
    assert alpha.getpixel((2, 0)) == 255


def test_apply_min_black_transparency_skips_when_exterior_and_center_already_transparent() -> None:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 55, 55), outline=(0, 0, 0, 255), width=8)
    payload = apply_min_black_transparency(_png_bytes(image), min_black_level=20)
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((32, 32)) == 0
    assert alpha.getpixel((32, 8)) == 255


def test_apply_min_black_transparency_still_applies_for_opaque_black_background() -> None:
    image = Image.new("RGBA", (24, 24), (0, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 17, 17), fill=(220, 70, 40, 255))
    payload = apply_min_black_transparency(_png_bytes(image), min_black_level=10)
    out = Image.open(BytesIO(payload)).convert("RGBA")
    alpha = out.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((10, 10)) == 255


def test_supported_image_extensions_include_avif() -> None:
    assert ".avif" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jpeg" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jpe" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jfif" in SUPPORTED_IMAGE_EXTENSIONS
