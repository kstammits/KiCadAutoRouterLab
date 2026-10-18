# Benchmark + behavior notes — 2026-10-15 full-scale runs

All five benchmarks ran full-scale today; JSON in `docs/benchmarks/*_2026-10-15.json`.
All timings below were measured on a 2022 Apple M2 chip.
New script: `scripts/benchmark_stitch_pour.py` (pour analysis + stitch repair,
budget sweep, determinism rerun). Also fixed a latent crash:
`benchmark_routing.py --with-stitch` passed `"DCCF+stitch"` as a fixture name
(`FileNotFoundError`); `time_case()` now takes a `label` override.
GUI pre-flight estimates in `ui/index.html` recalibrated to today's numbers
(placement was ~8x stale after the analytical-contact change; routing now shows
seconds + a stitch surcharge note).

No optimizations implemented tonight — notes only. Agreed order when we start:
**pour first**, then stitch, then disc-raster, then `is_empty`, then the
heuristic-weight experiment.

**Update (later same day): two cheapest items landed.** `is_empty` cache
(notes §7): tube111 35.8→24.1 ms/iter, DCCF 55.7→19.4 ms/iter; converged
25.4→15.1s / 0.91→0.44s. Disc painting via `stamp_discs` (notes §5+§6 combined
— the churn fix reduced to using the compiled stamper): tube111 pour
4.78→1.82s, DCCF 5.63→4.43s, stitch knock-on tube111 2.55→1.48s / DCCF
16.9→16.0s. Island/repair outputs bit-identical; placement + pour/stitch
suites green. `docs/benchmarks/*_2026-10-15.json` overwritten with post-fix
runs; GUI placement coefficient retuned to match. Still open: stitch EDT
hoists (§4), per-track alloc accumulation (§5 remainder), weight experiment (§1).

---

## 1. What `heuristic_weight` is, and what the bench actually found

**What it is.** Every A* pop orders nodes by `f = g + w·h`, where `g` is the
wire+penalty cost so far and `h` is Manhattan distance to the goal
(`router.py:415-417`, `CostMap.heuristic_weight`, default `3.0`, also
`pipeline.py:70`). `w = 1.0` is textbook A*: the heuristic never overestimates,
so the first time the goal pops the path is optimal. `w > 1` is "weighted A*":
greedier, usually *fewer* expansions in open space, but the path can be
suboptimal — and, crucially, an overestimating heuristic is *inconsistent*,
so the search can expand nodes and then have to **re-expand** them when a
cheaper `g` turns up later.

**What the bench measured** (`tight_spots_2026-10-15.json`, DCCF VIN):

| case | w=3.0 | w=1.0 |
|---|---|---|
| pair-control (C6–Q1) | 392k exp, 2.6s, cost 800 | **54k exp, 0.95s, cost 800** |
| pair-tight (Q1–Q2) | 159k exp | 159k exp (identical) |
| full-VIN | 272k exp, cost 25476 | 213k exp, cost 25476 |

Same success, same path cost, far fewer expansions at `w=1.0` on the control
pair. "Cuts expansions" = the search floods fewer grid cells before reaching
the goal, i.e. less CPU per net. Why inverted from the textbook (greedy should
expand *less*)? The codebase already knows: `router.py:409-414` says corridor-
restricted searches force `w=1.0` because "inside a narrow tolled corridor an
inflated heuristic thrashes (repeated re-expansion) instead of guiding."
The control pair's neighborhood is full of soft tolls (fanout halos,
courtyard/HIGH_COST); `w=3` charges goalward through tolled cells, hits the
expensive region, and re-expands massively. The tight pair is identical across
weights most likely because it already goes through the corridor path with the
forced `1.0` override. So the finding is really: **outside narrow corridors,
the default 3.0 thrashes in tolled neighborhoods the same way.**

**GUI/formula docs.** There is currently **no slider** for this — it is a
hidden `RoutingParams` field (not in `readRoutingParams`, not on the Route tab).
Proposed tooltip text if/when we expose it: *"A* greediness (f = g + w·h).
Lower respects tolls and usually searches less in dense boards; higher is
 greedier in open space. Bench: w=1 matches w=3 path cost with ~7x fewer
 expansions on DCCF VIN control (tight_spots_2026-10-15). Default 3.0."*
**Default unchanged tonight.** The user said to pick, but also no behavior
changes tonight — recorded proposal: try default `1.0`, gated on (a) full
routing regression green, (b) a broader sweep (tight-spot bench only covers
VIN; need tube111 signal nets + a DCCF sample), (c) path-cost parity check.

## 2. VIN on DCCF routes across same-footprint, different-net pads (bug report)

Observed: VIN tracks downward across inappropriate pads of the same footprint
(different nets). Facts gathered (no fix tonight):

- `VIN = C6.1 – Q1.1 – Q2.1`. Q1 is a THT row marching in −x:
  pad 1 VIN (236.8, 218.5), pad 2 GND (234.3), pad 4 −5V (229.2), pad 5 GND
  (226.7), pad 6 +5V (224.1). A track leaving Q1.1 downward/leftward has to
  thread this row — any slack crosses a foreign pad.
- Q2.1 (the "tight" terminal) is a 0.4×1.35mm SMD pad with GND pads 5/S1–S6
  within ~2mm, plus `unconnected-(Q2-PadN)` pads (note: `unconnected-*` net names
  — check how obstacles treats them: foreign copper or ignored?).
- Bench smell: `spot_facts` reads VIN terminal Q2's own cell as
  `own_cell_secondary=500` (INNER halo) with `via_blocked=True`. If the
  terminal's own cell carries a 500 toll, the search is actively pushed *off*
  its start cell toward neighbors — worth checking whether own-net exclusion
  covers this cell (`route()` docstring says own pads are excluded at build
  time; the 500 says otherwise for this cell — open question, not verified).
- Suspect list for the crossing itself: foreign-THT-pad marking in
  `obstacles.py` (`_mark_pad` THT branch — BLOCKED disc vs HIGH_COST halo?),
  `unconnected-*` net handling, grid-resolution gaps between adjacent pads at
  0.1mm, or the own-cell toll above redirecting the path. Repro: route VIN on
  DCCF and dump the corridor cells around Q1 pads 1–2.

## 3. DCCF GND pours are indeed half-missing (user's memory correct)

`DCCF.sved.kicad_pcb` has **exactly one** GND zone (both layers, clearance
0.508, bbox x 41.3–169.6, y 37.4–254.6) but **two** board regions: panel 1
x 44.7–144.7, panel 2 x 175.5–275.5. The zone covers panel 1 fully and panel 2
not at all (ends at x 169.6; panel 2 starts 175.5). All VIN parts (Q2 x181,
Q1 x237, C6 x245) sit in the unpoured panel. Consequences measured today:
stitch on DCCF placed **0 overlap vias** (nothing to overlap where there is no
pour) and repaired only 53/104 islands via 13 bridges, burning 16.9s to do it.
Fix is in pcbnew (add the zone to region 2), not in code — but note the
code is currently spending ~17s repairing a board whose pours are incomplete.
Do not "fix" the planner to compensate; fix the fixture (or accept the cost).

## 4. `plan_stitches` proposal (agreed smart — second after pour)

DCCF cProfile of `plan_stitches` (18.8s): `distance_transform_edt` 99 calls /
~8.7s, `binary_erosion` 115 calls / 2.4s, `joined_mask` rebuild 201 calls /
0.7s, `_astar_path` only ~0.5s. The bridges are *not* the problem — per-group
full-grid numpy ops are:

- `dist_to_joined = ndimage.distance_transform_edt(~jmask)` runs **per group
  per layer** (~50 groups × 2). Hoist/restrict: compute on the group bbox +
  `max_bridge_mm` window instead of the whole 4.8M-cell grid.
- `binary_erosion(copper[layer])` in the overlap-via siting is loop-invariant
  per layer — hoist out of the group loop (this alone recovers most of the
  2.4s).
- `joined_mask()` re-ORs every joined island per group (O(groups²) full-grid
  ops) — keep an incremental cached mask, OR in each newly joined group.
- Expected combined effect: stitch DCCF 17s → low single digits; tube111 2.6s
  → well under 1s. No behavior change (same siting math, less redundant work).

## 5. `pour_copper_mask` raster churn (do FIRST — low-hanging fruit)

`pour_copper_mask` loops zones, and **per zone** re-cuts *all* foreign tracks:
per track it allocates a fresh full-grid `cut = np.zeros((H,W))`, paints one
capsule, `copper &= ~cut` (`pour.py:171-180`; same pattern for vias/drills).
tube111's 487 tracks × zones × layers = thousands of 1.5M-cell alloc+AND pairs.
Measured signature: tube111 pour 1596 ms/Mcell vs DCCF 588 ms/Mcell on a 3.2x
*smaller* grid — cost follows track count, not cell count. Fix: accumulate all
cuts into **one** mask per zone (paint every track/via/drill into it, single
`copper &= ~cut`), same cells, ~all of the redundant allocs gone. Pure
mechanical change; island-count characterization tests (`test_pour.py`)
pin the output.

## 6. `_paint_disc` cost, explained

Painting a disc means setting every grid cell with `dr²+dc² ≤ r²` around a
center. `pour.py:_paint_disc` does this through `disc_cells`, a **Python
generator that yields one `(r, c)` at a time**, with `mask[rr,cc]=True`
executed per cell in Python (`pour.py:123-128`; `_paint_pad`/`_paint_track`
same pattern). DCCF stitch profile: 850 `_paint_disc` calls → **9M generator
iterations** (`disc_cells` 0.52s + `_paint_disc` body 1.1s + `astype`/`reduce`
overhead). Cost scales with copper object count × disc area — dense boards pay
per pad per via per call site, and pour/stitch call these helpers repeatedly
(`_foreign_mask`, `_drill_mask`, seed painting, barrel cells).
The punchline: a compiled version **already exists**. `obstacles.py:360-423`
has `_stamp_discs_nb` (`@njit`, "compiled twin of the `disc_cells` scan …
same loop bounds … bound-clipped") plus the `stamp_discs` batch wrapper that
stamps *all* centers/radii in **one** compiled call — `pipeline.py:776`
already uses it. `pour.py` just predates it and was never converted. The
reasonable change is to route `pour._paint_disc` (and later the capsule walk
in `_paint_track`) through `stamp_discs`: identical cells (the twin
guarantees it), ~all of the 1.6s+ Python-loop overhead gone, no behavior
change. Numba is already a hard dependency.

## 7. `is_empty`, explained (placement hotspot)

`is_empty` is a shapely predicate: "does this geometry contain no points."
In `placement.py` it guards the courtyard contact path — e.g. line 2012,
`if pi.is_empty or pj.is_empty: return` skips the pair before calling
`nearest_points(pi, pj)` (which would throw or return garbage on empty
input); same guard pattern at lines 783/815 (outline classification),
853, 2130, 2158–2199, 2239 (pin/body polygons). So it captures **"this side
has no courtyard/pin geometry,"** i.e. missing courtyards, single-pad parts
with no outline, ghost/marker footprints — a real condition, not a
placeholder.
Is it static? The inputs `pi, pj` are **posed per-iteration** polygons (rigid
transform of the outline by the current position), and posing rebuilds the
shapely object every iteration — hence 217k `is_empty` calls (with shapely's
decorator overhead, ~0.45s of the 1.36s `_compute_total_force` on the tube111
profile). But: an affine pose **cannot change emptiness** — a non-empty
outline stays non-empty no matter where it moves. Emptiness depends only on
the footprint's outline data, which is fixed for the board. So yes, cacheable:
compute one `has_courtyard` bool per footprint once per `run_placement` (or
per board) and skip both the per-pair predicate calls (and, where the polygon
is only built for this check, the rebuild). Only caveat: if outlines can be
edited mid-run (they can't — the model is immutable during simulation),
invalidate accordingly. Also consider `prepare()`/PIP-vectorized emptiness
later; the bool cache is the cheap 80%.

## 8. Is 1.2 s/net reasonable for DCCF? (routing estimate)

Short answer: **it is the small-net marginal, not the DCCF working average —
for your DCCF work it reads optimistic, and the data says why.** From
`routing_2026-10-15.json`: DCCF 1/2/5/10 nets took 3.3/4.1/6.7/10.6s, i.e.
~2.5s fixed (occupancy + per-net `via_ok` 0.54s + grids) + **~0.8s marginal**
per small signal net. 1.2s has ~50% headroom over that — fine for small nets.
What you feel on DCCF is the heavy tail the average hides:

- Big nets cost multi-seconds each: full-VIN alone is ~2.2s (272k expansions);
  the tight-spot control pair at default weight is 2.6s for *one pair search*.
- Failed nets burn to the 500k-expansion cap: ~3s+ each at ~100–150k exp/s —
  one stubborn net costs more than three easy ones.
- The per-net `via_ok` mask rebuild (0.54s on DCCF's 9.58M cells) is paid per
  net regardless of size.
- With GND stitching on (the default), add the §3 surcharge: **+17s on DCCF**.

So: keep the shipped `2 + 1.2·n` GUI formula for now (it is honest for small
nets and conservative-fixed-cost), but plan a per-net model next: seconds from
`est.pairs` (pad-count pairs, already computed in `estimateRoutingWork`) plus
a failed-net allowance, with stitch as its own line item (already added).
Revisit after the weight experiment (§1), which moves the biggest lever.
