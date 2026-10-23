# Roadmap

Open work only. Completed changes belong in git history, not here.

## Placement

- **DCCF long-budget regrowth**: the Legalize preset's 4 mm halo clears the
  RV1/U1 wedge at the 200-iteration audit, but 1000-iteration runs regrow
  C10/U2 (~1.8 → 6.1 mm²). Needs anchor-side or budget-aware work.
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
  and the tight Q2 neighborhood. Suspects: foreign-THT marking, `unconnected-*`
  handling, the own-cell secondary toll, and narrow inter-pad gaps.
- **Heuristic-weight experiment**: sweep `heuristic_weight` 3.0 vs 1.0 on
  tube111 signal nets and a DCCF sample; gate on routing regressions and
  path-cost parity before changing the default.
- **Routing time estimator**: replace the GUI `2 + 1.2·n` model with the
  per-net pad-count pair estimate plus a failed-net allowance.

## GND pours and stitching

- **DCCF fixture pour**: the single GND zone covers panel 1 only; panel 2
  (VIN parts) has no pour. Add the second zone in pcbnew so stitching does
  not spend time repairing an incomplete board.
- **`pour_copper_mask` foreign cuts**: accumulate different-net cuts into
  one mask per zone instead of allocating and ANDing a full `zeros(H, W)`
  per track.
- **`plan_stitches` hoists**: move `binary_erosion(copper)` out of the group
  loop, compute the windowed EDT on the group bbox, and cache `joined_mask`
  incrementally.

## Documentation

- **`docs/tests.md`**: per-file intent table, markers, and pytest
  instructions. Keep generated HTML reports untracked.
