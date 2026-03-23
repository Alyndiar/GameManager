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


def test_search_icon_candidates_prefers_steam_then_iconfinder(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        iconfinder_enabled=True,
        iconfinder_api_key="y",
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_steamgriddb",
        lambda *args, **kwargs: [_candidate("SteamGridDB", "https://a", 512, 512)],
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_iconfinder",
        lambda *_: [_candidate("Iconfinder", "https://b", 256, 256)],
    )
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert out[0].provider == "SteamGridDB"
    assert {x.provider for x in out} == {"SteamGridDB", "Iconfinder"}


def test_search_icon_candidates_deduplicates_by_image_url(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        iconfinder_enabled=False,
        iconfinder_api_key="",
    )
    monkeypatch.setattr(
        icon_sources,
        "_search_steamgriddb",
        lambda *args, **kwargs: [
            _candidate("SteamGridDB", "https://same", 300, 300),
            _candidate("SteamGridDB", "https://same", 512, 512),
        ],
    )
    monkeypatch.setattr(icon_sources, "_search_iconfinder", lambda *_: [])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert len(out) == 1


def test_search_steamgriddb_fetches_icons_and_logos(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        iconfinder_enabled=False,
        iconfinder_api_key="",
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
        settings, "Ace Attorney Investigations Collection", "Ace Attorney Investigations Collection"
    )
    assert any("/icons/game/123" in url for url in urls)
    assert any("/logos/game/123" in url for url in urls)
    assert any(c.candidate_id.startswith("icons:") for c in out)
    assert any(c.candidate_id.startswith("logos:") for c in out)


def test_search_steamgriddb_respects_requested_resources(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        iconfinder_enabled=False,
        iconfinder_api_key="",
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
        iconfinder_enabled=False,
        iconfinder_api_key="",
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
    monkeypatch.setattr(icon_sources, "_search_iconfinder", lambda *_: [])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert out
    assert out[0].candidate_id.startswith("icons:")


def test_search_steamgriddb_inferrs_dimensions_when_missing(monkeypatch) -> None:
    settings = IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="x",
        iconfinder_enabled=False,
        iconfinder_api_key="",
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
