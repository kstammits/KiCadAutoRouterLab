# SI Force Laws for Placement Dynamics

Normative reference for the new physics system (`feature/si-dynamics` branch).
Units are SI throughout the solver: **meters** for geometry, **newtons** for
force, **kilograms** for mass, **seconds** for time. Board files, the SVG, and
motion frames stay in millimeters at the boundaries (converted at
ingest/report); everything in this document is solver-internal.

We are NOT tied to legacy parameter values or ranges. Magnitudes below are
starting guesses only — `scripts/calibrate_si.py` learns the real ranges
empirically. Goals, in order: **safe** (no blowups, no tunneling, collisions
never sticky), **clean** (one knob per effect, consistent units), **clear**
(every formula inspectable here). How elastic colliding parts "should" be is
unknown: start soft and let the sweep decide.

Notation: pad position `x` (m), velocity `v` (m/s), footprint mass `m` (kg,
from `data/footprint_mass.json`), timestep `dt` (s), unit vector `û`.

## 1. Force laws (all in newtons)

### Pad repulsion (all-pairs, Coulomb-like)
```
F_rep(i←j) = k_rep · û(i−j) / d²        d in meters, k_rep in N·m²
```
Same shape as legacy (`kr/d²`), new units. Isolated-pair equilibrium with
attraction at `d = √(k_rep / k_attr)`.

### Net attraction (constant-magnitude springs)
```
F_attr = k_attr · w_edge · û(src→dst)   k_attr in N, w_edge dimensionless
```
Unchanged shape (no Hooke length term). `w_edge` keeps the existing
fanout/decap/power weighting (dimensionless, already unit-clean).

### Courtyard collision (walls)
Overlap (`area` in m²):
```
F_wall = k_wall · (1 + area / area_ref) · û      k_wall in N, area_ref in m²
```
The dimensionless ratio `area/area_ref` replaces the legacy raw-mm² term so
the constant doesn't explode across part sizes. At first contact (`area=0`)
this equals `k_wall`, continuous with the halo form below.

Near-miss halo (`gap`, `halo` in meters):
```
F_halo = k_wall · (1 − gap / halo) · û           0 at halo edge, k_wall at contact
```
Direction is the pair's nearest-points normal in both cases.

### Boundary repulsion
Same piecewise shape as legacy, in newtons: `0` beyond threshold,
linear `2·k_b·(t−d)/t` approaching the edge, saturating at `~k_b` outside.
`k_b` in N, threshold in meters.

### Ghost attraction
```
F_ghost = k_ghost · û(target→anchor)              k_ghost in N
```
Split equally over target pads; no reaction on the ghost (infinite mass).

### Viscous damping (the physics damper)
```
F_damp = −c · v                                   c in N·s/m
```
One clean knob. Per-pad application; rigid projection (below) keeps bodies
consistent.

### Dry friction (deadband, whole footprint)
```
if ‖F_pad‖max over footprint < f_fric → footprint frozen this step     f_fric in N
```
Keeps the legacy all-or-nothing rigid semantics, now in force units so it
stops micro-motion without a displacement hack.

## 2. Integrator (semi-implicit Euler + guards)

Per movable pad, per iteration:
```
v += (F / m) · dt        # kick (a = F/m in m/s²)
v *= 1 / (1 + c·dt / m)  # implicit-Euler viscous decay (stable for any dt)
if ‖v‖·dt > vmax:  v *= vmax / (‖v‖·dt)   # speed ceiling, replaces max_step_mm
x += v · dt              # drift
```
then the existing Kabsch rigid projection snaps pads onto exact bodies, and
velocity is re-derived from the actual projected displacement
`v := (x_proj − x_old) / dt` so projection can't inject phantom energy.

Rigid-step clamp (tunneling guard): the velocity ceiling alone cannot bound
rotation — the Kabsch snap can swing pads of a differentially-forced body
past `vmax·dt` (observed: 7 mm leap against a 3 mm cap; −40° spins). After
projection, each body's centroid shift is clamped to `vmax·dt` and its
rotation change to 5°/step, re-posed rigidly from the pre-step pose.
Velocity re-derivation runs after the clamp, so it reflects clamped motion.

Adaptive-dt guard (convergence safety net, 5 lines): track system kinetic
energy; halve `dt` when it rises iteration-over-iteration, regrow ×1.02 when
it falls, floored at `dt_min`, capped at `dt_max`. This is FIRE's
blowup-proofing without FIRE's extra state. Floor `dt_min = 2e-4` (not
1e-5): the halve/regrow asymmetry ratchets `dt` to the floor under
oscillation, where tiny steps fake convergence while forces are still
large. Ceiling `dt_max = 2e-3` keeps dense-board steps accurate.

Contact direction rule (overlap): push along the SAT minimum-translation
(MTV) axis — out the nearest edge — never centroid-to-centroid, which is
wrong for containment / small-in-large overlaps (shoves the small part
deeper along the long axis). Generic (non-rect/circle) polygons keep the
centroid fallback.

Per-pair aggregation: body + THT-pin-circle sub-paths of ONE footprint pair
aggregate into a single penalty (summed overlap areas along the strongest
sub-direction, else the strongest halo), so pin circles can't stack the
same contact 3–9×. Multi-neighbor superposition is unaffected (aggregation
is per pair, not global).

Convergence: max `‖Δx‖ < eps_m` (meters) per step. Long Coulomb-tail
cruises terminate via the friction deadband (whole-footprint freeze when
all pad forces drop below `friction_N`), which is why `friction_N` must
be scaled to the tail strength (`kr`): a tail that stays above the
deadband cruises past any iteration budget.

## 3. Parameters (calibrated 2026-10-15 — scripts/calibrate_si.py)

| Param | Unit | Value | Role |
|---|---|---|---|
| `repulsion_k_N_m2` | N·m² | 2e-9 | pair push (sub-contact only) |
| `attraction_k_N` | N | 8e-3 | legalize-regime net pull |
| `wall_k_N` | N | 8e-2 | contact/halo strength |
| `wall_area_ref_m2` | m² | 4e-6 | overlap normalization |
| `wall_halo_m` | m | 0.006 | near-miss reach |
| `boundary_k_N` | N | 1e-2 | edge push |
| `ghost_k_N` | N | 5e-5 | ghost spring pull (untuned) |
| `damping_Ns_m` | N·s/m | 0.12 | viscous damper |
| `friction_N` | N | 1e-6 | motion deadband |
| `dt_s` | s | 1e-3 | timestep |
| `dt_min_s` | s | 2e-4 | guard floor |
| `dt_max_s` | s | 2e-3 | guard ceiling |
| `vmax_m_s` | m/s | 1.0 | speed ceiling |
| `convergence_eps_m` | m | 2e-5 | settled threshold |
| `max_iterations` | — | 1000 | budget (unchanged) |
| `mass_density_kg_m2` | kg/m² | 3.0 | fallback mass slope |
| `mass_floor_kg` | kg | 1e-9 | minimum part mass |

Regime map (no single ratio clusters AND legalizes — use two stages):
- Cluster (scattered starts, e.g. magnet synthetics): `attraction_k_N`
  ≈ 0.04–0.06 with `wall_k_N` ≈ 0.02–0.03, `damping` 0.08, `dt` 2 ms.
  Tightens groups to ~0.3–0.6 of start spread, zero overlap.
- Legalize (placed boards: tube111, DCCF): `attraction_k_N` ≈ 0.005–0.01
  with `wall_k_N` ≈ 0.08–0.1, `damping` 0.12–0.15, `dt` 1 ms.
  Tube111 overlap more than halves; DCCF initial pairs shrink.
  The engine default above is the legalize side (safe).

Sanity anchor: an 0805 (2.5 mm² courtyard, ~8 mg) at `k_attr = 50 µN`
accelerates at ~6 m/s² — brisk but sub-ballistic at 1 ms steps
(`v·dt ≈ 6 µm/step`). If calibration lands far from these guesses, the
guesses were wrong, not the method.

Known residuals (follow-ups, not regressions): DCCF keeps 1–2 wedged
pairs (e.g. R13 vs RV1's 76 mm-wide courtyard span: net anchor lies
across a locked obstacle → shuttle limit cycle); sub-0.1°/step rotation
creep is dropped by the angular deadband (`MIN_ROT_STEP_RAD` in the
rigid-step clamp — a noise floor, not a slider), but large slow flips
still occur when the torque is genuine (observed: 0805 flipping ~160°
toward its rail over 400 iters — a compass-needle response, kept
deliberately since gradual movement must keep working).

Future, not planned: orientation-matching neighboring parts. It conflicts
with the force-driven orientation above (neighbors agreeing on an angle
vs each part following its own net/wall torque), so it needs its own
design before anyone builds it.

## 4. Mass model

`data/footprint_mass.json`: exact `footprint_id` → keyword substring →
area fallback (`m = density · courtyard_area + floor`, clamped to ≥ floor so
division by `m` is always safe). Users add rows for their libraries.
