#!/usr/bin/env bash
# Regenerate physics_primer.qmd figures by running the embedded Python cells.
# The QMD uses Quarto's executable code blocks, so this script just re-renders
# the document via Quarto. The figures are generated during render.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"
OUTPUT_DIR="$DOCS_DIR/_output"

echo "=== Regenerating Physics Primer ==="
echo "Repo root: $REPO_ROOT"
echo "Docs dir:  $DOCS_DIR"

cd "$DOCS_DIR"

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR/_figures"

# Check Quarto version
echo "Quarto version: $(quarto --version)"

# Render to HTML (with embedded figures)
echo "Rendering HTML..."
quarto render physics_primer.qmd --to html --output-dir "$OUTPUT_DIR/html"

# Render to PDF (requires TinyTeX; auto-installs on first run)
echo "Rendering PDF..."
quarto render physics_primer.qmd --to pdf --output-dir "$OUTPUT_DIR/pdf"

echo ""
echo "=== Done ==="
echo "HTML: $OUTPUT_DIR/html/physics_primer.html"
echo "PDF:  $OUTPUT_DIR/pdf/physics_primer.pdf"
echo ""
echo "Commit the updated docs/physics_primer.qmd and docs/_quarto.yml to git."
echo "The _output/ directory is gitignored."