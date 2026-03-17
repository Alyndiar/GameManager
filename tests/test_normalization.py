from gamemanager.services.normalization import (
    cleaned_name_from_full,
    normalize_separators,
    strip_trailing_versions,
)


def test_separator_cleanup_preserves_numeric_and_v_prefix_dots() -> None:
    assert normalize_separators("Game_Name.v2.3.56") == "Game Name v2.3.56"
    assert normalize_separators("Game.v.2.3") == "Game v.2.3"
    assert normalize_separators("Demo_v.3.5.07") == "Demo v.3.5.07"
    assert normalize_separators("My.Game_Name") == "My Game Name"


def test_strip_trailing_versions_trailing_only() -> None:
    assert strip_trailing_versions("Game v1.2") == "Game"
    assert strip_trailing_versions("Game 1.0.4") == "Game"
    assert strip_trailing_versions("Game build12") == "Game"
    assert strip_trailing_versions("v2.3 Game") == "v2.3 Game"


def test_cleaned_name_removes_approved_tags_and_versions_and_extension() -> None:
    approved = {"gog", "steam"}
    value = cleaned_name_from_full("Great_Game-v1.2-[GOG].zip", is_file=True, approved_tags=approved)
    assert value == "Great Game"


def test_cleaned_name_strips_trailing_version_suffix_delimited_by_dash() -> None:
    assert cleaned_name_from_full("Game-v1.2.zip", is_file=True, approved_tags=set()) == "Game"
    assert cleaned_name_from_full("Game-1.2.3.iso", is_file=True, approved_tags=set()) == "Game"
