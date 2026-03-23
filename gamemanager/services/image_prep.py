from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import re
import statistics
from typing import Callable

from PIL import Image, ImageChops, ImageOps
from gamemanager.services.template_transparency import (
    TemplateTransparencyOptions,
    make_background_transparent,
)


SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpe",
    ".jpeg",
    ".jfif",
    ".avif",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
)
MIN_BLACK_LEVEL_MAX: int = 30


@dataclass(slots=True)
class ImagePrepOptions:
    output_size: int = 512
    padding_ratio: float = 0.02
    min_padding_pixels: int = 1
    alpha_threshold: int = 8
    border_threshold: int = 16
    min_black_level: int = 0
    overwrite: bool = False
    recursive: bool = False


@dataclass(slots=True)
class ImagePrepReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)


ImagePrepProgressCallback = Callable[
    [int, int, str, str, str, str | None, str | None],
    None,
]


def _is_supported_image(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def icon_templates_dir() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / "IconTemplates").resolve()


def _iter_images(paths: list[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            if _is_supported_image(path):
                files.append(path)
            continue
        if not path.is_dir():
            continue
        iterator = path.rglob("*") if recursive else path.glob("*")
        for child in iterator:
            if child.is_file() and _is_supported_image(child):
                files.append(child.resolve())
    return sorted(set(files), key=lambda p: (str(p.parent).casefold(), p.name.casefold()))


def _median_border_color(rgb: Image.Image) -> tuple[int, int, int]:
    width, height = rgb.size
    pixels = rgb.load()
    reds: list[int] = []
    greens: list[int] = []
    blues: list[int] = []

    for x in range(width):
        top = pixels[x, 0]
        bottom = pixels[x, height - 1]
        reds.extend((int(top[0]), int(bottom[0])))
        greens.extend((int(top[1]), int(bottom[1])))
        blues.extend((int(top[2]), int(bottom[2])))
    for y in range(height):
        left = pixels[0, y]
        right = pixels[width - 1, y]
        reds.extend((int(left[0]), int(right[0])))
        greens.extend((int(left[1]), int(right[1])))
        blues.extend((int(left[2]), int(right[2])))

    return (
        int(statistics.median(reds)),
        int(statistics.median(greens)),
        int(statistics.median(blues)),
    )


def _alpha_content_bbox(rgba: Image.Image, alpha_threshold: int) -> tuple[int, int, int, int] | None:
    alpha = rgba.getchannel("A")
    binary = alpha.point(lambda a: 255 if int(a) > alpha_threshold else 0, mode="L")
    return binary.getbbox()


def _opaque_content_bbox(rgba: Image.Image, border_threshold: int) -> tuple[int, int, int, int] | None:
    rgb = rgba.convert("RGB")
    bg_color = _median_border_color(rgb)
    background = Image.new("RGB", rgb.size, bg_color)
    diff = ImageChops.difference(rgb, background).convert("L")
    mask = diff.point(lambda v: 255 if int(v) > border_threshold else 0, mode="L")
    return mask.getbbox()


def _expand_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    padding_ratio: float,
    min_padding_pixels: int,
) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = bbox
    content_w = max(1, right - left)
    content_h = max(1, bottom - top)
    padding = int(max(content_w, content_h) * max(0.0, padding_ratio))
    padding = max(int(min_padding_pixels), padding)
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def detect_content_bbox(
    rgba: Image.Image,
    *,
    alpha_threshold: int = 8,
    border_threshold: int = 16,
    padding_ratio: float = 0.02,
    min_padding_pixels: int = 1,
) -> tuple[int, int, int, int]:
    bbox = _alpha_content_bbox(rgba, alpha_threshold)
    full_bbox = (0, 0, rgba.width, rgba.height)
    alpha_extrema = rgba.getchannel("A").getextrema()
    alpha_is_fully_opaque = bool(alpha_extrema and alpha_extrema[0] >= 255 and alpha_extrema[1] >= 255)
    if bbox == full_bbox and alpha_is_fully_opaque:
        bbox = None
    if bbox is None:
        bbox = _opaque_content_bbox(rgba, border_threshold)
    if bbox is None:
        return (0, 0, rgba.width, rgba.height)
    return _expand_bbox(bbox, rgba.size, padding_ratio, min_padding_pixels)


def normalize_to_square_png(
    image_bytes: bytes,
    *,
    output_size: int = 512,
    alpha_threshold: int = 8,
    border_threshold: int = 16,
    padding_ratio: float = 0.02,
    min_padding_pixels: int = 1,
) -> bytes:
    image = Image.open(BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image).convert("RGBA")

    bbox = detect_content_bbox(
        image,
        alpha_threshold=alpha_threshold,
        border_threshold=border_threshold,
        padding_ratio=padding_ratio,
        min_padding_pixels=min_padding_pixels,
    )
    cropped = image.crop(bbox)
    fitted = ImageOps.contain(
        cropped,
        (output_size, output_size),
        method=Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 0))
    paste_x = (output_size - fitted.width) // 2
    paste_y = (output_size - fitted.height) // 2
    canvas.alpha_composite(fitted, (paste_x, paste_y))

    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def apply_min_black_transparency(
    image_bytes: bytes,
    *,
    min_black_level: int = 0,
) -> bytes:
    level = max(0, min(MIN_BLACK_LEVEL_MAX, int(min_black_level)))
    if level <= 0:
        return image_bytes
    return make_background_transparent(
        image_bytes,
        options=TemplateTransparencyOptions(
            threshold=level,
            color_tolerance_mode="max",
            use_edge_flood_fill=True,
            preserve_existing_alpha=True,
        ),
        background_color=(0, 0, 0),
    )


def _destination_path(output_dir: Path, source: Path, overwrite: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / f"{source.stem}.png"
    if overwrite or not base.exists():
        return base
    index = 2
    while True:
        candidate = output_dir / f"{source.stem}_{index}.png"
        if not candidate.exists():
            return candidate
        index += 1


def _template_sequence_state(template_dir: Path) -> tuple[int, int]:
    pattern = re.compile(r"^template(\d+)$", re.IGNORECASE)
    max_idx = 0
    width = 3
    for path in template_dir.glob("*.png"):
        match = pattern.match(path.stem)
        if not match:
            continue
        digits = match.group(1)
        try:
            idx = int(digits)
        except ValueError:
            continue
        max_idx = max(max_idx, idx)
        width = max(width, len(digits))
    next_idx = max_idx + 1
    width = max(width, len(str(next_idx)))
    return next_idx, width


def _next_template_path(
    template_dir: Path,
    next_idx: int,
    width: int,
) -> tuple[Path, int]:
    idx = max(1, int(next_idx))
    while True:
        name = f"template{idx:0{max(1, width)}d}.png"
        candidate = template_dir / name
        if not candidate.exists():
            return candidate, idx + 1
        idx += 1


def prepare_images_to_512_png(
    input_paths: list[str],
    output_dir: str,
    options: ImagePrepOptions | None = None,
    progress_cb: ImagePrepProgressCallback | None = None,
) -> ImagePrepReport:
    opts = options or ImagePrepOptions()
    report = ImagePrepReport()
    sources = _iter_images([Path(path) for path in input_paths], recursive=opts.recursive)
    if not sources:
        report.details.append("No supported images found.")
        return report

    out_dir = Path(output_dir).expanduser().resolve()
    total = len(sources)
    for source in sources:
        report.attempted += 1
        destination = _destination_path(out_dir, source, opts.overwrite)
        if destination.exists() and not opts.overwrite:
            report.skipped += 1
            report.details.append(f"Skipped (exists): {source.name}")
            if progress_cb is not None:
                progress_cb(
                    report.attempted,
                    total,
                    str(source),
                    str(destination),
                    "skipped",
                    None,
                    "exists",
                )
            continue
        try:
            image_bytes = source.read_bytes()
            image_bytes = apply_min_black_transparency(
                image_bytes,
                min_black_level=opts.min_black_level,
            )
            normalized = normalize_to_square_png(
                image_bytes,
                output_size=max(32, int(opts.output_size)),
                alpha_threshold=max(0, min(255, int(opts.alpha_threshold))),
                border_threshold=max(0, min(255, int(opts.border_threshold))),
                padding_ratio=max(0.0, min(0.5, float(opts.padding_ratio))),
                min_padding_pixels=max(0, int(opts.min_padding_pixels)),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(normalized)
        except Exception as exc:
            report.failed += 1
            report.details.append(f"Failed: {source.name} ({exc})")
            if progress_cb is not None:
                progress_cb(
                    report.attempted,
                    total,
                    str(source),
                    str(destination),
                    "failed",
                    None,
                    str(exc),
                )
            continue
        report.succeeded += 1
        report.output_files.append(str(destination))
        if progress_cb is not None:
            progress_cb(
                report.attempted,
                total,
                str(source),
                str(destination),
                "succeeded",
                str(destination),
                None,
            )
    return report


def prepare_images_to_template_folder(
    input_paths: list[str],
    options: ImagePrepOptions | None = None,
    output_dir: str | None = None,
    progress_cb: ImagePrepProgressCallback | None = None,
) -> ImagePrepReport:
    opts = options or ImagePrepOptions()
    report = ImagePrepReport()
    sources = _iter_images([Path(path) for path in input_paths], recursive=opts.recursive)
    if not sources:
        report.details.append("No supported images found.")
        return report

    template_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else icon_templates_dir()
    )
    template_dir.mkdir(parents=True, exist_ok=True)
    next_idx, width = _template_sequence_state(template_dir)
    width = max(width, len(str(next_idx + len(sources))))

    total = len(sources)
    for source in sources:
        report.attempted += 1
        destination, next_idx = _next_template_path(template_dir, next_idx, width)
        try:
            image_bytes = source.read_bytes()
            image_bytes = apply_min_black_transparency(
                image_bytes,
                min_black_level=opts.min_black_level,
            )
            normalized = normalize_to_square_png(
                image_bytes,
                output_size=max(32, int(opts.output_size)),
                alpha_threshold=max(0, min(255, int(opts.alpha_threshold))),
                border_threshold=max(0, min(255, int(opts.border_threshold))),
                padding_ratio=max(0.0, min(0.5, float(opts.padding_ratio))),
                min_padding_pixels=max(0, int(opts.min_padding_pixels)),
            )
            destination.write_bytes(normalized)
        except Exception as exc:
            report.failed += 1
            report.details.append(f"Failed: {source.name} ({exc})")
            if progress_cb is not None:
                progress_cb(
                    report.attempted,
                    total,
                    str(source),
                    str(destination),
                    "failed",
                    None,
                    str(exc),
                )
            continue
        report.succeeded += 1
        report.output_files.append(str(destination))
        if progress_cb is not None:
            progress_cb(
                report.attempted,
                total,
                str(source),
                str(destination),
                "succeeded",
                str(destination),
                None,
            )
    return report
