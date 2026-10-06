# PLAN — Vectorized force-spring placement (hundreds of components)

Status: planned 2026-09-30. Supersedes the "nudge" baby step; feeds `placement.py` in TODO.md.

## Context / findings

Docs review (`docs/`):
- Covered well: KiCad 10 manuals (exact version match), routing taxonomy + complexity, maze/Lee, force-directed model incl. Barnes–Hut/FADE note.
- Stale: `docs/autorouting_glossary.md:110` — "fine at our scale (tens of footprints)" no longer holds; needs vectorization strategy + complexity budget.
- Missing local sources: Barnes–Hut article (only mentioned inside FDGD doc); A* (cited in glossary, no source); Fruchterman–Reingold / Kamada–Kawai parameter schedules only cited-in-references.

Performance analysis:
- Naive all-pairs repulsion: O(n²)/iteration, O(n³) total. n=500 ≈ 125k pairs/iter.
- Pure Python ≈ 30–60 ms/iter (tens of seconds per run). numpy broadcasting over (n,n) distance matrix ≈ 1–2 ms/iter → sub-second total. Dense n² is plenty at "a few hundred"; Barnes–Hut only matters past ~3–5k nodes.
- Attraction along net edges: sparse O(E), vectorized with edge index arrays. Locked footprints = boolean mask (zero force).

Code findings:
- `placement.py` does not exist yet — force-spring is glossary + TODO only; current "nudge" is a constant dx/dy offset (`io.nudge_footprint`).
- Bug: `scripts/run_autoroute.py:57` calls `adapter.nudge_unlocked_footprints`, which is NOT defined in `pcbnew_adapter.py` → AttributeError at runtime.
- `board_model.py` does not parse the footprint `(locked ...)` token — needed for fixed nodes. Verified against KiCad source mirror that `.kicad_pcb` serializes it (writer ~line 1474, parser line 6142 of `pcb_io_kicad_sexpr*.cpp`).

Environment:
- `.venv`: Python 3.11 + numpy 2.3.5 (+ networkx per requirements.txt).
- KiCad bundled Python 3.9: NO numpy, NO networkx (verified). Simulation must run in .venv.
- `tests/fixtures/DCCF.sved.kicad_pcb` ≈ 97 footprints — mid-scale benchmark fixture.

## Decisions (2026-09-30)

1. **numpy host:** compute placement in `.venv` on the pure-Python parse; hand off per-footprint `(dx_mm, dy_mm)` deltas to writeback addressed by footprint UUID (`io.nudge_footprint_by_uuid`). KiCad app bundle stays untouched; `pcbnew_adapter.py` reserved for future routing-accurate semantics.
2. **Constraints v1:** soft forces only + hard clamp inside board-outline bounding box. No courtyard-overlap collision resolution or keepout exclusion yet (DRC validates after writeback).

## Decisions (2026-10-01)

3. **UUID identity:** `Footprint.uuid` is parsed from each footprint's `(uuid ...)` token and is the stable key for writeback and display — refs may be duplicated or missing (DCCF: H6 ×5, REF**/G*** ×2, 6 empty refs; all 97 UUIDs unique). The viewer labels unnamed parts with a short UUID.

## Work items (in order)

- [x] **1. `board_model.py`: lock state.** Add `locked: bool = False` to `Footprint`; parse top-level `(locked ...)` token inside each footprint node. Test with a fixture containing one locked part.
- [x] **2. New `src/kicad_autorouter/placement.py` (numpy, .venv).**
  - Input: `BoardModel`. Nodes = all footprints; edges = footprint pairs sharing ≥1 net (one spring per pair, deduped).
  - State: `(n, 2)` float64 positions; boolean locked mask.
  - Per iteration: repulsion via broadcasting distance matrix with softening ε (Coulomb `k_r/d²` along unit vectors); attraction Hooke toward ideal length k = √(area/n) via edge index arrays; decaying step size (Fruchterman–Reingold temperature schedule); zero locked rows; clamp to board-outline bbox.
  - Convergence: max displacement < ε_mm or iteration cap (~1000). Return per-footprint `(dx_mm, dy_mm)` deltas keyed by UUID (node-aligned) + diagnostics (iterations, final energy/displacement).
  - Docstring complexity budget: n=500 → sub-second; Barnes–Hut deferred until n ≳ 3–5k.
- [x] **3. Writeback path (.venv end-to-end).** Apply deltas via `io.nudge_footprint_by_uuid` per footprint → save → `validate.py` (`kicad-cli pcb drc`) as acceptance check. (Done in `scripts/run_autoroute.py`: per-UUID writeback + optional `--validate` DRC.)
- [x] **4. Fix latent bug.** Rewire `scripts/run_autoroute.py:57` (missing `adapter.nudge_unlocked_footprints`) to run placement in .venv and apply deltas via `io`; headless entry point must actually work. (Done 2026-10-01: script replaced with a pure-Python parse → place → writeback pass under the project .venv.)
- [x] **5. Tests + benchmark.** Synthetic 100/500-footprint boards: convergence, locked nodes stay put, unlocked move toward net neighbors, positions inside outline; timing assertion <2 s at n=500. Benchmark script: pure-Python vs numpy per-iteration cost at n=100/500/2000 to validate budget and document when Barnes–Hut becomes necessary.
- [x] **6. Docs updates.** `docs/autorouting_glossary.md`: replace stale "tens of footprints" caveat with vectorized complexity budget + numpy approach + Barnes–Hut as later scaling path; note lock-parsing source. Optional fetches into `docs/wikipedia/` + index update in `docs/README.md`: `barnes_hut_simulation.md`, `a_star_search_algorithm.md`.

## Multi-Board Region Support (2026-10-03)

**Context**: DCCF and tube111 fixtures have multiple disjoint board outlines (Edge.Cuts). Current single bbox causes parts from different boards to collapse into the gap between them.

### Work Items (in order)

- [x] **R1. `board_model.py`: Board region detection.**
  - `BoardRegion` dataclass: polygon (tuple of Points), bbox, uuid
  - `_extract_board_polygons(edge_cuts, edge_arcs)`: build graph from segments + arc approximations, find connected cycles, return closed polygons
  - `_assign_board_regions(footprints, regions)`: point-in-polygon test, assign each footprint to containing region
  - Extend `BoardModel` with `board_regions: Tuple[BoardRegion, ...]` and `footprint_region: Dict[str, int]`
  - Test: DCCF → 2 regions, tube111 → 2 regions (one rectangular, one complex with rounded corners)

- [x] **R2. `placement.py`: Per-region force simulation.**
  - Split pads by `footprint_region` after `_collect_pad_nodes`
  - Run independent simulation per region (sub-arrays for positions, net edges, rigid constraints)
  - Per-region temperature from region bbox, clamp to region bbox
  - No cross-region repulsion/attraction; log warning if net spans regions
  - Skip regions with no movable footprints
  - Merge deltas/forces across regions

- [ ] **R3. `placement.py`: Polygon courtyard collision (shapely).**
  - `_footprint_courtyard_polygon(fp)`: build shapely Polygon from courtyard segments (arcs approximated), or pad bbox fallback
  - Overlap detection: `poly_i.intersects(poly_j)` → penetration vector from intersection centroid
  - Force: always-on 1/d² falloff; overlap → penetration_area * penetration_vector / (dist + eps); near-miss → margin / d²
  - Apply per-pad (creates natural torque); all footprints participate (pad bbox for no-courtyard)
  - Replace current `_compute_courtyard_forces` entirely

- [ ] **R4. Parameter updates & dependency.**
  - `placement.json`: `courtyard_repulsion_kc` 500 → 2000
  - `requirements.txt`: add `shapely>=2.0.0`

- [x] **R5. Tests.**
  - `test_extract_board_polygons_dccf`: DCCF → 2 regions with correct bboxes
  - `test_extract_board_polygons_tube111`: tube111 → 2 regions
  - `test_assign_regions`: footprints assigned correctly (some unassigned due to complex polygon)
  - `test_per_region_simulation`: DCCF parts don't collapse to middle
  - `test_courtyard_polygon_collision`: overlapping polygons generate repulsion
  - `test_no_cross_region_forces`: repulsion/attraction don't cross regions
  - `test_empty_region_skipped`: region with no movable parts skipped

## Routing Stage (2026-10-03)

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

### Routing Work Items (in order)

- [x] **R1. Grid & Obstacle Infrastructure** (`src/kicad_autorouter/routing/`)
  - `grid.py`: `RoutingGrid` dataclass (resolution, layers, origin, dims, coordinate transforms)
  - `obstacles.py`: `build_occupancy_grid(model, grid)` → (n_layers, H, W) int16 cost map
    - Footprint courtyards → blocked
    - Pad centers → high-cost with clearance margin
    - Existing tracks/vias → blocked on their layers
    - Zones (ground/power) → blocked on their layer
    - Edge cuts → edge keepout zone (0.5mm)
    - **Through-hole pads → blocked on BOTH layers (free via)**
    - Per-class clearance inflation (halos sized by net class)
  - `grid.py`: `CostMap` with base cost, via cost (50 cells), high-cost zones, edge keepout

- [x] **R2. Single-Net Router (Lee + A*)**
  - `router.py`: `SingleNetRouter` with A* search on 3D grid (x, y, layer)
  - Cost = wire_length + via_penalty*N_vias + layer_change_penalty
  - Heuristic: Manhattan distance * base_cost (admissible)
  - Multi-terminal via MST: connect all pads of a net via pairwise A* distances → MST → route edges sequentially
  - **Through-hole pads: transition at zero via cost** (pending: tht_via_mask in grid)

- [x] **R3. Multi-Net Sequential + Rip-up**
  - `scheduler.py`: net ordering (power → ground → critical → decap → general)
  - `RipUpManager`: max attempts, affected-net detection, reorder/retry
  - Sequential loop: route → on fail rip up + reorder → retry until budget exhausted

- [x] **R4. Placement Integration**
  - `board_model.py:commit_placement()`: computes affected nets from moved footprints, prunes tracks/vias
  - `io.py:rip_up_nets()`: S-expression level track/via removal
  - **Iterative place/route cycle**: `run_iterative_pipeline()` (pending)

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
