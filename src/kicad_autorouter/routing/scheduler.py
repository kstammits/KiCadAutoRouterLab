"""Net scheduling and rip-up management for multi-net routing."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from ..board_model import is_ground_net


def is_rippupable_victim(net_name: Optional[str]) -> bool:
    """True when a net's copper may be evicted to make way for another net.

    GND-only rule: ground-pour copper (GND/GND_PWR/VGND, ...) is cheaply
    replicated elsewhere (zones refill + ``plan_stitches()`` rebuilds
    inter-layer continuity), so it is always fair game. Signal/power
    copper is never evicted except when explicitly requested or
    placement-moved — this is what stops tracks ripping each other out.
    """
    return is_ground_net(net_name)


class RipUpManager:
    """Manages rip-up and retry for failed nets."""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts
        self.attempts: Dict[str, int] = defaultdict(int)
        self.routed_paths: Dict[str, List[Tuple[int, int, int]]] = {}
        self.net_failures: Dict[str, str] = {}

    def can_rip(self, requestor_net: str, victim_net: Optional[str]) -> bool:
        """True when ``requestor_net`` may evict ``victim_net``'s copper.

        Priority rule ("don't rip anyone higher than you"): only GND-family
        victims are evictable, regardless of requestor. Same-net re-route
        (requestor == victim) is always allowed and handled by the
        pipeline's requested-net rip-up, not by this gate.
        """
        if victim_net is None:
            return False
        if requestor_net == victim_net:
            return True
        return is_rippupable_victim(victim_net)

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

    def get_failed_nets(self) -> List[str]:
        return [n for n, a in self.attempts.items() if a >= self.max_attempts]