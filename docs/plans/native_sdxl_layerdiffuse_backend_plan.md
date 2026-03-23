# Native SDXL + LayerDiffuse Backend Plan (Icon Templates)

## Goal
- Replace ComfyUI dependency for icon-template generation with a native in-app backend.
- Keep workflow fixed: same prompt pattern, SDXL + LayerDiffuse, transparent-background output.

## Why
- Lower runtime overhead than external ComfyUI orchestration.
- Better control of GPU/VRAM through existing `GpuMemoryOrchestrator`.
- Cleaner status/progress reporting in the main UI.
- Easier integration with existing icon adjust/composite/apply pipeline.

## Scope (v1)
- Add a native generation service for icon templates.
- Run generation in a dedicated worker process (keep UI responsive, isolate CUDA failures).
- Provide deterministic generation inputs (`seed`, `steps`, `cfg`, size preset).
- Produce `RGBA` output ready for existing icon pipeline.
- Keep backend toggle (`native` vs `comfyui`) so migration is reversible.

## Non-Goals (v1)
- Arbitrary node-graph editing.
- General-purpose image generation UI.
- Full Comfy custom-node parity beyond required fixed workflow.

## Proposed Architecture
1. `gamemanager/services/native_gen.py`
- Public API:
  - `generate_icon_template(request) -> GenerationResult`
  - `warm_native_gen()`
  - `release_native_gen(park_in_ram: bool = True)`
- Request fields:
  - `cleaned_name`, `prompt_preset`, `negative_prompt`, `seed`, `steps`, `cfg`, `width`, `height`.
- Result fields:
  - `image_rgba_path`, `provider`, `timings_ms`, `warnings`.

2. Worker process boundary
- New worker entrypoint for generation jobs.
- Main process sends request + receives progress events + final output path.
- Crash or OOM in worker does not terminate main UI.

3. GPU orchestration integration
- Register native generation stack as separate resource (e.g. `native_gen_stack`).
- Warm/load sequence controlled by orchestrator.
- Support parking/offload policy to reduce reload-from-disk cost.

4. UI integration
- Add backend selection in settings for icon generation source.
- Reuse existing status/progress widgets (`current/total`, operation label).
- Route icon-template generation calls through selected backend.

## Execution Plan
1. Backend scaffolding
- Create `native_gen.py` contracts + worker bootstrap + request/result dataclasses.
- Add backend selector settings storage and defaults.

2. Minimal generation pass
- Implement fixed prompt assembly from cleaned name.
- Implement deterministic inference path.
- Output one `RGBA` PNG artifact into project-local cache.

3. Orchestrator hooks
- Register and warm native generation models.
- Implement `release(... park_in_ram=True)` behavior.
- Add `ensure_free_vram(...)` checks before generation.

4. UI wiring
- Plug generation requests into current Set Icon flow.
- Surface progress, errors, and cancellation behavior.

5. Fallback and robustness
- If native backend unavailable, show actionable error and allow fallback to other backend.
- Add timeout, cancellation, and worker restart path.

## Validation / Tests
- Unit:
  - request validation and prompt assembly.
  - backend setting persistence.
  - orchestrator resource registration and status snapshots.
- Integration:
  - generate -> adjust framing -> apply icon for a single item.
  - multi-item run with cancel behavior.
  - worker crash containment (main app remains alive).
- Performance smoke:
  - cold vs warm latency.
  - VRAM before/after warm and after release/park.

## Notes
- Output storage must remain project-local (`.gamemanager_data/...`) per project policy.
- Secrets remain out of plain-text files (already handled elsewhere).
- If exact LayerDiffuse parity depends on Comfy-specific node behavior, add a compatibility shim in `native_gen.py` and capture differences in test fixtures.
