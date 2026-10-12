"""Tests for kicad_autorouter.validate using kicad-cli (DRC/ERC).

Fixtures are minimal KiCad 10 files copied from the app's template library.
Tests that need kicad-cli skip cleanly when it is not installed.
"""

from pathlib import Path

import pytest

from kicad_autorouter.validate import (
    KicadCliNotFound,
    find_kicad_cli,
    validate_pcb,
    validate_sch,
)

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "minimal.kicad_pcb"
SCH_FIXTURE = FIXTURES / "minimal.kicad_sch"


def _cli() -> str:
    try:
        return find_kicad_cli()
    except KicadCliNotFound:
        pytest.skip("kicad-cli not installed")


@pytest.fixture(scope="module")
def cli() -> str:
    return _cli()


def test_valid_pcb_passes_drc(cli):
    report = validate_pcb(PCB_FIXTURE, cli=cli)
    assert report.loaded
    assert report.error is None
    assert report.violations == []
    assert report.ok


def test_valid_sch_passes_erc(cli):
    report = validate_sch(SCH_FIXTURE, cli=cli)
    assert report.loaded
    assert report.error is None
    assert report.violations == []
    assert report.ok