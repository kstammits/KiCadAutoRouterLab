// app-placement.js — parts, selection, ghosts, motion, run/accept, mode.
// Needs core + viewer (el/api/zoom/overlays) at load; reads nets + routing at runtime.

function setConnected(ok) {
  const badge = el("conn-badge");
  if (badge) badge.hidden = ok;
  if (!ok && diagEl) {
    if (diagEl) diagEl.textContent = "disconnected — server unreachable (retrying)";
  }
}

// Route/rip buttons share one busy state: all four disable, the two
// route buttons show the spinner. (ripSelectedNets manages ripBtn's own
// spinner; route tails rely on updateNetSelectionUI for final enable.)
function setRouteBusy(on) {
  const btns = ["route-selected-btn", "route-all-btn", "select-unrouted-btn", "rip-selected-btn", "refine-btn", "shuffle-btn"]
    .map(id => el(id));
  for (const b of btns) if (b) b.disabled = on;
  for (const b of btns.slice(0, 2)) if (b) b.classList.toggle("loading", on);
  const shuffleCount = el("shuffle-count");
  if (shuffleCount) shuffleCount.disabled = on;
}

// Inlet/bridge state lives in one endpoint; read-modify-write callers
// share these so the URL and parse appear once.

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
      if (st.selection_version !== undefined) lastSelectionVersion = st.selection_version;
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
    } else if (st.selection_version !== undefined
        && st.selection_version !== lastSelectionVersion
        && inFlightSelection === 0) {
      // Selection/pin changed elsewhere (another tab): light sync only.
      // Never re-injects the SVG here — selection highlight is client-side
      // class toggles (updateFootprintVisuals), so this stays cheap.
      lastSelectionVersion = st.selection_version;
      await loadSelectionLight();
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
    initialViewBox = null;
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
        
        // Single bulk replace (the old code toggled each uuid, which
        // could *deselect* already-selected parts server-side).
        replaceSelectionBulk([...selectedUuids]);
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
// selectedUuids is the single source of truth on the client (optimistic).
// fp.selected / fp.pinned in the allFootprints cache are stamped to match
// after every local change or server sync — renderers read the Sets.
let selectedUuids = new Set();
let pinnedUuids = new Set();
let selectedGhostUuid = null; // Ghost selected for move/delete (never in selectedUuids)
let lastSelectionVersion = 0; // Server selection_version (view state, not board version)
let inFlightSelection = 0; // Optimistic POSTs in flight (poll skips light-sync while > 0)

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
  // Tidy scope line mirrors the same selection (move subset vs all).
  if (typeof syncTidyScope === "function") syncTidyScope();
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

// Mirror selectedUuids/pinnedUuids into the rendered table rows + header in
// place. Never rebuilds tbody (unlike renderFootprintTable), so the clicked
// checkbox keeps its native instant check and filter/sort/focus survive.
function syncFpRowSelection() {
  document.querySelectorAll("#fp-tbody tr").forEach(tr => {
    const uuid = tr.dataset.uuid;
    if (!uuid) return;
    const on = selectedUuids.has(uuid);
    const cb = tr.querySelector('input[type="checkbox"]');
    if (cb && cb.checked !== on) cb.checked = on;
    tr.classList.toggle("fp-selected-row", on || uuid === selectedGhostUuid);
    const fp = allFootprints.find(f => f.uuid === uuid);
    if (fp) {
      fp.selected = on;
      fp.pinned = pinnedUuids.has(uuid);
      tr.classList.toggle("fp-pinned-row", !!fp.pinned);
      tr.classList.toggle("fp-locked-row", !!fp.locked);
      if (cb) cb.disabled = !!(fp.locked || fp.pinned || fp.ghost);
      const badgeCell = tr.querySelector(".fp-col-state");
      if (badgeCell) badgeCell.innerHTML = getStateBadge(fp);
    }
  });
  syncSelectAll("fp-select-all", getFilteredFootprints(), fp => selectedUuids.has(fp.uuid));
  updateSelectionUI();
}

// Cheap paint: in-place row sync + SVG highlight. Selection handlers use
// this instead of renderFootprintTable() so the check paints immediately.
function applySelectionUI() {
  syncFpRowSelection();
  updateFootprintVisuals();
}

// Zoom a frame after paint so getBBox layout work never blocks the check.
function scheduleZoomToSelection() {
  requestAnimationFrame(() => {
    try { zoomToSelection(); } catch (e) { /* zoom is best-effort */ }
  });
}

// Light selection sync for the poll loop (another tab changed selection):
// no SVG re-inject, no footprint refetch — just adopt + repaint in place.
async function loadSelectionLight() {
  try {
    const [pinData, selData] = await Promise.all([
      apiJson("/api/placement/pinned"),
      apiJson("/api/placement/selected"),
    ]);
    pinnedUuids = new Set(pinData.pinned || []);
    selectedUuids = new Set(selData.selected || []);
    for (const fp of allFootprints) {
      fp.selected = selectedUuids.has(fp.uuid);
      fp.pinned = pinnedUuids.has(fp.uuid);
    }
    applySelectionUI();
  } catch {
    // Silent: this runs in the poll loop, and explicit callers already
    // reconcile — a toast every 2s would bury the real message.
  }
}

async function togglePin(uuid) {
  inFlightSelection++;
  try {
    const data = await postJSON("/api/placement/pin", { uuid });
    pinnedUuids = new Set(data.pinned || []);
    if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
    // If pinned, remove from selected
    if (pinnedUuids.has(uuid)) selectedUuids.delete(uuid);
    const fp = allFootprints.find(f => f.uuid === uuid);
    if (fp) { fp.pinned = pinnedUuids.has(uuid); fp.selected = selectedUuids.has(uuid); }
    applySelectionUI();
  } catch (err) {
    toast(`pin failed: ${err.message}`);
  } finally {
    inFlightSelection--;
  }
}

async function toggleSelect(uuid) {
  if (pinnedUuids.has(uuid)) return; // Can't select pinned
  const fp = allFootprints.find(f => f.uuid === uuid);
  if (fp && fp.ghost) return; // Ghosts use selectGhost, never placement selection
  if (fp && fp.locked) return;
  const on = !selectedUuids.has(uuid);
  if (on) selectedUuids.add(uuid);
  else selectedUuids.delete(uuid);
  if (fp) fp.selected = on;
  applySelectionUI();
  inFlightSelection++;
  try {
    const data = await postJSON("/api/placement/select", { uuid, selected: on });
    if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
  } catch (err) {
    // Keep the optimistic highlight (server is reconciled below / on the
    // next poll) — instant rollback is what made clicks look dead.
    toast(`select failed (kept locally): ${err.message}`);
    loadSelectionLight().catch(() => {});
  } finally {
    inFlightSelection--;
  }
}

async function selectAllUnlocked() {
  inFlightSelection++;
  try {
    const data = await apiJson("/api/placement/select_all", { method: "POST" });
    if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
    selectedUuids = new Set(data.selected || []);
    for (const fp of allFootprints) fp.selected = selectedUuids.has(fp.uuid);
    applySelectionUI();
    scheduleZoomToSelection();
  } catch (err) {
    toast(`select all failed: ${err.message}`);
  } finally {
    inFlightSelection--;
  }
}

async function clearSelection() {
  const prev = new Set(selectedUuids);
  const prevGhost = selectedGhostUuid;
  selectedUuids.clear();
  selectedGhostUuid = null;
  for (const fp of allFootprints) fp.selected = false;
  applySelectionUI();
  updateGhostSelUI();
  inFlightSelection++;
  try {
    const data = await apiJson("/api/placement/clear_selection", { method: "POST" });
    if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
  } catch (err) {
    toast(`clear selection failed (kept locally): ${err.message}`);
    loadSelectionLight().catch(() => {});
  } finally {
    inFlightSelection--;
  }
}

// Replace the whole selection in one round-trip (single-select, bulk ops).
async function replaceSelectionBulk(uuids, { zoom = false } = {}) {
  const prev = new Set(selectedUuids);
  selectedUuids = new Set(uuids);
  for (const fp of allFootprints) fp.selected = selectedUuids.has(fp.uuid);
  applySelectionUI();
  if (zoom) scheduleZoomToSelection();
  inFlightSelection++;
  try {
    const data = await postJSON("/api/placement/selection", { selected: [...selectedUuids] });
    if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
    if (Array.isArray(data.selected)) {
      selectedUuids = new Set(data.selected);
      for (const fp of allFootprints) fp.selected = selectedUuids.has(fp.uuid);
      applySelectionUI();
    }
  } catch (err) {
    toast(`selection failed (kept locally): ${err.message}`);
    loadSelectionLight().catch(() => {});
  } finally {
    inFlightSelection--;
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
      // Single select: clear others, select this (server notified via bulk
      // replace so SVG clicks stay in sync with Step/Run scope).
      if (!pinnedUuids.has(uuid)) {
        selectGhost(null); // switching back to parts clears ghost focus
        replaceSelectionBulk([uuid]);
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
  if (selectedUuids.has(fp.uuid) || fp.selected) return 3;
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
    // selectedUuids/pinnedUuids are the truth after fetchPinned/fetchSelected;
    // stamp the cache so renders stay coherent even if the server payload
    // lags an in-flight optimistic change.
    for (const fp of allFootprints) {
      fp.selected = selectedUuids.has(fp.uuid);
      if (pinnedUuids.has(fp.uuid)) fp.pinned = true;
    }
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
    if (filters.state === "selected" && !selectedUuids.has(fp.uuid) && !fp.selected) return false;
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
  if (selectedUuids.has(fp.uuid) || fp.selected) return '<span class="state-badge state-selected">Selected</span>';
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
  const isSel = selectedUuids.has(fp.uuid) || fp.selected;
  const isIllegal = illegalUuids.has(fp.uuid);
  const tooltip = escapeHtml(getLegalityTooltip(fp));
  const ref = escapeHtml(fp.ref);
  const fpId = escapeHtml(fp.footprint_id);
  const uuid = escapeHtml(fp.uuid);
  const netsTitle = escapeHtml(nets.join(", "));
  return `
    <tr class="${isSel || isGhostSel ? 'fp-selected-row' : ''} ${fp.locked ? 'fp-locked-row' : ''} ${fp.pinned ? 'fp-pinned-row' : ''} ${fp.ghost ? 'fp-ghost-row' : ''} ${isIllegal ? 'fp-illegal-row' : ''}" data-uuid="${uuid}" ${tooltip ? `title="${tooltip}"` : ''}>
      <td class="fp-col-select"><input type="checkbox" ${isSel ? 'checked' : ''} ${fp.locked || fp.pinned || fp.ghost ? 'disabled' : ''} data-uuid="${uuid}" title="Select ${ref} for placement" aria-label="Select ${ref} for placement"></td>
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
  
  // Checkbox toggles paint synchronously (native check), then sync in
  // place + notify the server. No tbody rebuild here: the row is already
  // correct, and a rebuild would wipe the just-painted check with stale
  // cache state before first paint.
  tbody.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", async e => {
      const uuid = e.target.dataset.uuid;
      const on = e.target.checked;
      const fp = allFootprints.find(f => f.uuid === uuid);
      if (fp && (fp.locked || fp.pinned || fp.ghost)) { e.target.checked = !on; return; }
      if (on) selectedUuids.add(uuid);
      else selectedUuids.delete(uuid);
      if (fp) fp.selected = on;
      // In-place row highlight + SVG highlight paint now; zoom next frame.
      syncFpRowSelection();
      updateFootprintVisuals();
      scheduleZoomToSelection();
      inFlightSelection++;
      try {
        const data = await postJSON("/api/placement/select", { uuid, selected: on });
        if (data.selection_version !== undefined) lastSelectionVersion = data.selection_version;
      } catch (err) {
        // Revert so UI and server agree.
        if (on) selectedUuids.delete(uuid);
        else selectedUuids.add(uuid);
        if (fp) fp.selected = !on;
        syncFpRowSelection();
        updateFootprintVisuals();
        toast(`select failed: ${err.message}`);
      } finally {
        inFlightSelection--;
      }
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

      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        // Additive toggle; highlight paints inside toggleSelect.
        toggleSelect(uuid).then(() => { scheduleZoomToSelection(); });
      } else {
        replaceSelectionBulk([uuid], { zoom: true });
      }
    });
  });

  // Update select all checkbox
  syncSelectAll("fp-select-all", filtered, fp => selectedUuids.has(fp.uuid));
  
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
  const next = new Set(selectedUuids);
  filtered.forEach(fp => {
    if (!fp.locked && !fp.pinned && !fp.ghost) {
      next.add(fp.uuid);
    }
  });
  // Single bulk replace (the old N×toggle could deselect server-side).
  replaceSelectionBulk([...next]);
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

// Select all checkbox in table header (bulk replace, in-place repaint).
on("fp-select-all", "change", e => {
  const filtered = getFilteredFootprints();
  const next = new Set(selectedUuids);
  if (e.target.checked) {
    filtered.forEach(fp => {
      if (!fp.locked && !fp.pinned && !fp.ghost) {
        next.add(fp.uuid);
      }
    });
  } else {
    filtered.forEach(fp => next.delete(fp.uuid));
  }
  replaceSelectionBulk([...next]);
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
    if (!data.frames || !data.frames.length) {
      if (!silent) diagEl.textContent = "no motion frames — Step to record a trajectory";
      resetMotionState();
      return;
    }
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
      ? rippedNames.map(n => `${n}: ${ripped[n].tracks || 0} tracks` +
          ((ripped[n].arcs || 0) ? `/${ripped[n].arcs} arcs` : "") +
          `/${ripped[n].vias || 0} vias`).join(", ")
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
        const changed = (rep.tracks_removed || 0) + (rep.arcs_removed || 0) + (rep.vias_converted || 0)
          + (rep.zones_removed || 0) + (rep.zones_restricted || 0);
        if (changed > 0) {
          const ok = confirm(
            `Two-layer export collapses inner copper (original untouched):\n` +
            `− ${rep.tracks_removed} inner tracks removed` +
            ((rep.arcs_removed || 0) ? `, ${rep.arcs_removed} inner arcs removed` : "") + `\n` +
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