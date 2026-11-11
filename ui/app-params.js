// app-params.js — placement settings panel, presets, URL presets.
// Needs core (el/api/toast) at load; placement reads params at runtime.
//
// Calibrated behavior knobs get main-panel sliders; power/decap/ghost,
// alignment, and the convergence threshold live under Advanced. Everything
// else (repulsion, solver internals, mass fallbacks) stays API-only at
// calibrated defaults — POST /api/placement/params merges, so hidden keys
// in placement.json survive GUI saves.
// Ranges cover Cluster (scattered starts), Legalize (placed boards), and
// Tidy (Manhattan snap + weak hold) regimes.

const PARAM_DEFS = [
  { key: "attraction_k_N", label: "Attraction k (N)", min: 1e-4, max: 2e-1, step: 1e-4, log: true },
  { key: "wall_k_N", label: "Wall stiffness k (N)", min: 1e-5, max: 1, step: 1e-5, log: true },
  // display: UI shows millimeters, engine/API stay SI (see toDisplay/fromDisplay).
  { key: "wall_halo_m", label: "Wall halo (mm)", min: 0, max: 0.015, step: 0.0005,
    display: { unit: "mm", scale: 1000, step: 0.5 } },
  { key: "damping_Ns_m", label: "Damping (N·s/m)", min: 1e-3, max: 1, step: 1e-4, log: true },
  { key: "align_angle_k_N", label: "Align angle k (N)", min: 1e-6, max: 1e-2, step: 1e-6, log: true },
  { key: "align_pos_k_N_m", label: "Align hold k (N/m)", min: 1e-3, max: 10, step: 1e-3, log: true },
  { key: "max_iterations", label: "Max iterations", min: 1, max: 20000, step: 10 },
  { key: "label_clearance_m", label: "Label clearance (mm)", min: 0, max: 20, step: 0.1,
    display: { unit: "mm", scale: 1000, step: 0.1 } },
  { key: "label_orbit_m", label: "Label orbit (mm)", min: 0, max: 20, step: 0.1,
    display: { unit: "mm", scale: 1000, step: 0.1 } },
];

const ADVANCED_PARAM_DEFS = [
  { key: "power_attraction_scale", label: "Power attraction", min: 0, max: 10, step: 0.05 },
  { key: "decap_link", label: "Decap\u2013chip link", min: 0, max: 16, step: 0.5 },
  { key: "ghost_k_N", label: "Ghost pull (N)", min: 1e-7, max: 1e-3, step: 1e-7, log: true },
  { key: "wall_exponent", label: "Wall exponent", min: 1.0, max: 3.0, step: 0.1 },
  { key: "cross_layer_pin_halo_m", label: "X-layer pin halo (mm)", min: 0, max: 5, step: 0.05,
    display: { unit: "mm", scale: 1000, step: 0.05 } },
  { key: "momentum_alpha", label: "Momentum alpha", min: 0.0, max: 1.0, step: 0.05 },
  { key: "contact_tangent_gamma", label: "Contact tangent γ", min: 0.0, max: 0.5, step: 0.01 },
  { key: "convergence_eps_m", label: "Convergence eps (mm)", min: 1e-7, max: 1e-3, step: 1e-7, log: true,
    display: { unit: "mm", scale: 1000, step: 0.0001 } },
  { key: "align_deadband_deg", label: "Align deadband (deg)", min: 0, max: 45, step: 1 },
  { key: "align_radius_m", label: "Align radius (mm)", min: 0.001, max: 0.05, step: 0.001,
    display: { unit: "mm", scale: 1000, step: 1 } },
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
  label_clearance_m: "Label clearance around the estimated text bbox. Shown in mm; stored in meters. The clearance rect collides via the wall law. Larger values give labels more board space.",
  label_orbit_m: "Label orbit radius around the parent part. Shown in mm; stored in meters. Orbit always on: each label is a low-mass node on a spring tether that homes on its parent and finds spare room inside this disc.",
  power_attraction_scale: "Power-rail attraction trim (fanout-scaled k/(fanout-1) per edge). 1 = full participation; 0 restores the old exclude-power behavior.",
  decap_link: "Weight of the supplemental symmetric pull between a small cap and its nearest chip (<=15mm, U* only); global rail pull is never boosted. 0 removes decap GND edges and disables local links.",
  ghost_k_N: "Ghost attractor spring scale (newtons); file ka values weight it. Log scale. Only matters with ghost attractors.",
  wall_exponent: "Exponent p in wall force F = k*(1-s/h)^p. 1 = linear, 2 = quadratic (stiffens deep penetration). Legalize: 2.0; Cluster: 1.0-1.5.",
  cross_layer_pin_halo_m: "Near-miss reach for THT pins protruding onto the far side vs opposite-side bodies. Shown in mm; stored in meters. Solder-fillet clearance (~0.7mm default), not the 4-6mm courtyard halo. 0 disables cross-layer pin response.",
  momentum_alpha: "Velocity blending alpha after rigid projection: 0 = re-derive from displacement, 1 = keep old velocity. Higher = more momentum persistence to nudge past wedges. Default 0.7.",
  contact_tangent_gamma: "Tangential assist coefficient γ: fraction of normal force applied along the tangential axis at contact. Helps parts slide along walls instead of wedging. 0 = disabled, 0.1-0.3 typical. Default 0.15.",
  convergence_eps_m: "Stop when max pad displacement per step falls below this. Shown in mm; stored in meters. Calibrated 20µm (si_force_laws.md): smaller fake-settles under contact chatter, larger stops earlier. Log scale.",
  vmax_m_s: "Speed ceiling for pad velocities and rigid-step clamp (m/s). Higher allows faster movement but less stable. Default 1.0 m/s.",
  dt_s: "Simulation timestep: 0.2-2ms, bounded by the dt_min/dt_max solver guards (wider values fail validation). Adaptive guard halves on energy rise, regrows on fall. Default 1ms.",
  friction_N: "Dry-friction deadband (newtons). Whole footprint freezes when all pad forces below this. Default 1e-6 N.",
  align_angle_k_N: "Manhattan-snap stiffness: constant-magnitude tangential couple (N), signed toward the nearest compass mode (0/90). Outliers rotate at a steady rate until inside the deadband; parts on a mode feel nothing. 0 = off. Tidy: 3e-3. Log scale.",
  align_pos_k_N_m: "Weak position leash (N/m) toward the pose held at the last align recompute — or the frozen row baseline/pitch frame for row-engaged parts — so rotating bodies settle instead of drifting. Anchors refresh as the board moves; wall pressure still ejects. 0 = off (also disables rows). Tidy: 0.5. Log scale.",
  align_deadband_deg: "Consensus error below this (degrees) is not an outlier: no torque. Diagonals and neat neighborhoods are never churned. Tidy: 10.",
  align_radius_m: "Neighbor search radius for mode agreement. Shown in mm; stored in meters. Only the 6 nearest parts inside count as neighbors. Tidy: 20mm.",
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
  // Ancestor title: hovering the slider or number box shows the same
  // description as the label (the label <span> title alone does not cover
  // its sibling inputs).
  if (desc) el.title = desc;
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
// AND legalizes, so offer the calibrated starting points. dt_s, vmax,
// friction ride along via the merge-POST even though they have no main-panel sliders.
// Calibrated values: Cluster uses a 6mm halo; Legalize uses 4mm so dense
// neighbor fields do not overpower a part's own wall. wall_exponent=2.0 and
// convergence_eps_m=2e-5 ride along via merge-POST. All three pin
// cross_layer_pin_halo_m=0.7mm so a stale saved value cannot silently
// diverge from calibration (far-side solder clearance is regime-independent).
// Cluster/Legalize pin both align stiffnesses at 0 (off); Tidy is a calm
// short polish pass: low attraction (no restructuring), firm walls,
// higher friction so only strong forces move parts, plus weak orientation
// snap with a weak hold. Angle k sits just above the viscous stall
// floor (~2.4 mN at legalize damping); the mode-agreement gate (half the
// neighbors must already sit near the mode) keeps lone parts from
// straightening against their thread mid-churn. Row hold reuses the leash
// spring with frozen baseline/pitch anchors (median edge, median pitch,
// deadband-gated, released past the radius). Short budget: angles
// converge in ~60-100 iterations; longer runs just churn positions.
// Starting points pending a calibration sweep (see roadmap).
const REGIME_PRESETS = {
  cluster: { attraction_k_N: 0.05, wall_k_N: 0.025, damping_Ns_m: 0.08, dt_s: 2e-3, vmax_m_s: 1.0, friction_N: 1e-6, wall_halo_m: 0.006, cross_layer_pin_halo_m: 0.0007, wall_exponent: 1.0, convergence_eps_m: 2e-5, align_angle_k_N: 0, align_pos_k_N_m: 0, align_deadband_deg: 10, align_radius_m: 0.020 },
  legalize: { attraction_k_N: 0.008, wall_k_N: 0.1, damping_Ns_m: 0.12, dt_s: 1e-3, vmax_m_s: 1.0, friction_N: 1e-6, wall_halo_m: 0.004, cross_layer_pin_halo_m: 0.0007, wall_exponent: 2.0, convergence_eps_m: 2e-5, align_angle_k_N: 0, align_pos_k_N_m: 0, align_deadband_deg: 10, align_radius_m: 0.020 },
  tidy: { attraction_k_N: 0.002, wall_k_N: 0.1, damping_Ns_m: 0.12, dt_s: 1e-3, vmax_m_s: 1.0, friction_N: 1e-5, wall_halo_m: 0.004, cross_layer_pin_halo_m: 0.0007, wall_exponent: 2.0, convergence_eps_m: 2e-5, align_angle_k_N: 3e-3, align_pos_k_N_m: 0.5, align_deadband_deg: 10, align_radius_m: 0.020, max_iterations: 150 },
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

on("regime-tidy-btn", "click", async () => {
  try { await applyRegimePreset("tidy"); }
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
