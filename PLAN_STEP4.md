# Step 4 Plan — Placement & routing hot-path optimization

**Goal:** Reduce per-iteration placement cost and routing A*/smoothing cost by cutting shapely/GEOS call frequency and adding spatial indexing. No algorithm behavior change.

**Prerequisite:** Step 3 complete (god modules split, suite green).

**Out of scope:** changing placement force laws, routing A* search semantics, adding Cython build steps, changing public dataclass shapes.

---

## Motivation (measured)

cProfile on tube111 placement (20 iters):
- `run_placement` 1.63s cumulative / `region.py:_run_region_simulation` 1.44s / `force.py:_compute_total_force` 1.12s (66 calls)
- `shapely/decorators.py:182(wrapper)` — 45,931 calls, 0.249s cumulative, 0.021s own. **Not numba**, not our code: it's shapely's `deprecate_positional` wrapper around GEOS calls (`intersection`, `difference`, `buffer`, `union`, `offset_curve`).
- `contact.py:_classify_shape` 0.237s (1,992 calls), `_classify_rectangle` 0.199s
- shapely `buffer` ~0.28s across ~11,000 calls

The wrapper overhead itself is ~1.3% of runtime; the underlying GEOS calls dominate. Optimization = *fewer* shapely calls, not faster per-call execution.

## Cython assessment — NOT recommended

- Numba `@njit(cache=True)` already compiles the array-heavy numeric kernels (`routing/obstacles.py:_stamp_discs_nb`, `routing/smooth.py:_footprint_clear_nb`, `_blocked_footprint_hit_nb`, `_line_walkable_nb`).
- Shapely is GEOS (C/C++); Cython can't speed up a single GEOS call.
- Placement's biggest pure-Python kernel (`_compute_total_force`) is irregular cause-dispatch code that doesn't vectorize or Cythonize cleanly without a rewrite.
- Verdict: skip Cython. Higher ROI is reducing shapely call counts.

---

## 1. Placement: cache shapely geometries across iterations

### 1a. `placement/contact.py` — cache footprint courtyard polygons
- `_classify_shape` / `pad_copper_polygon` rebuild shapely polygons per contact check.
- Add a per-run `Polygon` cache keyed by `(footprint_uuid, pose_quantized)` so a footprint whose pose hasn't changed beyond the rebuild threshold reuses the cached polygon.
- Reuse the existing `cache_rebuild_threshold_mm` / `cache_rebuild_threshold_rad` pattern from `region.py` (20µm / 0.02°).
- Target: cut `_classify_shape` + `pad_copper_polygon` calls (~4,000–5,000 per run) by reusing cached polygons for stationary footprints.

### 1b. `placement/legality.py` — spatial index for pairwise overlap checks
- `_check_pairwise_overlaps` (post-loop legality) does O(n²) shapely `intersection` over all footprint pairs.
- Build an R-tree spatial index (`shapely.STRtree`) of courtyard polygons once per legality check; query only overlapping candidate pairs.
- Target: reduce O(n²) shapely calls to O(overlapping pairs) — significant on tube111 (105 footprints).

---

## 2. Placement: reduce redundant force re-evaluations

### 2a. `placement/force.py:_compute_total_force`
- Confirm the 66-call-per-run count isn't hiding redundant recomputation of static components (board wall, ghost anchors) that don't change per iteration.
- If static components are recomputed each iteration, hoist them outside the per-iteration loop or cache them with invalidation on pose change.

---

## 3. Routing: confirm numba JIT warm-up is adequate

### 3a. `routing/obstacles.py` / `routing/smooth.py`
- Verify `@njit(cache=True)` functions (`_stamp_discs_nb`, `_footprint_clear_nb`, `_blocked_footprint_hit_nb`, `_line_walkable_nb`) hit the disk cache on second run (no recompilation penalty).
- Benchmark `benchmark_routing.py` micro-stage `via_ok_mask_s` before/after to confirm no JIT regression post-refactor.

---

## Verification

- [ ] `tests/test_placement.py`, `tests/test_placement_forces.py`, `tests/test_placement_settle.py`, `tests/test_placement_parity` (golden) pass unchanged.
- [ ] `tests/test_routing_search.py`, `tests/test_routing_astar.py`, `tests/test_routing_obstacles.py`, `tests/test_routing_smooth.py` pass unchanged.
- [ ] `.venv/bin/python -m pytest tests/ -q -m "not slow"` green.
- [ ] `scripts/benchmark_placement.py` shows reduced `ms_per_iter` on tube111 (baseline 35–36 ms/iter).
- [ ] `scripts/benchmark_routing.py --quick` shows no `astar_s` / `smooth_s` regression vs `docs/benchmarks/routing_2026-11-03.json`.
- [ ] No public dataclass/field/endpoint shapes changed.
- [ ] Commit message per repo style. Do not commit unless asked.

---

## Files to touch

- `src/kicad_autorouter/placement/contact.py` — polygon caching
- `src/kicad_autorouter/placement/legality.py` — STRtree spatial index
- `src/kicad_autorouter/placement/force.py` — static-component hoisting (optional)
- `scripts/benchmark_placement.py` / `scripts/benchmark_routing.py` — already exist; reuse for before/after comparison
- `docs/benchmarks/placement_*.json` / `docs/benchmarks/routing_*.json` — updated by benchmark runs (untracked scratch unless committed)

---

## Definition of done

- [ ] Placement `ms_per_iter` on tube111 measurably lower than baseline (35–36 ms/iter).
- [ ] Routing benchmarks within noise of pre-refactor baseline.
- [ ] Full fast suite green.
- [ ] Commit message per repo style. Do not commit unless asked.
