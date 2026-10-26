# Tests

Per-file intent, markers, and how to run. Generated HTML reports stay untracked.

## Markers (`pyproject.toml`)

`unit` pure no-fixture · `params` params validation · `physics` force-spring (needs nets) ·
`cli` CLI · `server` HTTP API · `regression` bug fixes · `minimal` 4-part fixture ·
`dccf` 97-part audio fixture. No `tube111` marker: the 105-part board is the
default heavy fixture (12+ files); deselect via `-m "not dccf"` + `--ignore` or
`-k` for fast loops. Use `minimal` for no-`kicad-cli` smoke tests.

## Running

`.venv/bin/python -m pytest tests/ -q` (fast: `-m "minimal or unit"`).
`scripts/gen_manual.py --check` in CI for manual/figure freshness.

## Files

| File | Intent |
|------|--------|
| `test_analytical_contact` | SAT rect/circle collision vs shapely |
| `test_board_model` | Model, deltas, locks, UUIDs, zones, geometry guards |
| `test_courtyard_behavior` | RV1 slide-pot, attraction vs wall |
| `test_magnet` | Magnet-board tightening + zero-overlap |
| `test_motion_roundtrip` | Player `translate·rotate` vs `apply_deltas` |
| `test_placement*` | Forces, frames, layers, movement, pairs, power (+membership), settle, tight |
| `test_params_validation` | PlacementParams + RoutingParams fail-fast |
| `test_routing_*` | A*, grid, obstacles/fanout, pour, search, smooth, THT via, tight-spot, crossing, chunked |
| `test_stitch`, `test_gnd_strip`, `test_pour`, `test_drc_routing` | GND pour, strip, stitch, DRC gate |
| `test_io`, `test_svg_render`, `test_track_ripup` | S-expr I/O, render, rip-up |
| `test_ghost*`, `test_padless*` | Ghosts, padless |
| `test_server*`, `test_run_autoroute` | API + CLI |
