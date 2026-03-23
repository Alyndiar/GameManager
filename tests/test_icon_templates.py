from pathlib import Path

from PIL import Image

from gamemanager.services import icon_pipeline


def _write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    image.save(path, format="PNG")


def test_icon_style_options_include_custom_templates(monkeypatch, tmp_path: Path) -> None:
    template_dir = tmp_path / "IconTemplates"
    _write_template(template_dir / "ThinGoldRound.png")
    _write_template(template_dir / "SteelSquareThin.png")

    monkeypatch.setattr(icon_pipeline, "CUSTOM_TEMPLATE_DIR", template_dir)
    options = icon_pipeline.icon_style_options()
    labels = {label for label, _ in options}
    ids = {value for _, value in options}
    assert "ThinGoldRound" in labels
    assert "SteelSquareThin" in labels
    assert "thingoldround" in ids
    assert "steelsquarethin" in ids


def test_resolve_custom_template_shape(monkeypatch, tmp_path: Path) -> None:
    template_dir = tmp_path / "IconTemplates"
    _write_template(template_dir / "CopperRound.png")
    _write_template(template_dir / "GraphiteSquare.png")
    monkeypatch.setattr(icon_pipeline, "CUSTOM_TEMPLATE_DIR", template_dir)

    round_template = icon_pipeline.resolve_icon_template("copperround")
    square_template = icon_pipeline.resolve_icon_template("graphitesquare")
    assert round_template.shape == "round"
    assert square_template.shape == "square"


def test_template_analysis_metadata_written(monkeypatch, tmp_path: Path) -> None:
    template_dir = tmp_path / "IconTemplates"
    template_path = template_dir / "SilverRound.png"
    template_dir.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas = Image.alpha_composite(img, draw)
    px = canvas.load()
    center = 256
    outer = 230
    inner = 170
    for y in range(512):
        for x in range(512):
            dx = x - center
            dy = y - center
            dist2 = dx * dx + dy * dy
            if inner * inner <= dist2 <= outer * outer:
                px[x, y] = (200, 200, 200, 255)
    canvas.save(template_path, format="PNG")

    monkeypatch.setattr(icon_pipeline, "CUSTOM_TEMPLATE_DIR", template_dir)
    payload = icon_pipeline.build_template_overlay_preview("silverround", size=128)
    assert payload.startswith(b"\x89PNG")
    analysis_path = template_path.with_suffix(".analysis.json")
    assert analysis_path.exists()
