# Roadmap

Open work only. Finished work lives in git history, not here.

## Placement (where parts go)

- **Label clearance calibration**: labels float on spring tethers and
  the orbit used to steer the Fab `fp_text` mirror instead of the
  DRC-visible `(property "Reference")` silk field — parse and writeback
  now track the property, mirrors stay glued, and ref-vs-ref / ref-vs-own
  silk overlaps on tube114 are zero. Residual: ~10 ref-field hits where
  a label sits on a neighbor's pad copper or hangs past Edge.Cuts
  (courtyard-only body model is blind there). Next: include pad-copper
  extents in the label contact bodies, then find a good default for
  `label_clearance_m` on real boards (tube111 subset first).
  Sweep script: `scripts/sweep_label_clearance.py`.
- **DCCF regrowth at long budgets**: the wall push (`wall_k_N` 0.1) and
  halo (4 mm) in the current defaults hold 1000-iteration runs clean.
  Still open: require any future wall change to pass the dense 1000-iter
  pair set first; fix the sweep script's weakened repulsion so future
  sweeps compare fairly. Alternative if this stalls: push on one side
  of the U2 cluster only.
- **Halo reach**: Legalize uses 4 mm, Cluster and the default use 6 mm.
  Run one full sweep before changing either default.
- **Default-parameter tests**: move the placement physics and courtyard
  tests off the old pinned settings onto current defaults. Test behavior
  (what holds), not exact numbers.
- **Selected-part highlight**: hovering a part in the table should
  highlight it in the board view.

## Routing (where copper goes)

- **Finer smoothing (was R9)**: corner fillets, broad sweeps, and the
  keep-cut kink guard are in, plus the `smooth_effort` slider (0 = raw
  paths, 2 = full default, 3 = high-effort retry) and the separate
  Polish action (re-smooth picked nets against the finished board,
  keep only what gets shorter and stays clean). Still open: search
  along curves directly instead of smoothing afterwards, and pushing
  copper aside.
- **Rip-and-relay ordering (was R11)**: refine trials already route
  last trial's failed nets first, and the headless signals stage
  retries with refine when trial 0 leaves failures (tube116: 50/10 ->
  53/7; R30 fixed). A casualty-repair relay re-routes nets the adopted
  winner broke (tube116 J1-Pad2: sealed even solo, so the repair
  correctly no-ops). Still open: ranking nets by difficulty up front.
True mid-run push-and-shove (was R10) is a bigger job — evicting
blockers mid-route needs a cost model for violations. Scoped
alternative landed: `routing/shove.py` retries GND-walled nets with
per-pin GND eviction (split, not amputation) plus restore-or-rollback
(unit-tested; tube116 stubborn-4 all route at search level, but the
strict GND gate rolls them back on tree-like rails where every
unrestored middle is a cut-edge). Residual-gap healing also landed:
unrestored cuts plus interior gap ends plus removed-barrel sites fly
one Tier-2 gap flight, arbitrated by the same fracture gate
(unit-tested adopt + rollback; tube116 gaps are sealed by the nets'
own new copper, so all attempts correctly roll back with zero board
drift; the stitcher only covers zoned pours). Tier-1 signal shove
also landed (sever routed signal blockers in pin boxes, adopt only on
victim-bit-identical restore, else gap-reconnect or rollback;
unit-tested; correct no-op on tube116 where stubborn boxes hold no
signal copper). Tier-2 gap reconnect landed with it: cut points plus
isolated pads as search terminals (connected pads excluded, else the
search joins the nearest pad without crossing the gap), kept stubs as
walkable seed, evicted-halo punching (stale halos seal the cleared gap
on the grid), and seeded coverage plus a layer-aware model-space check
plus the edge gate for adoption. Same-run fixes inside: the ledger now
pairs middles/nodes per layer+net (same-XY walls on both layers stole
one node and double-added it), synthetic test trees stamp the test
board's Edge.Cuts (the fixture outline collapsed reparse grids and
"routed" nets vacuously), coverage is layer-aware (a B.Cu middle
does not reconnect F.Cu SMD pads), and `route_gap_terminals` returns
the full 5-tuple on oversized grids (was a 4-tuple crash). Still open: the
  router's own edge keepout is a soft toll, so winning detours can hug
  Edge.Cuts (tube116 C1TubeOut: 5 edge errors) — the emit gate does not
  check edge clearance (shove gates it at a fixed 0.5 mm default).
- **Dense-board mazes**: the manufactured gate trips are fixed (the
  smoother no longer kinks walkable chords into copper), and the big
  tube rails route on the current board (+12V/−12V/GND in ~13 s).
  Leftover: sealed tight clusters can still beat A* at any chunk size;
   that needs a thread-preserving finder, not smaller chunks. Revisit
   only if it reappears — old benchmark runs live in git history.
- **Corridor guidance**: keep as-is. It only runs after the first
  search window fails, costs nothing elsewhere, and saves the VIN
  tight spot. No retuning needed.
- **Heuristic weight**: try 1.0 against the 3.0 default on tube111
  signals and a DCCF sample. Change the default only with zero routing
  regressions and equal path costs.
- **Routing time estimator**: the GUI's `2 + 1.2·n` guess should become
  a per-net pad-count estimate plus a failed-net allowance.
- **Tidy-regime calibration**: gate the Tidy defaults on a sweep —
  outlier counts down on tube111/DCCF, with no overlap or wire-length
  regression against legalize (wire length is the real score for
  straightening). Then publish the JSON under `docs/benchmarks/`.
- **Layer assignment**: prefer F.Cu for signals, B.Cu for return paths.
  Not started.
- **Track-tidy, parked**: Polish now covers re-smoothing. The rest —
  merging collinear tracks, stripping spikes and stubs, collapsing
  via pairs into tees — stays parked behind other work.

## Pour copper and stitching

- **Cross-net bridges**: user-declared only, never guessed from net
  names. Mounting holes stay unbridged (not electrical).
- **Faster pour cuts**: stop allocating a full-board mask per track
  when cutting foreign copper out of zones.
- **Width-aware rebuilds**: rebuilt grids mark old tracks centerline-only
  while fresh routes reserve the full width, so chunked and batched
  runs can disagree. Make rebuilds use widths too.

## Four-layer boards (viewer + plane-tap fanout landed)

Two-layer behavior is frozen. Four-layer routes outer-only, keeps the
inners, and taps planes for ground/power nets with inner zones.

- **Phase B nets table**: per-net clearance + width override editor.
- **Phase C per-net layers**: which copper layers each net may use
  (`net_allowed_layers`), enforced in search, soft-fail per net.
- **Phase D ground fanout**: dedicated `routing/fanout.py` for ground
  nets (own file, generalized beyond 2 layers). Keep it out of the
  presets until validated.

## Code health

- **Split large modules**: `routing/pipeline.py`,
  `placement/runner.py`, `ui/server.py` are hard to review. Carve along
  existing seams without behavior changes, suite green per split.
- **THT facing opposite-side SMD**: re-check courtyard overlap math
  when a through-hole part faces an SMD on the other side (pins occupy
  both sides). Needs a small fixture pair + legality case.
- **Partial re-route of huge nets**: chunking landed. Still open:
  picking chunks by footprint set or board region (not just row bands),
  and re-routing only the failed part (today a failed net keeps
  nothing).
- **Chunked-equivalence test**: `test_routing_chunked.py:40` is slow
  (~28 s) for a loose check. Sharpen it or replace it with a small
  deterministic probe so it can rejoin the default suite.

## Docs

None open — see `docs/tests.md` for the test map.
