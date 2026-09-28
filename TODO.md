# KiCad AutoRouter Lab — TODO / Session State

Project goal: a utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Current phase: **scaffolding + validation tooling** — no autorouter logic yet. System is on **KiCad 10** (installed app, `kicad-cli` 10.0.7); all local docs are KiCad 10.

## Completed

- [x] Moved `t.ipynb`, `trial.ipynb`, `trial2.ipynb`, `utilities.py` into `experiments/`.
- [x] Deleted generated artifacts: `Alice_memo.json`, `Bob_memo.json`, `__pycache__/`.
- [x] Wrote `.gitignore` (venv, `.env`, pycache, Jupyter checkpoints, memo jsons, OS/editor files).
- [x] Wrote `.env.example` (`ENV=DEV`).
- [x] Fixed `requirements.txt`: removed fake `kicad-cli>=6.0.0` pip dep (not a PyPI package); kept numpy, networkx, pyyaml, pytest, black, mypy; added comment that KiCad CLI is a system tool.
- [x] Created `src/kicad_autorouter/__init__.py` (version 0.1.0).
- [x] Created `src/kicad_autorouter/pipeline.py`: `Stage` dataclass + `STAGES` list — single source of truth for the UI. Stages: `ingest`, `parse`, `connectivity`, `route`, `drc`, `writeback`; all status `"planned"`.
- [x] Created `ui/server.py`: stdlib-only HTTP server (`http.server.ThreadingHTTPServer`), serves static files from `ui/` with path-traversal guard, plus `/api/stages` JSON endpoint. No new pip deps.
- [x] Created `ui/index.html`: dark-theme single-page workflow UI — pipeline stage cards fetched from `/api/stages`, drag-and-drop placeholder for `.kicad_pcb` / `.kicad_sch`.
- [x] `git init`, staged all files, initial commit (`65edcd8`). Git identity: `kstammits` / `karl.stamm@gmail.com`.
- [x] **Saved KiCad 10 docs as local markdown under `docs/kicad/`:** `pcbnew.md` (PCB editor manual), `kicad.md` (project manager, incl. "KiCad files and folders"), `eeschema.md` (schematic editor), `cli.md` (CLI reference). Source: HTML bundled with the installed app (`/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en/*.html`) — exact version match, no web fetch needed.
- [x] Wrote `docs/README.md`: index of local docs with source paths + upstream URLs (pattern `https://docs.kicad.org/10.0/en/<section>/<page>.html`).
- [x] **Determined the real file format: `.kicad_pcb` and `.kicad_sch` are S-expressions, NOT JSON.** KiCad 10 version tags: `(version 20241229)` for pcb, `(version 20250114)` for sch. Parser work must target an S-expression reader.
- [x] **Built CLI file validation:** `src/kicad_autorouter/validate.py` wraps `kicad-cli pcb drc` / `sch erc` with `--format json`; distinguishes load failures (exit 3, "Failed to load" on stderr) from design-rule violations. Header check (`looks_like_kicad_file`) works without the CLI.
- [x] **Tests:** `tests/test_validate.py` + minimal fixtures in `tests/fixtures/` (from KiCad's EuroCard template). 9 tests pass: header checks, valid pcb/sch pass DRC/ERC with 0 violations, corrupt files fail to load, missing file reports error. CLI-dependent tests skip cleanly if kicad-cli is absent.

## Active / Next Moves (in order)
- [ ] Review how to do a plugin for Kicad — we may need UI elements or extended CLI.
- [ ] Find an **S-expression grammar reference** for `.kicad_pcb` / `.kicad_sch` (the earlier "JSON schema" search was based on a wrong assumption). Candidates: KiCad source parsers (`pcb/`, `eeschema/` in the kicad repo), KiKit docs (`https://yaqwsx.github.io/KiKit/latest/`), plus the local manuals in `docs/kicad/`.
- [ ] Write `README.md`: project overview, repo layout, workflow stages, how to run the UI (`python ui/server.py`) and tests (`.venv/bin/python -m pytest tests/`), doc links, roadmap.

## Blocked / Known Issues

- No dedicated official "file format" page in the KiCad 10 user manual — need an S-expression grammar source (see candidates above).
- `kicad-cli` is not on PATH; full path: `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. `validate.find_kicad_cli()` handles this.

## Environment Notes

- Working dir: `/Users/karl/Workspace/KiCadAutoRouterLab`.
- `.env` contains only `ENV=DEV` — no secrets; gitignored, mirrored in `.env.example`.
- `.venv/` exists and is gitignored (do not delete). Project deps installed (`pip install -r requirements.txt`).
- UI is stdlib-only on purpose: no new pip dependencies.
- KiCad 10.0.7 app at `/Applications/KiCad/`; bundled English help in `KiCad.app/Contents/SharedSupport/help/en/`.
- User constraint: **do not build the autorouter yet** — docs + workflow UI + validation only for now.

## Repo Layout (current)

```
KiCadAutoRouterLab/
├── .env                  # gitignored, ENV=DEV
├── .env.example
├── .gitignore
├── TODO.md               # this file
├── requirements.txt      # numpy, networkx, pyyaml, pytest, black, mypy
├── docs/
│   ├── README.md         # index of local KiCad 10 docs + key facts
│   └── kicad/            # markdown copies: pcbnew.md, kicad.md, eeschema.md, cli.md
├── experiments/          # old agentic-chat trial notebooks + utilities.py
│   ├── t.ipynb
│   ├── trial.ipynb
│   ├── trial2.ipynb
│   └── utilities.py
├── src/kicad_autorouter/
│   ├── __init__.py       # version 0.1.0
│   ├── pipeline.py       # Stage dataclass + STAGES (ingest→parse→connectivity→route→drc→writeback)
│   └── validate.py       # kicad-cli DRC/ERC validation wrapper
├── tests/
│   ├── conftest.py       # adds src/ to sys.path
│   ├── fixtures/         # minimal.kicad_pcb, minimal.kicad_sch (KiCad EuroCard template)
│   └── test_validate.py  # 9 tests: headers, DRC/ERC pass/fail paths
└── ui/
    ├── server.py         # stdlib HTTP server, /api/stages endpoint
    └── index.html        # workflow UI: stage cards + file drop zone
```
