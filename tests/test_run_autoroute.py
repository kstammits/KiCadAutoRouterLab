"""Tests for the headless placement entry point (scripts/run_autoroute.py)."""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "run_autoroute.py"
MINIMAL_PCB = Path(__file__).parent / "fixtures" / "minimal.kicad_pcb"
MH1_UUID = "5c0af984-49c4-40a0-95aa-bb612ff4098b"  # the only unlocked part
MH2_UUID = "77dd5b36-4c98-4592-ae76-c20d6e053872"  # locked


@pytest.fixture(scope="module")
def run_autoroute():
    spec = importlib.util.spec_from_file_location("run_autoroute", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _at_by_uuid(tree):
    result = {}
    for node in tree.children("footprint"):
        u = node.find("uuid")
        if u is not None and u.args:
            at = node.find("at")
            result[str(u.args[0])] = (float(at.args[0]), float(at.args[1]))
    return result


def test_identity_default(tmp_path, run_autoroute):
    from kicad_autorouter.sexpr import parse_file

    out = tmp_path / "out.kicad_pcb"
    assert run_autoroute.main([str(MINIMAL_PCB), "-o", str(out)]) == 0
    # re-serialization is not byte-identical; compare parsed trees instead
    assert parse_file(out) == parse_file(MINIMAL_PCB)


def test_jitter_moves_only_unlocked(tmp_path, run_autoroute):
    from kicad_autorouter.placement import _jitter_for
    from kicad_autorouter.sexpr import parse_file

    out = tmp_path / "out.kicad_pcb"
    assert run_autoroute.main(
        [str(MINIMAL_PCB), "-o", str(out), "--demo-jitter-mm", "5.0"]
    ) == 0
    at = _at_by_uuid(parse_file(out))
    dx, dy = _jitter_for(MH1_UUID, 5.0)
    assert at[MH1_UUID] == pytest.approx((58.57 + dx, 50.55 + dy))
    assert at[MH2_UUID] == (58.57, 139.45)  # locked part stays put


def test_missing_input_returns_1(tmp_path, run_autoroute):
    missing = tmp_path / "nope.kicad_pcb"
    assert run_autoroute.main([str(missing), "-o", str(tmp_path / "out.kicad_pcb")]) == 1
