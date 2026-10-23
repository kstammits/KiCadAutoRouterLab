#!/usr/bin/env bash
# Clean local scratch + regenerable outputs (all gitignored, server recreates).
# Usage: scripts/clean.sh [--all]
#   default : dated test/plan boards, DRC reports, demo outputs, python/pytest
#             caches, and Quarto's docs/_output + docs/.quarto caches.
#   --all   : additionally wipes tmp/drc_current.* (server rewrites on next DRC
#             run) and .mypy_cache.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

rm -f tmp/test_*.kicad_pcb tmp/test_*.kicad_prl \
      tmp/plan_*.kicad_pcb tmp/plan_*.kicad_prl \
      tmp/drc_report_*.json \
      tmp/drc_sch_*.kicad_sch \
      tmp/magnet_* tmp/tube111_testrender.svg \
      tmp/dccf_regress_200iter.* tmp/repro_dccf_regress.py

find src tests ui scripts -type d -name __pycache__ -prune -exec rm -rf {} +
find . -name .DS_Store -not -path './.git/*' -not -path './.venv/*' -delete
rm -rf .pytest_cache docs/_output docs/_freeze docs/.quarto

if [ "${1:-}" = "--all" ]; then
    rm -f tmp/drc_current.kicad_pcb tmp/drc_current.kicad_prl \
          tmp/drc_report_current.json
    rm -rf .mypy_cache
fi

echo "Clean. $(du -sh tmp 2>/dev/null | cut -f1) left in tmp/."
