// app-viewer.js — the board picture: layers, zoom, overlays, refresh.
// Needs core (el/api/toast) at load; selection + params read at runtime only.

const boardEl = el("board");
const nameEl = el("board-name");
const viewport = el("board-viewport");
const zoomInfo = el("zoom-info");
const schChip = el("board-sch");
const overlayToggle = el("overlay-toggle");
const forcesToggle = el("forces-toggle");
const drcToggle = el("drc-toggle");
const layerTogglesEl = el("layer-toggles");

function layerToggleInputs() {
  return [...(layerTogglesEl?.querySelectorAll('input[data-layer]') ?? [])];
}

function layerToggleState() {
  // Map of copper layer -> visible, from the dynamic checkboxes.
  const state = new Map();
  for (const input of layerToggleInputs()) state.set(input.dataset.layer, input.checked);
  if (!state.size) { state.set("F.Cu", true); state.set("B.Cu", true); }
  return state;
}

function renderLayerToggles(copperLayers) {
  // Rebuild one checkbox + Solo button per stackup layer, preserving user
  // choices for layers that persist across board versions. Client-only:
  // visibility is display:none on the already-injected SVG (no refetch).
  const layers = (Array.isArray(copperLayers) && copperLayers.length) ? copperLayers : ["F.Cu", "B.Cu"];
  if (!layerTogglesEl) return;
  const prev = layerToggleState();
  const current = layerToggleInputs().map(i => i.dataset.layer);
  // Optional element (created below on first render): direct lookup so the
  // shared el() helper does not log a "missing" warning on a fresh board.
  if (current.length === layers.length && current.every((l, i) => l === layers[i]) && document.getElementById("layer-solo-all")) return;
  layerTogglesEl.innerHTML = "";
  for (const layer of layers) {
    const label = document.createElement("label");
    label.className = "chk";
    label.title = layer === "F.Cu" ? "Front copper (solid)" : layer === "B.Cu" ? "Back copper (dashed 3 2)" : "Inner copper (dashed, stackup order)";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.layer = layer;
    input.title = label.title;
    input.checked = prev.has(layer) ? prev.get(layer) : true;
    input.addEventListener("change", applyLayerVisibility);
    label.appendChild(input);
    label.appendChild(document.createTextNode(` ${layer}`));
    layerTogglesEl.appendChild(label);
    const solo = document.createElement("button");
    solo.className = "tab-btn btn-mini";
    solo.style.marginLeft = ".15rem";
    solo.textContent = "solo";
    solo.title = `Isolate ${layer} (display-only)`;
    solo.dataset.soloLayer = layer;
    solo.addEventListener("click", (e) => { e.preventDefault(); toggleSoloLayer(layer); });
    layerTogglesEl.appendChild(solo);
    layerTogglesEl.appendChild(document.createTextNode(" "));
  }
  const all = document.createElement("button");
  all.id = "layer-solo-all";
  all.className = "tab-btn btn-mini";
  all.textContent = "all";
  all.title = "Show all copper layers (display-only)";
  all.addEventListener("click", (e) => { e.preventDefault(); setSoloLayer(null); });
  layerTogglesEl.appendChild(all);
  renderLayerLegend(layers);
}

// Solo isolate state: null = follow checkboxes, otherwise only that layer.
let soloLayer = null;

function setSoloLayer(layer) {
  soloLayer = layer;
  applyLayerVisibility();
}

function toggleSoloLayer(layer) {
  soloLayer = (soloLayer === layer) ? null : layer;
  applyLayerVisibility();
}

// Copper legend mirroring svg_render.py: F red solid, B green "3 2", inners
// cycle teal/violet/amber/blue with dashes "5 2 / 2 2 / 7 2 2 2 / 4 1".
function renderLayerLegend(layers) {
  // Optional element (created here on first render): direct lookup to avoid
  // a "missing" warning from the shared el() helper on a fresh board.
  let legend = document.getElementById("layer-legend");
  if (!legend) {
    legend = document.createElement("span");
    legend.id = "layer-legend";
    legend.className = "chip layer-legend";
    layerTogglesEl.after(legend);
  }
  const FILL = { "F.Cu": "#e74c3c", "B.Cu": "#2ecc71" };
  const INNER_FILLS = ["#2dd4bf", "#a78bfa", "#f5a623", "#59a7f5"];
  const INNER_DASHES = ["5 2", "2 2", "7 2 2 2", "4 1"];
  const inners = layers.filter(l => l !== "F.Cu" && l !== "B.Cu");
  legend.innerHTML = layers.map(l => {
    let color, dash;
    if (l === "F.Cu") { color = FILL["F.Cu"]; dash = "solid"; }
    else if (l === "B.Cu") { color = FILL["B.Cu"]; dash = "3 2"; }
    else { const p = inners.indexOf(l); color = INNER_FILLS[p % INNER_FILLS.length]; dash = INNER_DASHES[p % INNER_DASHES.length]; }
    const border = dash === "solid" ? `border-top:2px solid ${color};` : `border-top:2px dashed ${color};`;
    const soloMark = (soloLayer === l) ? " ★" : "";
    const safe = escapeHtml(l);
    return `<span title="${safe} ${dash}${soloMark}"><i style="display:inline-block;width:1.2rem;${border}"></i> ${safe}${soloMark}</span>`;
  }).join("");
}

const zonesToggle = el("zones-toggle");
const zonesFillToggle = el("zones-fill-toggle");
const fitBtn = el("fit-btn");

let lastVersion = 0;
let viewBox = null; // [minX, minY, width, height]
let initialViewBox = null; // native board viewBox from the last injected SVG
let isPanning = false;
let panStart = null;

function boardSrc(version) {
  const overlayOn = overlayToggle?.checked ?? false;
  const forcesOn = forcesToggle?.checked ?? false;
  const drcOn = drcToggle?.checked ?? false;
  const fillsOn = zonesFillToggle?.checked ?? false;
  const layers = layerToggleState();
  const fcuOn = layers.get("F.Cu") ?? true;
  const bcuOn = layers.get("B.Cu") ?? true;
  const visible = [...layers.entries()].filter(([, on]) => on).map(([l]) => l).join(",");
  return `/api/board.svg?v=${version}&overlay=${overlayOn ? 1 : 0}&forces=${forcesOn ? 1 : 0}&drc=${drcOn ? 1 : 0}&fills=${fillsOn ? 1 : 0}&fcu=${fcuOn ? 1 : 0}&bcu=${bcuOn ? 1 : 0}&layers=${encodeURIComponent(visible)}`;
}

function updateZoomInfo() {
  if (!viewBox) { zoomInfo.textContent = "100%"; return; }
  // viewBox width relative to initial width (100% = initial fit)
  const ref = initialViewBox?.[2] ?? viewBox[2];
  const pct = Math.round((ref / viewBox[2]) * 100);
  zoomInfo.textContent = `${pct}%`;
}

function applyViewBox() {
  const svg = viewport?.querySelector("svg");
  if (!svg || !viewBox) return;
  svg.setAttribute("viewBox", viewBox.join(" "));
  // Close-zoom rail labels: show .pad-net-label text only when zoomed in
  // (viewBox narrower than ~40mm board units).
  viewport.classList.toggle("zoom-close", viewBox[2] < 40);
  updateZoomInfo();
}

function fitToView() {
  const svg = viewport?.querySelector("svg");
  if (!svg || !initialViewBox) return;
  // Restore the native board viewBox captured at inject time. Reading it
  // back from the SVG attribute does not work: applyViewBox() overwrites
  // that attribute on every zoom/pan, so it holds the zoomed box, not the
  // original — Fit would re-apply the current view and appear dead.
  viewBox = [...initialViewBox];
  applyViewBox();
}

// Zoom the viewer to the current footprint selection (union bbox, padded
// to the viewport aspect so nothing distorts). Geometry comes from the
// SVG groups; footprints hidden by layer visibility (zero bbox) fall back
// to their board position with a default window. No-op when nothing is
// selected or no SVG is loaded — deselect never yanks the camera.
function zoomToSelection() {
  const svg = viewport?.querySelector("svg");
  if (!svg || selectedUuids.size === 0) return;
  const boxes = [];
  for (const uuid of selectedUuids) {
    let pushed = false;
    const g = svg.querySelector(`[data-fp-uuid="${CSS.escape(uuid)}"]`);
    if (g) {
      try {
        const b = g.getBBox();
        if (b.width > 0 && b.height > 0) { boxes.push(b); pushed = true; }
      } catch (e) { /* fall through to position fallback */ }
    }
    if (!pushed) {
      const fp = allFootprints.find(f => f.uuid === uuid);
      if (fp && fp.x_mm != null && fp.y_mm != null) {
        boxes.push({ x: fp.x_mm - 2, y: fp.y_mm - 2, width: 4, height: 4 });
      }
    }
  }
  if (!boxes.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const b of boxes) {
    x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y);
    x1 = Math.max(x1, b.x + b.width); y1 = Math.max(y1, b.y + b.height);
  }
  frameBox(x0, y0, x1, y1);
}

// Frame a board-mm bbox in the viewer: padded to the viewport aspect so
// nothing distorts, with a floor so single points don't zoom to absurdity.
function frameBox(x0, y0, x1, y1) {
  const aspect = (viewport.clientWidth && viewport.clientHeight)
    ? viewport.clientWidth / viewport.clientHeight : 1;
  const pad = 1.6, minSize = 10; // mm: breathing room, floor for tiny parts
  let w = Math.max((x1 - x0) * pad, minSize);
  let h = w / aspect;
  if (h < (y1 - y0) * pad) { h = (y1 - y0) * pad; w = h * aspect; }
  viewBox = [(x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2, w, h];
  applyViewBox();
}

// Zoom the viewer to the selected nets' terminals (union over selection).
// Terminals are model pad positions: known before routing and after.
// On multi-region (panel) boards each pad also carries its region, painted
// as a color-coded dot overlay so a net spanning boards (e.g. tube111
// +12V) reads as two groups needing an assembly bridge — never one track.
// Best-effort and silent — zoom must never nag on a fetch hiccup.
const REGION_COLORS = ["#4fc3f7", "#ffd54f", "#ba68c8", "#81c784", "#ff8a65", "#9ccc65"];
let terminalsOverlayData = [];
function renderTerminalsOverlay() {
  makeSvgOverlay("terminals-overlay", terminalsOverlayData.length > 0, terminalsOverlayData, g => {
    for (const t of terminalsOverlayData) {
      const color = REGION_COLORS[(t.region ?? 0) % REGION_COLORS.length];
      const dot = svgEl("circle", {
        cx: t.x_mm, cy: t.y_mm, r: 0.6,
        fill: color, "fill-opacity": "0.85", stroke: "#111",
        "stroke-width": "0.15",
      });
      dot.appendChild(svgTitle(`${t.net} ${t.ref}.${t.pad} — region ${t.region}`));
      g.appendChild(dot);
    }
  });
}
async function zoomToNets() {
  if (!viewport?.querySelector("svg") || selectedNets.size === 0) return;
  try {
    const all = await Promise.all([...selectedNets].map(n =>
      apiJson(`/api/nets/terminals?net=${encodeURIComponent(n)}`).catch(() => null)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    let n = 0;
    const dots = [];
    for (const r of all) {
      for (const t of (r?.terminals || [])) {
        if (t.x_mm == null || t.y_mm == null) continue;
        x0 = Math.min(x0, t.x_mm); y0 = Math.min(y0, t.y_mm);
        x1 = Math.max(x1, t.x_mm); y1 = Math.max(y1, t.y_mm);
        n++;
        if (r?.multi_region) dots.push({ ...t, net: r.net });
      }
    }
    terminalsOverlayData = dots;
    renderTerminalsOverlay();
    if (!n) return;
    frameBox(x0, y0, x1, y1);
  } catch (e) { /* silent */ }
}

let costGridDebug = null;
let costGridOverlay = false;

function showCostGridDebug(debug) {
  costGridDebug = debug;
  // Static checkbox in the top bar (hidden until debug data arrives).
  const label = el("cost-grid-label");
  if (label) label.hidden = false;
  const netSel = el("cost-grid-net");
  if (netSel) {
    netSel.hidden = false;
    const cur = netSel.value;
    const names = (lastNetsByName && lastNetsByName.size)
      ? [...lastNetsByName.keys()].filter(Boolean).sort() : [];
    const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    netSel.innerHTML = `<option value="">occupancy only</option>` +
      names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (names.includes(cur)) netSel.value = cur;
    else if (debug && debug.net) netSel.value = debug.net;
  }
  renderCostGridOverlay();
}

async function fetchCostGridNet(net) {
  const q = net ? `?net=${encodeURIComponent(net)}` : "";
  try {
    costGridDebug = await apiJson(`/api/routing/debug/cost-grid${q}`);
  } catch (err) {
    toast(`cost grid fetch failed: ${err.message}`);
    return;
  }
  renderCostGridOverlay();
}

el("cost-grid-net")?.addEventListener("change", e => {
  costGridOverlay = el("cost-grid-toggle")?.checked ?? false;
  if (!costGridOverlay) return;
  fetchCostGridNet(e.target.value);
});

function clearCostGridOverlay() {
  // Raster images, terminal markers, and the floating legend share this
  // class (the legend div is HTML, not SVG — the selector still matches).
  viewport?.querySelectorAll(".cost-grid-overlay").forEach(n => n.remove());
  document.getElementById("cost-legend")?.remove();
}

// Display palette, mirroring COST_PALETTE / SECONDARY_PALETTE in
// ui/debug_grids.py (change both together). Single source for the canvas
// fallback painter and the floating legend below.
const COST_LEVELS = [
  { v: 0, name: "free", css: "rgba(0,0,0,0)" },
  { v: 25, name: "courtyard", css: "rgba(140,140,140,0.55)" },
  { v: 35, name: "pour", css: "rgba(46,168,122,0.55)" },
  { v: 50, name: "high-cost", css: "rgba(255,255,0,0.71)" },
  { v: 80, name: "edge-keepout", css: "rgba(255,165,0,0.78)" },
  { v: 100, name: "blocked ≥100", css: "rgba(255,0,0,0.86)" },
];
const SECONDARY_LEVELS = [
  { v: 1, name: "soft halo", css: "rgba(255,235,59,0.59)" },
  { v: 2, name: "inner halo", css: "rgba(255,140,0,0.67)" },
  { v: 3, name: "hard ring", css: "rgba(255,0,255,0.78)" },
];

function costLevelName(v) {
  if (v <= 0) return "free";
  for (const lv of COST_LEVELS) if (v <= lv.v) return lv.name;
  return "blocked ≥100";
}

// Board-mm geometry of one PNG raster layer. New payloads carry png_*
// (near-full grid resolution); old bundles only have the downsampled
// data grid, whose resolution_mm already includes its scale.
function costPngGeom(layer, originMm, fullResMm) {
  const cellMm = fullResMm * (layer.png_scale || 1);
  return {
    cellMm,
    xMm: originMm[0],
    yMm: originMm[1],
    wMm: layer.png_width * cellMm,
    hMm: layer.png_height * cellMm,
  };
}

function costFallbackGeom(layer, originMm, payloadResMm) {
  return {
    cellMm: payloadResMm,
    xMm: originMm[0],
    yMm: originMm[1],
    wMm: layer.width * payloadResMm,
    hMm: layer.height * payloadResMm,
  };
}

function appendCostImage(svg, href, geom) {
  const img = document.createElementNS("http://www.w3.org/2000/svg", "image");
  img.classList.add("cost-grid-overlay");
  img.style.pointerEvents = "none";
  img.style.opacity = "0.5";
  // Crisp cells under zoom: the raster is a grid, and bilinear smoothing
  // would imply precision it doesn't have.
  img.style.imageRendering = "pixelated";
  img.setAttribute("x", geom.xMm);
  img.setAttribute("y", geom.yMm);
  img.setAttribute("width", geom.wMm);
  img.setAttribute("height", geom.hMm);
  img.setAttribute("preserveAspectRatio", "none");
  img.setAttribute("href", href);
  svg.appendChild(img);
}

// Canvas fallback for payloads without png_b64 (bundles saved before the
// PNG transport). Paints the downsampled data grid with COST_LEVELS.
function paintCostCanvas(grid, gw, gh, secGrid) {
  const canvas = document.createElement("canvas");
  canvas.width = gw;
  canvas.height = gh;
  const ctx = canvas.getContext("2d");
  const imgData = ctx.createImageData(gw, gh);
  const px = imgData.data;
  const put = (o, v) => {
    if (v <= 0) return;
    const lv = COST_LEVELS.find(l => v <= l.v) || COST_LEVELS[COST_LEVELS.length - 1];
    const m = lv.css.match(/rgba?\((\d+),(\d+),(\d+)(?:,([\d.]+))?\)/);
    px[o] = +m[1]; px[o + 1] = +m[2]; px[o + 2] = +m[3];
    px[o + 3] = Math.round((m[4] === undefined ? 1 : +m[4]) * 255);
  };
  for (let r = 0; r < gh; r++) {
    const row = grid[r];
    for (let c = 0; c < gw; c++) put((r * gw + c) * 4, row[c]);
  }
  if (secGrid) {
    const bandCss = { 1: SECONDARY_LEVELS[0].css, 2: SECONDARY_LEVELS[1].css, 3: SECONDARY_LEVELS[2].css };
    for (let r = 0; r < gh; r++) {
      const row = secGrid[r];
      if (!row) continue;
      for (let c = 0; c < gw; c++) {
        const css = bandCss[row[c] >= 3 ? 3 : row[c]];
        if (!css) continue;
        const m = css.match(/rgba?\((\d+),(\d+),(\d+)(?:,([\d.]+))?\)/);
        const o = (r * gw + c) * 4;
        px[o] = +m[1]; px[o + 1] = +m[2]; px[o + 2] = +m[3];
        px[o + 3] = Math.round((m[4] === undefined ? 1 : +m[4]) * 255);
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);
  return canvas.toDataURL("image/png");
}

function renderCostLegend(hasSecondary) {
  document.getElementById("cost-legend")?.remove();
  const box = document.createElement("div");
  box.id = "cost-legend";
  box.className = "cost-grid-overlay";
  const sw = css => `<i style="display:inline-block;width:1.1rem;border-top:3px solid ${css};"></i>`;
  const primary = COST_LEVELS.filter(l => l.v > 0)
    .map(l => `<span title="cost ${l.v}">${sw(l.css)} ${l.name}</span>`).join("");
  const secondary = hasSecondary
    ? `<div class="cost-legend-row"><span class="cost-legend-head">halo</span>${SECONDARY_LEVELS.map(l => `<span>${sw(l.css)} ${l.name}</span>`).join("")}</div>`
      + `<div class="cost-legend-row"><span class="cost-legend-head">term</span>`
      + `<span>${sw("#22ff44")} open</span><span>${sw("#ff2222")} sealed</span></div>`
    : "";
  box.innerHTML = `<div class="cost-legend-row"><span class="cost-legend-head">cost</span>${primary}</div>`
    + secondary + `<div class="cost-legend-row cost-probe" id="cost-probe">hover board for cell value</div>`;
  viewport?.appendChild(box);
}

// Hover probe: cursor → board-mm (via the SVG transform, robust under
// pan/zoom) → coarse probe cell → exact raw value readout in the legend.
// The coarse data grid is the probe source; the PNG is display-only.
function probeCostCell(clientX, clientY) {
  const probe = document.getElementById("cost-probe");
  const svg = viewport?.querySelector("svg");
  const layer = costGridDebug?.layers.find(l => l.layer === "F.Cu");
  if (!probe || !svg || !layer?.data) return;
  const pt = new DOMPoint(clientX, clientY).matrixTransform(
    svg.getScreenCTM().inverse());
  const cellMm = costGridDebug.resolution_mm;
  const c = Math.floor((pt.x - costGridDebug.origin_mm[0]) / cellMm);
  const r = Math.floor((pt.y - costGridDebug.origin_mm[1]) / cellMm);
  if (r < 0 || c < 0 || r >= layer.height || c >= layer.width) {
    probe.textContent = "off grid";
    return;
  }
  const v = layer.data[r][c];
  let bandTxt = "";
  const sec = costGridDebug.secondary?.layers?.find(l => l.layer === "F.Cu");
  if (sec?.data?.[r]?.[c] !== undefined && layer.scale === sec.scale) {
    const b = sec.data[r][c];
    const names = { 0: "free", 1: "soft", 2: "inner", 3: "hard" };
    bandTxt = ` · halo ${names[b] ?? b}`;
  }
  probe.textContent = `cost ${v} (${costLevelName(v)})${bandTxt}`;
}

viewport?.addEventListener("mousemove", e => {
  if (!costGridOverlay || !costGridDebug) return;
  probeCostCell(e.clientX, e.clientY);
});
viewport?.addEventListener("mouseleave", () => {
  const probe = document.getElementById("cost-probe");
  if (probe) probe.textContent = "hover board for cell value";
});

function renderCostGridOverlay() {
  if (!viewport) return;
  if (!costGridDebug || !costGridOverlay) {
    clearCostGridOverlay();
    return;
  }

  clearCostGridOverlay();

  const svg = viewport?.querySelector("svg");
  if (!svg) return;

  // Use F.Cu layer data for the overlay. New payloads carry a near-full
  // resolution PNG per layer (png_b64); old bundles only have the
  // downsampled data grid and fall back to canvas painting.
  const fcuLayer = costGridDebug.layers.find(l => l.layer === "F.Cu");
  if (!fcuLayer) return;
  // SVG user units are board mm — the same space as footprints, terminal
  // dots, and partial-copper overlays. Grid row 0 sits at min-y
  // (see board_to_grid), matching the raster top.
  const fullResMm = costGridDebug.full_resolution_mm
    ?? (costGridDebug.resolution_mm / (fcuLayer.scale || 1));
  const ox = costGridDebug.origin_mm[0], oy = costGridDebug.origin_mm[1];

  const sec = costGridDebug.secondary;
  const secLayer = sec?.layers?.find(l => l.layer === "F.Cu");
  if (fcuLayer.png_b64) {
    const geom = costPngGeom(fcuLayer, costGridDebug.origin_mm, fullResMm);
    appendCostImage(svg, `data:image/png;base64,${fcuLayer.png_b64}`, geom);
    if (secLayer?.png_b64) {
      const sgeom = costPngGeom(secLayer, costGridDebug.origin_mm, fullResMm);
      appendCostImage(svg, `data:image/png;base64,${secLayer.png_b64}`, sgeom);
    }
  } else {
    // Legacy payload: paint the downsampled data grid to a canvas.
    const gw = fcuLayer.width, gh = fcuLayer.height;
    const href = paintCostCanvas(fcuLayer.data, gw, gh,
      (secLayer && fcuLayer.scale === secLayer.scale) ? secLayer.data : null);
    const geom = costFallbackGeom(fcuLayer, costGridDebug.origin_mm,
      costGridDebug.resolution_mm);
    appendCostImage(svg, href, geom);
  }

  // Terminal seals: vector circles (green = open, red = sealed shut).
  // F.Cu-layer terminals only, matching the raster panel. New payloads
  // carry full-resolution cell coords (fc/fr); legacy uses downsampled
  // coords, identical to grid_to_board either way.
  if (sec?.terminals?.length && secLayer) {
    const fcuIdx = sec.layers.findIndex(l => l.layer === "F.Cu");
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.classList.add("cost-grid-overlay", "overlay-none");
    for (const t of sec.terminals) {
      if (t.layer !== fcuIdx) continue;
      let cx, cy;
      if (t.fc !== undefined && t.fr !== undefined) {
        cx = ox + (t.fc + 0.5) * fullResMm;
        cy = oy + (t.fr + 0.5) * fullResMm;
      } else {
        const cellMm = costGridDebug.resolution_mm;
        cx = ox + (t.dc + 0.5) * cellMm;
        cy = oy + (t.dr + 0.5) * cellMm;
      }
      const cEl = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      cEl.setAttribute("cx", cx);
      cEl.setAttribute("cy", cy);
      cEl.setAttribute("r", 0.6);
      cEl.setAttribute("fill", "none");
      cEl.setAttribute("stroke", t.sealed ? "#ff2222" : "#22ff44");
      cEl.setAttribute("stroke-width", "0.25");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `term ${t.idx}: own-cell ${t.own_cell}${t.sealed ? " SEALED" : ""}`;
      cEl.appendChild(title);
      g.appendChild(cEl);
    }
    svg.appendChild(g);
  }
  renderCostLegend(!!secLayer);
}

async function fetchAndInjectSVG(version) {
  let src = boardSrc(version);
  // Motion owns the viewport while frames are loaded: its (dx, dy, da) group
  // transforms are deltas vs the COMMITTED board, so the base SVG must be the
  // committed view. An overlay=1 SVG already sits at the proposal pose
  // (apply_deltas) — re-applying motion on top doubles every move:
  // Step-1 looked fine, full Run flipped R8 out).
  // Also: force vectors must be rendered INSIDE footprint groups so they move
  // with the parts. Server renders forces at absolute coords; we need forces=0
  // and let the client inject them into the footprint groups.
  if (motion.frames.length) {
    src = src.replace("overlay=1", "overlay=0");
    src = src.replace("forces=1", "forces=0");
  }
  let svgText;
  try {
    const res = await apiFetch(src);
    svgText = await res.text();
  } catch (err) {
    toast(`board refresh failed: ${err.message}`);
    return;
  }
  if (!viewport) return;
  viewport.querySelectorAll("svg").forEach(n => n.remove());
  const tpl = document.createElement("template");
  tpl.innerHTML = svgText;
  for (const node of tpl.content.childNodes) viewport.appendChild(node);
  const svg = viewport?.querySelector("svg");
  // Single force layer while motion is active: the server base SVG must not
  // contribute absolute-coordinate static forces (they would stack under the
  // per-footprint client vectors and flicker). Normally forces=0 above already
  // excludes them; strip defensively for raced/cached injects.
  if (svg && motion.frames.length) {
    svg.querySelectorAll('g[class^="forces-"]').forEach(el => el.remove());
  }
  if (svg) {
    // Every injected SVG carries the native board viewBox: capture it before
    // applyViewBox() overwrites the attribute with the (possibly zoomed)
    // live viewBox. This is what Fit restores.
    const vbAttr = svg.getAttribute("viewBox");
    if (vbAttr) {
      const vb = vbAttr.split(" ").map(Number);
      if (vb.length === 4 && vb.every(Number.isFinite)) {
        initialViewBox = [...vb];
        if (viewBox === null) viewBox = [...vb];
      }
    }
  }
  applyViewBox();
  applyLayerVisibility();
  highlightSelectedNets();
  renderGndIslandsOverlay();
  renderTerminalsOverlay();
  // Re-apply footprint selection classes on the fresh nodes: every inject
  // path gets highlights by construction instead of relying on callers.
  if (typeof updateFootprintVisuals === "function") {
    try { updateFootprintVisuals(); } catch { /* paint is best-effort */ }
  }
  if (motion.frames.length) applyMotionFrame(motion.idx);
}
