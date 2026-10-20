# Legality-Detected Legalize — Change Spec

Date: 2026-10-20
Status: PROPOSAL (not implemented)
Motivation: 2B Q1 Legalize run ended on top of C6 and deep in the ghost wall,
yet reported legal-looking forces. Overlaps are systematic: the engine treats
courtyard overlap as a soft penalty force, and the convergence gate
(`_run_region_simulation`, `runner.py:649-653`) only checks quiet step +
settled net force — never geometry. A force-balanced wedge can therefore
report `converged` from a physically impossible assembly position.

Prior related fixes already on HEAD (do not re-do):
- Final `forces`/`force_components` are recomputed at final `positions`
  (`runner.py:689-696`), not reused from F0 — so post-run arrows match where
  parts actually ended up.
- `PlacementParams.from_dict` drops `dt_min_s`/`dt_max_s` (`params.py:125-131);
  solver-internal timestep guards are never user-controllable, so lowering
  `dt_s` to the UI min (0.2 ms) cannot fail validation against stale
  `placement.json`.

## 1. Goals / non-goals

Goals:
- Legalize must never report `converged` while a part is in an illegal
  assembly position (courtyard overlap or board-wall penetration beyond
  tolerance).
- Demarcate offending parts in the UI part-list table (not SVG red dots).
- Keep the change small, deterministic, and test-preserving where possible.

Explicit non-goals (per owner):
- No multi-round placement, no ensemble/varied-param retries. Same-params
  re-runs are just more iterations and do not fix force-balanced wedges.
- No "loaded contact" exemption. Overlap against a locked part is illegal,
  full stop. Owner's locked parts are physical UI elements (DCCF faders must
  stay put; jacks may overlap the board edge only, never other parts).
- No big SVG red-dot overlay. Mark the part rows in the part-list table.

## 2. Legality definition

Checked at final committed positions (after `deltas` are applied), in board mm.

Pairwise courtyard overlap (area, mm²):
- movable ↔ movable, overlap > `overlap_tolerance_mm2` → ILLEGAL.
- movable ↔ ghost, overlap > `overlap_tolerance_mm2` → ILLEGAL.
  Ghosts are parts for this purpose: they push parts around, so intruding
  into them counts.
- movable ↔ locked, overlap > `overlap_tolerance_mm2` → ILLEGAL.
  No loaded-contact exemption.
- ghost ↔ locked, overlap any → EXEMPT by design. Owner places ghost
  walls over locked parts deliberately; never flag these pairs. Never
  include them in `overlapping_pairs`.

Board-wall penetration (depth, mm):
- Any movable footprint whose courtyard is not covered by its region
  polygon with protrusion depth > `wall_tolerance_mm` → ILLEGAL.
  Uses the same primitives as the force path: `region_poly.covers(body_poly)`,
  `body_poly.disjoint(region_poly)`, vertex protrusion depth.
- A jack overlapping only the board edge is the boundary case: touching
  (depth ~ 0) is legal; protruding beyond `wall_tolerance_mm` is illegal.
- Ghost walls are not checked against the board wall (they are anchors).

Pin-level / THT-pin sub-contacts do not define legality: only courtyard
polygons (with the existing pad-bbox fallback) and the region wall.

## 3. New module: `src/kicad_autorouter/placement/legality.py`

New, pure, deterministic, no prints, no function-attribute state.

```python
@dataclass(frozen=True)
class LegalityReport:
    is_legal: bool
    max_pair_overlap_mm2: float
    overlapping_pairs: tuple[tuple[str, str, float], ...]  # (uuid_a, uuid_b, area_mm2), sorted
    max_wall_penetration_mm: float
    wall_violations: tuple[tuple[str, float], ...]          # (uuid, depth_mm), sorted
    exempt_pairs: tuple[tuple[str, str], ...]               # ghost-locked overlaps seen but exempt

def check_legality(
    model: BoardModel,
    positions_mm: dict[str, tuple[float, float, float]],  # uuid -> (x, y, angle) FINAL pose
    *,
    overlap_tolerance_mm2: float,
    wall_tolerance_mm: float,
) -> LegalityReport: ...
```

Implementation notes:
- Build posed courtyard polygons with `_footprint_courtyard_polygon`
  (`contact.py:426`) — the same builder the force path uses, including the
  pad-bbox fallback. Empty polygons contribute nothing (never illegal).
- Pairwise intersection via shapely `intersection().area`. Broadphase: reuse
  the region grouping — only compare footprints in the same
  `footprint_region`; skip pairs where both are locked (neither can move);
  skip ghost↔locked pairs entirely (exempt by design); still record
  ghost↔locked overlaps seen into `exempt_pairs` for auditability? Or skip
  silently — decide in implementation (prefer record-but-exempt).
- Deterministic ordering: sort pairs by `(uuid_a, uuid_b)` with
  `uuid_a < uuid_b`; sort wall violations by uuid. Round areas to 6 decimals.
- Wall check per movable footprint: resolve region via
  `model.footprint_region.get(uuid)` → `model.board_regions[idx]` polygon
  (shapely). If region missing/empty, skip (cannot judge). If covered →
  legal. If disjoint → outside board → violation with depth = distance to
  region (or a large sentinel?). If straddling → violation with depth = max
  protruding-vertex distance. Only flag when depth > `wall_tolerance_mm`.
- Performance: pairwise shapely intersections are O(n²) worst case; regions
  keep n small, and this runs once per region-run end (not per iteration).
  If profiling shows cost, add the same 15 mm spatial-grid broadphase the
  force path uses.

Inputs: final poses, NOT pad arrays. The caller (`runner.py`) derives final
`(x, y, angle)` per footprint from `positions` via
`_compute_footprint_pose_from_pads` (already done when building `deltas`),
or `check_legality` accepts the `deltas` dict plus committed `model` and
poses internally as `x + dx`. Prefer the latter (fewer new code paths):
signature `check_legality(model, deltas, *, overlap_tolerance_mm2,
wall_tolerance_mm)`.

## 4. Params: `src/kicad_autorouter/placement/params.py`

Add two user-controllable fields (unlike `dt_min_s`/`dt_max_s`):

```python
# Courtyard-overlap area (mm²) above which a pair is illegal.
overlap_tolerance_mm2: float = 0.1
# Board-wall protrusion depth (mm) above which a part is illegal.
wall_tolerance_mm: float = 0.1
```

- Include in `_validate` numeric tuple; require `>= 0` (they already fall
  under the generic `>= 0` rule if added to `numeric`; add explicitly).
- `from_dict` keeps them user-controllable (do NOT add to the dt purge list).
- `to_dict` round-trips them; UI may add sliders later (not in this change).

## 5. Runner: `src/kicad_autorouter/placement/runner.py`

### 5a. Post-hoc legality downgrade in `run_placement` (not in-loop)

Do NOT touch the settle/stall loop in `_run_region_simulation`. Leave the
per-iteration convergence gate (`runner.py:649-653`) exactly as is.

Instead, after `run_placement` merges per-region results into the final
`PlacementProposal`, compute legality ONCE globally from the final
`deltas` + committed `model`:

```python
legality = check_legality(model, all_deltas,
                          overlap_tolerance_mm2=params.overlap_tolerance_mm2,
                          wall_tolerance_mm=params.wall_tolerance_mm)
if all_stop == "converged" and not legality.is_legal:
    all_stop = "stalled"  # forces balanced but position is illegal
```

Rationale (owner decision 2026-10-20):
- One geometry pass per run, not per region, not per iteration — no
  cross-region pair blind spots, no mid-loop pose-derivation plumbing.
- "stalled" is kept as the reason (no new stop reason). Consequence: a
  settled-but-illegal run now reports "stalled" WITH small residual
  (< settle scale). This deliberately relaxes the old pinned contract
  ("stalled ⇒ residual remains"); the honest-stop test
  (`test_dccf_legalize_stop_is_honest`) must be updated to allow
  small-residual stalls when `legality_is_legal` is False (see §9).
- The settled-but-illegal branch needs no extra iterations: forces already
  balanced; more same-param steps will not escape (multi-round explicitly
  rejected).

### 5b. `PlacementProposal` fields

`PlacementProposal` (`runner.py:47-95`, frozen dataclass) gains:

```python
legality_is_legal: bool = True
overlapping_pairs: tuple[tuple[str, str, float], ...] = ()
wall_violations: tuple[tuple[str, float], ...] = ()
max_pair_overlap_mm2: float = 0.0
max_wall_penetration_mm: float = 0.0
```

- Flat tuples (not a nested dataclass) so `proposal_to_json`-style
  serialization and `BoardState` caching stay trivial.
- Computed once per region at run end from final positions; merged across
  regions in `run_placement` (union pairs, max depths, `is_legal = AND`).
- Single-region and virtual-region paths both populate these fields.

### 5c. `run_placement` merge + downgrade

- Union `overlapping_pairs`/`wall_violations` across regions; `is_legal =
  all(regions legal)`; max depths = max across regions. (Simpler: compute
  legality once globally from merged `all_deltas` + `model` after the
  region loop, instead of per-region + merge. Prefer the single global
  pass — it also catches cross-region pairs.)
- If merged `stop_reason == "converged"` but global legality is illegal,
  downgrade to `"stalled"`.
- No re-runs. Return after the single simulation pass as today.

## 6. Server: `ui/server.py`

- `/api/placement/run` response payload (`~line 1731`): add
  `stop_reason`, `residual_max_force_N`, `legality_is_legal`,
  `overlapping_pairs`, `wall_violations`, `max_pair_overlap_mm2`,
  `max_wall_penetration_mm` (all from the proposal; guard with `getattr`
  for backward compat).
- `proposal_to_json` (`~line 1100`): include the same legality fields so
  any consumer of the serialized proposal sees them.
- No new endpoints. No DRC subprocess involvement. No auto-retry.

## 7. UI: `ui/index.html`

- Part-list table: mark rows whose uuid appears in `overlapping_pairs` or
  `wall_violations` (e.g. CSS class `illegal` + ⚠ glyph + tooltip with
  peer uuid / depth). Owner implements table markup; engine only supplies
  the uuid lists.
- No SVG red-dot overlay in this change (explicitly deferred by owner).
- Optionally surface `stop_reason` + legality in the run diagnostics line
  (e.g. "stalled — Q1 overlaps C6 (0.42 mm²), Q1 in ghost wall GW2 (0.31 mm)").

## 8. Force interplay (unchanged)

- Per-iteration forces in the integration loop are untouched.
- Post-run `forces`/`force_components` remain recomputed at final positions
  (current HEAD behavior).
- Motion `force_frames` remain per-frame recomputed (continuous during
  scrub).
- Legality is a geometric post-check, not a force term. No new force law,
  no blending change, no `last_contact` change.

## 9. Tests

Update-or-add (synthetic models only; never the untracked `2B.kicad_pcb`).
NOTE (owner decision 2026-10-20): several existing stop-reason assertions
will change as a direct consequence of this spec and must be rebuilt
cleanly — a run that previously reported "converged" from an illegal
position will now report "stalled" (possibly with small residual). The
following list is the expected blast radius; treat each as update-to-suit,
not as a regression to avoid.

1. `test_illegal_overlap_blocks_converged` (new): two movable squares
   overlapping; Legalize-ish params; assert final `stop_reason == "stalled"`
   (not `"converged"`) and `overlapping_pairs` non-empty with the right uuids.
2. `test_ghost_overlap_is_illegal` (new): movable square overlapping a
   ghost wall; assert illegal + "stalled".
3. `test_ghost_over_locked_is_exempt` (new): ghost overlapping a locked
   part only; assert `is_legal` True and pair in `exempt_pairs` (or absent).
4. `test_wall_penetration_blocks_converged` (new): movable square
   straddling the region edge; assert `wall_violations` non-empty +
   "stalled".
5. `test_loaded_contact_now_stalls` (update existing
   `test_loaded_contact_settles_despite_per_pad_loads`): movable pressed
   against locked neighbor with small overlap now reports "stalled" (was
   "converged"). Overlap assertion (`< 0.5`) stays; stop-reason assertion
   changes. This is an intentional semantic change — document in the test.
6. `test_dccf_legalize_stop_is_honest` (update): DCCF has known overlaps
   (`KNOWN_DCCF_OVERLAPS_200`); if final positions are illegal it will now
   report "stalled" instead of "converged" — AND the residual may be small
   (< settle), because the settled-but-illegal downgrade fires with
   balanced forces. Relax the `otherwise residual > 1e-3` assertion to
   allow small residual when `legality_is_legal` is False. The allowlist
   semantics are unaffected (allowlist is about geometry audit, legality
   is about the stop gate).
7. Any other test asserting `stop_reason == "converged"` whose final
   position is illegal will now report "stalled" — update each to suit.
8. Full suite: `.venv/bin/python -m pytest tests/ -q`.

## 10. Rollout (no commits unless asked)

1. `placement/legality.py` (new) + export in `placement/__init__.py`.
2. `params.py`: add the two tolerance fields + validation.
3. `runner.py`: `PlacementProposal` legality fields + post-hoc global
   downgrade in `run_placement` (settle/stall loop untouched).
4. `ui/server.py`: payload + `proposal_to_json` fields.
5. `ui/index.html`: part-table marking (owner may do this part).
6. Tests: new legality tests (§9.1–4) + update loaded-contact + DCCF
   honest-stop tests (§9.5–6) + sweep any other `stop_reason ==
   "converged"` assertions (§9.7).
7. Run `.venv/bin/python -m pytest tests/ -q` and rebuild changed
   expectations cleanly per §9 note.

## 11. Open questions for owner (resolved 2026-10-20 unless noted)

1. Movable-over-locked overlap → ILLEGAL (no exemption). CONFIRMED.
2. Ghost-over-locked exemption: record in `exempt_pairs` for audit
   (spec default). OPEN — confirm or switch to skip-silently.
3. Stop reason for settled-but-illegal: "stalled". CONFIRMED — with the
   explicit consequence that stalled-with-small-residual is now possible
   when illegal; honest-stop test updated accordingly (§9.6).
4. Tolerances: `overlap_tolerance_mm2 = 0.1`,
   `wall_tolerance_mm = 0.1`. CONFIRMED.
