from dataclasses import dataclass

from gamemanager.ui.main_window import (
    _filter_only_duplicate_cleaned_names,
    _format_size_and_free,
    _source_display_text,
)


def test_source_display_mode_variants() -> None:
    assert _source_display_text("source", "D:", "Dragon") == "D:"
    assert _source_display_text("name", "D:", "Dragon") == "Dragon"
    assert _source_display_text("both", "D:", "Dragon") == "D: | Dragon"


def test_source_display_both_collapses_duplicate_labels() -> None:
    assert _source_display_text("both", "NekoNeko151", "NekoNeko151") == "NekoNeko151"
    assert _source_display_text("both", "nekonekO151", "NekoNeko151") == "nekonekO151"


def test_size_free_format_uses_required_spacing_and_rounding() -> None:
    total = int((1979.6) * (1024**3))
    free = int((27.04) * (1024**3))
    assert _format_size_and_free(total, free) == "Size : 1980 GB    Free : 27.04 GB"


@dataclass
class _Item:
    cleaned_name: str


def test_duplicate_filter_keeps_only_repeated_cleaned_names() -> None:
    items = [_Item("Alpha"), _Item("Beta"), _Item("alpha"), _Item("Gamma")]
    filtered = _filter_only_duplicate_cleaned_names(items)
    assert [x.cleaned_name for x in filtered] == ["Alpha", "alpha"]
