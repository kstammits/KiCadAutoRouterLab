"""Net scheduling and rip-up management for multi-net routing."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set

from ..board_model import BoardModel


def order_nets(model: BoardModel) -> List[str]:
    """Return net names in routing priority order.
    
    Priority:
    1. Power rails (V+, V-, PWR) - few terminals, wide tracks
    2. Ground stitching (GND) - connect ground splits
    3. Critical signals (CLK, CLK_, DIFF, USB, ETH) - from schematic
    4. Decoupling cap fanout (short, near chips)
    5. General signals (longest first)
    6. Remaining
    """
    if not model.nets:
        return []

    # Classify nets
    power_nets = []
    ground_nets = []
    critical_nets = []
    decap_nets = []
    general_nets = []
    other_nets = []

    for net_name, conns in model.nets.items():
        net_lower = net_name.lower()
        
        # Power rails
        if any(kw in net_lower for kw in ("vcc", "vdd", "v+", "v-", "pwr", "power", "+5v", "-5v", "+12v", "-12v", "3v3", "1v8", "1v2")):
            power_nets.append(net_name)
        # Ground
        elif any(kw in net_lower for kw in ("gnd", "ground", "vgnd")):
            ground_nets.append(net_name)
        # Critical signals
        elif any(kw in net_lower for kw in ("clk", "clock", "diff", "usb", "eth", "hdmi", "pcie", "sata", "spi", "i2c", "uart")):
            critical_nets.append(net_name)
        # Decoupling caps (typically 2 terminals, short)
        elif len(conns) == 2:
            decap_nets.append(net_name)
        else:
            general_nets.append(net_name)

    # Sort each category by terminal count (fewer first) then by name
    def sort_key(net_name):
        return (len(model.nets[net_name]), net_name)

    ordered = []
    for category in [power_nets, ground_nets, critical_nets, decap_nets, general_nets, other_nets]:
        ordered.extend(sorted(category, key=sort_key))

    return ordered


class RipUpManager:
    """Manages rip-up and retry for failed nets."""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts
        self.attempts: Dict[str, int] = defaultdict(int)
        self.routed_paths: Dict[str, List[Tuple[int, int, int]]] = {}
        self.net_failures: Dict[str, str] = {}

    def should_rip_up(
        self,
        net_name: str,
        affected_footprints: Optional[Set[str]] = None,
    ) -> bool:
        """Determine if a net should be ripped up."""
        # Already failed max attempts
        if self.attempts[net_name] >= self.max_attempts:
            return False
        
        # Footprints moved
        if affected_footprints:
            return True
        
        # Failed previously
        if net_name in self.net_failures:
            return True
        
        return False

    def rip_up(self, net_name: str) -> bool:
        """Mark net for rip-up. Returns True if actually ripped up."""
        if net_name in self.routed_paths:
            self.routed_paths.pop(net_name, None)
            self.net_failures[net_name] = "ripped up"
            return True
        return False

    def rip_up_multiple(self, net_names: List[str]) -> int:
        """Rip up multiple nets. Returns count actually ripped."""
        count = 0
        for net in net_names:
            if self.rip_up(net):
                count += 1
        return count

    def record_success(self, net_name: str, path: List[Tuple[int, int, int]]) -> None:
        """Record successful routing."""
        self.routed_paths[net_name] = path
        self.net_failures.pop(net_name, None)

    def record_failure(self, net_name: str, reason: str) -> None:
        """Record failed routing attempt."""
        self.attempts[net_name] += 1
        self.net_failures[net_name] = reason

    def can_retry(self, net_name: str) -> bool:
        return self.attempts[net_name] < self.max_attempts

    def get_remaining_attempts(self, net_name: str) -> int:
        return max(0, self.max_attempts - self.attempts[net_name])

    def get_routed_nets(self) -> List[str]:
        return list(self.routed_paths.keys())

    def get_failed_nets(self) -> List[str]:
        return [n for n, a in self.attempts.items() if a >= self.max_attempts]

    def clear(self) -> None:
        self.attempts.clear()
        self.routed_paths.clear()
        self.net_failures.clear()