
## Routing Work Items (in order)

- [ ] **R9. Smoother post-pass** — Full curve/spline interpolation after stick phase
  (B-spline or clothoid); convert grid tracks to smooth curves while preserving
  clearance guarantees
  - Test suite for smoothing with angle_step_rad parameter

- [ ] **R10. Push-and-shove cleanup** — Move already-routed nets to make room for
  later nets; iterative improvement with finite violation costs

- [ ] **R11. Net ordering by criticality** — When we have adversarial routing or push-shove, we should also note wihch tracks are sensitive or difficult to prioritize.

### Visual
- [ ] Highlight selected footprint in SVG on table hover
