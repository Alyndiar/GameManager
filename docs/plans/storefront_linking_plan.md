# Storefront Linking Plan (Post-Rebase Resume)

## Status
- State: paused by request.
- Resume trigger: after `IconCreator` branch rebase and feature-separation rework are complete.
- Scope excludes: Xbox / Microsoft Store and Rockstar.

## Scope Summary
- Add optional storefront account linking and owned-library sync for:
1. Steam
2. Epic
3. GOG
4. Ubisoft Connect
5. itch.io
6. Amazon Games
7. Battle.net
8. Humble
- Display storefront identity in both inventory views:
1. Icon mode: small platform badge/overlay.
2. List mode: `Store` column (single or multi-source marker).

## Feasibility Snapshot (Based on Playnite Integration Patterns)
1. Steam
- Most feasible.
- Supports installed + account library style import.
- Private-account path may need API key handling.

2. Epic
- Feasible with web auth/session flow and launcher-style endpoints.
- Higher fragility risk due endpoint/session changes.

3. GOG
- Feasible with web-session based account access plus installed detection.
- Endpoint behavior can change; fallback paths needed.

4. Ubisoft Connect
- Best treated as local-client-data integration first.
- Reliable installed/library detection from local launcher cache; account-wide remote API is weak.

5. itch.io
- Feasible and relatively clean if itch client/butler path is available.
- Installed + owned-key style library import possible.

6. Amazon Games
- Feasible with launcher-style web auth and entitlement fetch flow.
- Maintenance risk is medium/high due private endpoint behavior.

7. Battle.net
- Feasible with web auth + account endpoint reads.
- Maintenance risk medium due endpoint/session changes.

8. Humble
- Feasible with web auth + library/orders fetch.
- Maintenance risk medium due HTML/JSON shape changes.

## Product Behavior Requirements
1. Linking must be optional per storefront.
2. Backup inventory remains functional without any linked accounts.
3. Sync must not block UI.
4. If a connector fails, only that connector is degraded; app remains usable.
5. Platform tags must be persisted so badges/columns survive refresh.

## Data Model and Persistence
### Project-Local Database (SQLite)
- Add project-local tables:
1. `store_accounts`
2. `store_tokens_meta`
3. `store_owned_games`
4. `store_links` (inventory folder/game mapping)
5. `store_sync_runs`

### Suggested Fields
1. `store_accounts`
- `store_name`, `account_id`, `display_name`, `enabled`, `last_sync_utc`.
2. `store_tokens_meta`
- token metadata only (`expires_utc`, `scopes`, `status`), never plaintext secrets.
3. `store_owned_games`
- canonical game id per store, title, entitlement/install flags, last_seen.
4. `store_links`
- `inventory_path` to store title mapping, confidence, match method, last_verified.
5. `store_sync_runs`
- run status, duration, error summary.

### Secret Storage
1. Reuse secure secret path already used in app:
- Windows Credential Manager first.
2. Keep only non-secret metadata in project-local DB.
3. Do not write plaintext credentials/tokens to project files.

## Matching Strategy (Owned Game -> Backup Folder)
1. Deterministic match first:
- known app ids, manifest ids, launch URLs, existing metadata hints.
2. Normalized title match second:
- cleaned name, edition suffix stripping, token overlap thresholds.
3. Manual confirmation fallback:
- unresolved or ambiguous matches require user confirmation.
4. Persist confirmed matches in `store_links`.

## UI Changes
### Inventory List Mode
1. Add `Store` column to right table.
2. Display compact value:
- single store name/icon key for one link.
- short multi-store marker for multiple links.

### Inventory Icon Mode
1. Add badge overlay on each tile icon.
2. Support multi-store badge strategy:
- one primary icon + `+N`, or stacked mini badges.

### Settings
1. New `Storefront Accounts` section:
- per-store enable/disable.
- connect/disconnect.
- sync now.
- sync error diagnostics.
2. Keep existing Icon settings separate from account linking.

## Runtime and Architecture
1. Add storefront connector abstraction:
- one connector module per store under `gamemanager/services/storefronts/`.
2. Execute each connector in worker threads (or subprocess when needed).
3. Orchestrate sync in a coordinator service with cancellation + progress callbacks.
4. Isolate high-risk parsing/network logic to avoid crashing main UI flow.

## Suggested Module Layout
1. `gamemanager/services/storefronts/base.py`
2. `gamemanager/services/storefronts/steam_connector.py`
3. `gamemanager/services/storefronts/epic_connector.py`
4. `gamemanager/services/storefronts/gog_connector.py`
5. `gamemanager/services/storefronts/ubisoft_connector.py`
6. `gamemanager/services/storefronts/itch_connector.py`
7. `gamemanager/services/storefronts/amazon_connector.py`
8. `gamemanager/services/storefronts/battlenet_connector.py`
9. `gamemanager/services/storefronts/humble_connector.py`
10. `gamemanager/services/storefront_sync.py`
11. `gamemanager/services/store_linking.py`

## Error Handling Rules
1. Connector failures are isolated and reported per store.
2. Token expiry triggers re-auth flow, not silent data corruption.
3. Sync writes are atomic per store run.
4. Partial results can still update badges/column when safe.

## Testing Plan
### Unit
1. connector parsing and mapping logic.
2. token metadata lifecycle logic.
3. matching confidence and conflict handling.
4. UI formatting for badges and list column values.

### Integration
1. end-to-end sync for each connector via mocked responses/fixtures.
2. DB migration and backward compatibility.
3. cancellation and retry behavior.

### UI
1. list/icon rendering with zero/one/multi-store links.
2. sync status and error display.
3. account connect/disconnect workflows.

## Phased Implementation
1. Phase 0: foundation
- DB migration, connector interface, sync coordinator, UI column/badge plumbing.

2. Phase 1: low-risk connectors first
- Steam + itch.io.
- Deliver usable badges/column and match persistence.

3. Phase 2: medium-risk connectors
- GOG + Ubisoft + Humble + Battle.net.
- Add robust diagnostics and retry behavior.

4. Phase 3: high-maintenance connectors
- Epic + Amazon.
- Harden re-auth and endpoint-change fallback handling.

5. Phase 4: polish
- manual relink tools, conflict resolver UI, bulk resync controls.

## Effort Envelope (Post-Rebase)
1. Foundation + UI plumbing: 4 to 7 days.
2. Steam + itch.io: 5 to 9 days.
3. GOG + Ubisoft + Humble + Battle.net: 10 to 18 days.
4. Epic + Amazon hardening: 8 to 14 days.
5. Total practical range: 27 to 48 working days, depending on endpoint stability and match-quality expectations.

## Non-Goals (Initial)
1. Xbox / Microsoft Store integration.
2. Rockstar account-wide library sync.
3. Cloud save sync and launcher management.
4. Fully automatic conflict resolution without user override.

## Resume Checklist
1. Rebase complete and UI/module boundaries stabilized.
2. Confirm final naming for list column and badge style.
3. Confirm first delivery slice:
- recommended: Phase 0 + Steam + itch.io.
