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

## Routing Work Items (in order)

- [x] **R5. Power/Ground Special Handling** — placement side landed 2026-10-11 (see
  tests/test_placement_power.py); GND pour side landed 2026-10-11: islands diagnostics
  (`routing/pour.py` + debug endpoint + overlay), auto stitching vias/bridges
  (`routing/stitch.py`, region-aware), pours passable-at-toll with severance steering
  (see tests/test_pour.py, test_stitch.py, test_routing_pour.py)
  - Power rails: wide tracks, dedicated layer channels, daisy-chain/star from connector
  - Ground stitching: connect ground splits with vias at boundaries
  - Decap fanout: short direct routes, THT caps use both layers (free via)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate

## Scaling Notes
- Grid: 0.1mm (configurable), ~100 cells/mm2
- Dense grid operations: numpy arrays, avoid Python loops

## Documentation Updates
- `docs/autorouting_glossary.md`: add routing section with grid/Lee/A*/rip-up terms
- `docs/README.md`: index new routing docs