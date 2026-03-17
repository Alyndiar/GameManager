from datetime import datetime, timedelta

from gamemanager.services.sorting import sort_key_for_inventory


def test_sort_priority_cleaned_then_full_then_modified_desc() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    rows = [
        ("alpha", "alpha 2", now - timedelta(days=1)),
        ("alpha", "alpha 10", now),
        ("beta", "beta", now),
    ]
    ordered = sorted(rows, key=lambda r: sort_key_for_inventory(r[0], r[1], r[2]))
    assert ordered[0][1] == "alpha 2"
    assert ordered[1][1] == "alpha 10"
    assert ordered[2][0] == "beta"


def test_sort_handles_numeric_vs_text_prefix_without_type_error() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    rows = [
        ("10 game", "10 game", now),
        ("alpha game", "alpha game", now),
        ("2 game", "2 game", now),
    ]
    ordered = sorted(rows, key=lambda r: sort_key_for_inventory(r[0], r[1], r[2]))
    assert [row[0] for row in ordered] == ["2 game", "10 game", "alpha game"]


def test_sort_key_handles_pre_epoch_dates_without_timestamp_call() -> None:
    old = datetime(1601, 1, 1, 0, 0, 0)
    new = datetime(2026, 1, 1, 0, 0, 0)
    rows = [
        ("game", "game", old),
        ("game", "game", new),
    ]
    ordered = sorted(rows, key=lambda r: sort_key_for_inventory(r[0], r[1], r[2]))
    assert ordered[0][2] == new
    assert ordered[1][2] == old
