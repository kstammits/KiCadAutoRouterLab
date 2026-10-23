#!/usr/bin/env bash
# Regenerate the committed figures, the user manual, and the Quarto primer.
# Usage: scripts/regen_docs.sh [--pdf]
#   default : figures + ui/manual.html + docs/_output/html/physics_primer.html
#   --pdf   : also render docs/_output/pdf/physics_primer.pdf (needs TinyTeX)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"
OUTPUT_DIR="$DOCS_DIR/_output"

render_pdf=0
for arg in "$@"; do
    case "$arg" in
        --pdf) render_pdf=1 ;;
        -h|--help)
            sed -n '2,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

if ! command -v quarto >/dev/null 2>&1; then
    echo "quarto not found on PATH" >&2
    exit 1
fi

cd "$REPO_ROOT"

.venv/bin/python scripts/plot_force_laws.py --outdir docs/figures
.venv/bin/python scripts/gen_manual.py

(
    cd "$DOCS_DIR"
    quarto render physics_primer.qmd --to html --output-dir "$OUTPUT_DIR/html"
    if [ "$render_pdf" -eq 1 ]; then
        quarto render physics_primer.qmd --to pdf --output-dir "$OUTPUT_DIR/pdf"
    fi
)

echo "HTML:   $OUTPUT_DIR/html/physics_primer.html"
if [ "$render_pdf" -eq 1 ]; then
    echo "PDF:    $OUTPUT_DIR/pdf/physics_primer.pdf"
fi
echo "Manual: $REPO_ROOT/ui/manual.html"
