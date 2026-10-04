# TODO: KiCad PCB Version Detection & Warning System

## Overview
Add version awareness to the PCB loading pipeline so that:
- **v8/v9/v10 boards** load normally (supported)
- **v11+ boards** trigger a warning (unknown compatibility)
- Warning reaches the UI via both `/api/load` and `/api/state` endpoints
- `BoardModel` stores the detected version

---

## Changes Required

### 1. `src/kicad_autorouter/board_model.py`
- Add `version: str` field to `BoardModel` dataclass (line ~153)
- Add `version_warning: Optional[str]` field to `BoardModel` for v11+ warning
- Modify `board_model(tree)` function (line ~967) to:
  - Extract `generator_version` from tree (e.g., `"10.0"`, `"9.0"`, `"11.0"`)
  - Parse major version number
  - Set `version` and `version_warning` on the returned `BoardModel`

### 2. `src/kicad_autorouter/sexpr.py` (optional helper)
- Add helper function `get_generator_version(tree: SExpr) -> Optional[str]` to extract version cleanly

### 3. `ui/server.py` - `BoardState.load()` (line ~49)
- Capture `version` and `version_warning` from `board_model()` result
- Store in `BoardState` (add `self.board_version` and `self.version_warning` fields)

### 4. `ui/server.py` - `/api/load` endpoint (line ~418)
- Return `version` and `warning` in JSON response:
  ```json
  {"ok": true, "name": "...", "kind": "pcb", "version": 1, "board_version": "10.0", "warning": null}
  ```

### 5. `ui/server.py` - `/api/state` endpoint (line ~274)
- Include `board_version` and `warning` in state response:
  ```json
  {"loaded": true, "name": "...", "version": 1, "has_sch": false, "board_version": "10.0", "warning": null}
  ```

---

## Version Logic
| KiCad Version | Action |
|---------------|--------|
| 8.x, 9.x | Load normally, no warning |
| 10.x | Load normally, no warning (current target) |
| 11.x+ | Load but set `version_warning = "KiCad 11+ format detected; compatibility not verified"` |

---

## Testing
- Add test fixtures or mock trees with different `generator_version` values
- Test `board_model()` returns correct version/warning
- Test `/api/load` and `/api/state` responses include version info

---

## Notes
- Another agent is currently working on `server.py` - coordinate changes to avoid conflicts
- Use `generator_version` field (e.g., `"10.0"`) as primary source; fall back to `version` (date) if missing
- Warning should NOT block loading - return 200 with warning field populated