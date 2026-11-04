# Step 2 Plan — Harden boundaries

**Goal:** Establish clean public contracts between `ui/` and `src/` and within `src/` before splitting god modules. No behavior change except stricter validation opt-in and logging replacing swallowed output. Suite must stay green.

**Prerequisite:** Step 1 complete (duplicates extracted).

**Out of scope for step 2:** god-module splitting (#3), adding new UI features, changing `PlacementParams`/`RoutingParams` defaults.

---

## 1. Make `ui/` depend only on public engine API

### Problem
`ui/server.py` imports private engine symbols:
- `from kicad_autorouter.placement.model import _collect_pad_nodes, _find_decap_chip_links` (lines ~38–41)
- `from kicad_autorouter.routing.pipeline import _terminals_for_net` (line ~44)
- `from debug_grids import _cost_grid_to_debug, _secondary_to_debug` (line ~72) — a bare non-package-relative import that only works because `server.py` inserts `src/` into `sys.path` (line ~16).

`ui/server.py` also re-exports `STATE`, `_JOBS`, `_run_drc_now`, etc. (lines ~173–188) to preserve old import sites — an import-surface shim that has outlived its purpose.

### Extract / expose public API
- Add public wrappers in the owning modules for what `ui/` needs:
  - `board_model.py` or `placement/model.py`: public `collect_pad_nodes(model)`, `find_decap_chip_links(...)` (drop the leading underscore, or add non-private aliases).
  - `routing/pipeline.py`: public `terminals_for_net(model, grid, net_name, allowed_layers=None)` wrapping `_terminals_for_net`.
  - `ui/debug_grids.py`: keep as the canonical home for `_cost_grid_to_debug` / `_secondary_to_debug`, but import it through the package (`from ui.debug_grids import ...`) or move it under `src/kicad_autorouter/` as a public module and import from there.
- Remove `sys.path.insert(0, str(UI_DIR.parent / "src"))` from `ui/server.py` (line ~16); ensure all imports resolve via the installed package / `pythonpath`.
- Remove the re-export block (lines ~173–188); update `ui/server.py` handlers to import directly from `ui.server_state` / `ui.server_jobs`.

### Callers to update
- `ui/server.py` handler bodies using `_collect_pad_nodes`, `_find_decap_chip_links`, `_terminals_for_net`, `_cost_grid_to_debug`, `_secondary_to_debug`.
- `tests/` that import these private names from `ui.server` (check `tests/test_server*.py`).

### Verification
- `ui/server.py` imports cleanly without the `sys.path.insert`.
- `tests/test_server.py`, `tests/test_server_validation.py`, `tests/test_server_manual.py` pass.
- No `import _` prefixed names cross `ui/` → `src/`.

---

## 2. Single width/clearance resolution entry point

### Problem
Two width-policy entry points:
- `board_model/model.py::resolve_track_width_mm` (documented "single source of truth")
- `routing/params.py::resolve_track_width` — pipeline uses the latter; `ui/server.py` `/api/nets` calls the former directly. Drift risk.

### Extract / consolidate
- Keep `board_model/model.py::resolve_track_width_mm` as the canonical pure function.
- `routing/params.py::resolve_track_width` delegates to `board_model.model.resolve_track_width_mm` (or is removed in favor of it).
- `ui/server.py` `/api/nets` (lines ~707–715) uses the same canonical function as the router.

### Verification
- `tests/test_params_validation.py`, `tests/test_board_model.py` pass.
- Width values identical across UI and router for the same net params.

---

## 3. `from_dict` strict/warn mode for unknown keys

### Problem
`PlacementParams.from_dict` / `RoutingParams.from_dict` drop unknown keys silently (intentional for partial POSTs, but a typo'd knob vanishes with no warning).

### Extract
- Add `strict: bool = False` (or `warn: bool = True`) to `from_dict`:
  - `strict=False` (default): current behavior — drop unknown keys.
  - `strict=True`: raise `ValueError` listing unknown keys.
- `ui/server.py` partial-update merge paths (`/api/placement/params` POST, `/api/routing/params` POST) keep `strict=False`; headless/scripts can opt into `strict=True`.

### Verification
- `tests/test_params_validation.py` covers both modes.
- Server POST paths still accept partial params (no 400 on omitted keys).

---

## 4. Replace swallowed exceptions / print telemetry with logging

### Problem
- `placement/runner.py::_posed` (~205–206): `except Exception: continue` skips shapely errors silently.
- `placement/force.py`: `nearest_points`/polygon ops wrapped in try/except returning zeros.
- `ui/server.py`: `log_message` (line ~2166) swallows all request logs; `traceback.print_exc()` to stdout in placement endpoints (~1123–1124, ~1209–1211, ~1316–1318).
- `ui/server_jobs.py`: `BaseException` path (~148–152) releases the lock and re-raises but leaves the job `"running"` forever.

### Extract / fix
- Replace swallowed exceptions with `logging.debug`/`logging.warning` at the site (include context: uuid, pair, net name).
- Replace `traceback.print_exc()` in `ui/server.py` placement endpoints with `logging.exception`.
- Replace `log_message` suppression with a quiet `logging`-based handler or keep suppression but via `logging.NullHandler` equivalent.
- `ui/server_jobs.py` `BaseException` path: mark job `failed` before releasing/re-raising.

### Verification
- No new `print` calls in library code (already forbidden; extend to UI endpoints).
- `logging` capture in tests does not break; error paths still return 400/500 as before.

---

## Definition of done for step 2

- [ ] `ui/server.py` imports no private `src/` symbols; `sys.path.insert` removed.
- [ ] `ui/` imports `debug_grids` through a proper package path (no bare `from debug_grids import`).
- [ ] Re-export shim block removed from `ui/server.py`.
- [ ] Single canonical width/clearance function shared by UI + router.
- [ ] `from_dict` supports `strict=True`; default unchanged.
- [ ] Swallowed exceptions replaced with `logging`; `traceback.print_exc()` removed from UI endpoints.
- [ ] `.venv/bin/python -m pytest tests/ -q` green (excluding `slow`/`dccf` if the fast loop excludes them).
- [ ] Commit message per repo style. Do not commit unless asked.
