from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import statistics

from PIL import Image, ImageOps


@dataclass(slots=True)
class TemplateTransparencyOptions:
    threshold: int = 22
    color_tolerance_mode: str = "max"  # max | euclidean
    use_edge_flood_fill: bool = True
    use_center_flood_fill: bool = False
    preserve_existing_alpha: bool = True


def _distance_max(rgb: tuple[int, int, int], ref: tuple[int, int, int]) -> int:
    return max(abs(rgb[0] - ref[0]), abs(rgb[1] - ref[1]), abs(rgb[2] - ref[2]))


def _distance_euclidean(rgb: tuple[int, int, int], ref: tuple[int, int, int]) -> float:
    dr = rgb[0] - ref[0]
    dg = rgb[1] - ref[1]
    db = rgb[2] - ref[2]
    return (dr * dr + dg * dg + db * db) ** 0.5


def _median_border_color(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    px = rgb.load()
    width, height = rgb.size
    rs: list[int] = []
    gs: list[int] = []
    bs: list[int] = []
    for x in range(width):
        r1, g1, b1 = px[x, 0]
        r2, g2, b2 = px[x, height - 1]
        rs.extend((r1, r2))
        gs.extend((g1, g2))
        bs.extend((b1, b2))
    for y in range(height):
        r1, g1, b1 = px[0, y]
        r2, g2, b2 = px[width - 1, y]
        rs.extend((r1, r2))
        gs.extend((g1, g2))
        bs.extend((b1, b2))
    return (
        int(statistics.median(rs)),
        int(statistics.median(gs)),
        int(statistics.median(bs)),
    )


def _is_close_color(
    rgb: tuple[int, int, int],
    ref: tuple[int, int, int],
    threshold: int,
    mode: str,
) -> bool:
    if mode == "euclidean":
        return _distance_euclidean(rgb, ref) <= float(threshold)
    return _distance_max(rgb, ref) <= threshold


def _flood_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
    seeds: list[tuple[int, int]],
) -> list[bool]:
    rgb = image.convert("RGB")
    px = rgb.load()
    width, height = rgb.size
    total = width * height
    mask = [False] * total
    queue: deque[int] = deque()

    def _index(x: int, y: int) -> int:
        return y * width + x

    def _try_enqueue(x: int, y: int) -> None:
        idx = _index(x, y)
        if mask[idx]:
            return
        color = px[x, y]
        if _is_close_color(color, background_color, threshold, mode):
            mask[idx] = True
            queue.append(idx)

    for sx, sy in seeds:
        if 0 <= sx < width and 0 <= sy < height:
            _try_enqueue(sx, sy)

    while queue:
        idx = queue.popleft()
        x = idx % width
        y = idx // width
        if x > 0:
            _try_enqueue(x - 1, y)
        if x + 1 < width:
            _try_enqueue(x + 1, y)
        if y > 0:
            _try_enqueue(x, y - 1)
        if y + 1 < height:
            _try_enqueue(x, y + 1)
    return mask


def _edge_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
) -> list[bool]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    seeds: list[tuple[int, int]] = []
    for x in range(width):
        seeds.append((x, 0))
        seeds.append((x, height - 1))
    for y in range(height):
        seeds.append((0, y))
        seeds.append((width - 1, y))
    return _flood_background_mask(
        image,
        background_color=background_color,
        threshold=threshold,
        mode=mode,
        seeds=seeds,
    )


def _center_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
) -> list[bool]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    cx = width // 2
    cy = height // 2
    seeds = [
        (cx, cy),
        (cx - 1, cy),
        (cx + 1, cy),
        (cx, cy - 1),
        (cx, cy + 1),
    ]
    return _flood_background_mask(
        image,
        background_color=background_color,
        threshold=threshold,
        mode=mode,
        seeds=seeds,
    )


def _global_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
) -> list[bool]:
    rgb = image.convert("RGB")
    px = rgb.load()
    width, height = rgb.size
    total = width * height
    mask = [False] * total
    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if _is_close_color(px[x, y], background_color, threshold, mode):
                mask[idx] = True
    return mask


def make_background_transparent(
    image_bytes: bytes,
    *,
    options: TemplateTransparencyOptions | None = None,
    background_color: tuple[int, int, int] | None = None,
) -> bytes:
    opts = options or TemplateTransparencyOptions()
    image = Image.open(BytesIO(image_bytes))
    image.load()
    image = ImageOps.exif_transpose(image).convert("RGBA")
    width, height = image.size
    if width <= 0 or height <= 0:
        return image_bytes

    bg = background_color if background_color is not None else _median_border_color(image)
    threshold = max(0, min(255, int(opts.threshold)))
    mode = opts.color_tolerance_mode.strip().casefold()
    if mode not in {"max", "euclidean"}:
        mode = "max"

    if opts.use_edge_flood_fill:
        bg_mask = _edge_background_mask(
            image,
            background_color=bg,
            threshold=threshold,
            mode=mode,
        )
    elif opts.use_center_flood_fill:
        bg_mask = _center_background_mask(
            image,
            background_color=bg,
            threshold=threshold,
            mode=mode,
        )
    else:
        bg_mask = _global_background_mask(
            image,
            background_color=bg,
            threshold=threshold,
            mode=mode,
        )
    if opts.use_edge_flood_fill and opts.use_center_flood_fill:
        center_mask = _center_background_mask(
            image,
            background_color=bg,
            threshold=threshold,
            mode=mode,
        )
        bg_mask = [a or b for a, b in zip(bg_mask, center_mask)]

    out = image.copy()
    px = out.load()
    src_alpha = image.getchannel("A").load()
    for y in range(height):
        for x in range(width):
            idx = y * width + x
            r, g, b, a = px[x, y]
            if bg_mask[idx]:
                if opts.preserve_existing_alpha:
                    px[x, y] = (r, g, b, 0 if src_alpha[x, y] > 0 else a)
                else:
                    px[x, y] = (r, g, b, 0)
    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def process_template_file(
    input_path: str,
    output_path: str,
    *,
    options: TemplateTransparencyOptions | None = None,
    background_color: tuple[int, int, int] | None = None,
) -> None:
    source = Path(input_path)
    target = Path(output_path)
    payload = source.read_bytes()
    converted = make_background_transparent(
        payload,
        options=options,
        background_color=background_color,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(converted)
