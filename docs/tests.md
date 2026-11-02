# Tests

Per-file intent, size, and overlap. Generated HTML reports stay untracked.
Full suite: 536 collected, 535 passed + 1 skipped, zero warnings.

## Markers (`pyproject.toml`)

`unit` pure no-fixture · `params` params validation · `physics` force-spring (needs nets) ·
`cli` CLI · `server` HTTP API · `regression` bug fixes · `minimal` 4-part fixture ·
`dccf` 97-part audio fixture. No `tube111` marker: the 105-part board is the
default heavy fixture (12+ files); deselect via `-m "not dccf"` + `--ignore` or
`-k` for fast loops. Use `minimal` for no-`kicad-cli` smoke tests.

## Running

`.venv/bin/python -m pytest tests/ -q` (fast: `-m "minimal or unit"`).
`scripts/gen_manual.py --check` in CI for manual/figure freshness.

## Placement & physics (231 tests)

| File | n | Intent |
|------|---|--------|
| `test_placement.py` | 19 | Physics runners: locked/movable, per-region sim, halo bounds, ghost walls, force-vs-delta consistency |
| `test_placement_forces` | 17 | Force-matrix in isolation: what repulsion/attraction/ghost/courtyard each do. Companion to `test_magnet` (clustering heuristics) |
| `test_placement_power` | 31 | Power-net attraction: fanout scaling, decap one-sided edges, membership (keyword + `extra_power` override) |
| `test_placement_movement` | 13 | Trajectories: converge/repel/attract without pathology + golden parity pins |
| `test_placement_pairs` | 12 | Movable-vs-immobile pairing + wall containment invariants on DCCF |
| `test_placement_settle` | 13 | Honest stopping: `converged`/`stalled`/`budget`, residual-force gate |
| `test_placement_tight` | 12 | Tight-quarters corpus + rotation-in-squeeze regressions, one full-DCCF audit |
| `test_placement_layers` | 10 | Layer gating: same-side collides, opposite SMD stacks, THT pins collide cross-layer |
| `test_placement_frames` | 6 | SVG motion-player frames: downsample + rounding |
| `test_placement_refine` | 20 | Tidy refine pass (synthetic + minimal) |
| `test_placement_degenerate` | 16 | Debris courtyards (Fab pin-dots), pad-hull fallback, no-net mounts as geometry-only nodes |
| `test_courtyard_behavior` | 18 | Courtyard parsing (RV1 reversed edge, micro-gaps), overlap force, RV1 resistor scenarios |
| `test_analytical_contact` | 10 | SAT rect/rect, circle/circle, rect/circle vs shapely fallback |
| `test_magnet` | 6 | Magnet-board tightening + zero-overlap (behavioral clustering) |
| `test_ghost_create` | 20 | Ghost builders + S-expr round-trip (`unit` + `physics`) |
| `test_padless_graphical` | 5 | Padless art: no physics, no SVG, no `/api/footprints` |
| `test_motion_roundtrip` | 3 | Player `translate·rotate` vs `apply_deltas` + SVG frame bytes |

## Routing (107 tests)

| File | n | Intent |
|------|---|--------|
| `test_routing_astar` | 2 | A* properties: halo steers but never traps |
| `test_routing_search` | 13 | Windowed search, via mask, Kruskal pair ordering (all synthetic, fast) |
| `test_routing_grid` | 6 | Grid from Edge.Cuts + board↔grid transforms, DCCF two-region cover |
| `test_routing_obstacles` | 9 | Cost-grid primitives: pads/keepouts as blocked/high-cost |
| `test_routing_capsule` | 15 | Capsule-exact pad stamps: spine geometry, rect inflation, slot coverage, halo tiers, VIN unsealed |
| `test_routing_crossing` | 1 | X-nets must cross via layer hop (2 vias, structural) |
| `test_routing_chunked` | 1 | N one-net calls == one N-net call (UI progress contract, DCCF ~10s) |
| `test_routing_smooth` | 14 | Any-angle smoother: shortcut/clearance properties + staircase-arc detour regression |
| `test_routing_tight_spot` | 12 | Cramped DCCF VIN + synthetic tight spots |
| `test_routing_tht_free_via` | 10 | THT hole as free via (R6 pattern) |
| `test_routing_limits` | 9 | Piecemeal tripwire (`max_terminals_per_net` 16) + chunk opt-out |
| `test_routing_piecemeal` | 8 | Chunk split/merge/coverage + 17/21-terminal rail sizes + failed-net partial copper |
| `test_routing_pour` | 8 | Pours passable-at-toll, severance steers |
| `test_track_ripup` | 7 | `commit_placement` + `rip_up_nets` |

## Power / pour / DRC (69 tests)

| File | n | Intent |
|------|---|--------|
| `test_pour` | 11 | Pour continuity model: masks + GND-island detection (analysis only) |
| `test_stitch` | 12 | GND repair: stitching vias + bridges |
| `test_gnd_strip` | 9 | GND-only eviction: signals never rip each other |
| `test_plane_fanout` | 4 | Inner-zone stubs to plane in 4-layer mode |
| `test_drc_routing` | 1 | Magnet nets routed → DRC passes |
| `test_impedance` | 22 | Inlet-anchored DC-resistance screening (report only) |
| `test_collapse` | 8 | 4-layer → genuine 2-layer collapse (synthetic) |
| `test_validate` | 2 | `kicad-cli` DRC/ERC on template files (needs `kicad-cli`) |

## Board model / I/O / render (48 tests)

| File | n | Intent |
|------|---|--------|
| `test_board_model` | 28 | Model, deltas, locks, UUIDs, zones, geometry guards |
| `test_io` | 11 | pcb+sch read/modify/write + S-expr round-trip |
| `test_svg_render` | 9 | Model → SVG renderer |

## Server / CLI / strategy (66 tests)

| File | n | Intent |
|------|---|--------|
| `test_server` | 10 | Workflow API end-to-end (`server`, `minimal`) |
| `test_server_manual` | 9 | Manual nudge + ghost CRUD endpoints |
| `test_server_validation` | 5 | Bad input → 400, never 500/poisoned state |
| `test_server_docs` | 2 | Docs viewer: allowlist + traversal guards |
| `test_board_mode` | 8 | Board-mode switch: round-trip, preview, collapse download |
| `test_quad_layers` | 7 | Load/display all copper, route outer-only |
| `test_allowed_layers` | 5 | Subset routing + explicit terminal failures |
| `test_run_autoroute` | 3 | Headless `scripts/run_autoroute.py` (`cli`, `minimal`) |
| `test_strategy` | 6 | Mode inference, presets, overlay, warning helper |
| `test_params_validation` | 7 | PlacementParams + RoutingParams fail-fast |
| `test_debug_grids` | 7 | `ui.debug_grids` numpy→dict payloads incl. PNG raster transport (pure unit) |

## Cull candidates (overlap, not yet removed)

Small files that duplicate a neighbor's contract — merge, don't extend:

* `test_routing_crossing` (1) + `test_routing_chunked` (1) + `test_routing_astar` (2) → fold into `test_routing_search` (13) as `test_routing_properties`. All pin search-level contracts; chunked is a server-design tripwire, keep that one test but co-locate it.
* `test_debug_grids` (4) → cull if the debug endpoint goes; it pins a view-model, not physics.
* `test_motion_roundtrip` (3) → keep only while the SVG player exists; it pins a duplicated transform, not placement.
* Pour/GND cluster (`test_pour` 11 + `test_stitch` 12 + `test_gnd_strip` 9 + `test_routing_pour` 8 = 40) → heaviest overlap; if one goes, `test_stitch` is the behavior that matters.
* Layer-mode sprawl (`test_board_mode` 8 + `test_collapse` 8 + `test_quad_layers` 7 + `test_allowed_layers` 5 + `test_plane_fanout` 4 = 32) → five files for one mode switch; collapse to mode + layers.
* `test_magnet` (6) vs `test_placement_forces` (17) → intentional companion split (behavior vs isolation); keep both unless trimming hard, then keep `test_magnet`.

Culled 2026-10-28 (21 tests, all green after): `TestCourtyardCollision`, `test_physics_moves_unlocked`, `test_selective_movable_uuids_empty`, `test_c11_close_neighbor` (in `test_placement.py`); `TestLayerGating` (4) + print-only disc-cap tuning test (in `test_courtyard_behavior.py`); `test_nets_magnetically_attract` + `test_locked_parts_dont_move` + `test_all_test_resistors_moved` (in `test_magnet.py`, superseded by heuristics + `test_physics_respects_locked`); `test_mask_matches_legacy_scan_exactly`, `test_courtyard_blocks_shortcut_when_high_cost` (legacy pins), `test_via_halo_matches_track_halo` (arithmetic, covered), `test_wall_exponent_two_matches_default_run` (tautology after default flip); `test_package_namespace_has_no_stdlib_leakage` (lint, not unit); `test_routed_tree_loads_in_kicad` (heavy `kicad-cli` duplicate); `test_server_docs` qmd-length + rendered-artifact + generated-manual greps (3); `test_routing_smooth_detour.py` folded into `test_routing_smooth.py` (file deleted).
