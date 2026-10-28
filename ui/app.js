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

// Calibrated behavior knobs get main-panel sliders; power/decap/ghost and
// the convergence threshold live under Advanced. Everything else
// (repulsion, solver internals, mass fallbacks) stays API-only at
// calibrated defaults — POST /api/placement/params merges, so hidden keys
// in placement.json survive GUI saves.
// Ranges cover both Cluster (scattered starts) and Legalize (placed boards) regimes.
const PARAM_DEFS = [
  { key: "attraction_k_N", label: "Attraction k (N)", min: 1e-4, max: 2e-1, step: 1e-4, log: true },
  { key: "wall_k_N", label: "Wall stiffness k (N)", min: 1e-5, max: 1, step: 1e-5, log: true },
  // display: UI shows millimeters, engine/API stay SI (see toDisplay/fromDisplay).
  { key: "wall_halo_m", label: "Wall halo (mm)", min: 0, max: 0.015, step: 0.0005,
    display: { unit: "mm", scale: 1000, step: 0.5 } },
  { key: "damping_Ns_m", label: "Damping (N·s/m)", min: 1e-3, max: 1, step: 1e-4, log: true },
  { key: "max_iterations", label: "Max iterations", min: 1, max: 20000, step: 10 },
];

const ADVANCED_PARAM_DEFS = [
  { key: "power_attraction_scale", label: "Power attraction", min: 0, max: 10, step: 0.05 },
  { key: "decap_boost", label: "Decap boost", min: 0, max: 16, step: 0.5 },
  { key: "ghost_k_N", label: "Ghost pull (N)", min: 1e-7, max: 1e-3, step: 1e-7, log: true },
  { key: "wall_exponent", label: "Wall exponent", min: 1.0, max: 3.0, step: 0.1 },
  { key: "cross_layer_pin_halo_m", label: "X-layer pin halo (mm)", min: 0, max: 5, step: 0.05,
    display: { unit: "mm", scale: 1000, step: 0.05 } },
  { key: "momentum_alpha", label: "Momentum alpha", min: 0.0, max: 1.0, step: 0.05 },
  { key: "contact_tangent_gamma", label: "Contact tangent γ", min: 0.0, max: 0.5, step: 0.01 },
  { key: "convergence_eps_m", label: "Convergence eps (mm)", min: 1e-7, max: 1e-3, step: 1e-7, log: true,
    display: { unit: "mm", scale: 1000, step: 0.0001 } },
  { key: "vmax_m_s", label: "Max speed (mm/s)", min: 0.01, max: 5.0, step: 0.01,
    display: { unit: "mm/s", scale: 1000, step: 10 } },
  { key: "dt_s", label: "Timestep (ms)", min: 2e-4, max: 2e-3, step: 1e-4,
    display: { unit: "ms", scale: 1000, step: 0.1 } },
  { key: "friction_N", label: "Friction deadband (µN)", min: 0.0, max: 1e-4, step: 1e-7,
    display: { unit: "µN", scale: 1e6, step: 0.1 } },
];

const ALL_PARAM_DEFS = [...PARAM_DEFS, ...ADVANCED_PARAM_DEFS];

const PARAM_DESC = {
  attraction_k_N: "Constant-magnitude attraction between net-connected pads (newtons). Legalize: 0.005-0.01; Cluster: 0.04-0.06. Log scale.",
  wall_k_N: "Elastic collision stiffness when courtyards overlap or enter the halo (newtons). One linear law F = k*(1-s/h) in signed distance s (gap +, penetration -): zero past the halo, k at contact, growing with depth. Deepest contact wins per pair. Legalize: 0.08; Cluster: 0.02-0.03. Log scale.",
  wall_halo_m: "Near-miss reach past part outlines. Shown in mm; stored in meters. Legalize: 4mm; Cluster and engine default: 6mm.",
  damping_Ns_m: "Viscous damping F = −c·v (N·s/m). The physics damper: higher settles faster with less overshoot. Legalize: 0.12; Cluster: 0.08. Log scale.",
  max_iterations: "Hard limit on simulation steps (dimensionless).",
  power_attraction_scale: "Power-rail attraction trim (fanout-scaled k/(fanout-1) per edge). 1 = full participation; 0 restores the old exclude-power behavior.",
  decap_boost: "Decoupling-cap edge multiplier (on top of fanout scaling). Also gates the decap GND-side exception: 0 removes decap GND edges.",
  ghost_k_N: "Ghost attractor spring scale (newtons); file ka values weight it. Log scale. Only matters with ghost attractors.",
  wall_exponent: "Exponent p in wall force F = k*(1-s/h)^p. 1 = linear, 2 = quadratic (stiffens deep penetration). Legalize: 2.0; Cluster: 1.0-1.5.",
  cross_layer_pin_halo_m: "Near-miss reach for THT pins protruding onto the far side vs opposite-side bodies. Shown in mm; stored in meters. Solder-fillet clearance (~0.7mm default), not the 4-6mm courtyard halo. 0 disables cross-layer pin response.",
  momentum_alpha: "Velocity blending alpha after rigid projection: 0 = re-derive from displacement, 1 = keep old velocity. Higher = more momentum persistence to nudge past wedges. Default 0.7.",
  contact_tangent_gamma: "Tangential assist coefficient γ: fraction of normal force applied along the tangential axis at contact. Helps parts slide along walls instead of wedging. 0 = disabled, 0.1-0.3 typical. Default 0.15.",
  convergence_eps_m: "Stop when max pad displacement per step falls below this. Shown in mm; stored in meters. Calibrated 20µm (si_force_laws.md): smaller fake-settles under contact chatter, larger stops earlier. Log scale.",
  vmax_m_s: "Speed ceiling for pad velocities and rigid-step clamp (m/s). Higher allows faster movement but less stable. Default 1.0 m/s.",
  dt_s: "Simulation timestep: 0.2-2ms, bounded by the dt_min/dt_max solver guards (wider values fail validation). Adaptive guard halves on energy rise, regrows on fall. Default 1ms.",
  friction_N: "Dry-friction deadband (newtons). Whole footprint freezes when all pad forces below this. Default 1e-6 N.",
};

// Display-unit conversion: defs with `display: {scale}` show scaled values
// (e.g. mm) while the API/engine stay SI. All three inputs (range slider,
// number box, log slider position) speak display units; conversion happens
// only at the API boundary (loadParams in, readParams out).
function toDisplay(val, def) {
  return def.display ? val * def.display.scale : val;
}
function fromDisplay(val, def) {
  return def.display ? val / def.display.scale : val;
}
// Slider/number bounds in display units (def.min/max/step stay SI).
function dispStep(def) {
  return def.display && def.display.step ? def.display.step : def.step;
}
function dispDecimals(def) {
  const s = dispStep(def);
  if (s >= 1) return 0;
  if (s >= 0.1) return 1;
  if (s >= 0.001) return 3;
  return 4;
}

function buildParamRow(def, desc) {
  const el = document.createElement("div");
  el.className = "param";
  const isLog = def.log === true;
  const dmin = toDisplay(def.min, def);
  const dmax = toDisplay(def.max, def);
  const dstep = dispStep(def);

  if (isLog) {
    // Log-scale parameter
    const logMin = Math.log10(dmin);
    const logMax = Math.log10(dmax);
    el.innerHTML = `<span title="${desc}">${def.label}</span>
      <div class="row">
        <input type="range" data-key="${def.key}" data-log="true" min="${logMin}" max="${logMax}" step="0.01">
        <input type="number" data-key="${def.key}" min="${dmin}" max="${dmax}" step="${dstep}">
      </div>`;
  } else {
    // Linear parameter
    el.innerHTML = `<span title="${desc}">${def.label}</span>
      <div class="row">
        <input type="range" data-key="${def.key}" min="${dmin}" max="${dmax}" step="${dstep}">
        <input type="number" data-key="${def.key}" min="${dmin}" max="${dmax}" step="${dstep}">
      </div>`;
  }
  return el;
}

const paramsGrid = el("params-grid");
for (const def of PARAM_DEFS) {
  paramsGrid.appendChild(buildParamRow(def, PARAM_DESC[def.key] || ""));
}
const advancedGrid = el("advanced-params-grid");
for (const def of ADVANCED_PARAM_DEFS) {
  advancedGrid.appendChild(buildParamRow(def, PARAM_DESC[def.key] || ""));
}

// Full descriptions stay in tooltips (PARAM_DESC) + Docs panel; no static table.
void ALL_PARAM_DEFS;

function logToValue(logVal, def) {
  return Math.pow(10, logVal);
}

function valueToLog(val, def) {
  return Math.log10(val);
}

function syncParamInputs(key, def, source) {
  const grid = source?.closest?.("#params-grid, #advanced-params-grid") || document;
  const range = grid.querySelector(`input[type=range][data-key="${key}"]`);
  const num = grid.querySelector(`input[type=number][data-key="${key}"]`);
  if (!range || !num) return;
  const isLog = def?.log === true;

  if (source?.type === "range") {
    if (isLog) {
      num.value = logToValue(Number(range.value), def).toFixed(dispDecimals(def));
    } else {
      num.value = range.value;
    }
  } else if (source?.type === "number") {
    if (isLog) {
      range.value = valueToLog(Number(num.value), def).toFixed(2);
    } else {
      range.value = num.value;
    }
  }
}

// Slider input only syncs range<->number positions (no network): a drag
// fires dozens of input events, so the force recompute waits for `change`
// (drag release / number-box commit) instead of churning per tick.
function onParamInput(e) {
  const key = e.target.dataset.key;
  if (!key) return;
  const def = ALL_PARAM_DEFS.find(d => d.key === key);
  syncParamInputs(key, def, e.target);
}
function onParamChange(e) {
  const key = e.target.dataset.key;
  if (!key) return;
  // Refresh forces on release if currently shown.
  if (typeof forcesToggle !== "undefined" && forcesToggle.checked && lastVersion) {
    refreshForces();
  }
}
paramsGrid.addEventListener("input", onParamInput);
advancedGrid.addEventListener("input", onParamInput);
paramsGrid.addEventListener("change", onParamChange);
advancedGrid.addEventListener("change", onParamChange);

function readParams() {
  // Number boxes speak display units; convert back to SI for the API.
  const out = {};
  for (const def of ALL_PARAM_DEFS) {
    const num = document.querySelector(`#params-grid input[type=number][data-key="${def.key}"]`)
      || document.querySelector(`#advanced-params-grid input[type=number][data-key="${def.key}"]`);
    if (num) out[def.key] = fromDisplay(Number(num.value), def);
  }
  return out;
}

async function loadParams(defaultsOnly = false) {
  const p = await apiJson(`/api/placement/params${defaultsOnly ? "?defaults=1" : ""}`);
  for (const def of ALL_PARAM_DEFS) {
    if (!(def.key in p)) continue;
    // API speaks SI; convert to display units for both inputs.
    const shown = toDisplay(p[def.key], def);
    const range = document.querySelector(`input[type=range][data-key="${def.key}"]`);
    const num = document.querySelector(`input[type=number][data-key="${def.key}"]`);
    if (range && def.log) {
      // Guard non-positive saved values (log undefined): pin slider at min.
      const logVal = shown > 0 ? valueToLog(shown, def) : Math.log10(toDisplay(def.min, def));
      range.value = Math.min(Math.max(logVal,
        Math.log10(toDisplay(def.min, def))), Math.log10(toDisplay(def.max, def))).toFixed(2);
    } else if (range) {
      // Clamp the slider thumb into range; the number box keeps the exact
      // saved value so out-of-range params stay visible, not silent.
      range.value = Math.min(Math.max(shown,
        toDisplay(def.min, def)), toDisplay(def.max, def));
    }
    if (num) num.value = shown;
  }
}

// Regime presets from docs/si_force_laws.md §3: no single ratio clusters
// AND legalizes, so offer the two calibrated starting points. dt_s, vmax,
// friction ride along via the merge-POST even though they have no main-panel sliders.
// Calibrated values: Cluster uses a 6mm halo; Legalize uses 4mm so dense
// neighbor fields do not overpower a part's own wall. wall_exponent=2.0 and
// convergence_eps_m=2e-5 ride along via merge-POST. Both pin
// cross_layer_pin_halo_m=0.7mm so a stale saved value cannot silently
// diverge from calibration (far-side solder clearance is regime-independent).
const REGIME_PRESETS = {
  cluster: { attraction_k_N: 0.05, wall_k_N: 0.025, damping_Ns_m: 0.08, dt_s: 2e-3, vmax_m_s: 1.0, friction_N: 1e-6, wall_halo_m: 0.006, cross_layer_pin_halo_m: 0.0007, wall_exponent: 1.0, convergence_eps_m: 2e-5 },
  legalize: { attraction_k_N: 0.008, wall_k_N: 0.1, damping_Ns_m: 0.12, dt_s: 1e-3, vmax_m_s: 1.0, friction_N: 1e-6, wall_halo_m: 0.004, cross_layer_pin_halo_m: 0.0007, wall_exponent: 2.0, convergence_eps_m: 2e-5 },
};

async function applyRegimePreset(name) {
  const preset = REGIME_PRESETS[name];
  if (!preset) return;
  const res = await apiFetch("/api/placement/params", {
    method: "POST",
    body: JSON.stringify(preset),
  });
  await loadParams();
  if (diagEl) {
    if (diagEl) diagEl.textContent = `${name} preset applied (persists on next run)`;
  }
}

on("regime-cluster-btn", "click", async () => {
  try { await applyRegimePreset("cluster"); }
  catch (err) { if (diagEl) diagEl.textContent = `preset failed: ${err.message}`; }
});
on("regime-legalize-btn", "click", async () => {
  try { await applyRegimePreset("legalize"); }
  catch (err) { if (diagEl) diagEl.textContent = `preset failed: ${err.message}`; }
});

// Bookmarkable parameter presets via URL query params. Keys are the
// PlacementParams field names in SI units (same as the params API), e.g.
// ?attraction_k_N=0.05&wall_k_N=0.025&max_iterations=200. On load, recognized
// keys are merge-POSTed (same semantics as the regime preset buttons, so
// hidden keys persist) and the sliders refresh. "Copy link" serializes the
// full current set back into such a URL.
function parseUrlPreset(knownKeys) {
  const qs = new URLSearchParams(location.search);
  const out = {};
  for (const [key, raw] of qs) {
    if (!knownKeys.has(key)) continue;
    if (key === "include_power_nets") {
      const v = raw.trim().toLowerCase();
      if (["1", "true", "yes"].includes(v)) out[key] = true;
      else if (["0", "false", "no"].includes(v)) out[key] = false;
      continue;
    }
    const num = Number(raw);
    if (Number.isFinite(num)) out[key] = num;
  }
  return out;
}

async function applyUrlPreset() {
  const cur = await apiJson("/api/placement/params");
  const preset = parseUrlPreset(new Set(Object.keys(cur)));
  const n = Object.keys(preset).length;
  if (n === 0) return;
  const res = await fetch("/api/placement/params", {
    method: "POST",
    body: JSON.stringify(preset),
  });
  if (!res.ok) {
    if (diagEl) {
      if (diagEl) diagEl.textContent = `URL preset rejected: ${await res.text()}`;
    }
    return;
  }
  await loadParams();
  if (diagEl) {
    if (diagEl) diagEl.textContent = `${n} param${n === 1 ? "" : "s"} applied from URL (persists on next run)`;
  }
}

async function copyPresetLink() {
  const p = await apiJson("/api/placement/params");
  const url = new URL(location.href);
  url.search = "";
  for (const [key, val] of Object.entries(p)) {
    if (typeof val === "number" || typeof val === "boolean") {
      url.searchParams.set(key, String(val));
    }
  }
  const link = url.toString();
  try {
    await navigator.clipboard.writeText(link);
  } catch (err) {
    // Clipboard API needs a secure context; fall back to a prompt-less
    // textarea select so the user can copy manually.
    const ta = document.createElement("textarea");
    ta.value = link;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  if (diagEl) {
    if (diagEl) diagEl.textContent = "preset link copied to clipboard";
  }
}

on("copy-link-btn", "click", async () => {
  try { await copyPresetLink(); }
  catch (err) { if (diagEl) diagEl.textContent = `copy link failed: ${err.message}`; }
});

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
  if (current.length === layers.length && current.every((l, i) => l === layers[i]) && el("layer-solo-all")) return;
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
  let legend = el("layer-legend");
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

let lastVersion = 0;
let viewBox = null; // [minX, minY, width, height]
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
  const pct = Math.round((1 / viewBox[2]) * viewport.clientWidth * 100);
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
  if (!svg) return;
  // Read the original viewBox from the SVG
  const vbAttr = svg.getAttribute("viewBox");
  if (!vbAttr) return;
  const vb = vbAttr.split(" ").map(Number);
  if (vb.length === 4) {
    viewBox = [...vb];
    applyViewBox();
  }
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
  if (svg && viewBox === null) {
    const vbAttr = svg.getAttribute("viewBox");
    if (vbAttr) {
      const vb = vbAttr.split(" ").map(Number);
      if (vb.length === 4) {
        viewBox = [...vb];
      }
    }
  }
  applyViewBox();
  applyLayerVisibility();
  highlightSelectedNets();
  renderGndIslandsOverlay();
  if (motion.frames.length) applyMotionFrame(motion.idx);
}

function setConnected(ok) {
  const badge = el("conn-badge");
  if (badge) badge.hidden = ok;
  if (!ok && diagEl) {
    if (diagEl) diagEl.textContent = "disconnected — server unreachable (retrying)";
  }
}

// Single fetch helper: throws with server text on !ok. New code uses this;
// older call sites migrate as touched.
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

let selectedNets = new Set();  // Set of net names selected for routing
let routingParams = {};  // Current routing parameters

// Net color: single source is the server (/api/nets color, from
// svg_render.net_colors with fixed power/ground hues). Fall back to the
// sorted-names golden-angle sweep when the row carries no color (e.g.
// synthetic selection-only rows).
function netColorByName(net, idx) {
  const row = (lastNetsByName) ? lastNetsByName.get(net) : null;
  if (row && row.color) return row.color;
  let names = [];
  if (lastNetsByName && lastNetsByName.size) {
    names = [...lastNetsByName.keys()].filter(Boolean).sort();
  } else if (allFootprints && allFootprints.length) {
    names = [...new Set(allFootprints.flatMap(fp => fp.nets || []).filter(Boolean))].sort();
  }
  const i = names.indexOf(net);
  const hue = (((i >= 0 ? i : idx) * 137.508) % 360);
  return `hsl(${hue}, 75%, 62%)`;
}
// Fetch all nets from server and render net table
// lastNetsByName caches the most recent /api/nets rows (pad counts) so the
// routing pre-flight estimate can size the work without an extra fetch.
let lastNetsByName = new Map();
async function fetchNets() {
  if (!lastVersion) return;
  try {
    const data = await apiJson("/api/nets");
    lastNetsByName = new Map((data.nets || []).map(n => [n.name, n]));
    renderNetsTable(data.nets || []);
  } catch (err) {
    toast(`nets refresh failed: ${err.message}`);
  }
}

// Render nets table
function netRowHtml(net, idx) {
  const color = netColorByName(net.name, idx);
  const pwrTitle = net.power_user
    ? "Power net (your override — click Pwr cell to unmark)"
    : (net.is_power
      ? "Power net (keyword rules — click Pwr cell to unmark)"
      : "Signal net — click Pwr cell to mark as power");
  const pwr = net.is_power
    ? `<span class="power-badge is-power" title="${pwrTitle}">PWR</span>`
    : (net.is_ground
      ? `<span class="power-badge is-ground" title="Ground net (keyword rules)">GND</span>`
      : "");
  // Pwr cell control: always render a visible button (ghost bolt for
  // signal nets) — the old `${pwr || ""}` left signal cells empty and
  // undiscoverable. Name-column badge above stays static text.
  // Board-controlled strings are HTML-escaped (net/ref names come from the file).
  const safeName = escapeHtml(net.name);
  const netName = escapeHtml(net.name);
  const pwrCtrl = net.is_power
    ? `<button type="button" class="pwr-toggle is-on" aria-pressed="true" aria-label="Unmark ${safeName} as power" title="${pwrTitle}">${pwr}</button>`
    : (net.is_ground
      ? `<button type="button" class="pwr-toggle is-ground" aria-pressed="false" aria-label="Mark ${safeName} as power (override)" title="Ground net (keyword rules) — click to add your power override">${pwr}</button>`
      : `<button type="button" class="pwr-toggle" aria-pressed="false" aria-label="Mark ${safeName} as power" title="${pwrTitle}">&#9889;</button>`);
  const width = (net.width_mm ?? "") === "" ? "" : Number(net.width_mm).toFixed(2);
  const decaps = Array.isArray(net.decap_refs) ? net.decap_refs : [];
  const decap = decaps.length
    ? `<span class="decap-badge" title="Decoupling caps: ${escapeHtml(decaps.join(", "))}">◉×${decaps.length}</span>`
    : "";
  return `
    <tr class="${selectedNets.has(net.name) ? 'net-selected-row' : ''}" data-net="${safeName}" style="--net-color: ${color};">
      <td class="net-col-select"><input type="checkbox" class="net-col-select" ${selectedNets.has(net.name) ? 'checked' : ''} data-net="${safeName}"></td>
      <td class="net-col-name">
        <span class="net-swatch" style="background: ${color};"></span>
        ${netName}${pwr}
      </td>
      <td class="net-col-pads">${net.pad_count ?? ""}</td>
      <td class="net-col-fps">${net.footprint_count ?? ""}</td>
      <td class="net-col-tracks">${net.track_count ?? ""}</td>
      <td class="net-col-vias">${net.via_count ?? ""}</td>
      <td class="net-col-width">${width}</td>
      <td class="net-col-pwr" data-power-cell="${safeName}" title="${pwrTitle}" style="cursor:pointer;">${pwrCtrl}</td>
      <td class="net-col-decap">${decap}</td>
      <td class="net-col-protect">
        <label class="chk"><input type="checkbox" class="net-protect-cb" ${net.protected ? 'checked' : ''} data-net="${safeName}" title="Protected nets are routed first and never ripped up"></label>
      </td>
    </tr>
  `;
}

function renderNetsTable(nets) {
  const tbody = el("nets-tbody");
  if (!tbody) return;
  
  tbody.innerHTML = nets.map(netRowHtml).join("");
  
  // Add click handlers for checkboxes (in-place highlight, no re-render)
  tbody.querySelectorAll('input.net-col-select[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", e => {
      const net = e.target.dataset.net;
      if (e.target.checked) {
        selectedNets.add(net);
      } else {
        selectedNets.delete(net);
      }
      syncNetRowSelection();
      zoomToNets();
    });
  });
  
  // Add click handlers for protected checkboxes
  tbody.querySelectorAll('input.net-protect-cb[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", async e => {
      const net = e.target.dataset.net;
      const protectedFlag = e.target.checked;
      try {
        await postJSON("/api/nets/protected", { net, protected: protectedFlag });
        // Re-fetch to update table
        await fetchNets();
      } catch (err) {
        toast(`protect toggle failed: ${err.message}`);
        // Revert checkbox on error
        await fetchNets();
      }
    });
  });

  // Power membership toggles: click the Pwr cell to mark/unmark a net as
  // a power rail (wide tracks, route-first, placement magnetism). Badges,
  // widths and decap marks refresh via fetchNets.
  tbody.querySelectorAll('td[data-power-cell]').forEach(td => {
    td.addEventListener("click", async e => {
      if (e.target.type === "checkbox") return;
      const net = td.dataset.powerCell;
      const row = lastNetsByName.get(net);
      const markPower = !(row && row.is_power);
      try {
        await postJSON("/api/nets/power", { net, power: markPower });
        await fetchNets();
      } catch (err) {
        toast(`power toggle failed: ${err.message}`);
        await fetchNets();
      }
    });
  });
  
  // Row click to select/deselect (in-place: never re-render the table here —
  // renderNetsTable replaces tbody, so a synthetic subset would "vanish" rows).
  tbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", e => {
      if (e.target.type === "checkbox") return;
      if (e.target.closest("td[data-power-cell]")) return;
      const net = tr.dataset.net;
      if (!net) return;

      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        toggleNetSelection(net);
      } else {
        selectedNets.clear();
        selectedNets.add(net);
      }
      syncNetRowSelection();
      zoomToNets();
    });
  });
  
  // Update "select all" checkbox
  syncSelectAll("nets-select-all", nets, n => selectedNets.has(n.name));
  // Counts must match the selection after every (full-data) render.
  updateNetSelectionUI();
}

function toggleNetSelection(net) {
  if (selectedNets.has(net)) {
    selectedNets.delete(net);
  } else {
    selectedNets.add(net);
  }
}

// Single place that mirrors selectedNets into the rendered rows + header
// checkbox + action-button counts. Never re-renders the table (full data
// only comes from fetchNets), so the list can't "vanish" on click.
function syncNetRowSelection() {
  document.querySelectorAll("#nets-tbody tr").forEach(tr => {
    const n = tr.dataset.net;
    if (!n) return;
    const on = selectedNets.has(n);
    const cb = tr.querySelector("input.net-col-select");
    if (cb) cb.checked = on;
    tr.classList.toggle("net-selected-row", on);
  });
  const selectAllCb = el("nets-select-all");
  if (selectAllCb) {
    const rows = [...document.querySelectorAll("#nets-tbody tr")].map(tr => tr.dataset.net).filter(Boolean);
    selectAllCb.checked = rows.length > 0 && rows.every(n => selectedNets.has(n));
    selectAllCb.indeterminate = rows.some(n => selectedNets.has(n)) && !rows.every(n => selectedNets.has(n));
  }
  updateNetSelectionUI();
}

function updateNetSelectionUI() {
  const count = selectedNets.size;
  const routeBtn = el("route-selected-btn");
  if (routeBtn) {
    routeBtn.disabled = count === 0;
    routeBtn.textContent = count > 0 ? `Route ${count} Selected Net${count > 1 ? 's' : ''}` : "Route Selected Nets";
  }
  const ripBtn = el("rip-selected-btn");
  if (ripBtn) {
    ripBtn.disabled = count === 0;
    ripBtn.textContent = count > 0 ? `Rip Selected (${count})` : "Rip Selected Nets";
  }
}

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
on("gnd-islands-btn", "click", fetchGndIslands);

// GND pour continuity overlay (Phase 1 diagnostics): bbox per island,
// red dashed = dead (no pad/via connection, cf. KiCad isolated-fill DRC).
let gndIslandsData = null;
let gndIslandsVisible = false;

function renderGndIslandsOverlay() {
  makeSvgOverlay("gnd-island-overlay", gndIslandsVisible, gndIslandsData, g => {
    // NOTE: coordinates are board-mm SVG user units (same as every other
    // server-rendered element via the viewBox), so they place directly
    // as in-SVG rects with no pixel conversion.
    for (const [layer, items] of Object.entries(gndIslandsData.layers)) {
      for (const isl of items) {
        const [x0, y0, x1, y1] = isl.bbox_mm;
        const c = isl.connected ? OVERLAY_COLORS.pass : OVERLAY_COLORS.fail;
        const rect = svgEl("rect", {
          x: Math.min(x0, x1), y: Math.min(y0, y1),
          width: Math.abs(x1 - x0), height: Math.abs(y1 - y0),
          fill: c, "fill-opacity": "0.12", stroke: c,
          "stroke-width": isl.connected ? "0.15" : "0.3",
          ...(!isl.connected ? { "stroke-dasharray": "1 0.6" } : {}),
        });
        rect.appendChild(svgTitle(
          `${isl.net} ${layer}: ${isl.area_mm2}mm² ${isl.connected ? "fed" : "DEAD"}`));
        g.appendChild(rect);
        g.appendChild(svgEl("circle", { cx: isl.cx_mm, cy: isl.cy_mm, r: "0.5", fill: c }));
      }
    }
  });
}

async function fetchGndIslands() {
  await runDiagToggle({
    btnId: "gnd-islands-btn", boxId: "gnd-islands-results",
    offLabel: "GND Islands", onLabel: "Hide Islands",
    isOn: () => gndIslandsVisible, setOn: v => { gndIslandsVisible = v; },
    render: renderGndIslandsOverlay, failPrefix: "island analysis failed",
    load: async () => {
      const data = await apiJson("/api/routing/debug/gnd-islands");
      gndIslandsData = data;
      const parts = [];
      for (const [layer, items] of Object.entries(gndIslandsData.layers)) {
        const dead = items.filter(i => !i.connected).length;
        parts.push(`${layer}: ${items.length} island${items.length === 1 ? "" : "s"}${dead ? ` (${dead} DEAD)` : ""}`);
      }
      return parts.join(" · ") || "no zoned ground pours";
    },
  });
}

async function refreshInletsDiag() {
  const diag = inletsDiag;
  try {
    const data = await getInlets();
    const gl = data.gnd_list || data.gnd;
    const g = (Array.isArray(gl) ? gl : (gl ? [gl] : [])).map(a => `${a.ref}.${a.pad}`).join(", ") || "unset";
    const p = Object.entries(data.power || {}).map(([n, a]) => {
      const lst = Array.isArray(a) ? a : [a];
      return `${n}@${lst.map(x => `${x.ref}.${x.pad}`).join("+")}`;
    }).join(", ") || "unset";
    const b = (data.bridges || []).map(x => `${x.a.ref}.${x.a.pad}↔${x.b.ref}.${x.b.pad}`).join(", ");
    if (diag) diag.textContent = `GND inlet ${g} · PWR ${p}${b ? ` · bridges ${b}` : ""}`;
  } catch (err) {
    if (diag) diag.textContent = `inlets unreadable: ${err}`;
  }
}

async function saveInlets() {
  const diag = inletsDiag;
  const gRef = (el("inlet-gnd-ref")?.value ?? "").trim();
  const gPad = (el("inlet-gnd-pad")?.value ?? "").trim();
  const pNet = (el("inlet-pwr-net")?.value ?? "").trim();
  const pRef = (el("inlet-pwr-ref")?.value ?? "").trim();
  const pPad = (el("inlet-pwr-pad")?.value ?? "").trim();
  const body = { power: {} };
  if (gRef && gPad) body.gnd = { ref: gRef, pad: gPad };
  if (pNet && pRef && pPad) body.power[pNet] = { ref: pRef, pad: pPad };
  try {
    await setInlets(body);
    await refreshInletsDiag();
  } catch (err) {
    if (diag) diag.textContent = `inlet save failed: ${err}`;
  }
}

async function clearGndInlet() {
  const diag = inletsDiag;
  try {
    await setInlets({ gnd: null });
    await refreshInletsDiag();
  } catch (err) {
    if (diag) diag.textContent = `inlet clear failed: ${err}`;
  }
}

async function fetchImpedance() {
  await runDiagToggle({
    btnId: "impedance-btn", boxId: "impedance-results",
    offLabel: "Impedance", onLabel: "Hide Z",
    isOn: () => impedanceVisible, setOn: v => { impedanceVisible = v; },
    render: renderImpedanceOverlay, failPrefix: "impedance analysis failed",
    load: async () => {
      // The results live inside the collapsed section: open it so the
      // report is visible instead of filling a hidden box.
      const sec = el("impedance-details");
      if (sec) sec.open = true;
      // Fall back to each input's own default so empty fields can't silently
      // tighten the test to a stale hardcoded value.
      const gEl = el("imp-gnd-max");
      const pEl = el("imp-pwr-max");
      const gMax = gEl.value || gEl.defaultValue || "50";
      const pMax = pEl.value || pEl.defaultValue || "250";
      impedanceData = await apiJson(`/api/routing/debug/impedance?gnd_max_mohm=${encodeURIComponent(gMax)}&power_max_mohm=${encodeURIComponent(pMax)}`);
      renderImpedanceResults(); // unhides the box itself; no text summary
    },
  });
}

// Pad lists render worst-first, capped per net with a show-all toggle.
// The full data is already client-side, so toggling never refetches.
const impedanceExpanded = new Set();
function renderImpedanceResults() {
  const box = el("impedance-results");
  const data = impedanceData;
  if (!data || !box) return;
  const verdictColor = v => v === "pass" ? "#2ea87a" : (v === "attention" ? "#d9a13b" : "#e06c75");
  const s = data.summary || {};
  const br = data.bridges || {};
  let html = `<div>nets ${s.nets_evaluated ?? 0} · pads ${s.pads_evaluated ?? 0} · ` +
    `<b style="color:#e06c75;">fail ${s.failures ?? 0}</b> · ` +
    `<span style="color:#d9a13b;">attention ${s.attention ?? 0}</span>` +
    `<span style="color:#7d8b99;"> · bridges explicit ${(br.explicit || []).length} · auto ${br.auto ?? 0} · cross-net ${br.cross_net ?? 0}</span></div>`;
  for (const net of (data.nets || [])) {
    const inl = (net.inlets || []).map(i => `${escapeHtml(i.ref)}.${escapeHtml(i.pad)}`).join(", ") || "no-inlet";
    html += `<div style="margin-top:.35rem;"><b>${escapeHtml(net.net)}</b> <span style="color:#7d8b99;">inlet ${inl} · worst ${net.worst_mohm ?? "—"} mΩ · fail ${net.failures} · attn ${net.attention}</span>`;
    const expanded = impedanceExpanded.has(net.net);
    const pads = expanded ? (net.pads || []) : (net.pads || []).slice(0, 12);
    const rows = pads.map(p =>
      `<tr><td>${escapeHtml(p.ref)}.${escapeHtml(p.pad)}</td><td>${p.connected ? (p.r_mohm ?? "—") : "—"}</td>` +
      `<td>vias ${p.vias_used} · paths ${p.disjoint_paths}${p.bridges_used ? ` · br ${p.bridges_used}` : ""}</td>` +
      `<td style="color:${verdictColor(p.verdict)};">${escapeHtml(p.verdict)}${p.reason ? ` (${escapeHtml(p.reason)})` : ""}</td></tr>`
    ).join("");
    if (rows) html += `<table style="margin-top:.2rem;border-collapse:collapse;"><tbody>${rows}</tbody></table>`;
    const hidden = (net.pads || []).length - Math.min(12, (net.pads || []).length);
    if (hidden > 0) {
      html += `<div><button class="imp-expand-btn" data-net="${escapeHtml(net.net)}" style="font-size:.72rem;color:#7d8b99;background:none;border:none;cursor:pointer;padding:0;">` +
        (expanded ? "show less ▲" : `… ${hidden} more pads (worst first) ▼`) + `</button></div>`;
    }
    html += `</div>`;
  }
  box.hidden = false;
  box.innerHTML = html;
  box.querySelectorAll(".imp-expand-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const net = btn.dataset.net;
      if (impedanceExpanded.has(net)) impedanceExpanded.delete(net);
      else impedanceExpanded.add(net);
      renderImpedanceResults();
    });
  });
}

// Impedance pad overlay: one dot per evaluated pad, board-mm SVG user
// units like the islands overlay. Green = pass (redundant low-R path),
// amber = attention (single path), red = fail. Inlet pads get a ring.
let impedanceData = null;
let impedanceVisible = false;
function renderImpedanceOverlay() {
  makeSvgOverlay("impedance-overlay", impedanceVisible, impedanceData, g => {
    const color = v => OVERLAY_COLORS[v] || OVERLAY_COLORS.fail;
    for (const net of (impedanceData.nets || [])) {
      for (const p of (net.pads || [])) {
        if (p.x_mm === undefined) continue;
        const inlet = p.reason === "inlet";
        const dot = svgEl("circle", {
          cx: p.x_mm, cy: p.y_mm, r: inlet ? "1.0" : "0.6",
          fill: color(p.verdict), "fill-opacity": inlet ? "0.9" : "0.75",
          ...(inlet ? { stroke: "#ffffff", "stroke-width": "0.25" } : {}),
        });
        dot.appendChild(svgTitle(
          `${p.ref}.${p.pad} ${p.net}: ${p.connected ? p.r_mohm + " mΩ" : p.reason} via ${p.inlet || "—"}`));
        g.appendChild(dot);
      }
    }
  });
}
on("impedance-btn", "click", fetchImpedance);
on("inlets-save-btn", "click", saveInlets);
on("inlets-clear-btn", "click", clearGndInlet);

// ---- Bridges: mated inter-panel pairs (pick two pins + link) ----
let bridgePick = null; // 'a' | 'b' | null
function setBridgePick(which) {
  bridgePick = (bridgePick === which) ? null : which;
  const pa = el("bridge-pick-a");
  const pb = el("bridge-pick-b");
  if (pa) pa.textContent = bridgePick === "a" ? "Picking…" : "Pick A";
  if (pb) pb.textContent = bridgePick === "b" ? "Picking…" : "Pick B";
}
// Capture-phase: claim pad clicks while picking, before footprint selection.
viewport?.addEventListener("click", e => {
  if (!bridgePick) return;
  const padEl = e.target.closest("[data-pad]");
  if (!padEl) return;
  const ref = padEl.getAttribute("data-ref") || "";
  const pad = padEl.getAttribute("data-pad") || "";
  if (!ref || !pad) return;
  const _br = el(`bridge-${bridgePick}-ref`); if (_br) _br.value = ref;
  const _bp = el(`bridge-${bridgePick}-pad`); if (_bp) _bp.value = pad;
  setBridgePick(bridgePick); // disarm (toggles off)
  e.stopPropagation();
  e.preventDefault();
}, true);

async function postBridges(bridges) {
  const diag = bridgesDiag;
  try {
    await setInlets({ bridges });
    await refreshBridges();
    await refreshInletsDiag();
  } catch (err) {
    if (diag) diag.textContent = `bridge update failed: ${err}`;
  }
}

async function refreshBridges() {
  const box = el("bridges-list");
  if (!box) return;
  const diag = bridgesDiag;
  try {
    const data = await getInlets();
    const bridges = data.bridges || [];
    if (!bridges.length) {
      box.hidden = true;
      box.innerHTML = "";
      if (diag) diag.textContent = "no bridges";
      return;
    }
    box.hidden = false;
    box.innerHTML = bridges.map((b, i) =>
      `<span style="margin-right:.6rem;">${escapeHtml(b.a.ref)}.${escapeHtml(b.a.pad)}↔${escapeHtml(b.b.ref)}.${escapeHtml(b.b.pad)} (${b.r_mohm} mΩ)` +
      `<button data-bridge-del="${i}" title="Remove this bridge" style="margin-left:.2rem;">✕</button></span>`
    ).join("");
    box.querySelectorAll("[data-bridge-del]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const cur = (await getInlets()).bridges || [];
        cur.splice(Number(btn.dataset.bridgeDel), 1);
        await postBridges(cur);
      });
    });
    if (diag) diag.textContent = `${bridges.length} bridge${bridges.length === 1 ? "" : "s"}`;
  } catch (err) {
    if (diag) diag.textContent = `bridges unreadable: ${err}`;
  }
}

async function linkBridge() {
  const diag = bridgesDiag;
  const aRef = (el("bridge-a-ref")?.value ?? "").trim();
  const aPad = (el("bridge-a-pad")?.value ?? "").trim();
  const bRef = (el("bridge-b-ref")?.value ?? "").trim();
  const bPad = (el("bridge-b-pad")?.value ?? "").trim();
  const r = parseFloat(el("bridge-r")?.value) || 20.0; // BRIDGE_DEFAULT_MOHM
  if (!aRef || !aPad || !bRef || !bPad) {
    if (diag) diag.textContent = "fill A ref/pad and B ref/pad (or Pick from the board)";
    return;
  }
  const cur = (await getInlets()).bridges || [];
  cur.push({ a: { ref: aRef, pad: aPad }, b: { ref: bRef, pad: bPad }, r_mohm: r });
  await postBridges(cur);
}

async function suggestBridges() {
  const box = el("bridge-suggest-list");
  const diag = bridgesDiag;
  const sec = el("bridges-details");
  if (sec) sec.open = true;
  try {
    const data = await apiJson("/api/nets/bridge-suggestions");
    const mates = data.mates || [];
    if (!mates.length) {
      box.hidden = false;
      box.textContent = "no header mates found (need J-headers at matching panel positions)";
      return;
    }
    const same = mates.filter(m => m.match);
    const diff = mates.filter(m => !m.match);
    const all = [...same.map(m => ({ m, pre: true })), ...diff.map(m => ({ m, pre: false }))];
    box.hidden = false;
    box.innerHTML =
      `<div>${same.length} same-net pairs · ` +
      `<span style="color:#d9a13b;">${diff.length} different-net pairs (names differ by design to fool KiCad DRC — tick to link as intended)</span> ` +
      `<button id="bridge-apply-btn">Apply checked</button></div>` +
      all.map(({ m, pre }, i) =>
        `<label style="margin-right:.6rem;" title="${escapeHtml(m.net_a || "—")} ↔ ${escapeHtml(m.net_b || "—")}">` +
        `<input type="checkbox" class="bridge-sug" data-i="${i}"${pre ? " checked" : ""}> ` +
        `${escapeHtml(m.a.ref)}.${escapeHtml(m.a.pad)}↔${escapeHtml(m.b.ref)}.${escapeHtml(m.b.pad)} ` +
        `<span style="color:#7d8b99;">${m.match ? escapeHtml(m.net_a) : `${escapeHtml(m.net_a || "—")}↔${escapeHtml(m.net_b || "—")}`}</span></label>`
      ).join("");
    on("bridge-apply-btn", "click", async () => {
      const checked = [...box.querySelectorAll(".bridge-sug:checked")].map(cb => all[Number(cb.dataset.i)].m);
      if (!checked.length) return;
      const cur = (await getInlets()).bridges || [];
      for (const m of checked) {
        const b = { a: m.a, b: m.b, r_mohm: 20.0 }; // BRIDGE_DEFAULT_MOHM
        if (!cur.some(c => JSON.stringify(c) === JSON.stringify(b))) cur.push(b);
      }
      await postBridges(cur);
      box.hidden = true;
    });
  } catch (err) {
    box.hidden = false;
    box.textContent = `suggestions failed: ${err}`;
  }
}
on("bridge-pick-a", "click", () => setBridgePick("a"));
on("bridge-pick-b", "click", () => setBridgePick("b"));
on("bridge-link-btn", "click", linkBridge);
on("bridge-suggest-btn", "click", suggestBridges);
refreshBridges();
refreshInletsDiag();

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

// Select all checkbox in nets table (in-place: full list stays rendered)
on("nets-select-all", "change", e => {
  const rows = document.querySelectorAll("#nets-tbody tr");
  if (e.target.checked) {
    rows.forEach(tr => {
      const net = tr.dataset.net;
      if (net) selectedNets.add(net);
    });
  } else {
    rows.forEach(tr => {
      const net = tr.dataset.net;
      if (net) selectedNets.delete(net);
    });
  }
  syncNetRowSelection();
});

// Protect all checkbox
on("nets-protect-all", "change", async e => {
  const rows = document.querySelectorAll("#nets-tbody tr");
  for (const tr of rows) {
    const net = tr.dataset.net;
    const cb = tr.querySelector(".net-protect-cb");
    if (cb && cb.checked !== e.target.checked) {
      cb.checked = e.target.checked;
      try {
        await postJSON("/api/nets/protected", { net, protected: e.target.checked });
      } catch (err) {
        toast(`protect toggle failed: ${err.message}`);
      }
    }
  }
  await fetchNets();
});

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


// Shared horizontal-resizer scaffold: drag to resize `panel` within
// [minW, maxW], persisted under `storageKey`. `sign` is +1 when dragging
// right widens (left sidebar) and -1 when dragging right narrows (right
// drawer); `onDown` runs first (e.g. expand a collapsed panel).
// Saves parseInt px so a reload restores "<n>px" (previously the sidebar
// saved the "px"-suffixed string and restored "<n>pxpx", i.e. never).
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
