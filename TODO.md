# KiCad AutoRouter - TODO

## Future Enhancements 📋

### Visual
- [ ] Highlight selected footprint in SVG on table hover

### Placement Performance Optimization (2026-10-13)
- [x] Phase 1: Uniform grid spatial index for courtyard broadphase (O(n²) → O(n))
- [x] Phase 2: Polygon cache between iterations (avoid rebuild when displacement < threshold)
- [x] Phase 3: Test suite for analytical contact (SAT for rectangles/circles)
- [ ] Phase 3: Implement analytical contact for rectangles/circles (replace shapely for 80% of pairs)
- [ ] Phase 4: Force-based early termination (stop when max |F| < threshold)
- [ ] Phase 5: Parallel region execution (ProcessPoolExecutor for multi-region boards)
- [ ] Phase 6: Numba JIT on hot inner loops (_spread, _rigid_pose_vertices, etc.)

### GND Continuity / Copper Pours (agreed 2026-10-09, phased; landed 2026-10-11)
- Phase 0 — DONE: `Zone` carries fill rules (`clearance`, `min_thickness`, `thermal_gap/bridge`, `island_removal_mode`, `priority`); singular `(layer …)` zones parse (previously dropped, silently disabling stitch pairing).
- Phase 1 — DONE: `routing/pour.py` (`pour_copper_mask`, `pour_islands`, `gnd_islands`), `GET /api/routing/debug/gnd-islands`, UI bbox overlay (red dashed = dead). Validated vs KiCad's own `filled_polygon` (tube111 F: 10 vs 13, B: 26 vs 15).
- Phase 2 — DONE (automatic, per 2026-10-11 decision): `routing/stitch.py` `plan_stitches()` places GND-GND vias on inter-layer overlap + bridge tracks where no overlap exists; region-aware (never joins panels); runs at end of `route_nets`/`run_routing`. Supersedes centroid `route_ground_stitching` (deleted).
- Phase 3 — DONE (soft cost, per 2026-10-11 decision): pours are passable-at-toll (`POUR=35` + `CostMap.pour_penalty=10`, was hard `BLOCKED` — which had also made pads inside pours unroutable); `severance_cost()` secondary steers tracks off narrow pour corridors (clipped so it steers, never blocks).


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
