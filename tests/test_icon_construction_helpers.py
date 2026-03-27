from __future__ import annotations

from gamemanager.ui.dialogs.icon_construction_common import (
    normalize_upscale_method,
    shader_tone_label,
)
from gamemanager.ui.dialogs.icon_construction_cutout_state import (
    any_curve_mode_entries,
    cutout_mode_uses_curve_strength,
    default_curve_strength_for_mode,
    load_cutout_falloff_settings,
    load_cutout_picked_colors_state,
    normalize_cutout_falloff,
    normalize_cutout_scope,
    serialize_cutout_picked_rows,
    upsert_cutout_mark_point,
)


def test_upscale_method_normalization() -> None:
    assert normalize_upscale_method("lanczos") == "lanczos"
    assert normalize_upscale_method("LANCZOS_UNSHARP") == "lanczos_unsharp"
    assert normalize_upscale_method("invalid") == "qt_smooth"
    assert normalize_upscale_method(None) == "qt_smooth"
    assert shader_tone_label("hsl") == "Lightness"
    assert shader_tone_label("hsv") == "Value"


def test_cutout_scope_and_falloff_normalization() -> None:
    assert normalize_cutout_scope("contig") == "contig"
    assert normalize_cutout_scope("bad") == "global"
    assert normalize_cutout_falloff("exp") == "exp"
    assert normalize_cutout_falloff("BAD") == "flat"
    assert cutout_mode_uses_curve_strength("exp") is True
    assert cutout_mode_uses_curve_strength("flat") is False
    assert default_curve_strength_for_mode("exp") == 35
    assert default_curve_strength_for_mode("log") == 65
    assert default_curve_strength_for_mode("gauss") == 45
    assert default_curve_strength_for_mode("lin") == 50


def test_load_cutout_picked_colors_state_normalizes_rows() -> None:
    rows, history, next_row = load_cutout_picked_colors_state(
        {
            "picked_colors": [
                {
                    "color": [300, -5, 10],
                    "tolerance": 100,
                    "scope": "contig",
                    "falloff": "EXP",
                    "include_seeds": [[1.5, -0.2], [0.2, 0.3], [0.2, 0.3]],
                    "exclude_seeds": [[0.1, 0.1], ["x", "y"]],
                },
                {
                    "color": [1, 2, 3],
                    "tolerance": "bad",
                },
            ]
        },
        7,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == 7
    assert rows[0]["color"] == [255, 0, 10]
    assert rows[0]["tolerance"] == 30
    assert rows[0]["scope"] == "contig"
    assert rows[0]["falloff"] == "exp"
    assert rows[0]["include_seeds"] == [[1.0, 0.0], [0.2, 0.3]]
    assert rows[0]["exclude_seeds"] == [[0.1, 0.1]]
    assert history[7] == {"undo": [], "redo": []}
    assert next_row == 8


def test_load_cutout_falloff_settings_and_curve_mode_scan() -> None:
    advanced, strength = load_cutout_falloff_settings(
        {"pick_colors_advanced": True, "pick_colors_curve_strength": 140}
    )
    assert advanced is True
    assert strength == 100
    assert any_curve_mode_entries([{"falloff": "flat"}]) is False
    assert any_curve_mode_entries([{"falloff": "gauss"}]) is True


def test_serialize_cutout_rows_and_mark_point_upsert() -> None:
    serialized = serialize_cutout_picked_rows(
        [
            {
                "color": (1, 2, 3),
                "tolerance": 9,
                "scope": "bad",
                "falloff": "BAD",
                "include_seeds": [[0.1, 0.2]],
                "exclude_seeds": [[0.2, 0.1]],
            }
        ]
    )
    assert serialized == [
        {
            "color": [1, 2, 3],
            "tolerance": 9,
            "scope": "global",
            "falloff": "flat",
            "include_seeds": [[0.1, 0.2]],
            "exclude_seeds": [[0.2, 0.1]],
        }
    ]

    points: list[list[float]] = []
    upsert_cutout_mark_point(points, (0.5, 0.5))
    upsert_cutout_mark_point(points, (0.5005, 0.5005))
    assert points == [[0.5005, 0.5005]]

    points = []
    for idx in range(514):
        upsert_cutout_mark_point(points, (float(idx) / 100.0, 0.0))
    assert len(points) == 512
