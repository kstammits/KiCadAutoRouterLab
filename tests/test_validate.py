"""Tests for kicad_autorouter.validate using kicad-cli (DRC/ERC).

Fixtures are minimal KiCad 10 files copied from the app's template library.
Tests that need kicad-cli skip cleanly when it is not installed.
"""

from pathlib import Path

import pytest

from kicad_autorouter.validate import (
    KicadCliNotFound,
    file_header,
    find_kicad_cli,
    looks_like_kicad_file,
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


# --- header checks (no kicad-cli required) ---------------------------------


def test_pcb_header():
    assert file_header(PCB_FIXTURE) == "(kicad_pcb"


def test_sch_header():
    assert file_header(SCH_FIXTURE) == "(kicad_sch"


def test_looks_like_kicad_file():
    assert looks_like_kicad_file(PCB_FIXTURE)
    assert looks_like_kicad_file(SCH_FIXTURE)


def test_non_kicad_file_rejected(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello world\n")
    assert not looks_like_kicad_file(bad)


# --- kicad-cli checks ------------------------------------------------------


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


def test_corrupt_pcb_fails_to_load(cli, tmp_path):
    corrupt = tmp_path / "corrupt.kicad_pcb"
    content = PCB_FIXTURE.read_text(encoding="utf-8")
    corrupt.write_text(content[: len(content) // 2], encoding="utf-8")
    report = validate_pcb(corrupt, cli=cli)
    assert not report.loaded
    assert "Failed to load" in (report.error or "")


def test_corrupt_sch_fails_to_load(cli, tmp_path):
    corrupt = tmp_path / "corrupt.kicad_sch"
    content = SCH_FIXTURE.read_text(encoding="utf-8")
    corrupt.write_text(content[: len(content) // 2], encoding="utf-8")
    report = validate_sch(corrupt, cli=cli)
    assert not report.loaded
    assert "Failed to load" in (report.error or "")


def test_missing_file_reports_error(cli):
    missing = Path("/nonexistent/board.kicad_pcb")
    report = validate_pcb(missing, cli=cli)
    assert not report.loaded
    assert report.error is not None
