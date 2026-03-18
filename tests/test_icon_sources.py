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
        lambda *_: [_candidate("SteamGridDB", "https://a", 512, 512)],
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
        lambda *_: [
            _candidate("SteamGridDB", "https://same", 300, 300),
            _candidate("SteamGridDB", "https://same", 512, 512),
        ],
    )
    monkeypatch.setattr(icon_sources, "_search_iconfinder", lambda *_: [])
    out = icon_sources.search_icon_candidates("Game", "Game", settings)
    assert len(out) == 1
