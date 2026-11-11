// app-core.js — shared infrastructure every panel uses.
// Loads first. Owns: DOM helpers, web calls, toasts, SVG primitives,
// overlay/toggle scaffolds, table select-all, sidebar collapse, file
// loading, resizers. No domain logic; cross-panel state lives in its
// owning panel file.

// Persistent banner for boot/version problems toasts can't carry (they
// auto-hide in 4s). Hidden until shown.
function showCompat(msg) {
  try {
    const b = document.getElementById("compat-banner");
    if (b) { b.hidden = false; b.textContent = msg; b.title = msg; }
  } catch { /* DOM not ready */ }
}

// First-thing failure surface: a script that fails to load or evaluate
// otherwise dies silently — dead buttons, bare console errors. Name the
// file so the cause is actionable instead of mysterious.
window.addEventListener("error", (e) => {
  if (e.message && /ResizeObserver loop/.test(String(e.message))) return;
  const file = String(e.filename || "").split("/").pop();
  showCompat(`UI error${file ? ` in ${file}` : ""}: ${e.message || "failed to load"} — hard-refresh, check console`);
}, true);

// Fail-safe DOM helpers: a missing/renamed id warns instead of throwing at
// load and killing the whole script (script runs after the DOM, end of body).
function el(id) {
  const n = document.getElementById(id);
  if (!n) console.warn(`missing #${id}`);
  return n;
}
function on(id, evt, fn, opts) {
  const n = el(id);
  if (n) n.addEventListener(evt, fn, opts);
  return n;
}

// Shared element handles: cached once, read by every panel. They live
// here (first script) so no panel file can hit them uninitialized.

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

// Side panel collapse/expand
const sidePanel = el("side-panel");
const sidePanelToggle = el("side-panel-toggle");
let sidePanelCollapsed = false;
sidePanelToggle?.addEventListener("click", () => {
  sidePanelCollapsed = !sidePanelCollapsed;
  if (!sidePanel) return;
  if (sidePanelCollapsed) {
    sidePanel.dataset.width = sidePanel.style.width;
    sidePanel.style.width = "";
  } else if (sidePanel.dataset.width) {
    sidePanel.style.width = sidePanel.dataset.width;
  }
  sidePanel.classList.toggle("collapsed", sidePanelCollapsed);
  sidePanelToggle.textContent = sidePanelCollapsed ? "▶" : "◀";
});

// Left sidebar (parts list) collapse/expand — same pattern as the side
// panel: shrinking to a slim strip during routing work, restoring the
// (possibly resized) width on expand.
const partsSidebar = el("sidebar");
const partsSidebarToggle = el("sidebar-toggle");
let partsSidebarCollapsed = false;
partsSidebarToggle?.addEventListener("click", () => {
  partsSidebarCollapsed = !partsSidebarCollapsed;
  if (!partsSidebar) return;
  if (partsSidebarCollapsed) {
    partsSidebar.dataset.width = partsSidebar.style.width;
    partsSidebar.style.width = "";
  } else if (partsSidebar.dataset.width) {
    partsSidebar.style.width = partsSidebar.dataset.width;
  }
  partsSidebar.classList.toggle("collapsed", partsSidebarCollapsed);
  partsSidebarToggle.textContent = partsSidebarCollapsed ? "◀" : "▶";
});

async function uploadFile(f) {
  const kind = f.name.endsWith(".kicad_sch") ? "sch" : "pcb";
  const res = await apiFetch(
    `/api/load?name=${encodeURIComponent(f.name)}&kind=${kind}`,
    { method: "POST", body: f }
  );
  return res.json();
}

// Auto-pair PCB/SCH files: group by stem, load PCB first then SCH
async function uploadFiles(files) {
  const fileArray = [...files];
  // Group by stem (filename without extension)
  const byStem = new Map();
  for (const f of fileArray) {
    const stem = f.name.replace(/\.(kicad_pcb|kicad_sch)$/i, "");
    if (!byStem.has(stem)) byStem.set(stem, { pcb: null, sch: null });
    const entry = byStem.get(stem);
    if (f.name.endsWith(".kicad_pcb")) entry.pcb = f;
    else if (f.name.endsWith(".kicad_sch")) entry.sch = f;
  }
  
  // Upload: PCB first, then SCH for each pair
  for (const [stem, { pcb, sch }] of byStem) {
    try {
      if (pcb) await uploadFile(pcb);
      if (sch) await uploadFile(sch);
    } catch (err) {
      toast(`upload ${stem} failed: ${err.message}`);
    }
  }
  // Any unpaired files (shouldn't happen with current grouping, but just in case)
  const unpaired = fileArray.filter(f => {
    const stem = f.name.replace(/\.(kicad_pcb|kicad_sch)$/i, "");
    const entry = byStem.get(stem);
    return !entry || (!entry.pcb && !entry.sch);
  });
  for (const f of unpaired) {
    try {
      await uploadFile(f);
    } catch (err) {
      toast(`upload ${f.name} failed: ${err.message}`);
    }
  }
}

const dropZone = el("drop-zone");
dropZone?.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone?.addEventListener("dragleave", () => { dropZone.classList.remove("drag-over"); });
dropZone?.addEventListener("drop", async e => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  try {
    await uploadFiles(e.dataTransfer.files);
  } catch (err) {
    toast(`drop upload failed: ${err.message}`);
  }
});

// Prevent browser from opening dropped files globally (use capture to intercept early)
document.addEventListener("dragover", e => e.preventDefault(), { capture: true });
document.addEventListener("drop", e => e.preventDefault(), { capture: true });

const loadInput = el("load-input");
document.querySelectorAll("#load-btn, #load-btn-drop").forEach(btn => {
  btn.addEventListener("click", () => loadInput?.click());
});
loadInput?.addEventListener("change", async () => {
  try {
    await uploadFiles(loadInput.files);
  } catch (err) {
    toast(`upload failed: ${err.message}`);
  }
  loadInput.value = "";
});

// Demo board: loads the 4-part minimal fixture from the repo so first-time
// evaluators can try Place/Route with no KiCad files on hand.
on("demo-load-btn", "click", async () => {
  try {
    await apiFetch("/api/load?name=minimal.kicad_pcb&path=tests/fixtures/minimal.kicad_pcb", { method: "POST" });
  } catch (err) {
    toast(`demo load failed: ${err.message}`);
  }
});

// Shortcuts button: same list as the tooltip, as a toast for touch users.
on("shortcuts-btn", "click", () => {
  toast("A select unlocked · Shift+click multi-select · Alt+click pin · wheel zoom · drag pan · Fit resets · Esc clears · Space steps · Left/Right scrub or nudge · P pin · U unpin", true);
});

// Single fetch helper: throws with server text on !ok.
async function apiFetch(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res;
}

// JSON variant: parsed body, same throw-on-!ok semantics.
async function apiJson(path, opts) {
  return (await apiFetch(path, opts)).json();
}

// POST a JSON body; returns the parsed response.
async function postJSON(path, body) {
  return apiJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function jobUrl(id, suffix = "") {
  return `/api/routing/jobs/${encodeURIComponent(id)}${suffix}`;
}

async function getInlets() {
  return apiJson("/api/nets/inlets");
}
async function setInlets(body) {
  return postJSON("/api/nets/inlets", body);
}

// ---- SVG overlay + diagnostics-toggle scaffolds ----

const SVG_NS = "http://www.w3.org/2000/svg";
const OVERLAY_COLORS = { pass: "#2ea87a", attention: "#d9a13b", fail: "#e06c75" };

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function svgTitle(text) {
  const t = document.createElementNS(SVG_NS, "title");
  t.textContent = text;
  return t;
}

// Tri-state "select all" checkbox shared by the footprints and nets tables.
function syncSelectAll(id, items, isChecked) {
  const cb = el(id);
  if (!cb) return;
  cb.checked = items.length > 0 && items.every(isChecked);
  cb.indeterminate = items.some(isChecked) && !items.every(isChecked);
}

// Shared overlay scaffold: drop the stale group, bail when hidden, empty,
// or board-less, else hand a pointer-transparent <g> to drawFn.
function makeSvgOverlay(cls, visible, data, drawFn) {
  (viewport?.querySelectorAll(`.${cls}`) ?? []).forEach(n => n.remove());
  if (!visible || !data) return;
  const svg = viewport?.querySelector("svg");
  if (!svg) return;
  const g = document.createElementNS(SVG_NS, "g");
  g.classList.add(cls);
  g.classList.add("overlay-none");
  drawFn(g);
  svg.appendChild(g);
}

// Shared fetch-with-toggle-off scaffold for diagnostics buttons: first
// click fetches, paints the result box + overlay and relabels; second
// click clears the overlay without refetching. isOn/setOn close over the
// feature's module-level visible flag so existing references keep working.
async function runDiagToggle({ btnId, boxId, offLabel, onLabel, isOn, setOn, render, failPrefix, load }) {
  const btn = el(btnId);
  const box = el(boxId);
  if (!btn || !box) return;
  if (isOn()) {
    setOn(false);
    render();
    btn.textContent = offLabel;
    return;
  }
  btn.disabled = true;
  btn.textContent = "Analyzing…";
  try {
    const summary = await load();
    box.hidden = false;
    if (summary !== undefined) box.textContent = summary;
    setOn(true);
    btn.textContent = onLabel;
    render();
  } catch (err) {
    box.hidden = false;
    box.textContent = `${failPrefix}: ${err}`;
  } finally {
    btn.disabled = false;
    if (!isOn()) btn.textContent = offLabel;
  }
}

let toastTimer = 0;
function toast(msg, ok = false) {
  let toastEl = el("toast");
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.id = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.classList.toggle("ok", ok);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 4000);
  if (diagEl) diagEl.textContent = msg;
}

// Shared horizontal-resizer scaffold: drag to resize `panel` within
// [minW, maxW], persisted under `storageKey`. `sign` is +1 when dragging
// right widens (left sidebar) and -1 when dragging right narrows (right
// drawer); `onDown` runs first (e.g. expand a collapsed panel).
// Saves parseInt px so a reload restores "<n>px".
function initHorizResizer({ resizerId, panel, minW, maxW, storageKey, sign = 1, onDown }) {
  const resizer = el(resizerId);
  if (!resizer || !panel) return;
  const saved = localStorage.getItem(storageKey);
  if (saved) panel.style.width = parseInt(saved, 10) + "px";
  let startX = 0, startWidth = 0;
  const curWidth = () => {
    const px = parseInt(getComputedStyle(panel).width, 10);
    return isNaN(px) || px === 0 ? panel.offsetWidth : px;
  };
  const doDrag = e => {
    const width = startWidth + sign * (e.clientX - startX);
    panel.style.width = Math.max(minW, Math.min(maxW, width)) + "px";
  };
  const stopDrag = () => {
    resizer.classList.remove("dragging");
    document.removeEventListener("mousemove", doDrag);
    document.removeEventListener("mouseup", stopDrag);
    localStorage.setItem(storageKey, parseInt(panel.style.width, 10));
  };
  resizer.addEventListener("mousedown", e => {
    e.preventDefault();
    if (onDown) onDown();
    startX = e.clientX;
    startWidth = curWidth();
    resizer.classList.add("dragging");
    document.addEventListener("mousemove", doDrag);
    document.addEventListener("mouseup", stopDrag);
  });
}

// Resizable sidebar splitter (script runs after the DOM, so elements exist).
initHorizResizer({
  resizerId: "resizer", panel: partsSidebar,
  minW: 320, maxW: 500, storageKey: "sidebar-width", sign: 1,
});

// Side panel horizontal resizer (drag left = widen).
initHorizResizer({
  resizerId: "panel-resizer", panel: sidePanel,
  minW: 300, maxW: 600, storageKey: "side-panel-width", sign: -1,
  onDown: () => { if (sidePanelCollapsed) sidePanelToggle.click(); },
});