# PLAN — Routing Stage (2026-10-03)

**Constraints (2026-10-03)**:
- Grid: 0.1mm resolution (configurable)
- Via cost: 5mm wire equivalent, max ~100 vias total
- Layers: F.Cu + B.Cu (both signals + power/ground)
- Track widths: per-net (power wider, signal standard)
- **Rip-up during placement**: when parts move, their connected nets get ripped up, parts move freely, then re-routed
- **Through-hole as free vias**: THT pads connect both layers at zero via cost
- Two power rails (V+, V-) + broken ground planes on both layers
- Audio boards: sensitive, need generous clearance
- No pcbnew for routing (only for offline test parity); pure Python Lee/A* on grid
- **Pre-routed test board**: tube111.kicad_pcb (105 footprints, 487 tracks, 93 vias, 6 GND zones, 73 nets)

## Completed Routing Work Items

- [x] **R5. Power/Ground Special Handling** — placement side landed 2026-10-11 (see
  tests/test_placement_power.py); GND pour side landed 2026-10-11: islands diagnostics
  (`routing/pour.py` + debug endpoint + overlay), auto stitching vias/bridges
  (`routing/stitch.py`, region-aware), pours passable-at-toll with severance steering
  (see tests/test_pour.py, test_stitch.py, test_routing_pour.py)
  - Power rails: wide tracks, dedicated layer channels, daisy-chain/star from connector
  - Ground stitching: connect ground splits with vias at boundaries
  - Decap fanout: short direct routes, THT caps use both layers (free via)

- [x] **Smoother angle granularity** — 2026-10-14: configurable `angle_step_rad` parameter
  (default 10° = π/18) replacing hardcoded 45°; greedy pass prefers 10° multiples among
  equally-furthest nodes, repair pass scores Steiner candidates at 10° increments; 36
  angle options instead of 8 for shallower, more gradual routes

- [x] **Routing params persistence** — 2026-10-14: routing parameters now auto-save to
  `placement.json` under "routing" key when routing starts, matching placement behavior

- [x] **Analytical SAT courtyard collision** — 2026-10-14: rectangle classification +
  circle-rectangle SAT with test suite (test_analytical_contact.py); replaces shapely
  for courtyard contact detection

- [x] **Placement performance Phase 1-2** — 2026-10-13: uniform grid spatial index
  (O(n²)→O(n)) + polygon cache between iterations

## Routing Work Items (in order)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate

- [ ] **R9. Smoother post-pass** — Full curve/spline interpolation after stick phase
  (B-spline or clothoid); convert grid tracks to smooth curves while preserving
  clearance guarantees

- [ ] **R10. Push-and-shove cleanup** — Move already-routed nets to make room for
  later nets; iterative improvement with finite violation costs

- [ ] **R11. Net ordering by criticality** — Decap first, then power, then GND, then
  signal; within class by fanout/pad count

## Scaling Notes
- Grid: 0.1mm (configurable), ~100 cells/mm2
- Dense grid operations: numpy arrays, avoid Python loops

## Documentation Updates
- `docs/autorouting_glossary.md`: add routing section with grid/Lee/A*/rip-up terms
- `docs/README.md`: index new routing docs