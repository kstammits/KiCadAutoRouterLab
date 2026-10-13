"""User-controlled power membership: effective classifier + width unity.

Light pinning (behavior in flux): keyword defaults unchanged with an empty
override set; marked nets join power everywhere (order, width, decap
detection); KiCad power-named classes seed the set on load.
"""

import pytest

from kicad_autorouter.board_model import (
    NetClass,
    is_power_net_effective,
    resolve_track_width_mm,
    seed_power_nets_from_netclasses,
)
from kicad_autorouter.routing.power import identify_power_nets

from .helpers import make_fp
from .test_pour import _board


def _model(extra_nets=("VCC", "GND", "MOTOR_A", "N1")):
    fps = [make_fp("U1", "uuid-u1", 0.0, 0.0, extra_nets[:2]),
           make_fp("M1", "uuid-m1", 20.0, 0.0, extra_nets[2:])]
    from kicad_autorouter.board_model import NetConnection
    model = _board(fps=fps)
    return model.__class__(
        **{**model.__dict__, "nets": {n: (NetConnection("U1", "1"),) for n in extra_nets}}
    )


@pytest.mark.unit
class TestEffectiveClassifier:
    def test_keyword_unchanged(self):
        assert is_power_net_effective("VCC")
        assert is_power_net_effective("Net-+12V")
        assert not is_power_net_effective("MOTOR_A")
        assert not is_power_net_effective(None)

    def test_override_marks(self):
        assert is_power_net_effective("MOTOR_A", {"MOTOR_A"})
        assert not is_power_net_effective("N1", {"MOTOR_A"})

    def test_identify_split(self):
        power, ground, signal = identify_power_nets(_model())
        assert power == ["VCC"] and ground == ["GND"]
        assert set(signal) == {"MOTOR_A", "N1"}
        power, _, signal = identify_power_nets(_model(), {"MOTOR_A"})
        assert set(power) == {"VCC", "MOTOR_A"} and signal == ["N1"]


@pytest.mark.unit
class TestWidthUnity:
    def test_marked_net_gets_power_width(self):
        assert resolve_track_width_mm("MOTOR_A", extra_power={"MOTOR_A"}) == 0.5
        assert resolve_track_width_mm("MOTOR_A") == 0.25
        assert resolve_track_width_mm("VCC") == 0.5

    def test_netclass_still_first(self):
        nc = NetClass(name="Skinny", trace_width_mm=0.3, nets=("MOTOR_A",))
        w = resolve_track_width_mm(
            "MOTOR_A", net_netclass={"MOTOR_A": "Skinny"},
            net_classes=(nc,), extra_power={"MOTOR_A"},
        )
        assert w == pytest.approx(0.3)

    def test_explicit_still_first(self):
        w = resolve_track_width_mm(
            "VCC", net_widths={"VCC": 1.0}, extra_power=set()
        )
        assert w == pytest.approx(1.0)


@pytest.mark.unit
class TestNetclassSeed:
    def test_power_named_class_seeds(self):
        model = _model()
        cls = NetClass(name="Power", trace_width_mm=0.8, nets=("MOTOR_A", "NOPE"))
        model = model.__class__(**{**model.__dict__, "net_classes": (cls,)})
        assert seed_power_nets_from_netclasses(model) == {"MOTOR_A"}

    def test_signal_named_class_ignored(self):
        model = _model()
        cls = NetClass(name="Signal", trace_width_mm=0.2, nets=("MOTOR_A",))
        model = model.__class__(**{**model.__dict__, "net_classes": (cls,)})
        assert seed_power_nets_from_netclasses(model) == set()
