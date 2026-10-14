
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