// app.js domain map (single-file UI, deliberately unsplit — see triggers).
// Domains live in commented blocks; each owns the globals listed, everyone
// else only reads them:
//   core       el/on, apiFetch/apiJson/postJSON/jobUrl, toast, viewport,
//              resizers (this file, top)
//   viewer     viewBox, applyViewBox/fitToView/frameBox, svgEl, layer toggles,
//              all SVG overlays (cost grid, DRC, forces, islands)
//   placement  allFootprints, selectedUuids/pinnedUuids, lastVersion,
//              footprint table, ghosts, motion player, run/accept/undo
//   nets       selectedNets, lastNetsByName, nets table, power/protect toggles
//   routing    ROUTE_* caps, jobs, routeDiag/routeEst, routing params panel
//   diagnostics  GND islands, impedance, bridges (read selection + viewer only)
//   docs       rendered-docs viewer (standalone)
// Load order is declaration order in index.html <script> tags (single tag
// today). Cross-domain calls are fine (zoomToNets -> frameBox); new globals
// must be added to the owning domain's list here.
// Do NOT split this file until a trigger fires: (a) a second regular
// contributor and routine app.js merge conflicts, (b) one domain >1500
// lines, or (c) a cross-domain regression caused by file organization.
// Rationale: no JS test harness exists, so a split buys navigation only,
// at the cost of blame churn + load-order failure modes.

// Calibrated behavior knobs get main-panel sliders; power/decap/ghost and
// the convergence threshold live under Advanced. Everything else
// (repulsion, solver internals, mass fallbacks) stays API-only at
// calibrated defaults — POST /api/placement/params merges, so hidden keys
// in placement.json survive GUI saves.
// Ranges cover both Cluster (scattered starts) and Legalize (placed boards) regimes.

const diagEl = el("diag");
// Hot-button/diag lookups cached once (script runs after the DOM, as above).
const undoBtn = el("undo-btn");
const stepSelectedBtn = el("step-selected-btn");
const acceptBtn = el("accept-btn");
const routeDiag = el("route-diag");
const ghostDiag = el("ghost-diag");
const docsSelect = el("docs-select");
const bridgesDiag = el("bridges-diag");
const inletsDiag = el("inlets-diag");

function setConnected(ok) {
  const badge = el("conn-badge");
  if (badge) badge.hidden = ok;
  if (!ok && diagEl) {
    if (diagEl) diagEl.textContent = "disconnected — server unreachable (retrying)";
  }
}

// Route/rip buttons share one busy state: all three disable, the two
// route buttons show the spinner. (ripSelectedNets manages ripBtn's own
// spinner; route tails rely on updateNetSelectionUI for final enable.)
function setRouteBusy(on) {
  const btns = ["route-selected-btn", "route-all-btn", "rip-selected-btn"]
    .map(id => el(id));
  for (const b of btns) if (b) b.disabled = on;
  for (const b of btns.slice(0, 2)) if (b) b.classList.toggle("loading", on);
}

// Inlet/bridge state lives in one endpoint; read-modify-write callers
// share these so the URL and parse appear once.
async function getInlets() {
  return apiJson("/api/nets/inlets");
}
async function setInlets(body) {
  return postJSON("/api/nets/inlets", body);
}

async function poll() {
  let st;
  try {
    st = await apiJson("/api/state");
  } catch (err) {
    // Server down: badge it and skip all downstream refresh work. The
    // 2s tick doubles as the reconnect detector — next success clears it.
    setConnected(false);
    return;
  }
  setConnected(true);
  const dropZone = el("drop-zone");
  if (st.loaded) {
    if (boardEl) boardEl.hidden = false;
    if (sidePanel) sidePanel.hidden = false;
    if (dropZone) dropZone.hidden = true;
    if (nameEl) nameEl.textContent = `${st.name} — v${st.version}`;
    if (schChip) schChip.hidden = st.has_sch;
    const dlBtn = el("download-btn");
    if (dlBtn) dlBtn.hidden = false;
    const resetBtn = el("reset-btn");
    if (resetBtn) resetBtn.hidden = false;
    renderLayerToggles(st.copper_layers);
    updateModeUI(st);
    if (st.version !== lastVersion) {
      lastVersion = st.version;
      if (motion.playing) pauseMotion();
      resetMotionState();
      // Island analysis is board-version specific: drop the overlay and
      // force a fresh analyze on next click instead of showing stale boxes.
      gndIslandsData = null;
      gndIslandsVisible = false;
      const gndBtn = el("gnd-islands-btn");
      if (gndBtn) { gndBtn.textContent = "GND Islands"; gndBtn.disabled = false; }
      await fetchAndInjectSVG(st.version);
      await loadSelectionState();
      await loadMotionFrames(true);
    }
  } else {
    if (sidePanel) sidePanel.hidden = true;
    if (dropZone) dropZone.hidden = false;
    const dlBtn = el("download-btn");
    if (dlBtn) dlBtn.hidden = true;
    const resetBtn = el("reset-btn");
    if (resetBtn) resetBtn.hidden = true;
    if (undoBtn) undoBtn.disabled = true;
    lastVersion = 0;
    (viewport?.querySelectorAll("svg") ?? []).forEach(elm => elm.remove());
    viewBox = null;
  }
}

overlayToggle?.addEventListener("change", async () => {
  if (lastVersion) {
    await fetchAndInjectSVG(lastVersion);
    await loadSelectionState();
  }
});

async function refreshForces() {
  if (!lastVersion) return;
  const btn = el("refresh-forces-btn");
  const originalText = btn?.textContent;
  if (btn) { btn.textContent = "⟳"; btn.disabled = true; }
  try {
    const res = await apiFetch("/api/placement/forces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readParams())
    });
    if (diagEl) diagEl.textContent = "forces refreshed";
  } catch (err) {
    toast(`forces failed: ${err.message}`);
    if (forcesToggle) forcesToggle.checked = false;
    const legend = el("forces-legend");
    if (legend) legend.style.display = "none";
    if (diagEl) diagEl.textContent = `forces failed: ${err.message}`;
  } finally {
    if (btn) { btn.textContent = originalText; btn.disabled = false; }
  }
  await fetchAndInjectSVG(lastVersion);
  await loadSelectionState();
}

forcesToggle?.addEventListener("change", async () => {
  const legend = el("forces-legend");
  if (legend && forcesToggle) legend.style.display = forcesToggle.checked ? "inline-flex" : "none";
  if (!lastVersion) return;
  if (forcesToggle.checked) {
    await refreshForces();
  } else {
    await fetchAndInjectSVG(lastVersion);
    await loadSelectionState();
  }
  // No extra drawForceVectors here: fetchAndInjectSVG re-applies the motion
  // frame (which draws vectors) after inject. Drawing on the pre-inject SVG
  // would flash then be wiped by the inject response.
});

const drcRunBtn = el("drc-run-btn");
const drcInfo = el("drc-info");

function drcCountText(r) {
  const n = Array.isArray(r.violations) ? r.violations.length : (r.violations ?? 0);
  const unc = r.unconnected ?? 0;
  return `DRC: ${n} violations, ${unc} unconnected`;
}

async function runDrc() {
  if (!lastVersion) return;
  drcRunBtn.disabled = true;
  drcRunBtn.classList.add("loading");
  drcInfo.textContent = "DRC running…";
  try {
    const r = await apiJson("/api/drc", { method: "POST" });
    if (r.error) throw new Error(r.error);
    drcInfo.textContent = drcCountText(r) + (r.cached ? " (cached)" : "");
    drcToggle.checked = true;
  } catch (err) {
    drcInfo.textContent = `DRC failed: ${err.message}`;
  } finally {
    drcRunBtn.disabled = false;
    drcRunBtn.classList.remove("loading");
  }
  await fetchAndInjectSVG(lastVersion);
}

drcRunBtn.addEventListener("click", runDrc);

drcToggle.addEventListener("change", async () => {
  if (!lastVersion) return;
  if (!drcToggle.checked) {
    await fetchAndInjectSVG(lastVersion);
    return;
  }
  // Toggle-on: run DRC when the cache is empty, stale, or errored.
  try {
    const st = await apiJson("/api/drc/status");
    if (st.for_version == null || st.stale || st.error) {
      await runDrc();
      return;
    }
    drcInfo.textContent = `DRC: ${st.violations} violations, ${st.unconnected} unconnected (cached)`;
  } catch (err) {
    drcInfo.textContent = `DRC status failed: ${err.message}`;
  }
  await fetchAndInjectSVG(lastVersion);
});

function layerGroup(svg, layer, kind) {
  // Attribute selector: group ids contain dots ("layer-F.Cu-tracks"),
  // which a #id querySelector would misparse as id + class.
  return svg.querySelector(`[id="layer-${layer}-${kind}"]`);
}

function applyLayerVisibility() {
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  const zonesOn = zonesToggle?.checked ?? true;
  const layers = layerToggleState();
  // Solo isolate (display-only): when set, only that layer shows.
  const effective = new Map();
  for (const [layer, on] of layers) effective.set(layer, soloLayer ? layer === soloLayer : on);
  for (const [layer, layerOn] of effective) {
    const tracks = layerGroup(svg, layer, "tracks");
    if (tracks) tracks.style.display = layerOn ? "" : "none";
    const zones = layerGroup(svg, layer, "zones");
    if (zones) zones.style.display = (layerOn && zonesOn) ? "" : "none";
  }
  // Vias span layers (data-layers): visible when any spanned layer is on.
  // Solo narrows the span test to the isolated layer.
  svg.querySelectorAll("#layer-vias .via").forEach(el => {
    const span = (el.getAttribute("data-layers") || "F.Cu B.Cu").split(/\s+/).filter(Boolean);
    const visible = span.length ? span.some(l => effective.get(l) ?? false) : true;
    el.style.display = visible ? "" : "none";
  });
  const legend = el("layer-legend");
  if (legend) {
    const layersArr = [...layers.keys()];
    if (layersArr.length) renderLayerLegend(layersArr);
  }
}

layerTogglesEl?.addEventListener("change", applyLayerVisibility);

zonesToggle.addEventListener("change", applyLayerVisibility);

zonesFillToggle.addEventListener("change", async () => {
  await fetchAndInjectSVG(lastVersion);
});

el("cost-grid-toggle")?.addEventListener("change", e => {
  costGridOverlay = e.target.checked;
  if (costGridOverlay) {
    const net = el("cost-grid-net")?.value || "";
    if (net && (!costGridDebug?.secondary || costGridDebug?.net !== net)) {
      fetchCostGridNet(net);
      return;
    }
  }
  renderCostGridOverlay();
});

// Zoom / Pan handlers
viewport?.addEventListener("wheel", e => {
  e.preventDefault();
  if (!viewBox) return;
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  // Mouse position in SVG user coordinates (before zoom), exact under any aspect ratio
  const ctm = svg.getScreenCTM();
  if (!ctm) return;
  const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
  // Zoom factor
  const factor = e.deltaY > 0 ? 1.2 : 0.8333;
  // Keep mouse point fixed: anchor the new viewBox around p
  viewBox[0] = p.x - (p.x - viewBox[0]) * factor;
  viewBox[1] = p.y - (p.y - viewBox[1]) * factor;
  viewBox[2] *= factor;
  viewBox[3] *= factor;
  applyViewBox();
}, { passive: false });

let isMarqueeSelecting = false;
let marqueeStart = null;
let marqueeEl = null;

viewport?.addEventListener("mousedown", e => {
  if (e.button !== 0) return; // left click only
  
  // Check if clicking on a footprint
  const fpEl = e.target.closest(".footprint");
  const isEmptySpace = !fpEl;
  
  // Shift + click empty space = marquee select
  // Click empty space = pan
  // Click footprint = select (handled by click handler), drag = pan
  if (isEmptySpace && e.shiftKey) {
    isMarqueeSelecting = true;
    const startRect = viewport.getBoundingClientRect();
    marqueeStart = { x: e.clientX - startRect.left, y: e.clientY - startRect.top };
    marqueeEl = document.createElement("div");
    marqueeEl.className = "marquee";
    viewport.appendChild(marqueeEl);
    viewport.classList.add("selecting");
    e.preventDefault();
    return;
  }
  
  // Start panning (empty space without shift, or footprint drag)
  // Guard: viewBox is null until the first SVG loads — spreading null throws
  // "viewBox is not iterable" and breaks all subsequent mouse handling.
  if (!viewBox) return;
  isPanning = true;
  panStart = { x: e.clientX, y: e.clientY, vb: [...viewBox] };
  viewport.classList.add("panning");
});

window.addEventListener("mousemove", e => {
  if (isMarqueeSelecting && marqueeEl && marqueeStart) {
    const moveRect = viewport.getBoundingClientRect();
    const mx = e.clientX - moveRect.left;
    const my = e.clientY - moveRect.top;
    const left = Math.min(marqueeStart.x, mx);
    const top = Math.min(marqueeStart.y, my);
    const width = Math.abs(mx - marqueeStart.x);
    const height = Math.abs(my - marqueeStart.y);
    marqueeEl.style.left = left + "px";
    marqueeEl.style.top = top + "px";
    marqueeEl.style.width = width + "px";
    marqueeEl.style.height = height + "px";
    return;
  }
  
  if (!isPanning || !viewBox) return;
  // Uniform rendered scale under preserveAspectRatio="xMidYMid meet" (px per user unit)
  const scale = Math.min(viewport.clientWidth / viewBox[2], viewport.clientHeight / viewBox[3]);
  const dx = (e.clientX - panStart.x) / scale;
  const dy = (e.clientY - panStart.y) / scale;
  viewBox[0] = panStart.vb[0] - dx;
  viewBox[1] = panStart.vb[1] - dy;
  applyViewBox();
});

window.addEventListener("mouseup", e => {
  if (isMarqueeSelecting) {
    // Process marquee selection
    if (marqueeEl) {
      const rect = marqueeEl.getBoundingClientRect();

      // Convert marquee bounds to SVG user coordinates via the browser's exact CTM
      const svg = viewport?.querySelector("svg");
      if (svg && viewBox) {
        const inv = svg.getScreenCTM().inverse();
        const p1 = new DOMPoint(rect.left, rect.top).matrixTransform(inv);
        const p2 = new DOMPoint(rect.right, rect.bottom).matrixTransform(inv);

        const minX = Math.min(p1.x, p2.x);
        const maxX = Math.max(p1.x, p2.x);
        const minY = Math.min(p1.y, p2.y);
        const maxY = Math.max(p1.y, p2.y);
        
        // Find footprints whose centroids are within the marquee
        svg.querySelectorAll(".footprint").forEach(fpEl => {
          const uuid = fpEl.dataset.fpUuid;
          if (!uuid) return;
          
          // Get footprint centroid from the group transform or compute from courtyard
          const fp = allFootprints.find(f => f.uuid === uuid);
          if (fp) {
            const cx = fp.x_mm;
            const cy = fp.y_mm;
            if (cx >= minX && cx <= maxX && cy >= minY && cy <= maxY) {
              if (!fp.locked && !fp.pinned && !fp.ghost) {
                if (e.shiftKey) {
                  selectedUuids.add(uuid);
                } else {
                  selectedUuids.add(uuid);
                }
              }
            }
          }
        });
        
        // Update server
        selectedUuids.forEach(uuid => {
          fetch("/api/placement/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uuid })
          });
        });
        
        updateFootprintVisuals();
        updateSelectionUI();
        renderFootprintTable();
      }
    }
    
    marqueeEl.remove();
    marqueeEl = null;
    marqueeStart = null;
    isMarqueeSelecting = false;
    viewport.classList.remove("selecting");
    return;
  }
  
  if (isPanning) {
    isPanning = false;
    viewport.classList.remove("panning");
  }
});

fitBtn.addEventListener("click", fitToView);

// --- Selection state ---
let selectedUuids = new Set();
let pinnedUuids = new Set();
let selectedGhostUuid = null; // Ghost selected for move/delete (never in selectedUuids)

function updateSelectionUI() {
  const selToolbar = el("sel-toolbar");
  const selCount = el("sel-count");
  const pinBtn = el("pin-btn");

  if (selToolbar) {
    selToolbar.hidden = !(selectedUuids.size > 0 || pinnedUuids.size > 0);
  }
  if (selCount) selCount.textContent = `${selectedUuids.size} selected, ${pinnedUuids.size} pinned`;
  if (pinBtn) {
    pinBtn.textContent = selectedUuids.size > 0 ? "Pin" : "Unpin";
    pinBtn.disabled = selectedUuids.size === 0;
  }
  // Place-bar scope hint: Step/Run target the selection, or all movable when empty.
  const scopeHint = el("place-scope-hint");
  if (scopeHint) {
    scopeHint.textContent = selectedUuids.size > 0
      ? `${selectedUuids.size} selected → Step/Run targets selection`
      : "nothing selected → Step/Run targets all movable";
  }
  // Nudge pad mirrors the placement selection (ghosts excluded).
  const nudgeCount = el("nudge-count");
  if (nudgeCount) nudgeCount.textContent = String(selectedUuids.size);
  for (const id of ["nudge-up", "nudge-down", "nudge-left", "nudge-right", "nudge-ccw", "nudge-cw"]) {
    const btn = el(id);
    if (btn) btn.disabled = selectedUuids.size === 0;
  }
}

async function fetchPinned() {
  try {
    const data = await apiJson("/api/placement/pinned");
    pinnedUuids = new Set(data.pinned || []);
    updateSelectionUI();
  } catch (err) {
    toast(`pinned refresh failed: ${err.message}`);
  }
}

async function fetchSelected() {
  try {
    const data = await apiJson("/api/placement/selected");
    selectedUuids = new Set(data.selected || []);
    updateSelectionUI();
  } catch (err) {
    toast(`selection refresh failed: ${err.message}`);
  }
}

function updateFootprintVisuals() {
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  svg.querySelectorAll(".footprint").forEach(el => {
    const uuid = el.dataset.fpUuid;
    if (!uuid) return;
    el.classList.toggle("fp-selected", selectedUuids.has(uuid) || uuid === selectedGhostUuid);
    el.classList.toggle("fp-pinned", pinnedUuids.has(uuid));
  });
}

async function togglePin(uuid) {
  try {
    const data = await postJSON("/api/placement/pin", { uuid });
    pinnedUuids = new Set(data.pinned || []);
    // If pinned, remove from selected
    selectedUuids.delete(uuid);
    updateFootprintVisuals();
    updateSelectionUI();
  } catch (err) {
    toast(`pin failed: ${err.message}`);
  }
}

async function toggleSelect(uuid) {
  if (pinnedUuids.has(uuid)) return; // Can't select pinned
  const fp = allFootprints.find(f => f.uuid === uuid);
  if (fp && fp.ghost) return; // Ghosts use selectGhost, never placement selection
  if (selectedUuids.has(uuid)) {
    selectedUuids.delete(uuid);
  } else {
    selectedUuids.add(uuid);
  }
  try {
    await fetch("/api/placement/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuid })
    });
    updateFootprintVisuals();
    updateSelectionUI();
  } catch (err) {
    toast(`select failed: ${err.message}`);
  }
}

async function selectAllUnlocked() {
  try {
    const data = await apiJson("/api/placement/select_all", { method: "POST" });
    selectedUuids = new Set(data.selected || []);
    updateFootprintVisuals();
    updateSelectionUI();
  } catch (err) {
    toast(`select all failed: ${err.message}`);
  }
}

async function clearSelection() {
  selectedUuids.clear();
  selectedGhostUuid = null;
  try {
    await fetch("/api/placement/clear_selection", { method: "POST" });
    updateFootprintVisuals();
    updateSelectionUI();
    renderFootprintTable();
    updateGhostSelUI();
  } catch (err) {
    toast(`clear selection failed: ${err.message}`);
  }
}

// Click handler on SVG for footprint selection
viewport?.addEventListener("click", e => {
  if (isPanning) return; // Don't select while panning
  const fpEl = e.target.closest(".footprint");
  if (fpEl) {
    const uuid = fpEl.dataset.fpUuid;
    if (!uuid) return;

    // Ghosts select into the ghost panel (move/delete), never into the
    // placement selection that Step/Run operates on.
    const clickedFp = allFootprints.find(f => f.uuid === uuid);
    if (clickedFp && clickedFp.ghost) {
      selectGhost(uuid);
      return;
    }
    if (e.shiftKey) {
      selectGhost(null); // switching back to parts clears ghost focus
      toggleSelect(uuid);
    } else if (e.altKey) {
      togglePin(uuid);
    } else {
      // Single select: clear others, select this
      if (!pinnedUuids.has(uuid)) {
        selectGhost(null); // switching back to parts clears ghost focus
        selectedUuids.clear();
        selectedUuids.add(uuid);
        updateFootprintVisuals();
        updateSelectionUI();
      }
    }
    return;
  }
  // Copper click selects its net (net-only: never touches footprint selection)
  const copperEl = e.target.closest(".track, .via");
  if (copperEl) {
    selectNetFromCopper(copperEl.dataset.net, e.shiftKey || e.ctrlKey || e.metaKey);
  }
});

function highlightSelectedNets() {
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  svg.querySelectorAll(".track, .via").forEach(el => {
    el.classList.toggle("net-selected", selectedNets.has(el.dataset.net));
  });
}

function selectNetFromCopper(net, additive) {
  if (!net) {
    const diag = routeDiag;
    if (diag) diag.textContent = "copper has no net assignment";
    return;
  }
  if (additive) {
    if (selectedNets.has(net)) selectedNets.delete(net);
    else selectedNets.add(net);
  } else {
    selectedNets.clear();
    selectedNets.add(net);
  }
  // Net-only: never touch selectedUuids / footprints.
  highlightSelectedNets();
  // Sync nets table rows if rendered (single helper: rows + header + counts)
  syncNetRowSelection();
  const diag = routeDiag;
  if (diag) {
    diag.textContent = selectedNets.size
      ? `net: ${[...selectedNets].join(", ")}`
      : "";
  }
}

// Load footprints list from server
let allFootprints = []; // Cache of all footprints from server
// Legality state from last placement run
let illegalUuids = new Set(); // UUIDs in overlapping_pairs or wall_violations
let lastLegality = {};        // Full legality report for diagnostics/tooltips
let fpSortKey = null; // Current sort column: ref | footprint | layer | nets | state
let fpSortDir = 1; // 1 = asc, -1 = desc

function fpStateRank(fp) {
  if (fp.ghost) return 0;
  if (fp.locked) return 1;
  if (fp.pinned) return 2;
  if (fp.selected) return 3;
  return 4;
}

function compareFootprints(a, b) {
  if (!fpSortKey) return 0;
  let r = 0;
  if (fpSortKey === "ref") {
    r = (a.ref || "").localeCompare(b.ref || "", undefined, { numeric: true, sensitivity: "base" });
  } else if (fpSortKey === "footprint") {
    r = (a.footprint_id || "").localeCompare(b.footprint_id || "", undefined, { numeric: true, sensitivity: "base" });
  } else if (fpSortKey === "layer") {
    r = (a.layer || "").localeCompare(b.layer || "");
  } else if (fpSortKey === "nets") {
    const an = (a.nets && a.nets[0]) || "";
    const bn = (b.nets && b.nets[0]) || "";
    r = an.localeCompare(bn, undefined, { sensitivity: "base" }) || ((a.nets?.length || 0) - (b.nets?.length || 0));
  } else if (fpSortKey === "state") {
    r = fpStateRank(a) - fpStateRank(b);
  }
  if (r === 0) {
    // Stable tiebreak by ref (natural order)
    r = (a.ref || "").localeCompare(b.ref || "", undefined, { numeric: true, sensitivity: "base" });
  }
  return r * fpSortDir;
}

function updateFpSortHeaders() {
  document.querySelectorAll("#fp-table th.sortable").forEach(th => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.sort === fpSortKey) {
      th.classList.add(fpSortDir === 1 ? "sorted-asc" : "sorted-desc");
    }
  });
}

async function fetchFootprints() {
  if (!lastVersion) return;
  try {
    const data = await apiJson("/api/footprints");
    allFootprints = data.footprints || [];
    // Drop ghost focus when its ghost is gone (deleted/reset/undo).
    if (selectedGhostUuid && !allFootprints.some(fp => fp.uuid === selectedGhostUuid)) {
      selectedGhostUuid = null;
      updateGhostSelUI();
    }
    renderFootprintTable();
  } catch (err) {
    toast(`footprints refresh failed: ${err.message}`);
  }
}

function getFilterValues() {
  const val = (id, fallback = "") => el(id)?.value ?? fallback;
  return {
    search: val("fp-search").toLowerCase(),
    ref: val("filter-ref").toLowerCase(),
    net: val("filter-net").toLowerCase(),
    layer: val("filter-layer"),
    state: val("filter-state"),
    hideLocked: el("filter-hide-locked")?.checked ?? false,
    hideFixed: el("filter-hide-fixed")?.checked ?? false,
  };
}

function matchesFilter(fp, filters) {
  if (filters.hideLocked && fp.locked) return false;
  if (filters.hideFixed && (fp.locked || fp.pinned)) return false;
  if (filters.layer && fp.layer !== filters.layer) return false;
  if (filters.state) {
    if (filters.state === "movable" && (fp.locked || fp.pinned)) return false;
    if (filters.state === "locked" && !fp.locked) return false;
    if (filters.state === "pinned" && !fp.pinned) return false;
    if (filters.state === "selected" && !fp.selected) return false;
    if (filters.state === "ghost" && !fp.ghost) return false;
  }
  if (filters.ref) {
    const pattern = filters.ref.replace(/\*/g, ".*");
    const re = new RegExp(`^${pattern}$`, "i");
    if (!re.test(fp.ref)) return false;
  }
  if (filters.net) {
    const pattern = filters.net.replace(/\*/g, ".*");
    const re = new RegExp(`^${pattern}$`, "i");
    if (!fp.nets.some(n => re.test(n))) return false;
  }
  if (filters.search) {
    const s = filters.search;
    const inRef = fp.ref.toLowerCase().includes(s);
    const inFootprint = fp.footprint_id.toLowerCase().includes(s);
    const inNets = fp.nets.some(n => n.toLowerCase().includes(s));
    if (!inRef && !inFootprint && !inNets) return false;
  }
  return true;
}

function getFilteredFootprints() {
  const filters = getFilterValues();
  const filtered = allFootprints.filter(fp => matchesFilter(fp, filters));
  if (fpSortKey) filtered.sort(compareFootprints);
  return filtered;
}

function getStateBadge(fp) {
  if (fp.ghost) return '<span class="state-badge state-ghost">Ghost</span>';
  if (fp.locked) return '<span class="state-badge state-locked">Locked</span>';
  if (fp.pinned) return '<span class="state-badge state-pinned">Pinned</span>';
  if (fp.selected) return '<span class="state-badge state-selected">Selected</span>';
  if (illegalUuids.has(fp.uuid)) return '<span class="state-badge state-illegal">⚠ Illegal</span>';
  return '<span class="state-badge state-movable">Movable</span>';
}

function getLegalityTooltip(fp) {
  if (!lastLegality || !illegalUuids.has(fp.uuid)) return "";
  const parts = [];
  if (lastLegality.overlapping_pairs) {
    for (const [ua, ub, area] of lastLegality.overlapping_pairs) {
      if (ua === fp.uuid || ub === fp.uuid) {
        const peer = ua === fp.uuid ? ub : ua;
        parts.push(`Overlaps ${peer} (${area.toFixed(3)} mm²)`);
      }
    }
  }
  if (lastLegality.wall_violations) {
    for (const [uuid, depth] of lastLegality.wall_violations) {
      if (uuid === fp.uuid) {
        parts.push(`Wall penetration ${depth.toFixed(3)} mm`);
      }
    }
  }
  return parts.join("; ");
}

function fpRowHtml(fp) {
  const nets = Array.isArray(fp.nets) ? fp.nets : [];
  const netsStr = escapeHtml(nets.slice(0, 3).join(", ") + (nets.length > 3 ? ` +${nets.length - 3}` : ""));
  const layerClass = fp.layer === "B" ? "B" : "F";
  const isGhostSel = fp.ghost && selectedGhostUuid === fp.uuid;
  const isIllegal = illegalUuids.has(fp.uuid);
  const tooltip = escapeHtml(getLegalityTooltip(fp));
  const ref = escapeHtml(fp.ref);
  const fpId = escapeHtml(fp.footprint_id);
  const uuid = escapeHtml(fp.uuid);
  const netsTitle = escapeHtml(nets.join(", "));
  return `
    <tr class="${fp.selected || isGhostSel ? 'fp-selected-row' : ''} ${fp.locked ? 'fp-locked-row' : ''} ${fp.pinned ? 'fp-pinned-row' : ''} ${fp.ghost ? 'fp-ghost-row' : ''} ${isIllegal ? 'fp-illegal-row' : ''}" data-uuid="${uuid}" ${tooltip ? `title="${tooltip}"` : ''}>
      <td class="fp-col-select"><input type="checkbox" ${fp.selected ? 'checked' : ''} ${fp.locked || fp.pinned || fp.ghost ? 'disabled' : ''} data-uuid="${uuid}"></td>
      <td class="fp-col-ref" title="${ref}">${ref}</td>
      <td class="fp-col-footprint" title="${fpId}">${fpId}</td>
      <td class="fp-col-layer"><span class="fp-col-layer ${layerClass}">${escapeHtml(fp.layer)}</span></td>
      <td class="fp-col-nets" title="${netsTitle}">${netsStr}</td>
      <td class="fp-col-state">${getStateBadge(fp)}</td>
    </tr>
  `;
}

function renderFootprintTable() {
  const tbody = el("fp-tbody");
  if (!tbody) return;
  const filtered = getFilteredFootprints();
  updateFpSortHeaders();
  tbody.innerHTML = filtered.map(fpRowHtml).join("");
  
  // Add click handlers for checkboxes
  tbody.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", e => {
      const uuid = e.target.dataset.uuid;
      if (e.target.checked) {
        selectedUuids.add(uuid);
      } else {
        selectedUuids.delete(uuid);
      }
      // Update server
      fetch("/api/placement/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid })
      });
      updateFootprintVisuals();
      updateSelectionUI();
      renderFootprintTable(); // Re-render to update row highlighting
      zoomToSelection();
    });
  });
  
  // Add click handlers for rows (click row to select)
  tbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", e => {
      if (e.target.type === "checkbox") return; // Handled by checkbox
      const uuid = tr.dataset.uuid;
      if (!uuid) return;
      const fp = allFootprints.find(f => f.uuid === uuid);
      if (!fp) return;
      if (fp.ghost) { selectGhost(selectedGhostUuid === uuid ? null : uuid); return; }
      if (fp.locked || fp.pinned) return;
      
      if (e.shiftKey) {
        // Range select not implemented yet - just toggle
        toggleSelect(uuid).then(() => { renderFootprintTable(); zoomToSelection(); });
      } else if (e.ctrlKey || e.metaKey) {
        toggleSelect(uuid).then(() => { renderFootprintTable(); zoomToSelection(); });
      } else {
        selectedUuids.clear();
        selectedUuids.add(uuid);
        fetch("/api/placement/select", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ uuid })
        });
        updateFootprintVisuals();
        updateSelectionUI();
        renderFootprintTable();
        zoomToSelection();
      }
    });
  });
  
  // Update select all checkbox
  syncSelectAll("fp-select-all", filtered, fp => fp.selected);
  
  // Keep ghost target options + selected-ghost panel in sync with the cache.
  refreshGhostTargets();
  updateGhostSelUI();
}

// Net selection lives in the nets table + SVG copper click (selectNetFromCopper).
// The top-20 net legend was removed: it duplicated the table and coupled
// placement + routing selection in one click.

// Load pinned on board load
async function loadSelectionState() {
  illegalUuids.clear();
  lastLegality = {};
  await fetchPinned();
  await fetchSelected();
  await fetchFootprints();
  updateFootprintVisuals();
  updateSelectionUI();
}

// Toolbar buttons
on("select-all-btn", "click", selectAllUnlocked);
on("clear-sel-btn", "click", clearSelection);
on("pin-btn", "click", () => {
  // Pin all currently selected
  selectedUuids.forEach(uuid => togglePin(uuid));
});

// Sidebar filter event listeners
["fp-search", "filter-ref", "filter-net", "filter-layer", "filter-state", "filter-hide-locked", "filter-hide-fixed"].forEach(id => {
  on(id, "input", renderFootprintTable);
});

// Sortable footprint table headers
document.querySelectorAll("#fp-table th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (fpSortKey === key) {
      fpSortDir = fpSortDir === 1 ? -1 : 1;
    } else {
      fpSortKey = key;
      fpSortDir = 1;
    }
    renderFootprintTable();
  });
});

// Sidebar buttons
on("sidebar-select-visible", "click", () => {
  const filtered = getFilteredFootprints();
  filtered.forEach(fp => {
    if (!fp.locked && !fp.pinned && !fp.ghost) {
      selectedUuids.add(fp.uuid);
    }
  });
  // Update server for each
  filtered.forEach(fp => {
    if (!fp.locked && !fp.pinned && !fp.ghost) {
      fetch("/api/placement/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid: fp.uuid })
      });
    }
  });
  updateFootprintVisuals();
  updateSelectionUI();
  renderFootprintTable();
});

on("sidebar-clear-sel", "click", clearSelection);

on("sidebar-pin-sel", "click", () => {
  selectedUuids.forEach(uuid => togglePin(uuid));
});

on("sidebar-unpin-sel", "click", () => {
  // Unpin all currently pinned that are also selected (or all pinned if none selected)
  const toUnpin = selectedUuids.size > 0 
    ? Array.from(selectedUuids).filter(u => pinnedUuids.has(u))
    : Array.from(pinnedUuids);
  toUnpin.forEach(uuid => togglePin(uuid));
});

// Select all checkbox in table header
on("fp-select-all", "change", e => {
  const filtered = getFilteredFootprints();
  if (e.target.checked) {
    filtered.forEach(fp => {
      if (!fp.locked && !fp.pinned && !fp.ghost) {
        selectedUuids.add(fp.uuid);
      }
    });
  } else {
    filtered.forEach(fp => selectedUuids.delete(fp.uuid));
  }
  // Update server
  filtered.forEach(fp => {
    if (!fp.locked && !fp.pinned && !fp.ghost) {
      fetch("/api/placement/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid: fp.uuid, selected: e.target.checked })
      });
    }
  });
  updateFootprintVisuals();
  updateSelectionUI();
  renderFootprintTable();
});

on("refresh-forces-btn", "click", refreshForces);

// ==================== Manual nudge (selected parts, 1mm) ====================
// Shifts the placement selection via POST /api/placement/nudge (same
// dual-write + rip-up + undo semantics as Accept). Ghosts are never in
// selectedUuids (see selectGhost), so they can't be nudged from here.

async function nudgeSelected(dx_mm, dy_mm, da_deg = 0) {
  if (!selectedUuids.size || !lastVersion) return;
  if (motion.frames.length) pauseMotion();
  try {
    const r = await apiJson("/api/placement/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dx_mm, dy_mm, da_deg }),
    });
    const what = da_deg ? `rotate ${da_deg}°` : `move (${dx_mm}, ${dy_mm})`;
    if (diagEl) diagEl.textContent = `nudged ${(r.moved || []).length}: ${what} → v${r.version} (Undo restores)`;
    if (undoBtn) undoBtn.disabled = false;
    resetMotionState(); // server cleared the proposal; cached deltas are stale
    await poll();
  } catch (err) {
    if (diagEl) diagEl.textContent = `nudge failed: ${err.message}`;
  }
}

for (const [id, dx, dy] of [["nudge-up", 0, -1], ["nudge-down", 0, 1], ["nudge-left", -1, 0], ["nudge-right", 1, 0]]) {
  on(id, "click", e => {
    const step = e.shiftKey ? 10 : 1;
    nudgeSelected(dx * step, dy * step);
  });
}
for (const [id, da] of [["nudge-ccw", 15], ["nudge-cw", -15]]) {
  on(id, "click", e => {
    nudgeSelected(0, 0, e.shiftKey ? Math.sign(da) * 90 : da);
  });
}

// ==================== Ghost management ====================
// Ghost selection is client-side only (selectedGhostUuid): ghosts are fixed
// anchors, never part of the placement selection that Step/Run/Accept use.

function selectGhost(uuid) {
  if (selectedGhostUuid === uuid) return;
  selectedGhostUuid = uuid;
  updateFootprintVisuals();
  renderFootprintTable();
  updateGhostSelUI();
}

function updateGhostSelUI() {
  const panel = el("ghost-sel");
  const refEl = el("ghost-sel-ref");
  const sizeEl = el("ghost-size-label");
  const fp = allFootprints.find(f => f.uuid === selectedGhostUuid);
  if (!fp) {
    if (panel) panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const attrs = (fp.ghost_attractions || []).map(([t, ka]) => `${t},${ka}`).join("; ");
  refEl.textContent = `${fp.ref}${attrs ? ` → ${attrs}` : ""}`;
  refEl.title = fp.uuid;
  if (sizeEl && fp.bbox && fp.bbox.length === 4) {
    const w = fp.bbox[2] - fp.bbox[0];
    const h = fp.bbox[3] - fp.bbox[1];
    sizeEl.textContent = `${w.toFixed(1)}×${h.toFixed(1)}mm`;
    sizeEl.title = fp.angle_deg
      ? `Board-space extents (ghost rotated ${fp.angle_deg}°; resize writes local W/H)`
      : "Courtyard size (board mm)";
  }
}

function refreshGhostTargets() {
  const sel = el("ghost-target");
  if (!sel) return;
  const prev = sel.value;
  const refs = allFootprints.filter(fp => !fp.ghost && fp.ref).map(fp => fp.ref).sort();
  sel.innerHTML = refs.map(r => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join("");
  if (refs.includes(prev)) sel.value = prev;
}

function ghostViewCenter() {
  if (viewBox) return [viewBox[0] + viewBox[2] / 2, viewBox[1] + viewBox[3] / 2];
  return [50, 50];
}

function suggestGhostRef(kind) {
  const existing = new Set(allFootprints.map(fp => fp.ref));
  const prefix = kind === "attractor" ? "GHOST_ATTR_" : "GHOST_WALL_";
  for (let i = 1; i < 1000; i++) {
    const ref = `${prefix}${i}`;
    if (!existing.has(ref)) return ref;
  }
  return `${prefix}${Date.now() % 100000}`;
}

async function createGhost() {
  if (!lastVersion) return;
  const diag = ghostDiag;
  const kind = el("ghost-kind")?.value ?? "wall";
  const refInput = (el("ghost-ref")?.value ?? "").trim();
  const ref = refInput || suggestGhostRef(kind);
  const [cx, cy] = ghostViewCenter();
  const xRaw = el("ghost-x")?.value;
  const yRaw = el("ghost-y")?.value;
  const body = {
    kind, ref,
    x_mm: xRaw === "" ? Math.round(cx * 2) / 2 : Number(xRaw),
    y_mm: yRaw === "" ? Math.round(cy * 2) / 2 : Number(yRaw),
    courtyard_layer: "F.CrtYd",
  };
  if (kind === "wall") {
    body.w_mm = Number(el("ghost-w")?.value);
    body.h_mm = Number(el("ghost-h")?.value);
  } else {
    body.target_ref = el("ghost-target")?.value;
    body.ka = Number(el("ghost-ka")?.value);
    body.size_mm = Number(el("ghost-size")?.value);
  }
  try {
    const r = await apiJson("/api/ghosts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    diag.textContent = `ghost ${ref} added (v${r.version})`;
    const _gr = el("ghost-ref"); if (_gr) _gr.value = "";
    if (undoBtn) undoBtn.disabled = false;
    if (motion.frames.length) pauseMotion();
    resetMotionState();
    await poll();
    selectGhost(r.uuid);
  } catch (err) {
    diag.textContent = `add failed: ${err.message}`;
  }
}

async function moveGhost(dx_mm, dy_mm, da_deg = 0) {
  if (!selectedGhostUuid || !lastVersion) return;
  const diag = ghostDiag;
  try {
    const r = await apiJson("/api/ghosts/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuid: selectedGhostUuid, dx_mm, dy_mm, da_deg }),
    });
    diag.textContent = da_deg
      ? `ghost rotated ${da_deg}° → v${r.version}`
      : `ghost moved (${dx_mm}, ${dy_mm}) → v${r.version}`;
    if (undoBtn) undoBtn.disabled = false;
    if (motion.frames.length) pauseMotion();
    resetMotionState();
    await poll();
  } catch (err) {
    diag.textContent = `move failed: ${err.message}`;
  }
}

async function resizeGhost(dw_mm, dh_mm) {
  if (!selectedGhostUuid || !lastVersion) return;
  const diag = ghostDiag;
  const fp = allFootprints.find(f => f.uuid === selectedGhostUuid);
  if (!fp || !fp.bbox || fp.bbox.length !== 4) {
    diag.textContent = "resize failed: ghost extents unknown";
    return;
  }
  const w_mm = Math.max(0.5, (fp.bbox[2] - fp.bbox[0]) + dw_mm);
  const h_mm = Math.max(0.5, (fp.bbox[3] - fp.bbox[1]) + dh_mm);
  try {
    const r = await apiJson("/api/ghosts/resize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuid: selectedGhostUuid, w_mm, h_mm }),
    });
    diag.textContent = `ghost resized ${w_mm.toFixed(1)}×${h_mm.toFixed(1)} → v${r.version}`;
    if (undoBtn) undoBtn.disabled = false;
    if (motion.frames.length) pauseMotion();
    resetMotionState();
    await poll();
  } catch (err) {
    diag.textContent = `resize failed: ${err.message}`;
  }
}

async function deleteGhost() {
  if (!selectedGhostUuid || !lastVersion) return;
  const diag = ghostDiag;
  try {
    const r = await apiJson("/api/ghosts/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uuid: selectedGhostUuid }),
    });
    diag.textContent = `ghost deleted (v${r.version})`;
    selectedGhostUuid = null;
    if (undoBtn) undoBtn.disabled = false;
    if (motion.frames.length) pauseMotion();
    resetMotionState();
    await poll();
  } catch (err) {
    diag.textContent = `delete failed: ${err.message}`;
  }
}

on("ghost-create-btn", "click", createGhost);
on("ghost-delete-btn", "click", deleteGhost);
for (const [id, dx, dy] of [["ghost-up", 0, -1], ["ghost-down", 0, 1], ["ghost-left", -1, 0], ["ghost-right", 1, 0]]) {
  on(id, "click", e => {
    const step = e.shiftKey ? 10 : 1;
    moveGhost(dx * step, dy * step);
  });
}
for (const [id, da] of [["ghost-ccw", 15], ["ghost-cw", -15]]) {
  on(id, "click", e => {
    moveGhost(0, 0, e.shiftKey ? Math.sign(da) * 90 : da);
  });
}
for (const [id, dw, dh] of [["ghost-w-minus", -1, 0], ["ghost-w-plus", 1, 0], ["ghost-h-minus", 0, -1], ["ghost-h-plus", 0, 1]]) {
  on(id, "click", e => {
    const step = e.shiftKey ? 5 : 1;
    resizeGhost(dw * step, dh * step);
  });
}
on("ghost-kind", "change", e => {
  const isWall = e.target.value === "wall";
  const _gwr = el("ghost-wall-row"); if (_gwr) _gwr.hidden = !isWall;
  const _gar = el("ghost-attr-row"); if (_gar) _gar.hidden = isWall;
  if (!isWall) refreshGhostTargets();
});

// Common function to run placement.
// iterations == null → omit the key so the server runs to convergence
// (saved max_iterations); otherwise a small step budget is used.
//
// UX STAGE 1 — estimate-first: the POST is synchronous, so we show a
// pre-flight estimate + an elapsed ticker + a client-side timeout backstop.
// Full-board ms/iter ≈ 0.0045*N^2 + 1 (N = movable non-ghost footprints,
// from docs/benchmarks/placement_2026-10-26.json), scaled by the moved
// fraction. Order-of-magnitude only — real boards deviate with courtyard
// complexity — and worst-case (runs often converge early), so the line
// reads "up to".
function estimatePlacementMs(movableUuids, iters) {
  const isMovable = (fp) => !fp.locked && !fp.pinned && !fp.ghost &&
    (movableUuids === undefined || movableUuids.includes(fp.uuid));
  const movable = allFootprints.filter(isMovable);
  const pads = movable.reduce((s, fp) => s + (fp.pad_count ?? 0), 0);
  const nTotal = allFootprints.filter((fp) => !fp.locked && !fp.ghost).length || movable.length;
  const m = movable.length;
  const fullBoardMsPerIter = 0.0045 * nTotal * nTotal + 1;
  const frac = nTotal > 0 ? m / nTotal : 1;
  const msPerIter = fullBoardMsPerIter * (0.15 + 0.85 * frac);
  return { ms: Math.max(msPerIter, 0.05) * iters, msPerIter, fps: m, pads };
}

function fmtDuration(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `≈${s}s`;
  return `≈${Math.floor(s / 60)}min${s % 60 ? ` ${s % 60}s` : ""}`;
}

// ---- Motion player: scrub + slow playback of downsampled trajectory frames ----
// Frames are (dx, dy, da) deltas vs the committed board positions the SVG is
// drawn at, so each part animates via an in-place group transform — no SVG
// re-fetch per frame. Playback is deliberately slow (max 4 fps): frames are
// stride-sampled iterations, not wall-clock video, and fast playback hides
// the direction reversals this player exists to debug.
const motion = {
  frames: [],          // [{uuid: [dx, dy, da]}]
  force_frames: [],    // [{uuid: {cause: [fx, fy]}}] — per-cause forces at each frame
  stride: 1,
  iterations: 0,
  idx: 0,
  basePos: {},         // {uuid: [x_mm, y_mm]} — rotation pivots, captured per load
  forceAnchors: {},    // {uuid: [cx_board, cy_board]} — courtyard anchors (absolute)
  playing: false,
  timer: null,
};

function motionEls() {
  return {
    slider: el("motion-slider"),
    readout: el("motion-readout"),
    playBtn: el("motion-play-btn"),
    first: el("motion-first-btn"),
    prev: el("motion-prev-btn"),
    next: el("motion-next-btn"),
    last: el("motion-last-btn"),
    speed: el("motion-speed"),
    trails: el("motion-trails"),
    reset: el("motion-reset-btn"),
  };
}

function motionSetEnabled(on) {
  const { slider, playBtn, first, prev, next, last } = motionEls();
  const canPlay = on && motion.frames.length > 1;
  if (slider) slider.disabled = !on;
  if (playBtn) playBtn.disabled = !canPlay;
  for (const b of [first, prev, next, last]) if (b) b.disabled = !on;
}

function motionReadout() {
  const { readout, slider } = motionEls();
  const n = motion.frames.length;
  if (!readout || !slider) return;
  if (!n) { readout.textContent = "no frames"; return; }
  // frames[0] is the starting pose (iter 0); frames[k] follows k*stride
  // completed iterations; the last frame is always the final pose.
  const approxIter = (motion.idx >= n - 1)
    ? motion.iterations
    : Math.min(motion.iterations, motion.idx * motion.stride);
  const ds = motion.stride > 1 ? ` · stride ${motion.stride} (thinned)` : "";
  readout.textContent = `frame ${motion.idx + 1}/${n} · ~iter ${approxIter}/${motion.iterations}${ds}`;
  slider.value = String(motion.idx);
}

function motionFpNode(uuid) {
  return viewport?.querySelector(`g.footprint[data-fp-uuid="${uuid}"]`) ?? null;
}

// Force cause order (paint lives in CSS: .motion-force-vectors line.force-<cause>).
// Keep server svg_render.FORCE_CAUSE_COLORS and the #forces-legend chips in sync.
const FORCE_CAUSE_ORDER = ["repulsion", "attraction", "courtyard", "ghost", "tangential"];

function drawForceVectors(svg, frameIdx) {
  // Remove existing motion force vectors from footprint groups
  svg.querySelectorAll("g.footprint .motion-force-vectors").forEach(el => el.remove());
  
  // Use the sidebar forces toggle
  const forcesToggle = el("forces-toggle");
  if (!forcesToggle?.checked) return;
  if (!motion.force_frames.length) return;
  const fframe = motion.force_frames[frameIdx];
  if (!fframe) return;

  const NS = "http://www.w3.org/2000/svg";

  for (const [uuid, comps] of Object.entries(fframe)) {
    const fpGroup = motionFpNode(uuid);
    if (!fpGroup) continue;
    if (!comps) continue;

    // Find strongest cause for this footprint to scale others
    let maxMag = 0;
    for (const [cause, vec] of Object.entries(comps)) {
      const mag = Math.hypot(vec[0], vec[1]);
      if (mag > maxMag) maxMag = mag;
    }
    if (maxMag <= 0) continue;

    // Create a group for this footprint's force vectors (inside the footprint group)
    const forcesGroup = document.createElementNS(NS, "g");
    forcesGroup.setAttribute("class", "motion-force-vectors");
    forcesGroup.setAttribute("pointer-events", "none");

    for (const cause of FORCE_CAUSE_ORDER) {
      const vec = comps[cause];
      if (!vec) continue;
      const [fx, fy] = vec;
      const mag = Math.hypot(fx, fy);
      if (mag < 1e-6) continue;
      // Hide negligible causes (< 1% of strongest for this footprint),
      // matching the server _forces_overlay threshold to cut clutter.
      if (mag < 0.01 * maxMag) continue;

      // Log scale normalized per-footprint, capped at 3mm to prevent off-board
      // "sticks" inside transformed footprint groups (server static overlay
      // uses 5mm caps at absolute coords; motion intentionally uses 3mm).
      const length = Math.min(Math.max(5.0 * Math.log1p(mag) / Math.log1p(maxMag), 0.5), 3.0);
      const ux = fx / mag, uy = fy / mag;

      const line = document.createElementNS(NS, "line");
      // Absolute board coords at the courtyard anchor. Footprint children are
      // absolute, so the group translate+rotate carries vectors with the part.
      const anchor = motion.forceAnchors?.[uuid] || motion.basePos?.[uuid] || [0.0, 0.0];
      line.setAttribute("x1", anchor[0].toFixed(3));
      line.setAttribute("y1", anchor[1].toFixed(3));
      line.setAttribute("x2", (anchor[0] + ux * length).toFixed(3));
      line.setAttribute("y2", (anchor[1] + uy * length).toFixed(3));
      // Paint comes from CSS (.motion-force-vectors line.force-<cause>).
      line.setAttribute("class", `force-${cause}`);
      line.setAttribute("marker-end", `url(#force-${cause})`);
      forcesGroup.appendChild(line);
    }
    fpGroup.appendChild(forcesGroup);
  }
}

function applyMotionFrame(i) {
  const svg = viewport?.querySelector("svg");
  if (!svg || !motion.frames.length) return;
  // Motion owns forces: drop any server-rendered static layer so only the
  // per-footprint client vectors below remain (prevents dual-layer flicker).
  svg.querySelectorAll('g[class^="forces-"]').forEach(el => el.remove());
  motion.idx = Math.max(0, Math.min(motion.frames.length - 1, i));
  const frame = motion.frames[motion.idx];
  const seen = new Set();
  for (const [uuid, d] of Object.entries(frame)) {
    seen.add(uuid);
    const g = motionFpNode(uuid);
    if (!g) continue;
    const base = motion.basePos[uuid];
    if (!base) continue;
    const [dx, dy, da] = d;
    if (!dx && !dy && !da) { g.removeAttribute("transform"); continue; }
    // Board geometry rotates R(-da) while SVG rotate() is R(+a): negate.
    // Rotate about the committed center, then translate: p' = R(p-c)+c+d.
    g.setAttribute("transform", `translate(${dx} ${dy}) rotate(${-da} ${base[0]} ${base[1]})`);
  }
  // Clear stale transforms for parts absent from this frame (sparse frames,
  // or a newer run whose frame set omits a previously-moved part). Runs on
  // every frame, not just idx 0 — scrubbing back from a dense frame would
  // otherwise leave later transforms behind.
  (viewport?.querySelectorAll("g.footprint[transform]") ?? []).forEach(g => {
    const uuid = g.getAttribute("data-fp-uuid");
    if (uuid && !seen.has(uuid)) g.removeAttribute("transform");
  });
  drawMotionTrails();
  drawForceVectors(svg, motion.idx);
  motionReadout();
}

function drawMotionTrails() {
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  svg.querySelector("#motion-trails")?.remove();
  if (!motionEls().trails?.checked || !motion.frames.length || motion.idx < 1) return;
  const NS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(NS, "g");
  g.setAttribute("id", "motion-trails");
  g.setAttribute("pointer-events", "none");
  for (const uuid of Object.keys(motion.frames[motion.idx])) {
    const base = motion.basePos[uuid];
    if (!base) continue;
    const pts = [];
    for (let k = 0; k <= motion.idx; k++) {
      const d = motion.frames[k][uuid];
      if (!d) continue;
      pts.push(`${(base[0] + d[0]).toFixed(3)},${(base[1] + d[1]).toFixed(3)}`);
    }
    if (pts.length < 2) continue;
    const pl = document.createElementNS(NS, "polyline");
    pl.setAttribute("points", pts.join(" "));
    pl.setAttribute("class", "motion-trail");
    g.appendChild(pl);
    const [lx, ly] = pts[pts.length - 1].split(",");
    const dot = document.createElementNS(NS, "circle");
    dot.setAttribute("cx", lx); dot.setAttribute("cy", ly);
    dot.setAttribute("r", "0.25");
    dot.setAttribute("class", "motion-trail-dot");
    g.appendChild(dot);
  }
  // Trails below footprints/vectors: appended top-level groups paint on top,
  // which would cover per-footprint force arrowheads on overlap. Footprint
  // groups (and their motion-force-vectors children) must paint last.
  const anchor = svg.querySelector("g.footprint");
  if (anchor) svg.insertBefore(g, anchor);
  else svg.appendChild(g);
}

function clearMotionView() {
  if (!viewport) return;
  viewport.querySelectorAll("g.footprint[transform]").forEach(g => g.removeAttribute("transform"));
  viewport.querySelectorAll("g.footprint .motion-force-vectors").forEach(n => n.remove());
  viewport.querySelector("#motion-trails")?.remove();
}

function resetMotionState() {
  pauseMotion();
  clearMotionView();
  motion.frames = []; motion.force_frames = []; motion.stride = 1; motion.iterations = 0;
  motion.idx = 0; motion.basePos = {}; motion.forceAnchors = {};
  const { slider } = motionEls();
  slider.max = "0"; slider.value = "0";
  motionReadout();
  motionSetEnabled(false);
}

function pauseMotion() {
  if (motion.timer) { clearInterval(motion.timer); motion.timer = null; }
  motion.playing = false;
  motionEls().playBtn.textContent = "▶";
  motionEls().playBtn.title = "Play motion slowly";
}

function stepMotion(delta) {
  if (!motion.frames.length) return;
  pauseMotion();
  applyMotionFrame(motion.idx + delta);
}

function toggleMotionPlay() {
  if (!motion.frames.length) return;
  if (motion.playing) { pauseMotion(); return; }
  if (motion.idx >= motion.frames.length - 1) applyMotionFrame(0);
  motion.playing = true;
  const btn = motionEls().playBtn;
  btn.textContent = "⏸";
  btn.title = "Pause";
  // Slow on purpose: at most 4 fps so single-frame jumps stay visible.
  const fps = Math.min(4, Math.max(1, parseInt(motionEls().speed.value, 10) || 2));
  motion.timer = setInterval(() => {
    if (motion.idx >= motion.frames.length - 1) { pauseMotion(); return; }
    applyMotionFrame(motion.idx + 1);
  }, 1000 / fps);
}

async function loadMotionFrames(silent) {
  try {
    const res = await fetch("/api/placement/frames");
    if (!res.ok) {
      if (!silent) diagEl.textContent = "no motion frames — Step to record a trajectory";
      resetMotionState();
      return;
    }
    const data = await res.json();
    if (!data.frames || !data.frames.length) { resetMotionState(); return; }
    // Rotation pivots: committed footprint centers.
    // Force anchors: courtyard centroid in ABSOLUTE board coords (same space
    // as footprint children, so the group translate+rotate carries vectors).
    const fpRes = await fetch("/api/footprints");
    const basePos = {};
    const forceAnchors = {}; // {uuid: [cx_board, cy_board]}
    if (fpRes.ok) {
      const fpData = await fpRes.json();
      for (const fp of fpData.footprints || []) {
        if (fp.uuid != null && fp.x_mm != null) {
          basePos[fp.uuid] = [fp.x_mm, fp.y_mm];
          // Courtyard centroid from bbox [minx, miny, maxx, maxy].
          if (fp.bbox && fp.bbox.length === 4) {
            const cx = (fp.bbox[0] + fp.bbox[2]) / 2;
            const cy = (fp.bbox[1] + fp.bbox[3]) / 2;
            // Guard degenerate bboxes (e.g. [0,0,0,0]): fall back to origin.
            forceAnchors[fp.uuid] = (Number.isFinite(cx) && Number.isFinite(cy) && (cx || cy))
              ? [cx, cy] : [fp.x_mm, fp.y_mm];
          } else {
            // Fallback: footprint origin
            forceAnchors[fp.uuid] = [fp.x_mm, fp.y_mm];
          }
        }
      }
    }
    pauseMotion();
    motion.frames = data.frames;
    motion.force_frames = data.force_frames || [];
    motion.stride = data.frame_stride || 1;
    motion.iterations = data.iterations || data.frames.length;
    motion.basePos = basePos;
    motion.forceAnchors = forceAnchors;
    motion.idx = data.frames.length - 1; // rest on the final (proposal) pose
    // Re-inject the base SVG before applying: the live SVG may still show the
    // previous proposal (overlay=1 sits at apply_deltas poses), which would
    // stack under the new transforms. fetchAndInjectSVG forces overlay=0
    // while frames are loaded and re-applies the rest frame on inject.
    if (typeof lastVersion !== "undefined" && lastVersion) {
      await fetchAndInjectSVG(lastVersion);
    } else {
      clearMotionView();
    }
    const { slider } = motionEls();
    slider.max = String(data.frames.length - 1);
    motionSetEnabled(true);
    applyMotionFrame(motion.idx);
  } catch (err) {
    if (!silent) diagEl.textContent = `motion load failed: ${err.message}`;
    resetMotionState();
  }
}

on("motion-play-btn", "click", toggleMotionPlay);
on("motion-prev-btn", "click", () => stepMotion(-1));
on("motion-next-btn", "click", () => stepMotion(1));
on("motion-first-btn", "click", () => { pauseMotion(); applyMotionFrame(0); });
on("motion-last-btn", "click", () => { pauseMotion(); applyMotionFrame(motion.frames.length - 1); });
on("motion-reset-btn", "click", () => {
  pauseMotion();
  clearMotionView();
  if (motion.frames.length) applyMotionFrame(motion.frames.length - 1);
});
on("motion-slider", "input", (e) => {
  pauseMotion();
  // Debounce rapid scrub events via rAF: only the latest slider value paints
  // per frame, preventing queued applyMotionFrame calls that flash stale
  // vector layers.
  const v = parseInt(e.target.value, 10) || 0;
  if (motion._scrubQueued) { motion._scrubValue = v; return; }
  motion._scrubQueued = true;
  motion._scrubValue = v;
  requestAnimationFrame(() => {
    motion._scrubQueued = false;
    const slider = el("motion-slider");
    if (slider) slider.value = String(motion._scrubValue);
    applyMotionFrame(motion._scrubValue);
  });
});
on("motion-trails", "change", drawMotionTrails);
motionSetEnabled(false);

// Pure string builder for the placement result line (button state and
// table refresh stay with the caller).
function formatPlacementDiag(r) {
  if (!(r.moved > 0)) {
    return `v${r.version} — no movement (nothing movable, no nets, or already converged)`;
  }
  let diag = `v${r.version} — ${r.moved} moved, max Δ ${r.final_max_disp_mm.toFixed(2)} mm, ${r.iterations} iters, ${r.elapsed_s.toPrecision(2)} s`;
  if (r.stop_reason === "stalled" && !r.legality_is_legal) {
    const nOverlap = r.overlapping_pairs ? r.overlapping_pairs.length : 0;
    const nWall = r.wall_violations ? r.wall_violations.length : 0;
    if (nOverlap || nWall) diag += ` — ${nOverlap} overlap${nOverlap !== 1 ? "s" : ""}, ${nWall} wall violation${nWall !== 1 ? "s" : ""}`;
  }
  return diag;
}

async function runPlacement(movableUuids, iterations) {
  const stepBtn = stepSelectedBtn;
  const runBtn = el("run-selected-btn");
  stepBtn.classList.add("loading");
  runBtn.classList.add("loading");
  stepBtn.disabled = true;
  runBtn.disabled = true;
  const params = readParams();
  const budget = iterations ?? params.max_iterations ?? 1000;
  const est = estimatePlacementMs(movableUuids, budget);
  // Timeout scales with the estimate (worst case can legitimately take
  // minutes on 100-footprint boards), clamped to [60s, 10min]. Aborting is
  // client-side only — the server thread runs to completion regardless.
  const timeoutMs = Math.min(600000, Math.max(60000, est.ms * 4));
  const t0 = performance.now();
  const estKnown = allFootprints.length > 0;
  const ticker = setInterval(() => {
    const el = Math.round((performance.now() - t0) / 1000);
    if (diagEl) diagEl.textContent = estKnown
      ? `placing… ${el}s elapsed (est. up to ${fmtDuration(est.ms)})`
      : `placing… ${el}s elapsed`;
  }, 1000);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    if (diagEl) diagEl.textContent = estKnown
      ? `placing… (est. up to ${fmtDuration(est.ms)} at ${est.msPerIter.toFixed(1)}ms/iter, ` +
        `${est.fps} fps / ${est.pads} pads, budget ${budget} iters)`
      : `placing… (budget ${budget} iters)`;
    await apiFetch("/api/placement/params", { method: "POST", body: JSON.stringify(params) });

    const body = { movable_uuids: movableUuids, record_frames: true, max_frames: 200 };
    if (iterations != null) body.iterations = iterations;
    const r = await apiJson("/api/placement/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    
    // Capture legality data for parts table
    illegalUuids.clear();
    lastLegality = r;
    if (r.overlapping_pairs) {
      for (const [ua, ub] of r.overlapping_pairs) {
        illegalUuids.add(ua);
        illegalUuids.add(ub);
      }
    }
    if (r.wall_violations) {
      for (const [uuid] of r.wall_violations) {
        illegalUuids.add(uuid);
      }
    }
    await loadMotionFrames(true);
    if (diagEl) diagEl.textContent = formatPlacementDiag(r);
    if (acceptBtn) acceptBtn.disabled = !(r.moved > 0);
    if (stepSelectedBtn) stepSelectedBtn.textContent = "Step Again";
    renderFootprintTable(); // Re-render to show illegal state
  } catch (err) {
    const msg = err?.name === "AbortError"
      ? `run timed out after ${fmtDuration(timeoutMs)} — try fewer footprints or a smaller iteration budget`
      : `run failed: ${err.message}`;
    toast(msg);
  } finally {
    clearInterval(ticker);
    clearTimeout(timer);
    stepBtn.classList.remove("loading");
    runBtn.classList.remove("loading");
    stepBtn.disabled = false;
    runBtn.disabled = false;
  }
}

// Step: a few iterations on the selection (empty selection targets all movable)
stepSelectedBtn?.addEventListener("click", async () => {
  const itersRaw = parseInt(el("step-iters")?.value, 10);
  const iters = Number.isFinite(itersRaw) ? itersRaw : 1;
  const movableUuids = selectedUuids.size > 0 ? Array.from(selectedUuids) : undefined;
  await runPlacement(movableUuids, iters);
});

// Run: to convergence (saved max_iterations) on the selection (empty targets all)
on("run-selected-btn", "click", async () => {
  const movableUuids = selectedUuids.size > 0 ? Array.from(selectedUuids) : undefined;
  await runPlacement(movableUuids, null);
});

acceptBtn?.addEventListener("click", async () => {
  try {
    const r = await apiJson("/api/placement/accept", { method: "POST" });
    const ripped = r.ripped || {};
    const rippedNames = Object.keys(ripped);
    const rippedTxt = rippedNames.length
      ? rippedNames.map(n => `${n}: ${ripped[n].tracks || 0} tracks/${ripped[n].vias || 0} vias`).join(", ")
      : "no copper ripped";
    const skipped = (r.skipped_protected || []).join(", ");
    const moveTxt = r.moved != null ? `${r.moved} moved` : "";
    const iterTxt = r.iterations != null ? `${r.iterations} iters, disp ${r.final_max_disp_mm}mm` : "";
    if (diagEl) diagEl.textContent = `accepted v${r.version} (${moveTxt}; ${iterTxt}) — ripped ${rippedTxt}`
      + (skipped ? `; kept protected ${skipped}` : "")
      + `. Re-route via Route panel if needed.`;
    if (acceptBtn) acceptBtn.disabled = true;
    if (stepSelectedBtn) stepSelectedBtn.textContent = "Step";
    if (undoBtn) undoBtn.disabled = false;
    resetMotionState(); // positions committed — cached deltas are stale
  } catch (err) {
    if (diagEl) diagEl.textContent = `accept failed: ${err.message}`;
  }
});

on("clear-btn", "click", async () => {
  await fetch("/api/placement/clear", { method: "POST" });
  illegalUuids.clear();
  lastLegality = {};
  if (diagEl) diagEl.textContent = "proposal cleared";
  if (acceptBtn) acceptBtn.disabled = true;
  if (stepSelectedBtn) stepSelectedBtn.textContent = "Step";
  resetMotionState();
  renderFootprintTable();
});

undoBtn?.addEventListener("click", async () => {
  try {
    const r = await apiJson("/api/placement/undo", { method: "POST" });
    illegalUuids.clear();
    lastLegality = {};
    if (diagEl) diagEl.textContent = `undone — board reverted to v${r.version}`;
    if (acceptBtn) acceptBtn.disabled = true;
    if (stepSelectedBtn) stepSelectedBtn.textContent = "Step";
    resetMotionState();
    renderFootprintTable();
    try {
      const mres = await fetch("/api/mode");
      if (mres.ok) { const m = await mres.json(); updateModeUI({ mode: m.mode, mode_warning: m.warning }); }
    } catch (err) { /* mode chip refresh is best-effort */ }
  } catch (err) {
    if (diagEl) diagEl.textContent = `undo failed: ${err.message}`;
  }
});

// ==================== Board Mode Switch ====================

let boardMode = null;  // "two-layer" | "four-layer", from /api/state

function updateModeUI(st) {
  boardMode = st.mode || null;
  const b2 = el("mode-2l");
  const b4 = el("mode-4l");
  if (b2) b2.classList.toggle("active", boardMode === "two-layer");
  if (b4) b4.classList.toggle("active", boardMode === "four-layer");
  const warn = el("mode-warn");
  if (warn) {
    warn.hidden = !st.mode_warning;
    warn.textContent = "⚠ mode";
    warn.title = st.mode_warning || "";
  }
}

async function setBoardMode(mode) {
  try {
    const r = await apiJson("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode })
    });
    boardMode = r.mode;
    updateModeUI({ mode: r.mode, mode_warning: r.warning });
    await loadRoutingParams();  // refresh mode-owned checkbox gating
    toast(`mode → ${r.mode} (routing defaults applied)`, true);
  } catch (err) {
    toast(`mode switch failed: ${err.message}`, false);
  }
}

on("mode-2l", "click", () => setBoardMode("two-layer"));
on("mode-4l", "click", () => setBoardMode("four-layer"));

on("download-btn", "click", async () => {
  try {
    // Two-layer mode with 4-layer content collapses on export: confirm
    // first (report counts + impedance note + "original untouched").
    if (boardMode === "two-layer") {
      const prev = await fetch("/api/board/collapse-preview");
      if (prev.ok) {
        const r = await prev.json();
        const rep = r.report || {};
        const changed = (rep.tracks_removed || 0) + (rep.vias_converted || 0)
          + (rep.zones_removed || 0) + (rep.zones_restricted || 0);
        if (changed > 0) {
          const ok = confirm(
            `Two-layer export collapses inner copper (original untouched):\n` +
            `− ${rep.tracks_removed} inner tracks removed\n` +
            `− ${rep.vias_converted} vias restacked to through (F.Cu–B.Cu)\n` +
            `− ${rep.zones_removed} zones deleted, ${rep.zones_restricted} restricted\n` +
            `${rep.impedance_note || ""}\n\nDownload the collapsed 2-layer file?`
          );
          if (!ok) return;
        }
      }
    }
    const res = await apiFetch("/api/board/download");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = res.headers.get("Content-Disposition")?.split('filename="')[1]?.split('"')[0] || "board-modified.kicad_pcb";
    a.click();
    URL.revokeObjectURL(url);
    if (diagEl) diagEl.textContent = "PCB downloaded";
  } catch (err) {
    if (diagEl) diagEl.textContent = `download failed: ${err.message}`;
  }
});

on("reset-btn", "click", async () => {
  try {
    const r = await apiJson("/api/board/reset", { method: "POST" });
    illegalUuids.clear();
    lastLegality = {};
    if (diagEl) diagEl.textContent = `board reset to original (v${r.version})`;
    if (acceptBtn) acceptBtn.disabled = true;
    const runBtn = el("run-selected-btn");
    if (runBtn) runBtn.textContent = "Step placement";
    if (undoBtn) undoBtn.disabled = true;
    renderFootprintTable();
    try {
      const mres = await fetch("/api/mode");
      if (mres.ok) { const m = await mres.json(); updateModeUI({ mode: m.mode, mode_warning: m.warning }); }
      await loadRoutingParams();
    } catch (err) { /* mode refresh is best-effort */ }
  } catch (err) {
    if (diagEl) diagEl.textContent = `reset failed: ${err.message}`;
  }
});

// The original reset-btn handler for params (renamed to avoid conflict)
on("reset-params-btn", "click", async () => {
  try {
    await loadParams(true);
    if (diagEl) diagEl.textContent = "params reset to defaults (persists on next run)";
  } catch (err) {
    if (diagEl) diagEl.textContent = `reset failed: ${err.message}`;
  }
});

// ==================== Routing Panel Logic ====================

// Load routing params from server
async function loadRoutingParams() {
  try {
    const data = await apiJson("/api/routing/params");
    routingParams = data.params || {};
    // Mode-owned keys render disabled/badged ("Managed by mode"); the
    // server forces the preset at job time regardless of input state.
    // The owned set comes from the server (/api/mode `owned`, single
    // source in strategy.mode_owned_keys).
    let owned = [];
    let preset = {};
    try {
      const mres = await fetch("/api/mode");
      if (mres.ok) {
        const minfo = await mres.json();
        preset = minfo.preset || {};
        owned = minfo.owned || Object.keys(preset);
      }
    } catch (err) { /* no board loaded — nothing is mode-owned */ }
    renderRoutingParams(routingParams, owned, preset);
  } catch (err) {
    toast(`routing params load failed: ${err.message}`);
    renderRoutingParams({});
  }
}

// Render routing params sliders
function renderRoutingParams(params, modeOwned = [], modePreset = {}) {
  const grid = el("routing-params-grid");
  if (!grid) return;
  
  const ROUTING_PARAM_DEFS = [
    { key: "track_width_mm", label: "Track Width (mm)", min: 0.1, max: 2.0, step: 0.05, default: 0.25 },
    { key: "power_width_mm", label: "Power Width (mm)", min: 0.2, max: 3.0, step: 0.05, default: 0.5 },
    { key: "clearance_mm", label: "Clearance (mm)", min: 0.1, max: 1.0, step: 0.05, default: 0.2 },
    { key: "fanout_margin_mm", label: "Push away from foreign pins (mm, steering only)", min: 0, max: 1.0, step: 0.05, default: 0 },
    { key: "via_cost_mm", label: "Via Cost (mm)", min: 1, max: 20, step: 0.5, default: 5 },
    { key: "pour_severance_weight", label: "Pour Severance Weight (0 disables)", min: 0, max: 200, step: 5, default: 30 },
    { key: "grid_resolution_mm", label: "Grid Resolution (mm) — 0.05 is 4x cells, may be rejected on large boards", min: 0.05, max: 0.5, step: 0.05, default: 0.1 },
    { key: "max_terminals_per_net", label: "Max terminals per net (0 = no cap; larger nets fail fast)", min: 0, max: 128, step: 1, default: 32 },
  ];
  
  // Option flags share one full-width row below the sliders (not one
  // grid cell per checkbox) to save vertical space.
  const debugChecked = params.debug ? "checked" : "";
  const stitchChecked = params.stitch_gnd_vias ?? true ? "checked" : "";
  const stripChecked = params.strip_gnd_blockers ?? true ? "checked" : "";
  const refillChecked = params.refill_zones ? "checked" : "";
  const stitchManaged = modeOwned.includes("stitch_gnd_vias");
  const stripManaged = modeOwned.includes("strip_gnd_blockers");
  const refillManaged = modeOwned.includes("refill_zones");
  const layersManaged = modeOwned.includes("allowed_layers");
  const layersVal = modePreset.allowed_layers;
  const layersText = layersManaged
    ? (Array.isArray(layersVal) ? layersVal.join(", ") : "all copper layers")
    : null;
  const debugHtml = `
    <div class="param param-flags" style="grid-column:1/-1;">
      <span>Options</span>
      <div class="row" style="gap:.9rem; flex-wrap:wrap;">
        <label class="chk" style="margin:0;" title="${stitchManaged ? "Managed by mode" : "Auto-repair GND pour continuity after routing (stitching vias + bridge tracks)"}">
          <input type="checkbox" data-key="stitch_gnd_vias" ${stitchChecked} ${stitchManaged ? "disabled" : ""}> GND stitching${stitchManaged ? " (mode)" : ""}
        </label>
        <label class="chk" style="margin:0;" title="${stripManaged ? "Managed by mode" : "Evict GND vias/tracks upfront before routing (rebuildable via stitching); signal/power copper is never evicted"}">
          <input type="checkbox" data-key="strip_gnd_blockers" ${stripChecked} ${stripManaged ? "disabled" : ""}> Strip GND blockers${stripManaged ? " (mode)" : ""}
        </label>
        <label class="chk" style="margin:0;" title="${refillManaged ? "Managed by mode" : "Refill copper pours after routing using kicad-cli (requires KiCad installed)"}">
          <input type="checkbox" data-key="refill_zones" ${refillChecked} ${refillManaged ? "disabled" : ""}> Refill zones${refillManaged ? " (mode)" : ""}
        </label>
        <label class="chk" style="margin:0;" title="Enable debug output (cost grid, search frontier)">
          <input type="checkbox" data-key="debug" ${debugChecked}> Debug
        </label>
      </div>
      ${layersManaged ? `<div class="row" style="margin-top:.4rem; font-size:.85em; opacity:.85;" title="Managed by mode — forced at job time">Layers: ${layersText} (mode)</div>` : ""}
    </div>
  `;
  
  grid.innerHTML = ROUTING_PARAM_DEFS.map(def => {
    const value = params[def.key] ?? def.default;
    const managed = modeOwned.includes(def.key);
    return `
      <div class="param">
        <span title="${managed ? "Managed by mode" : def.label}">${def.label}${managed ? " (mode)" : ""}</span>
        <div class="row">
          <input type="range" data-key="${def.key}" min="${def.min}" max="${def.max}" step="${def.step}" value="${value}" ${managed ? "disabled" : ""}>
          <input type="number" data-key="${def.key}" min="${def.min}" max="${def.max}" step="${def.step}" value="${value}" ${managed ? "disabled" : ""}>
        </div>
      </div>
    `;
  }).join("") + debugHtml;
  
  // Sync range and number inputs
  grid.querySelectorAll('input[type="range"]').forEach(range => {
    range.addEventListener("input", e => {
      const key = e.target.dataset.key;
      const num = grid.querySelector(`input[type="number"][data-key="${key}"]`);
      if (num) num.value = range.value;
    });
  });
  grid.querySelectorAll('input[type="number"]').forEach(num => {
    num.addEventListener("input", e => {
      const key = e.target.dataset.key;
      const range = grid.querySelector(`input[type="range"][data-key="${key}"]`);
      if (range) range.value = num.value;
    });
  });
}

// Read routing params from UI
function readRoutingParams() {
  const grid = el("routing-params-grid");
  if (!grid) return {};
  const params = {};
  grid.querySelectorAll('input[data-key]').forEach(input => {
    if (input.type === "checkbox") {
      params[input.dataset.key] = input.checked;
    } else {
      params[input.dataset.key] = input.type === "number" ? Number(input.value) : input.value;
    }
  });
  return params;
}

// Save routing params to server
async function saveRoutingParams() {
  const params = readRoutingParams();
  try {
    const res = await apiFetch("/api/routing/params", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params)
    });
    routingParams = params;
  } catch (err) {
    toast(`routing params save failed: ${err.message}`);
  }
}

// Route selected nets — ONE NET PER BACKGROUND JOB.
//
// Each net starts POST /api/routing/jobs and is polled (1s) for pair-level
// progress; Cancel posts .../cancel so the server actually stops searching
// (already-routed nets are kept — surfaced in the summary line). Per-net
// failures don't abort the batch, except HTTP 400 (bad params, e.g.
// oversize grid) which would fail every net identically. Per-net cap is
// 10min: on cap the job is cancelled server-side and the batch continues.
let routeCancelRequested = false;
let routeCurrentJobId = null;

// Pre-flight cost estimate: pairwise A* searches per net = k*(k-1)/2 over
// pad counts (upper bound on terminals), plus a wall-time fit. Shown before
// the run so a 66-pad GND never surprises: it fails fast via
// max_terminals_per_net instead of burning hours of Steiner searches.
// Time fit from docs/benchmarks/routing_2026-10-26.json: first net pays
// ~2-2.6s fixed (occupancy+grids+via_ok), then ~1s/net (DCCF
// 1/2/5/10 nets: 2.6/3.3/5.3/11.6s; tube111 1/2: 1.3/1.6s). GND stitching
// is a separate surcharge measured in docs/benchmarks/stitch_pour_2026-10-26.json.
function estimateRoutingWork(netNames) {
  let pairs = 0;
  let worst = { net: null, pairs: 0 };
  for (const name of netNames) {
    const row = lastNetsByName.get(name);
    const k = row?.pad_count ?? 2;
    const p = k * (k - 1) / 2;
    pairs += p;
    if (p > worst.pairs) worst = { net: name, pairs: p };
  }
  const seconds = 2 + 1.2 * netNames.length;
  return { pairs, worst, seconds };
}

function fmtExpansions(e) {
  e = Number(e) || 0;
  if (e >= 1e6) return `${(e / 1e6).toFixed(1)}M expansions`;
  if (e >= 1e3) return `${Math.round(e / 1e3)}k expansions`;
  return `${e} expansions`;
}

// Compact expansions for the live status line ("15.5M/4M").
function fmtExpShort(e) {
  e = Number(e) || 0;
  if (e >= 1e6) return `${(e / 1e6).toFixed(1)}M`;
  if (e >= 1e3) return `${Math.round(e / 1e3)}k`;
  return `${e}`;
}

// Must match RoutingParams.max_expansions_per_net default (pipeline.py);
// the Route panel has no knob for it, so the status meter falls back here.
const ROUTE_EXP_CAP_FALLBACK = 4_000_000;

// Per-net server cap: the job keeps searching for quality, but a single
// net never blocks the batch forever — on cap it is cancelled server-side
// (partial routes kept) and recorded as a failure.
const ROUTE_JOB_CAP_S = 600;
const ROUTE_JOB_POLL_MS = 1000;

async function startRoutingJob(net, params) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const res = await fetch("/api/routing/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ net_names: [net], params }),
    });
    if (res.status === 409 && attempt === 0) {
      // Another job still finishing — wait, then retry once.
      await new Promise(r => setTimeout(r, 5000));
      continue;
    }
    if (!res.ok) {
      const msg = await res.text();
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    const data = await res.json();
    if (!data.job_id) throw new Error("server did not return a job id");
    return data.job_id;
  }
  throw new Error("another routing job is running");
}

async function cancelRoutingJob(jobId) {
  try {
    await apiFetch(jobUrl(jobId, "/cancel"), { method: "POST" });
  } catch (err) {
    toast(`cancel failed: ${err.message}`);
  }
}

async function pollRoutingJob(jobId, net, idx, total, diag, progress, estNote, t0, expCap, estSec) {
  const elapsed = () => `${Math.round((performance.now() - t0) / 1000)}s`;
  const cap = Number(expCap) > 0 ? Number(expCap) : 0;
  const deadline = performance.now() + ROUTE_JOB_CAP_S * 1000;
  for (;;) {
    if (routeCancelRequested) {
      await cancelRoutingJob(jobId);
      // Fall through to one final poll below to collect the outcome.
      const res = await fetch(jobUrl(jobId));
      if (res.ok) {
        const job = await res.json();
        if (job.state === "done" && job.result) return { result: job.result, userCancelled: true };
      }
      return { result: null, userCancelled: true };
    }
    const job = await apiJson(jobUrl(jobId));
    const p = job.progress || {};
    const frac = (p.pairs_total > 0) ? (p.pairs_done / p.pairs_total) : 0;
    progress.value = (idx + Math.min(1, frac)) / total;
    const pairTxt = p.pairs_total > 0 ? `pair ${Math.min(p.pairs_total, p.pairs_done + 1)}/${p.pairs_total}` : "starting";
    // Live line stays compact: the static estimate lives in #route-est;
    // full detail is one hover away. Budget meter answers "not fail-fast?"
    // at a glance (cap burn, not hang).
    const overEst = Number(estSec) > 0 && (performance.now() - t0) / 1000 > Number(estSec) * 1.5;
    const burning = cap > 0 && (p.expansions || 0) > 0.8 * cap;
    const flags = (overEst ? " ⚠ over est" : "") + (burning ? " ⚠ budget" : "");
    diag.textContent = `net ${idx + 1}/${total} (${net}) · ${pairTxt} · ${fmtExpShort(p.expansions)}/${fmtExpShort(cap)} · ${elapsed()}${flags}`;
    diag.title = `${pairTxt} · ${fmtExpansions(p.expansions)} of ${fmtExpansions(cap)} budget · elapsed ${elapsed()} — ${estNote}`;
    if (job.state === "done") return { result: job.result, userCancelled: false };
    if (job.state === "failed") {
      const err = new Error(job.error || "routing job failed");
      err.status = 500;
      throw err;
    }
    if (performance.now() > deadline) {
      await cancelRoutingJob(jobId);
      // Collect the (partial) outcome after the cancel lands.
      for (let k = 0; k < 10; k++) {
        await new Promise(r => setTimeout(r, ROUTE_JOB_POLL_MS));
        const r2 = await fetch(jobUrl(jobId));
        if (!r2.ok) break;
        const j2 = await r2.json();
        if (j2.state === "done") return { result: j2.result, timedOut: true };
        if (j2.state === "failed") break;
      }
      return { result: null, timedOut: true };
    }
    await new Promise(r => setTimeout(r, ROUTE_JOB_POLL_MS));
  }
}

// Pure string builder for the route-batch summary line.
function formatTotalsDiag({ totals, failures, doneCount, netCount, lastBoardVersion, elapsedTxt, cancelled }) {
  const kept = `v${lastBoardVersion} — ${totals.tracks} tracks, ${totals.vias} vias, ` +
    `${totals.routed} routed, ${totals.failed} failed (${elapsedTxt} · ${fmtExpansions(totals.expansions)}` +
    (totals.astarS ? `, ${totals.astarS.toFixed(0)}s A*` : "") + `)` +
    (totals.stitchVias || totals.stitchTracks
      ? ` — GND stitch: ${totals.stitchVias || 0} vias, ${totals.stitchTracks || 0} bridges` +
        (totals.gndBefore ? ` (islands ${totals.gndBefore}→${totals.gndAfter})` : "")
      : "");
  if (cancelled) return `cancelled after ${doneCount}/${netCount} nets — kept ${totals.routed} — ${kept}`;
  if (failures.length) return `${kept} — failures: ${failures.join("; ")}`;
  return kept;
}

async function routeSelectedNets() {
  if (selectedNets.size === 0) return;
  const diag = routeDiag;
  const cancelBtn = el("route-cancel-btn");
  const progress = el("route-progress");
  if (!diag || !progress) {
    console.warn("missing route diag/progress nodes");
    return;
  }

  const nets = Array.from(selectedNets);
  await saveRoutingParams();
  const params = readRoutingParams();
  const est = estimateRoutingWork(nets);
  const expCap = Number(params.max_expansions_per_net) > 0
    ? Number(params.max_expansions_per_net) : ROUTE_EXP_CAP_FALLBACK;
  const stitchNote = params.stitch_gnd_vias
    ? " + GND stitch (bench: +3s tube111-scale, +17s DCCF-scale)" : "";
  const estNote = `~${est.pairs} pair-searches across ${nets.length} net${nets.length > 1 ? "s" : ""}` +
    ` (est. up to ≈${Math.round(est.seconds)}s${stitchNote})` +
    (est.worst.pairs > 100 ? ` — worst: ${est.worst.net} (~${est.worst.pairs}; expect minutes)` : "");
  // Static estimate, written once: the live line only carries numbers.
  const routeEst = el("route-est");
  if (routeEst) {
    routeEst.textContent = `est. ~${est.pairs} pair-searches · typical ≈${Math.round(est.seconds)}s${stitchNote} · budget ${fmtExpShort(expCap)}/net`;
    routeEst.title = estNote;
  }

  setRouteBusy(true);
  progress.hidden = false;
  progress.value = 0;
  if (cancelBtn) cancelBtn.hidden = false;
  routeCancelRequested = false;

  const totals = { tracks: 0, vias: 0, routed: 0, failed: 0, expansions: 0, astarS: 0 };
  const failures = [];
  let doneCount = 0;
  let lastBoardVersion = lastVersion;
  const t0 = performance.now();
  const elapsed = () => `${Math.round((performance.now() - t0) / 1000)}s`;

  for (let i = 0; i < nets.length; i++) {
    if (routeCancelRequested) break;
    const net = nets[i];
    diag.textContent = `net ${i + 1}/${nets.length} (${net}) — starting — ${elapsed()}`;
    diag.title = estNote;
    progress.value = i / nets.length;

    let jobId = null;
    try {
      jobId = await startRoutingJob(net, params);
      routeCurrentJobId = jobId;
      const { result: r, userCancelled, timedOut } = await pollRoutingJob(jobId, net, i, nets.length, diag, progress, estNote, t0, expCap, est.seconds);
      routeCurrentJobId = null;
      if (userCancelled) break;
      if (timedOut) {
        if (r) {
          totals.tracks += r.tracks_added || 0;
          totals.vias += r.vias_added || 0;
          totals.routed += r.nets_routed || 0;
          if (r.version) {
            lastBoardVersion = r.version;
            lastVersion = r.version;
            await fetchAndInjectSVG(r.version);
          }
        }
        failures.push(`${net}: hit ${ROUTE_JOB_CAP_S / 60}min cap (cancelled server-side, partial kept)`);
        totals.failed += 1;
        continue;
      }
      if (!r) throw new Error("empty job result");
      totals.tracks += r.tracks_added || 0;
      totals.vias += r.vias_added || 0;
      totals.routed += r.nets_routed || 0;
      totals.failed += r.nets_failed || 0;
      totals.expansions += r.expansions || 0;
      if (r.timings?.astar_s) totals.astarS += r.timings.astar_s;
      totals.stitchVias = (totals.stitchVias || 0) + (r.stitch_vias_added || 0);
      totals.stitchTracks = (totals.stitchTracks || 0) + (r.stitch_tracks_added || 0);
      totals.gndStripped = (totals.gndStripped || 0) + (r.gnd_stripped || 0);
      if (r.gnd_islands_before) {
        totals.gndBefore = r.gnd_islands_before;
        totals.gndAfter = r.gnd_islands_after;
      }
      if (r.debug) showCostGridDebug(r.debug);
      // Incremental refresh: show each net's copper as it lands.
      if (r.version) {
        lastBoardVersion = r.version;
        lastVersion = r.version;
        await fetchAndInjectSVG(r.version);
      }
    } catch (err) {
      routeCurrentJobId = null;
      if (err?.status === 400) {
        // Bad params (e.g. oversize grid): every remaining net would fail
        // the same way — abort the whole batch, keep what's routed.
        failures.push(`${net}: ${err.message}`);
        totals.failed += nets.length - i;
        doneCount = nets.length;
        diag.textContent = `aborted: ${err.message} — kept ${totals.routed}/${nets.length} nets`;
        break;
      }
      failures.push(`${net}: ${err.message}`);
      totals.failed += 1;
    } finally {
      doneCount = i + 1;
      progress.value = doneCount / nets.length;
    }
  }

  // Re-fetch nets to update track/via counts.
  await fetchNets();

  diag.textContent = formatTotalsDiag({
    totals, failures, doneCount, netCount: nets.length, lastBoardVersion,
    elapsedTxt: elapsed(), cancelled: routeCancelRequested,
  });
  diag.title = diag.textContent;
  if (routeEst) routeEst.textContent = `done in ${elapsed()} (est ≈${Math.round(est.seconds)}s) · ${est.pairs} pair-searches`;

  setRouteBusy(false);
  cancelBtn.hidden = true;
  progress.hidden = true;
  updateNetSelectionUI();
}

// Rip up selected nets: remove their copper without re-routing.
// Protected nets are skipped server-side and reported in the diag line.
async function ripSelectedNets() {
  if (selectedNets.size === 0) return;
  const diag = routeDiag;
  const ripBtn = el("rip-selected-btn");
  const routeBtn = el("route-selected-btn");
  const routeAllBtn = el("route-all-btn");
  const nets = Array.from(selectedNets);
  for (const b of [ripBtn, routeBtn, routeAllBtn]) {
    if (b) { b.disabled = true; b.classList.add("loading"); }
  }
  try {
    diag.textContent = `ripping ${nets.length} net${nets.length > 1 ? "s" : ""}…`;
    const r = await postJSON("/api/routing/ripup", { net_names: nets });
    const ripped = r.ripped || {};
    const names = Object.keys(ripped);
    const txt = names.length
      ? names.map(n => `${n}: ${ripped[n].tracks || 0} tracks/${ripped[n].vias || 0} vias`).join(", ")
      : "no copper removed";
    const skipped = (r.skipped_protected || []).join(", ");
    diag.textContent = `ripped ${txt}` + (skipped ? `; kept protected ${skipped}` : "") + ` (v${r.version})`;
    if (undoBtn) undoBtn.disabled = false;
    if (r.version) {
      lastVersion = r.version;
      await fetchAndInjectSVG(r.version);
    }
    await fetchNets();
  } catch (err) {
    diag.textContent = `rip-up failed: ${err.message}`;
  } finally {
    for (const b of [ripBtn, routeBtn, routeAllBtn]) {
      if (b) b.classList.remove("loading");
    }
    updateNetSelectionUI();
  }
}

on("route-cancel-btn", "click", () => {
  // Cooperative cancel: the in-flight job stops between pairs/attempts
  // server-side (already-routed nets are kept). The poll loop observes the
  // flag, posts the cancel, then collects the partial outcome.
  routeCancelRequested = true;
  if (routeCurrentJobId) cancelRoutingJob(routeCurrentJobId);
});

// Route all unprotected nets
async function routeAllNets() {
  const diag = routeDiag;

  setRouteBusy(true);
  diag.textContent = "Routing...";
  
  try {
    const data = await apiJson("/api/nets");
    const unprotected = (data.nets || []).filter(n => !n.protected).map(n => n.name);
    if (unprotected.length === 0) return;
    
    // Select all unprotected, keep the full table rendered.
    selectedNets = new Set(unprotected);
    renderNetsTable(data.nets || []);
    await routeSelectedNets();
  } catch (err) {
    diag.textContent = `routing failed: ${err.message}`;
  } finally {
    setRouteBusy(false);
    updateNetSelectionUI();
  }
}

// Event listeners for routing panel
on("route-selected-btn", "click", routeSelectedNets);
on("route-all-btn", "click", routeAllNets);
on("rip-selected-btn", "click", ripSelectedNets);

// Collapsed route sections persist across reloads like other display-only
// prefs (layer visibility, overlay toggles). State keys are UI-only.
for (const id of ["impedance-details", "bridges-details"]) {
  const sec = el(id);
  if (!sec) continue;
  try {
    if (localStorage.getItem(`route-section:${id}`) === "open") sec.open = true;
  } catch { /* private mode: default collapsed */ }
  sec.addEventListener("toggle", () => {
    try {
      localStorage.setItem(`route-section:${id}`, sec.open ? "open" : "shut");
    } catch { /* ignore */ }
  });
}

const routingPanel = el("routing-panel");
const placementPanel = el("placement-panel");
const docsPanel = el("docs-panel");

function setActivePanel(which) {
  placementPanel.hidden = which !== "place";
  routingPanel.hidden = which !== "route";
  docsPanel.hidden = which !== "docs";
  el("place-tab-btn")?.classList.toggle("active", which === "place");
  el("route-tab-btn")?.classList.toggle("active", which === "route");
  el("docs-tab-btn")?.classList.toggle("active", which === "docs");
}

// Tab switching for Place/Route/Docs panels
on("place-tab-btn", "click", () => {
  setActivePanel("place");
});

on("route-tab-btn", "click", () => {
  setActivePanel("route");
  fetchNets();
  loadRoutingParams();
});

on("docs-tab-btn", "click", () => {
  setActivePanel("docs");
  loadDocsList();
});

// ---- Docs tab: curated markdown served by /api/docs/* (no new deps) ----
function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Docs viewer moved to app-docs.js (renderInline/renderMarkdown/loadDocs*).
// escapeHtml stays here: every panel uses it, so it belongs to core.

// Keyboard shortcuts
document.addEventListener("keydown", e => {
  // Don't trigger shortcuts when typing in inputs
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") return;
  
  if (e.key === "Escape") {
    clearSelection();
  } else if (e.key === "a" || e.key === "A") {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      selectAllUnlocked();
    }
  } else if (e.key === "p" || e.key === "P") {
    // Pin selected
    if (selectedUuids.size > 0) {
      selectedUuids.forEach(uuid => togglePin(uuid));
    }
  } else if (e.key === "u" || e.key === "U") {
    // Unpin selected
    const toUnpin = Array.from(selectedUuids).filter(u => pinnedUuids.has(u));
    toUnpin.forEach(uuid => togglePin(uuid));
  } else if (e.key === " ") {
    // Space - step selected
    e.preventDefault();
    stepSelectedBtn?.click();
  } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && motion.frames.length) {
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    e.preventDefault();
    stepMotion(e.key === "ArrowLeft" ? -1 : 1);
  } else if (e.key.startsWith("Arrow") && !motion.frames.length && selectedUuids.size > 0) {
    // Manual 1mm nudge of the placement selection (Shift: 10mm). Only when
    // no motion frames are loaded — otherwise arrows scrub the trajectory.
    e.preventDefault();
    const step = e.shiftKey ? 10 : 1;
    if (e.key === "ArrowLeft") nudgeSelected(-step, 0);
    else if (e.key === "ArrowRight") nudgeSelected(step, 0);
    else if (e.key === "ArrowUp") nudgeSelected(0, -step);
    else if (e.key === "ArrowDown") nudgeSelected(0, step);
  }
});

// Fetch and display app version
async function loadAppVersion() {
  try {
    const res = await fetch("/api/version");
    if (res.ok) {
      const data = await res.json();
      const _av = el("app-version"); if (_av) _av.textContent = "v" + data.version;
    }
  } catch (err) {
    /* version badge is best-effort */
  }
}

loadAppVersion();
loadParams().then(applyUrlPreset).catch(() => {});
{
  const _fl = el("forces-legend");
  if (_fl) _fl.style.display = el("forces-toggle")?.checked ? "inline-flex" : "none";
}
poll();
setInterval(poll, 2000);