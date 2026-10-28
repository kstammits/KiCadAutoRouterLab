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
// Best-effort and silent — zoom must never nag on a fetch hiccup.
async function zoomToNets() {
  if (!viewport?.querySelector("svg") || selectedNets.size === 0) return;
  try {
    const all = await Promise.all([...selectedNets].map(n =>
      apiJson(`/api/nets/terminals?net=${encodeURIComponent(n)}`).catch(() => null)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    let n = 0;
    for (const r of all) {
      for (const t of (r?.terminals || [])) {
        if (t.x_mm == null || t.y_mm == null) continue;
        x0 = Math.min(x0, t.x_mm); y0 = Math.min(y0, t.y_mm);
        x1 = Math.max(x1, t.x_mm); y1 = Math.max(y1, t.y_mm);
        n++;
      }
    }
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
  // Both the raster image and the terminal-marker group share this class.
  viewport?.querySelectorAll(".cost-grid-overlay").forEach(n => n.remove());
}

function renderCostGridOverlay() {
  if (!viewport) return;
  if (!costGridDebug || !costGridOverlay) {
    clearCostGridOverlay();
    return;
  }

  clearCostGridOverlay();

  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  
  // Get SVG viewBox for coordinate mapping
  const vbAttr = svg.getAttribute("viewBox");
  if (!vbAttr) return;
  const [vbx, vby, vbw, vbh] = vbAttr.split(" ").map(Number);
  
  // Use F.Cu layer data for the overlay (already downsampled to
  // max 200x200 by /api/routing/debug/cost-grid — no server PNG round-trip).
  const fcuLayer = costGridDebug.layers.find(l => l.layer === "F.Cu");
  if (!fcuLayer) return;

  // Rasterize the downsampled grid to a canvas (same palette as the old
  // server PNG: 25=gray COURTYARD, 35=teal POUR, 50=yellow HIGH_COST,
  // 80=orange EDGE_KEEPOUT, 100+=red BLOCKED, else transparent).
  // Per-net secondary bands paint on top: 1=yellow soft halo, 2=orange
  // inner halo, 3=magenta hard ring.
  const gw = fcuLayer.width, gh = fcuLayer.height;
  const grid = fcuLayer.data;
  const canvas = document.createElement("canvas");
  canvas.width = gw;
  canvas.height = gh;
  const ctx = canvas.getContext("2d");
  const imgData = ctx.createImageData(gw, gh);
  const px = imgData.data;
  for (let r = 0; r < gh; r++) {
    const row = grid[r];
    for (let c = 0; c < gw; c++) {
      const v = row[c];
      if (v <= 0) continue;
      const o = (r * gw + c) * 4;
      if (v === 25) { px[o] = 140; px[o + 1] = 140; px[o + 2] = 140; px[o + 3] = 140; }
      else if (v === 35) { px[o] = 46; px[o + 1] = 168; px[o + 2] = 122; px[o + 3] = 140; }
      else if (v === 50) { px[o] = 255; px[o + 1] = 255; px[o + 2] = 0; px[o + 3] = 180; }
      else if (v === 80) { px[o] = 255; px[o + 1] = 165; px[o + 2] = 0; px[o + 3] = 200; }
      else if (v >= 100) { px[o] = 255; px[o + 1] = 0; px[o + 2] = 0; px[o + 3] = 220; }
    }
  }
  const sec = costGridDebug.secondary;
  const secLayer = sec?.layers?.find(l => l.layer === "F.Cu");
  if (secLayer) {
    for (let r = 0; r < gh; r++) {
      const row = secLayer.data[r];
      if (!row) continue;
      for (let c = 0; c < gw; c++) {
        const b = row[c];
        if (b <= 0) continue;
        const o = (r * gw + c) * 4;
        if (b === 1) { px[o] = 255; px[o + 1] = 235; px[o + 2] = 59; px[o + 3] = 150; }
        else if (b === 2) { px[o] = 255; px[o + 1] = 140; px[o + 2] = 0; px[o + 3] = 170; }
        else if (b >= 3) { px[o] = 255; px[o + 1] = 0; px[o + 2] = 255; px[o + 3] = 200; }
      }
    }
  }
  ctx.putImageData(imgData, 0, 0);

  // Create image element for the overlay
  const img = document.createElementNS("http://www.w3.org/2000/svg", "image");
  img.classList.add("cost-grid-overlay");
  img.style.pointerEvents = "none";
  img.style.opacity = "0.5";

  // Map cost grid bounds to board coordinates
  const originX = costGridDebug.origin_mm[0];
  const originY = costGridDebug.origin_mm[1];
  const resolutionMm = costGridDebug.resolution_mm;
  const scale = fcuLayer.scale;
  const gridWidthMm = fcuLayer.width * scale * resolutionMm;
  const gridHeightMm = fcuLayer.height * scale * resolutionMm;

  // Position in board mm coordinates
  const xMm = originX;
  const yMm = originY + gridHeightMm; // Bottom of grid in board coords

  // Map to SVG coordinates (viewport units)
  const xPx = ((xMm - vbx) / vbw) * viewport.clientWidth;
  const yPx = ((vby + vbh - yMm) / vbh) * viewport.clientHeight;
  const widthPx = (gridWidthMm / vbw) * viewport.clientWidth;
  const heightPx = (gridHeightMm / vbh) * viewport.clientHeight;

  img.setAttribute("x", xPx);
  img.setAttribute("y", yPx - heightPx);
  img.setAttribute("width", widthPx);
  img.setAttribute("height", heightPx);
  img.setAttribute("preserveAspectRatio", "none");
  img.setAttribute("href", canvas.toDataURL("image/png"));
  svg.appendChild(img);

  // Terminal seals: full-resolution terminal cells would vanish in the
  // downsampled raster, so draw them as vector circles in the same pixel
  // space (green = open, red = sealed shut). F.Cu-layer terminals only,
  // matching the raster panel.
  if (sec?.terminals?.length && secLayer) {
    const fcuIdx = sec.layers.findIndex(l => l.layer === "F.Cu");
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.classList.add("cost-grid-overlay", "overlay-none");
    const cellPx = widthPx / gw;
    for (const t of sec.terminals) {
      if (t.layer !== fcuIdx) continue;
      const cx = xPx + ((t.dc + 0.5) / gw) * widthPx;
      const cy = (yPx - heightPx) + ((t.dr + 0.5) / gh) * heightPx;
      const cEl = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      cEl.setAttribute("cx", cx);
      cEl.setAttribute("cy", cy);
      cEl.setAttribute("r", Math.max(3, cellPx * 1.5));
      cEl.setAttribute("fill", "none");
      cEl.setAttribute("stroke", t.sealed ? "#ff2222" : "#22ff44");
      cEl.setAttribute("stroke-width", "2");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `term ${t.idx}: own-cell ${t.own_cell}${t.sealed ? " SEALED" : ""}`;
      cEl.appendChild(title);
      g.appendChild(cEl);
    }
    svg.appendChild(g);
  }
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
  if (motion.frames.length) applyMotionFrame(motion.idx);
}
