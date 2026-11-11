# Emit gate / smoother contract (landed)

Status: landed. The emit gate was stricter than its own physical model
(zero-width centerline vs finite cells, ~0.07 mm over-strict at 0.1 mm
grid) and the smoother manufactured violating geometry out of legal
search paths (J1-Pad3/Pad4 false refusals on the tube111 chain).

What landed instead of the proposed classify-then-repair loop:

- Gate-side margin classifier (`verify.gate_touch_margin`: TOUCH vs
  NON_TOUCH) with **fallback-to-raw** repair (`route_raw._resolve_gate_trip`:
  NON_TOUCH trips stamp the legal search path, TOUCH stays fatal).
- Effort control (`RoutingParams.smooth_effort` 0..3, default 2.0 =
  legacy behavior) via `smooth.resolve_config` / `_route_chunk`.
- Fine re-smoothing as the separate Polish action (`routing/polish.py`,
  server `/api/routing/polish`).

Residuals (manufactured gate trips that reappear, curve search, copper
push-aside) are tracked in `docs/roadmap.md`. Full incident analysis
lives in git history.
