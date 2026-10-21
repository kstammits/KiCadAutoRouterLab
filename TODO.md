# KiCad AutoRouter - TODO

### Contact Law Unification (landed 2026-10-15)

- Follow-up (documented residual, NOT contact): R13/RV1 stable squeeze wedge (southern halo field + net anchor across RV1 exceed the edge hold at every exit point; equilibrium is stiffness-independent). Needs anchor-side or placement-level work, not wall-law work.
- Follow-up: halo reach `h` is the only remaining distance lever (shorter `h` drops distant squeezers); global change, needs recalibration — do not touch without a sweep.
- [x] Wall-law follow-through (testing/docs/figures): the unified law landed, regression coverage of the min-s winner-take-all path beyond the C14/RV3 probes, and the glossary wall entry completion (done 2026-10-20). `docs/figures/` regenerated from current code + committed.

### Legality-detected Legalize (NEW — spec written 2026-10-20, not yet implemented)

- [ ] New `placement/legality.py` + `params.py` tolerances (`overlap_tolerance_mm2`, `wall_tolerance_mm`)
- [ ] `runner.py`: post-hoc "stalled" downgrade when `converged` but illegal
- [ ] `ui/server.py`: payload + `proposal_to_json` fields
- [ ] `ui/index.html`: part-table marking
- [ ] Tests: new legality tests + update `test_loaded_contact_settles...` + `test_dccf_legalize_stop_is_honest` (see `docs/legality_spec.md`)

### Routing

- [ ] Roadmap: PLAN.md R10 (push-and-shove), R11 (net ordering by criticality).
- [ ] Layer assignment heuristic (prefer F.Cu for signals, B.Cu for return paths)
- [ ] VIN-on-DCCF bug (reported 2026-10-15, NOT fixed): VIN tracks across same-footprint different-net pads (Q1 THT row: VIN pad 1 vs GND/−5V/+5V pads 2/4/5/6; Q2 tight SMD neighborhood). Suspects: foreign-THT marking in `obstacles._mark_pad`, `unconnected-*` net handling, own-cell secondary=500 toll on Q2 terminal pushing the path off-start, 0.1mm inter-pad gaps. Repro: route VIN on DCCF, dump corridor cells around Q1 pads 1–2.
- [ ] DCCF fixture: single GND zone (x ≤ 169.6) covers panel 1 only; panel 2 (x 175.5–275.5, all VIN parts) has NO pour — add the zone in pcbnew. Until then stitch burns ~17s repairing an incomplete board.
- [ ] Heuristic-weight experiment (NOT a default change): sweep default 3.0 vs 1.0 over tube111 signal nets + DCCF sample; gate on routing regression green + path-cost parity.

### Benchmarks & Measured Optimizations (2026-10-15 full-scale runs)

- [x] `pour_copper_mask` raster churn — DONE 2026-10-15: `_paint_disc`/`_paint_pad`/`_paint_track` route through numba `stamp_discs`. Measured: tube111 pour 4.78→1.82s (2.6x), DCCF 5.63→4.43s; stitch knock-on tube111 2.55→1.48s.
- [ ] `pour_copper_mask` per-track `cut = zeros(H,W)` alloc+AND per zone — accumulate foreign cuts into one mask per zone. Smaller win now that painting is compiled; revisit if pour profile regresses.
- [ ] `plan_stitches` loop-invariant hoists — `binary_erosion(copper)` out of the group loop, windowed `distance_transform_edt(~jmask)` on group bbox + `max_bridge_mm`, incremental `joined_mask` cache (kills O(groups²) full-grid ORs). DCCF stitch 17s → low single digits expected.
- [x] Placement `is_empty` per-footprint bool cache — DONE 2026-10-15: `body_empty`/`pins_empty` cached per (re)build in `poly_cache`. Measured: tube111 35.8→24.1 ms/iter (1.5x), DCCF 55.7→19.4 ms/iter (2.9x); converged runs 25.4→15.1s / 0.91→0.44s.

### Test Hygiene (NEW)

- [ ] Update placement physics tests to use default params where possible, testing behavioral/heuristic outcomes rather than exact force values. Currently `test_placement_forces.py:_base_params` and `test_courtyard_behavior.py:_base_params` pin `wall_exponent=1.0, momentum_alpha=0.0` to reproduce legacy dynamics — migrate to default params (`wall_exponent=2.0, momentum_alpha=0.7`) and update assertions to check behavioral invariants (e.g., "force grows with penetration", "parts separate", "no force cliff at contact") rather than exact magnitude formulas.
- [ ] Routing time estimator: move GUI `2 + 1.2·n` to a per-net pad-count model (`est.pairs` already computed) + failed-net allowance.

### Documentation (SI program support)

- [x] Physics primer: `docs/physics_primer.qmd` — formulas + why + units table + parameter-response graphs via `scripts/plot_force_laws.py` (done 2026-10-16).
- [x] Simulation glossary: extend `docs/autorouting_glossary.md` with an SI-dynamics quick-reference table (wall/halo/MTV/aggregation/Kabsch/clamp/deadband/guard/tightening/broadphase/decap-power-GND rules), each term with a pointer to the `placement.py` symbol or `si_force_laws.md` section. (Done 2026-10-20.)
- [ ] Test suite organization: new `docs/tests.md` (per-file intent table, markers, pytest instructions) + `scripts/pytest_report.py` wrapping `pytest --html`; reports are gitignored (`docs/benchmarks/test_report_*.html`), regenerate on demand. (`pytest-html` added to dev deps + installed in `.venv`; `regen_pytest_report.sh --html` works; `pytest_results.qmd` cells made portable.)