
## Routing Work Items (in order)

- [ ] **R8. Tests & Parity**
  - Unit tests: grid transforms, obstacle marking, A* pathfinding, MST, RipUpManager
  - Integration test: route tube111 (pre-routed) → verify connectivity, THT free vias, no DRC violations
  - Parity test: our routes vs existing tube111 routes
  - DRC gate: route → writeback → `kicad-cli drc` → iterate

- [ ] **R9. Smoother post-pass** — Full curve/spline interpolation after stick phase
  (B-spline or clothoid); convert grid tracks to smooth curves while preserving
  clearance guarantees

- [ ] **R10. Push-and-shove cleanup** — Move already-routed nets to make room for
  later nets; iterative improvement with finite violation costs

- [ ] **R11. Net ordering by criticality** — Decap first, then power, then GND, then
  signal; within class by fanout/pad count

We'd like to see the forces on each pin or centroid as applicable.  It's not a lot more data. Maybe manually move parts too (arrow keys). 

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
- Legacy test failures are expected and fine — port or delete, don't preserve.