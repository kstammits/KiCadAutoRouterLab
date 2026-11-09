# Proposal: fix the smoother / emit-gate contract mismatch (J1 false refusals)

Status: partially landed. The gate-side margin classifier
(`verify.gate_touch_margin`: TOUCH vs NON_TOUCH over foreign pads+vias)
landed with a **fallback-to-raw** repair instead of the proposed
block-and-resmooth loop (`route_raw._resolve_gate_trip`: NON_TOUCH trips
stamp the legal search path; TOUCH stays fatal). Effort control
(`RoutingParams.smooth_effort` 0..3, default 2.0 = legacy behavior) and
the bounded re-attempt ladder landed in `smooth.resolve_config` /
`_route_chunk`; fine re-smoothing lives in the separate Polish action
(`routing/polish.py`, server `/api/routing/polish`). Evidence from
scratch experiments in `$SCRATCH` (see below) still applies.

## 1. Problem statement

On the canonical tube111 chain (`tmp/tube115_rails` → signals), nets
`Net-(J1-Pad3)` and `Net-(J1-Pad4)` each burn ~1M A* expansions finding a
path, then fail **loudly** at the emit gate with 9 identical 1-cell
violations near (91.9–93.0, 68.4–69.5) on B.Cu (`/tmp/signals.log`).
Both nets are left unconnected (each fractures to 2 pad-components).

Experiment verdict: **all 9 trips are false refusals — zero copper is
touched.** The gate is stricter than its own physical model by up to
~0.07 mm, and the smoother manufactures the violating geometry out of a
legal search path. Two nets die per board for nothing.

## 2. Measured evidence

Setup: replayed the exact pipeline signal order (26 nets) up to J1-Pad3 via
`place_strip.run_route_stage` (in-memory, no file writes), with recorders
monkeypatched onto `route_raw._smooth_result` / `fitted_pieces_violate`.
The replay reproduces the pipeline failures byte-identically (C1Output 32
cells, C2Input 20 cells, J1-Pad3 9×1 cell), so the captured gate inputs are
the true pipeline inputs.

Classifier (per gate-flagged cell): distance from the emitted 0.25 mm track
(`LineString.buffer(0.125, flat caps)`) to the nearest *foreign copper
polygon* (`placement.contact.pad_copper_polygon`, exact shape, posed):

| gate call (net) | problems | TOUCH (track∩copper) | GUARD_GRAZE (0<d≤0.071) | CLEAR (d>0.196) |
|---|---|---|---|---|
| C1Output (32-cell chord) | 2 | **32** | 0 | 0 |
| C2Input (20-cell chord) | 2 | **19** | 0 | 1 |
| J1-Pad3 (9 micro-chords) | 9 | **0** | 3 | 4 |
| other 21 calls | 0 | 0 | 0 | 0 |

(`GUARD_GRAZE` = track edge inside the 0.0707 mm raster-guard ring only;
`CLEAR` = copper more than track_half+guard (0.196 mm) from the centerline —
the gate still fires because a cell *corner* can sit 0.196+0.071 mm out.)

EXP2 (search vs smoother blame), same masks, exact segment-vs-cell check:

| capture | raw A* segments | pre-smooth hits | post-smooth gate problems |
|---|---|---|---|
| J1-Pad3 standalone | 1255 | **0** | 2 |
| J1-Pad3 pipeline ctx | 707 | **0** | 9 |

The search path is legal in both contexts; **100% of the violations are
created downstream** (pull-string / fitter). Supporting case: the
standalone J1-Pad3 trip (different context, 17 cells) classifies as
TOUCH=13 incl. sibling-pin (J1-Pad4) copper — i.e. when the gate fires on a
real short it is a true positive; the mechanism discriminates correctly.

## 3. Root cause (two coupled defects)

**R1 — gate checks a zero-width centerline against finite cells**
(`routing/verify.py:109-129`). The physical model behind the mask is
"copper + track_half + guard" (`routing/obstacles.py:234-238`), but the
check is `centerline ∩ cell`, with **no endpoint exemption and no width**.
A centerline clipping a cell corner whose copper is >0.196 mm away still
trips it. Over-strictness bound: ~half-diagonal ≈ 0.07 mm at 0.1 mm grid.

**R2 — the smoother approves chords the gate rejects**
(`routing/smooth.py:246-308`). `_line_walkable_nb` samples at half-cell
steps with a **<0.5-cell endpoint exemption for the blocked layer**
(lines 277-280) and **<1.0-cell for the cost grid** (lines 289-293); the
footprint is a sampled disc, not an exact segment test. Consequences:

- 1-cell micro-chords (the entire J1 trip set) are dominated by the exempt
  endpoints — the middle sample sits at *exactly* 0.5 (`< 0.5` is strict),
  and the disc-vs-exact test disagrees at cell corners. Sampling gaps, the
  exact class the gate docstring says it closes.
- Unshortened A* steps are never re-checked at all; only *shortcuts* pay
  the walkability gate. A staircase the greedy pass keeps step-by-step
  carries whatever the grid sampling missed straight to emission.

Net effect: search-legal → smoother-mangled → gate-refused, with no retry
(`"gate"` is fatal, `route_raw.py:1316`, `attempts==1` pinned by test).
Correct outcome, wrong input: the refusal is honest only when copper is
touched (C1/C2), not for raster dust (J1).

## 4. Blast radius (why this matters beyond J1)

- 2 of 60 signal nets (3%) left unconnected per board for no physics reason;
  identical geometry kills both pins (shared corridor), so paired headers
  are systematically at risk wherever a route threads between SMD pads.
- The failure is *silent-ish*: loud in the log, but the DRC-vs-input triage
  shows no NEW violation for a refused net — the missing copper only shows
  up as `unconnected` + `FRACTURED`, easy to misread as "hard board".
- Precedent risk: any tolerance added naively (e.g. endpoint allowances in
  the gate) re-opens the tube115-J2 hole the guard was built to close
  (~0.05 mm graze through a boundary cell). The fix must keep C1/C2-class
  trips fatal — the classifier above proves that partition exists.

## 5. Proposed change

**Recommended: classify-then-repair (gate side + one bounded repair loop).**

1. **Gate reports margin, not just binary** (`verify.py` only): for each
   flagged cell, compute track-edge-to-copper distance (same STRtree
   machinery, copper polygons indexed per net — the experiment's `_ptree`
   pattern). Message gains the minimum margin, e.g.
   `crosses hard exclusion (1 cells, min track-to-copper 0.09mm)`.
   Cost: only on the already-flagged cells (≤ dozens), not per chunk —
   the hot path (cold 0.02 s / cached ~0 s) is untouched.
2. **Split the verdict**: `TOUCH` (margin ≤ 0) → fatal, exactly as today.
   `NON_TOUCH` (graze/clear only) → **one** repair attempt, then accept-or-
   escalate (see 3).
3. **Bounded repair** (`route_raw._route_chunk`): block the flagged cells
   in a copy of the region mask (they are ≤ ~10 cells; no geometry change
   elsewhere) and re-run smooth+fit **once**. If the re-gate is clean,
   stamp; if it still trips (now necessarily on different cells, or
   TOUCH), fail loudly as today. Cap: exactly 1 extra smooth+gate pass per
   chunk — worst case ~2× smooth cost on the few affected chunks, zero on
   the 21/24 clean ones.
4. **Tests** (new, synthetic-first per repo convention):
   - `test_routing_emit_check.py`: margin reporting — zero-width clip with
     copper at 0.15 mm reports non-touch; centerline-on-copper reports
     touch; parallel-run-outside stays clean.
   - Smoother contract test: the recorded J1 branch (707-seg input +
     mask, pickled as a fixture or inlined minimal) must smooth to zero
     gate problems after repair, or fail with margin>0 evidence.
   - End-to-end: J1-Pad3/Pad4 route and connect with 0 DRC violations on
     the 115 input; C1Output/C2Input still refused (true-positive guard).
5. **Rollout**: gate margin reporting is additive (message text only —
   update the two message assertions). Verdict split + repair loop behind
   the existing params plumbing (new `RoutingParams` flag, default ON once
   green; the fatal path is unchanged when OFF). No fixture changes.

**Considered and rejected:**

- *Endpoint allowance in the gate* (mirror the smoother): re-opens the J2
  hole by construction; the allowance is what created R2.
- *Eroding the guard ring*: same hole, plus it weakens the C1/C2-class
  protection the exclusion-guard tests pin.
- *Smoother-side exact checking of every candidate chord*: correct in the
  limit but pays exact-geometry cost inside the O(n²) greedy/sweep loops
  (the loops that already dominate smooth time); the repair loop confines
  exact geometry to ≤10 cells on ≤4/60 nets.
- *Do nothing*: cements 2 unconnected nets per board and a triage signal
  (`FRACTURED`+`unconnected`) that masks a fixable engine defect.

## 6. Acceptance criteria

1. J1-Pad3 + J1-Pad4 route, connect (1 pad-component each), 0 DRC
   violations attributable to their copper on the 115 input.
2. C1Output / C2Input still fail loudly with TOUCH evidence (no tolerance
   leakage onto real shorts).
3. Full fast suite green except the known pre-existing crossing failure;
   `attempts==1` semantics preserved for TOUCH trips.
4. No per-net regression: signals-stage wall time within noise of the
   current ~220 s (repair loop triggers on ≤4/60 nets, 1 extra pass each).
5. Gate messages carry margins; triage can distinguish touch vs graze
   without re-running analysis scripts.

## 7. Open items / risks

- **O1**: `test_diagonal_nets_respect_keepout` fails on clean HEAD (pre-
  existing, unrelated). Exact failing assertion not yet captured (deliberately
  not run: plan-mode constraint during analysis). P0 step of implementation:
  run it once, record the assertion — if it is the same smoother/gate class
  at 0.5 mm cells, this proposal may fix it as a side effect; if independent,
  it stays out of scope.
- **O2**: D3-Pad2 routed in the prefix replay but failed ("no path") in the
  monolithic pipeline run — order/context sensitivity in `route_nets` worth
  one investigation line, not blocking this proposal.
- **O3 (risk)**: repair-loop non-termination — mitigated by construction
  (exactly 1 re-smooth; second trip always fatal). **Risk**: copper-polygon
  index build cost per net — mitigated by building it lazily, only when the
  binary gate has already fired (≤4 nets/run in evidence).
- **O4 (risk)**: margin semantics vs manufacturing — GRAZE (track inside
  guard ring, ≤0.071 mm edge-to-copper) is DRC-clean today but eats the
  entire raster margin; the proposal *repairs* rather than *accepts* these,
  so no new precedent is set. If repair fails, the net stays refused.
- **O5**: J1-Pad4 assumed same-class by byte-identical message; implementation
  verifies it through the same capture path before closing.

## 8. Appendix: exact references

- Gate: `src/kicad_autorouter/routing/verify.py:77-160`
  (check_seg 109-129, arc flatten 147-160, tree cache 27-63).
- Smoother sampling + exemptions:
  `src/kicad_autorouter/routing/smooth.py:246-308`
  (0.5-cell blocked exemption 277-280, 1.0-cell cost exemption 289-293,
  disc footprint 141-161).
- Hard-core stamp + guard: `src/kicad_autorouter/routing/obstacles.py:213-238`
  (`track_half_mm + res·√2/2`, `THT_EXCLUSION=10000` line 44).
- Wiring + fatal kinds: `src/kicad_autorouter/routing/route_raw.py:507-600`
  (`_route_chunk`), `:1256-1338` (retry loop, `"budget"|"gate"` fatal 1316).
- Evidence: `/tmp/signals.log` (pipeline trips), `$SCRATCH/expj1_ALL.pkl`
  (24 recorded gate calls), `$SCRATCH/exp_j1_an3.log` (classification table).
- U1 (exclusion owner at trip site): SOIC-8 on B.Cu at (94.69, 67.15),
  `tests/fixtures/tube111.kicad_pcb:21136`; U1 pad copper starts x=93.28
  (measured), trip cells end x≈93.0 — consistent with guard-ring-only /
  corner-clip geometry.
- J1 header: `tests/fixtures/tube111.kicad_pcb:7266+`, 1.7 mm oval THT pads
  on 2.54 mm pitch (sibling-pin proximity is the standalone-trip mechanism).
