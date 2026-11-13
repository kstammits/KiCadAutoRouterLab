#!/usr/bin/env python3
"""Survey label geography: where labels sit vs five obstacle classes.

For every labeled footprint in each input board, measures the ink-bbox
rect (no halo — where the glyphs are, not the collision model) against:

- neighbor courtyards (gap mm, or overlap mm^2 when intersecting),
- neighbor pad copper (same; own footprint excluded),
- own pad copper (same; the structural floor — small parts cover their
  own pads even at home),
- other labels (same),
- Edge.Cuts overhang (area mm^2 outside the board outline + max corner
  excursion mm).

Also records orbit usage (label-center distance to the parent pad
centroid vs the orbit radius) and the home control: the same rect
re-posed at home (parent pad centroid, same board angle) measured
against own pad copper. Home overlap is the structural floor the orbit
can never clear — only the excess of actual over home is movable.

This is telemetry for sizing the orbit/slack defaults
(``label_orbit_m``/``label_slack_m``) — not a regression gate. Outputs
JSON for ``docs/benchmarks/`` (untracked) plus a printed summary table.

Usage:
    .venv/bin/python scripts/survey_labels.py [--json-out PATH] FILES...
    .venv/bin/python scripts/survey_labels.py tests/fixtures/tube111.kicad_pcb tmp/tube112.kicad_pcb tmp/tube113.kicad_pcb tmp/tube114.kicad_pcb
    .venv/bin/python scripts/survey_labels.py --from-samples TRAJ.json [--board FILE] [--json-out PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from shapely.geometry import LineString, Polygon  # noqa: E402
from shapely.ops import polygonize, unary_union  # noqa: E402

from kicad_autorouter.board_model import (  # noqa: E402
    board_model,
    label_board_pose,
)
from kicad_autorouter.board_model.labels import label_board_rect  # noqa: E402
from kicad_autorouter.placement.contact import pad_copper_polygon  # noqa: E402
from kicad_autorouter.sexpr import parse_file  # noqa: E402


def _courtyard_poly(fp):
    """Courtyard outline polygon from segments, or None."""
    lines = []
    for a, b in fp.courtyard or ():
        try:
            lines.append(LineString([
                (float(a.x_mm), float(a.y_mm)),
                (float(b.x_mm), float(b.y_mm)),
            ]))
        except (TypeError, ValueError):
            continue
    if not lines:
        return None
    try:
        polys = list(polygonize(lines))
    except Exception:
        return None
    if not polys:
        return None
    return max(polys, key=lambda p: p.area)


def _label_poly(fp, pose):
    cx, cy, angle = pose
    try:
        corners = label_board_rect(
            fp.label.text, float(cx), float(cy), float(angle),
            fp.label.size_mm, halo_mm=0.0)
    except (TypeError, ValueError):
        return None
    if not corners:
        return None
    try:
        return Polygon(corners)
    except Exception:
        return None


def _gap_or_overlap(rect, obstacle):
    """(gap_mm, overlap_mm2): gap when disjoint, overlap when intersecting."""
    try:
        if rect.intersects(obstacle):
            inter = rect.intersection(obstacle)
            return 0.0, float(inter.area)
        return float(rect.distance(obstacle)), 0.0
    except Exception:
        return float("nan"), 0.0


def _copper_unions(model):
    """Filed pad-copper union per footprint uuid (None when pad-less)."""
    coppers = {}
    for fp in model.footprints:
        if not fp.uuid:
            continue
        polys = []
        for pad in fp.pads or ():
            try:
                poly = pad_copper_polygon(pad)
            except Exception:
                continue
            if poly is not None and not poly.is_empty:
                polys.append(poly)
        coppers[fp.uuid] = unary_union(polys) if polys else None
    return coppers


def _pad_centroid(fp):
    """Filed pad centroid (orbit home at setup), or None when pad-less."""
    try:
        xs = [float(p.position.x_mm) for p in fp.pads or ()]
        ys = [float(p.position.y_mm) for p in fp.pads or ()]
    except (TypeError, ValueError):
        return None
    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def survey_file(path):
    model = board_model(parse_file(Path(path)))
    regions = []
    for reg in model.board_regions or ():
        try:
            pts = [(float(p.x_mm), float(p.y_mm)) for p in reg.polygon]
        except (TypeError, ValueError):
            continue
        if len(pts) >= 3:
            try:
                regions.append(Polygon(pts))
            except Exception:
                continue
    board = unary_union(regions) if regions else None

    labeled = [fp for fp in model.footprints
               if fp.label is not None and fp.uuid]
    poses, rects = {}, {}
    for fp in labeled:
        try:
            pose = label_board_pose(fp)
        except Exception:
            continue
        if pose is None:
            continue
        poly = _label_poly(fp, pose)
        if poly is None or poly.is_empty:
            continue
        poses[fp.uuid] = (fp, pose)
        rects[fp.uuid] = poly

    courts, coppers = {}, _copper_unions(model)
    for fp in model.footprints:
        if not fp.uuid:
            continue
        courts[fp.uuid] = _courtyard_poly(fp)

    rows = []
    for uuid, fp_pose in poses.items():
        fp, pose = fp_pose
        rect = rects[uuid]
        # Orbiting = the engine integrates this label (movable parent with
        # pads). Locked parents weld their labels; pad-less graphical parts
        # (e.g. G*** tube art, hidden REF** on mounting holes) never enter
        # the simulation or the SVG — report them separately, not as hits.
        orbits = (not fp.locked) and len(fp.pads or ()) > 0
        # Neighbor courtyards.
        best_gap, best_ov, best_ref = float("inf"), 0.0, None
        for ou, poly in courts.items():
            if ou == uuid or poly is None or poly.is_empty:
                continue
            gap, ov = _gap_or_overlap(rect, poly)
            if ov > best_ov or (best_ov == 0.0 and gap < best_gap):
                best_gap, best_ov = gap, ov
                if ov > 0:
                    best_ref = next(
                        (f.ref for f in model.footprints if f.uuid == ou), ou)
        # Neighbor pad copper (own footprint excluded).
        pad_gap, pad_ov, pad_ref = float("inf"), 0.0, None
        for ou, poly in coppers.items():
            if ou == uuid or poly is None or poly.is_empty:
                continue
            gap, ov = _gap_or_overlap(rect, poly)
            if ov > pad_ov or (pad_ov == 0.0 and gap < pad_gap):
                pad_gap, pad_ov = gap, ov
                if ov > 0:
                    pad_ref = next(
                        (f.ref for f in model.footprints if f.uuid == ou), ou)
        # Other labels.
        lab_gap, lab_ov = float("inf"), 0.0
        for ou, other in rects.items():
            if ou == uuid:
                continue
            gap, ov = _gap_or_overlap(rect, other)
            lab_ov += ov
            lab_gap = min(lab_gap, gap)
        # Edge overhang.
        over_area, over_max = 0.0, 0.0
        if board is not None and not board.is_empty:
            try:
                if not board.covers(rect):
                    outside = rect.difference(board)
                    over_area = float(outside.area)
                    for x, y in rect.exterior.coords:
                        from shapely.geometry import Point as _Pt
                        if not board.covers(_Pt(x, y)):
                            over_max = max(over_max, float(_Pt(x, y).distance(board)))
            except Exception:
                pass
        # Orbit usage: label center vs parent pad centroid. Home doubles
        # as the control pose: the same rect re-posed at home, measured
        # against own pad copper, is the structural floor (small parts
        # cover their own pads even centered). Only actual-over-home
        # excess is movable.
        home = _pad_centroid(fp)
        home_dist = math.hypot(pose[0] - home[0], pose[1] - home[1]) \
            if home is not None else float("nan")
        # Own pad copper (own footprint included — excluded above).
        own_gap, own_ov = float("inf"), 0.0
        own_poly = coppers.get(uuid)
        if own_poly is not None and not own_poly.is_empty:
            own_gap, own_ov = _gap_or_overlap(rect, own_poly)
        # Home control: rect at home (same board angle) vs own copper.
        # None when the parent has no pads (no home) or no own copper.
        home_ov = None
        if home is not None and own_poly is not None \
                and not own_poly.is_empty:
            try:
                home_rect = _label_poly(fp, (home[0], home[1], pose[2]))
            except (TypeError, ValueError):
                home_rect = None
            if home_rect is not None and not home_rect.is_empty:
                _, home_ov = _gap_or_overlap(home_rect, own_poly)
        rows.append({
            "ref": fp.ref, "uuid": uuid, "orbits": orbits,
            "pos": [round(pose[0], 3), round(pose[1], 3), round(pose[2], 2)],
            "court_gap_mm": round(best_gap, 3) if math.isfinite(best_gap) else None,
            "court_overlap_mm2": round(best_ov, 4),
            "court_ref": best_ref,
            "pad_gap_mm": round(pad_gap, 3) if math.isfinite(pad_gap) else None,
            "pad_overlap_mm2": round(pad_ov, 4),
            "pad_ref": pad_ref,
            "own_gap_mm": round(own_gap, 3) if math.isfinite(own_gap) else None,
            "own_overlap_mm2": round(own_ov, 4),
            "home_overlap_mm2": round(home_ov, 4) if home_ov is not None else None,
            "label_gap_mm": round(lab_gap, 3) if math.isfinite(lab_gap) else None,
            "label_overlap_mm2": round(lab_ov, 4),
            "edge_overhang_mm2": round(over_area, 4),
            "edge_excursion_mm": round(over_max, 3),
            "home_dist_mm": round(home_dist, 3) if math.isfinite(home_dist) else None,
        })
    return {"file": str(path), "labels": len(rows), "rows": rows}


def analyze_samples(model, doc):
    """Rebuild per-label overlap trajectories from stride samples.

    ``model`` is the filed board the instrumented run started from;
    ``doc`` carries ``label_samples`` (node + parent-carried anchor +
    parent board angle per orbiting label, stride-sampled) and
    ``label_orbit_m``. Each sample is exact rigid motion: the anchor is
    the posed pad centroid (translation vs the filed centroid) and the
    recorded parent angle gives rotation — copper is posed by
    rotate-about-filed-centroid + translate, no re-fitting.

    Same-region neighbors are posed from their own sample at the same
    iteration; static and cross-region copper stays filed (approximation:
    cross-region parts live on other outlines, far away). Returns
    per-region, per-label series plus summaries (own t0/end/min,
    rise count, rim onset, worst neighbor sit).
    """
    from shapely.affinity import rotate as _rot
    from shapely.affinity import translate as _mov

    from kicad_autorouter.board_model.labels import (  # noqa: E402
        label_board_angle as _lba,
        snap_label_angle as _sla,
    )

    by_uuid = {fp.uuid: fp for fp in model.footprints if fp.uuid}
    filed_copper = _copper_unions(model)
    filed_home = {u: _pad_centroid(fp) for u, fp in by_uuid.items()}
    # Per-region sample streams, sorted by iteration. The home offset
    # (anchor minus pad centroid) is read off the t0 sample per label:
    # anchor(t) = centroid(t) + R(t).offset holds for any rigid motion
    # under any home definition (filed anchor, centroid, ...), so the
    # current centroid — and hence the posed copper — recovers exactly
    # without knowing which home the engine used.
    regions = {}
    for s in doc.get("label_samples", []):
        regions.setdefault(s.get("region"), []).append(s)
    for ss in regions.values():
        ss.sort(key=lambda s: s.get("iteration", 0))
    home_offset = {}
    for ss in regions.values():
        if not ss:
            continue
        labmap = ss[0].get("labels", {}) or {}
        for uuid, vals in labmap.items():
            if len(vals) < 5 or uuid in home_offset:
                continue
            home = filed_home.get(uuid)
            if home is None:
                continue
            home_offset[uuid] = (vals[2] - home[0], vals[3] - home[1])
    filed_angle = {}
    for u, fp in by_uuid.items():
        try:
            filed_angle[u] = float(fp.angle_deg)
        except (TypeError, ValueError):
            filed_angle[u] = 0.0
    try:
        orbit_r = float(doc.get("label_orbit_m", 0.002)) * 1000.0
    except (TypeError, ValueError):
        orbit_r = 2.0

    def _posed(poly, fp_uuid, ax, ay, da):
        """Pose filed copper by the parent's rigid motion at a sample.

        Rotation da about the filed centroid; translation from the
        current centroid, recovered as anchor-now minus the posed home
        offset (read off the t0 sample — home-definition agnostic).
        """
        home = filed_home.get(fp_uuid)
        if poly is None or poly.is_empty or home is None:
            return poly
        try:
            rad = math.radians(da)
            ca, sa = math.cos(rad), math.sin(rad)
            ox, oy = home_offset.get(fp_uuid, (0.0, 0.0))
            # Current centroid = anchor now - posed home offset.
            ccx = ax - (ox * ca - oy * sa)
            ccy = ay - (ox * sa + oy * ca)
            dx, dy = ccx - home[0], ccy - home[1]
            if abs(da) > 1e-12:
                poly = _rot(poly, da, origin=home, use_radians=False)
            if abs(dx) > 1e-12 or abs(dy) > 1e-12:
                poly = _mov(poly, xoff=dx, yoff=dy)
        except Exception:
            return None
        return poly

    def _bounds_hit(rect, poly):
        try:
            rminx, rminy, rmaxx, rmaxy = rect.bounds
            pminx, pminy, pmaxx, pmaxy = poly.bounds
        except Exception:
            return True
        return not (rmaxx < pminx or pmaxx < rminx
                    or rmaxy < pminy or pmaxy < rminy)

    out = {"orbit_radius_mm": round(orbit_r, 3), "regions": {}}
    for region, ss in regions.items():
        series = {}
        for s in ss:
            it = s.get("iteration", 0)
            labmap = s.get("labels", {}) or {}
            # Posed copper for every sampled (same-region, movable)
            # parent at this iteration; everyone else stays filed.
            posed = {}
            for uuid, vals in labmap.items():
                if len(vals) < 5:
                    continue
                home = filed_home.get(uuid)
                if home is None:
                    continue
                da = vals[4] - filed_angle.get(uuid, 0.0)
                base = filed_copper.get(uuid)
                posed[uuid] = _posed(base, uuid, vals[2], vals[3], da) \
                    if base is not None else None
            for uuid, vals in labmap.items():
                fp = by_uuid.get(uuid)
                if fp is None or fp.label is None or len(vals) < 5:
                    continue
                nx, ny = vals[0], vals[1]
                try:
                    ang = _sla(_lba(float(fp.label.local_angle_deg),
                                    float(vals[4])))
                except (TypeError, ValueError):
                    continue
                rect = _label_poly(fp, (nx, ny, ang))
                if rect is None or rect.is_empty:
                    continue
                try:
                    hd = math.hypot(vals[0] - vals[2], vals[1] - vals[3])
                except (TypeError, ValueError):
                    hd = float("nan")
                own_ov = 0.0
                own = posed.get(uuid)
                if own is not None and not own.is_empty \
                        and _bounds_hit(rect, own):
                    _, own_ov = _gap_or_overlap(rect, own)
                n_ov, n_ref = 0.0, None
                for ou, poly in filed_copper.items():
                    if ou == uuid or poly is None or poly.is_empty:
                        continue
                    cand = posed.get(ou, poly)
                    if cand is None or cand.is_empty \
                            or not _bounds_hit(rect, cand):
                        continue
                    _, ov = _gap_or_overlap(rect, cand)
                    if ov > n_ov:
                        n_ov = ov
                        n_ref = next(
                            (f.ref for f in model.footprints
                             if f.uuid == ou), ou)
                ent = series.setdefault(uuid, {"ref": fp.ref, "rows": []})
                ent["rows"].append({
                    "it": it,
                    "home_dist_mm": round(hd, 3)
                    if math.isfinite(hd) else None,
                    "own_overlap_mm2": round(own_ov, 4),
                    "neigh_overlap_mm2": round(n_ov, 4),
                    "neigh_ref": n_ref,
                })
        reg = out["regions"][str(region)] = {}
        for uuid, ent in series.items():
            rows = ent["rows"]
            owns = [r["own_overlap_mm2"] for r in rows]
            neighs = [r["neigh_overlap_mm2"] for r in rows]
            hds = [r["home_dist_mm"] for r in rows
                   if r["home_dist_mm"] is not None]
            rises = sum(1 for a, b in zip(owns, owns[1:])
                        if b - a > 1e-9)
            rim_onset = next(
                (r["it"] for r in rows
                 if r["home_dist_mm"] is not None
                 and r["home_dist_mm"] >= 0.95 * orbit_r),
                None)
            imax = max(range(len(neighs)), key=lambda i: neighs[i])
            reg[uuid] = {
                "ref": ent["ref"],
                "own_t0_mm2": owns[0] if owns else 0.0,
                "own_end_mm2": owns[-1] if owns else 0.0,
                "own_min_mm2": min(owns) if owns else 0.0,
                "own_rises": rises,
                "rim_onset_iter": rim_onset,
                "rim_end_mm": hds[-1] if hds else None,
                "neigh_max_mm2": neighs[imax] if neighs else 0.0,
                "neigh_max_ref": rows[imax]["neigh_ref"] if rows else None,
                "neigh_max_iter": rows[imax]["it"] if rows else None,
                "neigh_end_mm2": neighs[-1] if neighs else 0.0,
                "series": rows,
            }
    return out


def summarize(board):
    rows = board["rows"]
    n = len(rows)
    out = {"file": board["file"], "labels": n}

    def dist(key):
        vals = sorted(r[key] for r in rows if r[key] is not None)
        if not vals:
            return {"n": 0}
        q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
        return {"n": len(vals), "min": vals[0], "p50": q(0.5),
                "p90": q(0.9), "max": vals[-1]}

    def hits(key, thresh=0.0, orbiting_only=True):
        pool = [r for r in rows if not orbiting_only or r["orbits"]]
        hs = [(r["ref"], r[key]) for r in pool if (r[key] or 0.0) > thresh]
        return {"count": len(hs),
                "worst": sorted(hs, key=lambda t: -t[1])[:10]}

    out["orbiting"] = sum(1 for r in rows if r["orbits"])
    out["static"] = len(rows) - out["orbiting"]
    out["court_gap"] = dist("court_gap_mm")
    out["pad_gap"] = dist("pad_gap_mm")
    out["label_gap"] = dist("label_gap_mm")
    out["home_dist"] = dist("home_dist_mm")
    out["court_hits"] = hits("court_overlap_mm2")
    out["pad_hits"] = hits("pad_overlap_mm2")
    out["own_hits"] = hits("own_overlap_mm2")
    out["home_hits"] = hits("home_overlap_mm2")
    out["label_hits"] = hits("label_overlap_mm2")
    edge = [(r["ref"], r["edge_overhang_mm2"]) for r in rows
            if r["orbits"] and (r["edge_overhang_mm2"] or 0.0) > 0.0]
    out["edge_hits"] = {"count": len(edge),
                        "worst": sorted(edge, key=lambda t: -t[1])[:10]}
    return out


def _print_trajectories(result):
    print(f"orbit_radius={result['orbit_radius_mm']}mm")
    for region, labels in result["regions"].items():
        print(f"-- region {region} ({len(labels)} labels)")
        for uuid, t in sorted(labels.items(),
                              key=lambda kv: -kv[1]["own_end_mm2"]):
            print(f"  {t['ref']:6s} own t0={t['own_t0_mm2']:.3f} "
                  f"end={t['own_end_mm2']:.3f} min={t['own_min_mm2']:.3f} "
                  f"rises={t['own_rises']} rim_onset={t['rim_onset_iter']} "
                  f"rim_end={t['rim_end_mm']} "
                  f"neigh_max={t['neigh_max_mm2']:.3f}"
                  f"({t['neigh_max_ref']}@{t['neigh_max_iter']}) "
                  f"neigh_end={t['neigh_end_mm2']:.3f}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("files", nargs="*")
    p.add_argument("--json-out", default=None)
    p.add_argument("--from-samples", default=None,
                   help="trajectory JSON (frames + label_samples) from an "
                   "instrumented run; analyzes overlap series instead of "
                   "surveying board files")
    p.add_argument("--board", default=None,
                   help="filed board the sampled run started from "
                   "(defaults to the path stored in the samples doc)")
    args = p.parse_args(argv)
    if args.from_samples:
        doc = json.loads(Path(args.from_samples).read_text(encoding="utf-8"))
        board_path = args.board or doc.get("board")
        if not board_path:
            raise SystemExit("--board is required (samples doc has no path)")
        result = analyze_samples(
            board_model(parse_file(Path(board_path))), doc)
        _print_trajectories(result)
        if args.json_out:
            out = Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=1) + "\n",
                           encoding="utf-8")
            print(f"wrote {out}")
        return 0
    if not args.files:
        raise SystemExit("need board FILES (or --from-samples)")
    boards = [survey_file(f) for f in args.files]
    summary = [summarize(b) for b in boards]
    for s in summary:
        print(f"== {s['file']} ({s['labels']} labels, "
              f"{s['orbiting']} orbiting / {s['static']} static)")
        for key in ("court_gap", "pad_gap", "label_gap", "home_dist"):
            d = s[key]
            if d.get("n"):
                print(f"  {key:10s} n={d['n']} min={d['min']} "
                      f"p50={d['p50']} p90={d['p90']} max={d['max']}")
        for key in ("court_hits", "pad_hits", "own_hits", "home_hits",
                     "label_hits", "edge_hits"):
            h = s[key]
            print(f"  {key:10s} count={h['count']}")
            for ref, mag in h["worst"][:5]:
                print(f"      {ref}: {mag}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"boards": boards, "summary": summary}, indent=1) + "\n",
            encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
