# KiCad AutoRouter Lab

> **Note:** This project is **mostly AI-generated** (Nemotron, Qwen3.8, and Muse Spark 1.3, plus Karl's brain) and has **not been extensively human-tested**. Expect bugs, incomplete features, and potential regressions. Use at your own risk.

Utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and performs iterative placement + grid-based routing. Current phase: **per-pad force-spring placement with organic rotation** plus **Lee/A* routing with rip-up/retry and GND pour continuity repair**. Pure-Python core (no `pcbnew` for routing); `pcbnew` only used as an offline test oracle.

## Layout

- `src/kicad_autorouter/` — pure-Python core (`placement.py`, `board_model.py`,
  `svg_render.py`, `validate.py`, `sexpr.py`, `io.py`) plus the `pcbnew_adapter.py`
  boundary (the only module that imports `pcbnew`).
- `scripts/run_autoroute.py` — headless placement pass (.venv): parse → per-pad
  force-spring proposal → per-UUID writeback → optional DRC.
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
2. **Adjust parameters** — repulsion/attraction etc.
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

## Routing panel

The **Route** tab (right drawer) drives the grid-based router. It runs
incrementally — one net per HTTP request — so progress/cancel work without
server threading changes.

### Routing controls

| Control | Meaning |
|---------|---------|
| **Track Width** | Default signal track width (mm) |
| **Power Width** | Default power/ground track width (mm) |
| **Via Cost** | Wire-equivalent mm cost per via (controls layer changes) |
| **Clearance** | Default clearance between tracks (mm) |
| **Grid Resolution** | A* grid cell size; 0.05mm = 4× cells, may be rejected on large boards |
| **Pour Severance Weight** | Soft cost for tracks that narrow a pour; 0 disables |
| **Debug** | Return cost-grid + search frontier for SVG overlay |
| **GND Stitching** | Auto-repair GND pour continuity after routing (stitching vias + bridge tracks) |
| **Strip GND Blockers** | Evict pre-existing GND vias/tracks before routing (rebuilt by stitcher); signal/power copper is never evicted |

### Nets table (Route tab)

The table lists every net with live status:

| Column | Meaning |
|--------|---------|
| **Net Name** | Click row to select; Shift/Ctrl for multi-select |
| **Pads / Footprints / Tracks / Vias** | Existing copper counts |
| **Width** | Resolved track width for this net (explicit → netclass → power → default) |
| **Pwr** | ⚡ badge = power rail (keyword rules **or** your override). **Click the cell to mark/unmark** — marked nets route first with wide tracks, get placement magnetism, and count for decap detection. `power_user` flag means you toggled it. |
| **Decap** | ◉ = decoupling caps on this net (C* footprint, power+ground) |
| **🔒 Protect** | Protected nets route first and are never ripped up |

**Power membership is user-controllable:**
- On load, nets sitting in KiCad netclasses whose *name* matches power keywords (e.g. a class literally called "Power") are auto-marked.
- After that you own the set — click the **Pwr** cell to add/remove. Badge, Width, and Decap columns refresh instantly.
- Ground stays keyword-only; on overlap power wins (e.g. `GND_PWR`).

### Routing actions

- **Route Selected Nets** — runs the chunked router for each ticked net (progress bar, cancel works)
- **Route All Unprotected** — temporary selects every non-protected net and routes them all
- **Stop** — aborts the current net's search; already-routed nets are kept

### What the router does today

- Two-layer (F.Cu / B.Cu) grid A* with via cost, THT pads = free vias
- Per-net fanout halos over foreign pads (steer without trapping)
- Pour-severance toll steers tracks away from narrow pour corridors (clipped, never blocks)
- **GND-only rip-up**: pre-existing GND vias/tracks can be stripped upfront (`Strip GND Blockers`); the stitcher (`plan_stitches()`) rebuilds continuity afterwards with overlap vias + bridge tracks. **Signal/power copper is never evicted** — no more tracks ripping each other out.
- Stitcher is region-aware (never joins separate board panels in the same file)
- DRC gate: `kicad-cli pcb drc` on the written file

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