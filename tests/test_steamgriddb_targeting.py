from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services import steamgriddb_targeting as targeting


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
        "discover_steam_appids",
        lambda folder_path, cleaned_name, full_name: ["620"],
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
    monkeypatch.setattr(
        targeting,
        "search_games",
        lambda settings, term, limit=10: [],
    )

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


def test_name_similarity_tracks_close_names() -> None:
    close = targeting.name_similarity("The Witcher 3", "Witcher 3")
    far = targeting.name_similarity("The Witcher 3", "Doom Eternal")
    assert close > 0.65
    assert far < 0.45


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
