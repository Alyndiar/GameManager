from PIL import Image

from gamemanager.services import icon_pipeline_templates as templates


def test_normalize_background_fill_mode_defaults_to_black() -> None:
    assert templates.normalize_background_fill_mode("unknown") == "black"
    assert templates.normalize_background_fill_mode(None) == "black"
    assert templates.normalize_background_fill_mode("edge_stretch") == "edge_stretch"
    assert templates.normalize_background_fill_mode("mirror") == "mirror"
    assert templates.normalize_background_fill_mode("soft_gradient") == "soft_gradient"
    assert templates.normalize_background_fill_mode("radial_blur") == "radial_blur"
    assert templates.normalize_background_fill_mode("zoom_blur") == "zoom_blur"
    assert templates.normalize_background_fill_mode("hybrid") == "hybrid"


def test_build_background_fill_layer_average_uses_source_color() -> None:
    source = Image.new("RGBA", (64, 64), (200, 40, 20, 255))
    fill = templates.build_background_fill_layer(source, 32, fill_mode="average")
    assert fill.size == (32, 32)
    assert fill.mode == "RGBA"
    red, green, blue, alpha = fill.getpixel((0, 0))
    assert alpha == 255
    assert red >= 180
    assert green <= 80
    assert blue <= 80


def test_build_background_fill_layer_blur_mode_matches_output_size() -> None:
    source = Image.new("RGBA", (180, 90), (30, 120, 220, 255))
    fill = templates.build_background_fill_layer(source, 48, fill_mode="blur")
    assert fill.size == (48, 48)
    assert fill.mode == "RGBA"


def test_build_background_fill_layer_edge_and_mirror_modes() -> None:
    source = Image.new("RGBA", (120, 60), (40, 40, 40, 255))
    for x in range(0, 120, 8):
        color = (220, 80, 40, 255) if (x // 8) % 2 == 0 else (40, 180, 220, 255)
        for y in range(60):
            source.putpixel((x, y), color)
    edge_fill = templates.build_background_fill_layer(source, 64, fill_mode="edge_stretch")
    mirror_fill = templates.build_background_fill_layer(source, 64, fill_mode="mirror")
    assert edge_fill.size == (64, 64)
    assert mirror_fill.size == (64, 64)
    assert edge_fill.mode == "RGBA"
    assert mirror_fill.mode == "RGBA"


def test_build_background_fill_layer_soft_gradient_mode() -> None:
    source = Image.new("RGBA", (140, 70), (180, 90, 30, 255))
    fill = templates.build_background_fill_layer(source, 72, fill_mode="soft_gradient")
    assert fill.size == (72, 72)
    assert fill.mode == "RGBA"


def test_build_background_fill_layer_radial_zoom_hybrid_modes() -> None:
    source = Image.new("RGBA", (160, 90), (70, 130, 220, 255))
    for x in range(0, 160, 10):
        for y in range(0, 90, 10):
            if ((x // 10) + (y // 10)) % 2 == 0:
                source.paste((210, 80, 80, 255), (x, y, x + 10, y + 10))
    for mode in ("radial_blur", "zoom_blur", "hybrid"):
        fill = templates.build_background_fill_layer(source, 80, fill_mode=mode)
        assert fill.size == (80, 80)
        assert fill.mode == "RGBA"
