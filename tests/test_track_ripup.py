"""Tests for track rip-up functionality: commit_placement and rip_up_nets."""

from pathlib import Path

import pytest

from kicad_autorouter.board_model import BoardModel, Footprint, Pad, Point, board_model, commit_placement
from kicad_autorouter.board_model import _net_name as _net_name_from_node
from kicad_autorouter.io import copper_counts_by_net, rip_up_nets
from kicad_autorouter.sexpr import SExpr, parse_file

FIXTURES = Path(__file__).parent / "fixtures"
TUBE111_PCB = FIXTURES / "tube111.kicad_pcb"


@pytest.fixture(scope="module")
def tube111_tree():
    return parse_file(TUBE111_PCB)


@pytest.fixture(scope="module")
def tube111_model(tube111_tree):
    return board_model(tube111_tree)


class TestCommitPlacementCore:
    """Core commit_placement tests."""

    def test_commit_placement_rips_tracks_on_affected_nets(self, tube111_model):
        """Moving a footprint should rip up tracks on ALL its nets."""
        # Find a footprint with a net that has tracks/vias
        # C1 has GND and +12V - both have copper
        fp = next((fp for fp in tube111_model.footprints if fp.ref == "C1"), None)
        assert fp is not None, "C1 footprint not found"
        # C1 has pads on GND and +12V
        pad_nets = {p.net_name for p in fp.pads if p.net_name}
        assert "GND" in pad_nets
        assert "+12V" in pad_nets

        initial_track_count = len(tube111_model.tracks)
        initial_via_count = len(tube111_model.vias)

        # Move the footprint WITHOUT protecting any nets
        deltas = {fp.uuid: (1.0, 1.0, 0.0)}
        result = commit_placement(tube111_model, deltas, set())

        # Both nets' tracks/vias should be removed
        for net_name in pad_nets:
            vias_before = len([v for v in tube111_model.vias if v.net_name == net_name])
            tracks_before = len([t for t in tube111_model.tracks if t.net_name == net_name])
            if vias_before > 0 or tracks_before > 0:
                vias_after = len([v for v in result.vias if v.net_name == net_name])
                tracks_after = len([t for t in result.tracks if t.net_name == net_name])
                assert vias_after < vias_before or tracks_after < tracks_before, \
                    f"Expected {net_name} copper to be ripped"

        # Now test WITH protecting one net
        result2 = commit_placement(tube111_model, deltas, {"GND"})
        # GND should be preserved
        gnd_vias_before = len([v for v in tube111_model.vias if v.net_name == "GND"])
        gnd_tracks_before = len([t for t in tube111_model.tracks if t.net_name == "GND"])
        gnd_vias_after2 = len([v for v in result2.vias if v.net_name == "GND"])
        gnd_tracks_after2 = len([t for t in result2.tracks if t.net_name == "GND"])
        assert gnd_vias_after2 == gnd_vias_before
        assert gnd_tracks_after2 == gnd_tracks_before
        # But +12V should still be ripped
        other_vias_before = len([v for v in tube111_model.vias if v.net_name == "+12V"])
        other_tracks_before = len([t for t in tube111_model.tracks if t.net_name == "+12V"])
        other_vias_after2 = len([v for v in result2.vias if v.net_name == "+12V"])
        other_tracks_after2 = len([t for t in result2.tracks if t.net_name == "+12V"])
        assert other_vias_after2 < other_vias_before or other_tracks_after2 < other_tracks_before

    def test_commit_placement_preserves_protected_nets(self, tube111_model):
        """Protected nets should never have tracks/vias ripped up."""
        # Find a footprint on a non-protected net (not GND, +12V, -12V)
        fp = next((fp for fp in tube111_model.footprints
                   if fp.pads and fp.pads[0].net_name not in ("GND", "+12V", "-12V")), None)
        assert fp is not None, "Expected a footprint on non-protected net"

        net_name = fp.pads[0].net_name
        initial_via_count = len([v for v in tube111_model.vias if v.net_name == net_name])
        initial_track_count = len([t for t in tube111_model.tracks if t.net_name == net_name])

        # Move footprint with its net protected
        deltas = {fp.uuid: (2.0, 0.0, 0.0)}
        protected = {net_name}
        result = commit_placement(tube111_model, deltas, protected)

        # Vias on protected net should be preserved
        preserved_vias = [v for v in result.vias if v.net_name == net_name]
        preserved_tracks = [t for t in result.tracks if t.net_name == net_name]
        assert len(preserved_vias) == initial_via_count
        assert len(preserved_tracks) == initial_track_count


class TestRipUpNetsSexpr:
    """S-expression rip_up_nets tests."""

    def test_rip_up_nets_removes_segments_and_vias(self, tube111_tree):
        """rip_up_nets should remove segments and vias on affected nets."""
        # Count initial vias on GND
        gnd_vias_before = sum(1 for v in tube111_tree.children("via")
                              if _net_name_from_node(v.find("net")) == "GND")
        assert gnd_vias_before > 0

        # Rip up GND net
        result = rip_up_nets(tube111_tree, {"GND"}, set())

        # GND vias should be removed
        gnd_vias_after = sum(1 for v in result.children("via")
                             if _net_name_from_node(v.find("net")) == "GND")
        assert gnd_vias_after == 0

        # Other nets' vias should remain
        other_vias = sum(1 for v in result.children("via")
                         if _net_name_from_node(v.find("net")) not in (None, "GND"))
        other_vias_before = sum(1 for v in tube111_tree.children("via")
                                if _net_name_from_node(v.find("net")) not in (None, "GND"))
        assert other_vias == other_vias_before

    def test_rip_up_nets_preserves_protected(self, tube111_tree):
        """Protected nets should be preserved even if in affected_nets."""
        gnd_vias_before = sum(1 for v in tube111_tree.children("via")
                              if _net_name_from_node(v.find("net")) == "GND")
        assert gnd_vias_before > 0

        # Rip up GND but protect it
        result = rip_up_nets(tube111_tree, {"GND"}, {"GND"})

        gnd_vias_after = sum(1 for v in result.children("via")
                             if _net_name_from_node(v.find("net")) == "GND")
        assert gnd_vias_after == gnd_vias_before

    def test_rip_up_nets_indexed_net_form(self, tube111_tree):
        """rip_up_nets must handle (net <idx> NAME) segment/via form (KiCad 6+)."""
        assert _net_name_from_node(SExpr("net", (5, "GND"))) == "GND"
        assert _net_name_from_node(SExpr("net", ("GND",))) == "GND"
        assert _net_name_from_node(SExpr("net", (0,))) is None
        assert _net_name_from_node(SExpr("net", (0, ""))) is None

        seg = SExpr(
            "segment",
            (
                SExpr("start", (0.0, 0.0)),
                SExpr("end", (1.0, 1.0)),
                SExpr("net", (5, "GND")),
            ),
        )
        via = SExpr(
            "via",
            (
                SExpr("at", (0.0, 0.0)),
                SExpr("net", (7, "SIG")),
            ),
        )
        tree = SExpr("kicad_pcb", (seg, via))
        assert copper_counts_by_net(tree) == {
            "GND": {"tracks": 1, "vias": 0},
            "SIG": {"tracks": 0, "vias": 1},
        }
        result = rip_up_nets(tree, {"GND"}, set())
        assert list(result.children("segment")) == []
        assert len(list(result.children("via"))) == 1
        # Protected indexed net is preserved
        result2 = rip_up_nets(tree, {"GND"}, {"GND"})
        assert len(list(result2.children("segment"))) == 1


class TestPCBTreeVsBoardModelSync:
    """Verify commit_placement and rip_up_nets produce identical track sets."""

    def test_commit_placement_and_rip_up_nets_match(self, tube111_model, tube111_tree):
        """commit_placement (BoardModel) and rip_up_nets (SExpr) should remove same copper."""
        # Find a footprint on a net with copper
        fp = next((fp for fp in tube111_model.footprints if fp.ref == "C1"), None)
        assert fp is not None, "C1 footprint not found"
        pad_nets = {p.net_name for p in fp.pads if p.net_name}

        deltas = {fp.uuid: (1.0, 1.0, 0.0)}
        protected = set()

        # Apply commit_placement to BoardModel
        board_result = commit_placement(tube111_model, deltas, protected)

        # Apply rip_up_nets to S-expression tree
        # Need to get affected nets the same way
        affected_nets = set()
        for footprint in tube111_model.footprints:
            if footprint.uuid in deltas and not footprint.locked:
                dx, dy, da = deltas[footprint.uuid]
                if dx != 0.0 or dy != 0.0 or da != 0.0:
                    for pad in footprint.pads:
                        if pad.net_name:
                            affected_nets.add(pad.net_name)

        tree_result = rip_up_nets(tube111_tree, affected_nets, protected)

        # Count tracks/vias per net in both results
        for net_name in affected_nets:
            # BoardModel results
            board_tracks = [t for t in board_result.tracks if t.net_name == net_name]
            board_vias = [v for v in board_result.vias if v.net_name == net_name]

            # S-expression results
            tree_tracks = [s for s in tree_result.children("segment")
                           if _net_name_from_node(s.find("net")) == net_name]
            tree_vias = [v for v in tree_result.children("via")
                         if _net_name_from_node(v.find("net")) == net_name]

            # Both should have removed the same copper
            assert len(board_tracks) == len(tree_tracks), \
                f"Track count mismatch for {net_name}: BoardModel={len(board_tracks)}, SExpr={len(tree_tracks)}"
            assert len(board_vias) == len(tree_vias), \
                f"Via count mismatch for {net_name}: BoardModel={len(board_vias)}, SExpr={len(tree_vias)}"

        # Also verify protected nets preserved in both
        for net_name in protected:
            board_tracks = [t for t in board_result.tracks if t.net_name == net_name]
            board_vias = [v for v in board_result.vias if v.net_name == net_name]

            tree_tracks = [s for s in tree_result.children("segment")
                           if _net_name_from_node(s.find("net")) == net_name]
            tree_vias = [v for v in tree_result.children("via")
                         if _net_name_from_node(v.find("net")) == net_name]

            # Should have ALL original copper for protected nets
            orig_board_tracks = [t for t in tube111_model.tracks if t.net_name == net_name]
            orig_tree_tracks = [s for s in tube111_tree.children("segment")
                                if _net_name_from_node(s.find("net")) == net_name]
            assert len(board_tracks) == len(orig_board_tracks)
            assert len(tree_tracks) == len(orig_tree_tracks)

    def test_footprint_on_multiple_nets_some_protected(self, tube111_model, tube111_tree):
        """Footprint on multiple nets (some protected, some not) - both handled correctly."""
        # C1 is on GND and +12V
        fp = next((fp for fp in tube111_model.footprints if fp.ref == "C1"), None)
        assert fp is not None
        pad_nets = {p.net_name for p in fp.pads if p.net_name}
        assert "GND" in pad_nets
        assert "+12V" in pad_nets

        deltas = {fp.uuid: (1.0, 1.0, 0.0)}
        # Protect only GND
        protected = {"GND"}

        # Apply commit_placement
        board_result = commit_placement(tube111_model, deltas, protected)

        # Apply rip_up_nets
        affected_nets = set()
        for footprint in tube111_model.footprints:
            if footprint.uuid in deltas and not footprint.locked:
                dx, dy, da = deltas[footprint.uuid]
                if dx != 0.0 or dy != 0.0 or da != 0.0:
                    for pad in footprint.pads:
                        if pad.net_name:
                            affected_nets.add(pad.net_name)

        tree_result = rip_up_nets(tube111_tree, affected_nets, protected)

        # Protected net (GND) should be preserved in both
        for net_name in protected:
            board_tracks = [t for t in board_result.tracks if t.net_name == net_name]
            board_vias = [v for v in board_result.vias if v.net_name == net_name]
            orig_board_tracks = [t for t in tube111_model.tracks if t.net_name == net_name]
            orig_board_vias = [v for v in tube111_model.vias if v.net_name == net_name]
            assert len(board_tracks) == len(orig_board_tracks), f"Protected net {net_name} tracks not preserved in BoardModel"
            assert len(board_vias) == len(orig_board_vias), f"Protected net {net_name} vias not preserved in BoardModel"

            tree_tracks = [s for s in tree_result.children("segment")
                           if _net_name_from_node(s.find("net")) == net_name]
            tree_vias = [v for v in tree_result.children("via")
                         if _net_name_from_node(v.find("net")) == net_name]
            orig_tree_tracks = [s for s in tube111_tree.children("segment")
                                if _net_name_from_node(s.find("net")) == net_name]
            orig_tree_vias = [v for v in tube111_tree.children("via")
                              if _net_name_from_node(v.find("net")) == net_name]
            assert len(tree_tracks) == len(orig_tree_tracks), f"Protected net {net_name} tracks not preserved in SExpr"
            assert len(tree_vias) == len(orig_tree_vias), f"Protected net {net_name} vias not preserved in SExpr"

        # Unprotected net (+12V) should be ripped in both
        for net_name in pad_nets - protected:
            board_tracks = [t for t in board_result.tracks if t.net_name == net_name]
            board_vias = [v for v in board_result.vias if v.net_name == net_name]

            tree_tracks = [s for s in tree_result.children("segment")
                           if _net_name_from_node(s.find("net")) == net_name]
            tree_vias = [v for v in tree_result.children("via")
                         if _net_name_from_node(v.find("net")) == net_name]

            assert len(board_tracks) == len(tree_tracks), \
                f"Unprotected net {net_name} track mismatch"
            assert len(board_vias) == len(tree_vias), \
                f"Unprotected net {net_name} via mismatch"