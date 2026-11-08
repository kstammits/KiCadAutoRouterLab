"""Stage B of the routing pipeline: stitching, tree writeback, refill, DRC.

Consumes a :class:`routing.route_raw.RawRoutes` bundle from :func:`route_raw`
(single trial or refine winner alike) and produces the
:class:`routing.pipeline.RoutingResult`.

Note: :mod:`routing.pipeline` defines :class:`RoutingResult` and imports
:func:`finish_routes` from here *after* the dataclass is bound, so the
``from .pipeline import RoutingResult`` above resolves at import time.
"""

from __future__ import annotations

import time
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from ..board_model import ArcTrack, BoardModel, Point, Track, Via, is_ground_net
from ..drc import run_drc_on_tree, DRCResult
from ..sexpr import parse_file, to_sexpr
from ..validate import refill_zones
from .grid import RoutingGrid
from .output import routes_to_tracks_vias, apply_routes_to_tree
from .output import Arc as OutputArc, Track as OutputTrack, Via as OutputVia
from .params import RoutingParams
from .pipeline import RoutingResult  # bound in pipeline before this module is imported
from .route_raw import RawRoutes
from .stitch import plan_stitches


def finish_routes(
    raw: RawRoutes,
    params: RoutingParams,
) -> Tuple[RoutingResult, np.ndarray, RoutingGrid]:
    """Finish raw copper: GND stitching, tree writeback, refill, DRC.

    Steps 10b–12 of the pipeline; consumes a :class:`RawRoutes` bundle from
    :func:`route_raw` (single trial or refine winner alike).
    """
    model = raw.model
    routing_grid = raw.routing_grid
    cost_grid = raw.cost_grid
    tracks = list(raw.tracks)
    vias = list(raw.vias)
    route_arcs = list(raw.route_arcs)

    # 10b. Automatic GND continuity repair (stitching vias + bridge tracks).
    # Runs on the routed board: newly routed copper joins the island model,
    # so fresh fragments get repaired in the same call. Appends standard
    # vias/tracks; KiCad refills the pours around them on open. Runs
    # whenever stitching is enabled — even after a GND strip left no copper
    # behind — so evicted stitch vias are always rebuilt in the same call.
    stitch_vias_added = 0
    stitch_tracks_added = 0
    islands_before = 0
    islands_after = 0
    if params.stitch_gnd_vias:
        routed_model = replace(
            model,
            tracks=model.tracks + tuple(
                Track(start=Point(*t.start), end=Point(*t.end),
                      width_mm=t.width_mm, layer=t.layer, net_name=t.net_name)
                for t in tracks
            ),
            arcs=model.arcs + tuple(
                ArcTrack(start=Point(*a.start), mid=Point(*a.mid), end=Point(*a.end),
                         width_mm=a.width_mm, layer=a.layer, net_name=a.net_name)
                for a in route_arcs
            ),
            vias=model.vias + tuple(
                Via(position=Point(*v.position), size_mm=v.size_mm,
                    drill_mm=v.drill_mm, layers=tuple(v.layers), net_name=v.net_name)
                for v in vias
            ),
        )
        plan = plan_stitches(
            routed_model, routing_grid,
            size_mm=params.via_size_mm,
            drill_mm=params.via_drill_mm,
            clearance_mm=params.clearance_mm,
            bridge_width_mm=params.stitch_bridge_width_mm,
            max_vias=params.stitch_vias_max,
            max_bridges=params.stitch_bridges_max,
            max_bridge_mm=params.stitch_bridge_max_mm,
            min_island_mm2=params.stitch_min_island_mm2,
        )
        for sv in plan.vias:
            vias.append(OutputVia(
                position=(sv.position.x_mm, sv.position.y_mm),
                size_mm=sv.size_mm, drill_mm=sv.drill_mm,
                layers=tuple(sv.layers), net_name=sv.net_name or "",
            ))
        for st in plan.tracks:
            tracks.append(OutputTrack(
                start=(st.start.x_mm, st.start.y_mm),
                end=(st.end.x_mm, st.end.y_mm),
                width_mm=st.width_mm, layer=st.layer, net_name=st.net_name or "",
            ))
        stitch_vias_added = len(plan.vias)
        stitch_tracks_added = len(plan.tracks)
        islands_before = plan.islands_before
        islands_after = plan.islands_after

    # 11. Apply routes to PCB tree
    pcb_tree = None
    if raw.pcb_tree is not None:
        pcb_tree = apply_routes_to_tree(raw.pcb_tree, tracks, vias, route_arcs)

    # 11b. Refill zones if requested (requires kicad-cli).
    # Runs on a temp copy so the in-memory tree is only replaced when
    # kicad-cli loads, refills, and saves cleanly.
    refill_done = False
    if params.refill_zones and pcb_tree is not None:
        with tempfile.TemporaryDirectory(prefix="refill_") as tmpdir:
            tmp_path = Path(tmpdir) / "route_refill.kicad_pcb"
            tmp_path.write_text(to_sexpr(pcb_tree) + "\n", encoding="utf-8")
            refill_result = refill_zones(tmp_path)
            # loaded=True means kicad-cli wrote a DRC report (file parsed);
            # a load error leaves error set and the tree untouched.
            refill_done = refill_result.loaded and not refill_result.error
            if refill_done:
                pcb_tree = parse_file(tmp_path)

    # 12. Run DRC if requested
    drc_result = None
    if params.run_drc and pcb_tree is not None:
        drc_result = run_drc_on_tree(pcb_tree, prefix="route_")

    timings = dict(raw.timings)
    timings["expansions"] = float(raw.expansions_total)
    timings["total_s"] = time.perf_counter() - raw.t_total
    return RoutingResult(
        tracks_added=len(tracks),
        vias_added=len(vias),
        nets_routed=raw.nets_routed,
        nets_failed=raw.nets_failed,
        drc_clean=drc_result.clean if drc_result else True,
        drc_violations=drc_result.total_issues if drc_result else 0,
        drc_result=drc_result,
        pcb_tree=pcb_tree,
        stitch_vias_added=stitch_vias_added,
        stitch_tracks_added=stitch_tracks_added,
        gnd_islands_before=islands_before,
        gnd_islands_after=islands_after,
        gnd_stripped=raw.gnd_stripped,
        cancelled=raw.cancelled,
        expansions=raw.expansions_total,
        timings=timings,
        refill_zones_done=refill_done,
        fanout_vias_added=len(raw.plane_vias),
        fanout_tracks_added=len(raw.plane_tracks),
        failed_reasons=dict(raw.ripup.net_failures),
        partial_tracks=tuple(raw.partial_tracks),
        partial_vias=tuple(raw.partial_vias),
        partial_arcs=tuple(raw.partial_arcs),
        arcs_added=len(route_arcs),
        total_wire_mm=raw.total_wire_mm,
    ), cost_grid, routing_grid
