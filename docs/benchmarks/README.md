# Benchmarks (local only)

`*.json` results in this directory are **not tracked** — they are
machine-specific wall-clock numbers. Regenerate with:

```bash
.venv/bin/python scripts/benchmark_placement.py
.venv/bin/python scripts/benchmark_routing.py
# … see scripts/benchmark_*.py (one per family)
```

Only the conclusions travel with the repo: the performance table in the
root `README.md` and the fitted constants cited in `ui/app-routing.js` /
`ui/app-placement.js` comments.
