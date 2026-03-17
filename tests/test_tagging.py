from gamemanager.services.normalization import remove_approved_suffix_tags
from gamemanager.services.tagging import collect_tag_candidates


def test_suffix_tag_detection_variants() -> None:
    names = [
        ("Game-GOG.iso", True),
        ("Another [GOG].zip", True),
        ("Title (Steam)", False),
        ("One More {itch}.rar", True),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    got = {c.canonical_tag for c in candidates}
    assert "gog" in got
    assert "steam" in got
    assert "itch" in got


def test_non_tag_suppression() -> None:
    names = [("Test [GOG].iso", True), ("Other-GOG", False)]
    candidates = collect_tag_candidates(names, non_tags={"gog"})
    assert candidates == []


def test_remove_approved_tag_wrapper_equivalence() -> None:
    approved = {"gog"}
    assert remove_approved_suffix_tags("Game [GOG]", approved) == "Game"
    assert remove_approved_suffix_tags("Game (gog)", approved) == "Game"
    assert remove_approved_suffix_tags("Game-GOG", approved) == "Game"
    assert remove_approved_suffix_tags("Game_{GOG}", approved) == "Game"


def test_dash_tag_rules_space_after_dash_is_skipped_and_last_dash_used() -> None:
    names = [
        ("Call-of-Duty-GOG.iso", True),
        ("Game- GOG.iso", True),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    got = {c.canonical_tag for c in candidates}
    assert "gog" in got
    # "- GOG" must be ignored as tag syntax.
    assert len(candidates) == 1

    approved = {"gog"}
    assert remove_approved_suffix_tags("Call-of-Duty-GOG", approved) == "Call-of-Duty"
    assert remove_approved_suffix_tags("Game- GOG", approved) == "Game- GOG"


def test_version_like_delimited_suffix_is_not_proposed_as_tag() -> None:
    names = [
        ("Game-v1.2.zip", True),
        ("Game-1.2.3.iso", True),
        ("Game-build12.rar", True),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    assert candidates == []


def test_numeric_only_suffixes_are_not_tags() -> None:
    names = [
        ("Game [0].iso", True),
        ("Other-3.zip", True),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    assert candidates == []


def test_numeric_series_prefix_blocks_delimited_suffix_tag() -> None:
    names = [
        ("1-3", False),
        ("1-2-3-4 Full series", False),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    assert candidates == []


def test_remove_approved_does_not_strip_numeric_or_numeric_series_suffixes() -> None:
    approved = {"0", "3", "4 full series"}
    assert remove_approved_suffix_tags("Game [0]", approved) == "Game [0]"
    assert remove_approved_suffix_tags("1-3", approved) == "1-3"
    assert (
        remove_approved_suffix_tags("1-2-3-4 Full series", approved)
        == "1-2-3-4 Full series"
    )


def test_mgq_number_series_suffix_with_text_is_not_tag() -> None:
    names = [("MGQ 1-3 English", False)]
    candidates = collect_tag_candidates(names, non_tags=set())
    assert candidates == []


def test_square_or_curly_suffix_tag_stops_earlier_tag_extraction() -> None:
    names = [
        ("Game-GOG [Steam]", False),
        ("Other-AAA {GOG}", False),
        ("Title-BBB (Steam)", False),
    ]
    candidates = collect_tag_candidates(names, non_tags=set())
    got = {c.canonical_tag for c in candidates}
    assert "steam" in got
    assert "gog" in got
    assert "aaa" not in got
    assert "bbb" not in got


def test_square_or_curly_suffix_tag_stops_earlier_tag_removal() -> None:
    approved = {"steam", "gog", "aaa"}
    assert remove_approved_suffix_tags("Game-GOG [Steam]", approved) == "Game-GOG"
    assert remove_approved_suffix_tags("Other-AAA {GOG}", approved) == "Other-AAA"
    assert remove_approved_suffix_tags("Title-BBB (Steam)", approved) == "Title-BBB"
