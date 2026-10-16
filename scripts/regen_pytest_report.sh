#!/usr/bin/env bash
# Regenerate pytest_results.qmd by running the test suite and capturing output.
# The QMD uses Quarto's executable code blocks that run pytest and embed results.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"
OUTPUT_DIR="$DOCS_DIR/_output"

echo "=== Regenerating Pytest Results ==="
echo "Repo root: $REPO_ROOT"
echo "Docs dir:  $DOCS_DIR"

cd "$REPO_ROOT"

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR/html"

# Prefer the project venv (has pytest-html + pinned deps); fall back to PATH.
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PY="$REPO_ROOT/.venv/bin/python"
else
    PY="python3"
fi

# Check pytest and Quarto versions
echo "Pytest version: $($PY -m pytest --version)"
echo "Quarto version: $(quarto --version)"

# Generate self-contained HTML report for detailed viewing
echo "Generating pytest HTML report..."
$PY -m pytest tests/ \
    --html="$OUTPUT_DIR/html/test_report.html" \
    --self-contained-html \
    -q \
    --tb=short

# Render the QMD (which re-runs pytest for summary tables)
echo "Rendering QMD to HTML..."
cd "$DOCS_DIR"
quarto render pytest_results.qmd --to html --output-dir "$OUTPUT_DIR/html"

# Also render to PDF
echo "Rendering QMD to PDF..."
quarto render pytest_results.qmd --to pdf --output-dir "$OUTPUT_DIR/pdf"

echo ""
echo "=== Done ==="
echo "HTML summary:  $OUTPUT_DIR/html/pytest_results.html"
echo "HTML report:   $OUTPUT_DIR/html/test_report.html"
echo "PDF:           $OUTPUT_DIR/pdf/pytest_results.pdf"
echo ""
echo "Commit the updated docs/pytest_results.qmd and docs/_quarto.yml to git."
echo "The _output/ directory is gitignored."