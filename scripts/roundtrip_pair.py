#!/usr/bin/env python3
"""Read a .kicad_pcb + .kicad_sch pair, apply a no-op edit, write new files, check them.

Pure Python (no pcbnew) — runs in the project .venv:

    .venv/bin/python scripts/roundtrip_pair.py [board.kicad_pcb] [sch.kicad_sch] [-o OUT_DIR]

Checks performed on the written files:
  1. they re-parse to trees structurally equal to the originals;
  2. when kicad-cli is available, DRC (pcb) / ERC (sch) load cleanly with no violations.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the src/ package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_autorouter.io import (  # noqa: E402
    RoundtripError,
    load_pair,
    noop_roundtrip,
    verify_noop,
)
from kicad_autorouter.sexpr import SExpr  # noqa: E402
from kicad_autorouter.validate import (  # noqa: E402
    KicadCliNotFound,
    find_kicad_cli,
    validate_pcb,
    validate_sch,
)


def _version(tree: SExpr) -> str:
    node = tree.find("version")
    return str(node.args[0]) if node is not None and node.args else "?"


def main(argv=None) -> int:
    repo = Path(__file__).resolve().parent.parent
    fixtures = repo / "tests" / "fixtures"

    p = argparse.ArgumentParser(
        description="No-op read/write round trip for a KiCad pcb+sch pair."
    )
    p.add_argument("pcb", nargs="?", default=str(fixtures / "minimal.kicad_pcb"))
    p.add_argument("sch", nargs="?", default=str(fixtures / "minimal.kicad_sch"))
    p.add_argument(
        "-o",
        "--out-dir",
        default=str(repo / "experiments" / "out" / "roundtrip"),
        help="where to write the new files (default: experiments/out/roundtrip)",
    )
    args = p.parse_args(argv)

    pcb_path, sch_path = Path(args.pcb), Path(args.sch)
    pair = load_pair(pcb_path, sch_path)
    n_fp = len(list(pair.pcb.children("footprint")))
    print(
        f"loaded {pcb_path.name}: kicad_pcb v{_version(pair.pcb)}, "
        f"{n_fp} footprints"
    )
    print(f"loaded {sch_path.name}: kicad_sch v{_version(pair.sch)}")

    out_pcb, out_sch = noop_roundtrip(pcb_path, sch_path, Path(args.out_dir))
    print(f"no-op edit: wrote {out_pcb}")
    print(f"             wrote {out_sch}")

    ok = True
    try:
        verify_noop(pcb_path, sch_path, out_pcb, out_sch)
        print("check 1/2 re-parse equality: OK")
    except RoundtripError as exc:
        ok = False
        print(f"check 1/2 re-parse equality: FAIL ({exc})")

    try:
        cli = find_kicad_cli()
    except KicadCliNotFound:
        print("check 2/2 kicad-cli DRC/ERC: skipped (kicad-cli not found)")
    else:
        for label, report in (
            ("pcb drc", validate_pcb(out_pcb, cli=cli)),
            ("sch erc", validate_sch(out_sch, cli=cli)),
        ):
            if report.ok:
                print(f"check 2/2 {label}: OK")
            else:
                ok = False
                print(
                    f"check 2/2 {label}: FAIL "
                    f"(loaded={report.loaded}, violations={len(report.violations)}, "
                    f"error={report.error})"
                )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
