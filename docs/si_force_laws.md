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
if ‖ΣF_fp‖ < f_fric → footprint frozen this step     f_fric in N
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

Adaptive-dt guard (convergence safety net, 5 lines): track system kinetic
energy; halve `dt` when it rises iteration-over-iteration, regrow ×1.08 when
it falls, floored at `dt_min`. This is FIRE's blowup-proofing without FIRE's
extra state.

Convergence: max `‖Δx‖ < eps_m` (meters), plus an energy-floor check
(specific kinetic energy below threshold = settled).

## 3. Parameters (initial guesses — calibration decides)

| Param | Unit | Guess | Role |
|---|---|---|---|
| `repulsion_k_N_m2` | N·m² | 1e-7 | pair push strength |
| `attraction_k_N` | N | 5e-5 | net spring pull |
| `wall_k_N` | N | 2e-3 | contact/halo strength |
| `wall_area_ref_m2` | m² | 4e-6 | overlap normalization |
| `wall_halo_m` | m | 0.003 | near-miss reach |
| `boundary_k_N` | N | 2e-2 | edge push (~10× wall) |
| `ghost_k_N` | N | 5e-5 | ghost spring pull |
| `damping_Ns_m` | N·s/m | 0.02 | viscous damper |
| `friction_N` | N | 1e-6 | motion deadband |
| `dt_s` | s | 1e-3 | timestep |
| `vmax_m_s` | m/s | 0.5 | speed ceiling |
| `convergence_eps_m` | m | 1e-5 | settled threshold |
| `max_iterations` | — | 1000 | budget (unchanged) |
| `mass_density_kg_m2` | kg/m² | 3.0 | fallback mass slope |
| `mass_floor_g` | g | 0.02 | minimum part mass |

Sanity anchor: an 0805 (2.5 mm² courtyard, ~8 mg) at `k_attr = 50 µN`
accelerates at ~6 m/s² — brisk but sub-ballistic at 1 ms steps
(`v·dt ≈ 6 µm/step`). If calibration lands far from these guesses, the
guesses were wrong, not the method.

## 4. Mass model

`data/footprint_mass.json`: exact `footprint_id` → keyword substring →
area fallback (`m = density · courtyard_area + floor`, clamped to ≥ floor so
division by `m` is always safe). Users add rows for their libraries.
