from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from gamemanager.services.icon_pipeline import build_multi_size_ico
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.pillow_image import load_image_rgba_bytes
from gamemanager.services.steamgriddb_upload import download_image_bytes, list_game_icons


@dataclass(slots=True)
class VisualOriginMatch:
    confidence: float
    matched_icon_id: int | None
    scanned_icons: int


def _load_ico_frame(image_bytes: bytes, size: int = 256) -> Image.Image:
    frame = load_image_rgba_bytes(image_bytes, preferred_ico_size=int(size))
    if frame.size != (int(size), int(size)):
        frame = frame.resize((int(size), int(size)), Image.Resampling.LANCZOS)
    return frame


def _normalize_rgba_bytes(image: Image.Image, size: int = 256) -> bytes:
    out = BytesIO()
    image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(
        out,
        format="PNG",
    )
    return out.getvalue()


def normalized_icon_png_bytes(icon_path: str | Path, size: int = 256) -> bytes:
    path = Path(icon_path)
    raw = path.read_bytes()
    frame = _load_ico_frame(raw, size=size)
    return _normalize_rgba_bytes(frame, size=size)


def icon_fingerprint256_from_ico(icon_path: str | Path) -> str:
    payload = normalized_icon_png_bytes(icon_path, size=256)
    return hashlib.sha256(payload).hexdigest()


def processed_fingerprint256_from_source_image(source_image_bytes: bytes) -> str:
    ico_payload = build_multi_size_ico(
        source_image_bytes,
        icon_style="none",
        bg_removal_engine="none",
        bg_removal_params={},
        text_preserve_config={"enabled": False, "method": "none"},
        border_shader={"enabled": False},
    )
    frame = _load_ico_frame(ico_payload, size=256)
    normalized = _normalize_rgba_bytes(frame, size=256)
    return hashlib.sha256(normalized).hexdigest()


def _dhash_16x16(image: Image.Image) -> int:
    gray = image.convert("L").resize((17, 16), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for y in range(16):
        row = y * 17
        for x in range(16):
            bits <<= 1
            left = pixels[row + x]
            right = pixels[row + x + 1]
            bits |= 1 if left > right else 0
    return bits


def _similarity_score(a: Image.Image, b: Image.Image) -> float:
    left = a.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
    right = b.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
    diff = ImageChops.difference(left, right)
    mean = ImageStat.Stat(diff).mean
    # RGBA means are in [0, 255].
    rgba_delta = sum(float(value) for value in mean[:4]) / (4.0 * 255.0)
    pixel_sim = 1.0 - max(0.0, min(1.0, rgba_delta))

    alpha_diff = ImageChops.difference(left.getchannel("A"), right.getchannel("A"))
    alpha_mean = ImageStat.Stat(alpha_diff).mean
    alpha_delta = float(alpha_mean[0]) / 255.0 if alpha_mean else 1.0
    alpha_sim = 1.0 - max(0.0, min(1.0, alpha_delta))

    hash_left = _dhash_16x16(left)
    hash_right = _dhash_16x16(right)
    hamming = (hash_left ^ hash_right).bit_count()
    hash_sim = 1.0 - (hamming / 256.0)

    combined = (0.55 * pixel_sim) + (0.25 * alpha_sim) + (0.20 * hash_sim)
    return max(0.0, min(1.0, combined))


def _build_processed_256_from_source(source_image_bytes: bytes) -> Image.Image:
    ico_payload = build_multi_size_ico(
        source_image_bytes,
        icon_style="none",
        bg_removal_engine="none",
        bg_removal_params={},
        text_preserve_config={"enabled": False, "method": "none"},
        border_shader={"enabled": False},
    )
    return _load_ico_frame(ico_payload, size=256)


def detect_sgdb_origin_by_visual(
    *,
    local_icon_path: str | Path,
    game_id: int,
    settings: IconSearchSettings,
    threshold: float = 0.95,
) -> VisualOriginMatch:
    if int(game_id) <= 0:
        return VisualOriginMatch(confidence=0.0, matched_icon_id=None, scanned_icons=0)
    if not Path(local_icon_path).exists():
        return VisualOriginMatch(confidence=0.0, matched_icon_id=None, scanned_icons=0)
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return VisualOriginMatch(confidence=0.0, matched_icon_id=None, scanned_icons=0)

    local_img = Image.open(BytesIO(normalized_icon_png_bytes(local_icon_path, size=256))).convert("RGBA")
    try:
        assets = list_game_icons(settings, int(game_id), limit=50, max_pages=2)
    except Exception:
        return VisualOriginMatch(confidence=0.0, matched_icon_id=None, scanned_icons=0)

    best_conf = 0.0
    best_id: int | None = None
    scanned = 0
    for asset in assets[:24]:
        try:
            source_bytes = download_image_bytes(asset.url, timeout_seconds=max(8.0, settings.timeout_seconds))
            sgdb_processed = _build_processed_256_from_source(source_bytes)
            confidence = _similarity_score(local_img, sgdb_processed)
        except Exception:
            continue
        scanned += 1
        if confidence > best_conf:
            best_conf = confidence
            best_id = int(asset.icon_id)
        if confidence >= float(threshold):
            break
    return VisualOriginMatch(
        confidence=max(0.0, min(1.0, best_conf)),
        matched_icon_id=best_id,
        scanned_icons=scanned,
    )
