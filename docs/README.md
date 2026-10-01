# Docs index

- [`autorouting_glossary.md`](autorouting_glossary.md) — project guidance: autorouting
  terminology aligned to the pipeline stages (`ingest→parse→connectivity→route→drc→writeback`),
  plus force-directed placement of unlocked components around locked ones. Built from the
  sources below.

## KiCad Documentation (local copies)

Local markdown copies of the **KiCad 10** user documentation, converted from the HTML
docs bundled with the installed KiCad app. These match the exact version in use
(`kicad-cli --version` → 10.0.7), so unlike web docs they cannot drift out of date.

| File | Contents | Source (local) | Upstream URL |
| --- | --- | --- | --- |
| `pcbnew.md` | PCB editor manual — layers, nets, tracks, pads, zones, board outline/edge cuts, DRC, file format notes | `/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en/pcbnew.html` | <https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html> |
| `kicad.md` | Project manager manual — includes "KiCad files and folders" (all `.kicad_*` file types) | `/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en/kicad.html` | <https://docs.kicad.org/10.0/en/kicad/kicad.html> |
| `eeschema.md` | Schematic editor manual — symbols, nets, ERC, sheet structure | `/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en/eeschema.html` | <https://docs.kicad.org/10.0/en/eeschema/eeschema.html> |
| `cli.md` | kicad-cli reference — all subcommands incl. `pcb drc`, `sch erc`, exports, upgrades | `/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en/cli.html` | <https://docs.kicad.org/10.0/en/cli/cli.html> |

Other sections available in the same local help folder (not yet copied):
`introduction.html`, `getting_started_in_kicad.html`, `gerbview.html`,
`pl_editor.html`, `pcb_calculator.html`.

## Wikipedia research sources (for the guidance doc)

Local markdown copies fetched from the en.wikipedia.org REST API on 2026-09-28
(Barnes–Hut and A* added 2026-09-30), to support drafting the autorouting
guidance/glossary doc:

| File | Actual article title | Fetched as | Upstream URL |
| --- | --- | --- | --- |
| `wikipedia/autorouter.md` | Routing (electronic design automation) — wire routing for PCBs/ICs, router task, NP-completeness of routing variants | `Autorouter` (redirect) | <https://en.wikipedia.org/wiki/Autorouter> |
| `wikipedia/maze_routing.md` | Maze runner — grid-based maze routing method; cites Lee's 1961 paper | `Maze_routing` (redirect to `Maze_runner`) | <https://en.wikipedia.org/wiki/Maze_routing> |
| `wikipedia/lee_algorithm.md` | Lee algorithm — BFS wave-expansion maze routing; optimal but slow and memory-hungry | `Lee_algorithm` | <https://en.wikipedia.org/wiki/Lee_algorithm> |
| `wikipedia/force_directed_graph_drawing.md` | Force-directed graph drawing — physical-simulation node placement (basis for force-directed component nudging) | `Force-directed_graph_drawing` | <https://en.wikipedia.org/wiki/Force-directed_graph_drawing> |
| `wikipedia/barnes_hut_simulation.md` | Barnes–Hut simulation — O(n log n) tree approximation of all-pairs forces; scaling path for repulsion past dense n² (~3–5k nodes) | `Barnes%E2%80%93Hut_simulation` (mobile-html REST API) | <https://en.wikipedia.org/wiki/Barnes%E2%80%93Hut_simulation> |
| `wikipedia/a_star_search_algorithm.md` | A* search algorithm — heuristic shortest-path search (f = g + h); cost cut for maze/Lee routing | `A*_search_algorithm` (mobile-html REST API) | <https://en.wikipedia.org/wiki/A*_search_algorithm> |

Note: the Wikipedia article "Maze-routing algorithm" is about general maze
solving (wall follower, Pledge, Trémaux's), not PCB routing — deliberately excluded.

## Key facts for this project

- **File formats are S-expressions, not JSON.** Both `.kicad_pcb` and `.kicad_sch`
  start with `(kicad_pcb (version ...)` / `(kicad_sch (version ...)`. The version tag
  in KiCad 10 is `20241229` (pcb) and `20250114` (sch). Any parser must be an
  S-expression reader, not a JSON one.
- **CLI validation** (see also `src/kicad_autorouter/validate.py`):
  - `kicad-cli pcb drc <board.kicad_pcb> --format json -o report.json` — loads the board and runs DRC; JSON report has top-level `violations[]` and `unconnected_items[]`.
  - `kicad-cli sch erc <sch.kicad_sch> --format json -o report.json` — loads the schematic and runs ERC; violations are nested under `sheets[].violations[]`.
  - A file that fails to load exits non-zero (3) with `Failed to load ...` on stderr.
  - `--exit-code-violations` makes a nonzero exit code reflect DRC/ERC violations instead of only load failures.
- **kicad-cli location on this machine:** `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` (not on PATH).

## Regenerating these copies

```sh
PYTHONPATH=$(mktemp -d) python3 - <<'EOF'  # after: pip install --target $PYTHONPATH markdownify
from markdownify import MarkdownConverter
import pathlib
src = pathlib.Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en")
dst = pathlib.Path("docs/kicad")
conv = MarkdownConverter(heading_style="ATX", bullets="-")
for name in ["pcbnew.html", "kicad.html", "cli.html", "eeschema.html"]:
    (dst / (name.replace(".html", ".md"))).write_text(conv.convert((src / name).read_text()), encoding="utf-8")
EOF
```

Wikipedia research sources are fetched from the `mobile-html` REST endpoint and
converted with markdownify (same settings as above):

```sh
TMP=$(mktemp -d); pip install --target "$TMP/py" markdownify
curl -sf "https://en.wikipedia.org/api/rest_v1/page/mobile-html/A*_search_algorithm" -o "$TMP/p.html"
PYTHONPATH="$TMP/py" python3 -c '
from markdownify import MarkdownConverter
import pathlib, sys
conv = MarkdownConverter(heading_style="ATX", bullets="-")
pathlib.Path("docs/wikipedia/a_star_search_algorithm.md").write_text(conv.convert(pathlib.Path(sys.argv[1]).read_text()), encoding="utf-8")
' "$TMP/p.html"
```
