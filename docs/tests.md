# Tests

Per-file intent, size, and overlap. Generated HTML reports stay untracked.
Full suite: 758 collected, zero warnings (2 pre-existing
`test_routing_crossing` failures, fail on clean HEAD too).

## Markers (`pyproject.toml`)

`unit` fast (<5s, synthetic or small fixtures) · `params` params validation · `physics` force-spring (needs nets) ·
`cli` CLI (incl. kicad-cli-gated, skips without the binary) · `server` HTTP API · `regression` bug fixes · `minimal` 4-part fixture ·
`dccf` 97-part audio fixture · `slow` >3s, excluded from the fast loop.
No `tube111` marker: the 105-part board is the
default heavy fixture (12+ files). Slow DCCF tests carry `@pytest.mark.slow`;
fast DCCF tests (grid builds, padless checks, tight-spot probes) run in the
fast loop. Deselect slow via `-m "not slow"`; deselect all DCCF via
`-m "not dccf"`. Use `minimal` for no-`kicad-cli` smoke tests.

## Running

`.venv/bin/python -m pytest tests/ -q` (fast: `-m "minimal or unit or (dccf and not slow)"`).
`scripts/gen_manual.py --check` in CI for manual/figure freshness.

## Placement & physics (235 tests)

| File | n | Intent |
|------|---|--------|
| `test_placement.py` | 19 | Physics runners: locked/movable, per-region sim, halo bounds, ghost walls, force-vs-delta consistency |
| `test_placement_forces` | 14 | Force-matrix in isolation: what repulsion/attraction/ghost/courtyard each do. Companion to `test_magnet` (clustering heuristics) |
| `test_placement_power` | 31 | Power-net attraction: fanout scaling, decap one-sided edges, membership (keyword + `extra_power` override) |
| `test_placement_movement` | 7 | Trajectories: converge/repel/attract without pathology (inequalities, not golden pins) |
| `test_placement_pairs` | 12 | Movable-vs-immobile pairing + wall containment invariants on DCCF |
| `test_placement_settle` | 13 | Honest stopping: `converged`/`stalled`/`budget`, residual-force gate |
| `test_placement_tight` | 12 | Tight-quarters corpus + rotation-in-squeeze regressions, one full-DCCF audit |
| `test_placement_layers` | 10 | Layer gating: same-side collides, opposite SMD stacks, THT pins collide cross-layer |
| `test_placement_frames` | 6 | SVG motion-player frames: downsample + rounding |
| `test_placement_align` | 35 | Tidy regime: Manhattan snap + row hold + weak leash (synthetic + minimal) |
| `test_placement_degenerate` | 16 | Debris courtyards (Fab pin-dots), pad-hull fallback, no-net mounts as geometry-only nodes |
| `test_courtyard_behavior` | 18 | Courtyard parsing (RV1 reversed edge, micro-gaps), overlap force, RV1 resistor scenarios |
| `test_analytical_contact` | 10 | SAT rect/rect, circle/circle, rect/circle vs shapely fallback |
| `test_magnet` | 6 | Magnet-board tightening + zero-overlap (behavioral clustering) |
| `test_ghost_create` | 20 | Ghost builders + S-expr round-trip (`unit` + `physics`) |
| `test_padless_graphical` | 5 | Padless art: no physics, no SVG, no `/api/footprints` |
| `test_motion_roundtrip` | 2 | Player `translate·rotate` vs `apply_deltas` + SVG frame bytes (frames-JSON test moved to `test_server`) |

## Routing (152 tests)

| File | n | Intent |
|------|---|--------|
| `test_routing_search` | 36 | Windowed search, via mask, Kruskal pair ordering + A* properties + THT hard-exclusion stamp (all synthetic, fast, `unit`) |
| `test_routing_grid` | 6 | Grid from Edge.Cuts + board↔grid transforms, DCCF two-region cover |
| `test_routing_obstacles` | 9 | Cost-grid primitives: pads/keepouts as blocked/high-cost |
| `test_routing_capsule` | 15 | Capsule-exact pad stamps: spine geometry, rect inflation, slot coverage, halo tiers, VIN unsealed |
| `test_routing_crossing` | 1 | X-nets must cross via layer hop (2 vias, structural) |
| `test_routing_smooth` | 30 | Any-angle smoother + effort ladder/resolve + Polish (folded from `test_smooth_polish`; refine-ordering moved to `test_order_refine`) |
| `test_routing_tight_spot` | 12 | Cramped DCCF VIN + synthetic tight spots |
| `test_routing_tht_free_via` | 10 | THT hole as free via (R6 pattern) |
| `test_routing_limits` | 10 | Piecemeal tripwire (`max_terminals_per_net` 16) + chunk opt-out |
| `test_routing_piecemeal` | 12 | Chunk split/merge/coverage + 17/21-terminal rail sizes + failed-net partial copper |
| `test_routing_pour` | 7 | Pours passable-at-toll, severance steers (route+stitch e2e moved to `test_stitch`) |
| `test_track_ripup` | 7 | `commit_placement` + `rip_up_nets` |

## Power / pour / DRC (83 tests)

| File | n | Intent |
|------|---|--------|
| `test_pour` | 11 | Pour continuity model: masks + GND-island detection (analysis only) |
| `test_stitch` | 20 | GND repair: stitching vias + bridges + route-level copper e2e (folded from `test_routing_pour`, `test_gnd_strip`) |
| `test_gnd_strip` | 15 | GND-only eviction: signals never rip each other (e2e moved to `test_stitch`) |
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

## Server / CLI / strategy (74 tests)

| File | n | Intent |
|------|---|--------|
| `test_server` | 20 | Workflow API end-to-end (folded `test_server_validation`; frames-JSON moved from `test_motion_roundtrip`) |
| `test_server_manual` | 9 | Manual nudge + ghost CRUD endpoints |
| `test_server_docs` | 2 | Docs viewer: allowlist + traversal guards |
| `test_board_mode` | 8 | Board-mode switch: round-trip, preview, collapse download |
| `test_quad_layers` | 7 | Load/display all copper, route outer-only |
| `test_allowed_layers` | 5 | Subset routing + explicit terminal failures |
| `test_run_autoroute` | 3 | Headless `scripts/run_autoroute.py` (`cli`, `minimal`) |
| `test_strategy` | 6 | Mode inference, presets, overlay, warning helper |
| `test_params_validation` | 7 | PlacementParams + RoutingParams fail-fast |
| `test_debug_grids` | 7 | `ui.debug_grids` numpy→dict payloads incl. PNG raster transport (pure unit) |

## Cull candidates (overlap, not yet removed)

Done 2026-11-10 (each green after): `test_server_validation` → `test_server`
(shared `tests/server_helpers.py` harness; frames-JSON test moved from
`test_motion_roundtrip`); `test_smooth_polish` → `test_routing_smooth`
(refine-ordering → `test_order_refine`); `test_routing_astar` +
`test_routing_exclusion_guard` → `test_routing_search` (all `unit`);
`test_routing_chunked` → `test_routing_piecemeal` (split cancel scenario
into its own test); route-level copper e2e → `test_stitch`; label params
+ MH1 round-trip deduped; settle/movement/align/shove/retry/emit/biarc/
pour/strip/limits tables parametrized; shared `tests/_shove_boards.py`,
`zeros_grid`/`zeros_plane`/`ladder_board`/`facing_label_pair` helpers;
`unit`/`minimal`/`dccf` marker corrections on small synthetic tests.
Deliberately kept separate: `test_nets_table` (zero-boot handler-direct),
`test_label_depenetrate` (pure geometry), `test_drc_routing` (sole
kicad-cli integration), `test_magnet` vs `test_placement_forces`
(behavior vs isolation), magnet apply/commit preservation (distinct
transform vs writeback contracts), THT-facing-SMD board builders
(region contexts differ).

Still open:

* `test_debug_grids` (7) → cull if the debug endpoint goes; it pins a view-model, not physics.
* `test_motion_roundtrip` (2) → keep only while the SVG player exists; it pins a duplicated transform, not placement.
* Layer-mode sprawl (`test_board_mode` 8 + `test_collapse` 8 + `test_quad_layers` 7 + `test_allowed_layers` 5 + `test_plane_fanout` 4 = 32) → five files for one mode switch; collapse to mode + layers.
* `test_routing_crossing` (2) → fold into `test_routing_search` with the next search-touching change.

Culled 2026-10-28 (21 tests, all green after — see git history for the list).
