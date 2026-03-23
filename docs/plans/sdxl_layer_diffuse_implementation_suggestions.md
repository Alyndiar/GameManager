# sdxl_layer_diffuse_implementation_suggestions

## Overview

Replace the ComfyUI-based icon-template generation subsystem with a direct in-process Python inference pipeline built around:

- SDXL base model
- LayerDiffuse transparent-image components
- Diffusers-based pipeline execution inside the Python app
- the existing icon optimization process as the downstream normalization step

### Core objective

Build icon-template generation directly into the Python program instead of calling ComfyUI.

The program must generate raw transparent border/frame candidates using **SDXL + LayerDiffuse**, then pass the raw result into the existing icon optimization pipeline so the final template matches the exact specifications.

## Final icon-template requirements

- Final deliverable is always a **512x512 PNG with alpha transparency**
- Both the inside and outside of the border/shape must be transparent
- Only the border/frame itself is visible
- Minimal or no padding around the border in the final normalized result
- Final result must be centered, symmetrical, reusable, and compositing-safe
- Raw generation should occur at **1024x1024 whenever practical**, with **768x768 as fallback**
- The existing icon optimization pipeline remains the authority for final normalization, padding correction, centering, cleanup, and final export

## Persistence and preset rules

Keep the preset-list behavior exactly as previously specified:

- shape, material, and width are dropdown-backed editable lists
- lists are persisted outside the project DB
- lists survive between runs and between databases
- lists are stored as separate JSON files

### Required JSON preset files

- `shapes.json`
- `materials.json`
- `widths.json`

Do **not** store these presets in the project database.

## Architecture to implement

Implement the following major components:

1. JSON-backed preset storage
2. UI dropdown + preset editor integration
3. direct Python inference engine for SDXL + LayerDiffuse
4. structured prompt builder with fixed slots for shape/material/width
5. generation job orchestration inside the app
6. post-generation handoff to the existing icon optimization pipeline
7. final validation against icon-template rules
8. metadata/debug artifact persistence

## Inference-engine design

Create a dedicated local inference module that runs inside the Python application process or in an app-managed worker process.

### Recommended implementation approach

- Use Hugging Face Diffusers for SDXL pipeline loading
- Use LayerDiffuse-compatible SDXL transparent components through the available Diffusers-compatible path
- Load:
  - SDXL base model
  - LayerDiffuse SDXL transparent-attn LoRA by default
  - transparent VAE decoder / transparency-capable decode path required for PNG alpha output
- Keep all inference configuration in a dedicated module, not scattered across UI code

### Important design rule

Do not implement this as freeform prompt submission from the UI.

The UI should select:

- shape
- material
- width

The app should then build a constrained prompt from those resolved presets.

## Model/loading notes

Target SDXL first.

Default stack should be:

- `stabilityai/stable-diffusion-xl-base-1.0` or compatible SDXL base checkpoint
- LayerDiffuse SDXL transparent-attn LoRA as the default transparent-generation transform
- transparent decoder path required for alpha output

Prefer the transparent-attn path first. Keep support for future alternate transparent-conv integration if needed. Do not overcomplicate phase 1 with model swapping UI unless the app already has a model-config system.

## Inference resolution policy

- Prefer **1024x1024** generation for SDXL when VRAM/performance allow
- Fall back to **768x768** if necessary
- Do not generate directly at **512x512** except debug/fallback mode
- The existing icon optimization process will convert the raw result to final **512x512** normalized output

## Preset schemas

Keep the preset JSON design editable and external to the DB.

### `shapes.json`

```json
[
  {
    "id": "circle",
    "label": "Circle",
    "prompt_value": "perfect circular ring",
    "enabled": true
  },
  {
    "id": "square",
    "label": "Square",
    "prompt_value": "square frame with sharp corners",
    "enabled": true
  },
  {
    "id": "rounded_square",
    "label": "Rounded Square",
    "prompt_value": "square frame with rounded corners",
    "enabled": true
  },
  {
    "id": "hexagon",
    "label": "Hexagon",
    "prompt_value": "regular hexagonal frame",
    "enabled": true
  },
  {
    "id": "octagon",
    "label": "Octagon",
    "prompt_value": "regular octagonal frame",
    "enabled": true
  }
]
```

### `materials.json`

```json
[
  {
    "id": "blue_brushed_metal",
    "label": "Blue brushed metal",
    "prompt_value": "blue brushed metal, reflective, fine machined grain, subtle radial and linear brushing",
    "negative_prompt_value": "",
    "enabled": true
  },
  {
    "id": "golden_glass",
    "label": "Golden glass",
    "prompt_value": "gold-tinted transparent glass, glossy polished edges, clean refraction, crisp specular highlights",
    "negative_prompt_value": "",
    "enabled": true
  },
  {
    "id": "pitted_dark_wood",
    "label": "Pitted dark wood",
    "prompt_value": "dark wood, visible grain, shallow pits, worn natural texture, matte to satin finish",
    "negative_prompt_value": "",
    "enabled": true
  }
]
```

### `widths.json`

```json
[
  {
    "id": "thinnest",
    "label": "Thinnest",
    "mode": "semantic",
    "pixel_width": 8,
    "prompt_value": "extremely thin border, approximately 8 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "thinner",
    "label": "Thinner",
    "mode": "semantic",
    "pixel_width": 10,
    "prompt_value": "very thin border, approximately 10 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "thin",
    "label": "Thin",
    "mode": "semantic",
    "pixel_width": 15,
    "prompt_value": "very thin border, approximately 15 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "medium",
    "label": "Medium",
    "mode": "semantic",
    "pixel_width": 25,
    "prompt_value": "medium border, approximately 25 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "wide",
    "label": "Wide",
    "mode": "semantic",
    "pixel_width": 35,
    "prompt_value": "wide prominent border, approximately 35 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "wider",
    "label": "Wider",
    "mode": "semantic",
    "pixel_width": 45,
    "prompt_value": "very wide prominent border, approximately 45 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "widest",
    "label": "Widest",
    "mode": "semantic",
    "pixel_width": 55,
    "prompt_value": "extremely wide prominent border, approximately 55 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_8",
    "label": "8 px",
    "mode": "pixel",
    "pixel_width": 8,
    "prompt_value": "border approximately 8 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_10",
    "label": "10 px",
    "mode": "pixel",
    "pixel_width": 10,
    "prompt_value": "border approximately 10 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_15",
    "label": "15 px",
    "mode": "pixel",
    "pixel_width": 15,
    "prompt_value": "border approximately 15 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_25",
    "label": "25 px",
    "mode": "pixel",
    "pixel_width": 25,
    "prompt_value": "border approximately 25 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_35",
    "label": "35 px",
    "mode": "pixel",
    "pixel_width": 35,
    "prompt_value": "border approximately 35 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_45",
    "label": "45 px",
    "mode": "pixel",
    "pixel_width": 45,
    "prompt_value": "border approximately 45 pixels at final 512x512 scale",
    "enabled": true
  },
  {
    "id": "px_55",
    "label": "55 px",
    "mode": "pixel",
    "pixel_width": 55,
    "prompt_value": "border approximately 55 pixels at final 512x512 scale",
    "enabled": true
  }
]
```

## UI requirements

Keep the UI behavior from the previous plan:

- dropdowns for Shape / Material / Width
- lists loaded from JSON, not hardcoded
- in-app editor for add/edit/remove/enable-disable/reorder
- changes persist to JSON files on disk
- these presets survive DB replacement/reset

### Guided preset creation

The plan must explicitly support adding new presets to each section from within the app.

Add a guided preset-creation flow for:

- Shapes
- Materials
- Widths

This flow should ask for all fields required to create a valid JSON entry instead of expecting the user to know the schema.

#### Guided creation behavior

- The UI opens a preset creation dialog or wizard
- The wizard adapts to the preset type being created
- The wizard validates required fields before save
- The wizard shows a live JSON preview before final confirmation
- The wizard writes the new preset to the correct external JSON file
- The new preset becomes immediately available in the dropdown lists
- The system must also support editing existing presets through the same guided flow

#### Required fields by preset type

**Shape preset**
- `id`
- `label`
- `prompt_value`
- `enabled`

Prompt the user for:
- display name
- internal id / slug
- exact SDXL prompt fragment
- enabled/disabled state

**Material preset**
- `id`
- `label`
- `prompt_value`
- `negative_prompt_value`
- `enabled`

Prompt the user for:
- display name
- internal id / slug
- positive prompt fragment
- optional negative prompt fragment
- enabled/disabled state

**Width preset**
- `id`
- `label`
- `mode`
- `pixel_width`
- `prompt_value`
- `enabled`

Prompt the user for:
- display name
- internal id / slug
- semantic or pixel mode
- target pixel width
- exact width prompt fragment
- enabled/disabled state

#### ID / slug assistance

The guided flow should help the user create safe ids by:
- suggesting an id automatically from the label
- lowercasing
- replacing spaces with underscores
- removing invalid characters
- warning on duplicates

#### Validation rules

Before saving a new preset, validate:
- id is unique within that preset file
- label is non-empty
- prompt_value is non-empty
- pixel_width is a positive integer for width presets
- mode is either `semantic` or `pixel` for width presets
- JSON remains valid after insertion

#### Querying the user for required details

The plan must explicitly support prompting the user for any missing required information when creating a preset.

Examples:
- If a new material preset is started without a negative prompt, the wizard should ask whether it should remain blank
- If a width preset is created without a pixel width, the wizard must require a numeric value before save
- If a shape preset id conflicts with an existing one, the wizard must ask for a different id or offer to edit the existing preset instead

#### Suggested functions

- `create_shape_preset_interactive()`
- `create_material_preset_interactive()`
- `create_width_preset_interactive()`
- `validate_new_preset_entry(entry, preset_type)`
- `suggest_preset_id(label)`
- `preview_preset_json(entry)`


## Inference service replacement

Remove the dependency on ComfyUI submission/polling and replace it with an internal inference runner.

Implement a dedicated generation module such as:

- `icon_template_inference.py`

This module should:

- initialize and cache the SDXL + LayerDiffuse pipeline
- accept a normalized generation spec
- build prompt + negative prompt
- run text-to-image generation directly
- save the raw output
- hand off to the existing icon optimization pipeline
- return raw path, optimized path, metadata, validation

### Suggested internal request model

```json
{
  "shape_id": "circle",
  "material_id": "blue_brushed_metal",
  "width_id": "wide",
  "target_final_size": 512,
  "preferred_generation_size": 1024,
  "seed": null,
  "steps": null,
  "cfg": null
}
```

### Normalized internal spec

```json
{
  "shape": {
    "id": "circle",
    "label": "Circle",
    "prompt_value": "perfect circular ring"
  },
  "material": {
    "id": "blue_brushed_metal",
    "label": "Blue brushed metal",
    "prompt_value": "blue brushed metal, reflective, fine machined grain, subtle radial and linear brushing"
  },
  "width": {
    "id": "wide",
    "label": "Wide",
    "mode": "semantic",
    "pixel_width": 40,
    "prompt_value": "wide prominent border, approximately 40 pixels at final 512x512 scale"
  },
  "generation_size": 1024,
  "final_size": 512,
  "transparent_background": true,
  "border_only": true
}
```

## Prompting system

Pre-analyze and implement one fixed SDXL + LayerDiffuse prompt structure with variable slots for:

- `{shape_prompt}`
- `{material_prompt}`
- `{width_prompt}`

Do not use random ad hoc prompt strings in multiple places. Implement a single authoritative prompt builder.

## Exact positive prompt template

Use this exact prompt template as the default SDXL + LayerDiffuse generation prompt:

```text
single centered icon template border, border only, hollow center, transparent background, transparent outside the frame, isolated reusable UI asset, straight-on orthographic front view, perfectly symmetrical, minimal outer padding, nearly full-frame, no clipping, crisp silhouette, clean anti-aliased edges, high-quality surface detail, {shape_prompt}, {material_prompt}, {width_prompt}, inner area fully transparent, outer area fully transparent, no fill, no center emblem, no inset symbol, no scene, no background plate, no environment
```

## Exact negative prompt template

Use this exact negative prompt template by default:

```text
filled center, opaque background, black background, white background, gradient background, scenery, landscape, environment, object inside frame, logo, text, letters, icon symbol, medallion, coin, badge, solid plate, perspective view, angled view, asymmetry, double border, multiple objects, clipping, crop, excessive empty margins, thick opaque center, inner artwork, posterization, blurry edges, muddy texture, deformed geometry
```

## Variable insertion rules

- `{shape_prompt}` comes from `shapes.json` `prompt_value`
- `{material_prompt}` comes from `materials.json` `prompt_value`
- `{width_prompt}` comes from `widths.json` `prompt_value`

### Important prompt rule

Always keep the structural part of the prompt stable. Only substitute the 3 preset fragments.

Do not let the UI directly override the whole prompt in phase 1.

## Suggested SDXL inference defaults

Start with sensible defaults for SDXL transparent template generation:

- `steps`: 28 to 40
- `guidance_scale`: 5.0 to 7.0
- `width`: 1024 or 768
- `height`: 1024 or 768
- sampler/scheduler: choose a stable SDXL-friendly default supported by the selected diffusers pipeline
- `seed`: optional, save if generated automatically

Keep these configurable in one settings object, not spread through code.

## Recommended geometry biasing

Because the use case is strict geometric borders, implement optional prompt augmentation / generation constraints:

- prepend `single centered`
- explicitly mention `orthographic front view`
- explicitly mention `perfectly symmetrical`
- explicitly mention `nearly full-frame`
- explicitly mention `inner area fully transparent` and `outer area fully transparent`

If later needed, leave room for adding shape masks or control conditioning in a future phase, but do not block phase 1 on that.

## Raw output handling

Save the raw generation result before any optimization.

Preserve:

- raw transparent PNG if available
- intermediate decoded PNG if useful for debugging
- metadata JSON including prompt, seed, resolution, preset ids, model identifiers

## Existing icon optimization integration

After raw generation completes, automatically pass the result into the existing icon optimization pipeline already in place.

Do not duplicate that logic unless absolutely necessary.

The optimization pipeline remains responsible for:

- final size normalization to 512x512
- final padding correction
- centering correction
- alpha cleanup
- ensuring only the border remains visible
- ensuring inside and outside remain transparent
- final export compliance

Use a clean explicit call such as:

```python
optimize_generated_icon_template(
    raw_image_path,
    target_pixel_width,
    shape_hint,
    material_hint,
    target_size=512,
    border_only=True,
    transparent_inside=True,
    transparent_outside=True,
)
```

If the existing function signature differs, adapt cleanly.

## Validation requirements

After optimization, validate the final output.

Checks:

- exact 512x512
- alpha channel exists
- outside region transparent
- inside region transparent
- only border visible
- border centered
- border not clipped
- padding minimal
- visible border width approximately matches requested width target
- shape consistent with selected preset

If validation fails:

- keep raw and optimized files
- write structured validation report
- log prompt, negative prompt, seed, generation size, preset ids, and model identifiers

## Metadata/debug persistence

Save per-job metadata JSON including:

- job id
- timestamp
- shape/material/width preset ids
- resolved pixel width
- generation resolution
- positive prompt
- negative prompt
- seed
- steps
- guidance scale
- model id
- LayerDiffuse component ids
- raw output path
- optimized output path
- validation results

### Suggested output layout

```text
outputs/
  icon_templates/
    raw/
    optimized/
    metadata/
```

## Implementation structure

Create modules/functions similar to the following.

### Preset/config

- `load_shape_presets()`
- `save_shape_presets()`
- `load_material_presets()`
- `save_material_presets()`
- `load_width_presets()`
- `save_width_presets()`
- `ensure_default_icon_template_presets()`

### Normalization

- `resolve_icon_template_request(request) -> IconTemplateSpec`
- `resolve_shape_preset(id)`
- `resolve_material_preset(id)`
- `resolve_width_preset(id)`

### Prompting

- `build_icon_template_prompt(spec) -> str`
- `build_icon_template_negative_prompt(spec) -> str`

### Inference

- `load_icon_template_pipeline()`
- `unload_icon_template_pipeline_if_needed()`
- `generate_raw_icon_template(spec) -> raw path + metadata`
- `choose_generation_size(spec, device_caps) -> 1024 or 768`
- `run_sdxl_layerdiffuse_inference(spec, prompt, negative_prompt) -> image`

### Pipeline

- `optimize_generated_icon_template(raw_path, spec) -> final path + metadata`
- `validate_icon_template(final_path, spec) -> validation report`
- `run_icon_template_pipeline(request) -> final result`

### UI integration

- populate dropdowns from JSON-backed presets
- allow in-app editing of presets
- submit generation using preset ids
- show raw and optimized previews if the app already supports preview panes
- expose seed/resolution/settings only in advanced mode if desired

## Error handling

Handle:

- missing preset ids
- corrupt preset JSON
- missing model files
- LayerDiffuse component load failure
- insufficient VRAM
- inference timeout or worker crash
- optimization failure
- validation failure

For corrupt preset JSON:

- report clearly
- do not silently erase custom presets
- back up malformed files before repair if repair is implemented

## Acceptance requirements

1. Shape / Material / Width dropdowns still exist and are editable in-app
2. Preset lists still persist outside the project DB in separate JSON files
3. The program can generate icon-template borders directly through a Python SDXL + LayerDiffuse inference path
4. Prompt generation uses the exact structured prompt template above with inserted variables
5. Raw generation happens at 1024 preferred, 768 fallback
6. Raw result is automatically passed into the existing icon optimization pipeline
7. Final result matches icon-template rules:
   - 512x512
   - transparent inside
   - transparent outside
   - border only
   - minimal padding
8. Debug metadata is preserved

## Do not do

- Do not store presets in the project DB
- Do not bypass the existing icon optimization process
- Do not allow freeform prompt chaos in phase 1
- Do not generate full icon illustrations
- Do not place inner symbols
- Do not output filled badges
- Do not assume a black background is acceptable

## Variable examples

### Shapes

**Circle**
```text
perfect circular ring
```

**Square**
```text
square frame with sharp corners
```

**Rounded Square**
```text
square frame with rounded corners
```

**Hexagon**
```text
regular hexagonal frame
```

**Octagon**
```text
regular octagonal frame
```

### Materials

**Blue brushed metal**
```text
blue brushed metal, reflective, fine machined grain, subtle radial and linear brushing
```

**Golden glass**
```text
gold-tinted transparent glass, glossy polished edges, clean refraction, crisp specular highlights
```

**Pitted dark wood**
```text
dark wood, visible grain, shallow pits, worn natural texture, matte to satin finish
```

### Widths

**Thinnest**
```text
extremely thin border, approximately 8 pixels at final 512x512 scale
```

**Thinner**
```text
very thin border, approximately 10 pixels at final 512x512 scale
```

**Thin**
```text
very thin border, approximately 15 pixels at final 512x512 scale
```

**Medium**
```text
medium border, approximately 25 pixels at final 512x512 scale
```

**Wide**
```text
wide prominent border, approximately 35 pixels at final 512x512 scale
```

**Wider**
```text
very wide prominent border, approximately 45 pixels at final 512x512 scale
```

**Widest**
```text
extremely wide prominent border, approximately 55 pixels at final 512x512 scale
```


## Exact prompt block for direct reuse

### Positive prompt

```text
single centered icon template border, border only, hollow center, transparent background, transparent outside the frame, isolated reusable UI asset, straight-on orthographic front view, perfectly symmetrical, minimal outer padding, nearly full-frame, no clipping, crisp silhouette, clean anti-aliased edges, high-quality surface detail, {shape_prompt}, {material_prompt}, {width_prompt}, inner area fully transparent, outer area fully transparent, no fill, no center emblem, no inset symbol, no scene, no background plate, no environment
```

### Negative prompt

```text
filled center, opaque background, black background, white background, gradient background, scenery, landscape, environment, object inside frame, logo, text, letters, icon symbol, medallion, coin, badge, solid plate, perspective view, angled view, asymmetry, double border, multiple objects, clipping, crop, excessive empty margins, thick opaque center, inner artwork, posterization, blurry edges, muddy texture, deformed geometry
```

## Practical implementation note

Treat the direct Python LayerDiffuse path as an internal inference backend abstraction, not hardwired UI code.

Suggested pattern:

- `IconTemplateGeneratorBackend` interface
- current implementation: `SdxlLayerDiffuseBackend`
- future implementations can be added later without rewriting the rest of the app


## Preset Syncing (JSON Defaults ↔ App)

Add automatic synchronization between the markdown-defined presets and the JSON preset files.

### Behavior

- On first run:
  - If `shapes.json`, `materials.json`, or `widths.json` do not exist → create them using the presets defined in this document.
- On subsequent runs:
  - Load existing JSON files
  - Do NOT overwrite user changes
  - Optionally add missing presets (non-destructive merge)
- Provide a “Reset to Defaults” button in UI to restore original presets

### Sync Rules

- Presets defined in this markdown = canonical defaults
- JSON files = user-customized state
- Merge strategy:
  - Add missing presets
  - Keep user edits
  - Never silently delete user presets

---

## Width Visual Preview System

Add a deterministic visual preview overlay to ensure pixel-accurate border thickness.

### Purpose

Diffusion models are inconsistent with thickness. The preview system ensures:
- predictable results
- visual validation before generation
- alignment with the optimization pipeline

### Implementation

Create a preview overlay renderer:

Function:
- `render_width_preview(shape, pixel_width, canvas_size=512) -> preview_image`

Behavior:
- Draw shape outline at exact pixel width
- Centered in 512x512 canvas
- Transparent background
- Overlay displayed in UI

### UI Integration

- When user selects Shape + Width:
  - Show preview overlay
- Allow toggle:
  - “Show width guide”
- Optionally overlay preview on generated result for comparison

### Shape-specific rendering rules

Circle:
- Use ellipse with exact stroke width

Square:
- Rect with stroke

Rounded Square:
- Rect with configurable radius

Hexagon / Octagon:
- Generate polygon points
- Stroke path with exact width

---

## Width Consistency Enforcement

To improve consistency across generations:

### During generation
- Include width prompt fragment (already defined)

### During optimization
- Measure detected border thickness
- Compare against target pixel width
- If deviation > tolerance:
  - Optionally rescale or adjust

Suggested tolerance:
- ±3 px

---

## Optional Future Enhancement

Add a hybrid mode:

- Generate base shape mask procedurally
- Use SDXL + LayerDiffuse only for material rendering
- Composite result onto mask

This would eliminate geometry drift entirely.

(Not required for phase 1)
