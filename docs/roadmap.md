# Roadmap

Open work only. Completed changes belong in git history, not here.

## Placement

- **DCCF long-budget regrowth**: `wall_k_N` 0.08 → 0.1 clears the RV1/U1
  wedge at the 200-iteration audit (sweep `wall_hardening_2026-10-26.json`:
  same-k DCCF cells drop 9–10 pairs / ~36–68 mm² to 1–2 pairs / ~8–12 mm²),
  but 1000-iteration runs regrow C10/U2 (~1.8 → 6.1 mm²). Needs anchor-side
  or budget-aware work.
- **Halo reach recalibration**: Legalize uses 4 mm while Cluster and the
  engine default remain 6 mm. A full sweep is still needed before changing
  either global default.
- **Default-parameter tests**: migrate the placement physics and courtyard
  suites off pinned legacy dynamics (`wall_exponent=1`, `momentum_alpha=0`)
  to the current defaults, asserting behavioral invariants rather than exact
  magnitudes.
- **Selected-footprint highlight**: highlight the selected footprint in the
  SVG on part-table hover.

## Routing

- **R9 smoother post-pass**: curve/spline interpolation after the stick
  phase (B-spline or clothoid) while preserving clearance.
- **R10 push-and-shove**: move already-routed nets to make room for later
  nets, with finite violation costs and iterative improvement.
- **R11 criticality ordering**: rank nets by sensitivity/difficulty before
  push-and-shove or adversarial routing.
- **Layer assignment**: prefer F.Cu for signals and B.Cu for return paths.
- **VIN on DCCF**: VIN crosses same-footprint pads of other nets around Q1
  and the tight Q2 neighborhood. Landed: no-net halos, `unconnected-*`
  handling, foreign-SMD hard exclusion, conservative track half. Remaining
  suspects: the own-cell secondary toll and narrow inter-pad gaps.
  Corridor guidance misses: the fine-grid walkable Q1–Q2 thread is sealed
  by ANY-blocked-supercell coarsening (no corridor at 4x or 2x); needs a
  thread-preserving finder or push-and-shove.
  2026-10-26 benchmarks: full-VIN tight-spot 0/4 (was 4/4 on 10-21;
  own-cell secondary 500 → 10000 after the foreign-SMD hard exclusion);
  one DCCF power net now fails at ~4M expansions / 25 s (was routed,
  951k / 9 s);   routing GND (66 terminals) as a signal net burned ~86M
  expansions / ~9 min and failed with the whole grid tolled and corridor
  never triggering. Landed: `max_terminals_per_net` fail-fast guard in
  `route_nets` (default 32, `0` = no cap) with an explicit failure reason.
  Remaining: piecemeal strategy for massive power nets (subnet selection
  routed against existing copper, per Future work below) instead of
  all-or-nothing single flight.
  Follow-up: tube111 +12V (21 terminals, under the cap) also hangs —
  >280 s with and without corridor, while the whole Oct-21 7-net row took
  ~8 s. The static grid is healthy (tolled 0.00, blocked ~0.16), so this
  is a search-dynamics regression for wide multi-terminal nets, not a
  blocked board; suspects are the Oct 23–25 fanout hardening, impedance
  screening, fill-only pours, and foreign hard exclusion. Keep the cap at
  32 (it only trips on plane-scale nets) and fix the search rather than
  lowering it; publish no new corridor JSON until power rows finish in
  reasonable time again.
  Quantified 2026-10-26: softening all hard secondary rings to toll level
  restores +12V in 8.2 s / 1.29M expansions (Oct-21 parity), but DRC on
  that board jumps 174 → 481 violations (+229 clearance) — Oct-21-era
  routability was violation-tolerant. w=1.0 control still fails (25.7M
  expansions at 100k cap): not guidance thrash, true maze. Shrinking the
  hard radius by track_half (R1) and capping all hard stamps at 2000 toll
  (R2) both still hang. An 11-terminal +12V subset routes DRC-clean in
  3.4 s / 447k expansions under hard rings, so clean subpaths exist and
  the full-net failure is search-scale: piecemeal (route subsets, splice)
  is the DRC-clean restoration path. Normal baseline: DCCF B1/B2 ~2.7 s /
  ~11k expansions, tube111 C1InCVLED 1.3 s / 51k.
- **Heuristic-weight experiment**: sweep `heuristic_weight` 3.0 vs 1.0 on
  tube111 signal nets and a DCCF sample; gate on routing regressions and
  path-cost parity before changing the default.
- **Routing time estimator**: replace the GUI `2 + 1.2·n` model with the
  per-net pad-count pair estimate plus a failed-net allowance.

## GND pours and stitching

- **Impedance cross-net mates**: user-declared bridges join across net
  names (names differ by design to fool KiCad DRC); mates are never
  inferred from net names — positional suggestions + manual linking only.
  Faceplate mounting holes stay unbridged (non-electrical) by policy.
- **`pour_copper_mask` foreign cuts**: accumulate different-net cuts into
  one mask per zone instead of allocating and ANDing a full `zeros(H, W)`
  per track.
- **Rebuild width-awareness**: rebuilt grids mark pre-existing tracks
  centerline-only (`obstacles._mark_track` has no width inflation) while
  in-batch `_apply_route_to_grid` reserves the full emitted-width corridor,
  so later chunked nets may take slightly tighter paths than batched ones.
  Make rebuilds width-aware so the flows converge.

## Four-layer support (part landed: viewer solo + plane-tap fanout)

Two-layer mode retains prior behavior verbatim (outer-only routing, collapse
on export, no plane fanout). Four-layer mode routes outer-only but preserves
inners and taps them: `fanout_planes` (two-layer `False` / four-layer `True`,
both mode-owned via `strategy.py`) short-circuits ground/power-effective nets
owning an inner zone to SMD stubs + through-vias (`fanout_plane_vias_max`
cap, default 16; THT barrels need no stub) instead of long outer MST tracks.
Counts are additive (`fanout_vias_added` / `fanout_tracks_added`).

- **Phase A viewer (landed)**: per-layer checkboxes + Solo/All isolate and a
  copper legend through `applyLayerVisibility` (`display:none`-only, no
  refetch). Dashes/colors mirror `_INNER_DASHES` / `FILL_INNER_COLORS`
  including the B-outer `3 2` dash.
- **Remaining**: Phase B nets-table widths/clearances, Phase C per-net
  `net_allowed_layers`, full Phase D `routing/fanout.py` (GND-only SMD
  hardening, stitcher-predicate extraction, `size/2 + clearance` vs
  `track_half` reconciliation).
- **Phase B nets table**: additive `clearance_mm` + `width_source`
  (`net` / `netclass:<name>` / `power` / `default` via
  `resolve_track_width_mm` + `extra_power`; `clearance<=0` falls back
  to default) plus per-net width override editor over
  `POST /api/routing/params`. Optional `net_clearances` threads into
  `resolve_clearance` only — base-grid halo stays global (assert
  non-inflation in tests).
- **Phase C per-net layers**: `RoutingParams.net_allowed_layers`
  (absent = unrestricted, list-copy both directions). Effective set =
  grid ∩ global `allowed_layers` ∩ per-net; enforce as an index skip
  in `SingleNetRouter._a_star` (same-layer moves + via transitions, no
  cost-grid copies). Unknown or valid-but-unroutable-inner entries
  soft-fail that net via `failed_reasons` (never raise — global stays
  fail-fast `ValueError`). Fix `_terminals_for_net` silent idx-0
  remap; picker sourced from `copper_layers` needs an inner-layer
  warning (grid is outer-only).
- **Phase D GND fanout**: new `routing/fanout.py`
  `plan_gnd_fanouts` — ground-nets-only, SMD-terminals-only, through
  `F.Cu`–`B.Cu`, capped per net (`fanout_vias_max`, default 16),
  pour-gated clean no-op. One pass per `route_nets` before the per-net
  loop, halo reserve via the `_apply_route_to_grid`-style path, count
  as additive `fanout_vias_added`. Extract (don't copy) the stitcher
  overlap-via predicates and generalize beyond 2 layers; reconcile
  `size/2 + clearance` vs `track_half`; add edge/`BLOCKED` bounds.
  Keep `fanout_gnd_vias` (default off) out of mode presets until
  validated.

## Future work

- **Split large modules**: `routing/pipeline.py`, `placement/runner.py`,
  `ui/server.py`, and `board_model.py` have grown past comfortable review
  size. Carve along existing seams (region simulation vs merge, job
  handlers vs state, parse vs query helpers) with behavior-neutral moves,
  suite green per split.
- **Aesthetic row-alignment regime**: third placement pass that refines
  eligible parts into straight rows (axis snap on position + coarse angle
  quantization for opted-in refs), scored so legality still dominates —
  rows never win over DRC-clean.
- **THT-vs-opposite-SMD overlap**: re-audit courtyard accounting for a THT
  part facing an SMD on the opposite layer — through-hole pins occupy both
  sides, so opposite-side overlap may undercount. Needs a fixture pair +
  legality case.
- **Partial rip-up/reroute of massive nets (PWR)**: today a net routes
  whole. Scope subnet selection (by footprint set, region rect, or
  failed-segment list), route only that subset against existing copper as
  obstacle, splice into the tree. Keeps big-rail iteration cheap.

## Documentation

(none open — see `docs/tests.md` for the test map.)
