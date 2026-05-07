from __future__ import annotations

from gamemanager.services.storefronts.gog_connector import GogConnector


def test_gog_connect_accepts_account_basic_json(monkeypatch) -> None:
    connector = GogConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.gog_connector._fetch_account_basic",
        lambda _token: {
            "userId": "12345",
            "username": "alice",
            "accessTokenExpires": 3600,
        },
    )
    result = connector.connect(
        {
            "account_basic_json": (
                '{"userId":"12345","username":"alice",'
                '"accessToken":"token-abc","accessTokenExpires":3600}'
            )
        }
    )
    assert result.success is True
    assert result.account_id == "12345"
    assert result.display_name == "alice"
    assert result.token_secret == "token-abc"
    assert result.auth_kind == "browser_session_token"


def test_gog_connect_accepts_public_account_name() -> None:
    connector = GogConnector()
    result = connector.connect({"username": "public_profile_name"})
    assert result.success is True
    assert result.account_id == "public_profile_name"
    assert result.display_name == "public_profile_name"
    assert result.token_secret == ""
    assert result.auth_kind == "public_account_name"


def test_gog_refresh_uses_stats_endpoint_and_extracts_slug(monkeypatch) -> None:
    connector = GogConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.gog_connector._fetch_account_basic",
        lambda _token: {"username": "alice"},
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.gog_connector._owned_games_from_stats",
        lambda **_kwargs: [
            {
                "game": {
                    "id": "1207658901",
                    "title": "Portal 2",
                    "url": "https://www.gog.com/game/portal_2",
                    "image": "https://images.gog.com/portal2.png",
                },
                "stats": {},
            }
        ],
    )
    rows = connector.refresh_entitlements("12345", token_secret="token-abc")
    assert len(rows) == 1
    row = rows[0]
    assert row.entitlement_id == "1207658901"
    assert row.title == "Portal 2"
    assert row.store_game_id == "portal_2"


def test_gog_refresh_falls_back_to_legacy_when_stats_empty(monkeypatch) -> None:
    connector = GogConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.gog_connector._owned_games_from_stats",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.gog_connector._owned_games_from_legacy",
        lambda **_kwargs: [
            {
                "game": {
                    "id": "1207658902",
                    "title": "No Man's Sky",
                    "url": "/game/no_mans_sky",
                    "image": "https://images.gog.com/nms.png",
                },
                "stats": {},
            }
        ],
    )
    rows = connector.refresh_entitlements("public_profile_name", token_secret="")
    assert len(rows) == 1
    row = rows[0]
    assert row.entitlement_id == "1207658902"
    assert row.store_game_id == "no_mans_sky"
