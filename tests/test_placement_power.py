"""Power nets in placement: fanout-scaled attraction + decap handling.

Regression suite for the 2026-10-11 change that ended the exclude-all
treatment of power nets in ``_build_net_edges_pad_level``. Before, any pad
on a power/ground net got zero attraction edges, so a decoupling cap (both
pads on power/GND) felt only Coulomb repulsion and wandered away from its
IC. Now:

- signal nets: weight 1.0 (unchanged);
- power nets: weight power_scale / (fanout - 1) per edge (bounded total
  pull per pad, no global collapse);
- ground nets: excluded, except decap-incident edges (decap GND-side
  exception), which ride at the same fanout-scaled rate;
- decap-incident edges: x decap_boost on top.

Boards mirror tests/test_placement_forces.py conventions (valid
head-to-tail courtyard rects via helpers.make_fp, virtual single region).
"""

import math

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    Point,
    is_ground_net,
    is_power_net,
)
from kicad_autorouter.placement import (
    PlacementParams,
    _build_net_edges_pad_level,
    _collect_pad_nodes,
    _find_decap_pad_indices,
    run_placement,
)

from .helpers import make_fp


EDGE = 60.0


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


def _pads_and_refs(model):
    pads, pad_to_fp_idx, _, _, _ = _collect_pad_nodes(model)
    ref_by_idx = {}
    for i, fp_idx in enumerate(pad_to_fp_idx):
        if fp_idx >= 0 and fp_idx not in ref_by_idx:
            ref_by_idx[fp_idx] = model.footprints[fp_idx].ref
    return pads, pad_to_fp_idx, ref_by_idx


def _decap_board():
    """IC at x=0 + decap at x=30 sharing VCC/GND. Weak Coulomb (kr=1)
    so the constant-magnitude attraction dominates; friction off so even
    the powerless control's ~0.004 drift is observable."""
    u1 = make_fp("U1", "uuid-u1", 0.0, 0.0, ("VCC", "GND"),
                 hw=0.25, hh=0.25)
    c1 = make_fp("C1", "uuid-c1", 30.0, 0.0, ("VCC", "GND"),
                 hw=0.25, hh=0.25)
    return _board([u1, c1])


def _decap_params(**kw):
    base = dict(
        max_iterations=1,
        convergence_eps_m=1e-15,
        repulsion_k_N_m2=1e-09,
        attraction_k_N=5e-05,
            wall_k_N=1e-15,

            # SI rename: friction_threshold_mm (displacement deadband) is
            # now friction_N (force deadband); 0.0 still disables it.
            friction_N=0.0,
        )
    base.update(kw)
    return PlacementParams(**base)


@pytest.mark.unit
class TestSharedClassification:
    """Placement and routing agree on power/ground names (substring rules)."""

    def test_power_names(self):
        for name in ("VCC", "VDD", "+12V", "-12V", "Net-+12V", "VCC_IO", "3V3", "VBAT"):
            assert is_power_net(name), name

    def test_ground_names(self):
        for name in ("GND", "GND_PWR_FILT", "GROUND", "VGND"):
            assert is_ground_net(name), name

    def test_signal_names(self):
        for name in ("N1", "SDA", "OUT_L", None, ""):
            assert not is_power_net(name), name
            assert not is_ground_net(name), name

    def test_routing_agrees(self):
        from kicad_autorouter.routing.power import identify_power_nets

        u1 = make_fp("U1", "uuid-u1", 0.0, 0.0, ("Net-+12V", "GND", "N1"))
        model = _board([u1])
        model = model.__class__(
            **{**model.__dict__, "nets": {"Net-+12V": [], "GND": [], "N1": []}}
        )
        power, ground, signal = identify_power_nets(model)
        assert power == ["Net-+12V"]
        assert ground == ["GND"]
        assert signal == ["N1"]


@pytest.mark.unit
class TestEdgeWeights:
    """Fanout scaling, GND exclusion + decap exception, kill-switch."""

    def _rail_board(self):
        # U1 (VCC, N1), U2 (VCC, N2), U3 (VCC, N3): VCC fanout 3.
        fps = [
            make_fp("U1", "uuid-u1", 0.0, 0.0, ("VCC", "N1")),
            make_fp("U2", "uuid-u2", 20.0, 0.0, ("VCC", "N2")),
            make_fp("U3", "uuid-u3", 40.0, 0.0, ("VCC", "N3")),
        ]
        return _board(fps)

    def test_power_edges_scaled_by_fanout(self):
        model = self._rail_board()
        pads, pad_to_fp_idx, _ = _pads_and_refs(model)
        src, dst, w = _build_net_edges_pad_level(pads, pad_to_fp_idx)
        assert len(src) == 3  # full clique on VCC, nothing else
        assert all(ww == pytest.approx(1.0 / (3 - 1)) for ww in w)

    def test_signal_edges_weight_one(self):
        fa = make_fp("A", "uuid-a", 0.0, 0.0, ("N1",))
        fb = make_fp("B", "uuid-b", 20.0, 0.0, ("N1",))
        model = _board([fa, fb])
        pads, pad_to_fp_idx, _ = _pads_and_refs(model)
        src, dst, w = _build_net_edges_pad_level(pads, pad_to_fp_idx)
        assert len(src) == 1
        assert w[0] == pytest.approx(1.0)

    def test_ground_excluded_without_decap(self):
        fa = make_fp("R1", "uuid-a", 0.0, 0.0, ("GND",))
        fb = make_fp("R2", "uuid-b", 20.0, 0.0, ("GND",))
        model = _board([fa, fb])
        pads, pad_to_fp_idx, ref_by_idx = _pads_and_refs(model)
        decap = _find_decap_pad_indices(pads, pad_to_fp_idx, ref_by_idx)
        assert decap == set()
        src, dst, w = _build_net_edges_pad_level(
            pads, pad_to_fp_idx, decap_pad_indices=decap
        )
        assert len(src) == 0

    def test_decap_gnd_exception_and_boost(self):
        # C1 (VCC, GND) + U1 (VCC, GND): both nets fanout 2.
        u1 = make_fp("U1", "uuid-u1", 0.0, 0.0, ("VCC", "GND"))
        c1 = make_fp("C1", "uuid-c1", 30.0, 0.0, ("VCC", "GND"))
        model = _board([u1, c1])
        pads, pad_to_fp_idx, ref_by_idx = _pads_and_refs(model)
        decap = _find_decap_pad_indices(pads, pad_to_fp_idx, ref_by_idx)
        assert len(decap) == 2  # both C1 pads, neither U1 pad
        src, dst, w = _build_net_edges_pad_level(
            pads, pad_to_fp_idx, decap_pad_indices=decap, decap_boost=4.0
        )
        assert len(src) == 2  # VCC pair + GND pair (exception)
        assert all(ww == pytest.approx(4.0 / (2 - 1)) for ww in w)

    def test_decap_boost_zero_kills_gnd_exception(self):
        u1 = make_fp("U1", "uuid-u1", 0.0, 0.0, ("VCC", "GND"))
        c1 = make_fp("C1", "uuid-c1", 30.0, 0.0, ("VCC", "GND"))
        model = _board([u1, c1])
        pads, pad_to_fp_idx, ref_by_idx = _pads_and_refs(model)
        decap = _find_decap_pad_indices(pads, pad_to_fp_idx, ref_by_idx)
        src, dst, w = _build_net_edges_pad_level(
            pads, pad_to_fp_idx, decap_pad_indices=decap, decap_boost=0.0
        )
        nets = {pads[s].net_name for s in src}
        assert "GND" not in nets

    def test_include_power_false_restores_exclusion(self):
        model = self._rail_board()
        pads, pad_to_fp_idx, _ = _pads_and_refs(model)
        src, dst, w = _build_net_edges_pad_level(
            pads, pad_to_fp_idx, include_power=False
        )
        assert len(src) == 0

    def test_non_c_ref_not_decap(self):
        # R1 straddling power+ground is NOT a decap (ref gate).
        r1 = make_fp("R1", "uuid-a", 0.0, 0.0, ("VCC", "GND"))
        u1 = make_fp("U1", "uuid-u1", 30.0, 0.0, ("VCC", "GND"))
        model = _board([r1, u1])
        pads, pad_to_fp_idx, ref_by_idx = _pads_and_refs(model)
        assert _find_decap_pad_indices(pads, pad_to_fp_idx, ref_by_idx) == set()


@pytest.mark.physics
class TestPowerBehavior:
    """Emergent behavior: rails pull weakly, decaps hug their IC."""

    def test_shared_rail_does_not_collapse(self):
        """Six pads on one rail settle at a finite spread (fanout scaling).

        Without the 1/(fanout-1) term each pad would feel 5x ka pulling it
        into the pile; scaled, the total per pad is ~ka and Coulomb holds
        a finite star open.
        """
        fps = [
            make_fp(f"U{i}", f"uuid-u{i}", x, 0.0, ("VCC",),
                    hw=0.25, hh=0.25)
            for i, x in enumerate((-25.0, -15.0, -5.0, 5.0, 15.0, 25.0))
        ]
        model = _board(fps)
        prop = run_placement(model, _decap_params(max_iterations=30))
        xs = [fp.x_mm + prop.deltas.get(fp.uuid, (0.0, 0.0, 0.0))[0]
              for fp in model.footprints]
        initial_diameter = 50.0
        assert max(xs) - min(xs) > 0.5 * initial_diameter, (
            f"rail star collapsed: {xs}"
        )

    def test_decap_step_toward_ic(self):
        """The reported bug: a far decap's first step heads to its IC.

        Single-step pin (not a settling run): long-horizon settling of
        this toy geometry is repulsion drift, so the spec pins only the
        first step. (Pre-2026-10-11 this docstring also noted a rigid-snap
        reversal from iter 3; rigid projection removed that quirk — the
        isolated pair now drifts monotonically apart.) The force field
        is: attraction (-0.4, 0) toward the IC dominates Coulomb repulsion
        here.
        """
        prop = run_placement(_decap_board(), _decap_params())
        assert "uuid-c1" in prop.deltas
        dx, _, _ = prop.deltas["uuid-c1"]
        assert dx < 0.0, f"decap must step toward IC (dx<0), got {dx}"
        fx, _ = prop.forces["uuid-c1"]
        assert fx < 0.0
        ax, _ = prop.force_components["uuid-c1"]["attraction"]
        # SI newtons: 2 boosted edges x ka=5e-5 x decap_boost=4.0.
        assert ax == pytest.approx(-0.0004), (
            f"decap attraction must be 2 boosted edges x ka=5e-5 x boost 4, got {ax}"
        )

    def test_decap_drifts_without_power(self):
        """Control at identical gains: powerless decap feels zero
        attraction and its step points away (repulsion only)."""
        prop = run_placement(
            _decap_board(),
            _decap_params(include_power_nets=False, decap_boost=0.0),
        )
        assert "uuid-c1" in prop.deltas
        dx, _, _ = prop.deltas["uuid-c1"]
        assert dx > 0.0, f"powerless decap must drift away (dx>0), got {dx}"
        assert prop.force_components["uuid-c1"]["attraction"] == (0.0, 0.0)

    def test_power_weaker_than_signal(self):
        """Same geometry: a power pair attracts less hard than a signal pair."""

        def pair_pull(net):
            fa = make_fp("A", "uuid-a", 0.0, 0.0, (net,), hw=0.25, hh=0.25)
            fb = make_fp("B", "uuid-b", 10.0, 0.0, (net,), hw=0.25, hh=0.25)
            # Third footprint shares the net to raise fanout to 3.
            fc = make_fp("C3", "uuid-c", 10.0, 20.0, (net,), hw=0.25, hh=0.25)
            prop = run_placement(_board([fa, fb, fc]), _decap_params())
            dx, _, _ = prop.deltas["uuid-b"]
            return -dx  # pull toward A is negative dx

        assert pair_pull("VCC") < pair_pull("N1")


@pytest.mark.params
class TestPowerParams:
    """New PlacementParams fields validate and round-trip."""

    def test_defaults(self):
        p = PlacementParams()
        assert p.power_attraction_scale == 1.0
        assert p.decap_boost == 4.0
        assert p.include_power_nets is True

    def test_roundtrip(self):
        p = PlacementParams(power_attraction_scale=0.5, decap_boost=8.0,
                            include_power_nets=False)
        assert PlacementParams.from_dict(p.to_dict()) == p

    @pytest.mark.parametrize("bad", [
        {"power_attraction_scale": -1.0},
        {"decap_boost": -0.5},
        {"include_power_nets": 1},
        {"include_power_nets": "yes"},
    ])
    def test_rejects_bad_values(self, bad):
        with pytest.raises(ValueError):
            PlacementParams.from_dict(bad)

    def test_zero_scales_allowed(self):
        p = PlacementParams.from_dict(
            {"power_attraction_scale": 0.0, "decap_boost": 0.0})
        assert p.power_attraction_scale == 0.0
        assert p.decap_boost == 0.0
