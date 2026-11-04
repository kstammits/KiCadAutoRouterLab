# Step 3 Plan — Split god modules

**Goal:** Split the three oversized modules along existing seams into behavior-neutral, reviewable units. Suite green per split.

**Prerequisite:** Steps 1 and 2 complete (duplicates extracted, boundaries hardened).

**Out of scope for step 3:** changing algorithm behavior, adding features, changing public dataclass shapes.

---

## 1. `placement/runner.py` — split region simulation vs. merge vs. proposal

### Current layout (~1427 lines)
- `PlacementProposal` god dataclass (lines 61–140)
- `_depenetrate_positions` (lines 160–312)
- `_run_region_simulation` (~800 lines, lines 315–1118)
- `_merge_region_lists` (lines 1124–1144)
- `run_placement` (lines 1147–1185 visible; continues beyond)

### Target modules
- `placement/proposal.py` — `PlacementProposal` dataclass + `proposal_to_json`-adjacent shaping (kept in engine, not UI).
- `placement/depenetrate.py` — `_depenetrate_positions` + solver-internal constants (`_DEPEN_RELAX`, etc.).
- `placement/region.py` — `_run_region_simulation` (per-region force loop).
- `placement/runner.py` — thin orchestrator: `run_placement`, `_merge_region_lists`, region grouping, delegation to `region.py`/`depenetrate.py`.

### Seams to cut
- Region loop (`_run_region_simulation`) takes ~12 params; group into a `RegionSimConfig` dataclass or pass `model` + `params` + derived lookups.
- Merge (`_merge_region_lists`) is generic over frame type; keep as a small utility.
- `run_placement` validates params, collects pads, groups by region, calls region sim, merges, runs legality, assembles `PlacementProposal`.

### Verification
- `tests/test_placement.py`, `tests/test_placement_forces.py`, `tests/test_placement_settle.py`, `tests/test_placement_pairs.py`, `tests/test_placement_layers.py`, `tests/test_placement_power.py`, `tests/test_placement_movement.py`, `tests/test_placement_refine.py`, `tests/test_placement_tight.py`, `tests/test_placement_degenerate.py`, `tests/test_placement_frames.py`, `tests/test_placement_parity` (golden) pass unchanged.
- `PlacementProposal` public fields unchanged.

---

## 2. `routing/pipeline.py` — split route/raw vs. finish vs. refine

### Current layout (~1908 lines)
- `RoutingResult` dataclass (lines 43–74)
- `RawRoutes` dataclass (lines 719–752) — already a boundary
- `route_raw` (~680 lines, lines 755–1432)
- `finish_routes` (lines 1435–1562)
- `route_nets` (lines 1565–1619)
- `refine_routing` (lines 1622–1759)
- Helpers (`_resolve_grid_layers`, `_terminals_for_net`, `_apply_route_to_grid`, `_chunk_terminals`, `_merge_chunk_results`, `_check_chunk_coverage`, `_smooth_result`, `_route_escapes_board`, `_tangle_groups`, `_refine_order`, `_tht_sites_by_net`, `_build_tht_via_mask`, `_rip_up_nets_from_model`, `_strip_gnd_blockers_from_model`)

### Target modules
- `routing/pipeline.py` — thin entry points: `route_nets`, `refine_routing`, `RoutingResult`; delegate to `route_raw`/`finish_routes`.
- `routing/route_raw.py` — `route_raw`, `RawRoutes`, `_route_one_net`, `_route_chunk` (from step 1), grid/cost-grid setup, power/ground/signal classification, plane fanout.
- `routing/finish_routes.py` — `finish_routes`: stitching, tree writeback, zone refill, DRC, `RoutingResult` assembly.
- `routing/refine.py` — `refine_routing`, `_tangle_groups`, `_refine_order`, candidate scoring.
- Keep `routing/pipeline.py` re-exporting names so existing import sites (`from .pipeline import ...`) keep working.

### Seams to cut
- `RawRoutes` is already the handoff boundary between `route_raw` and `finish_routes` — move each into its own module with `RawRoutes` imported across them.
- `refine_routing` calls `route_raw` then `finish_routes` on the winner — belongs in `routing/refine.py` orchestrating both.

### Verification
- `tests/test_routing_search.py`, `tests/test_routing_chunked.py`, `tests/test_routing_piecemeal.py`, `tests/test_drc_routing.py`, `tests/test_routing_tight_spot.py`, `tests/test_routing_astar.py`, `tests/test_routing_obstacles.py`, `tests/test_routing_regions.py`, `tests/test_routing_capsule.py`, `tests/test_routing_crossing.py`, `tests/test_routing_smooth.py`, `tests/test_track_ripup.py`, `tests/test_routing_tht_free_via.py`, `tests/test_routing_grid.py`, `tests/test_routing_pour.py`, `tests/test_routing_limits.py`, `tests/test_routing_pieces` (if any) pass unchanged.
- `RoutingResult` public fields unchanged.

---

## 3. `ui/server.py` — split HTTP dispatch into domain handlers

### Current layout (~2205 lines)
- Module-level constants, param load/save, `proposal_to_json` (lines 1–283)
- `Handler` class with monolithic `do_GET` (~line 294) / `do_POST` (~line 963) if/elif chains (~1900 lines)

### Target modules
- `ui/server.py` — `ThreadingHTTPServer` setup, `main()`, route registration/dispatch only (thin `Handler` that delegates to domain handler modules).
- `ui/handlers/placement.py` — all `/api/placement/*` endpoints.
- `ui/handlers/routing.py` — all `/api/routing/*` endpoints.
- `ui/handlers/nets.py` — all `/api/nets/*` endpoints.
- `ui/handlers/board.py` — `/api/board/*`, `/api/state`, `/api/version`, `/api/mode`, `/api/docs/*`, static fallback.
- `ui/handlers/__init__.py` — route table mapping path + method to handler.

### Seams to cut
- Dispatch by `(parsed.path, self.command)` in a shared registry; each domain module exports `handle_get(path, qs, handler)` / `handle_post(path, qs, body, handler)` returning `(code, body, content_type)` or `None` if not matched.
- `Handler._send` shared; domain handlers call it or return payloads for `server.py` to send.
- Shared state (`STATE`, `load_params`, `save_params`, `proposal_to_json`, `_cost_grid_to_debug`) imported by domain handlers from `ui.server_state`, `ui.server`, `ui.debug_grids`.

### Verification
- `tests/test_server.py`, `tests/test_server_validation.py`, `tests/test_server_manual.py` pass unchanged.
- Every route previously handled by `Handler.do_GET`/`do_POST` still returns the same status + body (route table covers all paths).
- `./scripts/check_js.sh` unaffected (no JS changes).

---

## Definition of done for step 3

- [ ] `placement/runner.py` split into `proposal.py` / `depenetrate.py` / `region.py` / `runner.py`; re-exports preserve old import sites.
- [ ] `routing/pipeline.py` split into `pipeline.py` / `route_raw.py` / `finish_routes.py` / `refine.py`; re-exports preserve old import sites.
- [ ] `ui/server.py` split into domain handler modules under `ui/handlers/`; `main()` and dispatch unchanged externally.
- [ ] `.venv/bin/python -m pytest tests/ -q` green per split (run after each sub-split, not only at the end).
- [ ] No public dataclass/field/endpoint shapes changed.
- [ ] Commit message per repo style. Do not commit unless asked.
