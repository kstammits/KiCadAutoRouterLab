# SI Force Laws for Placement Dynamics

Normative reference for the placement solver. Units are SI: **meters** for
geometry, **newtons** for force, **kilograms** for mass, **seconds** for
time. Board files, SVG output, and motion frames stay in millimeters at the
boundaries; everything here is solver-internal.

Goals, in order: **safe** (no blowups, no tunneling, collisions never
sticky), **clean** (one knob per effect, consistent units), **clear** (every
formula inspectable here).

Notation: pad position `x` (m), velocity `v` (m/s), footprint mass `m` (kg,
from `data/footprint_mass.json`), timestep `dt` (s), unit vector `û`.

## 1. Force laws (all in newtons)

### Pad repulsion (all-pairs, Coulomb-like)
```
F_rep(i←j) = k_rep · û(i−j) / d²        d in meters, k_rep in N·m²
```
Isolated-pair equilibrium with attraction sits at
`d = √(k_rep / k_attr)`. With the defaults this is sub-millimeter: the
courtyard wall owns contact response, not repulsion.

### Net attraction (constant-magnitude springs)
```
F_attr = k_attr · w_edge · û(src→dst)   k_attr in N, w_edge dimensionless
```
No Hooke length term. `w_edge` carries the fanout/decap/power weighting.

Decap-incident edges are **one-sided**: the decap receives the attraction,
the non-decap endpoint does not (`edge_sides` in `force.py`). This keeps a
large part from being dragged onto its decoupling cap when a rail pulls the
cap toward the part; decap–decap edges stay symmetric.

### Courtyard collision (walls)
One law in signed separation `s` (meters) along the pair normal
(`s > 0` gap, `s = 0` touching, `s = −penetration depth`):
```
F_wall = k_wall · (1 − s / halo)^p · û      k_wall in N, halo in m
```
Zero past the halo reach, `k_wall` at contact, growing with penetration.
`p = wall_exponent` (default 2). `p ≥ 1` keeps the law C0/C1-continuous at
first touch; `p < 1` is rejected because its slope is singular at the halo
edge. `wall_cap_depth_mm > 0` clamps evaluated penetration depth so deep
force saturates instead of running away.

Depth comes from SAT (rectangles/circles) or vertex-projection interval
overlap (generic polygons); no intersection areas are computed in the
contact path. Direction is the pair's SAT minimum-translation (MTV) axis —
out the nearest edge — in all cases.

### Board wall
The board region polygon is an inverted courtyard using the same
`_wall_mag` law: it pushes parts inward with `wall_k_N` / `wall_halo_m`.
There is no separate boundary stiffness parameter.

### Ghost attraction
```
F_ghost = k_ghost · û(target→anchor)              k_ghost in N
```
Split equally over target pads; no reaction on the ghost (infinite mass).

### Tangential assist (sliding assist)
Applied at courtyard contact pairs only (same-layer body/pin overlap), never
for halo-only approach. The board wall counts as a ghost courtyard with zero
velocity on its side:

```
F_t = γ · |F_n| · sign(v_rel_t)   applied along MTV-perp
```

- `γ = contact_tangent_gamma` (default 0.15) — fraction of the contact normal
  force applied perpendicular to the MTV axis; `0` disables.
- `v_rel_t` = relative tangential velocity between the two footprints
  (pad-average along the MTV-perp axis).
- `sign` is latched per contact pair: once decisive motion sets the
  direction, near-zero noise keeps it so a part pressed at an angle settles
  to one slide direction instead of rubbing left/right each step.
- Applied at the contact point via contact-weighted pad spread, so
  differential pad loading becomes torque through Kabsch — parts pressed
  against long walls slide along them rather than jam.

Sign latch behavior: `sign(v_rel_t)` updates only when `|v_rel_t| > 1e-9`
m/s; below that the last sign is held, defaulting to +1 on first contact.
This is internal solver memory (`ctx.last_tangent`), not a user param.

### Final depenetration pass (legalize eject)
Applied **once** after dynamics settle, before the final force readback:

```
x_i ← x_i + 0.5 · depth_i · û_near     per overlapping pair
```

- Direction comes from the pair's nearest points, not a SAT MTV; depth is
  measured along that axis with the vertex-projection interval overlap.
- Runs after dynamics stop (converged/stalled/budget), on the committed pad
  positions.
- Only true penetration (`s < 0`), never halo proximity — halo-only
  near-misses are untouched.
- Locked and ghost sides never move; both-movable pairs split the correction
  evenly.
- Jacobi (frozen-geometry) accumulation: all pairs' corrections are computed
  against the same pose, then applied together. Sequential in-place pushes
  would let later pairs steer on stale geometry and walk wedged parts
  around.
- Rotation is clamped to 2°/pass via the existing rigid-step clamp,
  re-posed rigidly (Kabsch) so bodies stay exact; velocities are re-derived
  from the projected displacement.
- One round only: iterating re-measures after each shove and ping-pongs
  closed wedges between neighbors. The remainder is reported via legality
  (`overlapping_pairs`, `max_pair_overlap_mm2`) for the next legalize run.
- Skipped entirely for static probes (`max_iterations=0` — report, never
  move) and walls-off configs (`wall_k_N`/`wall_halo_m == 0`).
- Velocities are zeroed after the pass; residual force is re-read at the
  ejected pose so the stop reason stays honest.

### Viscous damping (the physics damper)
```
F_damp = −c · v                                   c in N·s/m
```
Per-pad application; rigid projection keeps bodies consistent.

### Dry friction (deadband, whole footprint)
```
if max pad force over footprint < friction_N → footprint frozen this step
```
Whole-footprint all-or-nothing freeze in force units. Stops micro-motion
without a displacement hack.

## 2. Integrator (semi-implicit Euler + guards)

Per movable pad, per iteration:
```
v += (F / m) · dt        # kick (a = F/m in m/s²)
v *= 1 / (1 + c·dt / m)  # implicit-Euler viscous decay (stable for any dt)
if ‖v‖·dt > vmax:  v *= vmax / (‖v‖·dt)   # speed ceiling
x += v · dt              # drift
```
then Kabsch rigid projection snaps pads onto exact bodies and velocity is
re-derived from the actual projected displacement
`v := (x_proj − x_old) / dt` so projection cannot inject phantom energy.

Rigid-step clamp (tunneling guard): after projection, each body's centroid
shift is clamped to `vmax·dt` and its rotation change to 5°/step, re-posed
rigidly from the pre-step pose. Velocity re-derivation runs after the clamp.

Adaptive-dt guard: track system kinetic energy; halve `dt` on a significant
rise, regrow ×1.1 when it falls, floored at `dt_min_s`, capped at
`dt_max_s`. The absolute floor for a "significant" rise is a fraction of
`0.5·m_max·vmax²`, so contact noise does not ratchet the timestep down.

Contact direction rule: push along the SAT minimum-translation (MTV) axis —
out the nearest edge — never centroid-to-centroid, which is wrong for
containment / small-in-large overlaps. Generic (non-rect/circle) polygons
keep the centroid fallback.

Per-pair aggregation: body + THT-pin-circle sub-paths of one footprint pair
report signed separations and the single deepest contact wins
(winner-takes-all), so pin circles cannot stack the same contact. Multi-
neighbor superposition is unaffected (aggregation is per pair, not global).

Convergence: max `‖Δx‖ < convergence_eps_m` (meters) per step AND residual
forces below the settle scale
(`force_settle_N = max(20·friction, 4·ghost_k, 1e-3·max(wall, attraction))`).
`stop_reason` reports `converged` (quiet AND weak), `stalled` (no
window-mean progress with residual forces), `budget` (max_iterations with
residual force), or `empty`. Headless callers must treat `stalled` and
`budget` as needs-attention, never as settled.

Stall detection compares the mean pose over each `stall_window` against the
previous window mean. A limit cycle averages out; real transit moves the
mean. `stall_window=0` disables.

## 3. Parameters

| Param | Unit | Default | Role |
|---|---|---|---|
| `repulsion_k_N_m2` | N·m² | 2e-9 | pair push (sub-contact only) |
| `attraction_k_N` | N | 8e-3 | legalize-regime net pull |
| `wall_k_N` | N | 8e-2 | contact/halo strength |
| `wall_halo_m` | m | 0.006 | near-miss reach and depth scale (Legalize preset: 0.004) |
| `wall_exponent` | — | 2.0 | wall-law exponent (`≥ 1`) |
| `wall_cap_depth_mm` | mm | 0.0 | deep-penetration cap (0 = off) |
| `momentum_alpha` | — | 0.7 | old-velocity blend after rigid projection |
| `ghost_k_N` | N | 5e-5 | ghost spring scale |
| `contact_tangent_gamma` | — | 0.15 | tangential fraction (`0` = off) |
| `damping_Ns_m` | N·s/m | 0.12 | viscous damper |
| `friction_N` | N | 1e-6 | motion deadband |
| `dt_s` | s | 1e-3 | timestep |
| `dt_min_s` | s | 5e-5 | guard floor |
| `dt_max_s` | s | 2e-3 | guard ceiling |
| `vmax_m_s` | m/s | 1.0 | speed ceiling |
| `convergence_eps_m` | m | 2e-5 | settled threshold |
| `stall_window` | iterations | 100 | no-progress stop window (0 = off) |
| `max_iterations` | — | 1000 | step budget |
| `mass_density_kg_m2` | kg/m² | 3.0 | fallback mass slope |
| `mass_floor_kg` | kg | 1e-9 | minimum part mass |
| `power_attraction_scale` | — | 1.0 | power-rail attraction trim |
| `decap_boost` | — | 4.0 | decap-edge multiplier |
| `overlap_tolerance_mm2` | mm² | 0.1 | legality threshold |
| `wall_tolerance_mm` | mm | 0.1 | board-wall legality threshold |

Regime map (no single ratio both clusters scattered starts and legalizes
placed boards — use two stages):
- Cluster (scattered starts, e.g. magnet synthetics): `attraction_k_N`
  ≈ 0.04–0.06 with `wall_k_N` ≈ 0.02–0.03, `damping` 0.08, `dt` 2 ms.
  Tightens groups to ~0.3–0.6 of start spread, zero overlap.
- Legalize (placed boards: tube111, DCCF): `attraction_k_N` ≈ 0.005–0.01
  with `wall_k_N` ≈ 0.08–0.1, `damping` 0.12–0.15, `wall_halo_m` 4 mm,
  `dt` 1 ms. The shorter halo keeps dense neighbor fields from overriding a
  part's own wall; the engine default remains 6 mm, and Cluster uses it too.

Sanity anchor: an 0805 (2.5 mm² courtyard, ~8 mg) at `k_attr = 50 µN`
accelerates at ~6 m/s² — brisk but sub-ballistic at 1 ms steps
(`v·dt ≈ 6 µm/step`).

Known residual: DCCF keeps locked source overlaps (e.g. D5/Q2), and longer
legalize budgets can regrow attraction-dominated squeezes (e.g. C10/U2). The
equilibrium is stiffness-independent; resolving it needs anchor-side or
placement-level work, not a stiffer wall. Honest stopping means wedge depth
can grow with longer budgets on attraction-dominated traps, so depth is not
progress.

## 4. Mass model

`data/footprint_mass.json`: exact `footprint_id` → keyword substring →
area fallback (`m = density · courtyard_area + floor`, clamped to ≥ floor so
division by `m` is always safe). Users add rows for their libraries.
