# KiCad AutoRouter - TODO




### Contact Law Unification (landed 2026-10-15)

- Follow-up (documented residual, NOT contact): R13/RV1 stable squeeze wedge (southern halo field + net anchor across RV1 exceed the edge hold at every exit point; equilibrium is stiffness-independent). Needs anchor-side or placement-level work, not wall-law work.
- Follow-up: halo reach `h` is the only remaining distance lever (shorter `h` drops distant squeezers); global change, needs recalibration — do not touch without a sweep.
- [ ] Wall-law follow-through (testing/docs/figures): the unified law landed,
  but still needs regression coverage of the min-s winner-take-all path beyond
  the C14/RV3 probes, and the glossary wall entry completion
  (see Documentation below).
  (Progress: `docs/figures/` regenerated from current code + committed —
  the old PNGs differed from fresh regen across the full canvas on all 7
  figures, i.e. genuinely stale, not render noise. PNGs now RGB-optimized
  via `_save_optimized` in `plot_force_laws.py`.)


### Routing
- Roadmap: PLAN.md R10 (push-and-shove), R11 (net ordering by criticality).
- [ ] Layer assignment heuristic (prefer F.Cu for signals, B.Cu for return paths)
- [ ] VIN-on-DCCF bug (reported 2026-10-15, NOT fixed): VIN tracks across
  same-footprint different-net pads (Q1 THT row: VIN pad 1 vs GND/−5V/+5V pads
  2/4/5/6; Q2 tight SMD neighborhood). Suspects: foreign-THT marking in
  `obstacles._mark_pad`, `unconnected-*` net handling, own-cell secondary=500
  toll on Q2 terminal pushing the path off-start, 0.1mm inter-pad gaps.
  Repro: route VIN on DCCF, dump corridor cells around Q1 pads 1–2.
  Details: `docs/benchmark_notes_2026-10-15.md` §2.
- [ ] DCCF fixture: single GND zone (x ≤ 169.6) covers panel 1 only; panel 2
  (x 175.5–275.5, all VIN parts) has NO pour — add the zone in pcbnew.
  Until then stitch burns ~17s repairing an incomplete board (0 overlap vias,
  53/104 islands via bridges). Details: notes §3.
- [ ] Heuristic-weight experiment (NOT a default change): sweep default 3.0
  vs 1.0 over tube111 signal nets + DCCF sample; gate on routing regression
  green + path-cost parity. Bench motivation: w=1 matches w=3 cost with ~7x
  fewer expansions on DCCF VIN control. No UI slider exists yet (hidden
  `RoutingParams` field); proposed tooltip text in notes §1.

### Benchmarks & Measured Optimizations (2026-10-15 full-scale runs)
- Details + explanations: `docs/benchmark_notes_2026-10-15.md`. JSON:
  `docs/benchmarks/*_2026-10-15.json` (new: `stitch_pour_*`).
- [x] FIRST: `pour_copper_mask` raster churn — DONE 2026-10-15 via the
  cheaper half of the fix: `_paint_disc`/`_paint_pad`/`_paint_track` now route
  through the existing numba `stamp_discs` (`obstacles.py:360-423`) instead of
  per-cell Python `disc_cells` loops (same cells; twin guarantees it).
  Measured: tube111 pour 4.78→1.82s (2.6x), DCCF 5.63→4.43s; stitch knock-on
  tube111 2.55→1.48s. Island/repair parity verified identical. The deeper
  per-track alloc+AND accumulation is still open (see below) but no longer
  the bottleneck.
- [ ] FOLLOW-UP: `pour_copper_mask` per-track `cut = zeros(H,W)` alloc+AND per
  zone — accumulate foreign cuts into one mask per zone. Smaller win now
  that painting is compiled; revisit if pour profile regresses.
- [ ] SECOND: `plan_stitches` loop-invariant hoists — `binary_erosion(copper)`
  out of the group loop, windowed `distance_transform_edt(~jmask)` on group
  bbox + `max_bridge_mm`, incremental `joined_mask` cache (kills O(groups²)
  full-grid ORs). DCCF stitch 17s → low single digits expected.
- [x] Placement `is_empty` per-footprint bool cache — DONE 2026-10-15:
  `body_empty`/`pins_empty` cached per (re)build in `poly_cache`, pair loop
  reads bools (affine poses can't change emptiness). Measured: tube111
  35.8→24.1 ms/iter (1.5x), DCCF 55.7→19.4 ms/iter (2.9x); converged runs
  25.4→15.1s / 0.91→0.44s. Placement family green (52 + 56 + 79 tests).
  GUI estimate coefficient retuned 0.006→0.0045, floor 0.1→0.15. See notes §7.
- [ ] Routing time estimator: move GUI `2 + 1.2·n` to a per-net pad-count
  model (`est.pairs` already computed) + failed-net allowance; 1.2s is the
  small-net marginal, DCCF reality is heavy-tailed (VIN ~2.2s/net, failed
  searches to the 500k cap ~3s+, stitch +17s). See notes §8.

### Documentation (SI program support)
- [ ] Simulation glossary: extend `docs/autorouting_glossary.md` with an
  SI-dynamics section (wall/halo/MTV/aggregation/Kabsch/clamp/deadband/
  guard/tightening/broadphase/decap-power-GND rules), each term with a
  pointer to the `placement.py` symbol or `si_force_laws.md` section.
  (Progress 2026-10-15: wall entry rewritten for the unified law; full
  section still open.)
- [ ] Test suite organization: new `docs/tests.md` (per-file intent table,
  markers, pytest instructions) + `scripts/pytest_report.py` wrapping
  `pytest --html`; reports are gitignored
  (`docs/benchmarks/test_report_*.html`), regenerate on demand.
  Suite is green; the wrapper + `docs/tests.md` are what's missing.
  (Progress 2026-10-15: `pytest-html` added to dev deps + installed in `.venv`
  so `regen_pytest_report.sh --html` works; `pytest_results.qmd` cells made
  portable (repo-root walk-up, `QUARTO_PYTHON`/`sys.executable`, `--color=no`).
  `docs/tests.md` + `pytest_report.py` wrapper still open.)
- [x] Physics primer (was GATED behind settling the simulation method —
  gate passed with the SI merge `aaf82a8`): `docs/physics_primer.qmd` —
  formulas + why + units table + parameter-response graphs via
  `scripts/plot_force_laws.py` (matplotlib, dev dependency), figures
  embedded in the primer.
  (Done 2026-10-16: §§6–9 ghost/damping/integrator/mass written; ghost
  pull-in sweep runs live in the doc like the §4 heatmap; integrator
  sketch added as `fig_integrator_stability` in both the plot script and
  inline; damping/mass embed the existing PNG generators inline; all
  cited defaults verified against `PlacementParams`.)

