# KiCad AutoRouter - TODO

## Future Enhancements 📋

### Visual
- [ ] Highlight selected footprint in SVG on table hover

### GND Continuity / Copper Pours (agreed 2026-10-09, phased)
- Current state: `Zone` parsed + drawn, but routing treats pours as pure `BLOCKED` obstacles (`obstacles._mark_zone`); `route_ground_stitching` only handles ≥2 same-layer zones via centroid routing — real fragmentation (signal tracks carving one pour into islands) is undetected.
- [ ] Phase 1 — fragmentation diagnostics: `routing/pour.py` `gnd_islands()` (connected components of GND copper per layer: zones + pads + tracks on the grid), `GET /api/routing/debug/gnd-islands`, UI overlay. No routing changes.
- [ ] Phase 2 — stitching-via suggestions (preview list, accept per-via/all; no auto track-moving).
- [ ] Phase 3 — auto-stitch + continuity-aware routing (research-grade; settle GND-as-traces vs pours first).


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
