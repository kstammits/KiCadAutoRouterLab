"""Shared HTTP payload builders for the workflow UI server.

Extracted from ui/server.py: pure functions with no server-state imports,
so handlers and tests can use them without pulling the whole server module.
ui/server.py re-exports them for backward compatibility.
"""

from __future__ import annotations

from pathlib import Path


def _is_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves inside ``root`` (path-traversal guard)."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_filename(name: str, suffix: str) -> str:
    """Board-download filename that cannot break out of its header.

    ``STATE.name`` arrives verbatim from ``/api/load?name=``; strip path
    components and any quote/CR/LF (response splitting) plus other
    RFC 6266-unsafe characters, keeping ``[A-Za-z0-9._-]``. Always ends
    with ``suffix`` (a ``.kicad_pcb`` name).
    """
    stem = (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.endswith(".kicad_pcb"):
        stem = stem[: -len(".kicad_pcb")]
    stem = "".join(c for c in stem if c.isalnum() or c in "._-").strip("._") or "board"
    return stem[:128] + suffix


def proposal_to_json(proposal, include_frames=True) -> dict:
    def r5(x):
        return round(x, 5) if isinstance(x, float) else x

    def r3(x):
        return round(x, 3) if isinstance(x, float) else x

    def r2(x):
        return round(x, 2) if isinstance(x, float) else x

    out = {
        "deltas": {u: [r5(dx), r5(dy), r5(da)] for u, (dx, dy, da) in proposal.deltas.items()},
        "label_deltas": {
            u: [r5(dx), r5(dy)]
            for u, (dx, dy) in (getattr(proposal, "label_deltas", {}) or {}).items()
        },
        "iterations": proposal.iterations,
        "final_max_disp_mm": r5(proposal.final_max_disp_mm),
        "elapsed_s": r5(proposal.elapsed_s),
        "params": proposal.params,
        "stop_reason": getattr(proposal, "stop_reason", "budget"),
        "residual_max_force_N": r5(getattr(proposal, "residual_max_force_N", 0.0)),
        "depenetrated": getattr(proposal, "depenetrated", False),
        "skipped_uuidless": getattr(proposal, "skipped_uuidless", 0),
        "legality_is_legal": getattr(proposal, "legality_is_legal", True),
        "overlapping_pairs": getattr(proposal, "overlapping_pairs", []),
        "wall_violations": getattr(proposal, "wall_violations", []),
        "max_pair_overlap_mm2": r5(getattr(proposal, "max_pair_overlap_mm2", 0.0)),
        "max_wall_penetration_mm": r5(getattr(proposal, "max_wall_penetration_mm", 0.0)),
        "exempt_pairs": getattr(proposal, "exempt_pairs", []),
        "degenerate_bodies": list(getattr(proposal, "degenerate_bodies", ())),
    }
    frames = getattr(proposal, "frames", None) or []
    if include_frames and frames:
        # Frames pack (dx, dy, da, ldx, ldy); tolerate 3-tuples from
        # footprint-only history.
        def _fr_json(fr):
            o = {}
            for u, d in fr.items():
                dx, dy, da = d[0], d[1], d[2]
                e = [r3(dx), r3(dy), r2(da)]
                if len(d) >= 5:
                    e += [r3(d[3]), r3(d[4])]
                o[u] = e
            return o
        out["frames"] = [_fr_json(fr) for fr in frames]
        out["frame_stride"] = getattr(proposal, "frame_stride", 1)
        out["frame_count"] = len(frames)
    force_frames = getattr(proposal, "force_frames", None) or []
    if include_frames and force_frames:
        out["force_frames"] = force_frames
    return out


def _movable_uuids_for(model, state, req_uuids):
    """Resolve the movable footprint set for a placement request.

    ``req_uuids`` is the client-supplied list (or ``None``). When ``None``,
    derive all unlocked, non-pinned footprints. When provided, drop any pinned
    UUIDs. Returns a set of UUIDs.
    """
    if req_uuids is None:
        return {
            fp.uuid for fp in model.footprints
            if fp.uuid and not fp.locked and fp.uuid not in state.get_pinned_uuids()
        }
    pinned = state.get_pinned_uuids()
    return {u for u in req_uuids if u not in pinned}


def _footprint_summary(fp, pinned, selected, decaps, ghost=False):
    """Build one /api/footprints entry, shared by the real and ghost loops.

    Field order matches the historical hand-built dicts byte-for-byte.
    """
    nets = sorted({pad.net_name for pad in fp.pads if pad.net_name})
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for a, b in fp.courtyard:
        minx = min(minx, a.x_mm, b.x_mm)
        miny = min(miny, a.y_mm, b.y_mm)
        maxx = max(maxx, a.x_mm, b.x_mm)
        maxy = max(maxy, a.y_mm, b.y_mm)
    if minx == float("inf"):
        minx = miny = maxx = maxy = 0.0
    pad_count = sum(1 for pad in fp.pads if pad.net_name is not None)
    out = {
        "uuid": fp.uuid,
        "ref": fp.ref,
        "footprint_id": fp.footprint_id,
        "layer": fp.layer,
        "x_mm": fp.x_mm,
        "y_mm": fp.y_mm,
        "angle_deg": fp.angle_deg,
        "locked": fp.locked if not ghost else False,
        "pinned": (fp.uuid in pinned) if not ghost else False,
        "selected": (fp.uuid in selected) if not ghost else False,
        "nets": nets,
        "bbox": [minx, miny, maxx, maxy],
        "ghost": ghost,
    }
    if ghost:
        out["ghost_attractions"] = [[t, ka] for t, ka in (fp.ghost_attractions or ())]
    out["is_decap"] = (fp.uuid in decaps) if not ghost else False
    out["pad_count"] = pad_count
    return out


def _routing_result_payload(result) -> dict:
    """Shared routing-result body core (ok through partial_arcs).

    Endpoint-specific fields (version, mode_warning, candidates, debug) are
    added by the caller so JSON field order stays byte-identical to before.
    """
    return {
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
        "partial_arcs": [
            {"start": list(a.start), "mid": list(a.mid), "end": list(a.end),
             "width_mm": a.width_mm, "layer": a.layer,
             "net_name": a.net_name}
            for a in result.partial_arcs
        ],
    }
