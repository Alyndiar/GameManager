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

