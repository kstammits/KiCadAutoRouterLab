// app-tidy.js — Tidy work-tab: geometric neat pass after force-spring placement.
//
// Owns two PlacementParams sliders (refine_strength 0.05..1, refine_radius_m
// shown in mm) plus Subtle/Default/Strong presets, and the Run Tidy action
// (POST /api/placement/refine). Run is always enabled; a strength at or
// below 0 shows an inline hint instead of disabling the button. The result
// merges into the Place proposal — review and Accept there.
// Needs core (el/api/toast) at load; placement panel globals (poll,
// illegalUuids, lastLegality, renderFootprintTable, acceptBtn,
// setActivePanel, selectedUuids) at runtime.

const TIDY_PARAM_DEFS = [
  { key: "refine_strength", label: "Refine strength", min: 0.05, max: 1, step: 0.05,
    desc: "How far parts blend toward their neighbors' angles and gaps. Subtle 0.3, Default 0.6, Strong 1.0." },
  { key: "refine_radius_m", label: "Neighbor radius (mm)", min: 5, max: 50, step: 1,
    display: { unit: "mm", scale: 1000, step: 1 },
    desc: "Neighbor search radius. Larger reaches more parts; smaller only touches the closest row. Only the 6 nearest parts inside count." },
];

// Display default for a fresh board (engine default stays 0.0 = off;
// the UI just never suggests the off state).
const TIDY_DISPLAY_DEFAULTS = { refine_strength: 0.6, refine_radius_m: 0.020 };

function tidyToDisplay(val, def) {
  return def.display ? val * def.display.scale : val;
}
function tidyFromDisplay(val, def) {
  return def.display ? val / def.display.scale : val;
}

function tidySetStrength(v) {
  const grid = el("tidy-params-grid");
  if (!grid) return;
  const range = grid.querySelector('input[type=range][data-key="refine_strength"]');
  const num = grid.querySelector('input[type=number][data-key="refine_strength"]');
  if (range) range.value = v;
  if (num) num.value = v;
  syncTidyRunState();
}

function buildTidyParams() {
  const grid = el("tidy-params-grid");
  if (!grid || grid.dataset.built) return;
  grid.dataset.built = "1";
  for (const def of TIDY_PARAM_DEFS) {
    const wrap = document.createElement("div");
    wrap.className = "param";
    // Ancestor title covers slider + number box (the label <span> title
    // alone does not cover its sibling inputs).
    if (def.desc) wrap.title = def.desc;
    const dmin = tidyToDisplay(def.min, def);
    const dmax = tidyToDisplay(def.max, def);
    const dstep = def.display?.step ?? def.step;
    wrap.innerHTML = `<span title="${def.desc}">${def.label}</span>
      <div class="row">
        <input type="range" data-key="${def.key}" min="${dmin}" max="${dmax}" step="${dstep}">
        <input type="number" data-key="${def.key}" min="${dmin}" max="${dmax}" step="${dstep}">
      </div>`;
    grid.appendChild(wrap);
  }
  grid.addEventListener("input", (e) => {
    const key = e.target.dataset.key;
    if (!key) return;
    const range = grid.querySelector(`input[type=range][data-key="${key}"]`);
    const num = grid.querySelector(`input[type=number][data-key="${key}"]`);
    if (!range || !num) return;
    if (e.target.type === "range") num.value = range.value;
    else range.value = num.value;
    if (key === "refine_strength") syncTidyRunState();
  });
  el("tidy-presets-bar")?.addEventListener("click", (e) => {
    const v = e.target?.dataset?.tidyPreset;
    if (v !== undefined && v !== "") tidySetStrength(v);
  });
}

function readTidyParams() {
  const grid = el("tidy-params-grid");
  const out = {};
  for (const def of TIDY_PARAM_DEFS) {
    const num = grid?.querySelector(`input[type=number][data-key="${def.key}"]`);
    if (num) out[def.key] = tidyFromDisplay(Number(num.value), def);
  }
  return out;
}

async function loadTidyParams() {
  buildTidyParams();
  const grid = el("tidy-params-grid");
  let saved = {};
  try {
    saved = await apiJson("/api/placement/params");
  } catch { /* params panel owns the error surface */ }
  for (const def of TIDY_PARAM_DEFS) {
    let raw = saved[def.key] ?? TIDY_DISPLAY_DEFAULTS[def.key] ?? def.min;
    // A saved engine 0.0 means "never run"; the tab opens on a runnable
    // Default instead so Run works first click (engine default untouched
    // until the user runs and persists).
    if (def.key === "refine_strength" && !(raw > 0)) raw = TIDY_DISPLAY_DEFAULTS.refine_strength;
    const shown = tidyToDisplay(raw, def);
    const lo = tidyToDisplay(def.min, def);
    const hi = tidyToDisplay(def.max, def);
    const range = grid.querySelector(`input[type=range][data-key="${def.key}"]`);
    const num = grid.querySelector(`input[type=number][data-key="${def.key}"]`);
    if (range) range.value = Math.min(Math.max(shown, lo), hi);
    if (num) num.value = Math.min(Math.max(shown, lo), hi);
  }
  syncTidyRunState();
  syncTidyScope();
  refreshTidyMetrics();
}

function syncTidyRunState(hint) {
  const btn = el("tidy-run-btn");
  const diag = el("tidy-diag");
  if (!btn) return;
  // Run stays enabled so the action is discoverable; a non-positive
  // strength is an inline validation message, not a disabled button.
  btn.disabled = false;
  btn.title = "Straighten angles and even out gaps. Never adds overlaps; the result lands in the Place proposal for review.";
  const { refine_strength } = readTidyParams();
  if (!(refine_strength > 0) && diag) {
    diag.textContent = hint || "Pick a strength above 0 — try Default 0.6.";
  }
}

function syncTidyScope() {
  const scope = el("tidy-scope");
  if (!scope) return;
  const n = (typeof selectedUuids !== "undefined" && selectedUuids) ? selectedUuids.size : 0;
  scope.textContent = n > 0
    ? `${n} selected — only these move; the whole board stays alignment context.`
    : "No selection — all movable parts move; the whole board stays alignment context.";
}

// Overall board metrics for the Tidy tab: routed-net coverage, copper
// totals from /api/nets, and the cached DRC state. Best-effort: any fetch
// failure leaves a one-line reason instead of breaking the tab.
// `rolls` is the per-roll log from the last shuffle run (kept across
// refreshes so reopening the tab doesn't lose it).
let lastShuffleRolls = [];
async function refreshTidyMetrics(rolls) {
  if (Array.isArray(rolls)) lastShuffleRolls = rolls;
  const box = el("tidy-metrics");
  if (!box) return;
  if (typeof lastVersion === "undefined" || !lastVersion) {
    box.textContent = "no board loaded";
    return;
  }
  let nets;
  try {
    const data = await apiJson("/api/nets");
    nets = data.nets || [];
  } catch (err) {
    box.textContent = `metrics unavailable: ${err.message}`;
    return;
  }
  let drc = null;
  try {
    drc = await apiJson("/api/drc/status");
  } catch { drc = null; }
  const copper = (n) => (n.track_count || 0) + (n.arc_count || 0) + (n.via_count || 0);
  const routable = nets.filter((n) => (n.pad_count || 0) >= 2);
  const routed = routable.filter((n) => copper(n) > 0).length;
  const tracks = nets.reduce((s, n) => s + (n.track_count || 0), 0);
  const arcs = nets.reduce((s, n) => s + (n.arc_count || 0), 0);
  const vias = nets.reduce((s, n) => s + (n.via_count || 0), 0);
  const prot = nets.filter((n) => n.protected).length;
  let drcTxt;
  if (!drc) drcTxt = "DRC unknown";
  else if (drc.running) drcTxt = "DRC running…";
  else if (drc.error) drcTxt = `DRC error: ${drc.error}`;
  else if (drc.for_version == null) drcTxt = "DRC never run";
  else drcTxt = `DRC ${drc.stale ? "stale" : "cached"}: ${drc.violations} violations, ${drc.unconnected} unconnected`;
  let txt = `Board v${lastVersion} · nets ${routed}/${routable.length} routed` +
    ` · ${routable.length - routed} unrouted · ${prot} protected` +
    ` · copper ${tracks} tracks, ${arcs} arcs, ${vias} vias · ${drcTxt}`;
  if (lastShuffleRolls.length) {
    const rollTxt = lastShuffleRolls.map((r) => {
      if (!r.ok) return `${r.net} failed${r.note ? ` (${r.note})` : ""}`;
      const bits = [];
      if (r.tracks) bits.push(`+${r.tracks}t`);
      if (r.arcs) bits.push(`+${r.arcs}a`);
      if (r.vias) bits.push(`+${r.vias}v`);
      return `${r.net} ok${bits.length ? ` (${bits.join("/")})` : ""}`;
    }).join(", ");
    txt += ` · last shuffle: ${rollTxt}`;
  }
  box.textContent = txt;
  box.title = txt;
}

async function runTidy() {
  const btn = el("tidy-run-btn");
  const diag = el("tidy-diag");
  const p = readTidyParams();
  if (!(p.refine_strength > 0)) {
    syncTidyRunState("Pick a strength above 0 — try Default 0.6.");
    return;
  }
  btn?.classList.add("loading");
  if (btn) btn.disabled = true;
  try {
    if (diag) diag.textContent = "Tidying…";
    // Persist so refresh keeps the values (merge-POST keeps hidden keys).
    await apiFetch("/api/placement/params", { method: "POST", body: JSON.stringify(p) });
    const movable = (typeof selectedUuids !== "undefined" && selectedUuids.size > 0)
      ? Array.from(selectedUuids) : undefined;
    const body = { strength: p.refine_strength, radius_m: p.refine_radius_m };
    if (movable) body.movable_uuids = movable;
    const r = await apiJson("/api/placement/refine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (typeof illegalUuids !== "undefined") {
      illegalUuids.clear();
      lastLegality = r;
      for (const [ua, ub] of (r.overlapping_pairs || [])) {
        illegalUuids.add(ua);
        illegalUuids.add(ub);
      }
      for (const [uuid] of (r.wall_violations || [])) illegalUuids.add(uuid);
    }
    await poll();
    syncTidyScope();
    if (typeof renderFootprintTable === "function") renderFootprintTable();
    if (typeof acceptBtn !== "undefined" && acceptBtn) acceptBtn.disabled = !(r.refined > 0);
    if (diag) {
      const rep = (r && r.tidy_report) || {};
      const noN = Array.isArray(rep.no_neighbors) ? rep.no_neighbors.length : 0;
      const rolled = Array.isArray(rep.rolled_back) ? rep.rolled_back.length : 0;
      const considered = Number(rep.considered) || 0;
      const detail = [];
      if (rolled) detail.push(`${rolled} skipped — would overlap another part`);
      if (noN) detail.push(`${noN} had nothing nearby`);
      const suffix = detail.length ? ` (${detail.join("; ")})` : "";
      if (r.refined > 0) {
        diag.textContent = `Tidied ${r.refined} parts — review in Place, then Accept.${suffix}`;
      } else if (considered > 0 && noN >= considered) {
        diag.textContent = `No change — nothing within radius (tried ${considered} parts). Try a larger radius or stronger preset.`;
      } else {
        diag.textContent = detail.length
          ? `No change.${suffix}`
          : `No change — already neat at these settings.`;
      }
    }
  } catch (err) {
    if (diag) diag.textContent = `Tidy failed: ${err.message}`;
  } finally {
    btn?.classList.remove("loading");
    syncTidyRunState();
  }
}

buildTidyParams();
on("tidy-run-btn", "click", runTidy);
on("tidy-goto-place", "click", () => {
  if (typeof setActivePanel === "function") setActivePanel("place");
});
