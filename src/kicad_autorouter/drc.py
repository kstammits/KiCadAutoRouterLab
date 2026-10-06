"""DRC integration: run kicad-cli DRC on PCB trees and interpret results.

Uses a local tmp/ directory for output files so they persist for inspection.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

from .io import save_pair
from .sexpr import SExpr, to_sexpr
from .validate import find_kicad_cli, ValidationReport, KicadCliNotFound, validate_pcb


TMP_DIR = Path(__file__).parent.parent.parent / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# Default violation types to ignore for overlay display (silkscreen, library issues)
DEFAULT_IGNORED_TYPES: Set[str] = {
    "silk_over_copper",
    "silk_overlap",
    "silk_edge_clearance",
    "lib_footprint_issues",
    "lib_footprint_mismatch",
    "footprint_filters_mismatch",
    "footprint_type_mismatch",
    "missing_courtyard",
    "track_not_centered_on_via",
    "tuning_profile_track_geometries",
}


@dataclass
class DRCResult:
    """Result of running DRC on a PCB."""
    violations: list[dict] = field(default_factory=list)
    unconnected_items: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    report_path: Optional[Path] = None
    pcb_path: Optional[Path] = None

    @property
    def violation_count(self) -> int:
        return len(self.violations)

    @property
    def unconnected_count(self) -> int:
        return len(self.unconnected_items)

    @property
    def total_issues(self) -> int:
        return self.violation_count + self.unconnected_count

    @property
    def clean(self) -> bool:
        return self.total_issues == 0 and self.error is None

    def violations_by_type(self) -> dict[str, int]:
        """Count violations by type."""
        counts = {}
        for v in self.violations:
            vtype = v.get("type", "unknown")
            counts[vtype] = counts.get(vtype, 0) + 1
        return counts

    def violations_by_severity(self) -> dict[str, int]:
        """Count violations by severity."""
        counts = {}
        for v in self.violations:
            sev = v.get("severity", "unknown")
            counts[sev] = counts.get(sev, 0) + 1
        return counts

    def filter_violations(self, ignored_types: Optional[Set[str]] = None) -> list[dict]:
        """Return violations filtered by ignored types."""
        if ignored_types is None:
            ignored_types = DEFAULT_IGNORED_TYPES
        return [v for v in self.violations if v.get("type") not in ignored_types]

    def filtered_violation_count(self, ignored_types: Optional[Set[str]] = None) -> int:
        return len(self.filter_violations(ignored_types))

    def filtered_total_issues(self, ignored_types: Optional[Set[str]] = None) -> int:
        return self.filtered_violation_count(ignored_types) + self.unconnected_count

    def summary(self, ignored_types: Optional[Set[str]] = None) -> str:
        """Human-readable summary."""
        if ignored_types is None:
            ignored_types = DEFAULT_IGNORED_TYPES
        filtered = self.filter_violations(ignored_types)
        lines = [f"DRC: {len(filtered) + self.unconnected_count} issues ({len(filtered)} violations, {self.unconnected_count} unconnected)"]
        if filtered:
            counts = {}
            for v in filtered:
                vtype = v.get("type", "unknown")
                counts[vtype] = counts.get(vtype, 0) + 1
            lines.append("  By type (filtered):")
            for vtype, count in sorted(counts.items()):
                lines.append(f"    {vtype}: {count}")
        if self.violations_by_severity():
            lines.append("  By severity:")
            for sev, count in sorted(self.violations_by_severity().items()):
                lines.append(f"    {sev}: {count}")
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        if self.report_path:
            lines.append(f"  Full report: {self.report_path}")
        return "\n".join(lines)


def write_pcb_tree(tree: SExpr, out_path: Optional[Path] = None, prefix: str = "drc_") -> Path:
    """Write a PCB S-expression tree to a file in tmp/."""
    if out_path is None:
        out_path = TMP_DIR / f"{prefix}{uuid.uuid4().hex[:8]}.kicad_pcb"
    else:
        out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(to_sexpr(tree) + "\n", encoding="utf-8")
    return out_path


def run_drc_on_tree(
    tree: SExpr,
    out_path: Optional[Path] = None,
    prefix: str = "drc_",
    cli: Optional[str] = None,
    keep_report: bool = True,
    report_path: Optional[Path] = None,
) -> DRCResult:
    """Run DRC on a PCB S-expression tree.

    Args:
        tree: PCB S-expression tree
        out_path: Optional specific output path; otherwise uses tmp/
        prefix: Prefix for generated temp file name
        cli: Optional path to kicad-cli
        keep_report: If True, keep the JSON report file for inspection
        report_path: Optional fixed path for the JSON report (overwritten each run);
            otherwise a uuid-named file is created in tmp/

    Returns:
        DRCResult with violations, unconnected items, and paths
    """
    # Write PCB to temp file
    pcb_path = write_pcb_tree(tree, out_path, prefix)

    # Run DRC
    try:
        kicad_cli = cli or find_kicad_cli()
    except KicadCliNotFound as e:
        return DRCResult(
            error=f"kicad-cli not found: {e}",
            pcb_path=pcb_path,
        )

    if report_path is None:
        report_path = TMP_DIR / f"drc_report_{uuid.uuid4().hex[:8]}.json"
    else:
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)

    import subprocess
    cmd = [kicad_cli, "pcb", "drc", str(pcb_path), "--format", "json", "-o", str(report_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return DRCResult(
            error="kicad-cli timeout (300s)",
            pcb_path=pcb_path,
        )
    except OSError as e:
        return DRCResult(
            error=f"Failed to run kicad-cli: {e}",
            pcb_path=pcb_path,
        )

    stderr = proc.stderr.strip()
    load_error = next(
        (line for line in stderr.splitlines() if "Failed to load" in line), None
    )

    if not report_path.exists():
        return DRCResult(
            error=load_error or stderr or f"exit code {proc.returncode}",
            pcb_path=pcb_path,
        )

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return DRCResult(
            error=f"Failed to parse DRC report: {exc}",
            pcb_path=pcb_path,
            report_path=report_path,
        )

    violations = list(data.get("violations", []))
    unconnected = list(data.get("unconnected_items", []))

    result = DRCResult(
        violations=violations,
        unconnected_items=unconnected,
        error=load_error,
        report_path=report_path,
        pcb_path=pcb_path,
    )

    if not keep_report and report_path.exists():
        try:
            report_path.unlink()
        except OSError:
            pass

    return result


def run_drc_on_pair(
    pcb_tree: SExpr,
    sch_tree: Optional[SExpr] = None,
    out_pcb: Optional[Path] = None,
    out_sch: Optional[Path] = None,
    cli: Optional[str] = None,
) -> tuple[DRCResult, Optional[DRCResult]]:
    """Run DRC on PCB and optionally ERC on schematic."""
    pcb_result = run_drc_on_tree(pcb_tree, out_pcb, "drc_pcb_", cli)

    sch_result = None
    if sch_tree is not None:
        if out_sch is None:
            out_sch = TMP_DIR / f"drc_sch_{uuid.uuid4().hex[:8]}.kicad_sch"
        out_sch = Path(out_sch)
        out_sch.parent.mkdir(parents=True, exist_ok=True)
        out_sch.write_text(to_sexpr(sch_tree) + "\n", encoding="utf-8")

        from .validate import validate_sch
        report = validate_sch(out_sch, cli)
        sch_result = DRCResult(
            violations=report.violations,
            unconnected_items=report.unconnected_items,
            error=report.error,
            pcb_path=out_sch,
        )

    return pcb_result, sch_result


def cleanup_tmp(older_than_hours: Optional[int] = None):
    """Clean up tmp/ directory. If older_than_hours is set, only remove files older than that."""
    import time
    now = time.time()
    for f in TMP_DIR.iterdir():
        if f.is_file():
            if older_than_hours is None:
                f.unlink()
            else:
                if (now - f.stat().st_mtime) > older_than_hours * 3600:
                    f.unlink()