// app-tidy.js — Tidy work-tab: geometric neat pass after force-spring placement.
//
// Owns two PlacementParams sliders (refine_strength 0..1, refine_radius_m
// shown in mm) and the Run Tidy action (POST /api/placement/refine).
// Needs core (el/api/toast) at load; placement panel globals (poll,
// illegalUuids, lastLegality, renderFootprintTable, acceptBtn) at runtime.

const TIDY_PARAM_DEFS = [
  { key: "refine_strength", label: "Refine strength", min: 0, max: 1, step: 0.05,
    desc: "Geometric neat pass only: neighbor-median angle align + median-gap equalize. 0 = off." },
  { key: "refine_radius_m", label: "Neighbor radius (mm)", min: 5, max: 50, step: 1,
    display: { unit: "mm", scale: 1000, step: 1 },
    desc: "Neighbor search radius. Shown in mm; stored in meters. Only the 6 nearest parts inside count." },
];

function tidyToDisplay(val, def) {
  return def.display ? val * def.display.scale : val;
}
function tidyFromDisplay(val, def) {
  return def.display ? val / def.display.scale : val;
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
    const def = TIDY_PARAM_DEFS.find(d => d.key === key);
    const range = grid.querySelector(`input[type=range][data-key="${key}"]`);
    const num = grid.querySelector(`input[type=number][data-key="${key}"]`);
    if (!range || !num) return;
    if (e.target.type === "range") num.value = range.value;
    else range.value = num.value;
    const btn = el("tidy-run-btn");
    if (btn && key === "refine_strength") btn.disabled = !(Number(num.value) > 0);
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
    const shown = tidyToDisplay(saved[def.key] ?? (def.key === "refine_radius_m" ? 0.020 : 0), def);
    const range = grid.querySelector(`input[type=range][data-key="${def.key}"]`);
    const num = grid.querySelector(`input[type=number][data-key="${def.key}"]`);
    if (range) range.value = Math.min(Math.max(shown, tidyToDisplay(def.min, def)), tidyToDisplay(def.max, def));
    if (num) num.value = shown;
  }
  syncTidyRunState();
}

function syncTidyRunState() {
  const btn = el("tidy-run-btn");
  const diag = el("tidy-diag");
  if (!btn) return;
  const { refine_strength } = readTidyParams();
  const off = !(refine_strength > 0);
  btn.disabled = off;
  btn.title = off
    ? "Set Refine strength above 0 to enable"
    : "Geometric neat pass: align angles to neighbors + equalize gaps (legality-gated, merges into the Place proposal)";
  if (off && diag) diag.textContent = "strength 0 = off";
}

async function runTidy() {
  const btn = el("tidy-run-btn");
  const diag = el("tidy-diag");
  const p = readTidyParams();
  if (!(p.refine_strength > 0)) {
    if (diag) diag.textContent = "strength 0 = off";
    return;
  }
  btn?.classList.add("loading");
  if (btn) btn.disabled = true;
  try {
    if (diag) diag.textContent = "tidying…";
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
    if (typeof renderFootprintTable === "function") renderFootprintTable();
    if (typeof acceptBtn !== "undefined" && acceptBtn) acceptBtn.disabled = !(r.refined > 0);
    if (diag) {
      const rep = (r && r.tidy_report) || {};
      const noN = Array.isArray(rep.no_neighbors) ? rep.no_neighbors.length : 0;
      const rolled = Array.isArray(rep.rolled_back) ? rep.rolled_back.length : 0;
      const considered = Number(rep.considered) || 0;
      const baseN = Number(rep.base_overlaps) || 0;
      if (r.refined > 0) {
        let msg = `tidied ${r.refined} parts — review in Place → Accept`;
        const bits = [];
        if (baseN) bits.push(`left ${baseN} existing touch${baseN > 1 ? "es" : ""} alone`);
        if (rolled) bits.push(`${rolled} skipped (would land on another part)`);
        if (noN) bits.push(`${noN} had nothing nearby`);
        if (bits.length) msg += ` (${bits.join("; ")})`;
        diag.textContent = msg;
      } else if (considered > 0 && noN >= considered) {
        diag.textContent = `no tidy change — nothing within radius (tried ${considered} parts)`;
      } else {
        const bits = [];
        if (baseN) bits.push(`${baseN} existing touch${baseN > 1 ? "es" : ""} left alone`);
        if (rolled) bits.push(`${rolled} skipped (would land on another part)`);
        if (noN) bits.push(`${noN} had nothing nearby`);
        diag.textContent = bits.length
          ? `no tidy change (${bits.join("; ")})`
          : `no tidy change`;
      }
    }
  } catch (err) {
    if (diag) diag.textContent = `tidy failed: ${err.message}`;
  } finally {
    btn?.classList.remove("loading");
    syncTidyRunState();
  }
}

buildTidyParams();
on("tidy-run-btn", "click", runTidy);
