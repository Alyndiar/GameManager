from gamemanager.services import steamgriddb_targeting as targeting
from gamemanager.services.icon_sources import IconSearchSettings


def _settings() -> IconSearchSettings:
    return IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="k",
    )


def test_build_name_variants_contains_expected_tokens() -> None:
    variants = targeting.build_name_variants(
        "Portal 2 GOTY: Ultimate Edition",
        "Portal 2 GOTY",
        "Portal_2_GOTY",
    )
    assert "Portal 2 GOTY: Ultimate Edition" in variants
    assert "Portal 2 GOTY" in variants
    assert "Portal 2" in variants


def test_resolve_target_candidates_uses_exact_steam_appid(monkeypatch) -> None:
    monkeypatch.setattr(
        targeting,
        "discover_store_identity_hints",
        lambda folder_path, cleaned_name, full_name, **kwargs: (
            {"Steam": ["620"]},
            {"Steam": ["Portal 2"]},
        ),
    )
    monkeypatch.setattr(
        targeting,
        "resolve_game_by_platform_id",
        lambda settings, platform, platform_id: targeting.SgdbGameDetails(
            game_id=999,
            title="Portal 2",
            steam_appid="620",
        ),
    )
    monkeypatch.setattr(targeting, "search_games", lambda settings, term, limit=10: [])

    candidates, _variants, exact = targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/Portal 2",
        cleaned_name="Portal 2",
        full_name="Portal 2",
    )
    assert candidates
    assert candidates[0].game_id == 999
    assert candidates[0].confidence == 1.0
    assert exact == 999


def test_resolve_target_candidates_skips_name_search_when_store_mapping_found(monkeypatch) -> None:
    monkeypatch.setattr(
        targeting,
        "discover_store_identity_hints",
        lambda folder_path, cleaned_name, full_name, **kwargs: (
            {"Steam": ["12345"]},
            {"Steam": ["Exact Steam Game"]},
        ),
    )
    monkeypatch.setattr(
        targeting,
        "resolve_game_by_platform_id",
        lambda settings, platform, platform_id: targeting.SgdbGameDetails(
            game_id=777,
            title="Exact Steam Game",
            steam_appid="12345",
        ),
    )

    def _fail_search(*args, **kwargs):
        raise AssertionError("search_games should not run when store-id mapping succeeds")

    monkeypatch.setattr(targeting, "search_games", _fail_search)

    candidates, _variants, exact = targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/Exact Steam Game",
        cleaned_name="Exact Steam Game",
        full_name="Exact Steam Game",
    )
    assert candidates
    assert candidates[0].game_id == 777
    assert candidates[0].steam_appid == "12345"
    assert candidates[0].identity_store == "Steam"
    assert candidates[0].identity_store_id == "12345"
    assert candidates[0].store_ids.get("Steam") == "12345"
    assert exact == 777


def test_resolve_target_candidates_uses_store_names_when_mapping_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        targeting,
        "discover_store_identity_hints",
        lambda folder_path, cleaned_name, full_name, **kwargs: (
            {"Steam": ["620"]},
            {"Steam": ["Portal"]},
        ),
    )

    def _missing_platform(*args, **kwargs):
        raise RuntimeError("no platform mapping")

    monkeypatch.setattr(targeting, "resolve_game_by_platform_id", _missing_platform)
    seen_terms: list[str] = []

    def _search(settings, term, limit=10):
        seen_terms.append(term)
        return [targeting.SgdbGameDetails(game_id=999, title="Portal", steam_appid="620")]

    monkeypatch.setattr(targeting, "search_games", _search)

    candidates, _variants, exact = targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/Portal",
        cleaned_name="Portal",
        full_name="Portal",
    )
    assert seen_terms == ["Portal"]
    assert exact is None
    assert candidates
    assert candidates[0].game_id == 999
    assert candidates[0].steam_appid == "620"


def test_priority_short_circuit_stops_lower_stores_when_higher_store_has_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        targeting,
        "discover_store_identity_hints",
        lambda folder_path, cleaned_name, full_name, **kwargs: (
            {"Steam": ["620"], "EGS": ["epic-1"]},
            {"Steam": ["Portal"]},
        ),
    )
    seen_platforms: list[tuple[str, str]] = []

    def _resolve(settings, platform, platform_id):
        seen_platforms.append((platform, platform_id))
        raise RuntimeError("no mapping")

    monkeypatch.setattr(targeting, "resolve_game_by_platform_id", _resolve)
    monkeypatch.setattr(targeting, "search_games", lambda *_args, **_kwargs: [])

    targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/Portal",
        cleaned_name="Portal",
        full_name="Portal",
    )
    assert seen_platforms == [("steam", "620")]


def test_priority_uses_next_store_when_higher_store_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        targeting,
        "discover_store_identity_hints",
        lambda folder_path, cleaned_name, full_name, **kwargs: (
            {"EGS": ["epic-1"]},
            {},
        ),
    )

    def _resolve(settings, platform, platform_id):
        if platform in {"epic", "egs"} and platform_id == "epic-1":
            return targeting.SgdbGameDetails(game_id=321, title="EGS Game", steam_appid=None)
        raise RuntimeError("not found")

    monkeypatch.setattr(targeting, "resolve_game_by_platform_id", _resolve)
    monkeypatch.setattr(targeting, "search_games", lambda *_args, **_kwargs: [])

    candidates, _variants, exact = targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/EGS Game",
        cleaned_name="EGS Game",
        full_name="EGS Game",
    )
    assert exact is None
    assert candidates
    assert candidates[0].game_id == 321
    assert candidates[0].identity_store == "EGS"
    assert candidates[0].identity_store_id == "epic-1"
    assert candidates[0].store_ids.get("EGS") == "epic-1"


def test_name_similarity_tracks_close_names() -> None:
    close = targeting.name_similarity("The Witcher 3", "Witcher 3")
    far = targeting.name_similarity("The Witcher 3", "Doom Eternal")
    assert close > 0.65
    assert far < 0.45


def test_name_similarity_exact_after_standard_normalization() -> None:
    exact = targeting.name_similarity("Aaero-2: Deluxe!", "aaero 2 deluxe")
    assert exact == 1.0


def test_confidence_from_name_match_exact_is_one() -> None:
    raw, confidence = targeting._confidence_from_name_match("Prince-of Persia", "prince of persia", 1)
    assert raw == 1.0
    assert confidence == 1.0


def test_search_steam_store_appids_filters_low_similarity_results(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    payload = {
        "items": [
            {"id": 435100, "name": "2Dark"},
            {"id": 579740, "name": "2Dark Official Soundtrack and Artbook"},
            {"id": 40390, "name": "Risen 2: Dark Waters"},
        ]
    }
    monkeypatch.setattr(
        targeting.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(payload),
    )

    appids = targeting._search_steam_store_appids("2Dark")
    assert appids == ["435100"]


def test_search_steam_store_appids_prefers_exact_normalized_matches(monkeypatch) -> None:
    class _FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    payload = {
        "items": [
            {"id": 3010090, "name": "Aaero 2"},
            {"id": 3010091, "name": "Aaero 2 Soundtrack"},
        ]
    }
    monkeypatch.setattr(
        targeting.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(payload),
    )
    appids = targeting._search_steam_store_appids("Aaero-2")
    assert appids == ["3010090"]


def test_discover_steam_appids_can_ignore_assigned_marker(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "Portal"
    folder.mkdir(parents=True)
    (folder / "steam_appid.txt").write_text("620\n", encoding="utf-8")
    monkeypatch.setattr(targeting, "_search_steam_store_appids", lambda _query: [])

    appids_with_hints = targeting.discover_steam_appids(
        str(folder),
        "Portal",
        "Portal",
        include_assigned_hints=True,
    )
    appids_without_hints = targeting.discover_steam_appids(
        str(folder),
        "Portal",
        "Portal",
        include_assigned_hints=False,
    )
    assert "620" in appids_with_hints
    assert "620" not in appids_without_hints


def test_resolve_target_candidates_passes_include_assigned_hints(monkeypatch) -> None:
    observed = {"include": None}

    def _discover(folder_path, cleaned_name, full_name, *, include_assigned_hints=True):
        _ = (folder_path, cleaned_name, full_name)
        observed["include"] = include_assigned_hints
        return {}, {}

    monkeypatch.setattr(targeting, "discover_store_identity_hints", _discover)
    monkeypatch.setattr(targeting, "search_games", lambda *args, **kwargs: [])

    targeting.resolve_target_candidates(
        _settings(),
        folder_path="C:/Games/Portal",
        cleaned_name="Portal",
        full_name="Portal",
        include_assigned_hints=False,
    )
    assert observed["include"] is False
