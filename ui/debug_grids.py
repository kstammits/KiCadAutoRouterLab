"""Debug grid payloads for the SVG cost overlay (extracted from server.py).

Pure numpy→dict functions: no board state, no HTTP. ``server.py``
re-exports these names so existing imports keep working.
"""

import numpy as np

from kicad_autorouter.routing.router import SECONDARY_BLOCKED


def _cost_grid_to_debug(cost_grid: np.ndarray, routing_grid) -> dict:
    """Convert cost grid to downsampled debug format for SVG overlay.

    Cost values:
    - 0 = FREE
    - 50 = HIGH_COST (pad clearance)
    - 80 = EDGE_KEEPOUT (board edge)
    - 100+ = BLOCKED (tracks, vias, courtyards, zones)
    """
    n_layers, H, W = cost_grid.shape

    # Downsample to max 200x200 for reasonable JSON size
    max_dim = 200
    scale = max(H // max_dim, W // max_dim, 1)

    if scale > 1:
        # Take max in each block to preserve blocked cells
        h_ds = H // scale
        w_ds = W // scale
        ds_grid = np.zeros((n_layers, h_ds, w_ds), dtype=np.int16)
        for l in range(n_layers):
            for r in range(h_ds):
                for c in range(w_ds):
                    r0, r1 = r * scale, min((r + 1) * scale, H)
                    c0, c1 = c * scale, min((c + 1) * scale, W)
                    ds_grid[l, r, c] = np.max(cost_grid[l, r0:r1, c0:c1])
    else:
        ds_grid = cost_grid
        h_ds, w_ds = H, W

    # Convert to lists for JSON
    layers = []
    for l in range(n_layers):
        layer_data = ds_grid[l].tolist()
        # Find min/max for color scaling
        vals = [v for row in layer_data for v in row if v > 0]
        layers.append({
            "layer": routing_grid.layers[l] if l < len(routing_grid.layers) else f"layer_{l}",
            "data": layer_data,
            "height": h_ds,
            "width": w_ds,
            "scale": scale,
            "min_cost": min(vals) if vals else 0,
            "max_cost": max(vals) if vals else 0,
        })

    return {
        "layers": layers,
        "origin_mm": routing_grid.origin_mm,
        "resolution_mm": routing_grid.resolution_mm * scale,
        "cost_scale": {
            "FREE": 0,
            "HIGH_COST": 50,
            "EDGE_KEEPOUT": 80,
            "BLOCKED": 100,
        }
    }


def _secondary_to_debug(secondary: np.ndarray, routing_grid, terminals) -> dict:
    """Banded per-net secondary (fanout halo) grid for SVG overlay.

    Raw int16 values (up to THT_EXCLUSION 10000) would need linear scaling
    that hides everything below the max, so band the block-max instead:
    0 = free, 1 = soft (outer halo / pour severance, 1..499),
    2 = inner halo (500..999), 3 = hard ring (>= SECONDARY_BLOCKED).
    Terminals are reported in downsampled cell coords (same pixel space as
    the raster) with their raw own-cell value + sealed flag.
    """
    _, H, W = secondary.shape
    max_dim = 200
    scale = max(H // max_dim, W // max_dim, 1)
    h_ds, w_ds = H // scale, W // scale

    layers = []
    for l in range(secondary.shape[0]):
        if scale > 1:
            ds = np.zeros((h_ds, w_ds), dtype=np.int16)
            for r in range(h_ds):
                for c in range(w_ds):
                    ds[r, c] = np.max(
                        secondary[l, r * scale:min((r + 1) * scale, H),
                                  c * scale:min((c + 1) * scale, W)])
        else:
            ds = secondary[l][:, :]
        band = np.zeros_like(ds, dtype=np.int8)
        band[(ds > 0) & (ds < 500)] = 1
        band[(ds >= 500) & (ds < SECONDARY_BLOCKED)] = 2
        band[ds >= SECONDARY_BLOCKED] = 3
        layers.append({
            "layer": routing_grid.layers[l] if l < len(routing_grid.layers) else f"layer_{l}",
            "data": band.tolist(),
            "height": int(band.shape[0]),
            "width": int(band.shape[1]),
            "scale": scale,
        })

    terms = []
    for i, (c, r, lyr) in enumerate(terminals):
        raw = int(secondary[lyr, r, c]) if (
            0 <= lyr < secondary.shape[0] and 0 <= r < H and 0 <= c < W) else -1
        terms.append({"idx": i, "dc": c // scale, "dr": r // scale,
                      "layer": int(lyr), "own_cell": raw,
                      "sealed": bool(raw >= SECONDARY_BLOCKED)})
    return {"bands": {0: "free", 1: "soft", 2: "inner", 3: "hard"},
            "blocked_at": SECONDARY_BLOCKED,
            "layers": layers, "terminals": terms}
