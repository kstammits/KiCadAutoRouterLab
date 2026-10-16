
## Routing Work Items (in order)

- [ ] **R8. Tests & Parity**
  - [x] Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
    (`test_routing_grid/obstacles/search`, kruskal + `RipUpManager` via
    `test_routing_search/gnd_strip/tight_spot` — green 2026-10-15)
  - [~] Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
    (partial: `test_drc_routing` magnet→route→DRC-clean passes; strict-xfail DRC gate exists)
  - [ ] Parity test: our routes vs existing tube111 routes (no such test yet)
  - [~] DRC gate: route → writeback → `kicad-cli drc` → iterate (strict-xfail gate exists; full gate open)

- [ ] **R9. Smoother post-pass** — Full curve/spline interpolation after stick phase
  (B-spline or clothoid); convert grid tracks to smooth curves while preserving
  clearance guarantees
  - Test suite for smoothing with angle_step_rad parameter

- [ ] **R10. Push-and-shove cleanup** — Move already-routed nets to make room for
  later nets; iterative improvement with finite violation costs

- [ ] **R11. Net ordering by criticality** — Decap first, then power, then GND, then
  signal (signal timing); within class by fanout/pad count

## SI dynamics program (branch: `feature/si-dynamics`)

Replace the clamped pseudo-force stepper with real physics: newtons, meters,
seconds. We are NOT tied to legacy behavior or parameter ranges — old params,
their UI sliders, and legacy-pinned tests may be torn down freely on this
branch. Collisions should be safe and non-sticky above all else; how elastic
parts "should" be is unknown, so start soft/underdamped-safe and let the
calibration sweep decide.

- Force laws + integrator equations: `docs/si_force_laws.md` (normative for
  the new system; keep it updated as formulas change).
- Part masses: `data/footprint_mass.json` (keyword table with rough guesses
  for our common parts, area fallback, user-editable).
- Steps: (1) m/N/s foundation, (2) SI force laws, (3) mass table loader,
  (4) semi-implicit Euler + viscous damping + adaptive-dt guard + Kabsch
  projection, (5) legacy params/UI teardown, (6) `scripts/calibrate_si.py`
  sweep on ghost_test → tube111 → DCCF to learn good ranges, (7) merge gates
  (same-or-better overlap + iterations, no sustained oscillation, suite green).
- Update 2026-10-15: (8) unified wall law landed — `F = k·(1−s/h)`,
  `wall_area_ref_m2` deleted, min-s winner-take-all, inverse push fixed
  (C14/RV3). `docs/si_force_laws.md` kept normative-updated. Suite green:
  329 passed, 1 skipped (pre-existing). Known residual: R13/RV1 stable
  squeeze wedge (anchor-side, not contact — see TODO).
- Performance track (not SI-gated): full-scale benchmark results, measured
  optimization order, and behavior notes live in
  `docs/benchmark_notes_2026-10-15.md` with TODO items in `TODO.md`
  ("Benchmarks & Measured Optimizations"). New: `scripts/benchmark_stitch_pour.py`.