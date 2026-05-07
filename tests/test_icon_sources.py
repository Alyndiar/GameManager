from gamemanager.models import IconCandidate
from gamemanager.services import icon_sources
from gamemanager.services.icon_sources import IconSearchSettings


def _candidate(provider: str, image_url: str, width: int, height: int) -> IconCandidate:
    return IconCandidate(
        provider=provider,
        candidate_id=f"{provider}:{width}x{height}",
        title=f"{provider} icon",
        preview_url=image_url,
        image_url=image_url,
        width=width,
        height=height,
        has_alpha=True,
        source_url=image_url,
    )


def test_search_icon_candidates_prefers_steam_then_igdb(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        igdb_enabled=True,
        igdb_client_id="id",
        igdb_client_secret="secret",
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_steamgriddb",
        lambda *args, **kwargs: [_candidate("SteamGridDB", "https://a", 512, 512)],
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_igdb",
        lambda *_: [_candidate("IGDB", "https://b", 256, 256)],
    )
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert out[0].provider == "SteamGridDB"
    assert {x.provider for x in out} == {"SteamGridDB", "IGDB"}


def test_search_icon_candidates_keeps_steamgriddb_results_first(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        igdb_enabled=True,
        igdb_client_id="id",
        igdb_client_secret="secret",
    )
    sgdb = IconCandidate(
        provider="SteamGridDB",
        candidate_id="logos:1",
        title="Game",
        preview_url="https://sgdb.example/logo_t.png",
        image_url="https://sgdb.example/logo.png",
        width=256,
        height=256,
        has_alpha=False,
        source_url="https://sgdb.example/logo.png",
    )
    igdb = IconCandidate(
        provider="IGDB",
        candidate_id="igdb:artwork:1:xyz",
        title="Game icon",
        preview_url="https://igdb.example/art_t.jpg",
        image_url="https://igdb.example/art.jpg",
        width=1024,
        height=1024,
        has_alpha=True,
        source_url="https://igdb.example/art.jpg",
    )
    monkeypatch.setattr(icon_sources, "_search_steamgriddb", lambda *args, **kwargs: [sgdb])
    monkeypatch.setattr(icon_sources, "_search_igdb", lambda *args, **kwargs: [igdb])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert out
    assert out[0].provider == "SteamGridDB"


def test_search_icon_candidates_deduplicates_by_image_url(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_steamgriddb",
        lambda *args, **kwargs: [
            _candidate("SteamGridDB", "https://same", 300, 300),
            _candidate("SteamGridDB", "https://same", 512, 512),
        ],
    )
    monkeypatch.setattr(icon_sources, "_search_igdb", lambda *_: [])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert len(out) == 1


def test_search_steamgriddb_fetches_icons_and_logos(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
    )
    urls: list[str] = []

    monkeypatch.setattr(icon_sources, "_session", lambda: object())

    def _fake_get_json(_session, url: str, _headers, _timeout):
        urls.append(url)
        if "/search/autocomplete/" in url:
            return {"data": [{"id": 123, "name": "Ace Attorney Investigations Collection"}]}
        if "/icons/game/123" in url:
            return {
                "data": [
                    {
                        "id": 1,
                        "url": "https://cdn.example/icon.png",
                        "thumb": "https://cdn.example/icon_t.png",
                        "width": 512,
                        "height": 512,
                    }
                ]
            }
        if "/logos/game/123" in url:
            return {
                "data": [
                    {
                        "id": 2,
                        "url": "https://cdn.example/logo.png",
                        "thumb": "https://cdn.example/logo_t.png",
                        "width": 600,
                        "height": 200,
                    }
                ]
            }
        return None

    monkeypatch.setattr(icon_sources, "_safe_get_json", _fake_get_json)
    out = icon_sources._search_steamgriddb(
        settings,
        "Ace Attorney Investigations Collection",
        "Ace Attorney Investigations Collection",
    )
    assert any("/icons/game/123" in url for url in urls)
    assert any("/logos/game/123" in url for url in urls)
    assert any(c.candidate_id.startswith("icons:") for c in out)
    assert any(c.candidate_id.startswith("logos:") for c in out)


def test_search_steamgriddb_respects_requested_resources(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
    )
    urls: list[str] = []

    monkeypatch.setattr(icon_sources, "_session", lambda: object())

    def _fake_get_json(_session, url: str, _headers, _timeout):
        urls.append(url)
        if "/search/autocomplete/" in url:
            return {"data": [{"id": 123, "name": "Game"}]}
        if "/grids/game/123" in url:
            return {
                "data": [
                    {
                        "id": 9,
                        "url": "https://cdn.example/grid.png",
                        "thumb": "https://cdn.example/grid_t.png",
                        "width": 600,
                        "height": 900,
                    }
                ]
            }
        if "/icons/game/123" in url or "/logos/game/123" in url or "/heroes/game/123" in url:
            return {"data": []}
        return None

    monkeypatch.setattr(icon_sources, "_safe_get_json", _fake_get_json)
    out = icon_sources._search_steamgriddb(
        settings,
        "Game",
        "Game",
        sgdb_resources={"grids"},
    )
    assert any("/grids/game/123" in url for url in urls)
    assert not any("/icons/game/123" in url for url in urls)
    assert not any("/logos/game/123" in url for url in urls)
    assert not any("/heroes/game/123" in url for url in urls)
    assert out
    assert all(c.candidate_id.startswith("grids:") for c in out)


def test_search_icon_candidates_scores_icons_above_logos(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
    )
    icon_candidate = IconCandidate(
        provider="SteamGridDB",
        candidate_id="icons:10",
        title="Game",
        preview_url="https://x/icon_t.png",
        image_url="https://x/icon.png",
        width=512,
        height=512,
        has_alpha=True,
        source_url="https://x/icon.png",
    )
    logo_candidate = IconCandidate(
        provider="SteamGridDB",
        candidate_id="logos:20",
        title="Game",
        preview_url="https://x/logo_t.png",
        image_url="https://x/logo.png",
        width=512,
        height=512,
        has_alpha=True,
        source_url="https://x/logo.png",
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_steamgriddb",
        lambda *args, **kwargs: [logo_candidate, icon_candidate],
    )
    monkeypatch.setattr(icon_sources, "_search_igdb", lambda *_: [])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert out
    assert out[0].candidate_id.startswith("icons:")


def test_search_steamgriddb_inferrs_dimensions_when_missing(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
    )
    monkeypatch.setattr(icon_sources, "_session", lambda: object())

    def _fake_get_json(_session, url: str, _headers, _timeout):
        if "/search/autocomplete/" in url:
            return {"data": [{"id": 77, "name": "Game"}]}
        if "/icons/game/77" in url:
            return {
                "data": [
                    {
                        "id": 100,
                        "url": "https://cdn.example/icons/512x512/icon.png",
                        "thumb": {
                            "url": "https://cdn.example/icons/128x128/icon_t.png"
                        },
                        "style": "512x512",
                    }
                ]
            }
        if "/logos/game/77" in url:
            return {"data": []}
        return None

    monkeypatch.setattr(icon_sources, "_safe_get_json", _fake_get_json)
    out = icon_sources._search_steamgriddb(settings, "Game", "Game")
    assert out
    assert out[0].width == 512
    assert out[0].height == 512
    assert out[0].preview_url.endswith("icon_t.png")


def test_search_igdb_collects_cover_and_artwork_candidates(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        igdb_enabled=True,
        igdb_client_id="client",
        igdb_client_secret="secret",
    )
    monkeypatch.setattr(icon_sources, "_session", lambda: object())
    monkeypatch.setattr(icon_sources, "_igdb_access_token", lambda *_args, **_kwargs: "token")

    def _fake_post(
        _session,
        url,
        *,
        headers,
        data=None,
        body="",
        timeout=15.0,
        enforce_igdb_rate_limit=False,
    ):
        _ = (headers, data, timeout, enforce_igdb_rate_limit)
        if url.endswith("/games"):
            return [
                {
                    "id": 620,
                    "name": "Portal 2",
                    "url": "https://www.igdb.com/games/portal-2",
                    "cover": {"image_id": "co1abc"},
                    "artworks": [{"image_id": "ar1def"}],
                }
            ]
        return None

    monkeypatch.setattr(icon_sources, "_safe_post_json", _fake_post)
    out = icon_sources._search_igdb(settings, "Portal 2", "Portal 2")
    assert len(out) == 2
    assert out[0].provider == "IGDB"
    assert out[0].candidate_id.startswith("igdb:cover:")
    assert out[1].candidate_id.startswith("igdb:artwork:")


def test_lookup_igdb_store_ids_for_title_collects_external_and_website_ids(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        igdb_enabled=True,
        igdb_client_id="client",
        igdb_client_secret="secret",
    )
    monkeypatch.setattr(icon_sources, "_session", lambda: object())
    monkeypatch.setattr(icon_sources, "_igdb_access_token", lambda *_args, **_kwargs: "token")
    calls: list[str] = []

    def _fake_post(
        _session,
        url,
        *,
        headers,
        data=None,
        body="",
        timeout=15.0,
        enforce_igdb_rate_limit=False,
    ):
        _ = (headers, data, timeout, enforce_igdb_rate_limit)
        calls.append(url)
        if url.endswith("/games"):
            return [
                {"id": 101, "name": "Portal 2"},
                {"id": 102, "name": "Portal 2 Soundtrack"},
            ]
        if url.endswith("/external_games"):
            return [
                {"external_game_source": 1, "uid": "620", "url": "https://store.steampowered.com/app/620/"},
                {"external_game_source": 5, "uid": "portal_2", "url": "https://www.gog.com/en/game/portal_2"},
                {"external_game_source": 26, "uid": "epic-portal-2", "url": "https://store.epicgames.com/p/portal-2"},
            ]
        if url.endswith("/websites"):
            return [
                {"type": 15, "url": "https://foo.itch.io/portal-2"},
            ]
        return None

    monkeypatch.setattr(icon_sources, "_safe_post_json", _fake_post)
    out = icon_sources.lookup_igdb_store_ids_for_title(settings, "Portal 2")
    assert out["Steam"] == "620"
    assert out["GOG"] == "portal_2"
    assert out["EGS"] == "epic-portal-2"
    assert out["Itch.io"] == "https://foo.itch.io/portal-2"
    assert calls[0].endswith("/games")


def test_lookup_igdb_store_ids_for_title_requires_exact_name_match(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        igdb_enabled=True,
        igdb_client_id="client",
        igdb_client_secret="secret",
    )
    monkeypatch.setattr(icon_sources, "_session", lambda: object())
    monkeypatch.setattr(icon_sources, "_igdb_access_token", lambda *_args, **_kwargs: "token")

    def _fake_post(
        _session,
        url,
        *,
        headers,
        data=None,
        body="",
        timeout=15.0,
        enforce_igdb_rate_limit=False,
    ):
        _ = (headers, data, body, timeout, enforce_igdb_rate_limit)
        if url.endswith("/games"):
            return [{"id": 101, "name": "Portal 2 Soundtrack"}]
        return []

    monkeypatch.setattr(icon_sources, "_safe_post_json", _fake_post)
    out = icon_sources.lookup_igdb_store_ids_for_title(settings, "Portal 2")
    assert out == {}


def test_safe_post_json_enforces_igdb_rate_limit(monkeypatch) -> None:
    class _FakeResp:
        status_code = 200

        def json(self):
            return {}

    class _FakeSession:
        def post(self, *args, **kwargs):
            _ = (args, kwargs)
            return _FakeResp()

    state = {"t": 0.0, "sleep_calls": 0}

    def _mono():
        return float(state["t"])

    def _sleep(seconds: float):
        state["sleep_calls"] += 1
        state["t"] = float(state["t"]) + max(0.0, float(seconds))

    icon_sources._IGDB_RATE_TIMESTAMPS.clear()
    monkeypatch.setattr(icon_sources.time, "monotonic", _mono)
    monkeypatch.setattr(icon_sources.time, "sleep", _sleep)

    session = _FakeSession()
    for _ in range(5):
        out = icon_sources._safe_post_json(
            session,
            "https://api.igdb.com/v4/games",
            headers={},
            body="fields id;",
            timeout=5.0,
            enforce_igdb_rate_limit=True,
        )
        assert out == {}
    assert state["sleep_calls"] >= 1
