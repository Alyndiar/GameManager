from datetime import datetime
import json
from pathlib import Path

from gamemanager.models import InventoryItem, StoreOwnedGame
from gamemanager.services.store_linking import (
    persist_store_id_hint,
    preferred_store_id_for_owned_game,
    strict_match_inventory_to_owned_games,
)


def _inventory_item(path: str) -> InventoryItem:
    now = datetime(2026, 1, 1, 12, 0, 0)
    return InventoryItem(
        root_id=1,
        root_path="C:\\Games",
        source_label="C:",
        full_name="Portal",
        full_path=path,
        is_dir=True,
        extension="",
        size_bytes=0,
        created_at=now,
        modified_at=now,
        cleaned_name="Portal",
        scan_ts=now,
    )


def test_strict_match_links_only_on_strong_ids() -> None:
    item = _inventory_item("C:\\Games\\Portal")
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SteamAppId": "620"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert len(matches) == 1
    assert matches[0].store_name == "Steam"
    assert matches[0].match_method == "strong_id"


def test_strict_match_does_not_use_title_only_fuzzy_match() -> None:
    item = _inventory_item("C:\\Games\\Portal")
    item.cleaned_name = "Portalish"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SomeTitleHint": "Portal"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert matches == []


def test_strict_match_uses_unique_exact_title_match() -> None:
    item = _inventory_item("C:\\Games\\Aaero2")
    item.full_name = "Aaero2 [FitGirl Repack]"
    item.cleaned_name = "Aaero2"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="3010090",
        title="Aaero2",
        store_game_id="3010090",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {},
        owned_games_by_store={"Steam": [owned]},
    )
    assert len(matches) == 1
    assert matches[0].match_method == "exact_title_unique"
    assert matches[0].confidence == 1.0


def test_strict_match_skips_ambiguous_exact_title_match() -> None:
    item = _inventory_item("C:\\Games\\Portal")
    owned_a = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
    )
    owned_b = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-621",
        title="Portal",
        store_game_id="621",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {},
        owned_games_by_store={"Steam": [owned_a, owned_b]},
    )
    assert matches == []


def test_strict_match_exact_normalization_ignores_symbols_and_case() -> None:
    item = _inventory_item("C:\\Games\\Prince-of-Persia")
    item.full_name = "Prince-of-Persia"
    item.cleaned_name = "Prince-of-Persia"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="1001",
        title="prince of persia",
        store_game_id="1001",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {},
        owned_games_by_store={"Steam": [owned]},
    )
    assert len(matches) == 1
    assert matches[0].match_method == "exact_title_unique"
    assert matches[0].confidence == 1.0


def test_strict_match_steam_primary_metadata_overrides_legacy_conflict() -> None:
    item = _inventory_item("C:\\Games\\Audiosurf")
    item.full_name = "Audiosurf"
    item.cleaned_name = "Audiosurf"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-235800",
        title="Audiosurf 2",
        store_game_id="235800",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SteamAppId": "12900", "SteamID": "235800"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert matches == []


def test_strict_match_unmatched_explicit_id_blocks_exact_title_fallback() -> None:
    item = _inventory_item("C:\\Games\\Portal")
    item.full_name = "Portal"
    item.cleaned_name = "Portal"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SteamAppId": "999999"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert matches == []


def test_strict_match_steam_file_id_overrides_stale_metadata(tmp_path: Path) -> None:
    folder = tmp_path / "Audiosurf"
    folder.mkdir(parents=True)
    (folder / "steam_appid.txt").write_text("12900\n", encoding="utf-8")
    item = _inventory_item(str(folder))
    item.full_name = "Audiosurf"
    item.cleaned_name = "Audiosurf"
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-235800",
        title="Audiosurf 2",
        store_game_id="235800",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SteamID": "235800"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert matches == []


def test_strict_match_steam_legacy_metadata_still_supported() -> None:
    item = _inventory_item("C:\\Games\\Portal")
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
    )
    matches = strict_match_inventory_to_owned_games(
        [item],
        metadata_loader=lambda _path: {"SteamID": "620"},
        owned_games_by_store={"Steam": [owned]},
    )
    assert len(matches) == 1
    assert matches[0].match_method == "strong_id"


def test_preferred_store_id_prefers_numeric_steam_identifiers() -> None:
    owned = StoreOwnedGame(
        store_name="Steam",
        account_id="acc1",
        entitlement_id="ent-620",
        title="Portal",
        store_game_id="620",
        manifest_id="620",
    )
    assert preferred_store_id_for_owned_game("Steam", owned) == "620"


def test_persist_store_id_hint_writes_ids_file_and_steam_marker(tmp_path: Path) -> None:
    folder = tmp_path / "Portal"
    folder.mkdir(parents=True)
    changed = persist_store_id_hint(
        inventory_path=str(folder),
        store_name="Steam",
        store_id="620",
    )
    assert changed is True
    assert (folder / "steam_appid.txt").read_text(encoding="utf-8").strip() == "620"
    payload = json.loads((folder / ".gm_store_ids.json").read_text(encoding="utf-8"))
    assert payload["Steam"] == "620"


def test_persist_store_id_hint_writes_non_steam_id_file_only(tmp_path: Path) -> None:
    folder = tmp_path / "Hades"
    folder.mkdir(parents=True)
    changed = persist_store_id_hint(
        inventory_path=str(folder),
        store_name="GOG",
        store_id="gog-12345",
    )
    assert changed is True
    assert not (folder / "steam_appid.txt").exists()
    payload = json.loads((folder / ".gm_store_ids.json").read_text(encoding="utf-8"))
    assert payload["GOG"] == "gog-12345"
