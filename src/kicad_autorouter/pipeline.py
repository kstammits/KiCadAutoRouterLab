"""Workflow pipeline definition for the KiCad autorouter.

Stages run in order; `status` reflects implementation state, not runtime
state. This is the single source of truth shown by the workflow UI.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    description: str
    status: str  # "planned" | "implemented"


STAGES = [
    Stage(
        "ingest",
        "Ingest",
        "Load .kicad_pcb / .kicad_sch project files.",
        "planned",
    ),
    Stage(
        "parse",
        "Parse",
        "Extract pads, nets, vias, zones and board outline from the S-expression file format.",
        "planned",
    ),
    Stage(
        "connectivity",
        "Connectivity",
        "Build a net connectivity graph (pads per net) with networkx.",
        "planned",
    ),
    Stage(
        "route",
        "Route",
        "Compute trace paths between pads on each net (the autorouter core).",
        "planned",
    ),
    Stage(
        "drc",
        "DRC check",
        "Validate clearances, widths and layer rules against the design rule matrix.",
        "planned",
    ),
    Stage(
        "writeback",
        "Write back",
        "Emit an updated .kicad_pcb with new trace segments.",
        "planned",
    ),
]


def stages() -> list[dict]:
    return [asdict(s) for s in STAGES]
