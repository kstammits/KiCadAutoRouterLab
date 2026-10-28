#!/usr/bin/env python3
"""Plot the seal-probe picture: route outcomes + terminal seal dossier.

Reads /tmp/seal_probe.json (scripts/route_seal_probe.py), writes two PNGs
to /tmp: seal_outcomes.png and seal_dossier.png.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

SECONDARY_BLOCKED = 1000


def main() -> None:
    data = json.loads(Path("/tmp/seal_probe.json").read_text())
    rows = data["rows"]
    dossiers = {d["board"] + ":" + d["net"]: d for d in data["dossiers"]}

    cases = list(dict.fromkeys(r["case"] for r in rows))
    configs = ["w=3.0 halo=1.0", "w=1.0 halo=1.0",
               "w=3.0 halo=0.3", "w=1.0 halo=0.3"]

    # Fig 1: outcome per case x config — expansions (log), green=routed red=failed.
    fig, ax = plt.subplots(figsize=(13, 5))
    x = np.arange(len(cases))
    width = 0.18
    for j, cfg in enumerate(configs):
        exps, ok = [], []
        for c in cases:
            r = next(r for r in rows if r["case"] == c and r["config"] == cfg)
            exps.append(max(r["expansions"], 1))
            ok.append(r["success"])
        bars = ax.bar(x + (j - 1.5) * width, exps, width, label=cfg,
                      color=["#2ca02c" if s else "#d62728" for s in ok],
                      edgecolor="black", linewidth=0.4)
    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xticklabels([c.replace("tube111:", "t:").replace("DCCF.sved:", "D:")
                        .replace("beads:", "b:") for c in cases],
                       rotation=25, ha="right", fontsize=8)
    ax.set_ylabel("A* expansions (log)")
    ax.set_title("Seal probe: tight pairs fail at exp=3 in every halo/weight cell; "
                 "controls route (or maze-fail at 100k+)")
    ax.legend(handles=[mpatches.Patch(facecolor="#2ca02c", edgecolor="black",
                                             label="routed"),
                      mpatches.Patch(facecolor="#d62728", edgecolor="black",
                                             label="failed")],
              fontsize=8, loc="upper left")
    ax.set_xlabel("route case (bars left-to-right per case: " +
                  ", ".join(configs) + ")", fontsize=8)
    ax.text(0.99, 0.97, "green = routed   red = failed",
            transform=ax.transAxes, ha="right", va="top", fontsize=9,
            bbox=dict(facecolor="white", alpha=0.8, edgecolor="none"))
    fig.tight_layout()
    fig.savefig("/tmp/seal_outcomes.png", dpi=150)
    print("Saved /tmp/seal_outcomes.png")

    # Fig 2: seal dossier — own-cell secondary per terminal, tight idx marked.
    keys = list(dossiers)
    fig2, axes = plt.subplots(2, 3, figsize=(13, 6), sharey=True)
    for axi, key in zip(axes.flat, keys):
        d = dossiers[key]
        seals = d["seals"]
        vals = [max(s["own_secondary"], 1) for s in seals]
        tight = set(d["tight_idx"])
        colors = ["#d62728" if s["idx"] in tight else "#1f77b4" for s in seals]
        axi.bar([s["idx"] for s in seals], vals, color=colors, edgecolor="black",
                linewidth=0.4)
        axi.axhline(SECONDARY_BLOCKED, color="black", linestyle="--", linewidth=1)
        axi.set_yscale("log")
        axi.set_title(f"{key} ({d['n_terminals']} terms)", fontsize=9)
        axi.set_xlabel("terminal idx", fontsize=8)
        via = sum(1 for s in seals if s["via_blocked"])
        axi.text(0.95, 0.08, f"via_blocked {via}/{len(seals)}",
                 transform=axi.transAxes, ha="right", fontsize=8,
                 bbox=dict(facecolor="white", alpha=0.8, edgecolor="none"))
    axes.flat[0].set_ylabel("own-cell secondary (log)\n-- = hard-exclusion line (1000)")
    fig2.suptitle("Terminal dossier: tight terminals sit on cells at 10000 "
                  "(10x the hard-exclusion line); halo/weight sweeps cannot move them")
    fig2.tight_layout()
    fig2.savefig("/tmp/seal_dossier.png", dpi=150)
    print("Saved /tmp/seal_dossier.png")


if __name__ == "__main__":
    main()
