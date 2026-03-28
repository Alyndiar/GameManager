# Codex Implementation Plan — Offline Icon Downscaling Optimizer

## Objective

Build a local Python pipeline that:

- accepts a source bitmap icon, typically around **256×256**
- improves perceptual readability for **32×32, 24×24, 16×16**
- automatically drops low-value detail such as tiny text or noise
- preserves the most important visual elements
- optionally uses vectorization for suitable icons
- generates multiple candidate outputs per target size
- scores them automatically
- selects the best candidate for each size
- exports the chosen variants and optionally packs them into a Windows `.ico`

This is **not** a generic image resize tool. It is an **icon-specific perceptual simplification and hinting pipeline**.

## High-level design

Implement a **dual-path pipeline**:

### Path A — raster-first
Use this as the default path.

Best for:
- shaded icons
- textured icons
- semi-realistic objects
- icons with gradients or glow
- icons where full vectorization would over-simplify or distort identity

### Path B — vector-assisted
Use only when the icon is sufficiently geometric.

Best for:
- flat-color icons
- badges
- emblems
- logos
- crisp symbolic shapes
- icons with few dominant regions and low texture complexity

The pipeline should automatically decide whether to run:
- raster only
- vector only
- or both, then compare candidate outputs

## Required stack

### Core dependencies
- `numpy`
- `Pillow`
- `opencv-python`
- `opencv-contrib-python`
- `scikit-image`

Reason:
- OpenCV provides core image ops and saliency support.
- scikit-image provides morphology, segmentation helpers, and removal of small objects.
- Pillow handles image I/O and ICO packaging.

### Optional dependency
- `vtracer`

Reason:
- VTracer is open source, local, and intended for raster-to-vector conversion, including color inputs.

## Proposed project structure

```text
icon_optimizer/
├─ pyproject.toml
├─ README.md
├─ configs/
│  ├─ default.yaml
│  ├─ scoring.yaml
│  └─ presets/
├─ src/
│  └─ icon_optimizer/
│     ├─ __init__.py
│     ├─ cli.py
│     ├─ pipeline.py
│     ├─ models.py
│     ├─ io_utils.py
│     ├─ preprocess.py
│     ├─ analysis.py
│     ├─ saliency.py
│     ├─ simplify.py
│     ├─ raster_path.py
│     ├─ vector_path.py
│     ├─ render_targets.py
│     ├─ candidate_generation.py
│     ├─ scoring.py
│     ├─ ico_export.py
│     ├─ debug_viz.py
│     └─ utils/
├─ tests/
│  ├─ test_preprocess.py
│  ├─ test_analysis.py
│  ├─ test_scoring.py
│  ├─ test_ico_export.py
│  └─ fixtures/
└─ output/
```

## Implementation phases

## Phase 1 — baseline CLI and plumbing

### Goal
Create a minimal end-to-end pipeline that:
- loads an input image
- preprocesses it
- generates simple downscaled outputs
- saves outputs for 32, 24, 16
- optionally writes an `.ico`

### Tasks
1. Build CLI:
   - `icon-optimize input.png --out out_dir`
   - options:
     - `--sizes 16 24 32`
     - `--ico`
     - `--debug`
     - `--config configs/default.yaml`

2. Implement image loading:
   - preserve alpha
   - normalize mode to RGBA
   - verify square or pad to square

3. Implement basic output writer:
   - save PNG per target size
   - optionally save `.ico`

4. Implement simple baseline resize variants:
   - Lanczos
   - bicubic
   - area / box-like reduction
   - sharpened version

5. Add deterministic filenames

### Acceptance criteria
- pipeline runs locally from CLI
- outputs are reproducible
- `.ico` export works for standard sizes up to 256×256 via Pillow

## Phase 2 — preprocessing and cleanup

### Goal
Improve the source before any simplification or downscaling.

### Tasks
Implement `preprocess.py`:

1. **Alpha normalization**
   - detect fully transparent padding
   - crop to visible bounds with margin
   - re-center on square canvas

2. **Premultiplied alpha safety**
   - avoid dark edge halos
   - ensure compositing-safe RGB under alpha

3. **Color normalization**
   - convert to sRGB-safe working space
   - optionally flatten weird embedded profiles if present

4. **Noise and micro-detail reduction**
   - edge-preserving smoothing
   - bilateral or similar conservative denoise
   - avoid destroying main contours

5. **Palette simplification**
   - quantize to a reduced palette
   - configurable palette sizes, e.g. 8 / 12 / 16 / 24 colors

6. **Morphological cleanup**
   - remove tiny isolated fragments
   - fill tiny holes
   - merge tiny gaps

### Acceptance criteria
- source becomes cleaner without major semantic damage
- transparent-edge contamination is reduced
- small junk regions can be removed by threshold

## Phase 3 — icon analysis and routing

### Goal
Automatically classify the icon and decide how aggressively to simplify it, and whether vectorization is worth trying.

### Tasks
Implement `analysis.py` to compute:

1. **Complexity metrics**
   - edge density
   - connected component count
   - contour count
   - local entropy estimate
   - palette size after quantization

2. **Structure metrics**
   - foreground coverage ratio
   - dominant connected component area
   - centrality of dominant object
   - stroke thinness estimate
   - amount of tiny isolated detail

3. **Text-likeness heuristics**
   - repeated narrow shapes
   - parallel micro-strokes
   - clustered tiny components
   - long thin horizontal runs

4. **Raster-vs-vector friendliness score**
   - vector-friendly if:
     - low texture
     - few large color regions
     - strong closed contours
     - stable silhouette
   - raster-friendly otherwise

5. **Suggested simplification strength**
   - light / medium / aggressive

### Output
A structured `AnalysisResult` dataclass:

```python
@dataclass
class AnalysisResult:
    vector_friendly_score: float
    raster_friendly_score: float
    complexity_score: float
    text_likelihood: float
    dominant_region_ratio: float
    simplification_level: str
    should_try_vector: bool
```

### Acceptance criteria
- routing decision is explainable
- debug output can show why a branch was selected

## Phase 4 — saliency and importance map

### Goal
Estimate what parts of the icon matter most so the pipeline can drop noise and preserve the right regions.

### Tasks
Implement `saliency.py`:

1. Use OpenCV saliency as an optional contributor:
   - try `StaticSaliencyFineGrained`
   - normalize result to 0..1

2. Blend saliency with icon-specific heuristics:
   - centrality prior
   - dominant connected component prior
   - silhouette edge importance
   - high-contrast boundary weighting
   - text-like region penalty
   - tiny island penalty

3. Produce:
   - saliency map
   - binary “important region” mask
   - ranked connected components

### Important note
Do **not** trust generic saliency alone. Treat it as one signal among several.

### Acceptance criteria
- map highlights dominant icon subject more than incidental microdetail
- text-like thin features can be deprioritized

## Phase 5 — simplification engine

### Goal
Create a normalized, icon-friendly intermediate representation before target rendering.

### Tasks
Implement `simplify.py`:

1. **Region pruning**
   - remove sub-threshold connected components
   - merge nearby tiny islands into nearest major region when reasonable

2. **Hole handling**
   - fill or preserve holes based on size and importance

3. **Stroke normalization**
   - detect thin strokes likely to disappear at target size
   - thicken them selectively

4. **Contour smoothing**
   - simplify jagged edges without erasing silhouette

5. **Text dropping**
   - if text-likelihood exceeds threshold, remove probable text regions or strongly down-weight them

6. **Contrast redistribution**
   - increase separation between important foreground and background
   - compress low-value tonal nuance

7. **Multi-strength variants**
   - light
   - medium
   - aggressive

### Acceptance criteria
- simplification reduces clutter
- main silhouette remains recognizable
- tiny text/details can disappear without wrecking the icon

## Phase 6 — raster path

### Goal
Produce optimized low-res candidates while staying entirely in raster.

### Tasks
Implement `raster_path.py`:

1. Start from:
   - original preprocessed image
   - simplified image
   - high-contrast simplified image
   - silhouette-emphasis image

2. For each target size:
   - render in linear-light aware or gamma-conscious flow
   - use multiple kernels:
     - Lanczos
     - bicubic
     - area/box style
   - apply optional post-downscale:
     - mild unsharp
     - contour reinforcement
     - selective contrast lift
     - isolated-pixel cleanup

3. Generate candidate variants:
   - `base`
   - `high_contrast`
   - `thickened`
   - `silhouette_priority`
   - `aggressive_cleanup`
   - `no_text`

### Acceptance criteria
- at least 5–8 candidates per target size
- outputs are visibly different enough to justify scoring

## Phase 7 — vector path

### Goal
Try vector-assisted rendering for icons likely to benefit from clean path geometry.

### Tasks
Implement `vector_path.py`:

1. Gate execution:
   - run only if `should_try_vector == True`
   - or if user passes `--force-vector`

2. Prepare vectorization input:
   - simplified image
   - quantized image
   - optional silhouette mask
   - optional separated foreground/background version

3. Run VTracer:
   - produce SVG
   - store debug artifact

4. Render SVG back to raster targets
   - use a local Python-renderable approach if adopted in implementation
   - keep this abstraction behind an interface so renderer choice can be swapped later

5. Generate candidate variants:
   - `vector_clean`
   - `vector_silhouette`
   - `vector_reduced_palette`
   - `vector_thickened`

### Acceptance criteria
- vector branch is optional and non-blocking
- failures in vectorization do not break the pipeline
- SVG artifacts are available in debug mode

## Phase 8 — candidate scoring

### Goal
Automatically decide which result is best for each size.

### Tasks
Implement `scoring.py`.

### Score components

#### 1. Silhouette preservation score
Compare low-res candidate silhouette to downsampled reference silhouette.

#### 2. Connected-component sanity
Penalize:
- too many tiny isolated fragments
- excessive fragmentation
- accidental holes
- broken shapes

#### 3. Minimum stroke visibility
Reward candidates where important strokes survive.

#### 4. Edge contrast
Reward clear contour separation.

#### 5. Visual clutter penalty
Penalize high-frequency junk.

#### 6. Center stability
Penalize candidates where the visual center drifts too far.

#### 7. Saliency retention
Measure how much of the importance map survives.

#### 8. Text suppression bonus
If text-likeness was high, do not penalize removal too heavily.

#### 9. Optional family consistency metric
If processing multiple icons in a set later, measure style consistency.

### Score formula
Use weighted sum from config:

```yaml
weights:
  silhouette: 0.25
  saliency_retention: 0.20
  stroke_visibility: 0.15
  edge_contrast: 0.15
  clutter_penalty: 0.10
  component_sanity: 0.10
  center_stability: 0.05
```

### Acceptance criteria
- scorer consistently prefers readable candidates over mathematically faithful but muddy ones
- all score components are debuggable

## Phase 9 — selection, export, and debug artifacts

### Goal
Finalize outputs cleanly.

### Tasks
Implement `render_targets.py`, `ico_export.py`, and `debug_viz.py`.

1. For each size:
   - sort candidates by score
   - save winner
   - optionally save top-N candidates

2. Export:
   - PNG per size
   - optional composite preview sheet
   - optional `.ico`

3. Debug artifacts:
   - cropped source
   - saliency map
   - simplification masks
   - vector SVG if used
   - score table as JSON
   - per-candidate preview strip

4. Manifest file:

```json
{
  "input": "icon.png",
  "analysis": {
    "vector_friendly_score": 0.71,
    "raster_friendly_score": 0.29,
    "complexity_score": 0.42,
    "text_likelihood": 0.67,
    "dominant_region_ratio": 0.58,
    "simplification_level": "medium",
    "should_try_vector": true
  },
  "selected": {
    "32": "candidate_32_vector_clean.png",
    "24": "candidate_24_high_contrast.png",
    "16": "candidate_16_silhouette_priority.png"
  },
  "scores": {}
}
```

### Acceptance criteria
- user can inspect why a candidate won
- reruns with same config are reproducible

## Configuration design

Use YAML config so Codex can keep logic and tuning separate.

Example `default.yaml`:

```yaml
sizes: [32, 24, 16]
padding_ratio: 0.08

preprocess:
  palette_sizes: [24, 16, 12, 8]
  bilateral_filter: true
  crop_transparent_border: true
  recenter: true

analysis:
  enable_text_heuristics: true
  vector_friendly_threshold: 0.62

saliency:
  use_opencv_fine_grained: true
  centrality_weight: 0.20
  dominant_component_weight: 0.25
  edge_weight: 0.25
  text_penalty_weight: 0.15
  tiny_region_penalty_weight: 0.15

simplify:
  generate_levels: ["light", "medium", "aggressive"]
  remove_small_regions_px: [2, 4, 8]
  fill_small_holes_px: [2, 4, 6]
  thicken_strokes: true
  allow_text_drop: true

vector:
  enabled: true
  fallback_on_failure: true

scoring:
  weights:
    silhouette: 0.25
    saliency_retention: 0.20
    stroke_visibility: 0.15
    edge_contrast: 0.15
    clutter_penalty: 0.10
    component_sanity: 0.10
    center_stability: 0.05

export:
  save_ico: true
  save_debug: true
  save_top_n: 3
```

## CLI specification

### Commands

#### Single image
```bash
icon-optimize input.png --out output/
```

#### With ICO
```bash
icon-optimize input.png --out output/ --ico
```

#### Force both paths
```bash
icon-optimize input.png --out output/ --try-both
```

#### Debug mode
```bash
icon-optimize input.png --out output/ --debug
```

#### Batch mode
```bash
icon-optimize-batch input_folder/ --out output_folder/
```

### CLI requirements
- no GUI required
- all processing local
- all failures logged clearly
- vector branch must degrade gracefully if unavailable

## Testing plan

### Unit tests
- transparent crop correctness
- palette reduction stability
- morphology cleanup behavior
- scoring deterministic behavior
- ICO export contains expected sizes

### Regression fixtures
Create a test set with:
- flat logo icon
- emblem icon
- fantasy/game icon with gradients
- icon with text
- icon with glow
- icon with high-detail object
- icon with thin outline

For each fixture:
- save approved 32/24/16 outputs
- compare future pipeline changes against them

### Visual review harness
Generate contact sheets showing:
- source
- saliency
- simplified variants
- top candidates
- chosen winner

## Non-goals for v1

Do **not** attempt in v1:
- deep neural semantic segmentation
- cloud APIs
- external GUI apps
- learned ranking models
- perfect OCR/text detection
- style-consistent set optimization across hundreds of icons
- handling icon sizes above 256 inside Pillow’s ICO writer

## Future extensions after v1

### v2 ideas
- train a lightweight local ranker on your own icon preferences
- add set-level consistency scoring
- add icon-class presets:
  - flat UI icon
  - crest/emblem
  - realistic game icon
  - metallic badge
- add optional manual override masks
- add alternate export profiles:
  - Windows ICO
  - favicons
  - launcher tile packs

### v3 ideas
- learned small-icon “hinting” model trained on pairs:
  - source icon
  - manually preferred 16×16 result

## Recommended Codex execution order

Give Codex these milestones in order:

### Milestone 1
Set up project skeleton, CLI, config loading, PNG and ICO export.

### Milestone 2
Implement preprocessing and debug image export.

### Milestone 3
Implement analysis metrics and routing decision.

### Milestone 4
Implement saliency plus heuristic importance map.

### Milestone 5
Implement simplification engine with multiple strengths.

### Milestone 6
Implement raster candidate generation for 32/24/16.

### Milestone 7
Implement optional VTracer path behind a feature flag.

### Milestone 8
Implement scoring and candidate selection.

### Milestone 9
Add regression test suite and contact sheet generation.

### Milestone 10
Add batch mode and config presets.

## Codex instruction block

```text
Build a local-only Python project called icon_optimizer.

Goal:
Create an offline pipeline that takes a bitmap icon, analyzes and simplifies it, generates multiple candidate low-resolution outputs for 32x32, 24x24, and 16x16, scores them for perceptual readability, selects the best candidate for each size, and optionally exports a Windows .ico.

Constraints:
- No cloud services
- No GUI dependency
- No requirement for Inkscape
- Optional local vectorization branch only if a Python-integrable local tool is available
- Must run fully offline on Windows
- Prefer Python libraries: Pillow, OpenCV, scikit-image, numpy
- Optional VTracer branch behind feature flag and graceful fallback

Architecture requirements:
- Dual-path pipeline: raster-first plus optional vector-assisted branch
- Strong debug outputs at every stage
- YAML config-based tuning
- Deterministic outputs
- Clear CLI entry point
- Tests for preprocessing, scoring, and ICO export

Implementation phases:
1. CLI, config loader, image IO, PNG/ICO export
2. Preprocessing: alpha cleanup, crop, recenter, denoise, palette reduction, morphology cleanup
3. Analysis: complexity metrics, text-likeness heuristics, vector-friendliness score, simplification strength suggestion
4. Saliency: combine OpenCV saliency with icon-specific heuristics into an importance map
5. Simplification: remove low-value detail, normalize strokes, fill/remove tiny holes, create light/medium/aggressive variants
6. Raster path: generate multiple target-size candidates with several resize/postprocess strategies
7. Optional vector path: vectorize only suitable icons, render back to target sizes, generate vector-derived candidates
8. Scoring: silhouette retention, saliency retention, edge contrast, clutter penalty, component sanity, center stability, stroke visibility
9. Selection/export: pick best candidate per size, save artifacts, save manifest JSON, optionally save ICO
10. Tests and regression fixtures

Coding style:
- Use dataclasses for structured intermediate results
- Keep pipeline stages modular
- Add type hints
- Add helpful logging
- Add debug artifact saving for every major stage
- Avoid magic constants: move thresholds and weights into YAML config

Deliverables:
- Working CLI
- Source tree with modular pipeline
- Example config files
- Tests
- README with setup and usage
```

## Bottom line

The strongest v1 is:

- **raster-first**
- **heuristic saliency + simplification**
- **multi-candidate generation**
- **automatic scoring**
- **optional VTracer branch only when justified**

That is the most realistic offline implementation path for your use case.
