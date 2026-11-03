"""Background workers for the workflow UI server.

Extracted from server.py: the async routing-job registry + worker and the
single-flight kicad-cli DRC run. server.py re-exports these names so the
handlers keep working.
"""

import threading
import time
from typing import Dict

from kicad_autorouter.board_model import board_model
from kicad_autorouter.drc import TMP_DIR, run_drc_on_tree
from kicad_autorouter.routing.pipeline import RoutingParams, route_nets

from debug_grids import _cost_grid_to_debug

from server_state import STATE


# Single-flight DRC: at most one kicad-cli subprocess at a time. Concurrent
# callers block on this lock, then serve the fresh cache instead of re-running.
_drc_lock = threading.Lock()
DRC_PCB_PATH = TMP_DIR / "drc_current.kicad_pcb"
DRC_REPORT_PATH = TMP_DIR / "drc_report_current.json"

# Async routing jobs: POST /api/routing/jobs starts a background thread per
# job (the UI routes one net per job); GET polls progress; POST .../cancel
# sets a cooperative flag honored between nets/attempts/pairs and inside A*
# (every 4096 expansions). Only one routing job runs at a time — a second
# start gets 409 busy — because jobs share STATE.cost_grid/routing_grid.
# The synchronous POST /api/routing/run is unchanged for back-compat/tests.
_JOBS: Dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_ROUTING_JOB_LOCK = threading.Lock()
_MAX_JOBS = 20


def _prune_jobs_locked() -> None:
    """Drop oldest finished jobs past the cap (call with _JOBS_LOCK held)."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    finished = sorted(
        ((jid, j) for jid, j in _JOBS.items() if j.get("state") in ("done", "failed")),
        key=lambda kv: kv[1].get("finished", 0),
    )
    for jid, _ in finished[: len(_JOBS) - _MAX_JOBS]:
        del _JOBS[jid]


def _run_routing_job(job_id: str) -> None:
    """Background worker: route the job's nets, then apply to STATE."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            _ROUTING_JOB_LOCK.release()
            return
    t0 = time.perf_counter()
    model, _, _, _, _, _ = STATE.snapshot
    tree, _ = STATE.snapshot_pcb()
    protected_nets = STATE.get_protected_nets()
    extra_power = STATE.get_power_override_nets()
    with _JOBS_LOCK:
        job["state"] = "running"
        job["started"] = t0
        params_dict = dict(job.get("params_dict") or {})
        net_names = list(job.get("net_names") or [])

    def _should_cancel() -> bool:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            return bool(j and j.get("cancel_requested"))

    def _on_progress(net_name: str, done: int, total: int, expansions: int) -> None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j["progress"] = {
                "stage": "astar",
                "net": net_name,
                "pairs_done": done,
                "pairs_total": total,
                "expansions": expansions,
                "elapsed_s": round(time.perf_counter() - t0, 1),
            }

    try:
        route_params = RoutingParams.from_dict(params_dict)
    except (ValueError, TypeError) as exc:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"bad params: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    try:
        result, cost_grid, routing_grid = route_nets(
            model=model,
            net_names=net_names,
            protected_nets=protected_nets,
            cost_grid=STATE.cost_grid,
            routing_grid=STATE.routing_grid,
            params=route_params,
            original_pcb_tree=tree,
            extra_power_nets=extra_power,
            progress_cb=_on_progress,
            should_cancel=_should_cancel,
        )
    except ValueError as exc:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing rejected: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    except Exception as exc:  # noqa: BLE001 — surfaced via poll, never a hang
        import traceback
        traceback.print_exc()
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing failed: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    except BaseException:
        # Never wedge the single-flight lock (KeyboardInterrupt/SystemExit
        # in the worker must not 409-block the server forever).
        _ROUTING_JOB_LOCK.release()
        raise

    # Apply to shared state (same writeback as the sync endpoint: rebuild the
    # model from the updated tree so /api/board.svg and /api/nets see copper).
    with STATE._lock:
        STATE.cost_grid = cost_grid
        STATE.routing_grid = routing_grid
        if result.pcb_tree is not None:
            STATE.pcb_tree = result.pcb_tree
            try:
                STATE.model = board_model(result.pcb_tree)
            except Exception:
                STATE.routing_model = model
            else:
                STATE.routing_model = STATE.model
        else:
            STATE.routing_model = model
        STATE.version += 1
        version = STATE.version
    payload = {
        "ok": True,
        "tracks_added": result.tracks_added,
        "arcs_added": result.arcs_added,
        "vias_added": result.vias_added,
        "fanout_vias_added": result.fanout_vias_added,
        "fanout_tracks_added": result.fanout_tracks_added,
        "nets_routed": result.nets_routed,
        "nets_failed": result.nets_failed,
        "drc_clean": result.drc_clean,
        "drc_violations": result.drc_violations,
        "stitch_vias_added": result.stitch_vias_added,
        "stitch_tracks_added": result.stitch_tracks_added,
        "gnd_islands_before": result.gnd_islands_before,
        "gnd_islands_after": result.gnd_islands_after,
        "gnd_stripped": result.gnd_stripped,
        "cancelled": result.cancelled,
        "expansions": result.expansions,
        "timings": result.timings,
        "failed_reasons": result.failed_reasons,
        "partial_tracks": [
            {"start": list(t.start), "end": list(t.end),
             "width_mm": t.width_mm, "layer": t.layer,
             "net_name": t.net_name}
            for t in result.partial_tracks
        ],
        "partial_vias": [
            {"position": list(v.position), "size_mm": v.size_mm,
             "drill_mm": v.drill_mm, "layers": list(v.layers),
             "net_name": v.net_name}
            for v in result.partial_vias
        ],
        "version": version,
    }
    if job.get("mode_warning"):
        payload["mode_warning"] = job["mode_warning"]
    if route_params.debug:
        payload["debug"] = _cost_grid_to_debug(cost_grid, routing_grid)
    with _JOBS_LOCK:
        job["state"] = "done"
        job["result"] = payload
        job["finished"] = time.perf_counter()
        job["progress"] = {
            "stage": "done",
            "net": (net_names or [""])[0],
            "pairs_done": 0,
            "pairs_total": 0,
            "expansions": result.expansions,
            "elapsed_s": round(time.perf_counter() - t0, 1),
        }
    _ROUTING_JOB_LOCK.release()


def _run_drc_now(tree, for_version: int) -> dict:
    """Run kicad-cli DRC on ``tree`` and store the result stamped with ``for_version``."""
    STATE.set_drc_running(True)
    try:
        result = run_drc_on_tree(tree, out_path=DRC_PCB_PATH, report_path=DRC_REPORT_PATH)
    finally:
        STATE.set_drc_running(False)
    filtered = result.filter_violations()
    by_type: Dict[str, int] = {}
    for v in filtered:
        t = v.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    STATE.store_drc_result(
        filtered,
        for_version=for_version,
        unconnected=result.unconnected_count,
        error=result.error,
    )
    current_version = STATE.snapshot[3]
    return {
        "ok": True,
        "cached": False,
        "for_version": for_version,
        "current_version": current_version,
        "stale": for_version != current_version,
        "violations": len(filtered),
        "unconnected": result.unconnected_count,
        "by_type": by_type,
        "error": result.error,
    }
