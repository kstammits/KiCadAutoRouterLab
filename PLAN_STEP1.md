# Step 1 Plan — Extract duplicates

**Goal:** Remove near-duplicate logic across `placement/force.py`, `routing/pipeline.py`, and `ui/server.py` before any module splitting. No behavior change; pure extraction. Suite must stay green.

**Scope (in order within step 1):**

1. `placement/force.py` — contact-push spread duplication
2. `routing/pipeline.py` — attempt/retry/resume loop + chunk coverage
3. `ui/server.py` — movable_uuids computation, routing/job payloads, footprint/nets rendering

**Out of scope for step 1:** god-module splitting (#3), boundary hardening (#2), test additions beyond what extraction requires.

---

## 1. `placement/force.py` — unify `_spread` / `_spread_tangential` and push paths

### Problem
- `_spread` (line ~423) and `_spread_tangential` (line ~438) are near-identical, differing only in which accumulator they write (`poly_force` vs `tangential_force`) and which projection axis they use.
- Three push sites repeat the same `_spread`/`_spread_tangential` call pattern:
  - `_apply_contact_push` (~517–585)
  - `_apply_contact_push_analytical` (~587–639)
  - Aggregated per-pair block (~833–893)

### Extract
- `def _spread_force(rows, cx, cy, radius, ux, uy, fx, fy, poly_force, tangential_force)` — single spread helper that writes both accumulators when the tangential component is non-zero.
- `def _apply_push(li, lj, ...normal, ...tangential, ...s_mm)` — single contact-push entry used by body-body, pin-body, body-pin, pin-pin, and the aggregated winner block. Replaces the inline per-pair block (~833–893).

### Callers to update
- `_apply_contact_push`, `_apply_contact_push_analytical` delegate to `_apply_push`.
- Aggregated per-pair block (~833–893) delegates to `_apply_push`.
- `_spread`/`_spread_tangential` call sites consolidated into the new helper.

### Verification
- `tests/test_placement_forces.py`, `tests/test_placement_settle.py`, `tests/test_placement_pairs.py`, `tests/test_magnet.py`, `tests/test_courtyard_behavior.py` pass unchanged.
- Force-component breakdown keys unchanged (`repulsion`, `attraction`, `courtyard`, `ghost`, `tangential`).

---

## 2. `routing/pipeline.py` — extract attempt/resume and chunk-coverage helpers

### Problem
- `_route_one_net` (~1013–1321) contains the inline `_fly` closure (~1162–1225) plus the retry/resume loop (~1226–1321).
- `_check_chunk_coverage` (~485–523) and `_merge_chunk_results` (~420–460) already exist but the per-attempt wiring is inline.

### Extract
- `def _route_chunk(router, chunk, cost_grid, routing_grid, corridor, clearance_radius, soft_mask, region_blocked_mask, pinned, fit_arcs, arc_params, term_set, progress_cb, should_cancel, ...)` — one A* + smooth + board-escape check + stamp; returns `(NetRouteResult|None, fail_kind|None)`. Moves logic out of `_fly`.
- `def _resume_progress(grouped_chunks, group_progress)` — recomputes `done_base` for resume (currently inline ~1237–1241).
- Keep `_check_chunk_coverage` and `_merge_chunk_results` where they are; call from the new resume loop.

### Callers to update
- `_route_one_net` calls `_route_chunk` inside a thin retry loop over `grouped_chunks` / `group_progress`.
- `route_raw` unchanged externally.

### Verification
- `tests/test_routing_search.py`, `tests/test_routing_chunked.py`, `tests/test_routing_piecemeal.py`, `tests/test_drc_routing.py`, `tests/test_routing_tight_spot.py` pass unchanged.
- Expansion counts and partial-result behavior unchanged (these are observable outputs).

---

## 3. `ui/server.py` — deduplicate request handling

### Problem A: `movable_uuids` computation repeated three times
- `/api/placement/run` (~1102–1111)
- `/api/placement/forces` (~1301–1309)
- Identical "all unlocked except pinned" derivation.

### Extract
- `def _movable_uuids_for(model, state, req_uuids) -> set[str]` — returns explicit list filtered by pinned, or derives all unlocked non-pinned.

### Problem B: routing result payload repeated
- `/api/routing/run` (~2040–2086)
- `ui/server_jobs.py` worker result payload (~171–210) — near-duplicate.

### Extract
- `def _routing_result_payload(result, cost_grid, routing_grid, route_params, mode_warning=None, candidates=None, winner_seed=None, debug=None) -> dict` — shared by both call sites.

### Problem C: footprint/nets rendering duplication
- `/api/footprints` real-footprint loop (~556–624) and ghost-footprint loop (~594–624) share bbox/nets/pad_count construction.
- Forces-only `PlacementProposal` ad-hoc construction (~394–423) duplicates `proposal_to_json` shaping (~244–283).

### Extract
- `def _footprint_summary(fp, model, pinned, selected, extra_power, ghost=False) -> dict` — shared by real/ghost loops.
- Reuse `proposal_to_json` for the forces-only proposal instead of manual field copying.

### Verification
- `tests/test_server.py`, `tests/test_server_validation.py`, `tests/test_server_manual.py` pass unchanged.
- Response bodies byte-identical (json field order/rounding preserved).

---

## Definition of done for step 1

- [ ] `_spread`/`_spread_tangential` merged; `_apply_push` introduced in `placement/force.py`.
- [ ] `_route_chunk` (+ resume helpers) introduced in `routing/pipeline.py`; `_fly` removed.
- [ ] `_movable_uuids_for`, `_routing_result_payload`, `_footprint_summary` introduced in `ui/server.py`; call sites updated.
- [ ] `.venv/bin/python -m pytest tests/ -q` green (excluding `slow`/`dccf` if the fast loop excludes them).
- [ ] No changes to `PlacementProposal`, `RouteResult`, `RoutingResult` public shapes.
- [ ] No `print` telemetry added; exceptions go through `logging` where newly introduced.
- [ ] Commit message per repo style (concise, imperative). Do not commit unless asked.
