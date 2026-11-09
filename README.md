# KiCad AutoRouter Lab

> **Status:** Early stage — works on real boards (tube111, DCCF), DRC-validated. Expect rough edges. Use at your own risk.

## What Is This?

An autorouter for KiCad that **actually works**. No `pcbnew` scripting, no C++ plugins, no license servers. Drop a `.kicad_pcb` file into the web UI or run headless from CLI — get routed tracks back.

**The core idea:** Physics-based placement nudges parts organically around locked components, then a grid-based A* router finds paths with full clearance awareness. GND pours are handled specially (passable-at-toll, auto-stitched).

---

## Why It Exists

| You've probably... | This does instead... |
|--------------------|----------------------|
| Spent 4 hours manually routing a board | Routes the same board for you |
| Moved a connector and watched 50 nets break | Rip-up/retry is automatic — move parts, re-route, done |
| Had GND pours fragment into isolated islands | Auto-stitching vias + bridge tracks repair continuity |
| Fought netclass width rules being ignored | Per-net width resolution + click-to-override power rails |
| Wished for an autorouter that "just works" in KiCad | Pure Python, runs in your browser, saves to your board |

---

## The Unusual Feature: Physics-Based Part Migration

Most autorouters treat footprints as rigid blocks. This one runs a **per-pad force-spring simulation**:

- **Each pad feels forces independently** — connected pads attract (shorten wires), all pads repel (clearance)
- **Parts rotate and translate organically** — no pre-defined "slots" or "legal positions"
- **Locked parts are immovable anchors** — unlocked parts flow around them like water
- **Decoupling caps get magnetic attraction** to their power/ground pins — they snap into place

> **Result:** Parts migrate to natural positions for the actual connectivity, not a grid. You get layouts that look like a thoughtful human placed them — because the physics *is* thoughtful placement.

---

## Quick Start (2 minutes)

```bash
# 1. Clone and set up
git clone https://github.com/your-repo/KiCadAutoRouterLab.git
cd KiCadAutoRouterLab
python -m venv .venv
.venv/bin/pip install -e .

# 2. Launch the web UI
.venv/bin/python ui/server.py
# Opens http://127.0.0.1:8000
```

1. **Load a board** — drag `tests/fixtures/tube111.kicad_pcb` onto the page (or click "Load…")
2. **Place** — click parts to select, hit **Step** (5 iterations) or **Run** (to convergence), **Accept**
3. **Route** — switch to **Route** tab, pick nets, adjust widths if needed, **Route Selected**
4. **DRC** — tick **DRC** checkbox, **Run DRC** — violations overlay on the board
5. **Save** — **Download** the modified `.kicad_pcb`

**Try the included test boards:**
- `tests/fixtures/tube111.kicad_pcb` — 105 parts, 6 GND zones, 73 nets (reference board with pours)
- `tests/fixtures/DCCF.sved.kicad_pcb` — Audio board, decaps, split power rails

---

## Two Ways to Use It

| Mode | Best For | How |
|------|----------|-----|
| **Web UI** (recommended) | Interactive placement + routing, visual feedback, iteration | `python ui/server.py` → browser |
| **Headless CLI** | CI/CD, batch processing, automation | `scripts/run_autoroute.py board.kicad_pcb -o out.kicad_pcb --step-iterations 5 --max-total-iterations 500` |

Both use the same pure-Python core. The UI adds SVG visualization, motion playback, and live parameter sliders.

---

## Core Workflows

### 1. Placement — Nudge Parts Into Position
```
Select parts → Step/Run → Ghost preview shows proposed moves → Accept → Repeat
```
- **Step** = 5 iterations (fast, incremental)
- **Run** = converges to equilibrium (uses saved max iterations)
- **Pin** (Alt+click) = exclude parts from movement
- **Ghost courtyards + arrows** show where parts want to go

### Labels ride along — silkscreen is physics too
Each footprint's reference label (`F.SilkS`/`F.Fab`) moves with its part during placement — it is a low-mass node on a spring tether, not welded text. Labels snap to upright orientations (never upside-down) when their parent rotates, and they demand clearance around themselves through the same wall law as courtyards, so dense boards make room instead of plastering text over copper. One slider drives it all: **Label clearance (mm)** (default 5 mm) sets both the clearance reach and the orbit leash length; 0 parks every label at its filed anchor. Parent footprints never feel label forces, so pad trajectories match label-unaware runs.

### 2. Routing — Connect the Nets```
Route tab → Select nets (click rows, Shift/Ctrl) → Set Track/Power Width → Route Selected
```
- **Progress bar + cancel** per net (one net per request, no server threading needed)
- **Per-net width resolution**: explicit → netclass → power → default
- **Power rails**: click the **⚡ Pwr** column to mark any net as power → gets wide tracks + placement magnetism
- **GND Stitching**: auto-repairs pour continuity after routing (on by default)

### 3. Iterate — The Real Workflow
```
Move parts → nets rip up automatically → Re-route → DRC → Done
```
Placement and routing are separate passes but designed to cycle. Move a connector, the connected nets rip up, re-route cleanly.

---

## What Makes It Different

| Feature | Why It Matters |
|---------|----------------|
| **Per-pad physics** (not whole-footprint) | Parts rotate organically; decaps snap to pins; no "legal position" constraints |
| **Labels ride along** (orbit, always on) | Silkscreen refs are tethered nodes that snap upright and demand clearance — one **Label clearance** slider, parents unaffected |
| **GND pours passable-at-toll** | Router crosses pours when needed (soft cost), never gets stuck on pads inside pours |
| **GND-only rip-up** | `Strip GND Blockers` evicts only GND vias/tracks; signal/power copper is **never evicted** |
| **10° angle smoothing** (default) | 36 angle options vs hardcoded 45° — routes look shallower, more gradual |
| **Params auto-save** | All sliders persist to `placement.json` — your tuned settings survive restarts |
| **Region-aware** | Multi-panel boards: stitcher never joins separate panels |
| **Copper layer solo + legend** | Per-layer Solo/All isolate buttons and a layer color key (display-only, no re-fetch) |
| **DRC gate** | `kicad-cli pcb drc` runs on writeback — the acceptance check |

---

## Routing Controls Cheat Sheet

| Control | What It Does | Typical Range |
|---------|--------------|---------------|
| **Track Width** | Default signal track width | 0.15–0.25 mm |
| **Power Width** | Power/ground track width | 0.4–0.8 mm |
| **Via Cost** | Wire-equivalent mm per via (higher = fewer layer changes) | 3–10 mm |
| **Clearance** | Minimum spacing between tracks | 0.15–0.25 mm |
| **Grid Resolution** | A* cell size (0.05 = 4× cells, slower) | 0.1 mm default |
| **Pour Severance Weight** | Penalty for narrowing a pour corridor | 0–100 (0 = off) |
| **GND Stitching** | Auto-add vias/bridges to connect pour islands (2-layer mode) | **On** |
| **Strip GND Blockers** | Remove old GND copper before routing (2-layer mode) | **On** |
| **2-layer / 4-layer toggle** | 2-layer: outer-only routing, export collapses to genuine 2-layer. 4-layer: same outer routing, but inner planes are preserved and tapped (see below) | Top bar |

### Inner planes (4-layer boards)

In 4-layer mode, ground and power nets that own an inner-copper zone (e.g. an `In2.Cu` GND plane) are connected with a short outer stub + through-via per SMD pad instead of long outer tracks — the plane itself joins the stubs. Through-hole pads need no stub (the barrel already pierces the plane). Results are reported separately as `fanout_vias_added` / `fanout_tracks_added`. 2-layer mode never does this: inner copper is ignored while routing and removed on export.

---

## Keyboard Shortcuts (UI)

| Key | Action |
|-----|--------|
| `A` | Select all unlocked, unpinned footprints |
| `Escape` | Clear selection |
| `Shift+click` | Multi-select footprints / net rows |
| `Alt+click` | Pin/unpin footprint (orange dashed = pinned) |
| Mouse wheel | Zoom |
| Drag | Pan |
| `Fit` button | Reset view |

---

## Test Boards Included

| Board | Description | Good For Testing |
|-------|-------------|------------------|
| `tests/fixtures/tube111.kicad_pcb` | 105 footprints, 6 GND zones, 73 nets, pre-routed | Full flow: placement → routing → GND stitching → DRC |
| `tests/fixtures/DCCF.sved.kicad_pcb` | Audio board, decaps, split ± rails | Power/ground handling, decap fanout, organic placement |

---

## Learn More

| Doc | What You'll Find |
|-----|------------------|
| [`ui/manual.html`](ui/manual.html) | Generated placement manual with live before/after examples |
| [`docs/autorouting_glossary.md`](docs/autorouting_glossary.md) | Pipeline stages, terminology, algorithm guidance (Lee/A*, force-directed, pour handling) |
| [`docs/si_force_laws.md`](docs/si_force_laws.md) | Normative placement force laws and parameter defaults |
| [`docs/physics_primer.qmd`](docs/physics_primer.qmd) | Force-law primer with generated figures and math |
| [`docs/roadmap.md`](docs/roadmap.md) | Open placement, routing, and pour work |
| [`docs/README.md`](docs/README.md) | Upstream KiCad and Wikipedia references, with fetch recipes |

Regenerate the manual, figures, and rendered primer with `./scripts/regen_docs.sh`.

---

## Requirements

- Python 3.11+
- KiCad 10.x (for `kicad-cli` DRC validation — optional but recommended)
- `.venv` with project dependencies (`pip install -e .`)

**No `pcbnew` Python module needed** for routing/placement — only used as an offline test oracle.

---

## Expected Runtime Performance

Indicative wall-clock numbers on a desktop machine (load-dependent, ±30%).
Raw data: `docs/benchmarks/` (`placement` / `routing` / `stitch_pour` /
`tight_spots` / `corridor` dated JSONs, newest per family).

| Task | Board / scale | Typical time |
|------|---------------|--------------|
| Placement **Step** (5 iters) | tube111 (105 parts) | < 1 s |
| Placement **Run** to budget (1000 iters) | tube111 / DCCF (~100 parts) | ~25–35 s |
| Placement Run to budget | 256-part synthetic | ~90 s (~125 ms/iter) |
| Route one easy signal net | either reference board | ~0.5–1 s |
| Route full signal set, healthy board | DCCF, 44 nets, 0.1 mm grid | ~45 s (~1 s/net, linear) |
| Route full signal set, congested board | tube111, 60 nets | ~3–4 min, ~1/3 of nets fail |
| One failed hard net (e.g. DCCF VIN) | hits per-net expansion cap | ~30 s, then reported as failed |
| GND stitch + pour repair | either reference board | ~1–3 s |
| Coarser grid (0.2 mm vs 0.1 mm) | DCCF 10-net sample | ~4× faster (2.5 s vs ~10 s) |

Growth rules of thumb:

- **Placement** costs ~N^1.2–1.5 per iteration in the 64–256 part window
  (footprint-pair term dominates; pad count matters less), and
  to-convergence runs typically spend the full iteration budget — so
  estimate total cost as per-iteration cost × iterations.
- **Routing** is ~1 s/net while the board is healthy, then degrades
  superlinearly on congested tails (tube111's last 30 nets cost 10× the
  first 30 and mostly fail). Route easy nets first; hard power nets are
  cheaper retried piecemeal than all-at-once.
- **Grid resolution** finer than 0.1 mm rarely pays: 0.07 mm trips the
  12M-cell guard on DCCF-sized boards. Prefer 0.15–0.2 mm for scouting,
  0.1 mm for final.

### Example user stories

- **Lunch-break tweak**: "I moved a connector on my 100-part board and need
  to re-route 3 nets." Select the nets → Route Selected → done in
  ~5 s, DRC-clean.
- **Evening full route**: "Route all 44 signal nets on the DCCF audio
  board." One batch run, ~45 s, all routed; stitching repairs the pour in
  ~2 s; run DRC and export.
- **Congested board triage**: "Route everything on tube111." ~3–4 min,
  ~40/60 nets route; the 18 failures are all reported with reasons
  (`no path found` on tight clusters). Route the failures in small subsets
  or widen clearances, rather than re-running the whole board.
- **Placement exploration**: "Nudge 4 parts around a locked connector."
  Step a few times (< 1 s total), watch the ghost preview, Accept. Full
  100-part Run takes ~30 s — use it sparingly, Step often.

---

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).