"""Debug grid payloads for the SVG cost overlay (extracted from server.py).

Pure numpy→dict functions: no board state, no HTTP. ``server.py``
re-exports these names so existing imports keep working.

Transport is two-track per layer:

* ``png_b64`` — palette-mode PNG at near-full grid resolution for display.
  Flat zones (free/courtyard/pour/block) compress to almost nothing, so a
  full DCCF grid ships in the low hundreds of KB instead of ~20 MB as int
  arrays. Encoded with a stdlib-only writer (zlib + struct, no Pillow).
* ``data`` — the original block-max int grid capped at 200px, kept purely
  as the hover-probe source (exact per-cell values without decoding PNG
  client-side).

Palette contract (mirrored by the viewer legend ``COST_PALETTE`` in
app-viewer.js — change both together):

* primary cost value → color: 0 free (transparent), 25 courtyard gray,
  35 pour teal, 50 high-cost yellow, 80 edge-keepout orange, >=100 blocked
  red. The engine stamps with maximum semantics over exactly this set, so
  the mapping is exact; any other positive value rounds up to the next
  hotter level (conservative, never hides cost).
* secondary band → color: 0 free (transparent), 1 soft yellow,
  2 inner orange, 3 hard magenta.
"""

import base64
import struct
import zlib

import numpy as np

from kicad_autorouter.routing.router import SECONDARY_BLOCKED


# Palette index 0 is always fully transparent free space.
COST_PALETTE = (
    (0, 0, 0, 0),            # 0 free
    (140, 140, 140, 140),    # 1 courtyard (25)
    (46, 168, 122, 140),     # 2 pour (35)
    (255, 255, 0, 180),      # 3 high-cost (50)
    (255, 165, 0, 200),      # 4 edge-keepout (80)
    (255, 0, 0, 220),        # 5 blocked (>=100)
)
SECONDARY_PALETTE = (
    (0, 0, 0, 0),            # 0 free
    (255, 235, 59, 150),     # 1 soft halo
    (255, 140, 0, 170),      # 2 inner halo
    (255, 0, 255, 200),      # 3 hard ring
)

# Per-layer PNG byte budget; halve the raster (block-max) while over it.
PNG_BYTE_BUDGET = 1_000_000
PNG_MAX_HALVINGS = 2  # full, 1/2, 1/4


def _encode_paletted_png(indexed: np.ndarray, palette) -> bytes:
    """Index rows (uint8) + RGBA palette → palette-mode PNG bytes.

    Stdlib only: filter byte 0 per row, single IDAT, tRNS alpha chunk.
    """
    h, w = indexed.shape
    rows = b"".join(b"\x00" + indexed[r].astype(np.uint8).tobytes()
                    for r in range(h))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    plte = b"".join(bytes(entry[:3]) for entry in palette)
    trns = bytes(entry[3] for entry in palette)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"PLTE", plte) + _chunk(b"tRNS", trns)
            + _chunk(b"IDAT", zlib.compress(rows, 6)) + _chunk(b"IEND", b""))


def _block_max_half(indexed: np.ndarray) -> np.ndarray:
    """One block-max halving step (max preserves blocked cells)."""
    h, w = indexed.shape
    h2, w2 = max(h // 2, 1), max(w // 2, 1)
    out = np.zeros((h2, w2), dtype=np.uint8)
    for r in range(h2):
        for c in range(w2):
            out[r, c] = np.max(indexed[r * 2:min((r + 1) * 2, h),
                                       c * 2:min((c + 1) * 2, w)])
    return out


def _layer_png(indexed: np.ndarray, palette) -> dict:
    """Paletted PNG payload, stepping resolution down while over budget."""
    step = 1
    idx = np.ascontiguousarray(indexed, dtype=np.uint8)
    for _ in range(PNG_MAX_HALVINGS + 1):
        blob = _encode_paletted_png(idx, palette)
        if len(blob) <= PNG_BYTE_BUDGET or step == 2 ** PNG_MAX_HALVINGS:
            return {"png_b64": base64.b64encode(blob).decode(),
                    "png_scale": step,
                    "png_width": idx.shape[1], "png_height": idx.shape[0]}
        idx = _block_max_half(idx)
        step *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def _cost_to_index(cost: np.ndarray) -> np.ndarray:
    """Raw cost values → palette indices (exact on the engine's value set)."""
    idx = np.zeros(cost.shape, dtype=np.uint8)
    pos = cost > 0
    # searchsorted on the known levels, +1 for the transparent-0 slot;
    # values above every level (incl. >=100) land on blocked.
    idx[pos] = np.searchsorted(
        np.array([25, 35, 50, 80], dtype=cost.dtype), cost[pos],
        side="left") + 1
    return np.clip(idx, 0, len(COST_PALETTE) - 1).astype(np.uint8)


def _band_index(secondary: np.ndarray) -> np.ndarray:
    """Raw secondary values → band indices 0..3 (same bands as the JSON)."""
    band = np.zeros(secondary.shape, dtype=np.uint8)
    band[(secondary > 0) & (secondary < 500)] = 1
    band[(secondary >= 500) & (secondary < SECONDARY_BLOCKED)] = 2
    band[secondary >= SECONDARY_BLOCKED] = 3
    return band


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
            **_layer_png(_cost_to_index(cost_grid[l]), COST_PALETTE),
        })

    return {
        "layers": layers,
        "origin_mm": routing_grid.origin_mm,
        "full_resolution_mm": routing_grid.resolution_mm,
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
            **_layer_png(_band_index(secondary[l]), SECONDARY_PALETTE),
        })

    terms = []
    for i, (c, r, lyr) in enumerate(terminals):
        raw = int(secondary[lyr, r, c]) if (
            0 <= lyr < secondary.shape[0] and 0 <= r < H and 0 <= c < W) else -1
        terms.append({"idx": i, "dc": c // scale, "dr": r // scale,
                      "fc": int(c), "fr": int(r),
                      "layer": int(lyr), "own_cell": raw,
                      "sealed": bool(raw >= SECONDARY_BLOCKED)})
    return {"bands": {0: "free", 1: "soft", 2: "inner", 3: "hard"},
            "blocked_at": SECONDARY_BLOCKED,
            "layers": layers, "terminals": terms}
