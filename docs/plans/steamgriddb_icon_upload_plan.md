# SteamGridDB Icon Upload Plan (Safe Targeting First)

## Summary
- Add optional icon upload to SteamGridDB for icons created or modified locally (not sourced from SteamGridDB).
- Track upload eligibility/state per game folder.
- Add upload entry points:
1. Right-click menu: `Upload to SteamGridDB`
2. Adjust Framing: `Upload to SteamGridDB` toggle (applies after successful icon apply).
- Enforce strict target-game safety: no upload by fuzzy name alone.

## Verified API Feasibility
- SteamGridDB API v2 supports uploads with bearer auth.
- Relevant endpoints:
1. `POST /icons` (upload icon with `game_id` + file asset)
2. `GET /search/autocomplete/{term}` (candidate games)
3. `GET /games/id/{gameId}` and `/games/{platform}/{platformId}` (target verification)
- Source:
1. https://www.steamgriddb.com/static/openapi.yml

## Safety Rules (Must-Have)
1. Never upload by cleaned name without explicit target confirmation.
2. Require manual confirmation of exact SGDB game target when binding is unknown.
3. Persist binding `folder_path -> sgdb_game_id` after first confirmed upload target.
4. Before every upload, show a preflight confirmation:
- SGDB title
- SGDB game ID
- platform IDs if available
5. If multiple close matches exist, disable one-click upload and force target selection.

## Upload Eligibility and Provenance Rules
1. If icon source was SteamGridDB, default to "already SGDB-derived" (upload action disabled by default).
2. If icon is local file, web capture, or locally framed/generated output, mark as upload-eligible.
3. If user explicitly wants upload anyway, allow override.

## Metadata and Persistence
### Desktop Metadata
- Extend `desktop.ini` with a dedicated section, for example `[GameManager.Upload]`, containing:
1. `SgdbEligible=0|1`
2. `SgdbUploaded=0|1`
3. `SgdbGameId=<int or empty>`
4. `SgdbIconId=<int or empty>`
5. `SgdbLastUploadUtc=<iso8601 or empty>`
6. `SgdbLastSource=<local|framed|sgdb|web|unknown>`

### Local DB Metadata
- Add a project-local table for robust tracking independent of `desktop.ini` parsing:
1. `folder_path` (PK)
2. `sgdb_game_id`
3. `last_uploaded_icon_id`
4. `eligible`
5. `uploaded`
6. `last_upload_utc`
7. `last_error`
8. `source_kind`

Notes:
- `desktop.ini` remains the portable on-folder marker.
- DB is authoritative for operational UI state/history and error reporting.

## UI/UX Changes
### Right-Click Menu
1. Add `Upload to SteamGridDB` action with shortcut (to be assigned in shortcut map and tooltips).
2. Enable only when:
- item is folder
- icon exists and status valid
- SGDB API key configured and SGDB enabled
- item is eligible or user override is selected
3. If binding exists, action label can include target hint: `Upload to SteamGridDB (Game ID: ####)`.

### Adjust Framing Dialog
1. Add checkbox: `Upload to SteamGridDB after Apply`.
2. Default from persisted preference.
3. If checked, post-apply flow invokes the same safe upload workflow.

### Target Selection Dialog (New)
1. Displays SGDB candidates from autocomplete with:
- name
- SGDB ID
- known platform IDs
2. Requires explicit selection.
3. Supports "Remember for this folder" (default on).

## Service Architecture
### New Service Module
- `gamemanager/services/steamgriddb_upload.py`

### Core APIs
1. `search_sgdb_games(term, settings) -> list[SgdbGameCandidate]`
2. `resolve_sgdb_game_by_id(game_id, settings) -> SgdbGameDetails`
3. `upload_icon_to_sgdb(game_id, icon_path_or_bytes, settings) -> UploadResult`
4. `delete_uploaded_icon(icon_id, settings) -> DeleteResult` (optional but recommended)
5. `validate_upload_preconditions(item, settings, metadata) -> ValidationResult`

### AppState APIs
1. Read/write upload metadata (DB + desktop.ini sync helpers)
2. Resolve current folder SGDB binding
3. Execute upload operation with progress and cancellation hook

## Flow Design
### Manual Upload from Context Menu
1. User triggers upload.
2. Preconditions checked.
3. Target resolution:
- if bound SGDB game ID exists: show confirmation
- else open target selection dialog
4. Upload icon via `POST /icons`.
5. Parse success/failure, persist metadata, refresh row status.

### Auto Upload from Adjust Framing Toggle
1. Icon apply succeeds.
2. If toggle checked, run same target-safe flow.
3. If target unresolved, prompt immediately; if canceled, keep icon apply success and record upload skipped.

## Error Handling
1. Distinguish:
- auth/config errors (missing/invalid key)
- target resolution errors
- upload validation errors (mime/dimensions/size)
- API/network errors
2. Persist last error and expose in status/tooltip.
3. Never roll back local icon apply when upload fails.

## Validation and Test Plan
### Unit
1. SGDB upload request construction (`Authorization`, multipart payload, endpoint).
2. Target safety rules and binding fallback logic.
3. Desktop metadata parsing/writing for new upload section.

### Integration (mocked HTTP)
1. Unbound folder -> candidate dialog required.
2. Bound folder -> direct preflight -> upload.
3. Auth failure and validation failure handling.
4. Post-upload metadata updates (DB and desktop.ini).

### UI
1. Right-click enable/disable matrix.
2. Adjust Framing toggle behavior.
3. Shortcut/tooltip presence for upload action.

## Phased Implementation
1. Phase 1: service + data model + metadata persistence + manual upload menu action.
2. Phase 2: target selection dialog + binding persistence.
3. Phase 3: Adjust Framing upload toggle integration.
4. Phase 4: optional management actions (reupload, unlink binding, delete uploaded SGDB asset).

## Non-Goals (Initial)
1. Bulk auto-upload of all icons without user confirmation.
2. Auto-creating SGDB game entries.
3. Uploading grids/heroes/logos in the first pass.

