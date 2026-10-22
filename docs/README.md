# External references

Upstream sources for the guidance in
[`autorouting_glossary.md`](autorouting_glossary.md). Local KiCad manuals and
Wikipedia copies are not vendored; use the links below or restore copies with
the recipes at the end.

## KiCad documentation

| Topic | Upstream URL |
| --- | --- |
| PCB editor: layers, nets, tracks, pads, zones, outline, DRC | <https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html> |
| Project manager: KiCad files and folders | <https://docs.kicad.org/10.0/en/kicad/kicad.html> |
| Schematic editor: symbols, nets, ERC, hierarchy | <https://docs.kicad.org/10.0/en/eeschema/eeschema.html> |
| `kicad-cli`: `pcb drc`, `sch erc`, exports, upgrades | <https://docs.kicad.org/10.0/en/cli/cli.html> |

## Wikipedia background

| Article | Used for |
| --- | --- |
| [Autorouter](https://en.wikipedia.org/wiki/Autorouter) | Routing task and failure modes |
| [Maze routing](https://en.wikipedia.org/wiki/Maze_routing) | Grid maze-routing model |
| [Lee algorithm](https://en.wikipedia.org/wiki/Lee_algorithm) | BFS wave expansion |
| [Force-directed graph drawing](https://en.wikipedia.org/wiki/Force-directed_graph_drawing) | Physical placement model |
| [Barnes–Hut simulation](https://en.wikipedia.org/wiki/Barnes%E2%80%93Hut_simulation) | Scaling all-pairs repulsion |
| [A* search](https://en.wikipedia.org/wiki/A*_search_algorithm) | Heuristic shortest-path search |

## Key facts for this project

- **File formats are S-expressions, not JSON.** Both `.kicad_pcb` and
  `.kicad_sch` start with `(kicad_pcb (version ...)` /
  `(kicad_sch (version ...)`. The version tag in KiCad 10 is `20241229`
  (pcb) and `20250114` (sch).
- **CLI validation** (see also `src/kicad_autorouter/validate.py`):
  - `kicad-cli pcb drc <board.kicad_pcb> --format json -o report.json` —
    the JSON report has top-level `violations[]` and `unconnected_items[]`.
  - `kicad-cli sch erc <sch.kicad_sch> --format json -o report.json` —
    violations are nested under `sheets[].violations[]`.
  - A file that fails to load exits non-zero (3) with `Failed to load ...`
    on stderr.
  - `--exit-code-violations` makes the exit code reflect DRC/ERC violations
    instead of only load failures.
- **kicad-cli location on this machine:**
  `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` (not on PATH).

## Restoring local copies

KiCad manuals (requires `markdownify`):

```sh
PYTHONPATH=$(mktemp -d) python3 - <<'EOF'
from markdownify import MarkdownConverter
import pathlib
src = pathlib.Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/help/en")
dst = pathlib.Path("docs/kicad")
dst.mkdir(parents=True, exist_ok=True)
conv = MarkdownConverter(heading_style="ATX", bullets="-")
for name in ["pcbnew.html", "kicad.html", "cli.html", "eeschema.html"]:
    (dst / name.replace(".html", ".md")).write_text(
        conv.convert((src / name).read_text()), encoding="utf-8"
    )
EOF
```

Wikipedia articles (example for A*):

```sh
TMP=$(mktemp -d)
curl -sf "https://en.wikipedia.org/api/rest_v1/page/mobile-html/A*_search_algorithm" \
  -o "$TMP/page.html"
PYTHONPATH="$TMP/py" python3 -c '
from markdownify import MarkdownConverter
import pathlib, sys
conv = MarkdownConverter(heading_style="ATX", bullets="-")
pathlib.Path("docs/wikipedia/a_star_search_algorithm.md").write_text(
    conv.convert(pathlib.Path(sys.argv[1]).read_text()), encoding="utf-8"
)
' "$TMP/page.html"
```
