# Karltron Autorouter

> **Status:** Early stage — works on real boards (tube111, DCCF), DRC-validated. Expect rough edges. Use at your own risk.
> Part of the KiCad AutoRouter Lab project.

Pure-Python autorouter for KiCad: drop a `.kicad_pcb` into the web UI or run headless, get routed tracks back. No `pcbnew` scripting, no C++ plugins.

**Workflow: 1 Load → 2 Place (Step / Run + Accept) → 3 Route → 4 DRC + Download.**

| What it does | How you reach it |
|--------------|------------------|
| **Per-pad force placement** — connected pads attract, all pads repel; parts rotate and slide around locked anchors | Place tab → Step / Run / Accept; Regime presets (Scatter start, Placed-board polish, Tidy align); ghosts as keepout walls / attractors |
| **Grid A* routing** — clearance-aware, per-net widths, power-rail overrides, GND pours passable-at-toll with auto-stitching | Route tab → Route Selected / Route All / Refine Order / Polish; 2-layer / 4-layer mode switch |
| **Power integrity (report only)** — inlet-anchored DC-resistance screening, mated-pair bridges | Route tab → Power integrity + Mated pairs sections |
| **DRC gate** — `kicad-cli pcb drc` runs on writeback; nothing DRC-dirty is stamped silently | Top bar → Run DRC, then Download |
| **Review & share** — motion playback, force vectors, context save/load, copy-link presets | Motion bar, Show proposal / forces toggles, Save/Load ctx, Copy link |

---

## Quick Start (2 minutes)

```bash
git clone https://github.com/kstammits/KiCadAutoRouterLab.git
cd KiCadAutoRouterLab
python -m venv .venv
.venv/bin/pip install -e ".[dev]" --constraint requirements-lock.txt
.venv/bin/python ui/server.py
# Opens http://127.0.0.1:8000
```

1. **Load** — drag `tests/fixtures/tube111.kicad_pcb` onto the page.
2. **Place** — select parts, **Step** (5 iters) or **Run** (to convergence), **Accept**. Alt+click pins a part.
3. **Route** — Route tab → select nets → **Route Selected**.
4. **DRC** — tick **DRC**, **Run DRC**, then **Download** the board.

Headless: `scripts/run_autoroute.py board.kicad_pcb -o out.kicad_pcb --step-iterations 5 --max-total-iterations 500`. Same core; the UI adds SVG view, motion playback, sliders.

Test boards: `tests/fixtures/tube111.kicad_pcb` (105 parts, 73 nets) and `tests/fixtures/DCCF.sved.kicad_pcb` (audio, split rails).

---

## Routing Controls

| Control | Typical Range |
|---------|---------------|
| **Track / Power Width** | 0.15–0.25 / 0.4–0.8 mm; per-net override, then netclass, then power flag |
| **Via Cost / Clearance** | 3–10 mm per via; 0.15–0.25 mm spacing |
| **Grid Resolution** | 0.1 mm default (0.15–0.2 for scouting; finer than 0.1 rarely pays) |
| **Smoothing Effort** | 0 = raw search path … 2 = full arcs (default), 3 = high-effort retry; **Polish** re-smooths picked nets, keeps only shorter+clean |
| **GND Stitching / Strip GND Blockers** | On (2-layer mode) — stitching repairs pour islands; strip evicts only GND copper, never signals |
| **2-layer / 4-layer** | 2-layer collapses to genuine 2-layer on export; 4-layer taps inner planes with stub+vias (`fanout_vias_added`) |

Details live in the manual and glossary (links below), not here.

---

## Keyboard Shortcuts

`A` select unlocked · `Shift+click` multi-select · `Alt+click` pin · wheel zoom · drag pan · `Fit` reset · `Escape` clear.

---

## Performance

Indicative desktop wall-clock (±30%), measured locally — regenerate with `scripts/benchmark_*.py` (`docs/benchmarks/` is untracked; see its README).

| Task | Typical time |
|------|--------------|
| Placement **Step** (5 iters, ~100 parts) | < 1 s |
| Placement **Run** to budget (1000 iters) | ~25–35 s |
| tube111 full signal set (60 nets, 0.1 mm) | ~15 s, 60/60 routed |
| DCCF full signal set (44 nets) | ~45 s |
| GND stitch + pour repair | ~1–3 s |

Rules of thumb: placement costs ~N^1.2–1.5 per iteration — estimate per-iteration cost × iterations. Routing is ~1 s/net on healthy boards.

---

## Learn More

| Doc | What You'll Find |
|-----|------------------|
| [`ui/manual.html`](ui/manual.html) | **User:** Placement manual with live before/after examples (placement only; routing lives in the app Route tab) |
| [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) | **User:** Pipeline stages, terminology, algorithm guidance |
| [`docs/si_force_laws.md`](docs/si_force_laws.md) | **Dev reference:** Placement force laws and parameter defaults |
| [`docs/physics_primer.qmd`](docs/physics_primer.qmd) | **Dev reference:** Force-law primer with figures and math |
| [`docs/roadmap.md`](docs/roadmap.md) | **Dev:** Open work |
| [`docs/README.md`](docs/README.md) | **Dev:** Upstream references with fetch recipes |

Regenerate the manual/figures with `.venv/bin/python scripts/gen_manual.py`, or everything (figures + manual + primer) with `./scripts/regen_docs.sh [--only-manual | --skip-quarto | --pdf]`.

---

## Requirements

- Python 3.11+, `.venv` with project deps (`pip install -e ".[dev]" --constraint requirements-lock.txt`, ~600 MB with dev extras)
- KiCad 10.x `kicad-cli` for DRC validation (recommended; `pcbnew` module not needed)

---

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
