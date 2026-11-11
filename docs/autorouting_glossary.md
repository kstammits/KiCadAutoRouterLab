# Autorouting Glossary & Guidance

Terminology and design guidance for this project, aligned to the workflow
stages `ingest → parse → connectivity → place → route → drc → writeback`.
Sources are the upstream references listed under [Source index](#source-index).
Local copies are not vendored; `docs/README.md` has fetch recipes.

## Stage: ingest — load `.kicad_pcb` / `.kicad_sch`

- **S-expression**: the file format of both `.kicad_pcb` and `.kicad_sch`. Nested
  parenthesized lists, e.g. `(kicad_pcb (version 20241229) ...)`. Not JSON — any parser
  must be an S-expression reader. Version tags in KiCad 10: `20241229` (pcb),
  `20250114` (sch). See [KiCad files and folders](https://docs.kicad.org/10.0/en/kicad/kicad.html).
- **Load failure**: a file KiCad cannot parse exits non-zero (code 3) with
  `Failed to load ...` on stderr — distinct from rule violations. Handled in
  `src/kicad_autorouter/validate.py`.

## Stage: parse — extract board objects

- **Footprint** (`footprint`): a placed component; carries position, rotation, and the
  native lock flag (`fp.IsLocked()` / `fp.SetLocked(True)`). Locked footprints are fixed
  anchors; only unlocked ones may move. In `.kicad_pcb` files the lock is serialized as a
  top-level `(locked ...)` token inside each footprint node — verified against the KiCad
  source mirror (`pcb_io_kicad_sexpr*.cpp`: writer ~line 1474, parser line 6142).
- **Pad**: a connectable copper shape on a footprint (or standalone), assigned to a net.
  Pads are the terminals the router must connect.
- **Net**: an electrical connection, named or numbered; all pads of one net must be
  connected and no pad of different nets may touch.
- **Track** (`track`): a routed copper segment on one layer between two points, with a
  width. The unit the router emits during writeback.
- **Via**: connects tracks across layers at one point.
- **Zone**: a poured copper area (e.g. ground plane); passable at a toll for routing
  (narrow pour corridors carry an extra severance cost so tracks prefer wide copper).
- **Board outline / edge cuts** (`gr_line` on Edge.Cuts): the physical board boundary;
  routing must stay inside it.
- **Layer**: copper or technical layer, e.g. `F.Cu`, `B.Cu`. Design rules can differ per
  layer (widths, spacings).

## Stage: connectivity — net graph

- Build a graph with one node per pad and edges between pads of the same net (networkx).
  This is the input to routing: each net becomes a set of terminals that must be joined.
- **Terminal / pin**: synonym for pad in router literature; "pins on cells" are the
  pre-existing polygons a router is given, along with obstacles and optional preroutes.

## Stage: place — SI dynamics simulation

Placement moves unlocked footprints with real physics in SI units
(newtons, meters, seconds — millimeters only at file ingest and
reporting). Normative formulas: [`si_force_laws.md`](si_force_laws.md);
intuition and figures: [`physics_primer.qmd`](physics_primer.qmd);
calibration ranges: `scripts/calibrate_si.py`.

### SI dynamics quick reference

| Term | Definition | `si_force_laws.md` | Module symbol |
|------|------------|--------------------|----------------------|
| **wall / halo** | One law in signed separation `s` (gap +, penetration −): `F = k_wall·(1 − s/halo)^wall_exponent` | §1 Courtyard collision | `PlacementParams.wall_k_N`, `PlacementParams.wall_halo_m`, `_wall_mag` |
| **MTV (minimum-translation vector)** | SAT overlap direction — out the nearest edge; correct for containment/small-in-large | §1 Courtyard collision (Contact direction rule) | `_apply_contact_push_analytical`, `_sat_rect_rect`, `_rect_circle_contact` |
| **Per-pair aggregation** | Deepest contact wins per pair (summed areas, strongest direction); pin circles don't stack | §1 Per-pair aggregation | `_apply_contact_push_analytical`, `_contact_weights` |
| **Kabsch snap / projection** | SVD rigid fit of proposed pads vs reference shape; bodies stay exact with no stiffness parameter | §2 Integrator (Rigid-step clamp) | `_project_rigid_poses` |
| **Rigid-step clamp** | Bounds post-projection body motion: centroid shift ≤ `vmax·dt`, rotation half measured step capped at 5°/step | §2 Rigid-step clamp | `_clamp_rigid_step` |
| **Deadband (angular)** | `MIN_ROT_STEP_RAD` (currently 0.0 = disabled); parking micro-rotations wedged pairs | §2 Rigid-step clamp | `_clamp_rigid_step`, `MIN_ROT_STEP_RAD` |
| **Adaptive-dt guard** | Halve dt + bleed velocity on KE jumps (>1.25×), regrow ×1.1; bounded by `dt_min_s`/`dt_max_s` | §2 Adaptive-dt guard | `_run_region_simulation` (adaptive-dt block) |
| **Tightening ratio** | Final vs start pooled intra-group distance; <1 means the board clusters | §3 Regime map | `tighten_mean`, `tighten_max` (calibration) |
| **Broadphase** | 15 mm uniform-grid + bounding-circle cull before exact courtyard geometry | §1 Broadphase | `_run_region_simulation` (courtyard cache) |
| **Decap / power / GND rules** | Fanout scaling on power edges; ground excluded except decap-incident; `decap_link` multiplier + local chip links | §1 Decap-chip link, Fanout scaling, Ground exclusion | `_build_net_edges_pad_level`, `_find_decap_chip_links`, `PlacementParams.decap_link`, `PlacementParams.power_attraction_scale` |
| **Tangential assist** | Sliding assist along contact (MTV-perp): `γ·|F_n|·sign(v_rel_t)`, latched per contact pair so near-zero velocity noise doesn't flip-slide | §1 Tangential assist | `PlacementParams.contact_tangent_gamma`, `_tangent_sign` |
| **Final depenetration pass** | One relaxed nearest-point separation round after dynamics settle: ejects residual courtyard overlap the penalty law couldn't clear | `src/kicad_autorouter/placement/runner.py` (`_depenetrate_positions`) | `_DEPEN_RELAX`, `_DEPEN_CLEAN_AREA_MM2`, `_DEPEN_MAX_ROT_RAD` |

### Physics in brief

- **Integrator**: semi-implicit Euler per movable pad (kick, viscous decay, speed-ceiling clamp, drift), adaptive-dt guard on KE jumps, Kabsch rigid projection so bodies stay exact, phantom-energy re-derivation, stop at `convergence_eps_m`. Details: `si_force_laws.md` §2.
- **Forces**: Coulomb-like repulsion (sub-mm equilibrium; the wall owns contact), constant-magnitude net attraction (default 8e-3 polish, ~0.05 cluster), one courtyard wall law `F = k·(1−s/halo)^exp` shared by part–part contact and the board edge, ghost pull, decap-chip links, Tidy Manhattan snap, power fanout scaling, GND exclusion (decap-incident only), tangential sliding assist. Details: `si_force_laws.md` §1.
- **Contact**: courtyard per footprint (`F/B.CrtYd`, else Fab/SilkS, else pad-bbox); THT pin circles on both layers; SAT MTV direction; per-pair aggregation (deepest contact wins, no pin stacking); bounding-circle + uniform-grid broadphase.
- **Motion**: only unlocked in-region `movable_uuids` integrate; locked parts are anchors; UI moves are `(dx_mm, dy_mm, dangle_deg)` deltas by UUID; ghosts are fixed pseudo-parts (wall or attractor). Mass comes from `data/footprint_mass.json` (never zero); identity keys on footprint UUID.
- **Calibration**: magnet board (`scripts/calibrate_si.py`, `tests/test_magnet.py`), seeds 42/7/123, tightening ratio <1 means clustering, cluster vs polish regimes in `si_force_laws.md` §3. Test markers: `physics`, `dccf`, `server`, `regression`, `unit`, `params`, `minimal`, `cli`.
## Stage: route — the autorouter core

The routing task (from [Wikipedia: Autorouter](https://en.wikipedia.org/wiki/Autorouter)): create
geometries such that all terminals of each net connect, no different nets connect, and
all design rules are obeyed. Failure modes:

- **Open**: a terminal left unconnected.
- **Short**: two terminals of different nets connected by mistake.
- **DRC violation**: clearance/width/layer rule broken (see stage `drc`).

Key facts that shape the algorithm choice:

- Routing is intractable — even the single-net, single-layer shortest route (Steiner tree)
  is NP-complete. Real routers are therefore **heuristics** aiming for "good enough", not
  optimal.
- **Maze router**: represents routing space as a grid sized to the wiring pitch; blocked
  cells = components, existing tracks (pours are passable at a toll, not blocked). Find a chain of free cells from A to B
  ([Wikipedia: Maze routing](https://en.wikipedia.org/wiki/Maze_routing)).
- **Lee algorithm**: BFS wave expansion over that grid — mark start `0`, repeatedly mark
  unlabeled neighbors with the next index until the target is reached or no points remain,
  then backtrace through decreasing marks. Always optimal if a path exists, but slow and
  memory-hungry ([Wikipedia: Lee algorithm](https://en.wikipedia.org/wiki/Lee_algorithm)). A* search
  ([Wikipedia: A* search](https://en.wikipedia.org/wiki/A*_search_algorithm)) is
  commonly used in maze/Lee routers to cut the cost.
- **Global routing**: first pick an approximate course per net on a coarse grid (optionally
  assigning layers), then do detailed routing cell by cell — limits the size of the hard part.
- **Rip-up and reroute**: route nets in sequence; if some fail, remove selected routings,
  reorder, retry until all nets are routed or we give up.
- **Push-and-shove** ("shove-aside"): move already-routed nets out of the way to make room.
- **Smoothing / string-pulling**: shortcut grid-path nodes where line-of-sight is walkable.
  Our 4-directional A* staircases become fewer, longer any-angle tracks via bidirectional
  greedy passes + Steiner detour repair; ties break by admitted fillet radius, so the
  rounder corner wins.

### Tangent-arc corner fillets (`arc_fillets`, default on)

KiCad tracks are `(segment)`/`(via)`, but `(arc start mid end)` is also legal
copper. When `RoutingParams.arc_fillets` is on, each smoothed run is filleted
in place: broad sweeps collapse jagged windows into one circumcircle sweep,
remaining sharp joints get one tangent arc each (tangent length capped at half
each leg and `arc_max_radius_mm`; legs below `arc_min_leg_mm` stay sharp).
Arcs pass a clearance gate sampled at `arc_sample_mm` (fail → sharp joint),
replace chord emission branch by branch, and count/rip with their net.
Degenerate arcs (start == end) are zero copper everywhere.
Implementation: `routing/biarc.py:fit_branch_arcs_with_sweeps`.

### Copper pours in routing

- **Passable-at-toll**: zones (pours) are not blocked; they carry a soft cost
  (`POUR=35` + `CostMap.pour_penalty=10`) so the router can cross them but prefers
  to go around. This avoids the hard-block problem where pads inside pours became
  unroutable.
- **Severance steering**: `severance_cost()` adds a secondary toll for tracks that
  narrow a pour corridor below `min_thickness`; it is clipped (steers, never blocks)
  so the router can still cross if needed.
- **GND-only rip-up**: pre-existing GND vias/tracks can be stripped before routing
  (`Strip GND Blockers`); the stitcher (`plan_stitches()`) rebuilds continuity
  afterwards with overlap vias + bridge tracks. **Signal/power copper is never
  evicted** — no more tracks ripping each other out.
- **Island diagnostics**: `gnd_islands()` computes connected pour regions per layer;
  UI shows red dashed bboxes for dead islands (no pad/via connection), matching
  KiCad's isolated-fill DRC.
- **Region-aware stitching**: stitcher respects board panel boundaries (never joins
  separate panels in the same file).
- **Stitch siting**: candidate vias clear foreign copper analytically (tracks
  included) and every drilled hole — including mounting boreholes and same-net
  barrels — by the hole clearance; part-less pour slivers below
  `stitch_min_island_mm2` are ignored instead of stitched.

### Zone refill round-trip

- **kicad-cli refill**: after routing, the pipeline can invoke
  `kicad-cli pcb drc --refill-zones --save-board <board>` to regenerate all zone
  fills using KiCad's native algorithm (corner rounding, thermal reliefs, min-thickness
  pruning, nearby-poly connection). This ensures the output board has correct pour
  geometry and passes KiCad's own DRC for isolated fills.
- **Refill zones parameter**: `RoutingParams.refill_zones` (default `False`) enables the
  refill step in `route_nets()`. The UI exposes a "Refill zones" checkbox in the
  routing options.
- **Workflow**: route → apply tracks/vias → refill zones → final DRC. The refill step
  runs on the written board file, so it sees the exact copper the router placed.

### Secondary grid / fanout cost improvements

- **No-net pad steering**: pads with `net_name=None` (unconnected mounting holes,
  mechanical pins) now cast halos in the secondary cost grid (`build_fanout_cost`).
  Previously they were skipped, allowing tracks to route through them.
- **Unconnected-net handling**: nets named `unconnected-*` (KiCad's placeholder for
  explicitly unconnected pins) are also treated as no-net for steering purposes.
- **Foreign SMD halos**: foreign SMD pads stamp oriented capsules in three
  tiers — hard exclusion on copper only, steep toll over the clearance +
  track-half margin, soft toll beyond. A* can squeeze a tight thread at high
  cost but never touch foreign copper.
- **Conservative track half**: pad marking uses 1/8 mm so tracks stay clear
  of pad edges even at coarse grids.

## Stage: drc — design rule check

- **Design rule matrix**: per-layer minimum track width, clearance between tracks/pads,
  via sizes, etc. The DRC stage validates the routed board against it.
- `kicad-cli pcb drc <board> --format json -o report.json` produces a JSON report with
  top-level `violations[]` and `unconnected_items[]`; `--exit-code-violations` makes the
  exit code reflect violations, not just load failures (see [kicad-cli reference](https://docs.kicad.org/10.0/en/cli/cli.html)
  and `src/kicad_autorouter/validate.py`).
- Example violation: `track_dangling | Track has unconnected end`.
- **Ignored violation classes**: the UI overlay and `DRCResult.filter_violations()` hide
  non-routing noise by default (silkscreen, footprint metadata, `track_not_centered_on_via`;
  see `DEFAULT_IGNORED_TYPES` in `src/kicad_autorouter/drc.py`). UI counts are post-filter;
  the raw `kicad-cli` JSON in `tmp/` keeps everything.

## Stage: writeback — emit updated `.kicad_pcb`

- Write new tracks (and moved footprints) back as S-expressions. In the current pass this
  is pure Python in `.venv`: per-footprint deltas are applied by UUID via
  `io.nudge_footprint_by_uuid` and serialized with `sexpr.to_sexpr`.
- Always re-run DRC after writeback — it is the acceptance check for a pass.

## Placement controls

- **Ghost (pseudo-part)**: a fixed anchor that participates in placement
  forces but is never moved or written as copper — a **wall**
  (courtyard-only obstacle that pushes parts away, e.g. a keepout) or an
  **attractor** (pulls specific refs via
  `(property "ghost_attract_N" "TARGET,ka")` springs). Serialized as a
  regular `(footprint ...)` plus `(property "ghost" "yes")`, courtyard on
  `F.CrtYd` (or `F.Fab`, which the parser falls back to); created with
  `ghost.make_ghost_wall / make_ghost_attractor` + `io.add_ghost_to_tree`,
  managed from the UI Ghosts panel (move / rotate ±15°, resize W/H
  steppers, delete; rotation pivots about the anchor).
- **Manual nudge**: hand-shift the placement selection by 1 mm (arrow keys
  / nudge pad, Shift for 10 mm) and rotate it ±15° (Shift for 90°) via
  `POST /api/placement/nudge` — same dual-write, rip-up (protected kept),
  and Undo semantics as Accept.
- **Complexity budget (vectorized)**: naive all-pairs repulsion is O(n²)
  per iteration, O(n³) total. Computing it as numpy broadcasting over the
  dense (n,n) distance matrix costs ≈1–2 ms per iteration; net-edge
  attraction stays sparse O(E), vectorized with edge index arrays, and
  locked footprints are a boolean mask (zero force). Dense n² is plenty at
  "a few hundred" nodes; Barnes–Hut
  ([Wikipedia: Barnes–Hut simulation](https://en.wikipedia.org/wiki/Barnes%E2%80%93Hut_simulation))
  becomes necessary past ~3–5k nodes.

## Source index

| Upstream source | Used for |
| --- | --- |
| [Wikipedia: Autorouter](https://en.wikipedia.org/wiki/Autorouter) | Routing task, failure modes, complexity, router taxonomy, global/detailed + rip-up strategies |
| [Wikipedia: Maze routing](https://en.wikipedia.org/wiki/Maze_routing) | Grid maze routing model |
| [Wikipedia: Lee algorithm](https://en.wikipedia.org/wiki/Lee_algorithm) | BFS wave expansion, optimality vs cost |
| [Wikipedia: Force-directed graph drawing](https://en.wikipedia.org/wiki/Force-directed_graph_drawing) | Spring/Coulomb placement model for unlocked footprints |
| [Wikipedia: Barnes–Hut simulation](https://en.wikipedia.org/wiki/Barnes%E2%80%93Hut_simulation) | Barnes–Hut O(n log n) approximation of all-pairs repulsion; scaling path past ~3–5k nodes |
| [Wikipedia: A* search](https://en.wikipedia.org/wiki/A*_search_algorithm) | Heuristic grid search (f = g + h); cost cut for maze/Lee routing |
| [KiCad: project files and folders](https://docs.kicad.org/10.0/en/kicad/kicad.html) | `.kicad_*` file types, S-expression formats |
| [KiCad: PCB editor manual](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html) | Footprints, pads, nets, tracks, zones, DRC in the PCB editor |
| [KiCad: CLI reference](https://docs.kicad.org/10.0/en/cli/cli.html) | `kicad-cli pcb drc` / `sch erc` JSON reports and exit codes |
| [`src/kicad_autorouter/routing/smooth.py`](src/kicad_autorouter/routing/smooth.py) | String-pulling smoothing with bidirectional greedy + Steiner detour repair |
| [`tests/test_analytical_contact.py`](tests/test_analytical_contact.py) | SAT courtyard collision test suite (rectangle/circle SAT) |
