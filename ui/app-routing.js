// app-routing.js — routing params panel, route/rip jobs, status lines.
// Needs core (el/api/toast) at load; reads nets + viewer at runtime.

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
    { key: "max_terminals_per_net", label: "Max terminals per net (0 = no cap; larger nets auto-split into chunks)", min: 0, max: 128, step: 1, default: 16 },
    { key: "piecemeal_chunk_terminals", label: "Piecemeal chunk size (0 = disable chunking; over-cap nets fail fast)", min: 0, max: 32, step: 1, default: 12 },
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
// the run so a 66-pad GND never surprises: it auto-splits into chunks via
// max_terminals_per_net / piecemeal_chunk_terminals instead of burning
// hours of single-flight Steiner searches (chunking off = fail fast).
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
// Display-only copper from failed nets' routed chunks (piecemeal): orange
// dashed overlay, never committed to the board or the save. Cleared on the
// next route run; board refreshes wipe SVG overlays, so this re-renders
// after the batch's final refresh.
let partialCopperData = [];
function renderPartialCopperOverlay() {
  makeSvgOverlay("partial-copper-overlay", partialCopperData.length > 0, partialCopperData, g => {
    for (const item of partialCopperData) {
      for (const t of item.tracks || []) {
        const line = svgEl("line", {
          x1: t.start[0], y1: t.start[1], x2: t.end[0], y2: t.end[1],
          stroke: OVERLAY_COLORS.attention, "stroke-width": Math.max(t.width_mm, 0.15),
          "stroke-dasharray": "1 0.6", "stroke-opacity": "0.9", fill: "none",
        });
        line.appendChild(svgTitle(`${item.net} partial chunk copper (not saved — review only)`));
        g.appendChild(line);
      }
      for (const v of item.vias || []) {
        const dot = svgEl("circle", {
          cx: v.position[0], cy: v.position[1], r: Math.max(v.size_mm / 2, 0.4),
          fill: "none", stroke: OVERLAY_COLORS.attention, "stroke-width": "0.2",
          "stroke-dasharray": "0.5 0.4",
        });
        dot.appendChild(svgTitle(`${item.net} partial via (not saved)`));
        g.appendChild(dot);
      }
    }
  });
}

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
  partialCopperData = [];
  renderPartialCopperOverlay();

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
      for (const [n, reason] of Object.entries(r.failed_reasons || {})) {
        const pCount = (r.partial_tracks || []).filter(t => t.net_name === n).length;
        failures.push(`${n}: ${reason}${pCount ? ` — ${pCount} partial tracks shown in orange (not saved)` : ""}`);
      }
      if ((r.partial_tracks || []).length || (r.partial_vias || []).length) {
        partialCopperData.push({ net, tracks: r.partial_tracks || [], vias: r.partial_vias || [] });
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
  // Board refreshes wipe SVG overlays; re-render partials last so failed
  // nets stay reviewable (cost grid + per-net secondary stay available via
  // the Debug checkbox and /api/routing/debug/cost-grid?net=).
  renderPartialCopperOverlay();

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
    partialCopperData = [];
    renderPartialCopperOverlay();
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
