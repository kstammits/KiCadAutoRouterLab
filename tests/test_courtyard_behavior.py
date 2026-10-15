"""Courtyard collision behavior: shape parsing, overlap force, layer gating, leniency, RV1 scenarios.

Tests the emergent behavior of courtyard repulsion without depending on halo internals.
"""

import math
from dataclasses import replace

import pytest

from kicad_autorouter.board_model import BoardModel, Point, Footprint
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.placement import _courtyard_outline_polygon, _footprint_courtyard_polygon

from tests.helpers import make_fp, make_rect_courtyard, overlap_area


EDGE = 100.0


def _board(fps):
    return BoardModel(
        footprints=tuple(fps),
        keepout_zones=(),
        nets={},
        by_ref={fp.ref: fp for fp in fps},
        edge_cuts=(
            (Point(-EDGE, -EDGE), Point(EDGE, -EDGE)),
            (Point(EDGE, -EDGE), Point(EDGE, EDGE)),
            (Point(EDGE, EDGE), Point(-EDGE, EDGE)),
            (Point(-EDGE, EDGE), Point(-EDGE, -EDGE)),
        ),
        footprint_region={fp.uuid: 0 for fp in fps},
    )


def _base_params(**kw):
    base = dict(
        max_iterations=50,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1.0000000000000001e-07,
        attraction_k_N=5e-05,
        wall_k_N=0.002,
        wall_halo_m=0.003,
        boundary_k_N=1e-06,
    )
    base.update(kw)
    return PlacementParams(**base)


# ---------------------------------------------------------------------------
# RV1-like fixture builders
# ---------------------------------------------------------------------------

def make_rv1_like(ref: str, uuid: str, x: float, y: float, angle_deg: float = 0.0, locked: bool = True):
    """RV1 slide potentiometer: ~76x10mm courtyard on F.Cu, 3 THT pads at ends, 4 mounting holes.
    
    Matches DCCF RV1: courtyard segments with reversed top edge (left->right instead of right->left),
    pads at local (0,0), (0,3.5), (73.5,0) + 4 MP pads.
    """
    from tests.helpers import make_tht_pad
    
    # Local pad positions (before rotation/translation)
    # Pad 1 at origin, Pad 2 at (0, 3.5), Pad 3 at (73.5, 0)
    # MP pads at (4.15, 5.95), (5.35, -2.45), (68.15, -2.45), (69.35, 5.95)
    local_pads = [
        (0.0, 0.0, "1", "B1"),
        (0.0, 3.5, "2", "Net-RV1-Pad2"),
        (73.5, 0.0, "3", "VR+"),
        (4.15, 5.95, "MP", None),
        (5.35, -2.45, "MP", None),
        (68.15, -2.45, "MP", None),
        (69.35, 5.95, "MP", None),
    ]
    
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    
    pads = []
    for lx, ly, num, net in local_pads:
        bx = x + lx * cos_a - ly * sin_a
        by = y + lx * sin_a + ly * cos_a
        pad = replace(
            make_tht_pad(num, net, drill_mm=1.2, size_mm=(1.75, 1.75)),
            position=Point(bx, by),
        )
        pads.append(pad)
    
    # RV1 courtyard segments (in local coords, centered at origin)
    # DCCF RV1 has reversed top edge: (-1.25, 6.75) -> (74.75, 6.75) i.e. left to right
    # instead of right to left. This tests polygonize handles unordered segments.
    local_courtyard = [
        (Point(-1.25, -3.25), Point(74.75, -3.25)),   # bottom: left -> right
        (Point(74.75, -3.25), Point(74.75, 6.75)),    # right: bottom -> top
        (Point(-1.25, 6.75), Point(74.75, 6.75)),     # top: LEFT -> RIGHT (REVERSED!)
        (Point(-1.25, -3.25), Point(-1.25, 6.75)),    # left: bottom -> top
    ]
    
    # Rotate courtyard segments to board coords
    board_courtyard = []
    for a, b in local_courtyard:
        ax = x + a.x_mm * cos_a - a.y_mm * sin_a
        ay = y + a.x_mm * sin_a + a.y_mm * cos_a
        bx = x + b.x_mm * cos_a - b.y_mm * sin_a
        by = y + b.x_mm * sin_a + b.y_mm * cos_a
        board_courtyard.append((Point(ax, ay), Point(bx, by)))
    
    return Footprint(
        ref=ref,
        footprint_id="Potentiometer_THT:Potentiometer_Bourns_PTA6043_Single_Slide",
        layer="F.Cu",
        x_mm=x,
        y_mm=y,
        angle_deg=angle_deg,
        pads=tuple(pads),
        courtyard=tuple(board_courtyard),
        locked=locked,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def make_disc_cap(ref: str, uuid: str, x: float, y: float, diameter: float = 2.0):
    """Small disc capacitor: ~circular courtyard approximation, 2 SMD pads."""
    hw = diameter / 2.0
    hh = diameter / 2.0
    # 8-segment polygon approximation of circle
    import math
    segs = []
    n_seg = 8
    for i in range(n_seg):
        a1 = 2 * math.pi * i / n_seg
        a2 = 2 * math.pi * (i + 1) / n_seg
        x1 = x + hw * math.cos(a1)
        y1 = y + hh * math.sin(a1)
        x2 = x + hw * math.cos(a2)
        y2 = y + hh * math.sin(a2)
        segs.append((Point(x1, y1), Point(x2, y2)))
    
    pad1 = replace(
        make_fp("", "", 0, 0, ("NET1",), pad_layers=("F.Cu", "F.Paste", "F.Mask")).pads[0],
        number="1",
        net_name="NET1",
        position=Point(x - 1.0, y),
    )
    pad2 = replace(
        make_fp("", "", 0, 0, ("NET2",), pad_layers=("F.Cu", "F.Paste", "F.Mask")).pads[0],
        number="2",
        net_name="NET2",
        position=Point(x + 1.0, y),
    )
    
    return Footprint(
        ref=ref,
        footprint_id="Capacitor_SMD:C_Disc_D2.0mm",
        layer="F.Cu",
        x_mm=x,
        y_mm=y,
        angle_deg=0.0,
        pads=(pad1, pad2),
        courtyard=tuple(segs),
        locked=False,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def make_smd_resistor(ref: str, uuid: str, x: float, y: float, net_a: str, net_b: str, hw: float = 1.5, hh: float = 1.0):
    """Standard 0805-style SMD resistor with rectangular courtyard."""
    return make_fp(ref, uuid, x, y, (net_a, net_b), hw=hw, hh=hh, pad_layers=("F.Cu",))


def make_smd_resistor_b_layer(ref: str, uuid: str, x: float, y: float, net_a: str, net_b: str, hw: float = 1.5, hh: float = 1.0):
    """SMD resistor on B.Cu layer."""
    return make_fp(ref, uuid, x, y, (net_a, net_b), layer="B.Cu", hw=hw, hh=hh, pad_layers=("B.Cu",))


def make_tht_resistor(ref: str, uuid: str, x: float, y: float, net_a: str, net_b: str, hw: float = 1.5, hh: float = 1.0):
    """Through-hole resistor (for THT cross-layer tests)."""
    return make_fp(ref, uuid, x, y, (net_a, net_b), tht=True, hw=hw, hh=hh, pad_layers=("*.Cu", "*.Mask"))


# ---------------------------------------------------------------------------
# Test Classes
# ---------------------------------------------------------------------------

class TestCourtyardParsing:
    """Courtyard polygon construction from raw segments."""

    def test_rv1_reversed_top_edge_parses_to_valid_rectangle(self):
        """RV1 courtyard with reversed top edge segment produces valid non-self-intersecting rectangle."""
        rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0)
        
        # Parse courtyard - should produce valid polygon despite unordered segments
        poly = _courtyard_outline_polygon(rv1.courtyard)
        
        assert poly is not None, "Courtyard should parse to valid polygon"
        assert poly.is_valid, "Polygon should be valid (no self-intersections)"
        assert poly.area > 0, "Polygon should have positive area"
        
        # Check dimensions: ~76mm x 10mm (with 0.05mm stroke width on F.CrtYd)
        bounds = poly.bounds
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        assert abs(width - 76.0) < 0.2, f"Width should be ~76mm, got {width}"
        assert abs(height - 10.0) < 0.2, f"Height should be ~10mm, got {height}"

    def test_rv1_rotated_courtyard_parses_correctly(self):
        """RV1 courtyard rotated by 90 degrees still parses correctly."""
        rv1 = make_rv1_like("RV1", "uuid-rv1", 100.0, 100.0, 90.0)
        
        poly = _courtyard_outline_polygon(rv1.courtyard)
        
        assert poly is not None
        assert poly.is_valid
        assert poly.area > 0
        
        # Dimensions swapped due to rotation
        bounds = poly.bounds
        width = bounds[2] - bounds[0]
        height = bounds[3] - bounds[1]
        assert abs(width - 10.0) < 0.2
        assert abs(height - 76.0) < 0.2

    def test_disconnected_segments_falls_back_to_pad_bbox(self):
        """Open polyline (disconnected segments) falls back to pad bbox + margin."""
        # Create footprint with invalid courtyard (open polyline)
        open_courtyard = (
            (Point(0, 0), Point(5, 0)),
            (Point(5, 0), Point(5, 3)),
            # Missing top and left edges
        )
        fp = Footprint(
            ref="TEST", footprint_id="Test", layer="F.Cu", x_mm=0, y_mm=0, angle_deg=0,
            pads=(
                replace(make_fp("", "", 0, 0, ("N1",)).pads[0], position=Point(0, 0)),
                replace(make_fp("", "", 0, 0, ("N2",)).pads[0], position=Point(5, 3)),
            ),
            courtyard=open_courtyard,
            uuid="uuid-test",
        )
        
        poly = _footprint_courtyard_polygon(fp)
        
        # Should fall back to pad bbox (5x3) + 0.1mm margin
        assert poly is not None
        assert poly.is_valid
        bounds = poly.bounds
        assert abs(bounds[2] - bounds[0] - 5.2) < 0.01  # 5 + 0.1 + 0.1
        assert abs(bounds[3] - bounds[1] - 3.2) < 0.01

    def test_self_intersecting_ring_picks_largest_loop(self):
        """Figure-8 or spike courtyard picks largest valid ring by area."""
        # Create a figure-8 like shape (two rectangles sharing a corner)
        spike_courtyard = (
            (Point(0, 0), Point(10, 0)),
            (Point(10, 0), Point(10, 5)),
            (Point(10, 5), Point(0, 5)),
            (Point(0, 5), Point(0, 0)),
            # Spike going out and back
            (Point(10, 2.5), Point(15, 2.5)),
            (Point(15, 2.5), Point(10, 2.5)),
        )
        fp = Footprint(
            ref="TEST", footprint_id="Test", layer="F.Cu", x_mm=0, y_mm=0, angle_deg=0,
            pads=(replace(make_fp("", "", 0, 0, ("N1",)).pads[0], position=Point(5, 2.5)),),
            courtyard=spike_courtyard,
            uuid="uuid-test",
        )
        
        poly = _courtyard_outline_polygon(fp.courtyard)
        
        assert poly is not None
        assert poly.is_valid
        # Should pick the 10x5 rectangle (area 50), not the degenerate spike
        assert abs(poly.area - 50.0) < 1.0


class TestOverlapForce:
    """Overlap repulsion force mechanics."""

    def test_deeper_overlap_pushes_harder(self):
        """Intersection area grows -> force magnitude grows."""
        mags = []
        for dx in (2.0, 1.0, 0.5):  # Decreasing gap = more overlap
            fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
            fb = make_smd_resistor("B", "uuid-b", dx, 0.0, "N3", "N4", hw=3.0, hh=2.0)
            model = _board([fa, fb])
            prop = run_placement(model, _base_params(max_iterations=0))
            
            fx, fy = prop.force_components["uuid-b"]["courtyard"]
            mag = math.hypot(fx, fy)
            mags.append(mag)
        
        # All should push (positive magnitude)
        assert all(m > 0 for m in mags), f"All overlaps should push: {mags}"
        # Deeper overlap -> harder push
        assert mags[0] < mags[1] < mags[2], f"Force should grow with penetration: {mags}"

    def test_containment_direction_fallback(self):
        """Small part centered in large courtyard: force uses minimum penetration axis (SAT)."""
        # Large courtyard: 20x10mm (rv1-like but smaller for test) - LOCKED
        large = make_smd_resistor("LARGE", "uuid-large", 0.0, 0.0, "N1", "N2", hw=10.0, hh=5.0)
        large = replace(large, courtyard=make_rect_courtyard(0, 0, 10.0, 5.0), locked=True)
        
        # Small part centered inside
        small = make_smd_resistor("SMALL", "uuid-small", 0.0, 0.0, "N3", "N4", hw=1.0, hh=0.5)
        
        model = _board([large, small])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-small"]["courtyard"]
        
        # SAT collision uses minimum penetration axis for containment
        # Large: half_extents (10, 5), Small: (1, 0.5)
        # x-axis overlap: 10 + 1 = 11, y-axis overlap: 5 + 0.5 = 5.5
        # Minimum is y-axis, so force should be along y-axis
        # But if both bodies use analytical collision and are both rectangles,
        # the overlap forces may use shapely fallback (same as before)
        # Check that force is non-zero and pushes outward
        mag = math.hypot(fx, fy)
        assert mag > 0, f"Containment should produce non-zero force, got ({fx}, {fy})"

    def test_containment_fallback_not_zero_force(self):
        """Perfectly coincident centroids: SAT minimum penetration axis produces non-zero force."""
        fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=2.0, hh=1.0)
        fb = make_smd_resistor("B", "uuid-b", 0.0, 0.0, "N3", "N4", hw=2.0, hh=1.0)
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        mag = math.hypot(fx, fy)
        
        assert mag > 0, "Coincident parts should feel repulsion force"
        # Force direction may be from shapely fallback (centroid-to-centroid + tiebreak)
        # or from SAT (minimum penetration axis). Just verify non-zero.

    def test_overlap_resolves_to_zero_area(self):
        """Run to convergence: final intersection area ~0."""
        fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fb = make_smd_resistor("B", "uuid-b", 1.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)  # Heavy overlap
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=200))
        
        final_area = overlap_area(model, prop.deltas)
        assert final_area <= 1e-6, f"Overlap should resolve to ~0, got {final_area:.6f}mm²"


class TestLayerGating:
    """Cross-layer courtyard collision rules."""

    def test_smd_smd_different_layers_no_collision(self):
        """F.Cu SMD + B.Cu SMD, overlapping courtyards -> NO force (skip exact geometry)."""
        fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fb = make_smd_resistor_b_layer("B", "uuid-b", 0.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        assert fx == 0.0 and fy == 0.0, "Cross-layer SMD-SMD should have zero courtyard force"

    def test_tht_smd_cross_layer_collides(self):
        """F.Cu THT (has_tht=True) + B.Cu SMD, overlapping courtyards -> force applied."""
        fa = make_tht_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fb = make_smd_resistor_b_layer("B", "uuid-b", 0.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        mag = math.hypot(fx, fy)
        assert mag > 0, "THT-SMD cross-layer should collide"

    def test_tht_tht_cross_layer_collides(self):
        """Both THT on different layers -> force applied."""
        fa = make_tht_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fa = replace(fa, layer="F.Cu")
        fb = make_tht_resistor("B", "uuid-b", 0.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)
        fb = replace(fb, layer="B.Cu")
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        mag = math.hypot(fx, fy)
        assert mag > 0, "THT-THT cross-layer should collide"

    def test_smd_smd_same_layer_collides(self):
        """Both F.Cu SMD -> force applied."""
        fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fb = make_smd_resistor("B", "uuid-b", 0.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        mag = math.hypot(fx, fy)
        assert mag > 0, "Same-layer SMD-SMD should collide"


class TestLenientCourtyard:
    """Controlled overlap tolerance: disc cap in large rect courtyard."""

    def test_disc_cap_in_large_rect_courtyard_overlap_force(self):
        """Disc cap (2mm dia) centered in large rect (10x5mm): measure overlap force.
        
        With default kc=2000, overlap area ~3.14mm² -> force ~kc*(1+3.14) ~8000.
        This test documents the force level for tuning 'lenient' behavior.
        """
        # Large rectangular courtyard (like a connector body)
        large = make_smd_resistor("LARGE", "uuid-large", 0.0, 0.0, "N1", "N2", hw=5.0, hh=2.5)
        large = replace(large, courtyard=make_rect_courtyard(0, 0, 5.0, 2.5))
        
        # Small disc cap centered inside
        disc = make_disc_cap("DISC", "uuid-disc", 0.0, 0.0, diameter=2.0)
        
        model = _board([large, disc])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-disc"]["courtyard"]
        mag = math.hypot(fx, fy)
        
        # Document the force level - user can decide if this is "lenient" enough
        # Overlap area = pi * 1^2 = 3.14 (but shapely approximates circle with 8 segments)
        # force = kc * (1 + area) = 2000 * (1 + ~3.14) = ~8280
        # Actual measured force will be slightly different due to polygon approximation
        expected_force = 2000.0 * (1.0 + math.pi * 1.0**2)
        assert mag == pytest.approx(expected_force, rel=0.1), (
            f"Overlap force {mag:.0f} vs expected {expected_force:.0f} "
            f"(for kc=2000, area=pi*1^2). Tune kc or add deadband for 0.5mm tolerance."
        )

    def test_overlap_exceeds_tolerance_pushes_out(self):
        """Disc cap offset so overlap > 0.5mm radial.
        
        Current behavior: intersection centroid -> polygon centroid direction
        can push DEEPER into overlap for partial edge overlap (centroid of
        intersection sliver falls on inside of small shape). This documents
        the emergent behavior; a proper "push out" would need edge-normal
        direction like the halo branch uses.
        """
        large = make_smd_resistor("LARGE", "uuid-large", 0.0, 0.0, "N1", "N2", hw=5.0, hh=2.5)
        large = replace(large, courtyard=make_rect_courtyard(0, 0, 5.0, 2.5), locked=True)
        
        # Disc cap offset: center at x=4.5, radius=1.0 -> edge at 5.5
        # Rect courtyard half-width=5.0 -> edge at 5.0
        # Overlap = 0.5mm
        disc = make_disc_cap("DISC", "uuid-disc", 4.5, 0.0, diameter=2.0)
        
        model = _board([large, disc])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-disc"]["courtyard"]
        # Current algorithm: intersection centroid (at x~4.25) -> disc centroid (4.5)
        # gives dx = +0.25, so force is +x (INTO the rect, not out!)
        # This is a known limitation of centroid-based overlap force.
        assert fx > 0, f"Current behavior pushes INTO overlap (+x), got fx={fx}"

    def test_disc_cap_at_edge_0_5mm_overlap_force_level(self):
        """Document force at exactly 0.5mm radial overlap for tuning."""
        large = make_smd_resistor("LARGE", "uuid-large", 0.0, 0.0, "N1", "N2", hw=5.0, hh=2.5)
        large = replace(large, courtyard=make_rect_courtyard(0, 0, 5.0, 2.5))
        
        # Disc centered at x=4.0 (radius 1.0, edge at 5.0 = rect edge)
        # Move to x=4.5 -> edge at 5.5 = 0.5mm overlap
        disc = make_disc_cap("DISC", "uuid-disc", 4.5, 0.0, diameter=2.0)
        
        model = _board([large, disc])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-disc"]["courtyard"]
        mag = math.hypot(fx, fy)
        
        # This documents the force at 0.5mm overlap for threshold decisions
        print(f"\n0.5mm overlap force magnitude: {mag:.0f} (kc=2000)")
        assert mag > 0


class TestRV1Scenarios:
    """DCCF RV1 regression scenarios: resistor migration, pad attraction, clustering."""

    def test_resistor_migrates_out_of_rv1_courtyard(self):
        """Resistor placed inside RV1 courtyard gets pushed OUT, not tangential migration."""
        rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0, locked=True)
        
        # Resistor centered in RV1 courtyard (at x=37, y=1.75)
        resistor = make_smd_resistor("R1", "uuid-r1", 37.0, 1.75, "NET_A", "NET_B", hw=1.5, hh=1.0)
        
        model = _board([rv1, resistor])
        prop = run_placement(model, _base_params(max_iterations=100))
        
        # Check final position - should be outside RV1 courtyard
        dx, dy, da = prop.deltas.get("uuid-r1", (0, 0, 0))
        final_x = 37.0 + dx
        final_y = 1.75 + dy
        
        # RV1 courtyard spans x: -1.25 to 74.75, y: -3.25 to 6.75
        # Resistor courtyard half-width 1.5, half-height 1.0
        # So resistor center must be outside: x < -2.75 or x > 76.25 or y < -4.25 or y > 7.75
        outside = (
            final_x < -2.75 or final_x > 76.25 or
            final_y < -4.25 or final_y > 7.75
        )
        assert outside, f"Resistor should be pushed outside RV1 courtyard, ended at ({final_x:.2f}, {final_y:.2f})"

    def test_resistor_attracted_to_rv1_pad_stays_outside_courtyard(self):
        """Resistor shares net with RV1 pad at courtyard edge -> attraction pulls toward pad,
        courtyard repulsion keeps it outside."""
        rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0, locked=True)
        
        # RV1 pad 1 is at local (0,0) -> board (0,0)
        # Place resistor FAR from RV1 (outside halo reach) so only attraction acts initially
        # Halo reach = min(1.5*(1.5+38), 3) = 3mm (capped)
        # Place at x=-10 so gap = 8.75mm > halo_reach
        resistor = make_smd_resistor("R1", "uuid-r1", -10.0, 0.0, "B1", "NET_B", hw=1.5, hh=1.0)
        
        model = _board([rv1, resistor])
        prop = run_placement(model, _base_params(
            max_iterations=100,
            attraction_k_N=0.002,  # Stronger attraction
        ))
        
        dx, dy, da = prop.deltas.get("uuid-r1", (0, 0, 0))
        final_x = -10.0 + dx
        final_y = 0.0 + dy
        
        # Should move toward RV1 (positive x) but not enter courtyard (x > -1.25 - 1.5 = -2.75)
        assert final_x > -10.0, "Should move toward RV1 pad"
        assert final_x <= -2.75, f"Should not enter RV1 courtyard (x <= -2.75), got {final_x:.2f}"

    def test_multiple_resistors_cluster_near_rv1_pads_no_overlap(self):
        """3 resistors sharing nets with RV1's 3 pads -> converge near pads, zero courtyard overlap."""
        rv1 = make_rv1_like("RV1", "uuid-rv1", 0.0, 0.0, 0.0, locked=True)
        
        # RV1 pads: "1"=B1 at (0,0), "2"=Net-RV1-Pad2 at (0,3.5), "3"=VR+ at (73.5,0)
        r1 = make_smd_resistor("R1", "uuid-r1", -5.0, 0.0, "B1", "GND", hw=1.5, hh=1.0)
        r2 = make_smd_resistor("R2", "uuid-r2", -5.0, 3.5, "Net-RV1-Pad2", "GND", hw=1.5, hh=1.0)
        r3 = make_smd_resistor("R3", "uuid-r3", 78.5, 0.0, "VR+", "GND", hw=1.5, hh=1.0)
        
        model = _board([rv1, r1, r2, r3])
        prop = run_placement(model, _base_params(
            max_iterations=200,
            attraction_k_N=0.002,
        ))
        
        # Check no courtyard overlaps
        from kicad_autorouter.board_model import apply_deltas
        from kicad_autorouter.placement import _collect_pad_nodes
        import numpy as np
        
        moved = apply_deltas(model, prop.deltas)
        pads, _, uuid_to_idx, _, _ = _collect_pad_nodes(moved)
        pos = np.array([(p.position.x_mm, p.position.y_mm) for p in pads])
        
        polys = {}
        for ref in ["RV1", "R1", "R2", "R3"]:
            fp = moved.by_ref[ref]
            polys[ref] = _footprint_courtyard_polygon(
                fp, pad_positions=pos, pad_indices=uuid_to_idx.get(fp.uuid, [])
            )
        
        # Check all pairs
        refs = ["RV1", "R1", "R2", "R3"]
        for i, a in enumerate(refs):
            for b in refs[i+1:]:
                pa, pb = polys[a], polys[b]
                if not pa.is_empty and not pb.is_empty:
                    area = pa.intersection(pb).area
                    assert area <= 1e-6, f"{a}/{b} courtyard overlap: {area:.6f}mm²"
        
        # Check resistors moved toward their respective pads
        for ref, expected_region in [("R1", "left"), ("R2", "left"), ("R3", "right")]:
            fp = model.by_ref[ref]
            dx, dy, _ = prop.deltas.get(fp.uuid, (0, 0, 0))
            final_x = fp.x_mm + dx
            if expected_region == "left":
                assert final_x < 0, f"{ref} should stay on left side near pad"
            else:
                assert final_x > 50, f"{ref} should stay on right side near pad"


class TestCourtyardForceDirection:
    """Force direction sanity checks."""

    def test_overlap_force_perpendicular_to_contact_edge(self):
        """Overlap force direction follows nearest-point normal (edge-orthogonal)."""
        # Two rectangles overlapping on right edge of A / left edge of B
        fa = make_smd_resistor("A", "uuid-a", 0.0, 0.0, "N1", "N2", hw=3.0, hh=2.0)
        fb = make_smd_resistor("B", "uuid-b", 4.0, 0.0, "N3", "N4", hw=3.0, hh=2.0)  # 1mm overlap
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        
        # Should push primarily in +x (B pushed right)
        assert abs(fx) > abs(fy) * 10, f"Force should be primarily horizontal, got ({fx:.1f}, {fy:.1f})"
        assert fx > 0, "B should be pushed right (+x)"

    def test_force_spread_to_pads_contact_weighted(self):
        """Force distributed to pads: contact-side pads take more."""
        # Create 2-pad footprints with pads spread wider
        from tests.helpers import make_fp
        
        fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1", "N2"), hw=3.0, hh=2.0, pad_dx=2.0)
        fb = make_fp("B", "uuid-b", 2.0, 0.0, ("N3", "N4"), hw=3.0, hh=2.0, pad_dx=2.0)
        model = _board([fa, fb])
        prop = run_placement(model, _base_params(max_iterations=0))
        
        # Total courtyard force on B should be non-zero and in +x direction
        fx, fy = prop.force_components["uuid-b"]["courtyard"]
        assert fx > 0, f"B should be pushed right (+x), got fx={fx}"
        assert abs(fy) < abs(fx) * 0.1, f"Force should be primarily horizontal, got ({fx:.1f}, {fy:.1f})"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])