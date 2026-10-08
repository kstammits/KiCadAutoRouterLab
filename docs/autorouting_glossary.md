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
- **Zone**: a poured copper area (e.g. ground plane); an obstacle for routing.
- **Board outline / edge cuts** (`gr_line` on Edge.Cuts): the physical board boundary;
  routing must stay inside it.
- **Layer**: copper or technical layer, e.g. `F.Cu`, `B.Cu`. Design rules can differ per
  layer (widths, spacings).

## Stage: connectivity — net graph

- Build a graph with one node per pad and edges between pads of the same net (networkx).
  This is the input to routing: each net becomes a set of terminals that must be joined.
- **Terminal / pin**: synonym for pad in router literature; "pins on cells" are the
  pre-existing polygons a router is given, along with obstacles and optional preroutes.

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
  cells = components, zones, existing tracks. Find a chain of free cells from A to B
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
- Secondary objectives that conflict with shortest path: crosstalk, via count, metal
  density, timing.

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
