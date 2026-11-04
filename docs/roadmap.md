# Roadmap

Open work only. Completed changes belong in git history, not here.

## Placement

- **DCCF long-budget regrowth**: `wall_k_N` 0.08 → 0.1 (plus p=2.0) has
  landed in the engine defaults (sweep `wall_hardening_2026-10-26.json`:
  same-k DCCF cells drop 9–10 pairs / ~36–68 mm² to 1–2 pairs / ~8–12 mm²).
  The C10/U2 regrowth (~1.8 → 6.1 mm²) no longer reproduces — C10/U2 reads
  0.0 at 200 and 1000 iters under both halos. Dense re-probe at honest
  defaults (`scripts/plot_regrowth_forces.py`,
  `docs/figures/fig_regrow_wall_sweep.png` +
  `fig_r11u2_force_balance.png`): k ≥ 0.10 holds 1000-iter runs clean
  (8.2 mm², fixture D5/Q2 only; stable across 900/1000/1100 budgets),
  while k < 0.10 is a chaotic zone (12–30 mm², R11/U2 driver, 900–1100
  spreads overlap — a threshold region, not a cliff). Repulsion is
  load-bearing: the sweep script's neutered 1e-15 regime grows R11/U2
  0 → 11.4 plus RV1/U2 and C19/U4, while engine-default 2e-9 holds clean
  and 10x (2e-8) is slightly worse (12.3) — Coulomb keeps the pileup off,
  but over-stiffening churns elsewhere. Halo 4 mm is near-optimal: mean
  wall force per part falls as halo widens, areas show no improving trend
  past 4 mm, and halo ≥ 5 mm grows the RV1/U1 wedge (→ ~4.7 mm² at 6 mm).
  Geometry transects show U2 truly wedged: both R11-side and locked-RV1-side
  wall ramps cross flat ~0.05–0.06 N attraction, with overlap ending just
  past each crossing. All DCCF runs still end `budget` with multi-newton
  residuals — the 200-iter audit samples mid-churn. Next: gate any
  `wall_k_N` move on the dense-k 1000-iter pair set at defaults plus
  press-probe theory match and magnet-clustering parity, or go targeted
  (one-sided edges on the U2 cluster); also rescale the sweep script's
  `base_kw` repulsion to the engine default so future sweeps are not
  confounded by the neutering.
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
  phase (B-spline or clothoid) while preserving clearance. Landed subset:
  tangent-arc corner fillets (`arc_fillets`, default off) — the smoother
  ranks distance/angle ties by admitted fillet radius and the biarc fitter
  rounds clearance-gated corners into native `(arc)` copper. Open: curved
  *paths* (arcs as search primitives, not post-pass), push-aside.
- **R10 push-and-shove**: move already-routed nets to make room for later
  nets, with finite violation costs and iterative improvement.
- **R11 criticality ordering**: rank nets by sensitivity/difficulty before
  push-and-shove or adversarial routing.
- **Layer assignment**: prefer F.Cu for signals and B.Cu for return paths.
- **VIN on DCCF**: VIN crosses same-footprint pads of other nets around Q1
  and the tight Q2 neighborhood. Landed: no-net halos, `unconnected-*`
  handling, foreign-SMD hard exclusion, conservative track half. Remaining
  suspects: the own-cell secondary toll and narrow inter-pad gaps.
  Corridor guidance now finds the Q1–Q2 thread (capsule-exact halos
  survive ANY-blocked-supercell coarsening at 4x/2x); full VIN routes in
  ~9 s / 611k expansions (was 0/4 at 4M-budget fail), DRC 11 → 16
  (+4 zone-pour artifacts of the pre-existing class, +1× 24 µm pad
  graze at 0.176 vs 0.2 mm — copper untouchable, zero shorts).
  2026-10-26 benchmarks: full-VIN tight-spot 0/4 (was 4/4 on 10-21;
  own-cell secondary 500 → 10000 after the foreign-SMD hard exclusion);
  one DCCF power net now fails at ~4M expansions / 25 s (was routed,
  951k / 9 s);   routing GND (66 terminals) as a signal net burned ~86M
  expansions / ~9 min and failed with the whole grid tolled and corridor
  never triggering.   Landed: `max_terminals_per_net` tripwire in `route_nets` (default 16,
  `0` = no cap) with auto-split into `piecemeal_chunk_terminals` chunks
  (default 12, `0` = fail fast as before) and an explicit per-chunk failure
  reason. GND-66 chunks in ~9 s bounded (chunk 2 hits per-chunk budget)
  instead of the 86M-expansion / 9-min hang. Remaining: the maze itself —
  sealed tight clusters (own-cell secondary 10000, ANY-blocked supercell
  sealing) still defeat every chunk granularity; needs a thread-preserving
  finder or push-and-shove (R10) rather than smaller chunks.
  Follow-up: tube111 +12V (21 terminals) also hangs — >280 s with and
  without corridor, while the whole Oct-21 7-net row took ~8 s. The static
  grid is healthy (tolled 0.00, blocked ~0.16), so this is a search-dynamics
  regression for wide multi-terminal nets, not a blocked board; suspects
  are the Oct 23–25 fanout hardening, impedance screening, fill-only pours,
  and foreign hard exclusion. Landed 2026-10-31: piecemeal auto-split —
  the tripwire is now 16 terminals (uniform across net classes; no GND
  special case — without a pour GND is just another big net) with
  `piecemeal_chunk_terminals` 12, kept copper walkable + a valid A* goal
  plus a trunk connector between bands. Chunk-1 of +12V still burns its
  4M budget in ~23 s at any granularity (12/8/6): bounded and localized
  instead of a 280 s+ hang, but the maze itself is unmoved — the sealed
  tight cluster needs the search-side fixes below. Failed nets now return
  their routed chunks as display-only partial copper (orange overlay, never
  written back) with per-chunk reasons in the route diag, so the stuck
  region is reviewable alongside the debug cost grid. Publish no new
  corridor JSON until power rows finish in reasonable time again.
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
- **Track-tidy mode, deferred**: Tidy tab is placement-only today
  (footprint angle/gap refine, merges into Place → Accept), so routed-track
  cleanup has no home and users cannot tell when Tidy applies. Proposed:
  a track-tidy section in the Tidy tab operating on selected routed nets,
  proposal-based (nothing auto-applies): robust collinear merge, spike /
  double-back strip, via-pair → tee collapse (e.g. power track going up
  one via and down another where one tee suffices), corner-cut, stub
  prune, layer-stay. Clearance-first for signals (conservative corridor
  footprint, own-pads exempt; `drc_ok=false` proposals shown but never
  applied). Needs `routing/tidy_tracks.py` proposal generator +
  `POST /api/tidy/tracks` (propose) / `/apply` (subset commit, DRC-gated)
  + proposal cards in `app-tidy.js`. Parked — do other work first.

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
  and `ui/server.py` have grown past comfortable review size. Carve along
  existing seams (region simulation vs merge, job handlers vs state) with
  behavior-neutral moves, suite green per split. Landed:
  `routing/params.py` (config + validation + width/clearance),
  `ui/debug_grids.py` (cost/secondary payloads + unit tests),
  `board_model/` package (`model`/`query`/`parse` + re-export shim) —
  all via leaf-first moves with import-surface checks.
- **UI split landed** (2026-10-28): `app.js` (4187 lines, over the 4000
  budget) is now one domain file per panel — `app-core/viewer/params/nets/
  routing/diagnostics/placement/docs.js` + a 140-line `app.js` entry point
  (element cache, tabs, shortcuts, boot). Verbatim moves, zero import-site
  churn (classic scripts share globals), no-duplicate-names byte-scan per
  commit, `./scripts/check_js.sh` (`node --check`) green throughout. The
  4000-line rule now holds for every file; revisit only if a domain file
  itself approaches the budget.
- **Aesthetic row-alignment regime**: third placement pass that refines
  eligible parts into straight rows (axis snap on position + coarse angle
  quantization for opted-in refs), scored so legality still dominates —
  rows never win over DRC-clean.
- **THT-vs-opposite-SMD overlap**: re-audit courtyard accounting for a THT
  part facing an SMD on the opposite layer — through-hole pins occupy both
  sides, so opposite-side overlap may undercount. Needs a fixture pair +
  legality case.
- **Partial rip-up/reroute of massive nets (PWR)**: count-chunk
  piecemeal landed (spatial bands, kept-copper join, trunk connector).
  Remaining: subnet selection by footprint set / region rect (chunk
  composition is row-major only), and true partial re-route (today a
  failed net keeps nothing; splice keeps only full passes).
- **Sharpen the chunked-equivalence test**: `test_routing_chunked.py:40`
  costs ~28s per run for a loose 0.8–1.2x ratio assertion with a
  documented chunked-vs-batched divergence. Marked `slow` + `dccf` so the
  fast loop skips it; sharpen to a tight bound or replace with a smaller
  deterministic equivalence probe so it can return to the default suite.

## Documentation

(none open — see `docs/tests.md` for the test map.)
