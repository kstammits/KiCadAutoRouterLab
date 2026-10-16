# KiCad AutoRouter - TODO

## Future Enhancements 

### Visual
- [ ] Highlight selected footprint in SVG on table hover

### Placement Performance Optimization (2026-10-13)
- [x] Phase 1: Uniform grid spatial index for courtyard broadphase (O(n²) → O(n))
- [x] Phase 2: Polygon cache between iterations (avoid rebuild when displacement < threshold)
- [x] Phase 3: Test suite for analytical contact (SAT for rectangles/circles) — tests/test_analytical_contact.py
- [x] Phase 3: Analytical SAT courtyard collision (rectangle classification + circle-rectangle SAT) — 2026-10-14
- [x] Phase 3: Analytical contact is the primary path (approx-rect classification; zero `intersection.area` calls left in the contact path — depth via SAT/projection) — 2026-10-15
- [ ] Phase 4: Force-based early termination (stop when max |F| < threshold)
- [ ] Phase 5: Parallel region execution (ProcessPoolExecutor for multi-region boards)
- [ ] Phase 6: Numba JIT on hot inner loops (_spread, _rigid_pose_vertices, etc.)

### Contact Law Unification (landed 2026-10-15)
- DONE: one linear wall law `F = k·(1−s/h)` in signed separation (gap +/penetration −), C0+C1 continuous at contact. `wall_area_ref_m2` deleted everywhere (dataclass, validation, `server.py`, `calibrate_si.py`, norm docs, primer, glossary, UI text). Coulomb pad repulsion untouched.
- DONE: inverse-push fix — per-pair min-s winner-take-all (no area summation, no pin size-caps). C14/RV3 escape axis corrected (2.84 → 0.075mm²); R13/RV1 hold direction verified correct by isolated-pair probes.
- DONE: DCCF regression tests — 1-mobile-vs-many-locked synthetic cases (ring, linear wedge, attracted-to-pad) + fixture tests (R11, 5-resistor group, C14, R13) in `tests/test_placement_pairs.py`. Suite: 329 passed, 1 skipped (pre-existing fixture-gated skip).
- DONE: UI advanced-slider SI fix — `vmax`/`dt_s`/`friction` defs were display-units-as-SI (timestep showed 100–5000ms and every run failed validation); ranges corrected + `loadParams` clamping.
- Follow-up (documented residual, NOT contact): R13/RV1 stable squeeze wedge (southern halo field + net anchor across RV1 exceed the edge hold at every exit point; equilibrium is stiffness-independent). Needs anchor-side or placement-level work, not wall-law work.
- Follow-up: halo reach `h` is the only remaining distance lever (shorter `h` drops distant squeezers); global change, needs recalibration — do not touch without a sweep.
- [ ] Wall-law follow-through (testing/docs/figures): the unified law landed,
  but still needs regression coverage of the min-s winner-take-all path beyond
  the C14/RV3 probes, and the glossary wall entry completion
  (see Documentation below).
  (Progress: `docs/figures/` regenerated from current code + committed —
  the old PNGs differed from fresh regen across the full canvas on all 7
  figures, i.e. genuinely stale, not render noise. PNGs now RGB-optimized
  via `_save_optimized` in `plot_force_laws.py`.)

### GND Continuity / Copper Pours (agreed 2026-10-09, phased; landed 2026-10-11)
- Phase 0 — DONE: `Zone` carries fill rules (`clearance`, `min_thickness`, `thermal_gap/bridge`, `island_removal_mode`, `priority`); singular `(layer …)` zones parse (previously dropped, silently disabling stitch pairing).
- Phase 1 — DONE: `routing/pour.py` (`pour_copper_mask`, `pour_islands`, `gnd_islands`), `GET /api/routing/debug/gnd-islands`, UI bbox overlay (red dashed = dead). Validated vs KiCad's own `filled_polygon` (tube111 F: 10 vs 13, B: 26 vs 15).
- Phase 2 — DONE (automatic, per 2026-10-11 decision): `routing/stitch.py` `plan_stitches()` places GND-GND vias on inter-layer overlap + bridge tracks where no overlap exists; region-aware (never joins panels); runs at end of `route_nets`/`run_routing`. Supersedes centroid `route_ground_stitching` (deleted).
- Phase 3 — DONE (soft cost, per 2026-10-11 decision): pours are passable-at-toll (`POUR=35` + `CostMap.pour_penalty=10`, was hard `BLOCKED` — which had also made pads inside pours unroutable); `severance_cost()` secondary steers tracks off narrow pour corridors (clipped so it steers, never blocks).

### Routing Smoothing & Post-Pass
- Roadmap: PLAN.md R9 (smoother post-pass + `angle_step_rad` test suite).

### Routing
- Roadmap: PLAN.md R10 (push-and-shove), R11 (net ordering by criticality).
- [ ] Layer assignment heuristic (prefer F.Cu for signals, B.Cu for return paths)
- [ ] VIN-on-DCCF bug (reported 2026-10-15, NOT fixed): VIN tracks across
  same-footprint different-net pads (Q1 THT row: VIN pad 1 vs GND/−5V/+5V pads
  2/4/5/6; Q2 tight SMD neighborhood). Suspects: foreign-THT marking in
  `obstacles._mark_pad`, `unconnected-*` net handling, own-cell secondary=500
  toll on Q2 terminal pushing the path off-start, 0.1mm inter-pad gaps.
  Repro: route VIN on DCCF, dump corridor cells around Q1 pads 1–2.
  Details: `docs/benchmark_notes_2026-10-15.md` §2.
- [ ] DCCF fixture: single GND zone (x ≤ 169.6) covers panel 1 only; panel 2
  (x 175.5–275.5, all VIN parts) has NO pour — add the zone in pcbnew.
  Until then stitch burns ~17s repairing an incomplete board (0 overlap vias,
  53/104 islands via bridges). Details: notes §3.
- [ ] Heuristic-weight experiment (NOT a default change): sweep default 3.0
  vs 1.0 over tube111 signal nets + DCCF sample; gate on routing regression
  green + path-cost parity. Bench motivation: w=1 matches w=3 cost with ~7x
  fewer expansions on DCCF VIN control. No UI slider exists yet (hidden
  `RoutingParams` field); proposed tooltip text in notes §1.

### Benchmarks & Measured Optimizations (2026-10-15 full-scale runs)
- Details + explanations: `docs/benchmark_notes_2026-10-15.md`. JSON:
  `docs/benchmarks/*_2026-10-15.json` (new: `stitch_pour_*`).
- [x] FIRST: `pour_copper_mask` raster churn — DONE 2026-10-15 via the
  cheaper half of the fix: `_paint_disc`/`_paint_pad`/`_paint_track` now route
  through the existing numba `stamp_discs` (`obstacles.py:360-423`) instead of
  per-cell Python `disc_cells` loops (same cells; twin guarantees it).
  Measured: tube111 pour 4.78→1.82s (2.6x), DCCF 5.63→4.43s; stitch knock-on
  tube111 2.55→1.48s. Island/repair parity verified identical. The deeper
  per-track alloc+AND accumulation is still open (see below) but no longer
  the bottleneck.
- [ ] FOLLOW-UP: `pour_copper_mask` per-track `cut = zeros(H,W)` alloc+AND per
  zone — accumulate foreign cuts into one mask per zone. Smaller win now
  that painting is compiled; revisit if pour profile regresses.
- [ ] SECOND: `plan_stitches` loop-invariant hoists — `binary_erosion(copper)`
  out of the group loop, windowed `distance_transform_edt(~jmask)` on group
  bbox + `max_bridge_mm`, incremental `joined_mask` cache (kills O(groups²)
  full-grid ORs). DCCF stitch 17s → low single digits expected.
- [x] Placement `is_empty` per-footprint bool cache — DONE 2026-10-15:
  `body_empty`/`pins_empty` cached per (re)build in `poly_cache`, pair loop
  reads bools (affine poses can't change emptiness). Measured: tube111
  35.8→24.1 ms/iter (1.5x), DCCF 55.7→19.4 ms/iter (2.9x); converged runs
  25.4→15.1s / 0.91→0.44s. Placement family green (52 + 56 + 79 tests).
  GUI estimate coefficient retuned 0.006→0.0045, floor 0.1→0.15. See notes §7.
- [ ] Routing time estimator: move GUI `2 + 1.2·n` to a per-net pad-count
  model (`est.pairs` already computed) + failed-net allowance; 1.2s is the
  small-net marginal, DCCF reality is heavy-tailed (VIN ~2.2s/net, failed
  searches to the 500k cap ~3s+, stitch +17s). See notes §8.

### Documentation (SI program support)
- [ ] Simulation glossary: extend `docs/autorouting_glossary.md` with an
  SI-dynamics section (wall/halo/MTV/aggregation/Kabsch/clamp/deadband/
  guard/tightening/broadphase/decap-power-GND rules), each term with a
  pointer to the `placement.py` symbol or `si_force_laws.md` section.
  (Progress 2026-10-15: wall entry rewritten for the unified law; full
  section still open.)
- [ ] Test suite organization: new `docs/tests.md` (per-file intent table,
  markers, pytest instructions) + `scripts/pytest_report.py` wrapping
  `pytest --html`; reports are gitignored
  (`docs/benchmarks/test_report_*.html`), regenerate on demand.
  Suite is green; the wrapper + `docs/tests.md` are what's missing.
  (Progress 2026-10-15: `pytest-html` added to dev deps + installed in `.venv`
  so `regen_pytest_report.sh --html` works; `pytest_results.qmd` cells made
  portable (repo-root walk-up, `QUARTO_PYTHON`/`sys.executable`, `--color=no`).
  `docs/tests.md` + `pytest_report.py` wrapper still open.)
- [ ] QMD pipeline repair — DONE 2026-10-15: book render was broken (dangling
  `include-files.lua` filter — removed, nothing used includes) and is green
  again (`docs/_output/html/`, all pytest cells return 0). `freeze: auto`
  (+ gitignored `_freeze/`) so re-renders skip unchanged cells; fixed
  `index.qmd` fence; venv-aware `regen_pytest_report.sh`.
- [ ] Physics primer (GATED behind settling the simulation method — do not
  start before merge): new `docs/physics_primer.md` — formulas + why +
  units table + parameter-response graphs via `scripts/plot_force_laws.py`
  (matplotlib, dev dependency), figures embedded in the primer.
  (Progress 2026-10-15: `docs/physics_primer.qmd` wall section/figure/table
  rewritten for the unified law; §5 Boundary filled from the normative law;
  §6+ deferred-sections roadmap added (ghost/damping/integrator/mass stay
  gated); all cited defaults verified against `PlacementParams`.)


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
