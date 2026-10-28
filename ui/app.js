// app.js — entry point: shared element cache, panel tabs, shortcuts, boot.
// Panel domains live in app-*.js (load order in index.html); this file runs
// last among app scripts and kicks off loading (loadAppVersion, loadParams,
// poll). Cross-file calls are fine; keep top-level names unique across files
// (byte-scan before each extraction).

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