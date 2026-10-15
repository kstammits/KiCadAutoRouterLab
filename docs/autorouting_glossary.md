# Autorouting Glossary & Guidance

Terminology and design guidance for this project, aligned to the pipeline stages in
`src/kicad_autorouter/pipeline.py`: `ingest → parse → connectivity → route → drc → writeback`.
Sources are the local copies under [`wikipedia/`](wikipedia/) (fetched 2026-09-28) and
[`kicad/`](kicad/) (KiCad 10.0.7, exact version match).

## Stage: ingest — load `.kicad_pcb` / `.kicad_sch`

- **S-expression**: the file format of both `.kicad_pcb` and `.kicad_sch`. Nested
  parenthesized lists, e.g. `(kicad_pcb (version 20241229) ...)`. Not JSON — any parser
  must be an S-expression reader. Version tags in KiCad 10: `20241229` (pcb),
  `20250114` (sch). See [`kicad/kicad.md`](kicad/kicad.md) ("KiCad files and folders").
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
(newtons, meters, seconds, kilograms — millimeters only at file
ingest and result reporting). Normative formulas:
[`si_force_laws.md`](si_force_laws.md); calibration ranges:
`scripts/calibrate_si.py`. This supersedes the Hooke-spring model in
"Force-directed placement of unlocked components" below (constant-magnitude
attraction replaced springs; semi-implicit Euler + projection replaced the
stiffness-capped stepper).

### Units & integrator

- **m/N/s foundation**: the solver runs entirely in meters, newtons,
  seconds; board geometry (mm) converts at force evaluation and reporting
  (`src/kicad_autorouter/placement.py` module docstring).
- **Semi-implicit Euler**: per movable pad per step: kick `v += (F/m)·dt`,
  viscous decay, speed-ceiling clamp, drift `x += v·dt`
  (`_run_region_simulation`).
- **Viscous damping** (`damping_Ns_m`, N·s/m): implicit-Euler decay
  `v *= 1/(1 + c·dt/m)` — the one physics damper, stable at any dt
  (`PlacementParams.damping_Ns_m`).
- **Dry friction** (`friction_N`, N): whole-footprint deadband — below this
  total force the footprint freezes for the step. Stops micro-motion; kept
  low so gradual movement keeps working. Not the same as damping
  (`PlacementParams.friction_N`).
- **Adaptive-dt guard**: halve dt + bleed velocity on kinetic-energy jumps
  (>1.25×), regrow ×1.1 otherwise, bounded by `dt_min_s`/`dt_max_s`
  (`_run_region_simulation`).
- **Kabsch snap / projection**: SVD rigid fit of proposed pads vs reference
  shape — bodies stay exact to float precision with no stiffness parameter
  (`_project_rigid_poses`).
- **Phantom energy**: velocity is re-derived from the actual projected
  displacement (`v := (x_proj − x_old)/dt`), so projection can't inject
  energy into the guard (`_run_region_simulation`).
- **No-cooling invariant**: integrator state is a deterministic function of
  the trajectory, not the iteration budget.
- **Converged / settled**: stop when max pad displacement per step falls
  below `convergence_eps_m` (meters); only moves >1e-9 reported
  (`_run_region_simulation` tail).

### Forces (all in newtons)

- **Repulsion** (`repulsion_k_N_m2`, N·m²): Coulomb-like `k/d²` between pads;
  calibrated so the pair equilibrium sits sub-mm — the wall owns contact
  response, not this (`PlacementParams.repulsion_k_N_m2`).
- **Attraction** (`attraction_k_N`, N): constant-magnitude pull along
  net-sharing pad pairs. Calibrated default 8e-3 (placed-board polish);
  scattered-start clustering wants ~0.04–0.06
  (`PlacementParams.attraction_k_N`).
- **Wall / courtyard contact** (`wall_k_N`, N): elastic push-out on
  courtyard overlap, `F = k_wall·(1 + area/area_ref)`
  (`PlacementParams.wall_k_N`, `wall_area_ref_m2`).
- **Halo** (`wall_halo_m`, m): near-miss reach where the wall ramps in
  before touch, saturating at `wall_k_N` and falling linearly to zero at
  the edge (`PlacementParams.wall_halo_m`).
- **Boundary** (`boundary_k_N`, N): bounded linear falloff from region
  edges (max 2·kb at the edge, saturating pull-back outside); reach is
  dynamic — 10% of body size, min 1 mm
  (`PlacementParams.boundary_k_N`, `_boundary_edge_push`).
- **Ghost pull** (`ghost_k_N`, N): fixed pseudo-part attractor; file
  `TARGET,ka` values weight it, no reaction on the ghost
  (`PlacementParams.ghost_k_N`).
- **Decap boost** (`decap_boost`, dimensionless): multiplier on
  decoupling-cap-incident edges; 0 also removes the decap GND-side
  exception (`PlacementParams.decap_boost`).
- **Fanout scaling**: power edges weigh `power_scale/(fanout−1)` so a
  shared rail pulls each pad about as hard in total as one signal edge —
  rails minimize wirelength without collapsing the board
  (`_build_net_edges_pad_level`).
- **Ground exclusion**: ground nets get zero attraction edges (except
  decap-incident ones) — pours connect GND anywhere, so general GND
  pull would only clump parts (`_build_net_edges_pad_level`).

### Contact & collision

- **Courtyard**: collision outline per footprint — `F/B.CrtYd` polygon,
  else Fab/SilkS, else pad-bbox + margin
  (`_footprint_courtyard_polygon`).
- **Body vs pin courtyards**: THT pads get pin circles on both layers,
  checked body–body / pin–body / pin–pin (`_local_shape`
  in `_run_region_simulation`).
- **MTV (minimum-translation vector)**: SAT overlap direction — out the
  nearest edge. Used for rect/circle overlap; correct for
  containment/small-in-large where centroid-to-centroid shoves the small
  part deeper (`_apply_contact_push_analytical`).
- **Per-pair aggregation**: body + pin sub-paths of one footprint pair
  merge into a single penalty (summed areas, strongest direction), so pin
  circles can't stack the same contact 3–9×
  (`_apply_contact_push_analytical`).
- **SAT / analytical contact** (`ShapeType`): rectangle/circle fast path
  vs shapely fallback for generic polygons
  (`_classify_shape`, `_sat_rect_rect`, `_rect_circle_contact`).
- **Contact weights / spread**: pair push distributed over pads with
  weights summing to 1 — totals conserved by construction
  (`_contact_weights`, inner `_spread`).
- **Broadphase**: vectorized bounding-circle + uniform-grid cull of far
  pairs before exact geometry (`_run_region_simulation` courtyard cache;
  TODO Phase 1).

### Bodies & motion

- **Movable**: unlocked + in-region + in `movable_uuids`; only these pads
  integrate (`_run_region_simulation`: `pad_movable`).
- **Locked**: fixed anchor mirroring KiCad's lock flag; never in deltas,
  never integrated (`Footprint.locked` in
  `src/kicad_autorouter/board_model.py`).
- **Deltas**: `(dx_mm, dy_mm, dangle_deg)` per footprint UUID vs the
  committed board — the unit the UI accepts
  (`PlacementProposal`, `apply_deltas`).
- **Rigid-step clamp**: bounds post-projection body motion per step —
  centroid shift ≤ `vmax·dt`, rotation ≤ 5°/step — so tunneling is
  impossible by construction (`_clamp_rigid_step`).
- **Angular deadband** (`MIN_ROT_STEP_RAD`, 0.1°): sub-threshold per-step
  rotations dropped as creep noise; genuine torque passes through. A noise
  floor, not a tuning knob — deliberately not a slider
  (`_clamp_rigid_step`).
- **Speed ceiling** (`vmax_m_s`, m/s): caps velocities and, via the clamp,
  body motion (`PlacementParams.vmax_m_s`).
- **Frames**: stride-sampled `(dx, dy, da)` vs start for the SVG motion
  player; first frame is the zero start, last is final
  (`PlacementProposal`, `_thin_frames`, `run_placement`).
- **Force frames / components**: per-frame `{uuid: {cause: (fx, fy)}}` for
  `repulsion / attraction / courtyard / boundary / ghost` — what the UI
  force overlay draws (`PlacementProposal`, `FORCE_CAUSE_ORDER` in
  `ui/index.html`, `applyMotionFrame` pivoting about committed origins).
- **Proposal**: deltas + frames from `run_placement`; empty means
  converged/nothing movable. Accepted via the accept flow: push undo →
  affected nets → `commit_placement` → `rip_up_nets` → nudge by UUID
  (`commit_placement` in `board_model.py`).
- **Ghost (wall / attractor)**: fixed pseudo-part — courtyard-only
  obstacle or spring anchor; never moved or written as copper
  (`ghost.py`: `make_ghost_wall` / `make_ghost_attractor`).

### Mass & identity

- **Mass table** (`data/footprint_mass.json`): exact `footprint_id` →
  keyword substring → area fallback (`m = density·area + floor`), always
  clamped ≥ floor so `F=ma` never divides by zero
  (`footprint_mass_kg`, `courtyard_bbox_area_m2` in
  `src/kicad_autorouter/mass.py`).
- **uuid**: stable part identity (refs duplicate or miss; first-wins
  `by_ref`); deltas, regions, and writeback all key on it
  (`Footprint.uuid` in `board_model.py`).
- **THT vs SMD**: `thru_hole`/`np_thru_hole` + drill vs surface pads —
  decides pin courtyards and cross-layer collision
  (`Pad.is_through_hole` in `board_model.py`).
- **Region**: Edge.Cuts outline polygon simulated independently;
  `footprint_region` maps uuid → index (−1 = outside)
  (`BoardRegion` in `board_model.py`).

### Calibration vocabulary

- **Magnet board**: cheap synthetic — 7 resistors on shared nets,
  overlap-free scattered starts inside the region bbox
  (`scripts/calibrate_si.py`, `tests/test_magnet.py`).
- **Seed**: scattered-start index (`build_magnet_board(seed)`); default
  sweep seeds 42/7/123).
- **Sweep**: focused param grid (`--quick`: 24 combos × 3 seeds) ranking
  by tightening + violations + overlap (`scripts/calibrate_si.py`).
- **Tightening ratio**: final vs start pooled intra-group distance; <1
  means the board clusters (`tighten_mean`, worst-seed `tighten_max`).
- **Regime map**: no single attraction/wall ratio both clusters scattered
  starts and polishes placed boards — cluster at ka≈0.05/wall≈0.02–0.03,
  polish at ka≈0.008–0.01/wall≈0.08–0.1
  ([`si_force_laws.md`](si_force_laws.md) §3).
- **Merge gates**: same-or-better overlap + iterations vs `main`, no
  sustained oscillation, suite green (`PLAN.md`).

### Test & player vocabulary

- **Shear scenario**: locked part grazing one pad of a 2-pad mover —
  torque + travel (~49°) exposing sign/pivot errors in frame math
  (`tests/test_motion_roundtrip.py`).
- **Roundtrip**: player `translate·rotate` formula vs `apply_deltas`
  agreeing to 1e-9 per frame (also: SVG-file pose match, params
  serialize match, ghost S-expr match — each test names its sense).
- **Magnet heuristics**: tightening + zero-overlap asserted across seeds
  and gains, relational not exact (`tests/test_magnet.py`).
- **RV1 scenarios**: DCCF slide-pot regression — migration out of its
  76 mm courtyard, attraction-vs-wall balance at the edge
  (`tests/test_courtyard_behavior.py`).
- **Markers** (`pyproject.toml`): `physics` (needs nets), `dccf`
  (97-footprint board), `server`, `regression`, `unit`, `params`,
  `minimal`, `cli`.
- **Retired names** (do not use): `repulsion_kr` → `repulsion_k_N_m2`,
  `attraction_ka` → `attraction_k_N`, `courtyard_repulsion_kc` →
  `wall_k_N`, `courtyard_halo_mm` → `wall_halo_m`,
  `boundary_repulsion_kb` → `boundary_k_N`,
  `convergence_eps_mm` → `convergence_eps_m`,
  `friction_threshold_mm` → `friction_N`, `max_step_mm` → `vmax_m_s`.
  `from_dict` still silently drops these for old files.

## Stage: route — the autorouter core

The routing task (from [`wikipedia/autorouter.md`](wikipedia/autorouter.md)): create
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
  ([`wikipedia/maze_routing.md`](wikipedia/maze_routing.md)).
- **Lee algorithm**: BFS wave expansion over that grid — mark start `0`, repeatedly mark
  unlabeled neighbors with the next index until the target is reached or no points remain,
  then backtrace through decreasing marks. Always optimal if a path exists, but slow and
  memory-hungry ([`wikipedia/lee_algorithm.md`](wikipedia/lee_algorithm.md)). A* search
  ([`wikipedia/a_star_search_algorithm.md`](wikipedia/a_star_search_algorithm.md)) is
  commonly used in maze/Lee routers to cut the cost.
- **Global routing**: first pick an approximate course per net on a coarse grid (optionally
  assigning layers), then do detailed routing cell by cell — limits the size of the hard part.
- **Rip-up and reroute**: route nets in sequence; if some fail, remove selected routings,
  reorder, retry until all nets are routed or we give up.
- **Iterative improvement**: treat shorts/violations as finite costs in an objective
  function with per-pass weights (early passes penalize wire length, later passes penalize
  violations heavily), rip-up and reroute each net to minimize it.
- **Push-and-shove** ("shove-aside"): move already-routed nets out of the way to make room —
  the interactive-router feature; a useful cleanup idea for our core too.
- **Smoothing / string-pulling**: post-process a grid path by shortcutting intermediate
  nodes where straight line-of-sight is walkable. The 4-directional A* produces staircases;
  smoothing replaces them with fewer, longer any-angle tracks. Our implementation uses
  iterative bidirectional greedy passes + outward Steiner detour repair around blocking
  lumps. Configurable **angle granularity** (`angle_step_rad`, default 10° = π/18)
  replaces hardcoded 45° multiples, giving 36 angle options for shallower routes.
- Secondary objectives that conflict with shortest path: crosstalk, via count, metal
  density, timing.

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

## Stage: drc — design rule check

- **Design rule matrix**: per-layer minimum track width, clearance between tracks/pads,
  via sizes, etc. The DRC stage validates the routed board against it.
- `kicad-cli pcb drc <board> --format json -o report.json` produces a JSON report with
  top-level `violations[]` and `unconnected_items[]`; `--exit-code-violations` makes the
  exit code reflect violations, not just load failures (see [`kicad/cli.md`](kicad/cli.md)
  and `src/kicad_autorouter/validate.py`).
- Example violation (from the old demo-track pass): `track_dangling | Track has unconnected end`.
- **Ignored violation classes**: the UI overlay and `DRCResult.filter_violations()` hide
  non-routing noise by default (`DEFAULT_IGNORED_TYPES` in `src/kicad_autorouter/drc.py`,
  shared by the SVG overlay): silkscreen (`silk_over_copper`, `silk_overlap`,
  `silk_edge_clearance`), library/footprint metadata (`lib_footprint_issues`,
  `lib_footprint_mismatch`, `footprint_filters_mismatch`, `footprint_type_mismatch`,
  `missing_courtyard`), and router-irrelevant geometry (`track_not_centered_on_via`,
  `tuning_profile_track_geometries`). Counts shown in the UI are post-filter;
  the raw `kicad-cli` JSON report in `tmp/` keeps everything.

## Stage: writeback — emit updated `.kicad_pcb`

- Write new tracks (and moved footprints) back as S-expressions. In the current pass this
  is pure Python in `.venv`: per-footprint deltas are applied by UUID via
  `io.nudge_footprint_by_uuid` and serialized with `sexpr.to_sexpr`;
  `pcbnew_adapter.py` is reserved for future routing-accurate semantics.
- Always re-run DRC after writeback — it is the acceptance check for a pass.

## Force-directed placement of unlocked components

> Superseded by "Stage: place" above on `feature/si-dynamics`: Hooke
> springs are gone (constant-magnitude attraction) and the stepper is
> semi-implicit Euler + Kabsch projection in SI units. Kept for the
> ghost / nudge / complexity notes, which still hold.

Basis: [`wikipedia/force_directed_graph_drawing.md`](wikipedia/force_directed_graph_drawing.md).
Treat footprints as nodes and nets as edges; run a physical simulation until equilibrium:

- **Attraction**: spring-like force (Hooke's law) along each net edge pulls connected
  footprints toward each other — shortens the wires the router must draw.
- **Repulsion**: Coulomb-like force between all node pairs pushes unconnected footprints
  apart — keeps clearances and avoids overlap.
- **Fixed nodes**: locked footprints are pinned (infinite mass); only unlocked ones move.
  This is exactly our "nudge around the locked parts" step.
- **Convergence**: iterate until positions stop changing; damping/step-size schedules
  control stability (Fruchterman–Reingold temperature schedule). Simulated annealing or
  stress majorization are alternatives if plain simulation stalls in a bad local minimum.
- **Ghost (pseudo-part)**: a fixed anchor that participates in placement forces but is
  never moved or written as copper — a **wall** (courtyard-only obstacle that pushes
  parts away, e.g. a keepout) or an **attractor** (pulls specific refs via
  `(property "ghost_attract_N" "TARGET,ka")` springs). Serialized as a regular
  `(footprint ...)` plus `(property "ghost" "yes")`, courtyard on `F.CrtYd` (or `F.Fab`,
  which the parser falls back to); created with `ghost.make_ghost_wall /
  make_ghost_attractor` + `io.add_ghost_to_tree`, fully managed from the UI Ghosts panel
  (move / rotate ±15°, resize W/H steppers, delete; rotation pivots about the anchor).
- **Manual nudge**: hand-shift the placement selection by 1 mm (arrow keys / nudge pad,
  Shift for 10 mm) and rotate it ±15° (Shift for 90°) via `POST /api/placement/nudge` —
  same dual-write, rip-up (protected kept), and Undo semantics as Accept.
- **Complexity budget (vectorized)**: naive all-pairs repulsion is O(n²) per iteration,
  O(n³) total — at n=500 that is ≈125k pairs/iteration, tens of seconds in pure Python.
  Computing it as numpy broadcasting over the dense (n,n) distance matrix costs ≈1–2 ms
  per iteration → sub-second total for a full run; net-edge attraction stays sparse O(E),
  vectorized with edge index arrays, and locked footprints are a boolean mask (zero force).
  Dense n² is therefore plenty at "a few hundred" nodes; Barnes–Hut
  ([`wikipedia/barnes_hut_simulation.md`](wikipedia/barnes_hut_simulation.md)) only becomes
  necessary past ~3–5k nodes.

## Source index

| Local file | Used for |
| --- | --- |
| [`wikipedia/autorouter.md`](wikipedia/autorouter.md) | Routing task, failure modes, complexity, router taxonomy, global/detailed + rip-up strategies |
| [`wikipedia/maze_routing.md`](wikipedia/maze_routing.md) | Grid maze routing model |
| [`wikipedia/lee_algorithm.md`](wikipedia/lee_algorithm.md) | BFS wave expansion, optimality vs cost |
| [`wikipedia/force_directed_graph_drawing.md`](wikipedia/force_directed_graph_drawing.md) | Spring/Coulomb placement model for unlocked footprints |
| [`wikipedia/barnes_hut_simulation.md`](wikipedia/barnes_hut_simulation.md) | Barnes–Hut O(n log n) approximation of all-pairs repulsion; scaling path past ~3–5k nodes |
| [`wikipedia/a_star_search_algorithm.md`](wikipedia/a_star_search_algorithm.md) | Heuristic grid search (f = g + h); cost cut for maze/Lee routing |
| [`kicad/kicad.md`](kicad/kicad.md) | `.kicad_*` file types, S-expression formats |
| [`kicad/pcbnew.md`](kicad/pcbnew.md) | Footprints, pads, nets, tracks, zones, DRC in the PCB editor |
| [`kicad/cli.md`](kicad/cli.md) | `kicad-cli pcb drc` / `sch erc` JSON reports and exit codes |
| [`src/kicad_autorouter/routing/smooth.py`](src/kicad_autorouter/routing/smooth.py) | String-pulling smoothing with configurable angle_step_rad (10° default), bidirectional greedy + Steiner detour repair |
| [`tests/test_analytical_contact.py`](tests/test_analytical_contact.py) | SAT courtyard collision test suite (rectangle/circle SAT) |
