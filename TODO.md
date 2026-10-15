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

### GND Continuity / Copper Pours (agreed 2026-10-09, phased; landed 2026-10-11)
- Phase 0 — DONE: `Zone` carries fill rules (`clearance`, `min_thickness`, `thermal_gap/bridge`, `island_removal_mode`, `priority`); singular `(layer …)` zones parse (previously dropped, silently disabling stitch pairing).
- Phase 1 — DONE: `routing/pour.py` (`pour_copper_mask`, `pour_islands`, `gnd_islands`), `GET /api/routing/debug/gnd-islands`, UI bbox overlay (red dashed = dead). Validated vs KiCad's own `filled_polygon` (tube111 F: 10 vs 13, B: 26 vs 15).
- Phase 2 — DONE (automatic, per 2026-10-11 decision): `routing/stitch.py` `plan_stitches()` places GND-GND vias on inter-layer overlap + bridge tracks where no overlap exists; region-aware (never joins panels); runs at end of `route_nets`/`run_routing`. Supersedes centroid `route_ground_stitching` (deleted).
- Phase 3 — DONE (soft cost, per 2026-10-11 decision): pours are passable-at-toll (`POUR=35` + `CostMap.pour_penalty=10`, was hard `BLOCKED` — which had also made pads inside pours unroutable); `severance_cost()` secondary steers tracks off narrow pour corridors (clipped so it steers, never blocks).

### Routing Smoothing & Post-Pass
- [ ] Full curve/spline post-pass after stick phase (B-spline or clothoid interpolation)
- [ ] Test suite for smoothing with angle_step_rad parameter

### Routing
- [ ] Push-and-shove cleanup pass (move already-routed nets to make room)
- [ ] Net ordering by criticality (signal timing, decap first, then power, then GND, then signal)
- [ ] Layer assignment heuristic (prefer F.Cu for signals, B.Cu for return paths)

### Documentation (SI program support)
- [ ] Simulation glossary: extend `docs/autorouting_glossary.md` with an
  SI-dynamics section (wall/halo/MTV/aggregation/Kabsch/clamp/deadband/
  guard/tightening/broadphase/decap-power-GND rules), each term with a
  pointer to the `placement.py` symbol or `si_force_laws.md` section.
  (Progress 2026-10-15: wall entry rewritten for the unified law; full
  section still open.)
- [ ] Test suite organization: new `docs/tests.md` (per-file intent table,
  markers, pytest instructions) + `scripts/pytest_report.py` wrapping
  `pytest --html` (add `pytest-html` to dev deps); reports are gitignored
  (`docs/benchmarks/test_report_*.html`), regenerate on demand.
  Gated on suite-green.
- [ ] Physics primer (GATED behind settling the simulation method — do not
  start before merge): new `docs/physics_primer.md` — formulas + why +
  units table + parameter-response graphs via `scripts/plot_force_laws.py`
  (matplotlib, dev dependency), figures embedded in the primer.
  (Progress 2026-10-15: `docs/physics_primer.qmd` wall section/figure/table
  rewritten for the unified law; freeze cache regenerates on next render.)


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
