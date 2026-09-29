# KiCad AutoRouter Lab — TODO / Session State

Goal: utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Phase: **minimal read-modify-write pass done** (load board → nudge unlocked parts → add demo track → save). Full autorouter logic + guidance docs still pending. System on **KiCad 10** (`kicad-cli` 10.0.7); local docs are KiCad 10.

## Active / Next Moves (in order)
- [ ] **Gather terminology for guidance docs (in progress).** PCB autorouting challenges + force-directed placement of unlocked components around locked ones. Sources: Wikipedia (`Autorouter`, `Maze_routing`, `Lee_algorithm`, `Force-directed_graph_drawing`) + local `docs/kicad/pcbnew.md`. Then draft a glossary/guidance doc under `docs/` linked from the README **References** section, aligned to stages `ingest→parse→connectivity→route→drc→writeback`.
- [ ] Find an **S-expression grammar reference** for `.kicad_pcb` / `.kicad_sch` (NOT JSON). Candidates: KiCad source parsers (`pcb/`, `eeschema/`), KiKit docs, local manuals in `docs/kicad/`.
- [ ] **Extend the autorouter beyond the baby step.** Both interfaces, shared core; SWIG `pcbnew` runtime; PCM packaging = build target only (nothing to package yet).
  - Core stays pure Python (no `pcbnew` import) → testable in `.venv`. New: `model.py` (abstract board), `placement.py` (force-directed nudge, locked=fixed), `routing.py` (maze/Lee, later); `pcbnew_adapter.py` already exists (only place importing `pcbnew`).
  - Entry points: `plugin/` action plugin (`class Nudge(pcbnew.ActionPlugin)`, `.register()`) + `scripts/run_autoroute.py` headless CLI (first pass done).
  - Locked components = native footprint lock `fp.IsLocked()` / `fp.SetLocked(True)`; only unlocked parts move.
  - Runs under bundled Python 3.9: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3` (verify numpy/networkx availability there).
  - Install dir: `~/Documents/KiCad/10.0/scripting/plugins/<name>/`. PCM scaffold later in `packaging/` (`metadata.json.template` v2 + `build_package.py` stub).

## Completed
- [x] Moved trial notebooks + `utilities.py` into `experiments/`; deleted generated artifacts; wrote `.gitignore`, `.env.example`.
- [x] Fixed `requirements.txt`: removed fake `kicad-cli` pip dep (system tool); kept numpy, networkx, pyyaml, pytest, black, mypy.
- [x] `src/kicad_autorouter/pipeline.py`: `Stage` dataclass + `STAGES` (`ingest`, `parse`, `connectivity`, `route`, `drc`, `writeback`; all `"planned"`) — single source of truth for UI.
- [x] `ui/server.py` (stdlib HTTP, `/api/stages`, path-traversal guard) + `ui/index.html` (dark workflow UI: stage cards + file drop). No new pip deps.
- [x] **File format confirmed:** `.kicad_pcb` / `.kicad_sch` are S-expressions, not JSON. Version tags `(version 20241229)` pcb, `(version 20250114)` sch → parser must be an S-expression reader.
- [x] `src/kicad_autorouter/validate.py`: wraps `kicad-cli pcb drc` / `sch erc --format json`; distinguishes load failures (exit 3) from rule violations; header check works without CLI.
- [x] Tests: `tests/test_validate.py` + fixtures in `tests/fixtures/` (EuroCard template). 9 pass; CLI tests skip if `kicad-cli` absent.
- [x] Saved KiCad 10 docs as markdown under `docs/kicad/`: `pcbnew.md`, `kicad.md`, `eeschema.md`, `cli.md` (from app-bundled HTML, exact version match). Wrote `docs/README.md` index.
- [x] `git init`; initial commit `65edcd8`. Identity: `kstammits` / `karl.stamm@gmail.com`.
- [x] Top-level `README.md`: overview, layout, run instructions, and a **References** section pointing guidance docs to `docs/` (keeps the human-facing README lean).
- [x] `src/kicad_autorouter/pcbnew_adapter.py`: only module importing `pcbnew`; `load_board()`, `unlocked_footprints()`, `nudge_unlocked_footprints()`, `add_track()`, `save_board()`.
- [x] `scripts/run_autoroute.py`: headless first-pass entry point (default nudge `--dx-mm 1.0` / `--dy-mm 0.0`; demo F.Cu track unless `--no-track`).
- [x] `.gitignore`: ignore generated outputs under `experiments/out/`.
- [x] **Baby-step first pass verified** under bundled Python: loaded 4 footprints, nudged unlocked `MH1` `(58.57,50.55)→(59.57,50.55)` mm, added F.Cu track `(80,95)→(135,95)` w `0.25`, saved `experiments/out/baby_step.kicad_pcb` (+ `.kicad_pro`). DRC loads clean with 1 expected `track_dangling | Track has unconnected end`; `pytest tests/ -q` → 9 pass.

## Blocked / Known Issues
- No official "file format" page in KiCad 10 manual — need an S-expression grammar source (see Active).
- `kicad-cli` not on PATH; full path `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. Handled by `validate.find_kicad_cli()`.
- Wikipedia fetch results truncated to navigation boilerplate; article content not captured yet (re-fetch needed for guidance docs).
- `Maze_routing` fetch appeared titled "Maze runner" — verify the correct source before drafting the guidance doc.
- Pipeline stage list (`ingest→parse→connectivity→route→drc→writeback`) is tentative, pending user review.

## Environment Notes
- Working dir: `/Users/karl/Workspace/KiCadAutoRouterLab`. `.venv/` exists (gitignored, do not delete); deps installed.
- `.env` = `ENV=DEV` only; gitignored, mirrored in `.env.example`. UI is stdlib-only on purpose.
- KiCad 10.0.7 at `/Applications/KiCad/`; bundled help in `KiCad.app/Contents/SharedSupport/help/en/`.
- **Scope relaxed:** a minimal read-modify-write pass (load → nudge unlocked parts → add demo track → save) is allowed and done. Full routing algorithm / net connectivity still pending; guidance-doc research now in progress.

## Repo Layout (current)
```
KiCadAutoRouterLab/
├── .env / .env.example / .gitignore / README.md / TODO.md / requirements.txt
├── docs/            # README.md index + kicad/{pcbnew,kicad,eeschema,cli}.md
├── experiments/     # old trial notebooks + utilities.py; out/ = generated boards (gitignored)
├── scripts/         # run_autoroute.py (headless first pass)
├── src/kicad_autorouter/   # __init__.py (0.1.0), pipeline.py, validate.py, pcbnew_adapter.py
├── tests/           # conftest.py, fixtures/, test_validate.py
└── ui/              # server.py (/api/stages), index.html
```
