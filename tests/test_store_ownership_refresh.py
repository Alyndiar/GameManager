from pathlib import Path

from gamemanager.app_state import AppState
from gamemanager.models import StoreOwnedGame


def test_refresh_populates_owned_stores_from_store_links(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))

    app.db.upsert_store_link(
        inventory_path=str(game_dir),
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        match_method="strong_id",
        confidence=1.0,
        verified=True,
    )
    app.db.upsert_store_link(
        inventory_path=str(game_dir),
        store_name="GOG",
        account_id="acc2",
        entitlement_id="ent-gog-portal",
        match_method="strong_id",
        confidence=1.0,
        verified=True,
    )

    _, items = app.refresh()
    game = next(item for item in items if item.is_dir and Path(item.full_path) == game_dir)
    assert game.owned_stores == ["Steam", "GOG"]
    assert game.primary_store == "Steam"


def test_manual_owned_store_assignment_updates_refresh_output(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Hades"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))

    assigned = app.set_manual_owned_stores(str(game_dir), ["EGS", "Steam"])
    assert assigned == 2

    _, items = app.refresh()
    game = next(item for item in items if item.is_dir and Path(item.full_path) == game_dir)
    assert game.owned_stores == ["Steam", "EGS"]
    assert game.primary_store == "Steam"


def test_name_match_backfills_steamid_for_future_strong_linking(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Aaero2 [FitGirl Repack]"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))
    app.db.upsert_store_account(
        "Steam",
        "acc1",
        "tester",
        "steam_openid_webapi",
        enabled=True,
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "acc1",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="acc1",
                entitlement_id="3010090",
                title="Aaero2",
                store_game_id="3010090",
            )
        ],
    )

    _, items = app.refresh()
    linked = app.rebuild_store_links_from_inventory(items)
    assert linked == 1
    assert (game_dir / "steam_appid.txt").read_text(encoding="utf-8").strip() == "3010090"
    metadata = app.read_folder_icon_metadata(str(game_dir))
    assert metadata.get("SteamAppId") == "3010090"


def test_rebuild_store_links_is_incremental_when_nothing_changed(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal 2"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))
    app.db.upsert_store_account(
        "Steam",
        "acc1",
        "tester",
        "steam_openid_webapi",
        enabled=True,
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "acc1",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="acc1",
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
            )
        ],
    )
    app.assign_steam_appid(str(game_dir), "620")

    _, items = app.refresh()
    first = app.rebuild_store_links_from_inventory(items)
    second = app.rebuild_store_links_from_inventory(items)

    assert first == 1
    assert second == 0


def test_store_id_change_forces_rebuild_for_that_store(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal 2"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))
    app.db.upsert_store_account(
        "Steam",
        "acc1",
        "tester",
        "steam_openid_webapi",
        enabled=True,
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "acc1",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="acc1",
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
            )
        ],
    )
    app.assign_steam_appid(str(game_dir), "620")
    _, items = app.refresh()
    assert app.rebuild_store_links_from_inventory(items) == 1

    app.assign_steam_appid(str(game_dir), "999999")
    _, items2 = app.refresh()
    changes = app.rebuild_store_links_from_inventory(items2)

    assert changes >= 1
    links = app.db.list_store_links_for_paths([str(game_dir)], verified_only=True)
    assert links == {}


def test_force_rebuild_all_reapplies_links_even_without_local_changes(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal 2"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))
    app.db.upsert_store_account(
        "Steam",
        "acc1",
        "tester",
        "steam_openid_webapi",
        enabled=True,
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "acc1",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="acc1",
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
            )
        ],
    )
    app.assign_steam_appid(str(game_dir), "620")
    _, items = app.refresh()
    assert app.rebuild_store_links_from_inventory(items) == 1

    forced = app.rebuild_store_links_from_inventory(items, force_rebuild_all=True)
    assert forced >= 1


def test_name_signature_change_forces_rebuild_for_game(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal 2"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))
    app.db.upsert_store_account(
        "Steam",
        "acc1",
        "tester",
        "steam_openid_webapi",
        enabled=True,
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "acc1",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="acc1",
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
            )
        ],
    )
    app.assign_steam_appid(str(game_dir), "620")
    _, items = app.refresh()
    assert app.rebuild_store_links_from_inventory(items) == 1
    assert app.rebuild_store_links_from_inventory(items) == 0

    dir_item = next(item for item in items if item.is_dir and Path(item.full_path) == game_dir)
    dir_item.cleaned_name = "Portal II"
    changed = app.rebuild_store_links_from_inventory(items)
    assert changed >= 1


def test_clear_owned_store_info_for_inventory_resets_links_and_id_hints(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    root = tmp_path / "root"
    game_dir = root / "Portal"
    game_dir.mkdir(parents=True)
    app.add_root(str(root))

    app.db.upsert_store_link(
        inventory_path=str(game_dir),
        store_name="Steam",
        account_id="acc1",
        entitlement_id="620",
        match_method="strong_id",
        confidence=1.0,
        verified=True,
    )
    app.db.upsert_store_link(
        inventory_path=str(game_dir),
        store_name="GOG",
        account_id="acc2",
        entitlement_id="gog-620",
        match_method="manual_confirmed",
        confidence=1.0,
        verified=True,
    )
    app.assign_store_id_hint(
        folder_path=str(game_dir),
        store_name="Steam",
        store_id="620",
    )
    app.assign_store_id_hint(
        folder_path=str(game_dir),
        store_name="GOG",
        store_id="gog-620",
    )

    app.clear_owned_store_info_for_inventory(str(game_dir))

    links = app.db.list_store_links_for_paths([str(game_dir)], verified_only=True)
    assert links == {}
    assert not (game_dir / "steam_appid.txt").exists()
    assert not (game_dir / ".gm_store_ids.json").exists()
