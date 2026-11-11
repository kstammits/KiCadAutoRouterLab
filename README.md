# KiCad AutoRouter Lab

> **Status:** Early stage — works on real boards (tube111, DCCF), DRC-validated. Expect rough edges. Use at your own risk.

Pure-Python autorouter for KiCad: drop a `.kicad_pcb` into the web UI or run headless, get routed tracks back. No `pcbnew` scripting, no C++ plugins.

- **Per-pad force placement** — connected pads attract, all pads repel; parts rotate and slide organically around locked anchors. Decap magnets snap caps to pins; silkscreen labels ride as tethered nodes.
- **Grid A* routing** — full clearance awareness, per-net widths, power-rail overrides, GND pours passable-at-toll with auto-stitching.
- **DRC gate** — `kicad-cli pcb drc` runs on writeback; nothing DRC-dirty is stamped silently.

---

## Quick Start (2 minutes)

```bash
git clone https://github.com/kstammits/KiCadAutoRouterLab.git
cd KiCadAutoRouterLab
python -m venv .venv
.venv/bin/pip install -e .
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

Indicative desktop wall-clock (±30%). Raw data: `docs/benchmarks/`, newest per family.

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
| [`ui/manual.html`](ui/manual.html) | Placement manual with live before/after examples |
| [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) | Pipeline stages, terminology, algorithm guidance |
| [`docs/si_force_laws.md`](docs/si_force_laws.md) | Placement force laws and parameter defaults |
| [`docs/physics_primer.qmd`](docs/physics_primer.qmd) | Force-law primer with figures and math |
| [`docs/roadmap.md`](docs/roadmap.md) | Open work |
| [`docs/README.md`](docs/README.md) | Upstream references with fetch recipes |

Regenerate the manual/figures/primer with `./scripts/regen_docs.sh`.

---

## Requirements

- Python 3.11+, `.venv` with project deps (`pip install -e .`)
- KiCad 10.x `kicad-cli` for DRC validation (recommended; `pcbnew` module not needed)

---

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
