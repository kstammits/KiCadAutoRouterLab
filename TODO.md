# KiCad AutoRouter - TODO

- [x] Move the Route/Place dialogs from the bottom to the right side as a pull-out panel (done: `ui/index.html` right-side `#side-panel` drawer, 2026-10-08).
- [x] Version number visible + bumped past 0.2 (done: top-bar `#app-version` chip via `/api/version`, now v0.3.0; `pyproject.toml` + `__init__.py` synced, 2026-10-08).

## ⚠️ Session State Notes (2026-10-08, verified)

**Working tree batch** (not yet committed; branch is 4 commits ahead of origin):
per-region placement rewrite + shapely courtyard collision (`placement.py`), routing infra R1–R4/R7, new `src/kicad_autorouter/drc.py`, track rip-up (`io.rip_up_nets`, `board_model.commit_placement`) + `tests/test_track_ripup.py`, UI overhaul phases 1–4, pyproject.toml editable install, shapely in requirements.txt, start_server.sh browser-open.
Plus this session: move-preservation fix (`apply_deltas`/`commit_placement` via `replace`) + `TestMovePreservation`, tube111 3-region asserts, pipeline stages → implemented, right-side `#side-panel` drawer, version 0.3.0 sync, `run-btn` null guard.

**Test status:** 239 passed, 12 skipped — all green (full suite 2026-10-08; `test_server.py` 14 passed after UI changes).
(Fixed 2026-10-05: `accept_proposal` unpacked `identify_power_nets` as 2 values and used list `|` union; now unpacks 3 and converts to sets. `ui/server.py` import is fine — `UI_DIR`/`REPO_ROOT` are defined at module level.)
(Fixed 2026-10-08: `apply_deltas` dropped `pad_type`/`drill_mm`/`layers`/`ghost`/`version`/regions on move; now uses `replace` throughout. `commit_placement` preserves board metadata via `replace(moved_model, tracks=…, vias=…)`.)

### Half-wired features ✅ (wired 2026-10-08 — verified live on tube111)
1. **DRC overlay:** added "Run DRC" button (`POST /api/drc`) + auto-run when the toggle is switched on with an empty/stale cache; result counts shown in `#drc-info`. Verified: 28 violations + 4 unconnected, overlay renders with `drc=1`, absent with `drc=0`.
2. **Layer toggles (F.Cu/B.Cu):** fixed selector bug — `#layer-F.Cu-tracks` never matches (dot parses as class); now uses `[id="…"]` attribute selectors via `applyLayerVisibility()`, re-applied on every SVG inject. Toggles hide/show the layer's tracks+zones groups (vias share one group and stay visible).

## In Progress / Next Session 📋

### Routing Completion (R6, R8)
- [x] **R6. Through-Hole "Free Via" Support** (done 2026-10-10 — `tests/test_routing_tht_free_via.py`, 10 tests)
  - THT pad marks HIGH_COST ring on BOTH layers ✅ (was already; ring radius = pad extent + clearance + track half-width)
  - Free hop: `tht_via_mask` is now the drill-hole disc (`tht_drill_cells`, was single cell) → 0-cost transition in `_a_star`; `routes_to_tracks_vias` suppresses `Via` emission on the net's own holes (existing barrel, per-net `tht_sites`) — TFREE routes F.Cu→B.Cu with `vias_added == 0`, survives `via_cost_mm=1000`, no-THT control drills exactly 1
  - Foreign exclusion: `THT_EXCLUSION` (10000) inner disc in the per-net secondary grid, impassable at `router.SECONDARY_BLOCKED` (1000); soft halos (500/100) still steer-only. Own net excluded → connects + hops free. NFOREIGN detours with ≥1.0mm clearance; `build_via_block_mask` carves drill discs out so builders never disagree (`test_tht_pads_stay_legal` caught an SMD-halo/THT-disc overlap on DCCF)
  - Via price reinterpreted as mm: `CostMap.via_cost_mm` (was cells `via_cost=50` @0.1mm); router scales via `via_cost_cells(res)` (`int()` truncation — identical numbers at tested resolutions); identical vialess routes at 0.5/0.25mm grids
  - Keepout zones wired into `build_occupancy_grid` (were parsed but ignored): `tracks_allowed=False` → BLOCKED on listed layers (+ `_mark_keepout`; vias-allowed/track-forbidden split is follow-up)
  - Power/ground nets: THT pads become automatic stitching points (deferred)
  - Assumptions: mfg tolerance folds into `clearance_mm` (no new knob); secondary blocking is fail-safe with no goal exemption (overlap → "no path", never a short)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate
  - Router fixes landed 2026-10-09 (found while removing demo jitter):
    - `obstacles._mark_zone` passed (col,row) to `_fill_polygon` which takes (row,col) → phantom full-height blocked bars; fixed + orientation regression test
    - Courtyard outlines were BLOCKED, trapping every enclosed SMD pad; now HIGH_COST (placement guides, not copper keepouts) + escapability regression test
    - `router._a_star` never recorded parent pointers → every route collapsed to its final step; fixed + full-chain regression test
  - Scores/widths landed 2026-10-09: `CostMap.heuristic_weight` 200→3 (penalties steer again), graded per-net fanout halo (+500/+100 secondary grid, own-net excluded, steers without trapping), `(net_class trace_width/clearance)` parsing + `RoutingParams.net_widths` override (params → file → heuristic → default), width-aware pad rings + emitted-width corridor reservation, `run_routing` hardcoded 0.25 + hardcoded via sizes fixed, duplicate-via dedupe. `test_drc_routing` DRC gate passes un-xfailed (3/3 nets, only lib/synthetic + single-pad-unconnected violations remain, both ignored).

### Test + routing helper unification (done 2026-10-10)
- [x] `tests/helpers.py`: `make_smd_pad`/`make_tht_pad`/`make_rect_courtyard`/`make_fp` (pads at ±pad_dx, origin- or board-space courtyards)/`plan_segments`/`closest_approach`/`vias_in_path`/`overlap_area` — migrated crossing/tht/pairs/layers/placement/obstacles/astar (behavior-preserving: exact layers/shape/courtyard-coords passed through; suite after each file). New `tests/__init__.py` (package mode) + `tests/conftest.py` (`minimal_model`/`dccf_model`/`grid`/`cost_grid`/`routing_setup`); fixed the one cross-test import (`test_routing_pipeline` → `tests.test_drc_routing`).
- [x] Routing prod dedup (`obstacles.py`, `pipeline.py`): `disc_cells` generator + `pad_halo_radius_cells`/`drill_radius_cells` (one radius spelling; `_mark_pad`'s cells-based variant intentionally left — different input units); `bresenham_cells` iterator + `stamp_square` (line/thick-line/route-corridor trio + arc stamper); `_mark_polygon_on_layers` core behind `_mark_zone`/`_mark_keepout`. Equivalence tests: `TestDiscHelpers`, bresenham-vs-wrappers.
- [x] Net-name parsers → one: `io._net_name_of` + test-local `_net_name_from_node` (args[0]-only, missed indexed form) deleted; all call sites use `board_model._net_name`. Dead code deleted: `io._pad_to_sexpr`, `io._courtyard_to_sexpr` (zero call sites), `obstacles.VIA_COST`. Finders → `_find_footprint_where` core; arcs → `board_model.sample_quadratic_bezier` shared by outline extraction + keepout stamping.
- [ ] Deliberately NOT unified (documented): rotation math (Kabsch-frame divergence is load-bearing per `board_model.py` note — needs characterization tests first); power-keyword lists (semantically divergent: exact vs substring vs different members — needs product decision); `from_dict` (different validation hooks); rip-up predicates (tree vs model levels); angle normalize (SExpr-token vs float contexts); bbox/centroid fallbacks (different contexts).
- Full suite 2026-10-10: 350 passed, 11 skipped.

### Iterative Place/Route Cycle
- [ ] `run_iterative_pipeline(model, params, max_cycles=5)`
  - Place → get affected nets → rip up → route affected → repeat until converged

### Protected Nets Configuration
- [ ] Add "Protected nets" input field in placement panel (comma-separated)
- [ ] Persist protected nets in placement.json
- [ ] Pass protected nets from UI → `/api/placement/accept`
- [ ] Fallback: auto-detect + hardcoded if UI list empty

### Download Validation
- [ ] Downloaded PCB after accept loads in KiCad without DRC errors
- [ ] Integration test: accept → download → kicad-cli drc passes

### Server Test with Real Tracks
- [ ] Add test_accept_proposal_with_tracks using tube111 fixture (has tracks/nets)
- [ ] Verify version increments, proposal cleared, tracks reduced

### Performance Baseline
- [ ] Benchmark rip_up_nets on large boards (5000+ segments)
- [ ] Profile commit_placement on 200+ footprints

### Placement Progress + Benchmark (Stage 1 done 2026-10-09; Stage 2 planned)
- [x] `scripts/benchmark_placement.py`: fixture ladder (minimal/tube111/DCCF) + synthetic 8–64fp boards; per-iter ms, convergence iters, cProfile top, fitted `ms/iter = a*pads^2 + b*fps^2 + c`. Timings captured in `docs/benchmarks/placement_2026-10-09.json`.
  - Findings: shapely courtyard loop dominates (~70% of force time on tube111); vectorized repulsion is the rest. DCCF/tube111 run all 1000 default iters without converging (34–54s worst case). Fit: a=3.3e-4, b=2.2e-3, c=0.94 (max err 4.3ms — real boards deviate with courtyard complexity).
- [x] UI estimate-first: `runPlacement` shows fitted pre-flight estimate + elapsed ticker + scaled client timeout (60s–10min); `/api/footprints` gained `pad_count` for the estimate. No server changes; synchronous flow preserved.
- [x] Courtyard shapely-loop fix (2026-10-09): cached local shapes + numpy rigid poses + bounding-circle broadphase (exact values + 1e-9 margin, so the cull is a provable no-op). New numbers: tube111 46→34ms/iter, DCCF 50→25ms/iter (~2x on real boards, more on sparse layouts), DCCF full run 54→36s. UI estimate coefficients refreshed (a=-3.2e-5, b=3.0e-3, c=1.31).
  - Parity: `tests/test_placement_parity.py` pins 30-iter trajectories to golden (`tests/fixtures/placement_parity_golden.json`) at 1e-9 — BIT-IDENTICAL on tube111/ghost/DCCF. Debugging notes for future refactors: contact forces are stiff 1/d singularities, so even 1ulp input diffs compound to mm-scale over 30 iters (an early version with a too-tight cull bound diverged 5.6mm; the +ri+rj term in the bound was the fix). Two real bugs found by the parity test: (1) padless-courtyard polys dropped by a lazy builder, (2) netless pads must skip pose transform (length-mismatch identity rule). Golden values must only ever be regenerated from independently-trusted physics code.
- [ ] Stage 2 — server job model: `on_progress`/`cancelled` hooks in sim loop, job store + poll/cancel endpoints, progress bar + Cancel in UI (estimate-first was chosen deliberately; chunking like routing is impossible — pads interact every iteration, so repeated small POSTs would restart the sim from scratch).

### Routing fixes (2026-10-10)
- [x] Removed dead `max_via_count` knob (`RoutingParams`, `run_routing` kwarg, UI slider, skipped test): declared + serialized + UI-exposed but never read by any routing logic (`max_via_count=0` still emitted vias; only `via_cost_mm` prices vias). `from_dict` still ignores the stale key so old saved configs load. New `tests/test_routing_crossing.py`: 4 corner resistors on minimal form an X (X1 NW-SE, X2 NE-SW); both nets route with X2 hopping through 2 vias, and X2's B.Cu span is asserted to cross X1's F.Cu copper in plan view. Sensitivity checked: via price 1000 → via-free detour.
- [ ] `max_via_count` was removed, not enforced — if a real cap is wanted later, it needs budget semantics in `SingleNetRouter` (THT free transitions exempt? rip-up rollback?) plus a fast `route_nets`-level test, not the skipped DCCF one.

### Placement physics fixes (2026-10-10, from R11-into-RV2 incident on DCCF)
- [x] Pair-loop ordering: movable footprints now collide with ALL others regardless of file order (`placement.py` — old `j > i` loop never evaluated movable-vs-earlier-immobile pairs, so a lone selected part felt no courtyard force from ~half the board). Movable-movable pairs still evaluated once. Pinned by `tests/test_placement_pairs.py` (immobile-first AND immobile-last layouts).
- [x] Boundary repulsion: near-edge term was `kb*2*(t-dist)/(dist+eps)` with a singularity (~3.5e7 on near-edge parts); now bounded linear falloff `kb*2*(t-dist)/t` (max 2*kb at the edge) via testable `_boundary_edge_push` helper + unit/integration tests in `tests/test_placement_pairs.py`. R11's boundary force went from -3.5e7 to 0.06.
- [x] Fallout handled: parity golden regenerated (trajectories moved — expected for deliberate physics fixes; magnet behavioral suite fully green as justification); `test_c11_quarter_inch_clearance_is_quiet` re-anchored to the rest layout (`max_iterations=0`) after showing the post-motion value oscillates 0→18→2.5→17 across adjacent iteration counts (hot-start pad slosh pins a chaotic landing otherwise). Bonus: the two stale failures in `.pytest_cache` (`test_magnet.py::TestNetMagnet::test_write_outputs`, `test_placement_layers.py::test_cross_layer_shared_net_still_attracts`) pass again — the pair-loop fix repaired the missing-pair physics they depend on. Full suite 2026-10-10: 335 passed, 12 skipped.
- [x] Courtyard-shape debt (fixed 2026-10-10): new `_courtyard_outline_polygon` helper (`shapely.ops.polygonize` + `unary_union`, largest ring wins) shared by `_local_shape` and `_footprint_courtyard_polygon`. DCCF: 87 closed rings, 0 fallbacks (was 21 valid / 66 silent pad-bbox); RV2 bounds now exactly file truth. Consequence: true outlines sit closer than the old bboxes (C11/R11 gap is 1.3mm, inside the 3mm halo), so location-pinning tests were re-baselined (see next item), not left skipped.
- [x] Tripwire re-activation (2026-10-10): parity golden regenerated from current physics (tube111/ghost/dccf all 30 iters; justification gate `test_magnet.py` 12 passed + pairs/layers 9 passed) and `pytestmark.skip` removed — `test_courtyard_opt_parity_trajectories` green at 1e-9. C11 test renamed to `test_c11_close_neighbor_contact_is_bounded`: rest-layout courtyard now measures ~22.7 (legitimate R11 contact at 1.30mm; R14 at exactly 3.00mm halo edge), so the band `5 < courtyard < 50` replaces the old `courtyard < 1.0` silence assert — upper bound still catches the ~3800 distant-RV blow-up by 2 orders, lower bound guards against regressing to silent bbox shapes. Full suite 2026-10-10: 336 passed, 11 skipped (all remaining skips environmental/manual-gate).

### Magnet Heuristic Behaviors (done 2026-10-09)
- `tests/test_magnet.py::TestMagnetHeuristics` — the behavioral correctness bar for placement physics (the golden parity file pins an implementation, not correctness): attraction tightens groups vs own start across 3 layouts, ka=0 control stays loose (layout-scoped to seed 42), final courtyards never overlap at default AND high repulsion gain (assembly-spacing intent), determinism + finiteness.
- Measured dynamics worth knowing: ka=20 overdrive can jam instead of tightening, and high kr can pack parts along edges rather than spreading them — so tests assert separation, never mean-spread ordering.
- No THT part in the harness (deliberate 2026-10-09): placement physics is pad-type-blind, so THT would add lines without testing new behavior; THT plumbing stays covered by `TestMovePreservation`.
- [x] Layer-aware courtyard collision (done 2026-10-09): same-layer pairs collide; cross-layer SMD–SMD exempt; any pair involving THT pads collides (protruding leads). Broadphase gates on `same_layer | tht_i | tht_j`. Attraction/repulsion/rigid stay layer-blind. Tests in `tests/test_placement_layers.py`; golden regenerated with magnet before/after report.

### Pad Model Enhancement ✅ (done 2026-10-08 — verified in tree, guarded by `TestMovePreservation`)
- [x] Add `pad_type`, `drill_mm`, `layers` fields to `Pad` dataclass
- [x] Parse `thru_hole`/`smd` from S-expression in `_pad()`
- [x] Add `is_through_hole` property (pad_type=="thru_hole" + drill>0 + *.Cu in layers)
- [x] Update `obstacles.py` to use `pad.is_through_hole` instead of shape check (`obstacles.py:101`)

---

## Future Enhancements 📋

### Selection/Workflow
- [ ] Range select in table (Shift+click for range)
- [ ] Filter by footprint library name
- [ ] Filter by component value (e.g., "10k", "0.1uF")
- [ ] Selection persistence across accepts/undos
- [ ] "Select by net" dropdown in toolbar

### Visual
- [ ] Highlight selected footprint in SVG on table hover
- [ ] Show force vectors as SVG overlay option
- [ ] Mini-map/overview panel
- [x] Layer toggle (F.Cu / B.Cu / both) — done 2026-10-08 for tracks+zones (vias share one group)

### Routing Integration
- [x] "Route" button in UI (done: Route Selected / Route All in routing panel)
- [x] Show routing progress (done 2026-10-08: loading spinner; upgraded 2026-10-09 to per-net chunked loop — see below)
- [x] DRC violations overlay (done 2026-10-08: Run button + toggle auto-run + svg overlay)
- [ ] Writeback/download with routed tracks

### Chunked Routing UI (done 2026-10-09)
- UI routes ONE NET PER REQUEST (`routeSelectedNets` loop in `ui/index.html`) instead of a single batch POST: per-net progress bar (`#route-progress`), Cancel button (`#route-cancel-btn`, cooperative — stops after the in-flight net, routed nets kept), pre-flight pair-search estimate (`estimateRoutingWork`, `k*(k-1)/2` per net), per-net failure continuation (HTTP 400 aborts the batch — bad params would fail every net; other failures continue), incremental SVG refresh per net.
- DECISION: chunking over a server job model (background thread + poll + cancel) — chunking needs no server threading changes because each one-net call rebuilds grids from the current model, which already holds prior nets' copper. Server job model deferred until per-pair progress proves necessary.
- Equivalence: `tests/test_routing_chunked.py` — chunked sequential == batched (routed/failed sets identical; track totals within ±20% — see known rebuild tradeoff noted in `pipeline.route_nets` step 2 comment).
- KNOWN FOLLOW-UP: width-aware rebuilds — `obstacles._mark_track` marks centerline-only while `_apply_route_to_grid` reserves full corridor; chunked later nets run slightly tighter. Left as-is; belongs with GND-continuity work below.

### Routing Benchmarks (agreed 2026-10-09, not yet implemented)
- [ ] `scripts/benchmark_routing.py`: DCCF ladder (2/3/8-terminal nets + synthetic 20-terminal net), per-stage wall times + cProfile top-20 for one A*; anchors: occupancy ≈0.1s, 3-pad route ≈1s.
- [ ] Trivial cleanup only if profiling confirms free: drop redundant `cost_matrix` in `SingleNetRouter.route()` (byte-identical to `dist_matrix`), hoist `heapq` import.
- [ ] Structural scaling (heuristic-MST → A* on tree edges only) deferred; GND excluded from trace-speed investment (copper pours later).

### GND Continuity / Copper Pours (agreed 2026-10-09, phased)
- Current state: `Zone` parsed + drawn, but routing treats pours as pure `BLOCKED` obstacles (`obstacles._mark_zone`); `route_ground_stitching` only handles ≥2 same-layer zones via centroid routing — real fragmentation (signal tracks carving one pour into islands) is undetected.
- [ ] Phase 1 — fragmentation diagnostics: `routing/pour.py` `gnd_islands()` (connected components of GND copper per layer: zones + pads + tracks on the grid), `GET /api/routing/debug/gnd-islands`, UI overlay. No routing changes.
- [ ] Phase 2 — stitching-via suggestions (preview list, accept per-via/all; no auto track-moving).
- [ ] Phase 3 — auto-stitch + continuity-aware routing (research-grade; settle GND-as-traces vs pours first).

### Performance
- [ ] Virtual scrolling for large footprint lists (500+)
- [ ] Debounced filter input
- [ ] Web Worker for placement simulation

### Persistence
- [ ] Save/load session state (selection, pinned, params)
- [ ] Project file (.kicar_autorouter) with UI state

---

## Notes

### Server Startup
```bash
./start_server.sh [port]  # defaults to 8000, opens browser
```

### Key Files Modified
- `ui/index.html` - Complete UI overhaul; right-side `#side-panel` drawer (2026-10-08)
- `ui/server.py` - New endpoints, accept logic, rip-up integration
- `src/kicad_autorouter/io.py` - `rip_up_nets()` function
- `src/kicad_autorouter/board_model.py` - `commit_placement()` function; move-preservation fix via `replace` (2026-10-08)
- `src/kicad_autorouter/svg_render.py` - Movable footprint styling
- `src/kicad_autorouter/sexpr.py` - Version extraction helpers
- `src/kicad_autorouter/placement.py` - Vectorized force-spring simulation
- `src/kicad_autorouter/routing/` - Grid, obstacles, router, scheduler, output, pipeline
- `src/kicad_autorouter/pipeline.py` - Stage statuses corrected to implemented (2026-10-08)
- `tests/test_magnet.py` - `TestMovePreservation` regression (2026-10-08)
- `tests/test_board_model.py`, `tests/test_placement.py` - tube111 3-region asserts (2026-10-08)
- `pyproject.toml`, `src/kicad_autorouter/__init__.py` - version 0.3.0 sync (2026-10-08)
- 2026-10-09 (jitter/stub removal + step/run UX): `placement.py` (drop `demo_jitter_mm`/`stub`/`_jitter_for`, 1e-9 delta filter), `scripts/run_autoroute.py` (drop `--stub`/`--demo-jitter-mm`, step default 5), `ui/server.py` (replace-based iter override, reject empty accepts), `ui/index.html` (drop Physics/Preview/stub UI, step default 5, Run Selected/All), `placement.json` (drop jitter/stub keys), `routing/obstacles.py` (zone (row,col) fix, courtyard HIGH_COST), `routing/router.py` (A* parent pointers), `tests/test_drc_routing.py` (board-space courtyards, strict-xfail DRC gate)

### Protected Nets Logic
Auto-detected via `identify_power_nets()` + hardcoded fallbacks:
```python
protected = power_nets | ground_nets | {"GND", "GND_PWR", "VCC", "VDD", "VSS", "GROUND"}
```

### Accept Flow
1. Push undo state (skipped when the proposal is empty — server rejects it with 400)
2. Compute affected nets from moved footprints' pads
3. `commit_placement(model, deltas, protected)` → BoardModel with pruned tracks
4. `rip_up_nets(pcb_tree, affected, protected)` → S-expression without segments/vias
5. `nudge_footprint_by_uuid()` for each moved footprint
6. Clear proposal, increment version

### Placement Run Flow (2026-10-09)
- Demo jitter + stub mode removed entirely (they let net-free boards drift forever past Edge.Cuts). Physics is the only mode; net-free boards correctly propose no movement.
- UI: Step (default 5 iters) for previews, Run (saved max_iterations) to convergence; Accept stays disabled on empty proposals with a "no movement" message.
- Sub-mm float noise filtered (`1e-9`) so converged boards propose `{}`.
