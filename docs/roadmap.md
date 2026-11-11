# Roadmap

Open work only. Finished work lives in git history, not here.

## Placement (where parts go)

- **Label clearance calibration**: ref-vs-ref / ref-vs-own silk overlaps are zero; residual ~10 hits where a label sits on a neighbor's pad copper or hangs past Edge.Cuts (courtyard-only body model). Next: include pad-copper extents in label bodies, then pick a default for `label_clearance_m` (tube111 subset first). Sweep: `scripts/sweep_label_clearance.py`.
- **DCCF regrowth at long budgets**: current wall defaults (`wall_k_N` 0.1, halo 4 mm) hold 1000-iter runs clean. Gate any future wall change on the dense 1000-iter pair set; fix the sweep script's weakened repulsion so sweeps compare fairly.
- **Halo reach**: Legalize uses 4 mm, Cluster/default 6 mm. One full sweep before changing either.
- **Default-parameter tests**: move physics/courtyard tests off pinned settings onto current defaults. Test behavior, not numbers.
- **Selected-part highlight**: hovering a table row should highlight the part on the board.

## Routing (where copper goes)

- **Finer smoothing**: fillets, broad sweeps, kink guard, `smooth_effort` slider, and Polish are in. Still open: search along curves directly, push copper aside.
- **Rip-and-relay ordering**: refine retries failed nets first; casualty-repair relay re-routes nets the winner broke. Still open: rank nets by difficulty up front.
- **Push-and-shove**: scoped alternative landed (`routing/shove.py`: GND eviction + gap healing + Tier-1 signal shove + Tier-2 gap reconnect, all with adopt-or-rollback gates). Still open: edge-hugging detours (router edge keepout is a soft toll; emit gate doesn't check edge clearance) and a true mid-run violation cost model.
- **Dense-board mazes**: manufactured gate trips fixed; tube rails route (+12V/−12V/GND in ~13 s). Leftover sealed clusters need a thread-preserving finder, not smaller chunks. Revisit only if it reappears.
- **Corridor guidance**: keep as-is (post-first-failure only, saves the VIN tight spot).
- **Heuristic weight**: try 1.0 vs default 3.0 on tube111 + DCCF; change only with zero regressions and equal costs.
- **Routing time estimator**: replace the `2 + 1.2·n` guess with per-net pad-count + failed-net allowance.
- **Tidy-regime calibration**: gate Tidy defaults on a sweep (outliers down, no overlap/wire-length regression vs legalize), publish JSON under `docs/benchmarks/`.
- **Layer assignment**: prefer F.Cu signals, B.Cu returns. Not started.
- **Track-tidy, parked**: Polish covers re-smoothing; collinear merges, spike/stub stripping, via-pair tees stay parked.

## Pour copper and stitching

- **Cross-net bridges**: user-declared only; mounting holes stay unbridged.
- **Faster pour cuts**: stop allocating a full-board mask per track when cutting zones.
- **Width-aware rebuilds**: rebuilt grids mark old tracks centerline-only while fresh routes reserve full width — make rebuilds use widths so chunked and batched agree.

## Four-layer boards (viewer + plane-tap fanout landed)

Two-layer frozen. Four-layer routes outer-only, keeps inners, taps planes for zoned ground/power nets.

- **Phase B nets table**: per-net clearance + width override editor.
- **Phase C per-net layers**: `net_allowed_layers`, enforced in search, soft-fail per net.
- **Phase D ground fanout**: dedicated `routing/fanout.py`, generalized beyond 2 layers. Keep out of presets until validated.

## Code health

- **Split large modules**: `routing/pipeline.py`, `placement/runner.py`, `ui/server.py`. Carve along seams, suite green per split.
- **THT facing opposite-side SMD**: re-check courtyard overlap math (pins occupy both sides). Needs a small fixture pair + legality case.
- **Partial re-route of huge nets**: chunking landed. Still open: chunk by footprint set/region (not row bands), re-route only the failed part.
- **Chunked-equivalence test**: `tests/test_routing_piecemeal.py:203` is slow (~28 s) for a loose check. Sharpen or replace with a small deterministic probe.

## Docs

None open — see `docs/tests.md` for the test map.
