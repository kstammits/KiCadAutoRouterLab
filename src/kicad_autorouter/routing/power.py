"""Power/ground net classification shared by routing and placement."""

from __future__ import annotations

from typing import Collection, List, Tuple

from ..board_model import BoardModel, is_ground_net, is_power_net_effective


def identify_power_nets(
    model: BoardModel, extra_power: Collection[str] = ()
) -> Tuple[List[str], List[str], List[str]]:
    """Classify nets into power rails, ground, and signal.

    Returns (power_nets, ground_nets, signal_nets).
    Power = keyword match OR user-marked membership (extra_power, toggled
    in the app and seeded from power-named KiCad netclasses on load);
    shared with placement (power wins on overlap, e.g. "GND_PWR").
    """
    power = []
    ground = []
    signal = []

    for net_name in model.nets:
        if is_power_net_effective(net_name, extra_power):
            power.append(net_name)
        elif is_ground_net(net_name):
            ground.append(net_name)
        else:
            signal.append(net_name)

    return power, ground, signal

