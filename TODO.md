# KiCad AutoRouter Lab — TODO / Session State

Project goal: a utility that reads KiCad files (`.kicad_pcb`, `.kicad_sch`) and updates PCB trace placement.
Current phase: **scaffolding only** — no autorouter logic yet. Grab KiCad docs, set up workflow UI, get repo committed. We're also updating the system to Kicad 10, so the below links to docs for v9 may be outdated. Review the docs links. 

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
- [x] Identified working KiCad doc URL pattern: `https://docs.kicad.org/9.0/en/<section>/<page>.html`. Sections: `introduction`, `getting_started_in_kicad`, `kicad`, `eeschema`, `pcbnew`, `gerbview`, `pl_editor`, `pcb_calculator`, `cli`.
- [x] Fetched full PCB editor manual (`https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html`, ~471 KB) — saved to tool-output file `/Users/karl/.local/share/opencode/tool-output/tool_0e89828e3001pkJ7A9256OL55G` (ephemeral; re-fetch if gone).
- [x] Fetched KiCad project manager manual TOC (`https://docs.kicad.org/9.0/en/kicad/kicad.html`) — has "KiCad files and folders" section with subsections on project/schematic/board/common/fabrication files.

## Active / Next Moves (in order)
- [ ] Review how to do a plugin for Kicad, we may need UI elements or extended CLI. 
- [ ] **Save KiCad docs as local markdown under `docs/kicad/`:**
  - Extract relevant sections from the PCB editor manual: board layers, nets, tracks, pads, zones, edge cuts / board outline. Source: re-fetch `https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html` (tool-output file may be gone).
  - Fetch + save "KiCad files and folders" section from `https://docs.kicad.org/9.0/en/kicad/kicad.html`.
- [ ] **Find a `.kicad_pcb` / `.kicad_sch` JSON file-format reference** (NOT in the official 9.0 user manual — grep confirmed). Candidates:
  - GitLab raw source files: `https://gitlab.com/kicad/source/mirror/-/raw/master/doc/<file>` (tree pages return 403 to bots; try raw URLs).
  - KiKit docs: `https://yaqwsx.github.io/KiKit/latest/` (repo: `github.com/yaqwsx/KiKit`) — practical reference for `.kicad_pcb` JSON parsing.
  - KiCad 9 release notes / dev.kicad.info wiki (`https://dev.kicad.info/trac/wiki/User/FileFormats` was a transport error last try).
- [ ] Write `README.md`: project overview, repo layout, workflow stages, how to run the UI (`python ui/server.py`), doc links, roadmap.
- [ ] Write `docs/README.md`: index of local KiCad docs with upstream source URLs.
- [ ] `git init`, stage all files, initial commit. (Git identity already configured: `kstammits` / `karl.stamm@gmail.com`, git 2.39.3.)

## Blocked / Known Issues

- No dedicated official "file format" page in the KiCad 9.0 user manual — need alternative source for `.kicad_pcb` JSON schema (see candidates above).
- GitLab blocks bot access to tree pages (403); raw-file URLs not yet tried.
- `dev.kicad.info` wiki unreachable (transport error) as of last session.

## Environment Notes

- Working dir: `/Users/karl/Workspace/KiCadAutoRouterLab`.
- `.env` contains only `ENV=DEV` — no secrets; gitignored, mirrored in `.env.example`.
- `.venv/` exists and is gitignored (do not delete).
- UI is stdlib-only on purpose: no new pip dependencies.
- User constraint: **do not build the autorouter yet** — docs + workflow UI only for now.

## Repo Layout (current)

```
KiCadAutoRouterLab/
├── .env                  # gitignored, ENV=DEV
├── .env.example
├── .gitignore
├── TODO.md               # this file
├── requirements.txt      # numpy, networkx, pyyaml, pytest, black, mypy
├── experiments/          # old agentic-chat trial notebooks + utilities.py
│   ├── t.ipynb
│   ├── trial.ipynb
│   ├── trial2.ipynb
│   └── utilities.py
├── src/kicad_autorouter/
│   ├── __init__.py       # version 0.1.0
│   └── pipeline.py       # Stage dataclass + STAGES (ingest→parse→connectivity→route→drc→writeback)
└── ui/
    ├── server.py         # stdlib HTTP server, /api/stages endpoint
    └── index.html        # workflow UI: stage cards + file drop zone
```
