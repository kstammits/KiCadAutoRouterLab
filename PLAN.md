# PLAN — Routing Stage (2026-10-03)

**Context**: Placement pass works (spring migration, per-UUID writeback, SVG preview). Next: **additive two-layer routing** (F.Cu + B.Cu) with per-net-class width/clearance. Tracks are additive only — we never delete existing tracks, only add new ones for unconnected pad pairs.

**Constraints (2026-10-03)**:
- Grid: 0.1mm resolution (configurable)
- Via cost: 5mm wire equivalent, max ~100 vias total
- Layers: F.Cu + B.Cu (both signals + power/ground)
- Track widths: per-net (power wider, signal standard)
- Vias: <100 total, cost=5mm equivalent
- **Rip-up during placement**: when parts move, their connected nets get ripped up, parts move freely, then re-routed
- **Through-hole as free vias**: THT pads connect both layers at zero via cost
- Two power rails (V+, V-) + broken ground planes on both layers
- Audio boards: sensitive, need generous clearance
- No pcbnew for routing (only for offline test parity); pure Python Lee/A* on grid
- **Pre-routed test board**: tube111.kicad_pcb (105 footprints, 487 tracks, 93 vias, 6 GND zones, 73 nets)

## Routing Work Items (in order)

- [ ] **R5. Power/Ground Special Handling** — deferred per user request
  - Power rails: wide tracks, dedicated layer channels, daisy-chain/star from connector
  - Ground stitching: connect ground splits with vias at boundaries
  - Decap fanout: short direct routes, THT caps use both layers (free via)

- [ ] **R6. Through-Hole "Free Via" Support** (partial)
  - THT pad marks cell blocked on BOTH layers ✅
  - Layer transition at THT pad = 0 via cost (pending: tht_via_mask in router)
  - Power/ground nets: THT pads become automatic stitching points (deferred)

- [x] **R7. Output & Writeback**
  - `output.py`: routes → KiCad track/via S-expressions (collinear merge, per-class widths)
  - `io.py` extensions: track/via writer → insert into PCB S-expression tree
  - Pipeline entry: parse → place → route → writeback → optional `kicad-cli drc` (stubbed in `pipeline.py`)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate

## Scaling Notes
- Grid: 0.1mm (configurable), ~100 cells/mm
- Dense grid operations: numpy arrays, avoid Python loops
- Lee BFS: deque + int8 grid, bit-packed if needed
- Multi-net: O(nets × grid_cells) per pass; rip-up adds constant factor
- Barnes–Hut for repulsion only needed at n ≳ 3–5k (current n≈100)

## Documentation Updates
- `docs/autorouting_glossary.md`: add routing section with grid/Lee/A*/rip-up terms
- `docs/README.md`: index new routing docs