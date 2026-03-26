from __future__ import annotations

from collections import deque
import colorsys
from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
import statistics

from PIL import Image, ImageOps


_FALLOFF_ALIASES: dict[str, str] = {
    "linear": "lin",
    "cosine": "cos",
    "gaussian": "gauss",
}
TEMPLATE_FALLOFF_VALUES: tuple[str, ...] = (
    "flat",
    "lin",
    "smooth",
    "cos",
    "exp",
    "log",
    "gauss",
)


@dataclass(slots=True)
class TemplateTransparencyOptions:
    threshold: int = 22
    color_tolerance_mode: str = "max"  # max | euclidean
    compare_color_space: str = "rgb"  # rgb | hsv
    falloff_mode: str = "flat"  # flat | lin | smooth | cos | exp | log | gauss
    curve_strength: int = 50  # 0..100 (used for exp/log/gauss)
    use_edge_flood_fill: bool = True
    use_center_flood_fill: bool = False
    preserve_existing_alpha: bool = True


def normalize_falloff_mode(mode: str | None) -> str:
    token = str(mode or "flat").strip().casefold()
    token = _FALLOFF_ALIASES.get(token, token)
    if token not in TEMPLATE_FALLOFF_VALUES:
        return "flat"
    return token


def falloff_uses_curve_strength(mode: str | None) -> bool:
    return normalize_falloff_mode(mode) in {"exp", "log", "gauss"}


def default_curve_strength_for_falloff(mode: str | None) -> int:
    token = normalize_falloff_mode(mode)
    if token == "exp":
        return 35
    if token == "log":
        return 65
    if token == "gauss":
        return 45
    return 50


def _distance_max(rgb: tuple[int, int, int], ref: tuple[int, int, int]) -> int:
    return max(abs(rgb[0] - ref[0]), abs(rgb[1] - ref[1]), abs(rgb[2] - ref[2]))


def _distance_euclidean(rgb: tuple[int, int, int], ref: tuple[int, int, int]) -> float:
    dr = rgb[0] - ref[0]
    dg = rgb[1] - ref[1]
    db = rgb[2] - ref[2]
    return (dr * dr + dg * dg + db * db) ** 0.5


def _rgb_to_hsv255(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r = max(0, min(255, int(rgb[0]))) / 255.0
    g = max(0, min(255, int(rgb[1]))) / 255.0
    b = max(0, min(255, int(rgb[2]))) / 255.0
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return (
        int(round(h * 255.0)) % 256,
        int(round(s * 255.0)),
        int(round(v * 255.0)),
    )


def _hue_diff(a: int, b: int) -> int:
    delta = abs(int(a) - int(b)) % 256
    return min(delta, 256 - delta)


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
    space: str,
) -> bool:
    return _color_distance(rgb, ref, mode=mode, space=space) <= float(threshold)


def _color_distance(
    rgb: tuple[int, int, int],
    ref_rgb: tuple[int, int, int],
    *,
    mode: str,
    space: str,
) -> float:
    if space == "hsv":
        hsv = _rgb_to_hsv255(rgb)
        hsv_ref = _rgb_to_hsv255(ref_rgb)
        dh = _hue_diff(hsv[0], hsv_ref[0])
        ds = abs(hsv[1] - hsv_ref[1])
        dv = abs(hsv[2] - hsv_ref[2])
        if mode == "euclidean":
            return float((dh * dh + ds * ds + dv * dv) ** 0.5)
        return float(max(dh, ds, dv))
    if mode == "euclidean":
        return float(_distance_euclidean(rgb, ref_rgb))
    return float(_distance_max(rgb, ref_rgb))


def _curve_param_from_strength(strength: int) -> float:
    s = max(0, min(100, int(strength)))
    k_min = 0.35
    k_max = 8.0
    ratio = (k_max / k_min) ** (s / 100.0)
    return k_min * ratio


def _sigma_from_strength(strength: int) -> float:
    s = max(0, min(100, int(strength)))
    sigma_max = 1.2
    sigma_min = 0.15
    ratio = (sigma_min / sigma_max) ** (s / 100.0)
    return sigma_max * ratio


def _falloff_removal(
    distance: float,
    *,
    tolerance: int,
    mode: str,
    curve_strength: int,
) -> float:
    tol = max(0, int(tolerance))
    d = max(0.0, float(distance))
    normalized_mode = normalize_falloff_mode(mode)
    if normalized_mode == "flat":
        return 1.0 if d <= float(tol) else 0.0

    radius = float(tol + 1)
    if radius <= 0.0:
        return 0.0
    u = max(0.0, min(1.0, d / radius))
    if normalized_mode == "lin":
        return max(0.0, 1.0 - u)
    if normalized_mode == "smooth":
        smooth = (3.0 * u * u) - (2.0 * u * u * u)
        return max(0.0, min(1.0, 1.0 - smooth))
    if normalized_mode == "cos":
        return max(0.0, min(1.0, math.cos((math.pi * 0.5) * u)))
    if normalized_mode == "exp":
        k = _curve_param_from_strength(curve_strength)
        den = 1.0 - math.exp(-k)
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        num = math.exp(-k * u) - math.exp(-k)
        return max(0.0, min(1.0, num / den))
    if normalized_mode == "log":
        k = _curve_param_from_strength(curve_strength)
        den = math.log1p(k)
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        return max(0.0, min(1.0, 1.0 - (math.log1p(k * u) / den)))
    if normalized_mode == "gauss":
        sigma = _sigma_from_strength(curve_strength)
        if sigma <= 1e-9:
            return max(0.0, 1.0 - u)
        g_u = math.exp(-(u * u) / (2.0 * sigma * sigma))
        g_1 = math.exp(-1.0 / (2.0 * sigma * sigma))
        den = 1.0 - g_1
        if den <= 1e-9:
            return max(0.0, 1.0 - u)
        return max(0.0, min(1.0, (g_u - g_1) / den))
    return max(0.0, 1.0 - u)


def _flood_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
    space: str,
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
        if _is_close_color(color, background_color, threshold, mode, space):
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
    space: str,
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
        space=space,
        seeds=seeds,
    )


def _center_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
    space: str,
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
        space=space,
        seeds=seeds,
    )


def _global_background_mask(
    image: Image.Image,
    *,
    background_color: tuple[int, int, int],
    threshold: int,
    mode: str,
    space: str,
) -> list[bool]:
    rgb = image.convert("RGB")
    px = rgb.load()
    width, height = rgb.size
    total = width * height
    mask = [False] * total
    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if _is_close_color(px[x, y], background_color, threshold, mode, space):
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
    space = opts.compare_color_space.strip().casefold()
    if space not in {"rgb", "hsv"}:
        space = "rgb"
    falloff_mode = normalize_falloff_mode(opts.falloff_mode)
    curve_strength = max(0, min(100, int(opts.curve_strength)))
    mask_threshold = threshold if falloff_mode == "flat" else min(255, threshold + 1)

    if opts.use_edge_flood_fill:
        bg_mask = _edge_background_mask(
            image,
            background_color=bg,
            threshold=mask_threshold,
            mode=mode,
            space=space,
        )
    elif opts.use_center_flood_fill:
        bg_mask = _center_background_mask(
            image,
            background_color=bg,
            threshold=mask_threshold,
            mode=mode,
            space=space,
        )
    else:
        bg_mask = _global_background_mask(
            image,
            background_color=bg,
            threshold=mask_threshold,
            mode=mode,
            space=space,
        )
    if opts.use_edge_flood_fill and opts.use_center_flood_fill:
        center_mask = _center_background_mask(
            image,
            background_color=bg,
            threshold=mask_threshold,
            mode=mode,
            space=space,
        )
        bg_mask = [a or b for a, b in zip(bg_mask, center_mask)]

    out = image.copy()
    px = out.load()
    src_alpha = image.getchannel("A").load()
    if falloff_mode == "flat":
        for y in range(height):
            for x in range(width):
                idx = y * width + x
                if not bg_mask[idx]:
                    continue
                r, g, b, a = px[x, y]
                if opts.preserve_existing_alpha:
                    px[x, y] = (r, g, b, 0 if src_alpha[x, y] > 0 else a)
                else:
                    px[x, y] = (r, g, b, 0)
        buffer = BytesIO()
        out.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    for y in range(height):
        for x in range(width):
            idx = y * width + x
            r, g, b, a = px[x, y]
            if bg_mask[idx]:
                distance = _color_distance((r, g, b), bg, mode=mode, space=space)
                removal = _falloff_removal(
                    distance,
                    tolerance=threshold,
                    mode=falloff_mode,
                    curve_strength=curve_strength,
                )
                if removal <= 0.0:
                    continue
                alpha_base = int(src_alpha[x, y]) if opts.preserve_existing_alpha else int(a)
                new_alpha = int(round(float(alpha_base) * (1.0 - removal)))
                px[x, y] = (r, g, b, max(0, min(255, new_alpha)))
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
