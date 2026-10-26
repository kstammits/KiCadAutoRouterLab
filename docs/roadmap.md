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

## Four-layer support (retired spec; none landed)

No `fanout.py`, no `net_allowed_layers`, no `width_source`/solo in tree.

- **Phase A viewer (client-only)**: layer-name solo/isolate + copper
  legend through `applyLayerVisibility` (stay `display:none`-only, no
  refetch). Mirror `_INNER_DASHES` / `FILL_INNER_COLORS` exactly,
  including the B-outer `3 2` dash.
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

## Documentation

(none open — see `docs/tests.md` for the test map.)
