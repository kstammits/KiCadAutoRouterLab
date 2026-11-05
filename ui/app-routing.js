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
    let mode = null;
    try {
      const mres = await fetch("/api/mode");
      if (mres.ok) {
        const minfo = await mres.json();
        preset = minfo.preset || {};
        owned = minfo.owned || Object.keys(preset);
        mode = minfo.mode || null;
      }
    } catch (err) { /* no board loaded — nothing is mode-owned */ }
    renderRoutingParams(routingParams, owned, preset, mode);
  } catch (err) {
    toast(`routing params load failed: ${err.message}`);
    renderRoutingParams({});
  }
}

// One-line new-user descriptions for the routing sliders. Slider/number
// inputs are siblings of the label <span>, so the description also goes on
// the wrapping .param div (ancestor title covers both inputs on hover).
const ROUTING_PARAM_DESCS = {
  track_width_mm: "Default signal track width (mm). Per-net overrides and power width win over this.",
  power_width_mm: "Track width for power (PWR-marked) nets.",
  clearance_mm: "Copper clearance: minimum gap between different-net copper (mm).",
  fanout_margin_mm: "Steering-only push away from foreign (other-net) pins (mm). Guides the search; never changes clearance rules.",
  via_cost_mm: "Extra cost per via, in mm of track — higher means fewer vias.",
  pour_severance_weight: "4-layer only — penalty for cutting GND-pour continuity. 0 disables pour-awareness.",
  max_terminals_per_net: "Nets with more terminals auto-split into chunks (piecemeal). 0 = no cap.",
  piecemeal_chunk_terminals: "Terminals per chunk when a net exceeds the cap. 0 = disable chunking (over-cap nets fail fast).",
  via_size_mm: "Via outer diameter (mm).",
  via_drill_mm: "Via drill diameter (mm).",
  max_expansions_per_net: "Total expansion budget across one net's pair-searches. 0 = no cap.",
  refine_seeds: "Refine candidates: shuffled re-routes tried per Refine run (plus keeping current copper).",
  refine_seed: "Refine base seed: shuffles derive from base..base+seeds-1 (reproducible; auto-advances per run).",
};
// Removed sliders stay engine/API-valid (RoutingParams + validation
// untouched; POST merges) — grid resolution, arc radii/sample, edge
// margin, heuristic weight, per-search attempts/expansions, search
// windows, stitch caps, and plane-fanout cap are set-and-forget or
// script-tuned, so they live outside the panel.

// Main-panel sliders: everyday copper knobs. Chunking + via geometry +
// the single per-net search budget live in Advanced; everything else
// (grid resolution, arc radii, margin, heuristic, per-search caps,
// windows, stitch caps, plane-fanout cap) is API-only at calibrated
// defaults — POST /api/routing/params merges, so hidden keys survive.
const ROUTING_PARAM_DEFS = [
  { key: "track_width_mm", label: "Track Width (mm)", min: 0.1, max: 2.0, step: 0.05, default: 0.25 },
  { key: "power_width_mm", label: "Power Width (mm)", min: 0.2, max: 3.0, step: 0.05, default: 0.5 },
  { key: "clearance_mm", label: "Clearance (mm)", min: 0.1, max: 1.0, step: 0.05, default: 0.2 },
  { key: "fanout_margin_mm", label: "Push away from foreign pins (mm, steering only)", min: 0, max: 1.0, step: 0.05, default: 0 },
  { key: "via_cost_mm", label: "Via Cost (mm)", min: 1, max: 20, step: 0.5, default: 5 },
  // 4-layer-only knob: hidden in two-layer mode (mode forces it there),
  // amber-badged elsewhere (see routingSliderHtml + .param-4l in style.css).
  { key: "pour_severance_weight", label: "Pour Severance Weight (0 disables)", min: 0, max: 200, step: 5, default: 30, fourLayerOnly: true },
];

const ROUTING_ADVANCED_PARAM_DEFS = [
  { key: "via_size_mm", label: "Via Size (mm)", min: 0.4, max: 1.5, step: 0.05, default: 0.8 },
  { key: "via_drill_mm", label: "Via Drill (mm)", min: 0.2, max: 0.8, step: 0.05, default: 0.4 },
  { key: "max_expansions_per_net", label: "Max Expansions / net (0 = no cap)", min: 0, max: 20000000, step: 100000, default: 4000000 },
  { key: "max_terminals_per_net", label: "Max terminals per net (0 = no cap; larger nets auto-split into chunks)", min: 0, max: 128, step: 1, default: 16 },
  { key: "piecemeal_chunk_terminals", label: "Piecemeal chunk size (0 = disable chunking; over-cap nets fail fast)", min: 0, max: 32, step: 1, default: 12 },
  { key: "refine_seeds", label: "Refine Candidates", min: 1, max: 8, step: 1, default: 3 },
  { key: "refine_seed", label: "Refine Base Seed", min: 0, max: 9999, step: 1, default: 0 },
];

function routingSliderHtml(def, params, modeOwned, mode = null) {
  const value = params[def.key] ?? def.default;
  const managed = modeOwned.includes(def.key);
  // 4-layer-only knobs (e.g. pour severance) are hidden in two-layer
  // mode; elsewhere they carry an amber 4L badge (see .param-4l).
  const fourL = def.fourLayerOnly === true && mode !== "two-layer";
  const desc = ROUTING_PARAM_DESCS[def.key] || def.label;
  const tip = (fourL ? "4-layer only — " : "") + (managed ? `Managed by mode — ${desc}` : desc);
  return `
      <div class="param${fourL ? " param-4l" : ""}" title="${tip}">
        <span title="${tip}">${def.label}${fourL ? ' <span class="mode4l-badge">4L</span>' : ""}${managed ? " (mode)" : ""}</span>
        <div class="row">
          <input type="range" data-key="${def.key}" min="${def.min}" max="${def.max}" step="${def.step}" value="${value}" ${managed ? "disabled" : ""}>
          <input type="number" data-key="${def.key}" min="${def.min}" max="${def.max}" step="${def.step}" value="${value}" ${managed ? "disabled" : ""}>
        </div>
      </div>
    `;
}

function syncRoutingGrid(grid) {
  if (!grid) return;
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
// Render routing params sliders (main + Advanced grids). `mode` is the
// board mode name ("two-layer"/"four-layer"/null when no board): 4-layer-only
// knobs are hidden in two-layer mode and amber-badged elsewhere.
function renderRoutingParams(params, modeOwned = [], modePreset = {}, mode = null) {
  const grid = el("routing-params-grid");
  const advGrid = el("routing-advanced-params-grid");
  if (!grid) return;
  const visibleMainDefs = ROUTING_PARAM_DEFS.filter(
    def => !(def.fourLayerOnly === true && mode === "two-layer"));

  // Option flags share one full-width row below the sliders (not one
  // grid cell per checkbox) to save vertical space.
  const debugChecked = params.debug ? "checked" : "";
  const arcsChecked = params.arc_fillets ? "checked" : "";
  const stitchChecked = params.stitch_gnd_vias ?? true ? "checked" : "";
  const stripChecked = params.strip_gnd_blockers ?? true ? "checked" : "";
  const refillChecked = params.refill_zones ? "checked" : "";
  const corridorChecked = params.corridor ?? true ? "checked" : "";
  const runDrcChecked = params.run_drc ? "checked" : "";
  const fanoutChecked = params.fanout_planes ? "checked" : "";
  const stitchManaged = modeOwned.includes("stitch_gnd_vias");
  const stripManaged = modeOwned.includes("strip_gnd_blockers");
  const refillManaged = modeOwned.includes("refill_zones");
  const fanoutManaged = modeOwned.includes("fanout_planes");
  const layersManaged = modeOwned.includes("allowed_layers");
  const layersVal = modePreset.allowed_layers;
  const layersText = layersManaged
    ? (Array.isArray(layersVal) ? layersVal.join(", ") : "all copper layers")
    : null;
  const debugHtml = `
    <div class="param param-flags" style="grid-column:1/-1;">
      <span>Options</span>
      <div class="row" style="gap:.9rem; flex-wrap:wrap;">
        <label class="chk" style="margin:0;" title="Round sharp corners into tangent copper arcs (clearance-gated; sharp joints kept where legs are short or copper is near)">
          <input type="checkbox" data-key="arc_fillets" ${arcsChecked}> Arc fillets
        </label>
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
  const advFlagsHtml = `
    <div class="param param-flags" style="grid-column:1/-1;">
      <span>Search options</span>
      <div class="row" style="gap:.9rem; flex-wrap:wrap;">
        <label class="chk" style="margin:0;" title="Attempt coarse corridor guidance when the initial window fails (slower but escapes tight spots)">
          <input type="checkbox" data-key="corridor" ${corridorChecked}> Corridor guidance
        </label>
        <label class="chk" style="margin:0;" title="${fanoutManaged ? "Managed by mode" : "Satisfy plane-owned nets with short outer stubs + through-vias tapping the inner plane instead of long outer tracks"}">
          <input type="checkbox" data-key="fanout_planes" ${fanoutChecked} ${fanoutManaged ? "disabled" : ""}> Plane fanout${fanoutManaged ? " (mode)" : ""}
        </label>
        <label class="chk" style="margin:0;" title="Run kicad-cli DRC after routing (slow)">
          <input type="checkbox" data-key="run_drc" ${runDrcChecked}> Run DRC
        </label>
      </div>
    </div>
  `;

  grid.innerHTML = visibleMainDefs.map(def => routingSliderHtml(def, params, modeOwned, mode)).join("") + debugHtml;
  syncRoutingGrid(grid);

  if (advGrid) {
    advGrid.innerHTML =
      ROUTING_ADVANCED_PARAM_DEFS.map(def => routingSliderHtml(def, params, modeOwned, mode)).join("") + advFlagsHtml;
    syncRoutingGrid(advGrid);
  }
}

// Read routing params from UI (main + Advanced grids)
function readRoutingParams() {
  const params = {};
  for (const id of ["routing-params-grid", "routing-advanced-params-grid"]) {
    const grid = el(id);
    if (!grid) continue;
    grid.querySelectorAll('input[data-key]').forEach(input => {
      if (input.type === "checkbox") {
        params[input.dataset.key] = input.checked;
      } else {
        params[input.dataset.key] = input.type === "number" ? Number(input.value) : input.value;
      }
    });
  }
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
// Three on-arc points (KiCad semantics) to a polyline: circumcenter +
// angle interpolation, so no SVG large-arc/sweep flags to get wrong.
// Degenerate (collinear) input falls back to a straight chord.
function arcToPolyline(start, mid, end, n) {
  const segs = Math.max(2, n || 8);
  const [sx, sy] = start, [mx, my] = mid, [ex, ey] = end;
  const d = 2 * (sx * (my - ey) + mx * (ey - sy) + ex * (sy - my));
  if (Math.abs(d) < 1e-9) return [start, end];
  const s2 = sx * sx + sy * sy, m2 = mx * mx + my * my, e2 = ex * ex + ey * ey;
  const ux = (s2 * (my - ey) + m2 * (ey - sy) + e2 * (sy - my)) / d;
  const uy = (s2 * (ex - mx) + m2 * (sx - ex) + e2 * (mx - sx)) / d;
  const norm = (a) => Math.atan2(Math.sin(a), Math.cos(a));
  const a0 = Math.atan2(sy - uy, sx - ux);
  const a1 = Math.atan2(my - uy, mx - ux);
  const a2 = Math.atan2(ey - uy, ex - ux);
  const sweep = norm(a1 - a0) + norm(a2 - a1);
  const r = Math.hypot(sx - ux, sy - uy);
  const pts = [];
  for (let i = 0; i <= segs; i++) {
    const a = a0 + (sweep * i) / segs;
    pts.push([ux + r * Math.cos(a), uy + r * Math.sin(a)]);
  }
  return pts;
}
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
      for (const a of item.arcs || []) {
        const pts = arcToPolyline(a.start, a.mid, a.end, 8)
          .map(p => p.join(",")).join(" ");
        const arc = svgEl("polyline", {
          points: pts,
          stroke: OVERLAY_COLORS.attention, "stroke-width": Math.max(a.width_mm, 0.15),
          "stroke-dasharray": "1 0.6", "stroke-opacity": "0.9", fill: "none",
        });
        arc.appendChild(svgTitle(`${item.net} partial arc copper (not saved — review only)`));
        g.appendChild(arc);
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
  const kept = `v${lastBoardVersion} — ${totals.tracks} tracks` +
    (totals.arcs ? `, ${totals.arcs} arcs` : "") + `, ${totals.vias} vias, ` +
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

  const totals = { tracks: 0, arcs: 0, vias: 0, routed: 0, failed: 0, expansions: 0, astarS: 0 };
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
          totals.arcs += r.arcs_added || 0;
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
      totals.arcs += r.arcs_added || 0;
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
        const pCount = (r.partial_tracks || []).filter(t => t.net_name === n).length +
          (r.partial_arcs || []).filter(t => t.net_name === n).length;
        failures.push(`${n}: ${reason}${pCount ? ` — ${pCount} partial tracks shown in orange (not saved)` : ""}`);
      }
      if ((r.partial_tracks || []).length || (r.partial_vias || []).length || (r.partial_arcs || []).length) {
        partialCopperData.push({ net, tracks: r.partial_tracks || [], vias: r.partial_vias || [], arcs: r.partial_arcs || [] });
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
  // finally-guaranteed: a throw anywhere below (overlay render, summary
  // format) must never wedge the route buttons busy — that stuck state
  // was observed after a failed refine run.
  try {
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
  } finally {
    setRouteBusy(false);
    if (cancelBtn) cancelBtn.hidden = true;
    progress.hidden = true;
    updateNetSelectionUI();
  }
}

// Rip up selected nets: remove their copper without re-routing.
// Protected nets are skipped server-side and reported in the diag line.
async function ripSelectedNets() {
  if (selectedNets.size === 0) return;
  const diag = routeDiag;
  const ripBtn = el("rip-selected-btn");
  const routeBtn = el("route-selected-btn");
  const routeAllBtn = el("route-all-btn");
  const selectUnroutedBtn = el("select-unrouted-btn");
  const shuffleBtn = el("shuffle-btn");
  const nets = Array.from(selectedNets);
  for (const b of [ripBtn, routeBtn, routeAllBtn, selectUnroutedBtn, shuffleBtn]) {
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
      ? names.map(n => `${n}: ${ripped[n].tracks || 0} tracks` +
          ((ripped[n].arcs || 0) ? `/${ripped[n].arcs} arcs` : "") +
          `/${ripped[n].vias || 0} vias`).join(", ")
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
    for (const b of [ripBtn, routeBtn, routeAllBtn, selectUnroutedBtn, shuffleBtn]) {
      if (b) b.classList.remove("loading");
    }
    updateNetSelectionUI();
  }
}

function requestRouteCancel() {
  // Cooperative cancel: the in-flight job stops between pairs/attempts
  // server-side (already-routed nets are kept). The poll loop observes the
  // flag, posts the cancel, then collects the partial outcome.
  routeCancelRequested = true;
  if (routeCurrentJobId) cancelRoutingJob(routeCurrentJobId);
}

on("route-cancel-btn", "click", requestRouteCancel);
on("shuffle-cancel-btn", "click", requestRouteCancel);

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

// Refine: re-route the selected nets under several shuffled orders, keep
// the best by (nets routed, then wire + via cost). One background job;
// the server restores prior copper unless a candidate strictly wins.
async function startRefineJob(nets, params) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const res = await fetch("/api/routing/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ net_names: nets, params, refine: true }),
    });
    if (res.status === 409 && attempt === 0) {
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

function formatRefineSummary(r) {
  const cands = r.candidates || [];
  const candTxt = cands.map(c => (c.seed === -1 ? "kept" : `s${c.seed}`) +
    `:${c.nets_routed} routed/${c.score}mm`).join(" · ");
  return `best of ${cands.length} (seed ${r.winner_seed}): ` +
    `v${r.version} — ${r.tracks_added} tracks` +
    (r.arcs_added ? `, ${r.arcs_added} arcs` : "") + `, ${r.vias_added} vias, ` +
    `${r.nets_routed} routed, ${r.nets_failed} failed ` +
    `(${fmtExpansions(r.expansions)}) [${candTxt}]`;
}

async function refineSelectedNets() {
  if (selectedNets.size === 0) return;
  const diag = routeDiag;
  const cancelBtn = el("route-cancel-btn");
  const progress = el("route-progress");
  if (!diag || !progress) return;

  const nets = Array.from(selectedNets);
  await saveRoutingParams();
  const params = readRoutingParams();
  const t0 = performance.now();
  const elapsed = () => `${Math.round((performance.now() - t0) / 1000)}s`;
  const deadline = t0 + ROUTE_JOB_CAP_S * 1000;

  setRouteBusy(true);
  progress.hidden = false;
  progress.value = 0;
  if (cancelBtn) cancelBtn.hidden = false;
  routeCancelRequested = false;
  partialCopperData = [];
  renderPartialCopperOverlay();

  let jobId = null;
  try {
    jobId = await startRefineJob(nets, params);
    routeCurrentJobId = jobId;
    for (;;) {
      if (routeCancelRequested) {
        await cancelRoutingJob(jobId);
        break;
      }
      const job = await apiJson(jobUrl(jobId));
      const p = job.progress || {};
      const candTxt = p.candidates > 0 ? `candidate ${p.candidate}/${p.candidates} · ` : "";
      const pairTxt = p.pairs_total > 0 ? `pair ${Math.min(p.pairs_total, p.pairs_done + 1)}/${p.pairs_total}` : "starting";
      diag.textContent = `refine ${candTxt}${pairTxt} · ${elapsed()}`;
      if (p.candidates > 0) progress.value = Math.min(0.99, (p.candidate || 1) / p.candidates);
      if (job.state === "done") {
        routeCurrentJobId = null;
        const r = job.result;
        if (!r) throw new Error("empty job result");
        if (r.version) {
          lastVersion = r.version;
          await fetchAndInjectSVG(r.version);
        }
        await fetchNets();
        renderPartialCopperOverlay();
        diag.textContent = formatRefineSummary(r) + ` (${elapsed()})`;
        diag.title = diag.textContent;
        // Advance the base seed past the tried range so the next Refine
        // explores fresh orders instead of replaying these. Uses the
        // requested count (not candidates.length) since duplicate orders
        // are skipped silently and return fewer candidates.
        const tried = Number(params.refine_seeds || 0) ||
          (r.candidates || []).filter(c => c.seed >= 0).length;
        if (tried > 0) {
          const nextSeed = Number(params.refine_seed || 0) + tried;
          for (const id of ["routing-params-grid", "routing-advanced-params-grid"]) {
            const g = el(id);
            if (!g) continue;
            const num = g.querySelector('input[type="number"][data-key="refine_seed"]');
            const range = g.querySelector('input[type="range"][data-key="refine_seed"]');
            if (num) num.value = nextSeed;
            if (range) range.value = Math.min(Number(range.max), nextSeed);
          }
        }
        if (undoBtn) undoBtn.disabled = false;
        break;
      }
      if (job.state === "failed") throw new Error(job.error || "refine job failed");
      if (performance.now() > deadline) {
        await cancelRoutingJob(jobId);
        diag.textContent = `refine hit ${ROUTE_JOB_CAP_S / 60}min cap — kept current copper`;
        break;
      }
      await new Promise(r => setTimeout(r, ROUTE_JOB_POLL_MS));
    }
  } catch (err) {
    routeCurrentJobId = null;
    diag.textContent = `refine failed: ${err.message}`;
  } finally {
    setRouteBusy(false);
    if (cancelBtn) cancelBtn.hidden = true;
    progress.hidden = true;
    updateNetSelectionUI();
  }
}

// Shuffle lives in the Route panel: rip and re-route random routable nets,
// N times in a queue. Picks each net uniformly from unprotected 2+ pad
// nets (repeats allowed, so a "lucky" net can roll again). Single-net
// routing jobs already strip that net's copper first (pipeline stage 0a),
// so no explicit rip-up call is needed. Own diag/progress/cancel in the
// Shuffle bar; the shared route busy-state still locks the Route buttons
// (single-flight server jobs). Per-net failures don't abort the queue
// (except HTTP 400, which would fail every net identically).
// `rolls` is the per-roll log from the last shuffle run (kept across
// refreshes so reopening the panel doesn't lose it).
let lastShuffleRolls = [];
async function refreshRouteMetrics(rolls) {
  if (Array.isArray(rolls)) lastShuffleRolls = rolls;
  const box = el("route-metrics");
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
async function shuffleRandomNets() {
  const diag = el("shuffle-diag");
  const cancelBtn = el("shuffle-cancel-btn");
  const progress = el("shuffle-progress");
  const countInput = el("shuffle-count");
  if (!diag || !progress) return;

  let times = Math.round(Number(countInput?.value) || 10);
  if (!(times >= 1)) times = 1;
  if (times > 50) times = 50;
  if (countInput) countInput.value = String(times);

  if (!lastNetsByName || lastNetsByName.size === 0) await fetchNets();
  const pool = [...(lastNetsByName?.values() || [])]
    .filter(n => (n.pad_count || 0) >= 2 && !n.protected)
    .map(n => n.name);
  if (pool.length === 0) {
    diag.textContent = "shuffle: no routable nets (need 2+ pads, unprotected)";
    return;
  }
  await saveRoutingParams();
  const params = readRoutingParams();
  const queue = [];
  for (let i = 0; i < times; i++) {
    queue.push(pool[Math.floor(Math.random() * pool.length)]);
  }

  const expCap = Number(params.max_expansions_per_net) > 0
    ? Number(params.max_expansions_per_net) : ROUTE_EXP_CAP_FALLBACK;
  diag.textContent = `shuffle ×${times} from ${pool.length} nets · budget ${fmtExpShort(expCap)}/net`;
  diag.title = `Random queue: ${queue.join(", ")}`;

  setRouteBusy(true);
  if (countInput) countInput.disabled = true;
  progress.hidden = false;
  progress.value = 0;
  if (cancelBtn) cancelBtn.hidden = false;
  routeCancelRequested = false;

  const totals = { tracks: 0, arcs: 0, vias: 0, routed: 0, failed: 0, expansions: 0, astarS: 0 };
  const failures = [];
  const perRoll = [];
  let doneCount = 0;
  let lastBoardVersion = lastVersion;
  const t0 = performance.now();
  const elapsed = () => `${Math.round((performance.now() - t0) / 1000)}s`;

  for (let i = 0; i < queue.length; i++) {
    if (routeCancelRequested) break;
    const net = queue[i];
    diag.textContent = `shuffle ${i + 1}/${queue.length} (${net}) — starting — ${elapsed()}`;
    diag.title = `Random queue: ${queue.join(", ")}`;
    progress.value = i / queue.length;

    let jobId = null;
    try {
      jobId = await startRoutingJob(net, params);
      routeCurrentJobId = jobId;
      const { result: r, userCancelled, timedOut } = await pollRoutingJob(jobId, net, i, queue.length, diag, progress, `shuffle ${i + 1}/${queue.length}`, t0, expCap, 0);
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
        perRoll.push({ net, ok: false, note: "cap" });
        totals.failed += 1;
        continue;
      }
      if (!r) throw new Error("empty job result");
      totals.tracks += r.tracks_added || 0;
      totals.arcs += r.arcs_added || 0;
      totals.vias += r.vias_added || 0;
      totals.routed += r.nets_routed || 0;
      totals.failed += r.nets_failed || 0;
      totals.expansions += r.expansions || 0;
      if (r.timings?.astar_s) totals.astarS += r.timings.astar_s;
      for (const [n, reason] of Object.entries(r.failed_reasons || {})) {
        failures.push(`${n}: ${reason}`);
      }
      perRoll.push({
        net,
        ok: (r.nets_failed || 0) === 0,
        tracks: r.tracks_added || 0,
        arcs: r.arcs_added || 0,
        vias: r.vias_added || 0,
      });
      if (r.debug) showCostGridDebug(r.debug);
      if (r.version) {
        lastBoardVersion = r.version;
        lastVersion = r.version;
        await fetchAndInjectSVG(r.version);
      }
    } catch (err) {
      routeCurrentJobId = null;
      if (err?.status === 400) {
        failures.push(`${net}: ${err.message}`);
        totals.failed += queue.length - i;
        doneCount = queue.length;
        diag.textContent = `shuffle aborted: ${err.message} — kept ${totals.routed}/${queue.length} rolls`;
        break;
      }
      failures.push(`${net}: ${err.message}`);
      perRoll.push({ net, ok: false, note: String(err.message || err) });
      totals.failed += 1;
    } finally {
      doneCount = i + 1;
      progress.value = doneCount / queue.length;
    }
  }

  await fetchNets();
  renderPartialCopperOverlay();
  try { await refreshRouteMetrics(perRoll); } catch { /* metrics are best-effort */ }
  diag.textContent = `shuffled ${doneCount}/${queue.length} (${queue.slice(0, doneCount).join(", ")}) — ` +
    formatTotalsDiag({
      totals, failures, doneCount, netCount: queue.length, lastBoardVersion,
      elapsedTxt: elapsed(), cancelled: routeCancelRequested,
    });
  diag.title = diag.textContent;

  setRouteBusy(false);
  if (countInput) countInput.disabled = false;
  if (cancelBtn) cancelBtn.hidden = true;
  progress.hidden = true;
  updateNetSelectionUI();
}

// Event listeners for routing panel
on("route-selected-btn", "click", routeSelectedNets);
on("route-all-btn", "click", routeAllNets);
on("rip-selected-btn", "click", ripSelectedNets);
on("refine-btn", "click", refineSelectedNets);
on("shuffle-btn", "click", shuffleRandomNets);
