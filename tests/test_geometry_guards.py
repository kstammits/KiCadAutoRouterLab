"""Degenerate-geometry guards: defined behavior instead of crashes.

Markers: unit
"""

import numpy as np
import pytest

from kicad_autorouter.board_model import (
    Point,
    _polygon_bbox,
    sample_quadratic_bezier,
)
from kicad_autorouter.placement.contact import _classify_rectangle


@pytest.mark.unit
def test_bezier_zero_segments_yields_endpoints():
    pts = list(sample_quadratic_bezier((0.0, 0.0), (1.0, 2.0), (2.0, 0.0), 0))
    assert pts == [(0.0, 0.0), (2.0, 0.0)]


@pytest.mark.unit
def test_empty_polygon_bbox_rejected_with_context():
    with pytest.raises(ValueError, match="empty polygon"):
        _polygon_bbox([])


@pytest.mark.unit
def test_degenerate_courtyard_corner_is_not_rectangle():
    verts = np.array(
        [
            [0.0, 0.0],
            [0.0, 0.0],  # repeated vertex: zero-length edge
            [2.0, 0.0],
            [2.0, 1.0],
        ]
    )
    assert _classify_rectangle(verts) is None


@pytest.mark.unit
def test_valid_rectangle_still_classifies():
    verts = np.array(
        [
            [0.0, 0.0],
            [2.0, 0.0],
            [2.0, 1.0],
            [0.0, 1.0],
        ]
    )
    assert _classify_rectangle(verts) is not None
    assert _polygon_bbox([Point(x, y) for x, y in verts]) == (0.0, 2.0, 0.0, 1.0)
