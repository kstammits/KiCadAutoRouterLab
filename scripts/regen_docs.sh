#!/usr/bin/env bash
# Regenerate the committed figures, the user manual, and the Quarto primer.
# Usage: scripts/regen_docs.sh [--only-manual] [--skip-quarto] [--pdf]
#   default       : figures + ui/manual.html + docs/_output/html/physics_primer.html
#   --only-manual : figures + ui/manual.html only (never needs Quarto)
#   --skip-quarto : figures + manual, then primer only if quarto is present
#   --pdf         : also render docs/_output/pdf/physics_primer.pdf (needs TinyTeX)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"
OUTPUT_DIR="$DOCS_DIR/_output"

render_pdf=0
only_manual=0
skip_quarto=0
for arg in "$@"; do
    case "$arg" in
        --pdf) render_pdf=1 ;;
        --only-manual) only_manual=1 ;;
        --skip-quarto) skip_quarto=1 ;;
        -h|--help)
            sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

cd "$REPO_ROOT"

.venv/bin/python scripts/plot_force_laws.py --outdir docs/figures
.venv/bin/python scripts/gen_manual.py

if [ "$only_manual" -eq 1 ]; then
    echo "Manual: $REPO_ROOT/ui/manual.html (primer skipped)"
    exit 0
fi

if ! command -v quarto >/dev/null 2>&1; then
    if [ "$skip_quarto" -eq 1 ]; then
        echo "quarto not found on PATH — primer skipped (--skip-quarto)" >&2
        echo "Manual: $REPO_ROOT/ui/manual.html"
        exit 0
    fi
    echo "quarto not found on PATH (use --only-manual or --skip-quarto to refresh figures + manual without it)" >&2
    exit 1
fi

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
