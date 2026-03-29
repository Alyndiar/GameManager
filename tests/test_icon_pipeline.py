from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from gamemanager.services import icon_pipeline
from gamemanager.services.icon_pipeline import (
    ICO_SIZES,
    apply_text_preserve_to_cutout,
    build_text_extraction_overlay,
    build_multi_size_ico,
    build_preview_png,
    normalize_text_preserve_config,
    text_preserve_to_dict,
)


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


def test_build_multi_size_ico_authors_explicit_frames_per_size(monkeypatch) -> None:
    calls: list[int] = []

    def _fake_build(_master, size, _template, foreground=None, border_shader=None):
        calls.append(int(size))
        return Image.new("RGBA", (int(size), int(size)), (int(size) % 256, 0, 0, 255))

    monkeypatch.setattr(icon_pipeline, "_build_composited_icon", _fake_build)
    monkeypatch.setattr(icon_pipeline, "_apply_size_profile", lambda image, size, *_args: image)
    monkeypatch.setattr(
        icon_pipeline,
        "_tiny_icon_legibility_pass",
        lambda image, size, *_args: image,
    )

    ico_data = build_multi_size_ico(
        _sample_png_bytes(),
        circular_ring=False,
        icon_style="none",
        size_improvements={
            int(size): {"pre_enabled": False, "silhouette_enabled": False, "tiny_enabled": False}
            for size in ICO_SIZES
        },
    )
    assert calls == [int(size) for size in ICO_SIZES]

    for size in ICO_SIZES:
        opened = Image.open(BytesIO(ico_data))
        opened.size = (int(size), int(size))
        opened.load()
        frame = opened.convert("RGBA")
        assert frame.getpixel((0, 0))[0] == int(size) % 256


def test_build_preview_png_outputs_png_bytes() -> None:
    preview = build_preview_png(_sample_png_bytes(), size=96, circular_ring=False)
    opened = Image.open(BytesIO(preview))
    assert opened.format == "PNG"
    assert opened.size == (96, 96)
    assert len(ICO_SIZES) >= 5


def test_tiny_icon_legibility_pass_skips_large_sizes() -> None:
    src = Image.new("RGBA", (64, 64), (80, 120, 200, 255))
    out = icon_pipeline._tiny_icon_legibility_pass(src, 48)
    assert out.tobytes() == src.tobytes()


def test_tiny_icon_legibility_pass_cleans_soft_alpha_in_tiny_sizes() -> None:
    src = Image.new("RGBA", (16, 16), (120, 110, 90, 255))
    src.putpixel((1, 1), (240, 240, 240, 6))
    src.putpixel((2, 1), (240, 240, 240, 18))
    out = icon_pipeline._tiny_icon_legibility_pass(src, 16)
    alpha = out.getchannel("A")
    assert alpha.getpixel((1, 1)) == 0
    assert alpha.getpixel((2, 1)) > 0


def test_tiny_icon_legibility_pass_prunes_isolated_tiny_islands() -> None:
    src = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    src.putpixel((1, 1), (255, 255, 255, 255))
    for x in range(10, 13):
        for y in range(10, 13):
            src.putpixel((x, y), (255, 255, 255, 255))
    out = icon_pipeline._tiny_icon_legibility_pass(
        src,
        16,
        {
            16: {
                "tiny_enabled": True,
                "tiny_unsharp_enabled": False,
                "tiny_micro_contrast_enabled": False,
                "tiny_alpha_cleanup_enabled": False,
                "tiny_prune_enabled": True,
                "tiny_prune_min_pixels": 3,
                "tiny_prune_alpha_threshold": 10,
            }
        },
    )
    alpha = out.getchannel("A")
    assert alpha.getpixel((1, 1)) == 0
    assert alpha.getpixel((11, 11)) > 0


def test_normalize_silhouette_boosts_tiny_subject_coverage() -> None:
    src = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(src)
    draw.rectangle((10, 10, 12, 12), fill=(255, 200, 100, 255))
    before_coverage = sum(1 for value in src.getchannel("A").tobytes() if int(value) > 8) / float(
        24 * 24
    )
    out = icon_pipeline._normalize_silhouette(
        src,
        24,
        {
            24: {
                "silhouette_enabled": True,
                "silhouette_target_min": 0.20,
                "silhouette_target_max": 0.60,
                "silhouette_alpha_threshold": 8,
                "silhouette_max_upscale": 3.0,
                "silhouette_min_scale": 0.5,
            }
        },
    )
    after_coverage = sum(
        1 for value in out.getchannel("A").tobytes() if int(value) > 8
    ) / float(24 * 24)
    assert after_coverage > before_coverage


def test_pre_downscale_prepare_source_uses_working_scale() -> None:
    src = Image.new("RGBA", (64, 64), (120, 80, 30, 255))
    out = icon_pipeline._pre_downscale_prepare_source(
        src,
        24,
        {
            24: {
                "pre_enabled": True,
                "pre_working_scale": 2.5,
                "pre_simplify_enabled": False,
                "pre_prune_enabled": False,
                "pre_stroke_boost_enabled": False,
            }
        },
    )
    assert out.size == (60, 60)


def test_normalize_silhouette_does_not_shrink_when_downscale_disabled() -> None:
    src = Image.new("RGBA", (24, 24), (180, 120, 60, 255))
    out = icon_pipeline._normalize_silhouette(
        src,
        24,
        {
            24: {
                "silhouette_enabled": True,
                "silhouette_target_min": 0.10,
                "silhouette_target_max": 0.30,
                "silhouette_alpha_threshold": 8,
                "silhouette_max_upscale": 2.0,
                "silhouette_min_scale": 0.5,
                "silhouette_allow_downscale": False,
            }
        },
    )
    assert out.tobytes() == src.tobytes()


def test_bordered_preview_keeps_outside_transparent() -> None:
    preview = build_preview_png(_sample_png_bytes(), size=128, circular_ring=True)
    opened = Image.open(BytesIO(preview)).convert("RGBA")
    alpha = opened.getchannel("A")
    assert alpha.getpixel((0, 0)) == 0


def test_opaque_cutout_output_is_ignored(monkeypatch) -> None:
    opaque = Image.new("RGBA", (400, 280), (0, 255, 0, 255))
    b = BytesIO()
    opaque.save(b, format="PNG")
    opaque_png = b.getvalue()

    monkeypatch.setattr(
        icon_pipeline,
        "remove_background_bytes",
        lambda _bytes, engine=None, params=None: opaque_png,
    )
    no_cutout = build_preview_png(
        _sample_png_bytes(),
        size=128,
        circular_ring=True,
        bg_removal_engine="none",
    )
    with_cutout = build_preview_png(
        _sample_png_bytes(),
        size=128,
        circular_ring=True,
        bg_removal_engine="rembg",
    )
    no_img = Image.open(BytesIO(no_cutout)).convert("RGBA")
    yes_img = Image.open(BytesIO(with_cutout)).convert("RGBA")
    assert no_img.getpixel((64, 64)) == yes_img.getpixel((64, 64))


def _source_with_bottom_text() -> bytes:
    img = Image.new("RGBA", (512, 512), (3, 8, 16, 255))
    draw = ImageDraw.Draw(img)
    draw.text((146, 486), "ALIEN", fill=(255, 255, 255, 255))
    payload = BytesIO()
    img.save(payload, format="PNG")
    return payload.getvalue()


def test_text_extraction_works_with_cutout_disabled() -> None:
    source = _source_with_bottom_text()
    without_text = build_preview_png(
        source,
        size=512,
        icon_style="round",
        bg_removal_engine="none",
        text_preserve_config={"enabled": False},
    )
    with_text = build_preview_png(
        source,
        size=512,
        icon_style="round",
        bg_removal_engine="none",
        text_preserve_config={"enabled": True, "strength": 80, "feather": 1},
    )
    without_alpha = Image.open(BytesIO(without_text)).convert("RGBA").getchannel("A")
    with_alpha = Image.open(BytesIO(with_text)).convert("RGBA").getchannel("A")
    # Bottom text is drawn near y=486 and should reappear outside the ring when
    # text extraction is enabled, even with cutout engine disabled.
    assert with_alpha.getpixel((158, 492)) > without_alpha.getpixel((158, 492))


def test_text_extraction_feather_control_changes_soft_edges() -> None:
    source = Image.open(BytesIO(_source_with_bottom_text())).convert("RGBA")
    transparent_cutout = Image.new("RGBA", source.size, (0, 0, 0, 0))
    sharp = apply_text_preserve_to_cutout(
        source,
        transparent_cutout,
        {"enabled": True, "strength": 80, "feather": 0},
    )
    soft = apply_text_preserve_to_cutout(
        source,
        transparent_cutout,
        {"enabled": True, "strength": 80, "feather": 3},
    )
    sharp_alpha = sharp.getchannel("A")
    soft_alpha = soft.getchannel("A")
    sharp_partial = sum(1 for value in sharp_alpha.tobytes() if 0 < int(value) < 255)
    soft_partial = sum(1 for value in soft_alpha.tobytes() if 0 < int(value) < 255)
    assert soft_partial >= sharp_partial


def test_text_extraction_method_normalization() -> None:
    # Backward compatibility: enabled=True with missing method maps to heuristic.
    cfg = normalize_text_preserve_config({"enabled": True, "strength": 40, "feather": 1})
    assert cfg.method == "heuristic"
    assert cfg.enabled is True
    # Explicit none wins and disables extraction.
    cfg_none = normalize_text_preserve_config({"enabled": True, "method": "none"})
    assert cfg_none.method == "none"
    assert cfg_none.enabled is False


def test_text_seed_colors_normalize_to_group_count_and_range() -> None:
    cfg = normalize_text_preserve_config(
        {
            "enabled": True,
            "method": "heuristic",
            "color_groups": 3,
            "seed_colors": [
                [255, 12, 34],
                {"r": -10, "g": 300, "b": 120},
                [40, 50, 60],
                [1, 2, 3],  # truncated to color_groups
            ],
            "seed_tolerance": 300,
        }
    )
    assert cfg.color_groups == 3
    assert cfg.seed_colors == ((255, 12, 34), (0, 255, 120), (40, 50, 60))
    assert cfg.seed_tolerance == 96


def test_text_seed_colors_roundtrip_dict() -> None:
    payload = text_preserve_to_dict(
        {
            "enabled": True,
            "method": "heuristic",
            "seed_colors": [(12, 34, 56), [200, 10, 0]],
            "seed_tolerance": 18,
        }
    )
    assert payload["seed_colors"] == [[12, 34, 56], [200, 10, 0]]
    assert payload["seed_tolerance"] == 18


def test_template_disabled_forces_cutout_and_text_disabled(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def _fake_escape(
        _image_bytes,
        bg_removal_engine,
        bg_removal_params=None,
        text_preserve_config=None,
    ):
        observed["engine"] = bg_removal_engine
        cfg = normalize_text_preserve_config(text_preserve_config)
        observed["text_method"] = cfg.method
        observed["text_enabled"] = cfg.enabled
        return None

    monkeypatch.setattr(icon_pipeline, "_escape_foreground_image", _fake_escape)
    build_preview_png(
        _sample_png_bytes(),
        size=96,
        icon_style="none",
        bg_removal_engine="rembg",
        text_preserve_config={"enabled": True, "method": "heuristic", "strength": 80},
    )
    assert observed["engine"] == "none"
    assert observed["text_method"] == "none"
    assert observed["text_enabled"] is False


def test_text_mask_is_not_blocked_by_opaque_cutout(monkeypatch) -> None:
    source = Image.new("RGBA", (128, 128), (20, 20, 20, 255))
    cutout = Image.new("RGBA", (128, 128), (20, 20, 20, 255))
    forced = Image.new("L", (128, 128), 0)
    forced.putpixel((40, 96), 255)

    monkeypatch.setattr(icon_pipeline, "_detected_text_mask", lambda *_args, **_kwargs: forced)
    overlay = build_text_extraction_overlay(
        source,
        cutout,
        {"enabled": True, "method": "paddleocr", "strength": 70, "feather": 0},
    )
    assert overlay is not None
    alpha = overlay.getchannel("A")
    assert alpha.getpixel((40, 96)) > 0


def test_glow_is_soft_alpha_not_opaque() -> None:
    source = Image.new("RGBA", (192, 192), (8, 8, 8, 255))
    draw = ImageDraw.Draw(source)
    draw.rectangle((66, 96, 126, 128), fill=(255, 255, 255, 255))
    transparent_cutout = Image.new("RGBA", source.size, (0, 0, 0, 0))

    overlay = build_text_extraction_overlay(
        source,
        transparent_cutout,
        {
            "enabled": True,
            "method": "heuristic",
            "strength": 85,
            "feather": 0,
            "include_outline": False,
            "include_shadow": False,
            "glow_mode": "both",
            "glow_radius": 3,
            "glow_strength": 100,
        },
    )
    assert overlay is not None
    alpha = overlay.getchannel("A").tobytes()
    partial = sum(1 for value in alpha if 0 < int(value) < 255)
    assert partial > 0


def test_roi_guided_never_underperforms_heuristic(monkeypatch) -> None:
    source = Image.new("RGBA", (96, 96), (5, 5, 5, 255))
    cutout = Image.new("RGBA", (96, 96), (0, 0, 0, 0))

    def _fake_heuristic(_rgb, _alpha, _cfg):
        mask = Image.new("L", (96, 96), 0)
        mask.putpixel((32, 80), 255)
        return mask

    def _fake_roi(_rgb, _alpha, _cfg):
        return Image.new("L", (96, 96), 0)

    monkeypatch.setattr(icon_pipeline, "_heuristic_text_candidate_mask", _fake_heuristic)
    monkeypatch.setattr(icon_pipeline, "_roi_guided_text_candidate_mask", _fake_roi)

    overlay = build_text_extraction_overlay(
        source,
        cutout,
        {"enabled": True, "method": "roi_guided", "strength": 70, "feather": 0},
    )
    assert overlay is not None
    alpha = overlay.getchannel("A")
    assert alpha.getpixel((32, 80)) > 0


def test_expand_seed_color_regions_recovers_gradient_text_family() -> None:
    cv2 = pytest.importorskip("cv2")
    _ = cv2  # silence lint for optional import
    source = Image.new("RGB", (220, 90), (12, 16, 24))
    draw = ImageDraw.Draw(source)
    # Gradient-like text family proxy.
    for x in range(20, 200):
        t = (x - 20) / 180.0
        color = (180 + int(60 * t), 30 + int(70 * t), 30 + int(25 * t))
        draw.rectangle((x, 30, x, 54), fill=color)
    seed = Image.new("L", source.size, 0)
    ImageDraw.Draw(seed).rectangle((95, 34, 125, 50), fill=255)
    cfg = icon_pipeline.normalize_text_preserve_config(
        {"enabled": True, "method": "roi_guided", "strength": 85, "color_groups": 6}
    )
    grown = icon_pipeline._expand_seed_color_regions(seed, source, cfg)
    # Should grow beyond the initial seed bounds in both directions.
    assert grown.getpixel((84, 40)) > 0
    assert grown.getpixel((136, 40)) > 0


def test_suppress_text_flakes_trims_attached_bleed() -> None:
    cv2 = pytest.importorskip("cv2")
    _ = cv2
    seed = Image.new("L", (220, 120), 0)
    seed_draw = ImageDraw.Draw(seed)
    seed_draw.rectangle((70, 50, 140, 78), fill=255)

    candidate = Image.new("L", (220, 120), 0)
    cand_draw = ImageDraw.Draw(candidate)
    # Main text block.
    cand_draw.rectangle((66, 46, 146, 84), fill=255)
    # Attached flake/bleed to the right.
    cand_draw.rectangle((146, 58, 210, 96), fill=255)
    # Thin bridge that makes it contiguous.
    cand_draw.rectangle((142, 62, 149, 72), fill=255)

    cfg = icon_pipeline.normalize_text_preserve_config(
        {"enabled": True, "method": "roi_guided", "strength": 80}
    )
    cleaned = icon_pipeline._suppress_text_flakes(candidate, seed, cfg)
    # Text core should remain.
    assert cleaned.getpixel((95, 62)) > 0
    # Far attached bleed should be trimmed.
    assert cleaned.getpixel((205, 90)) == 0


def test_roi_hard_gates_final_alpha_to_roi(monkeypatch) -> None:
    source = Image.new("RGBA", (120, 80), (20, 20, 20, 255))
    cutout = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    def _forced_roi_local(rgb, _alpha, _cfg):
        mask = Image.new("L", rgb.size, 0)
        # Force a full candidate in the working image (ROI crop size).
        ImageDraw.Draw(mask).rectangle((0, 0, rgb.width - 1, rgb.height - 1), fill=255)
        return mask

    monkeypatch.setattr(icon_pipeline, "_heuristic_text_candidate_mask", _forced_roi_local)

    overlay = build_text_extraction_overlay(
        source,
        cutout,
        {
            "enabled": True,
            "method": "heuristic",
            "strength": 80,
            "feather": 0,
            "roi": [0.25, 0.25, 0.25, 0.25],  # x=30..60, y=20..40
        },
    )
    assert overlay is not None
    alpha = overlay.getchannel("A")
    assert alpha.getpixel((10, 10)) == 0
    assert alpha.getpixel((40, 30)) > 0
