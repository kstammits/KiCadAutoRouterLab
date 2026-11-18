# SI Force Laws for Placement Dynamics

Normative reference for the placement solver. Units are SI: **meters** for
geometry, **newtons** for force, **kilograms** for mass, **seconds** for
time. Board files, SVG output, and motion frames stay in millimeters at the
boundaries; everything here is solver-internal.

Goals, in order: **safe** (no blowups, no tunneling, collisions never
sticky), **clean** (one knob per effect, consistent units), **clear** (every
formula inspectable here).

Notation: pad position `x` (m), velocity `v` (m/s), footprint mass `m` (kg),
timestep `dt` (s), unit vector `û`.

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

Global decap-incident rail edges are **one-sided** at the unboosted
fanout-scaled rate: the decap receives the attraction, the non-decap
endpoint does not (`edge_sides` in `force.py`). This keeps a large part
from being dragged onto its decoupling cap when a rail pulls the cap
toward the part; decap–decap edges stay symmetric. Small caps
additionally gain symmetric supplemental decap-chip links (one per
shared net to the nearest `U*` within 15 mm, weight `decap_link`;
distant caps and bulk electrolytics stay global-only), so the chip
rotates toward its cap and the link overpowers rail fanout.

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

### Label orbit (parent spring + dead zone + clearance contact)
Each footprint's silkscreen reference label owns one extra integrated node
(derived ink mass at `LABEL_INK_DENSITY_KG_M2`, floored at `mass_floor_kg`)
starting at its filed `fp_text` anchor:
```
F_spring = m · (2π·40 Hz)² · excess · û(centroid→node)   outside the dead zone
```
- **Spring home** is the parent's pad centroid: the spring pulls every
  label toward its own part, and the parent carries its node along
  exactly each step (zero lag by construction), so the spring governs
  only the part-relative offset. Filed spots seed the start only.
- **Dead zone** (`label_slack_m`, default 0.5 mm): inside the dead-zone
  radius the label feels nothing — contact owns the placement and the
  friction freeze is the settle state. Beyond it the 40 Hz
  mass-proportional spring acts on the excess, so the force is
  continuous at the boundary. Small, so labels parked far away in empty
  space still feel the pull home.
- **Clamp ellipse** (`label_orbit_m`, default 3.5 mm reach, floored at
  1 mm): the node is hard-clamped to a per-label ellipse around its
  parent's centroid — the escape backstop. Each semi-axis is the reach
  floored at the parent's body half-extent along that local axis + text
  extent + 0.5 mm margin, so a stick grows room only along its length
  and the clamp hugs the flanks instead of parking the label past the
  ends. Small parts keep the plain orbit disc.
- **Clearance contact** (loop only): the label's clearance rect (text bbox
  inflated by the font-relative halo `LABEL_HALO_FONT_FRACTION`, not by a
  param) pushes through the same `_wall_mag` law as courtyards against
  nearby bodies, and shares the viscous decay / friction scaled by
  `m_label / m_ref` so labels share the parts' decay timescale. The halo
  is a soft settlement force — the finish below scores bare text, so a
  halo graze never relocates a label. The shove always beats the
  spring, so ink ends clear of copper and visible after assembly.
- **Live switch** (`label_clearance_m`, default 5 mm): a boolean gate, not
  a reach — `0` welds every label at its filed spot (no orbit rows built),
  any positive value enables orbit. The magnitude sizes nothing.
- Pads never feel labels: the pad-indexed kernel excludes label rows and
  the parent body stays the bare courtyard. Label orientation snaps to the
  nearest readable angle (0/90/180/270; 180 survives for mirrored
  bottom-side text, exact ties break upright); the final board-local offset
  is written back to `fp_text (at)`.
- **Settle repeats until quiet** (`run_label_settle`, `max_passes=3`): one
  loop rarely finishes — the end-of-loop label de-penetration relocates
  wedged labels, disturbing the equilibrium just found — so passes repeat
  from the new state until a pass moves no label (writeback-quiet, 1 nm).
  Deltas accumulate (frozen bodies share one frame, so offsets add).
- **Clean-spot search** (four rings of sixteen): a still-dirty label
  spirals outward from its anchor — current spot, center, then 0.25/0.5/
  0.75/1.0 of the bound window — and takes the nearest fully-clean
  candidate, in-ellipse only. Ring gaps can still miss a narrow slot
  beside a small part; that remainder is reported, not hidden.
- **One dirt membership, two rects**: eject rounds and search sweeps share
  the same obstacle set — own body excluded, all copper (including the
  parent's own) and nearby-neighbor silk included — so the phases cannot
  disagree about what counts as dirt. Both score bare text rects; the
  halo lives only in the loop above as a soft settlement force, never as
  finish dirt. A label buried in its own pads is pushed
  out and relocated to nearest-clean — least-dirty in-ellipse spot when
  no fully-clean candidate exists. Eject travel is capped at 2 mm per
  label; longer moves belong to the search, which picks nearest-clean
  and otherwise takes least-dirty only if strictly better than staying
  put.

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
- `sign` is latched per contact pair against near-zero noise (updates
  only when `|v_rel_t| > 1e-9` m/s, defaulting to +1 on first contact),
  so a part pressed at an angle settles to one slide direction instead of
  rubbing left/right each step. Internal solver memory
  (`ctx.last_tangent`), not a user param.
- Applied at the contact point via contact-weighted pad spread, so
  differential loading becomes torque through Kabsch.

### Manhattan snap (Tidy-regime orientation + row hold)
```
F_align = ±k_angle · t̂(pair)      on the reference pair only (pure torque)
F_hold  = −k_pos · (c − anchor)   spread over the footprint's pads
```
- The snapped quantity is the PACKAGE rotation (filed angle tracked
  through live rigid rotation), not the longest pad pair: for dual-row
  packs the longest pair is a diagonal, and servoing it would twist a
  straight pack askew. The pair still applies the torque (max lever).
- Targets are the nearest compass mode ({0, 90}, at most a 45° snap) for
  movable multi-pad outliers (`err > deadband`), frozen at first
  engagement and released below half the deadband. Parts snap freely
  unless a coherent neighborhood disagrees (a quarter of the in-radius
  neighbors within twice the deadband). Single-pad parts never rotate
  (no axis to couple) but vote with their filed angle. Locked and ghost
  bodies vote but never receive.
- Release is enforced every dynamics step; the recompute still owns
  engagement, target freezing, and anchor refresh.
- Position hold is a weak leash to the last recompute anchor (off at
  `k_pos = 0`); row-engaged parts carry frozen baseline/pitch targets
  instead of the leash anchor. The wall law vets every rotation the same
  iteration it happens, so torque-balanced pockets rest off-mode — the
  wall still wins every shove.

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
if net footprint force < 20·friction_N AND rim-equivalent torque < 20·friction_N
→ footprint frozen this step
```
Whole-footprint all-or-nothing freeze in force units (`rim-equivalent`
torque is `|torque| / max lever`, same bar, so pure torque cannot strand
a rocking body). Net (pads summed),
not per-pad max: pressed-into-wall rests carry big canceling per-pad
loads with ~zero net, and must rest instead of chattering. Re-evaluated
live every step; 0 disables.

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

Adaptive-dt guard: track kinetic energy per footprint plus the board-wide
total; halve `dt` (and bleed velocity) on any significant jump, regrow
×1.1 when quiet, floored at `dt_min_s`, capped at `dt_max_s`. The
per-part floor scales with the part's own mass (the global floor scales
with `m_max`, hiding single light parts — without the local guard they
ping-pong wall-to-wall forever). Both floors are damping-aware. A leaky
trigger counter (heat) suppresses regrowth while trips recur: isolated
transients recover almost immediately, a chronic hopper pins `dt` down
until it actually stops. The absolute floor for a "significant" rise is
a fraction of `0.5·m·vmax²`, so contact noise does not ratchet the
timestep down.

Contact direction rule: push along the SAT minimum-translation (MTV) axis —
out the nearest edge — never centroid-to-centroid, which is wrong for
containment / small-in-large overlaps.

Per-pair aggregation: body + THT-pin-circle sub-paths of one footprint pair
report signed separations and the single deepest contact wins
(winner-takes-all), so pin circles cannot stack the same contact. Multi-
neighbor superposition is unaffected (aggregation is per pair, not global).

Convergence: max `‖Δx‖ < convergence_eps_m` (meters) per step AND residual
forces below the settle scale
(`force_settle_N = max(20·friction, 4·ghost_k, 1e-3·max(wall, attraction))`),
OR dead window-mean drift (`< convergence_eps_m`) with quiet residual —
limit-cycle chatter whose center sits still with nothing left to obey.
`stop_reason` reports `converged` (quiet AND weak, either path), `stalled` (no
window-mean progress with residual forces), `budget` (max_iterations with
residual force), or `empty`. Headless callers must treat `stalled` and
`budget` as needs-attention, never as settled. `depenetrated` flags a
post-loop geometric eject: the final pose moved after dynamics stopped,
so `converged` + `depenetrated` means settled-then-pushed, and the
residual was re-read at the ejected pose.

Stall detection compares the mean pose over each `stall_window` against the
previous window mean. A limit cycle averages out; real transit moves the
mean. `stall_window=0` disables.

## 3. Parameters

| Param | Unit | Default | Role |
|---|---|---|---|
| `repulsion_k_N_m2` | N·m² | 2e-9 | pair push (sub-contact only) |
| `attraction_k_N` | N | 8e-3 | net pull (Legalize preset: 0.004) |
| `wall_k_N` | N | 0.1 | contact/halo strength |
| `wall_halo_m` | m | 0.006 | near-miss reach and depth scale (Legalize/Tidy presets: 0.0005; Cluster: 0.001) |
| `cross_layer_pin_halo_m` | m | 0.0007 | THT-pin vs far-side body near-miss (0 = off) |
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
| `decap_link` | — | 4.0 | local decap-chip link weight (globals never boosted) |
| `overlap_tolerance_mm2` | mm² | 0.1 | legality threshold |
| `wall_tolerance_mm` | mm | 0.1 | board-wall legality threshold |
| `label_clearance_m` | m | 0.005 | label orbit live switch (0 = welded at filed spots) |
| `label_orbit_m` | m | 0.0035 | label clamp-ellipse floor reach (1 mm floor, per-axis) |
| `label_slack_m` | m | 0.0005 | label free-float radius (spring on excess only) |
| `align_angle_k_N` | N | 0.0 | Manhattan-snap couple (0 = off; Tidy: 3e-3) |
| `align_pos_k_N_m` | N/m | 0.0 | position-hold leash (0 = off; Tidy: 0.5) |
| `align_deadband_deg` | deg | 10.0 | consensus error ignored by the snap |
| `align_radius_m` | m | 0.020 | neighbor-search radius (k-nearest 6) |
| `include_power_nets` | — | true | kill-switch for power-rail participation |

Regime map (no single ratio both clusters scattered starts and legalizes
placed boards — use two stages):
- Cluster (scattered starts, e.g. magnet synthetics): `attraction_k_N`
  ≈ 0.04–0.06 with `wall_k_N` ≈ 0.02–0.03, `damping` 0.08, `dt` 2 ms.
  Tightens groups to ~0.3–0.6 of start spread, zero overlap.
- Legalize (placed boards: tube111, DCCF): `attraction_k_N` ≈ 0.004
  with `wall_k_N` ≈ 0.08–0.1, `damping` 0.12–0.15, `wall_halo_m` 0.5 mm,
  `dt` 1 ms. The shorter halo (engine default 6 mm) keeps dense neighbor
  fields from overriding a part's own wall; Cluster uses a 1 mm halo.
- Tidy (placed boards + Manhattan snap): Legalize values plus
  `align_angle_k_N` 3e-3, `align_pos_k_N_m` 0.5, `friction_N` 1e-4 (freeze
  bar 2 mN: above chatter, below the ~6 mN align couple),
  `contact_tangent_gamma` 0 (polish must settle; unjamming is Legalize's
  job), `max_iterations` 150 — weak enough that the wall still wins
  every shove.

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
area fallback (`m = density · courtyard_area + floor`, never zero).
Users add rows for their libraries.
