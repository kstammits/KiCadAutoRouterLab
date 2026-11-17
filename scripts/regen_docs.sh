#!/usr/bin/env bash
# Regenerate the user manual (the only remaining generated doc).
# Usage: scripts/regen_docs.sh [-- args for gen_manual.py ...]
#
# Runs scripts/gen_manual.py, which rebuilds ui/manual.html from the
# current engine (placement only; no screenshots, no Quarto).
# Extra arguments are passed through, e.g.:
#   scripts/regen_docs.sh --check          # assert examples without writing
#   scripts/regen_docs.sh --only collision  # rebuild one example section

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

.venv/bin/python scripts/gen_manual.py "$@"

echo "Manual: $REPO_ROOT/ui/manual.html"
