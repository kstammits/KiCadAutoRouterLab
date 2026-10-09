"""Magnet test: verify placement organizes components by shared net.

This test creates 7 resistors with 2 nets each (shared + unique), randomly
scattered across the board. The shared nets (NET_A, NET_B, NET_C) should
magnetically attract their associated components into clusters.

Run with: python -m pytest tests/test_magnet.py -v -s
"""

import random
import math
import os
import uuid
from dataclasses import dataclass, replace
from typing import List, Tuple
from pathlib import Path

import pytest
import numpy as np

from kicad_autorouter.sexpr import parse_file, SExpr, to_sexpr
from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Footprint,
    Pad,
    Point,
    apply_deltas,
    commit_placement,
)
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.svg_render import render_board_svg


# ---------------------------------------------------------------------------
# Magnet test parameters (tune these for desired behavior)
# ---------------------------------------------------------------------------
MAGNET_PARAMS = PlacementParams(
    max_iterations=200,
    repulsion_kr=2.0,
    attraction_ka=2.0,
    ideal_length_mm=2.0,
    courtyard_repulsion_kc=30000.0,
    # 3x2mm parts: 6mm halo preserves this test's original ~5.4mm
    # (1.5x radii sum) near-miss reach under the bounded-halo regime.
    courtyard_halo_mm=6.0,
    boundary_repulsion_kb=500000.0,
    convergence_eps_mm=0.1,
    rigid_stiffness=5e5,
)


# ---------------------------------------------------------------------------
# Board constants
# ---------------------------------------------------------------------------
BOARD_SIZE_MM = 100.0
MARGIN_MM = 5.0
COURTYARD_HALF_W = 1.5
COURTYARD_HALF_H = 1.0
PAD_OFFSET = 1.0
PAD_SIZE = (1.0, 1.0)


def _make_pad(number: str, net_name: str, local_x: float, local_y: float) -> Pad:
    """Create a pad in local coordinates (will be transformed to board coords)."""
    return Pad(
        number=number,
        net_name=net_name,
        position=Point(x_mm=local_x, y_mm=local_y),
        size_mm=PAD_SIZE,
        shape="rect",
        angle_deg=0.0,
        pad_type="smd",
        drill_mm=0.0,
        layers=("F.Cu", "F.Paste", "F.Mask"),
    )


def _make_courtyard() -> Tuple[Tuple[Point, Point], ...]:
    """Return courtyard segments for 3x2mm rectangle centered at origin."""
    hw, hh = COURTYARD_HALF_W, COURTYARD_HALF_H
    corners = [
        Point(-hw, -hh), Point(hw, -hh),
        Point(hw, hh), Point(-hw, hh),
        Point(-hw, -hh),
    ]
    return tuple((corners[i], corners[i + 1]) for i in range(4))


def _create_resistor(
    ref: str,
    uuid: str,
    shared_net: str,
    unique_net: str,
    x_mm: float,
    y_mm: float,
) -> Footprint:
    """Create a 2-pad 0805-style resistor footprint at board position (x_mm, y_mm)."""
    pad1 = _make_pad("1", shared_net, -PAD_OFFSET, 0.0)
    pad2 = _make_pad("2", unique_net, PAD_OFFSET, 0.0)

    def to_board(px: float, py: float) -> Point:
        return Point(x_mm + px, y_mm + py)

    pads = (
        replace(pad1, position=to_board(pad1.position.x_mm, pad1.position.y_mm)),
        replace(pad2, position=to_board(pad2.position.x_mm, pad2.position.y_mm)),
    )

    courtyard = _make_courtyard()

    return Footprint(
        ref=ref,
        footprint_id="Test:R_0805_Magnet",
        layer="F.Cu",
        x_mm=x_mm,
        y_mm=y_mm,
        angle_deg=0.0,
        pads=pads,
        courtyard=courtyard,
        locked=False,
        uuid=uuid,
        ghost=False,
        ghost_attractions=(),
    )


def _random_position(rng: random.Random) -> Tuple[float, float]:
    """Generate random position within board margins."""
    return (
        rng.uniform(MARGIN_MM, BOARD_SIZE_MM - MARGIN_MM),
        rng.uniform(MARGIN_MM, BOARD_SIZE_MM - MARGIN_MM),
    )


def _get_rng() -> random.Random:
    """Get RNG for test fixture. Uses fixed seed for CI, true random if env var set."""
    if os.environ.get("MAGNET_TRUE_RANDOM") == "1":
        return random.Random()
    return random.Random(42)


# ---------------------------------------------------------------------------
# Fixture: builds the magnet test board programmatically
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def magnet_model() -> BoardModel:
    """Build board with minimal fixture + 7 magnet test resistors."""
    FIXTURES = Path("tests/fixtures")
    minimal_model = __import__("kicad_autorouter.board_model", fromlist=["board_model"]).board_model(
        parse_file(FIXTURES / "minimal.kicad_pcb")
    )

    locked_footprints = tuple(
        replace(fp, locked=True) for fp in minimal_model.footprints
    )

    rng = _get_rng()

    resistor_specs = [
        ("R1", "mag-r1-uuid-0001", "NET_A", "NET_D"),
        ("R2", "mag-r2-uuid-0002", "NET_A", "NET_E"),
        ("R3", "mag-r3-uuid-0003", "NET_A", "NET_F"),
        ("R4", "mag-r4-uuid-0004", "NET_B", "NET_G"),
        ("R5", "mag-r5-uuid-0005", "NET_B", "NET_H"),
        ("R6", "mag-r6-uuid-0006", "NET_C", "NET_I"),
        ("R7", "mag-r7-uuid-0007", "NET_C", "NET_J"),
    ]

    test_resistors = []
    for ref, uuid_str, shared_net, unique_net in resistor_specs:
        x, y = _random_position(rng)
        test_resistors.append(_create_resistor(ref, uuid_str, shared_net, unique_net, x, y))

    all_footprints = locked_footprints + tuple(test_resistors)

    footprint_region = {}
    for fp in all_footprints:
        if fp.uuid:
            footprint_region[fp.uuid] = 0

    by_ref = {}
    for fp in all_footprints:
        if fp.ref and fp.ref not in by_ref:
            by_ref[fp.ref] = fp

    board_regions = minimal_model.board_regions

    return BoardModel(
        footprints=all_footprints,
        board_regions=board_regions,
        footprint_region=footprint_region,
        by_ref=by_ref,
        nets=minimal_model.nets,
        zones=minimal_model.zones,
        tracks=minimal_model.tracks,
        vias=minimal_model.vias,
        keepout_zones=minimal_model.keepout_zones,
        ghost_footprints=(),
        edge_cuts=minimal_model.edge_cuts,
        edge_arcs=minimal_model.edge_arcs,
        version=minimal_model.version,
        version_warning=minimal_model.version_warning,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _centroid(fp: Footprint, prop) -> Tuple[float, float]:
    """Compute final centroid from proposal deltas."""
    dx, dy, da = prop.deltas.get(fp.uuid, (0.0, 0.0, 0.0))
    return (fp.x_mm + dx, fp.y_mm + dy)


def _group_spread(centroids: dict, refs: List[str]) -> float:
    """Compute Y-spread (max - min) for a group of references."""
    ys = [centroids[r][1] for r in refs]
    return max(ys) - min(ys)


def _initial_spread(model: BoardModel, refs: List[str]) -> float:
    """Compute initial Y-spread for a group."""
    ys = [model.by_ref[r].y_mm for r in refs]
    return max(ys) - min(ys)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestNetMagnet:
    """Test that shared nets magnetically attract their components."""

    def test_nets_magnetically_attract(self, magnet_model: BoardModel):
        """Shared nets (NET_A/B/C) should pull their components together."""
        prop = run_placement(magnet_model, MAGNET_PARAMS)

        centroids = {
            ref: _centroid(magnet_model.by_ref[ref], prop)
            for ref in ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
        }

        groups = {
            "A": ["R1", "R2", "R3"],
            "B": ["R4", "R5"],
            "C": ["R6", "R7"],
        }

        intra_dists = {}
        for name, refs in groups.items():
            dists = []
            for i, r1 in enumerate(refs):
                for r2 in refs[i+1:]:
                    c1, c2 = centroids[r1], centroids[r2]
                    d = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
                    dists.append(d)
            intra_dists[name] = sum(dists) / len(dists) if dists else 0.0

        cross_dists = []
        all_refs = ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
        for i, r1 in enumerate(all_refs):
            for r2 in all_refs[i+1:]:
                g1 = next(g for g, rs in groups.items() if r1 in rs)
                g2 = next(g for g, rs in groups.items() if r2 in rs)
                if g1 != g2:
                    c1, c2 = centroids[r1], centroids[r2]
                    d = math.hypot(c1[0] - c2[0], c1[1] - c2[1])
                    cross_dists.append(d)

        avg_intra = sum(intra_dists.values()) / len(intra_dists)
        avg_cross = sum(cross_dists) / len(cross_dists)

        print(f"\n=== Magnet Test Results ===")
        for name, d in intra_dists.items():
            print(f"  Group {name} avg intra-dist: {d:.1f}mm")
        print(f"  Average intra-group distance: {avg_intra:.1f}mm")
        print(f"  Average cross-group distance: {avg_cross:.1f}mm")
        print(f"  Ratio (intra/cross): {avg_intra/avg_cross:.2f}")
        print(f"  Params: kr={MAGNET_PARAMS.repulsion_kr}, ka={MAGNET_PARAMS.attraction_ka}, "
              f"ideal={MAGNET_PARAMS.ideal_length_mm}, iter={MAGNET_PARAMS.max_iterations}")

        assert avg_intra < avg_cross * 0.85, (
            f"Intra-group distance ({avg_intra:.1f}mm) not sufficiently smaller than "
            f"cross-group ({avg_cross:.1f}mm). Ratio: {avg_intra/avg_cross:.2f}"
        )

    def test_locked_parts_dont_move(self, magnet_model: BoardModel):
        """Locked footprints from minimal fixture should remain fixed."""
        prop = run_placement(magnet_model, MAGNET_PARAMS)

        for fp in magnet_model.footprints:
            if fp.locked and fp.uuid in prop.deltas:
                dx, dy, da = prop.deltas[fp.uuid]
                assert abs(dx) < 0.01 and abs(dy) < 0.01, (
                    f"Locked footprint {fp.ref} ({fp.uuid}) moved: dx={dx:.3f}, dy={dy:.3f}"
                )

    def test_all_test_resistors_moved(self, magnet_model: BoardModel):
        """All 7 test resistors should have non-zero deltas."""
        prop = run_placement(magnet_model, MAGNET_PARAMS)

        for ref in ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]:
            fp = magnet_model.by_ref[ref]
            assert fp.uuid in prop.deltas, f"{ref} has no delta"
            dx, dy, da = prop.deltas[fp.uuid]
            displacement = math.hypot(dx, dy)
            assert displacement > 0.1, f"{ref} barely moved: {displacement:.3f}mm"

    def test_boundary_repulsion_keeps_within_outline(self, magnet_model: BoardModel):
        """With high repulsion, all components should stay within board boundaries."""
        boundary_params = PlacementParams(
            max_iterations=200,
            repulsion_kr=50.0,
            attraction_ka=0.5,
            ideal_length_mm=5.0,
            courtyard_repulsion_kc=20000.0,
            boundary_repulsion_kb=500000.0,
            convergence_eps_mm=0.1,
            rigid_stiffness=5e5,
        )
        prop = run_placement(magnet_model, boundary_params)

        region = magnet_model.board_regions[0]
        min_x, max_x, min_y, max_y = region.bbox
        margin = COURTYARD_HALF_W + 0.5

        for ref in ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]:
            fp = magnet_model.by_ref[ref]
            dx, dy, da = prop.deltas.get(fp.uuid, (0.0, 0.0, 0.0))
            final_x = fp.x_mm + dx
            final_y = fp.y_mm + dy

            assert min_x + margin <= final_x <= max_x - margin, (
                f"{ref} x={final_x:.2f} outside board bounds [{min_x+margin:.1f}, {max_x-margin:.1f}]"
            )
            assert min_y + margin <= final_y <= max_y - margin, (
                f"{ref} y={final_y:.2f} outside board bounds [{min_y+margin:.1f}, {max_y-margin:.1f}]"
            )

        for fp in magnet_model.footprints:
            if fp.locked and fp.uuid in prop.deltas:
                dx, dy, da = prop.deltas[fp.uuid]
                assert abs(dx) < 0.01 and abs(dy) < 0.01, (
                    f"Locked footprint {fp.ref} moved: dx={dx:.3f}, dy={dy:.3f}"
                )

    def test_write_svg_output(self, magnet_model: BoardModel):
        """Write final layout SVG to ./tmp/ for visual verification."""
        prop = run_placement(magnet_model, MAGNET_PARAMS)

        tmp_dir = Path("/Users/karl/Workspace/KiCadAutoRouterLab/tmp")
        tmp_dir.mkdir(exist_ok=True)

        svg_path = tmp_dir / "magnet_final_layout.svg"
        svg_content = render_board_svg(
            model=magnet_model,
            title="Magnet Test - Final Layout",
            proposal=prop,
            pinned_uuids=set(),
            selected_uuids=set(),
            show_forces=False,
        )
        svg_path.write_text(svg_content)
        print(f"\n=== Output Written ===")
        print(f"SVG: {svg_path}")

        assert svg_path.exists() and svg_path.stat().st_size > 1000


@pytest.mark.regression
class TestMovePreservation:
    """A moved part must keep its pad/footprint/board properties.

    Regression for apply_deltas()/commit_placement() rebuilding Pad /
    Footprint / BoardModel with a subset of fields (dropping pad_type,
    drill_mm, layers, ghost, ghost_attractions, version, regions, ...).
    """

    def _enriched_model(self, magnet_model: BoardModel) -> BoardModel:
        """Return magnet_model with R1 enriched as THT + ghost attractions."""
        orig = magnet_model.by_ref["R1"]
        tht_pads = tuple(
            replace(
                p,
                pad_type="thru_hole",
                drill_mm=0.8,
                layers=("F.Cu", "B.Cu", "*.Cu"),
            )
            for p in orig.pads
        )
        enriched = replace(
            orig,
            pads=tht_pads,
            ghost_attractions=(("R2", 25.0, 0.1),),
        )
        footprints = tuple(
            enriched if fp.uuid == orig.uuid else fp
            for fp in magnet_model.footprints
        )
        by_ref = dict(magnet_model.by_ref)
        by_ref["R1"] = enriched
        ghost = _create_resistor("GHOST1", "ghost-uuid-0001", "NET_A", "NET_B", 10.0, 10.0)
        ghost = replace(ghost, ghost=True)
        return replace(
            magnet_model,
            footprints=footprints,
            by_ref=by_ref,
            ghost_footprints=(ghost,),
            version="10.0",
        )

    def test_apply_deltas_preserves_moved_part_properties(
        self, magnet_model: BoardModel
    ):
        model = self._enriched_model(magnet_model)
        before = model.by_ref["R1"]
        assert before.pads[0].is_through_hole

        moved = apply_deltas(model, {before.uuid: (5.0, -3.0, 10.0)})
        after = moved.by_ref["R1"]

        # Pose actually changed.
        assert (after.x_mm, after.y_mm, after.angle_deg) == pytest.approx(
            (before.x_mm + 5.0, before.y_mm - 3.0, before.angle_deg + 10.0)
        )
        # Pad identity preserved (this is the serious THT free-via bug).
        assert len(after.pads) == len(before.pads)
        for pad_before, pad_after in zip(before.pads, after.pads):
            assert pad_after.number == pad_before.number
            assert pad_after.net_name == pad_before.net_name
            assert pad_after.pad_type == pad_before.pad_type
            assert pad_after.drill_mm == pytest.approx(pad_before.drill_mm)
            assert tuple(pad_after.layers) == tuple(pad_before.layers)
            assert pad_after.size_mm == pad_before.size_mm
            assert pad_after.shape == pad_before.shape
            assert pad_after.angle_deg == pytest.approx(pad_before.angle_deg)
        assert after.pads[0].is_through_hole
        # Footprint identity preserved.
        assert after.ref == before.ref
        assert after.footprint_id == before.footprint_id
        assert after.layer == before.layer
        assert after.locked == before.locked
        assert after.uuid == before.uuid
        assert after.ghost == before.ghost
        assert tuple(after.ghost_attractions) == tuple(before.ghost_attractions)
        # Board metadata preserved.
        assert moved.version == model.version
        assert moved.version_warning == model.version_warning
        assert moved.board_regions == model.board_regions
        assert moved.footprint_region == model.footprint_region
        assert moved.ghost_footprints == model.ghost_footprints
        assert moved.keepout_zones == model.keepout_zones
        assert moved.nets == model.nets

    def test_commit_placement_preserves_properties(
        self, magnet_model: BoardModel
    ):
        model = self._enriched_model(magnet_model)
        before = model.by_ref["R1"]

        moved = commit_placement(
            model, {before.uuid: (2.0, 1.0, 0.0)}, protected_nets=set()
        )
        after = moved.by_ref["R1"]

        assert after.pads[0].pad_type == "thru_hole"
        assert after.pads[0].drill_mm == pytest.approx(0.8)
        assert tuple(after.pads[0].layers) == ("F.Cu", "B.Cu", "*.Cu")
        assert tuple(after.ghost_attractions) == (("R2", 25.0, 0.1),)
        assert moved.version == model.version
        assert moved.ghost_footprints == model.ghost_footprints
        assert moved.board_regions == model.board_regions
        assert moved.footprint_region == model.footprint_region

    def test_locked_part_ignores_delta(self, magnet_model: BoardModel):
        locked = next(fp for fp in magnet_model.footprints if fp.locked)
        moved = apply_deltas(model=magnet_model, deltas={locked.uuid: (5.0, 5.0, 45.0)})
        after = moved.by_ref.get(locked.ref, None)
        # First-wins by_ref may point at another fp with same ref; fall back to uuid lookup.
        if after is None or after.uuid != locked.uuid:
            after = next(fp for fp in moved.footprints if fp.uuid == locked.uuid)
        assert (after.x_mm, after.y_mm, after.angle_deg) == pytest.approx(
            (locked.x_mm, locked.y_mm, locked.angle_deg)
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
