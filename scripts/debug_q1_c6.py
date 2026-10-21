#!/usr/bin/env python3
"""Debug: run Legalize on 2B with Q1 as the ONLY movable part; measure Q1 forces
and the Q1<->C6 courtyard overlap. C6 is held fixed (not in movable set).
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
print(f"Q1: uuid={q1.uuid} locked={q1.locked} layer={q1.layer} pos=({q1.x_mm:.2f},{q1.y_mm:.2f}) angle={q1.angle_deg}")
print(f"C6: uuid={c6.uuid} locked={c6.locked} layer={c6.layer} pos=({c6.x_mm:.2f},{c6.y_mm:.2f}) angle={c6.angle_deg}")

# Net membership for pads
print("\nQ1 pads:")
for p in q1.pads:
    print(f"  pad ({p.position.x_mm:.2f},{p.position.y_mm:.2f}) net={getattr(p, 'net_name', None)}")
print("\nC6 pads:")
for p in c6.pads:
    print(f"  pad ({p.position.x_mm:.2f},{p.position.y_mm:.2f}) net={getattr(p, 'net_name', None)}")

q1_nets = {getattr(p, 'net_name', None) for p in q1.pads if getattr(p, 'net_name', None)}
c6_nets = {getattr(p, 'net_name', None) for p in c6.pads if getattr(p, 'net_name', None)}
print(f"\nQ1 nets: {q1_nets}")
print(f"C6 nets: {c6_nets}")
print(f"Shared nets: {q1_nets & c6_nets}")

def courtyard(fp, pos):
    return _footprint_courtyard_polygon(fp, pad_positions=pos,
                                        pad_indices=list(range(len(fp.pads))))

def overlap(poly_a, poly_b):
    if poly_a is None or poly_b is None or poly_a.is_empty or poly_b.is_empty:
        return 0.0, 0.0
    inter = poly_a.intersection(poly_b)
    cq = poly_a.centroid; cc = poly_b.centroid
    dx, dy = cq.x - cc.x, cq.y - cc.y
    dist = (dx*dx + dy*dy) ** 0.5
    ux, uy = dx / dist, dy / dist
    depth = _overlap_depth_mm(poly_a, poly_b, ux, uy)
    return inter.area, depth

def pad_pos(fp, dx=0.0, dy=0.0):
    return np.array([[p.position.x_mm + dx, p.position.y_mm + dy] for p in fp.pads])

# Initial overlap
p_q1 = pad_pos(q1); p_c6 = pad_pos(c6)
cy_q1 = courtyard(q1, p_q1); cy_c6 = courtyard(c6, p_c6)
area0, depth0 = overlap(cy_q1, cy_c6)
print(f"\nInitial Q1-C6: intersection area={area0:.4f} mm^2 depth={depth0:.4f} mm")

# Static force probe (max_iterations=0) with Q1 movable only
params_static = PlacementParams(
    max_iterations=0,
    attraction_k_N=0.008,
    wall_k_N=0.08,
    wall_halo_m=0.006,
    wall_exponent=2.0,
    convergence_eps_m=2e-5,
    damping_Ns_m=0.12,
    dt_s=1e-3,
    vmax_m_s=1.0,
    friction_N=1e-6,
    debug=True,
)
prop_static = run_placement(model, params_static, movable_uuids={q1.uuid})
print("\n=== STATIC PROBE (Q1 only movable, 0 iters) ===")
print(f"stop_reason={prop_static.stop_reason} iterations={prop_static.iterations}")
print(f"Q1 force (summed): {prop_static.forces.get(q1.uuid)}")
print(f"Q1 force_components: {prop_static.force_components.get(q1.uuid)}")
print(f"C6 in deltas? {c6.uuid in prop_static.deltas} (expect False)")
print(f"legality_is_legal={prop_static.legality_is_legal}")
print(f"overlapping_pairs: {prop_static.overlapping_pairs}")
print(f"wall_violations: {prop_static.wall_violations}")
print(f"max_pair_overlap_mm2={prop_static.max_pair_overlap_mm2}")

# Full Legalize run with Q1 only movable
params = PlacementParams(
    max_iterations=1000,
    attraction_k_N=0.008,
    wall_k_N=0.08,
    wall_halo_m=0.006,
    wall_exponent=2.0,
    convergence_eps_m=2e-5,
    damping_Ns_m=0.12,
    dt_s=1e-3,
    vmax_m_s=1.0,
    friction_N=1e-6,
    debug=True,
)
prop = run_placement(model, params, movable_uuids={q1.uuid})
print("\n=== LEGALIZE RUN (Q1 only movable) ===")
print(f"stop_reason={prop.stop_reason} iterations={prop.iterations} residual_max_force={prop.residual_max_force_N:.3g}N")
print(f"moved footprints: {list(prop.deltas.keys())}")
print(f"Q1 delta (dx,dy,da): {prop.deltas.get(q1.uuid)}")
print(f"Q1 force (final, summed): {prop.forces.get(q1.uuid)}")
print(f"Q1 force_components (final): {prop.force_components.get(q1.uuid)}")
print(f"C6 moved? {c6.uuid in prop.deltas} (expect False)")
print(f"legality_is_legal={prop.legality_is_legal}")
print(f"overlapping_pairs: {prop.overlapping_pairs}")
print(f"wall_violations: {prop.wall_violations}")
print(f"max_pair_overlap_mm2={prop.max_pair_overlap_mm2}")

# Final Q1-C6 overlap after the run
if q1.uuid in prop.deltas:
    dx, dy, da = prop.deltas[q1.uuid]
else:
    dx = dy = 0.0
cy_q1_f = courtyard(q1, pad_pos(q1, dx, dy))
cy_c6_f = courtyard(c6, pad_pos(c6))
area1, depth1 = overlap(cy_q1_f, cy_c6_f)
print(f"\nFinal Q1-C6: intersection area={area1:.4f} mm^2 depth={depth1:.4f} mm")
print(f"Q1 final pos=({q1.x_mm + dx:.2f},{q1.y_mm + dy:.2f})")

# Debug trace report
if prop.debug_trace:
    iters = prop.debug_trace.get("per_iteration", [])
    depen = prop.debug_trace.get("depen", [])
    print(f"\n=== DEBUG TRACE ===")
    print(f"per-iteration snapshots: {len(iters)}")
    if iters:
        # Show Q1 forces at first, middle, last iteration
        for label, snap in (("first", iters[0]), ("middle", iters[len(iters)//2]), ("last", iters[-1])):
            fdata = snap["footprints"].get(q1.uuid)
            if fdata:
                nf = fdata["net_force"]
                print(f"  {label} iter {snap['iteration']}: Q1 net_force=({nf[0]:+.4f},{nf[1]:+.4f})N pos=({fdata['pad_positions'][0][0]:.2f},{fdata['pad_positions'][0][1]:.2f})")
    if depen:
        print(f"depen pairs: {len(depen)}")
        for d in depen:
            if q1.uuid in d["pair"] or c6.uuid in d["pair"]:
                print(f"  depen {d['pair']}: area={d['area_mm2']:.4f}mm^2 depth={d['depth_mm']:.4f}mm step={d['step_mm']:.4f}mm sa={d['sa_mm']:.4f} sb={d['sb_mm']:.4f}")
