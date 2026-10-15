"""Padless graphical-only parts: no physics, no SVG, no /api/footprints.

Graphical footprints (logos, snail art, title blocks) carry Fab/Silk art
but zero ``(pad ...)`` children. Since courtyard imputation (Fab -> SilkS
fallback), their decorative art became thousands of courtyard segments
(DCCF: 2x snail @7247 + 2x WEEE logo @519 segs) that flooded the SVG DOM
and acted as bogus placement collision obstacles.

They stay in the model (import untouched, writeback is tree-based), but:
- placement ignores them (no collision for/against them),
- the SVG renderer skips them (and their art no longer inflates the viewBox),
- GET /api/footprints omits them (ghosts unaffected throughout).
"""

from dataclasses import replace
from pathlib import Path

import pytest

from kicad_autorouter.board_model import (
    BoardModel,
    BoardRegion,
    Footprint,
    board_model,
)
from kicad_autorouter.placement import PlacementParams, run_placement
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.svg_render import _bounds, render_board_svg

from .helpers import make_fp as make_test_fp
from .helpers import make_rect_courtyard

FIXTURES = Path(__file__).parent / "fixtures"
DCCF = FIXTURES / "DCCF.sved.kicad_pcb"

UUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UUID_B = "bbbbbbbb-0000-0000-0000-000000000002"
UUID_ART = "cccccccc-0000-0000-0000-000000000003"


def _two_squares_with_logo():
    """Two 1-pad squares 2mm apart + a padless logo overlapping both."""
    fpa = make_test_fp("A1", UUID_A, 0.0, 0.0, ("N1",),
                       footprint_id="T:R_0805", hw=1.0, hh=1.0,
                       pad_shape="circle", pad_layers=())
    fpb = make_test_fp("B1", UUID_B, 4.0, 0.0, ("N2",),
                       footprint_id="T:R_0805", hw=1.0, hh=1.0,
                       pad_shape="circle", pad_layers=())
    logo = Footprint(
        ref="LOGO", footprint_id="Symbol:snail4", layer="F.Cu",
        x_mm=2.0, y_mm=0.0, angle_deg=0.0, pads=(),
        courtyard=make_rect_courtyard(2.0, 0.0, hw=6.0, hh=6.0),
        locked=False, uuid=UUID_ART, ghost=False, ghost_attractions=(),
    )
    region = BoardRegion(uuid="r0", polygon=(), bbox=(-10.0, 20.0, -10.0, 10.0))
    base = BoardModel(
        footprints=(fpa, fpb),
        keepout_zones=(),
        nets={},
        by_ref={"A1": fpa, "B1": fpb},
        board_regions=(region,),
        footprint_region={fpa.uuid: 0, fpb.uuid: 0},
    )
    with_logo = replace(
        base,
        footprints=(fpa, fpb, logo),
        footprint_region={fpa.uuid: 0, fpb.uuid: 0, logo.uuid: 0},
    )
    return base, with_logo


def _params():
    return PlacementParams(
        max_iterations=0, repulsion_k_N_m2=1e-09,
        wall_k_N=9.999999999999999e-05, wall_halo_m=0.003,
    )


@pytest.mark.unit
def test_padless_logo_does_not_change_forces():
    """The overlapping logo must not push the squares (0-iter force probe)."""
    base, with_logo = _two_squares_with_logo()
    rb = run_placement(base, _params())
    rl = run_placement(with_logo, _params())
    assert set(rl.deltas) == set(rb.deltas) == set()
    for uuid in (UUID_A, UUID_B):
        for cause in ("courtyard", "repulsion"):
            b = rb.force_components[uuid][cause]
            lv = rl.force_components[uuid][cause]
            assert b == pytest.approx(lv), (uuid, cause, b, lv)


@pytest.mark.unit
def test_padless_never_gets_deltas_or_forces():
    base, with_logo = _two_squares_with_logo()
    r = run_placement(
        with_logo,
        PlacementParams(max_iterations=5, convergence_eps_m=1e-15),
    )
    assert UUID_ART not in r.deltas
    assert UUID_ART not in r.forces
    assert UUID_ART not in r.force_components
    # ... and the run matches the logo-free model exactly.
    r2 = run_placement(
        base, PlacementParams(max_iterations=5, convergence_eps_m=1e-15)
    )
    assert set(r.deltas) == set(r2.deltas)
    for uuid, d in r2.deltas.items():
        assert tuple(r.deltas[uuid]) == pytest.approx(tuple(d))


@pytest.mark.unit
def test_padless_hidden_from_svg():
    base, with_logo = _two_squares_with_logo()
    plain = render_board_svg(base)
    assert UUID_ART not in render_board_svg(with_logo)
    assert f'data-fp-uuid="{UUID_A}"' in plain
    # Logo's 4 courtyard segments add no lines when hidden.
    assert render_board_svg(with_logo).count("<line") == plain.count("<line")


@pytest.mark.unit
def test_padless_art_does_not_inflate_bounds():
    m = board_model(parse_file(DCCF))
    padless = [f for f in m.footprints if not f.pads]
    assert padless  # fixture has snail + WEEE-logo parts
    before = _bounds(m)
    far = []
    for f in padless:
        moved = tuple(
            (replace(a, x_mm=a.x_mm + 5000.0), replace(b, x_mm=b.x_mm + 5000.0))
            for a, b in f.courtyard
        )
        far.append(replace(f, courtyard=moved))
    kept = [f for f in m.footprints if f.pads]
    shifted = replace(m, footprints=tuple(kept + far))
    assert _bounds(shifted) == pytest.approx(before)


@pytest.mark.dccf
def test_dccf_padless_hidden_from_svg_groups():
    import re

    m = board_model(parse_file(DCCF))
    padless_uuids = {f.uuid for f in m.footprints if not f.pads}
    assert len(padless_uuids) == 4
    svg = render_board_svg(m)
    groups = set(re.findall(r'data-fp-uuid="([^"]+)"', svg))
    assert not (groups & padless_uuids)
    assert len(groups) == sum(1 for f in m.footprints if f.pads)


@pytest.mark.server
@pytest.mark.dccf
def test_footprints_api_omits_padless():
    import json
    import sys
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    import server

    server.STATE.model = None
    server.STATE.sch = None
    server.STATE.name = ""
    server.STATE.version = 0
    server.STATE.proposal = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        data = DCCF.read_bytes()
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/load?name=dccf",
            data=data, method="POST",
        )
        with urllib.request.urlopen(req) as r:
            assert json.load(r)["ok"] is True
        with urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/footprints"
        ) as r:
            payload = json.load(r)
        uuids = {fp["uuid"] for fp in payload["footprints"]}
        m = board_model(parse_file(DCCF))
        padless_uuids = {f.uuid for f in m.footprints if not f.pads}
        assert padless_uuids and not (uuids & padless_uuids)
        assert len(uuids) == sum(
            1 for f in m.footprints if f.pads and f.uuid
        )
    finally:
        httpd.shutdown()
