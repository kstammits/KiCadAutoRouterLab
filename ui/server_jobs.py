"""Background workers for the workflow UI server.

Extracted from server.py: the async routing-job registry + worker and the
single-flight kicad-cli DRC run. server.py re-exports these names so the
handlers keep working.
"""

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_UI_DIR.parent))

from kicad_autorouter.board_model import board_model
from kicad_autorouter.drc import TMP_DIR, run_drc_on_tree
from kicad_autorouter.io import rip_up_nets
from kicad_autorouter.routing.output import (
    apply_routes_to_tree,
    routes_to_tracks_vias,
)
from kicad_autorouter.routing.pipeline import (
    RoutingParams,
    _tht_sites_by_net,
    refine_routing,
    route_nets,
)
from kicad_autorouter.routing.polish import polish_routes

from ui.debug_grids import _cost_grid_to_debug

from ui.server_state import STATE


# Single-flight DRC: at most one kicad-cli subprocess at a time. Concurrent
# callers block on this lock, then serve the fresh cache instead of re-running.
_drc_lock = threading.Lock()
DRC_PCB_PATH = TMP_DIR / "drc_current.kicad_pcb"
DRC_REPORT_PATH = TMP_DIR / "drc_report_current.json"

# Async routing jobs: POST /api/routing/jobs starts a background thread per
# job (the UI routes one net per job); GET polls progress; POST .../cancel
# sets a cooperative flag honored between nets/attempts/pairs and inside A*
# (every 4096 expansions). Only one routing job runs at a time — a second
# start gets 409 busy — because jobs share STATE.cost_grid/routing_grid
# and the obstacle cache.
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
    from server import _routing_result_payload  # lazy: server imports this module
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
            prog = {
                "stage": "astar",
                "net": net_name,
                "pairs_done": done,
                "pairs_total": total,
                "expansions": expansions,
                "elapsed_s": round(time.perf_counter() - t0, 1),
            }
            if j.get("refine"):
                prog["stage"] = "refine"
                prog["candidate"] = j.get("refine_candidate", 0)
                prog["candidates"] = j.get("refine_total", 0)
            j["progress"] = prog

    def _on_stage(idx: int, total: int) -> None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j["refine_candidate"] = idx
            j["refine_total"] = total

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
        route_fn = refine_routing if job.get("refine") else route_nets
        kwargs = {}
        if job.get("refine"):
            kwargs["stage_cb"] = _on_stage
        result, cost_grid, routing_grid = route_fn(
            model=model,
            net_names=net_names,
            protected_nets=protected_nets,
            cost_grid=STATE.cost_grid,
            routing_grid=STATE.routing_grid,
            obstacle_cache=STATE.obstacle_cache,
            params=route_params,
            original_pcb_tree=tree,
            extra_power_nets=extra_power,
            progress_cb=_on_progress,
            should_cancel=_should_cancel,
            **kwargs,
        )
    except ValueError as exc:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing rejected: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    except Exception as exc:  # noqa: BLE001 — surfaced via poll, never a hang
        logger.exception("routing job %s failed", job_id)
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = f"routing failed: {exc}"
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()
        return
    except BaseException:
        # Never wedge the single-flight lock (KeyboardInterrupt/SystemExit
        # in the worker must not 409-block the server forever). Mark the job
        # failed first so pollers don't see it stuck in "running" forever.
        with _JOBS_LOCK:
            if job is not None:
                job["state"] = "failed"
                job["error"] = "routing job interrupted"
                job["finished"] = time.perf_counter()
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
    try:
        STATE.set_last_failed(result.failed_reasons or {})
    except (AttributeError, TypeError):
        pass
    payload = _routing_result_payload(result)
    payload["version"] = version
    try:
        STATE.set_last_polishable(getattr(result, "net_routes", None) or {},
                                  getattr(result, "width_map", None) or {})
    except (AttributeError, TypeError):
        pass
    if job.get("mode_warning"):
        payload["mode_warning"] = job["mode_warning"]
    if job.get("refine"):
        payload["candidates"] = [
            {k: (list(v) if k == "order" else v) for k, v in c.items()}
            for c in result.candidates
        ]
        payload["winner_seed"] = result.winner_seed
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


def _run_polish_job(job_id: str) -> None:
    """Background worker: re-smooth stored routes at a higher effort.

    Polish never re-searches (no A*): each net's stashed branches are
    re-smoothed against the pinned final matrix and kept only when
    gate-clean and shorter. Changed nets are ripped from the tree and
    the improved copper applied; the stash is refreshed so Polish
    iterates (dial effort up, polish again).
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            _ROUTING_JOB_LOCK.release()
            return
    t0 = time.perf_counter()
    model, _, _, version_at_start, _, _ = STATE.snapshot
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

    def _fail(msg: str) -> None:
        with _JOBS_LOCK:
            job["state"] = "failed"
            job["error"] = msg
            job["finished"] = time.perf_counter()
        _ROUTING_JOB_LOCK.release()

    def _on_progress(net_name: str, done: int, total: int) -> None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is None:
                return
            j["progress"] = {
                "stage": "polish",
                "net": net_name,
                "pairs_done": done,
                "pairs_total": total,
                "expansions": 0,
                "elapsed_s": round(time.perf_counter() - t0, 1),
            }

    try:
        route_params = RoutingParams.from_dict(params_dict)
    except (ValueError, TypeError) as exc:
        _fail(f"bad params: {exc}")
        return
    if not route_params.smooth_effort > 0:
        _fail("smooth_effort must be > 0 to polish (0 skips smoothing)")
        return
    bundle = STATE.get_last_polishable()
    if not bundle.get("routes"):
        _fail("no routed copper to polish — route first")
        return
    if bundle.get("version") != version_at_start:
        _fail("board changed since routing — re-route first, then polish")
        return
    routing_grid = STATE.routing_grid
    if routing_grid is None:
        _fail("no routing grid — route first")
        return
    stored = bundle["routes"]
    widths = bundle["widths"]
    missing = sorted({n for n in net_names if n not in stored})
    skipped_protected = sorted({n for n in net_names if n in protected_nets})
    todo = [n for n in net_names
            if n in stored and n not in protected_nets]
    if not todo:
        _fail(f"nothing polishable (missing: {missing}; "
              f"protected: {skipped_protected})")
        return
    try:
        new_results, report = polish_routes(
            stored, widths, model, routing_grid, route_params, todo,
            float(route_params.smooth_effort),
            extra_power_nets=extra_power,
            progress_cb=_on_progress,
            should_cancel=_should_cancel,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced via poll, never a hang
        logger.exception("polish job %s failed", job_id)
        _fail(f"polish failed: {exc}")
        return
    except BaseException:
        _fail("polish job interrupted")
        raise
    changed = sorted(n for n, r in report.get("nets", {}).items()
                     if r.get("status") == "polished")
    payload: dict = {
        "ok": True,
        "report": report,
        "changed": changed,
        "missing": missing,
        "skipped_protected": skipped_protected,
        "drc_clean": True,
        "drc_violations": 0,
    }
    version = version_at_start
    if changed:
        try:
            tracks, vias, arcs = routes_to_tracks_vias(
                {n: new_results[n] for n in changed},
                routing_grid,
                net_class_widths=widths,
                default_width_mm=route_params.track_width_mm,
                power_width_mm=route_params.power_width_mm,
                via_size_mm=route_params.via_size_mm,
                via_drill_mm=route_params.via_drill_mm,
                tht_sites=_tht_sites_by_net(model, routing_grid),
                extra_power_nets=extra_power,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("polish job %s conversion failed", job_id)
            _fail(f"polish conversion failed: {exc}")
            return
        STATE.push_undo()
        with STATE._lock:
            tree = STATE.pcb_tree
            tree = rip_up_nets(tree, set(changed), protected_nets)
            tree = apply_routes_to_tree(tree, tracks, vias, arcs)
            try:
                STATE.pcb_tree = tree
                STATE.model = board_model(tree)
                STATE.routing_model = STATE.model
            except Exception:
                STATE.routing_model = model
            STATE.cost_grid = None  # copper moved: force a rebuild next run
            STATE.version += 1
            version = STATE.version
        if route_params.run_drc:
            try:
                drc = run_drc_on_tree(tree)
                payload["drc_clean"] = bool(drc.clean)
                payload["drc_violations"] = int(drc.total_issues)
            except Exception as exc:  # noqa: BLE001 — DRC is advisory here
                logger.warning("polish DRC failed: %s", exc)
        try:
            STATE.set_last_polishable(
                {n: new_results.get(n, stored[n]) for n in stored},
                widths)
        except (AttributeError, TypeError):
            pass
    payload["version"] = version
    with _JOBS_LOCK:
        job["state"] = "done"
        job["result"] = payload
        job["finished"] = time.perf_counter()
        job["progress"] = {
            "stage": "done",
            "net": "",
            "pairs_done": len(todo),
            "pairs_total": len(todo),
            "expansions": 0,
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
