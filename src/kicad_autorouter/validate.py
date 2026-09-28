"""Validate KiCad files using kicad-cli (DRC for boards, ERC for schematics).

KiCad 10 board (.kicad_pcb) and schematic (.kicad_sch) files are S-expression
text files. A file is considered "good" when:
  1. it has the expected header token ((kicad_pcb / (kicad_sch), and
  2. kicad-cli can load it and run its checker without a load error.

Checker violations (DRC/ERC) are reported separately from load failures —
a file that loads cleanly but has design-rule violations is still a valid
KiCad file, just not a clean one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

KICAD_CLI_CANDIDATES = (
    "kicad-cli",  # on PATH
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS app bundle
    "/opt/homebrew/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
)

PCB_HEADER = "(kicad_pcb"
SCH_HEADER = "(kicad_sch"


class KicadCliNotFound(Exception):
    """Raised when no kicad-cli binary can be located."""


@dataclass
class ValidationReport:
    kind: str  # "pcb" or "sch"
    file: Path
    loaded: bool  # False if kicad-cli could not load the file
    violations: list = field(
        default_factory=list
    )  # raw violation dicts from JSON report
    error: str | None = None  # stderr message when loading fails

    @property
    def ok(self) -> bool:
        return self.loaded and not self.violations


def find_kicad_cli() -> str:
    """Return the path to a usable kicad-cli binary, or raise KicadCliNotFound."""
    for candidate in KICAD_CLI_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    raise KicadCliNotFound(
        "kicad-cli not found; install KiCad (e.g. `brew install --cask kicad`) "
        f"or add it to PATH. Searched: {', '.join(KICAD_CLI_CANDIDATES)}"
    )


def file_header(path: Path) -> str | None:
    """Return the first token of a KiCad file, e.g. '(kicad_pcb' or '(kicad_sch'.

    Returns None if the file cannot be read or has no recognizable header.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline(256).strip()
    except OSError:
        return None
    token = first_line.split(" ", 1)[0] if first_line else ""
    return token or None


def looks_like_kicad_file(path: Path) -> bool:
    """Cheap header check that does not require kicad-cli."""
    return file_header(path) in (PCB_HEADER, SCH_HEADER)


def _run_checker(cli: str, subcommand: tuple[str, ...], path: Path) -> ValidationReport:
    kind = "pcb" if subcommand[0] == "pcb" else "sch"
    report_path = None
    try:
        # Unique path that is NOT pre-created: it only exists if kicad-cli wrote a report.
        report_path = (
            Path(tempfile.gettempdir()) / f"kicad_report_{uuid.uuid4().hex}.json"
        )
        cmd = [cli, *subcommand, str(path), "--format", "json", "-o", str(report_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        stderr = proc.stderr.strip()
        # kicad-cli prints harmless fontconfig warnings on macOS; keep only load errors.
        load_error = next(
            (line for line in stderr.splitlines() if "Failed to load" in line), None
        )

        if not report_path.exists():
            return ValidationReport(
                kind=kind,
                file=path,
                loaded=False,
                error=load_error or stderr or f"exit code {proc.returncode}",
            )

        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return ValidationReport(kind=kind, file=path, loaded=False, error=str(exc))

        if kind == "pcb":
            violations = list(data.get("violations", []))
            violations.extend(data.get("unconnected_items", []))
        else:  # sch: violations are nested per sheet
            violations = [
                v
                for sheet in data.get("sheets", [])
                for v in sheet.get("violations", [])
            ]

        return ValidationReport(
            kind=kind, file=path, loaded=True, violations=violations, error=load_error
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ValidationReport(kind=kind, file=path, loaded=False, error=str(exc))
    finally:
        if report_path is not None and report_path.exists():
            try:
                report_path.unlink()
            except OSError:
                pass


def validate_pcb(path: Path, cli: str | None = None) -> ValidationReport:
    """Run `kicad-cli pcb drc` on a board file and parse the JSON report."""
    return _run_checker(cli or find_kicad_cli(), ("pcb", "drc"), path)


def validate_sch(path: Path, cli: str | None = None) -> ValidationReport:
    """Run `kicad-cli sch erc` on a schematic file and parse the JSON report."""
    return _run_checker(cli or find_kicad_cli(), ("sch", "erc"), path)
