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

