# KiCad AutoRouter Lab

Utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace
placement. Current phase: a headless force-spring **placement** pass (v0 stub
physics) plus a no-op pcb+sch round trip. No routing algorithm yet.

## Layout
- `src/kicad_autorouter/` — pure-Python core (`pipeline.py`, `validate.py`,
  `sexpr.py`, `io.py`) plus the `pcbnew_adapter.py` boundary (the only module that
  imports `pcbnew`).
- `scripts/run_autoroute.py` — headless placement pass (.venv): parse → force-spring proposal → per-UUID writeback → optional DRC.
- `scripts/roundtrip_pair.py` — pure-Python no-op read→edit→write of a pcb+sch pair, with checks.
- `tests/` — pytest suite + fixtures.
- `ui/` — stdlib workflow UI + board viewer (`python ui/server.py`).
- `docs/` — local KiCad 10 docs + project guidance/reference notes.

## Run the placement pass
The headless entry point runs under the project `.venv` (pure Python, no `pcbnew` needed):

```sh
.venv/bin/python scripts/run_autoroute.py tests/fixtures/minimal.kicad_pcb \
    -o experiments/out/placed_minimal.kicad_pcb --validate
```

Then validate the result:

```sh
.venv/bin/python -m pytest tests/          # existing suite
# or DRC-check a specific board:
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc experiments/out/placed_minimal.kicad_pcb --format json
```

## View a board
The workflow UI renders loaded boards as SVG snapshots (pure Python, no `pcbnew` needed):

```sh
.venv/bin/python ui/server.py   # http://127.0.0.1:8000
```

Open the page and drop a `.kicad_pcb` file onto the drop zone — it renders in the board panel (outline, zones, courtyards, tracks, vias, pads, ref text). The UI polls `/api/state` every 2 s and refreshes the snapshot whenever the server's version changes.

## References
Guidance and reference notes live under `docs/`, not in this README. Start at the index:

- [`docs/README.md`](docs/README.md) — index of local KiCad 10 docs + key facts for this project.
- [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) — autorouting terminology and guidance, aligned to the pipeline stages (`ingest→parse→connectivity→route→drc→writeback`), incl. force-directed placement of unlocked components around locked ones.
- Local KiCad 10 manuals (exact version match, `kicad-cli` 10.0.7):
  - [`docs/kicad/pcbnew.md`](docs/kicad/pcbnew.md) — PCB editor: layers, nets, tracks, pads, zones, DRC, file format notes.
  - [`docs/kicad/eeschema.md`](docs/kicad/eeschema.md) — schematic editor: symbols, nets, ERC, sheet structure.
  - [`docs/kicad/cli.md`](docs/kicad/cli.md) — `kicad-cli` reference (`pcb drc`, `sch erc`, exports).
  - [`docs/kicad/kicad.md`](docs/kicad/kicad.md) — project manager + all `.kicad_*` file types.

