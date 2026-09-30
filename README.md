# KiCad AutoRouter Lab

Utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace
placement. Current phase: a minimal read-modify-write pass — load a board, make a
slight update to component placement and traces, and resave it. No routing
algorithm yet.

## Layout
- `src/kicad_autorouter/` — pure-Python core (`pipeline.py`, `validate.py`,
  `sexpr.py`, `io.py`) plus the `pcbnew_adapter.py` boundary (the only module that
  imports `pcbnew`).
- `scripts/run_autoroute.py` — headless first pass (run under KiCad's bundled Python).
- `scripts/roundtrip_pair.py` — pure-Python no-op read→edit→write of a pcb+sch pair, with checks.
- `tests/` — pytest suite + fixtures.
- `ui/` — stdlib workflow UI (`python ui/server.py`).
- `docs/` — local KiCad 10 docs + project guidance/reference notes.

## Run the first pass
The board load/modify/save step needs KiCad's bundled Python (it ships `pcbnew`):

```sh
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 \
    scripts/run_autoroute.py tests/fixtures/minimal.kicad_pcb -o experiments/out/baby_step.kicad_pcb
```

Then validate the result (pure Python, runs in `.venv`):

```sh
.venv/bin/python -m pytest tests/          # existing suite
# or DRC-check a specific board:
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc experiments/out/baby_step.kicad_pcb --format json
```

## No-op pair round trip (pure Python)

Reads a `.kicad_pcb` + `.kicad_sch` pair, applies an identity edit, writes new files,
and checks them (re-parse tree equality + `kicad-cli` DRC/ERC when available):

```sh
.venv/bin/python scripts/roundtrip_pair.py [board.kicad_pcb] [sch.kicad_sch] [-o OUT_DIR]
```

## References
Guidance and reference notes live under `docs/`, not in this README. Start at the index:

- [`docs/README.md`](docs/README.md) — index of local KiCad 10 docs + key facts for this project.
- [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) — autorouting terminology and guidance, aligned to the pipeline stages (`ingest→parse→connectivity→route→drc→writeback`), incl. force-directed placement of unlocked components around locked ones.
- Local KiCad 10 manuals (exact version match, `kicad-cli` 10.0.7):
  - [`docs/kicad/pcbnew.md`](docs/kicad/pcbnew.md) — PCB editor: layers, nets, tracks, pads, zones, DRC, file format notes.
  - [`docs/kicad/eeschema.md`](docs/kicad/eeschema.md) — schematic editor: symbols, nets, ERC, sheet structure.
  - [`docs/kicad/cli.md`](docs/kicad/cli.md) — `kicad-cli` reference (`pcb drc`, `sch erc`, exports).
  - [`docs/kicad/kicad.md`](docs/kicad/kicad.md) — project manager + all `.kicad_*` file types.

Deeper integration of the full `eeschema`/`pcbnew` manuals into the glossary is
left as future work (a whole session on its own).
