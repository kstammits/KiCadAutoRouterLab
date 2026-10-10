import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINIMAL_PCB = FIXTURES / "minimal.kicad_pcb"
DCCF_PCB = FIXTURES / "DCCF.sved.kicad_pcb"


@pytest.fixture(scope="module")
def minimal_model():
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    return board_model(parse_file(MINIMAL_PCB))


@pytest.fixture(scope="module")
def dccf_model():
    from kicad_autorouter.board_model import board_model
    from kicad_autorouter.sexpr import parse_file
    return board_model(parse_file(DCCF_PCB))


@pytest.fixture
def grid(minimal_model):
    from kicad_autorouter.routing.grid import create_grid_from_model
    return create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)


@pytest.fixture
def cost_grid(grid, minimal_model):
    from kicad_autorouter.routing.obstacles import build_occupancy_grid
    return build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)


@pytest.fixture
def routing_setup(minimal_model):
    """Create grid, cost_grid, and router for testing."""
    from kicad_autorouter.routing.grid import create_grid_from_model
    from kicad_autorouter.routing.obstacles import build_occupancy_grid
    from kicad_autorouter.routing.router import CostMap, SingleNetRouter
    grid = create_grid_from_model(minimal_model, resolution_mm=0.1, margin_mm=2.0)
    cost_grid = build_occupancy_grid(minimal_model, grid, default_clearance_mm=0.2)
    cost_map = CostMap()
    router = SingleNetRouter(grid, cost_grid, cost_map)
    return grid, cost_grid, router
