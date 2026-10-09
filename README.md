# KiCad AutoRouter Lab

> **Note:** This project is **mostly AI-generated** (Nemotron, Qwen3.8, and Muse Spark 1.3, plus Karl's brain) and has **not been extensively human-tested**. Expect bugs, incomplete features, and potential regressions. Use at your own risk.

## Contributors

- Karl
- Nemotron
- Qwen3.8
- Muse Spark 1.3

Utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace
placement. Current phase: **per-pad force-spring placement with organic rotation**
plus iterative UI for component selection and step-by-step refinement. No routing
algorithm yet.

## Layout

- `src/kicad_autorouter/` — pure-Python core (`placement.py`, `board_model.py`,
  `svg_render.py`, `validate.py`, `sexpr.py`, `io.py`) plus the `pcbnew_adapter.py`
  boundary (the only module that imports `pcbnew`).
- `scripts/run_autoroute.py` — headless placement pass (.venv): parse → per-pad
  force-spring proposal → per-UUID writeback → optional DRC.
- `scripts/roundtrip_pair.py` — pure-Python no-op read→edit→write of a pcb+sch
  pair, with checks.
- `tests/` — pytest suite + fixtures.
- `ui/` — stdlib workflow UI + board viewer (`python ui/server.py`).
- `docs/` — local KiCad 10 docs + project guidance/reference notes.

## Run the placement pass

The headless entry point runs under the project `.venv` (pure Python, no `pcbnew`
needed):

```sh
# Iterative physics mode (per-pad MST force-spring, step-by-step)
.venv/bin/python scripts/run_autoroute.py tests/fixtures/DCCF.sved.kicad_pcb \
    -o experiments/out/placed_dccf.kicad_pcb --step-iterations 5 --max-total-iterations 500 --validate
```

Then validate the result:

```sh
.venv/bin/python -m pytest tests/          # existing suite
# or DRC-check a specific board:
/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli pcb drc experiments/out/placed_minimal.kicad_pcb --format json
```

## Workflow UI

The UI renders loaded boards as SVG snapshots (pure Python, no `pcbnew` needed)
with interactive zoom/pan and component selection for iterative placement:

```sh
.venv/bin/python ui/server.py   # http://127.0.0.1:8000
```

### UI workflow

1. **Load** — drop a `.kicad_pcb` file (or use "Load…" button)
2. **Adjust parameters** — repulsion/attraction, ideal length, etc.
3. **Select components** — click a footprint to select it; Shift+click for multi-select; Alt+click to pin (prevent movement)
4. **Step/Run placement** — Step runs a few iterations (default 5); Run goes to convergence (saved max iterations); ghost courtyards + arrows show proposed moves. Boards with no nets correctly propose no movement.
5. **Accept proposal** — commits changes to the board model
6. **Repeat** — continue stepping until satisfied

### Component states (visual)

| State | Color | Pattern | Movable? |
|-------|-------|---------|----------|
| KiCad-locked | Red | dashed | Never |
| User-pinned | Orange | dashed | No (excluded) |
| Selected | Green | solid | Yes |
| Default unlocked | Gray-blue | solid | Yes (if no selection) |

### Keyboard shortcuts

- `A` / "Select All" button — select all unlocked, unpinned footprints
- `Escape` / "Clear" button — clear selection
- "Pin" button — pin all currently selected footprints
- Mouse wheel — zoom; drag — pan; "Fit" button — reset view

### DRC overlay

Tick **DRC** or press **Run DRC** to run `kicad-cli pcb drc` on the current
board; violations render as markers on the SVG. Silkscreen, library-metadata
and router-irrelevant classes are hidden by default — see
[`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) (DRC stage) for
the ignored list.

## Headless iterative mode

The CLI supports the same iterative flow:

```sh
.venv/bin/python scripts/run_autoroute.py board.kicad_pcb -o out.kicad_pcb \
  --step-iterations 5 --max-total-iterations 500 --validate
```

- `--step-iterations` — iterations per step (default 5)
- `--max-total-iterations` — total budget across all steps (default 1000)

## References

Guidance and reference notes live under `docs/`, not in this README. Start at the index:

- [`docs/README.md`](docs/README.md) — index of local KiCad 10 docs + key facts for this project.
- [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) — autorouting terminology and guidance, aligned to the pipeline stages (`ingest→parse→connectivity→route→drc→writeback`), incl. force-directed placement of unlocked components around locked ones.
- Local KiCad 10 manuals (exact version match, `kicad-cli` 10.0.7):
  - [`docs/kicad/pcbnew.md`](docs/kicad/pcbnew.md) — PCB editor: layers, nets, tracks, pads, zones, DRC, file format notes.
  - [`docs/kicad/eeschema.md`](docs/kicad/eeschema.md) — schematic editor: symbols, nets, ERC, sheet structure.
  - [`docs/kicad/cli.md`](docs/kicad/cli.md) — `kicad-cli` reference (`pcb drc`, `sch erc`, exports).
  - [`docs/kicad/kicad.md`](docs/kicad/kicad.md) — project manager + all `.kicad_*` file types.