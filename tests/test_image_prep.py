from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from gamemanager.services.image_prep import (
    ImagePrepOptions,
    SUPPORTED_IMAGE_EXTENSIONS,
    apply_min_black_transparency,
    detect_content_bbox,
    icon_templates_dir,
    normalize_to_square_png,
    prepare_images_to_template_folder,
    prepare_images_to_512_png,
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


def test_supported_image_extensions_include_avif() -> None:
    assert ".avif" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jpeg" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jpe" in SUPPORTED_IMAGE_EXTENSIONS
    assert ".jfif" in SUPPORTED_IMAGE_EXTENSIONS
