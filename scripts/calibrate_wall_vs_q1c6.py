#!/usr/bin/env python3
"""Calibration sweep: wall_k_N vs Q1<->C6 overlap on 2B, Q1 only movable.

For each wall_k_N, run Legalize with Q1 movable, C6 fixed, then measure
the final Q1-C6 courtyard intersection area and depth.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np

from kicad_autorouter.board_model import board_model
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.placement.contact import _footprint_courtyard_polygon
from kicad_autorouter.placement.force import _overlap_depth_mm

IN = Path("tests/fixtures/2B.kicad_pcb")

tree = parse_file(IN)
model = board_model(tree)

by_ref = {fp.ref: fp for fp in model.footprints if fp.ref}
q1 = by_ref["Q1"]
c6 = by_ref["C6"]

# Verify net membership + count pads per net (who's pulling on Q1?)
def nets_of(fp):
    return {getattr(p, "net_name", None) for p in fp.pads if getattr(p, "net_name", None)}

q1_nets = nets_of(q1); c6_nets = nets_of(c6)
shared = q1_nets & c6_nets
print(f"Q1 nets: {sorted(q1_nets)}")
print(f"C6 nets: {sorted(c6_nets)}")
print(f"Shared nets (attraction active): {sorted(shared)}")

# Count all pads per shared net on 2B (multi-pad nets pull harder)
pad_net_count = {}
for fp in model.footprints:
    if not fp.pads:
        continue
    for p in fp.pads:
        n = getattr(p, "net_name", None)
        if n:
            pad_net_count.setdefault(n, 0)
            pad_net_count[n] += 1
for n in sorted(shared):
    print(f"  net {n}: {pad_net_count.get(n, 0)} total pads on board")

def courtyard(fp, pos):
    return _footprint_courtyard_polygon(fp, pad_positions=pos,
                                        pad_indices=list(range(len(fp.pads))))

def pad_pos(fp, dx=0.0, dy=0.0):
    return np.array([[p.position.x_mm + dx, p.position.y_mm + dy] for p in fp.pads])

def overlap(poly_a, poly_b):
    if poly_a is None or poly_b is None or poly_a.is_empty or poly_b.is_empty:
        return 0.0, 0.0
    inter = poly_a.intersection(poly_b)
    cq = poly_a.centroid; cc = poly_b.centroid
    dx, dy = cq.x - cc.x, cq.y - cc.y
    dist = (dx*dx + dy*dy) ** 0.5
    if dist > 0:
        ux, uy = dx / dist, dy / dist
    else:
        ux, uy = 1.0, 0.0
    depth = _overlap_depth_mm(poly_a, poly_b, ux, uy)
    return float(inter.area), float(depth)

WALL_KNS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64]
results = []

for wall_k in WALL_KNS:
    params = PlacementParams(
        max_iterations=1000,
        attraction_k_N=0.008,
        wall_k_N=wall_k,
        wall_halo_m=0.006,
        wall_exponent=2.0,
        convergence_eps_m=2e-5,
        damping_Ns_m=0.12,
        dt_s=1e-3,
        vmax_m_s=1.0,
        friction_N=1e-6,
        debug=False,
    )
    prop = run_placement(model, params, movable_uuids={q1.uuid})

    dx_q1 = prop.deltas.get(q1.uuid, (0.0, 0.0, 0.0))
    cy_q1_f = courtyard(q1, pad_pos(q1, dx_q1[0], dx_q1[1]))
    cy_c6_f = courtyard(c6, pad_pos(c6))
    area, depth = overlap(cy_q1_f, cy_c6_f)
    results.append((wall_k, prop.stop_reason, prop.iterations,
                    prop.residual_max_force_N, area, depth, dx_q1))
    print(f"wall_k={wall_k:6.3f} stop={prop.stop_reason:9s} iters={prop.iterations:4d} "
          f"resid={prop.residual_max_force_N:.4g}N "
          f"Q1-C6 area={area:7.3f}mm^2 depth={depth:5.3f}mm "
          f"Q1_delta=({dx_q1[0]:+6.2f},{dx_q1[1]:+6.2f})mm rot={dx_q1[2]:+6.1f}deg")

# Summary: smallest wall_k that gets overlap under tolerance
tol = 0.1
ok = [r for r in results if r[4] < tol]
if ok:
    best = min(ok, key=lambda r: r[0])
    print(f"\nFirst wall_k under {tol}mm^2: {best[0]} (area={best[4]:.3f}mm^2 depth={best[5]:.3f}mm)")
else:
    print(f"\nNo wall_k up to {WALL_KNS[-1]} got Q1-C6 under {tol}mm^2")

print("\nWall-only (attraction=0) control:")
params = PlacementParams(
    max_iterations=1000, attraction_k_N=0.0, wall_k_N=0.32,
    wall_halo_m=0.006, wall_exponent=2.0, convergence_eps_m=2e-5,
    damping_Ns_m=0.12, dt_s=1e-3, vmax_m_s=1.0, friction_N=1e-6,
)
prop = run_placement(model, params, movable_uuids={q1.uuid})
dx_q1 = prop.deltas.get(q1.uuid, (0.0, 0.0, 0.0))
cy_q1_f = courtyard(q1, pad_pos(q1, dx_q1[0], dx_q1[1]))
cy_c6_f = courtyard(c6, pad_pos(c6))
area, depth = overlap(cy_q1_f, cy_c6_f)
print(f"  wall_k=0.32 attr=0: Q1-C6 area={area:.3f}mm^2 depth={depth:.3f}mm "
      f"Q1_delta=({dx_q1[0]:+6.2f},{dx_q1[1]:+6.2f})mm stop={prop.stop_reason}")
