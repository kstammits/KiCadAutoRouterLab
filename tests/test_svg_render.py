"""Tests for the model -> SVG renderer."""

import math
from pathlib import Path

import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.placement import PlacementProposal
from kicad_autorouter.sexpr import parse_file
from kicad_autorouter.svg_render import render_board_svg

FIXTURES = Path(__file__).parent / "fixtures"
MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"


def _minimal_model():
    return board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))


def _proposal(dx: float, dy: float) -> PlacementProposal:
    return PlacementProposal(
        deltas={MH1_UUID: (dx, dy)},
        iterations=0,
        final_max_disp_mm=math.hypot(dx, dy),
        elapsed_s=0.0,
        params={},
    )


def test_renders_valid_document():
    m = board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))
    s = render_board_svg(m, title="t")
    assert s.startswith("<svg") and s.rstrip().endswith("</svg>")
    assert "<title>t</title>" in s


def test_minimal_draws_zones_and_outline():
    m = board_model(parse_file(FIXTURES / "minimal.kicad_pcb"))
    s = render_board_svg(m)
    # 2 keepout zones, each with F.Cu and B.Cu layers -> 4 polygons
    assert s.count("<polygon") == 4  
    assert "<line" in s              # edge cuts


def test_dccf_draws_zone_and_refs():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    s = render_board_svg(m)
    assert "<polygon" in s  # GND zone
    assert "<text " in s    # reference designators


def test_dccf_labels_unnamed_footprints_with_short_uuid():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    unnamed = [fp for fp in m.footprints if not fp.ref]
    assert unnamed  # fixture has empty-ref footprints
    s = render_board_svg(m)
    for fp in unnamed:
        assert f">{fp.uuid[:8]}</text>" in s


def test_no_overlay_without_proposal():
    s = render_board_svg(_minimal_model())
    assert 'id="move-arrow"' not in s
    assert 'stroke-dasharray="0.6 0.4"' not in s


def test_empty_deltas_render_plain():
    prop = PlacementProposal(
        deltas={}, iterations=0, final_max_disp_mm=0.0, elapsed_s=0.0, params={}
    )
    s = render_board_svg(_minimal_model(), proposal=prop)
    assert 'id="move-arrow"' not in s


def test_overlay_draws_ghost_and_arrow():
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    fp = next(f for f in m.footprints if f.courtyard)  # minimal fixture has none
    dx, dy = 2.0, -1.0
    prop = PlacementProposal(
        deltas={fp.uuid: (dx, dy)},
        iterations=0,
        final_max_disp_mm=math.hypot(dx, dy),
        elapsed_s=0.0,
        params={},
    )
    s = render_board_svg(m, proposal=prop)
    assert 'id="move-arrow"' in s

    # Renderer now uses board coordinates directly (Y-up = SVG Y-down)
    # No Y-flip applied
    arrow = (
        f'<line x1="{fp.x_mm:.3f}" y1="{fp.y_mm:.3f}" '
        f'x2="{(fp.x_mm + dx):.3f}" y2="{(fp.y_mm + dy):.3f}" '
        'marker-end="url(#move-arrow)"/>'
    )
    assert arrow in s

    a, b = fp.courtyard[0]
    ghost = (
        f'<line x1="{a.x_mm:.3f}" y1="{a.y_mm:.3f}" '
        f'x2="{b.x_mm:.3f}" y2="{b.y_mm:.3f}"/>'
    )
    # Ghost courtyard at original position (dashed, gray)
    assert ghost in s

    # Moved courtyard baked at the proposed position inside the footprint
    # group (no group transform): what you see is what Accept writes.
    assert "translate(" not in s
    shifted = (
        f'<line x1="{(a.x_mm + dx):.3f}" y1="{(a.y_mm + dy):.3f}" '
        f'x2="{(b.x_mm + dx):.3f}" y2="{(b.y_mm + dy):.3f}"'
    )
    assert shifted in s


def test_viewbox_grows_with_max_move():
    m = _minimal_model()

    def vb_width(s: str) -> float:
        return float(s.split('viewBox="')[1].split('"')[0].split()[2])

    plain = render_board_svg(m)
    dx, dy = 5.0, 0.0
    moved = render_board_svg(m, proposal=_proposal(dx, dy))
    assert vb_width(moved) == pytest.approx(vb_width(plain) + 2 * math.hypot(dx, dy))


def _tube111_model():
    return board_model(parse_file(FIXTURES / "tube111.kicad_pcb"))


def test_preview_matches_accept_with_rotation():
    """Preview footprint groups must equal the post-Accept render.

    Regression for the C11-on-DCCF bug: Step showed the part in one spot
    (group transform rotating about the courtyard centroid, SVG-clockwise)
    while Accept wrote it elsewhere (rotation about the footprint origin,
    board-CCW). Preview now renders apply_deltas() output, so every
    footprint group is byte-identical to rendering the accepted model.
    """
    import re

    from kicad_autorouter.board_model import apply_deltas

    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    c11 = next(f for f in m.footprints if f.ref == "C11")
    sw6 = next(f for f in m.footprints if f.ref == "SW6")  # 90-degree part
    deltas = {c11.uuid: (-1.58, -2.42, 25.45), sw6.uuid: (0.5, 1.0, -30.0)}
    prop = PlacementProposal(
        deltas=deltas, iterations=5, final_max_disp_mm=2.9,
        elapsed_s=0.0, params={},
    )
    preview = render_board_svg(m, proposal=prop)
    accepted = render_board_svg(apply_deltas(m, deltas))

    def groups(svg):
        return {
            mm[1]: mm[0] + mm[2]
            for mm in re.findall(
                r'<g class="([^"]*)" data-fp-uuid="([^"]+)"(.*?)</g>',
                svg, re.DOTALL,
            )
        }

    g_preview, g_accepted = groups(preview), groups(accepted)
    assert len(g_preview) == len(g_accepted) == len(m.footprints)
    assert [u for u in g_preview if g_preview[u] != g_accepted.get(u)] == []
    assert "translate(" not in preview


def test_tube111_back_footprints_inside_region_in_svg():
    """D6/D7 courtyards must render inside board region 2 (not mirrored out).

    Regression for the B.Cu mirror bug: the model used to mirror B.Cu local
    X, so D6 stuck out the left edge (112.34 < 115.55) and D7 the right
    edge (149.56 > 143.55) while pcbnew showed both inside.
    """
    import re

    m = _tube111_model()
    region = m.board_regions[2]
    min_x, max_x, min_y, max_y = region.bbox
    s = render_board_svg(m)
    for ref, uuid_suffix in (("D6", "61efabca"), ("D7", "61efabe9")):
        fp = next(
            f for f in m.footprints
            if f.ref == ref and f.uuid.endswith(uuid_suffix)
        )
        # Extract this footprint's group and its courtyard line coords.
        grp = re.search(
            rf'<g class="[^"]*" data-fp-uuid="{re.escape(fp.uuid)}".*?</g>',
            s, re.DOTALL,
        )
        assert grp, f"no SVG group for {ref}"
        xs, ys = [], []
        for ln in re.finditer(
            r'<line x1="([\d.]+)" y1="([\d.]+)" '
            r'x2="([\d.]+)" y2="([\d.]+)"', grp.group(0),
        ):
            xs += [float(ln.group(1)), float(ln.group(3))]
            ys += [float(ln.group(2)), float(ln.group(4))]
        assert xs, f"no courtyard lines for {ref}"
        assert min(xs) >= min_x - 1e-6 and max(xs) <= max_x + 1e-6
        assert min(ys) >= min_y - 1e-6 and max(ys) <= max_y + 1e-6


def test_tube111_back_pad_rect_uses_effective_angle():
    """Q1 (B.Cu, fp 90, pads file-angle 90) renders pads rotated 90.

    The file stores each pad's effective board orientation, so the renderer
    must use it directly instead of combining it with the footprint angle
    (which double-counts and draws the rect unrotated).
    """
    import re

    m = _tube111_model()
    s = render_board_svg(m)
    q1 = m.by_ref["Q1"]
    grp = re.search(
        rf'<g class="[^"]*" data-fp-uuid="{re.escape(q1.uuid)}".*?</g>',
        s, re.DOTALL,
    )
    assert grp
    # Pad 1 is a 1.05 x 0.8 rect at board (130.63, 94.345); effective
    # orientation 90 must appear as rotate(90 ...) in the SVG.
    assert 'rotate(90.000 130.630 94.345)' in grp.group(0)


def test_preview_rotates_square_pads_with_part():
    """DCCF Q1 pad 1 (rect 1.7x1.7, file-angle 270) advances by da in preview.

    Regression: apply_deltas used to keep pad angles stale, so Step showed
    the courtyard/positions rotated while square pads stayed unrotated.
    """
    import re

    from kicad_autorouter.board_model import apply_deltas

    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    q1 = m.by_ref["Q1"]
    assert q1.pads[0].angle_deg == 270.0
    prop = PlacementProposal(
        deltas={q1.uuid: (1.5, -2.25, 25.0)}, iterations=5,
        final_max_disp_mm=2.7, elapsed_s=0.0, params={},
    )
    preview = render_board_svg(m, proposal=prop)
    accepted = render_board_svg(apply_deltas(m, prop.deltas))
    grp_preview = re.search(
        rf'<g class="[^"]*" data-fp-uuid="{re.escape(q1.uuid)}".*?</g>',
        preview, re.DOTALL,
    ).group(0)
    grp_accepted = re.search(
        rf'<g class="[^"]*" data-fp-uuid="{re.escape(q1.uuid)}".*?</g>',
        accepted, re.DOTALL,
    ).group(0)
    # 270 + 25 = 295 must appear (not stale 270), identically in both.
    assert "rotate(295.000" in grp_preview
    assert "rotate(270.000" not in grp_preview
    assert grp_preview == grp_accepted


def _c11_uuid(m):
    return next(f.uuid for f in m.footprints if f.ref == "C11")


def test_forces_overlay_per_cause_arrows():
    """Each force cause gets its own colored arrow + marker."""
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    uuid = _c11_uuid(m)
    prop = PlacementProposal(
        deltas={}, iterations=0, final_max_disp_mm=0.0, elapsed_s=0.0,
        params={},
        forces={uuid: (6.0, 0.0)},
        force_components={uuid: {
            "repulsion": (10.0, 0.0),
            "attraction": (-4.0, 0.0),
            "rigid": (0.0, 0.0),
            "ghost": (0.0, 0.0),
            "courtyard": (0.0, 3.0),
            "boundary": (0.0, 0.0),
        }},
    )
    s = render_board_svg(m, proposal=prop, show_forces=True)
    for cause in ("repulsion", "attraction", "courtyard"):
        assert f"url(#force-{cause})" in s
        assert f'id="force-{cause}"' in s
    # Negligible rigid/ghost/boundary contributions are hidden.
    assert "url(#force-rigid)" not in s
    assert "url(#force-ghost)" not in s
    assert "url(#force-boundary)" not in s


def test_forces_overlay_falls_back_to_total():
    """Proposals without a breakdown keep the legacy single arrow."""
    m = board_model(parse_file(FIXTURES / "DCCF.sved.kicad_pcb"))
    uuid = _c11_uuid(m)
    prop = PlacementProposal(
        deltas={}, iterations=0, final_max_disp_mm=0.0, elapsed_s=0.0,
        params={},
        forces={uuid: (10.0, 0.0)},
    )
    s = render_board_svg(m, proposal=prop, show_forces=True)
    assert "url(#move-arrow)" in s
    assert "url(#force-" not in s
