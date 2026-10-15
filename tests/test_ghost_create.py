"""Tests for ghost (pseudo-part) creation: builders + S-expression round-trip.

Markers: unit, physics
"""

from pathlib import Path

import pytest

from kicad_autorouter import board_model as bm
from kicad_autorouter.board_model import board_model
from kicad_autorouter.ghost import make_ghost, make_ghost_attractor, make_ghost_wall
from kicad_autorouter.io import add_ghost_to_tree, footprint_to_sexpr, remove_ghost_from_tree
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file

FIXTURES = Path(__file__).parent / "fixtures"
GHOST_TEST_PCB = FIXTURES / "ghost_test.kicad_pcb"


@pytest.mark.unit
class TestGhostBuilders:
    def test_wall_is_padless_ghost_with_rect_courtyard(self):
        g = make_ghost_wall("GHOST_W1", 10, 10, 20, 5, uuid="ghost-w1")
        assert g.ghost is True
        assert g.pads == ()
        assert g.ghost_attractions == ()
        assert g.uuid == "ghost-w1"
        assert len(g.courtyard) == 4
        xs = [p.x_mm for seg in g.courtyard for p in seg]
        ys = [p.y_mm for seg in g.courtyard for p in seg]
        assert min(xs) == pytest.approx(0.0)
        assert max(xs) == pytest.approx(20.0)
        assert min(ys) == pytest.approx(7.5)
        assert max(ys) == pytest.approx(12.5)

    def test_attractor_carries_target_spring(self):
        g = make_ghost_attractor("GHOST_A1", 50, 20, "U1", 0.1, uuid="ghost-a1")
        assert g.ghost is True
        assert g.ghost_attractions == (("U1", 0.1),)

    def test_attractor_extra_targets(self):
        g = make_ghost_attractor(
            "GHOST_A2", 0, 0, "U1", 0.1,
            extra_attractions=[("R2", 0.2)], uuid="ghost-a2",
        )
        assert g.ghost_attractions == (("U1", 0.1), ("R2", 0.2))

    def test_validation(self):
        with pytest.raises(ValueError):
            make_ghost_wall("", 0, 0, 10, 5)
        with pytest.raises(ValueError):
            make_ghost_wall("G", 0, 0, 0, 5)
        with pytest.raises(ValueError):
            make_ghost_wall("G", 0, 0, 10, -1)
        with pytest.raises(ValueError):
            make_ghost_attractor("G", 0, 0, "U1", -0.1)
        with pytest.raises(ValueError):
            make_ghost_attractor("G", 0, 0, "", 0.1)
        with pytest.raises(ValueError):
            make_ghost("G", 0, 0, 10, 5, attractions=[("U1", "bad")])

    def test_uuid_generated_when_omitted(self):
        a = make_ghost_wall("G1", 0, 0, 10, 5)
        b = make_ghost_wall("G2", 0, 0, 10, 5)
        assert a.uuid and b.uuid and a.uuid != b.uuid


@pytest.mark.unit
class TestGhostSerialization:
    def test_round_trip_wall(self):
        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_wall("GHOST_W1", 10, 10, 20, 5, uuid="ghost-w1")
        tree2 = add_ghost_to_tree(tree, g)
        model = board_model(tree2)
        back = next(f for f in model.ghost_footprints if f.uuid == "ghost-w1")
        assert back.ref == "GHOST_W1"
        assert back.x_mm == pytest.approx(10.0)
        assert len(back.courtyard) == 4
        # Ghosts stay out of the real-footprint ref index.
        assert "GHOST_W1" not in model.by_ref

    def test_round_trip_attractor(self):
        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_attractor("GHOST_A1", 50, 30, "U1", 0.2, uuid="ghost-a1")
        model = board_model(add_ghost_to_tree(tree, g))
        back = next(f for f in model.ghost_footprints if f.uuid == "ghost-a1")
        assert back.ghost_attractions == (("U1", 0.2),)

    def test_fab_courtyard_layer_still_parses(self):
        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_wall("GHOST_FAB", 10, 10, 20, 5, uuid="ghost-fab")
        model = board_model(add_ghost_to_tree(tree, g, courtyard_layer="F.Fab"))
        back = next(f for f in model.ghost_footprints if f.uuid == "ghost-fab")
        assert len(back.courtyard) == 4

    def test_footprint_to_sexpr_emits_ghost_markers(self):
        g = make_ghost_attractor("GHOST_A1", 50, 30, "U1", 0.2, uuid="ghost-a1")
        node = footprint_to_sexpr(g, "Ghost:Attractor")
        props = {p.args[0]: p.args[1] for p in node.children("property")}
        assert props["ghost"] == "yes"
        assert props["ghost_attract_1"] == "U1,0.2"

    def test_add_rejects_non_ghost(self):
        from .helpers import make_fp

        tree = parse_file(GHOST_TEST_PCB)
        with pytest.raises(ValueError):
            add_ghost_to_tree(tree, make_fp("R9", "uuid-r9", 0, 0))

    def test_remove_ghost(self):
        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_wall("GHOST_W1", 10, 10, 20, 5, uuid="ghost-w1")
        tree2 = add_ghost_to_tree(tree, g)
        tree3 = remove_ghost_from_tree(tree2, "ghost-w1")
        model = board_model(tree3)
        assert not [f for f in model.ghost_footprints if f.uuid == "ghost-w1"]

    def test_remove_refuses_real_footprint(self):
        tree = parse_file(GHOST_TEST_PCB)
        with pytest.raises(KeyError):
            remove_ghost_from_tree(tree, "aaaaaaaa-1111-2222-3333-444444444444")
        with pytest.raises(KeyError):
            remove_ghost_from_tree(tree, "no-such-uuid")


@pytest.mark.unit
class TestGhostResize:
    def _extents(self, model, uuid):
        g = next(f for f in model.ghost_footprints if f.uuid == uuid)
        xs = [p.x_mm for seg in g.courtyard for p in seg]
        ys = [p.y_mm for seg in g.courtyard for p in seg]
        return g, max(xs) - min(xs), max(ys) - min(ys)

    def test_resize_changes_extents_in_place(self):
        from kicad_autorouter.io import resize_ghost_in_tree

        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_wall("GHOST_W1", 10, 10, 20, 5, uuid="ghost-w1")
        tree2 = add_ghost_to_tree(tree, g)
        model = board_model(resize_ghost_in_tree(tree2, "ghost-w1", 30, 8))
        back, w, h = self._extents(model, "ghost-w1")
        assert (w, h) == (pytest.approx(30.0), pytest.approx(8.0))
        assert (back.x_mm, back.y_mm) == (pytest.approx(10.0), pytest.approx(10.0))
        assert back.ref == "GHOST_W1" and back.ghost is True

    def test_resize_preserves_attractions_and_layer(self):
        from kicad_autorouter.io import resize_ghost_in_tree

        tree = parse_file(GHOST_TEST_PCB)
        g = make_ghost_attractor("GHOST_A1", 50, 30, "U1", 0.2, uuid="ghost-a1")
        tree2 = add_ghost_to_tree(tree, g, courtyard_layer="F.Fab")
        model = board_model(resize_ghost_in_tree(tree2, "ghost-a1", 10, 6))
        back, w, h = self._extents(model, "ghost-a1")
        assert (w, h) == (pytest.approx(10.0), pytest.approx(6.0))
        assert back.ghost_attractions == (("U1", 0.2),)

    def test_resize_refuses_real_and_bad_sizes(self):
        from kicad_autorouter.io import resize_ghost_in_tree

        tree = parse_file(GHOST_TEST_PCB)
        with pytest.raises(KeyError):
            resize_ghost_in_tree(
                tree, "aaaaaaaa-1111-2222-3333-444444444444", 5, 5)
        with pytest.raises(ValueError):
            resize_ghost_in_tree(tree, "ghost-rail-top-001", 0, 5)
        with pytest.raises(ValueError):
            resize_ghost_in_tree(tree, "ghost-rail-top-001", 5, -1)


@pytest.mark.physics
class TestCreatedGhostPhysics:
    def test_created_wall_repels(self):
        model = board_model(parse_file(GHOST_TEST_PCB))
        wall = make_ghost_wall("GHOST_WALL", 50, 42, 60, 4, uuid="ghost-wall-test")
        model2 = bm.replace(model, ghost_footprints=model.ghost_footprints + (wall,))
        p = PlacementParams(
            max_iterations=30, wall_k_N=0.005,
            wall_halo_m=0.04, repulsion_k_N_m2=1e-08,
        )
        prop = run_placement(model2, p)
        assert "ghost-wall-test" not in prop.deltas
        # Wall at y=42 sits just below U1/R2 (y=50): both pushed down.
        for uuid, (dx, dy, da) in prop.deltas.items():
            assert dy > 0, f"{uuid} should move down, got dy={dy}"

    def test_created_attractor_pulls_target(self):
        model = board_model(parse_file(GHOST_TEST_PCB))
        attr = make_ghost_attractor(
            "GHOST_PULL", 50, 20, "R2", 0.5, uuid="ghost-pull-test")
        model2 = bm.replace(model, ghost_footprints=model.ghost_footprints + (attr,))
        p = PlacementParams(
            max_iterations=30, wall_k_N=0.0, repulsion_k_N_m2=1e-08)
        prop = run_placement(model2, p)
        r2 = "bbbbbbbb-1111-2222-3333-444444444444"
        assert r2 in prop.deltas
        assert prop.deltas[r2][1] < 0, "R2 should move up toward attractor"
