"""Tests for the label geography survey (scripts/survey_labels.py).

Markers: minimal, unit.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "survey_labels.py"
MINIMAL_PCB = Path(__file__).parent / "fixtures" / "minimal.kicad_pcb"


@pytest.fixture(scope="module")
def survey():
    spec = importlib.util.spec_from_file_location("survey_labels", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.minimal
@pytest.mark.unit
def test_own_and_home_columns_present(survey):
    board = survey.survey_file(str(MINIMAL_PCB))
    assert board["labels"] > 0
    for row in board["rows"]:
        assert "own_overlap_mm2" in row
        assert "home_overlap_mm2" in row
        assert row["own_overlap_mm2"] >= 0.0
        assert row["home_overlap_mm2"] is None or row["home_overlap_mm2"] >= 0.0
    # The only orbiting label has pads, so its home control is defined.
    orbiting = [r for r in board["rows"] if r["orbits"]]
    assert orbiting
    assert all(r["own_overlap_mm2"] >= 0.0 for r in orbiting)


@pytest.mark.minimal
@pytest.mark.unit
def test_summary_reports_own_and_home_hits(survey):
    board = survey.survey_file(str(MINIMAL_PCB))
    summary = survey.summarize(board)
    for key in ("own_hits", "home_hits"):
        assert key in summary
        assert summary[key]["count"] >= 0
        assert "worst" in summary[key]


@pytest.mark.unit
def test_analyze_samples_rebuilds_series(survey):
    """Trajectory analysis on the synthetic facing pair: exact join-free.

    Samples carry node + anchor + parent angle, so analysis needs no
    frame join — series iterations match the sample stream and
    home_dist matches the recorded node-anchor distance.
    """
    import math

    from kicad_autorouter.placement import PlacementParams, run_placement

    from .helpers import facing_label_pair

    model = facing_label_pair()
    prop = run_placement(
        model,
        PlacementParams(max_iterations=10, debug=True),
        record_frames=True, frame_stride=2,
    )
    doc = {"label_samples": prop.debug_trace.get("label_samples", []),
           "label_orbit_m": 0.002}
    assert doc["label_samples"]
    result = survey.analyze_samples(model, doc)
    assert result["orbit_radius_mm"] == 2.0
    assert len(result["regions"]) == 1
    labels = next(iter(result["regions"].values()))
    assert set(labels) == {"uuid-a", "uuid-b"}
    sample_iters = [s["iteration"] for s in doc["label_samples"]]
    for uuid, t in labels.items():
        assert [r["it"] for r in t["series"]] == sample_iters
        first = doc["label_samples"][0]["labels"][uuid]
        assert t["series"][0]["home_dist_mm"] == pytest.approx(
            math.hypot(first[0] - first[2], first[1] - first[3]), abs=1e-3)
        for r in t["series"]:
            assert r["own_overlap_mm2"] >= 0.0
            assert r["neigh_overlap_mm2"] >= 0.0


@pytest.mark.unit
def test_analyze_samples_zero_motion_matches_filed(survey):
    """Posed copper at iteration 0 is filed copper (no double-count).

    Regression: the anchor-minus-centroid translation double-counted
    the filed offset for off-center homes. At zero motion the series
    must equal the rect-vs-filed-copper survey value.
    """
    from shapely.geometry import Polygon

    from kicad_autorouter.board_model.labels import (
        label_board_angle as _lba,
        label_board_rect as _lbr,
        snap_label_angle as _sla,
    )
    from kicad_autorouter.placement import PlacementParams, run_placement

    from .helpers import facing_label_pair

    model = facing_label_pair()
    prop = run_placement(
        model,
        PlacementParams(max_iterations=2, debug=True),
        record_frames=True, frame_stride=1,
    )
    doc = {"label_samples": prop.debug_trace.get("label_samples", []),
           "label_orbit_m": 0.002}
    result = survey.analyze_samples(model, doc)
    labels = next(iter(result["regions"].values()))
    coppers = survey._copper_unions(model)
    by_uuid = {fp.uuid: fp for fp in model.footprints}
    s0 = doc["label_samples"][0]["labels"]
    for uuid, t in labels.items():
        fp = by_uuid[uuid]
        nx, ny, _ax, _ay, pa = s0[uuid]
        ang = _sla(_lba(float(fp.label.local_angle_deg), float(pa)))
        rect = Polygon(_lbr(fp.label.text, nx, ny, ang, fp.label.size_mm,
                            halo_mm=0.0))
        _, expect = survey._gap_or_overlap(rect, coppers[uuid])
        assert t["series"][0]["own_overlap_mm2"] == pytest.approx(
            round(expect, 4))
