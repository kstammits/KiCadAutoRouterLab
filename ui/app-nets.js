// app-nets.js — nets table, selection, power/protect toggles.
// Needs core (el/api/toast) at load; placement + routing read selection at runtime.

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

let netSortKey = null; // name | status | pads | airwire | cost | segs | vias | width | pwr | protect
let netSortDir = 1; // 1 = asc, -1 = desc

function netStatusRank(net) {
  if (net.failed_reason) return 0;
  if (net.needs_bridge) return 1;
  if (net.multi_region) return 2; // bridged multi-region
  return 3;
}

function netNum(v, fallback = -1) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function compareNets(a, b) {
  if (!netSortKey) return 0;
  let r = 0;
  if (netSortKey === "name") {
    r = (a.name || "").localeCompare(b.name || "", undefined, { numeric: true, sensitivity: "base" });
  } else if (netSortKey === "status") {
    r = netStatusRank(a) - netStatusRank(b);
  } else if (netSortKey === "pads") {
    r = netNum(a.pad_count, -1) - netNum(b.pad_count, -1);
  } else if (netSortKey === "airwire") {
    r = netNum(a.airwire_len_mm, -1) - netNum(b.airwire_len_mm, -1);
  } else if (netSortKey === "cost") {
    r = netNum(a.copper_cost_mm, -1) - netNum(b.copper_cost_mm, -1);
  } else if (netSortKey === "segs") {
    r = (netNum(a.track_count, 0) + netNum(a.arc_count, 0))
      - (netNum(b.track_count, 0) + netNum(b.arc_count, 0));
  } else if (netSortKey === "vias") {
    r = netNum(a.via_count, -1) - netNum(b.via_count, -1);
  } else if (netSortKey === "width") {
    r = netNum(a.width_mm, -1) - netNum(b.width_mm, -1);
  } else if (netSortKey === "pwr") {
    const rank = n => (n.is_power ? 0 : (n.is_ground ? 1 : 2));
    r = rank(a) - rank(b);
  } else if (netSortKey === "protect") {
    r = ((a.protected ? 0 : 1)) - ((b.protected ? 0 : 1));
  }
  if (r === 0) {
    r = (a.name || "").localeCompare(b.name || "", undefined, { numeric: true, sensitivity: "base" });
  }
  return r * netSortDir;
}

function updateNetSortHeaders() {
  document.querySelectorAll("#nets-table th.sortable").forEach(th => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.sort === netSortKey) {
      th.classList.add(netSortDir === 1 ? "sorted-asc" : "sorted-desc");
    }
  });
}

function getSortedNets(nets) {
  if (!netSortKey) return nets;
  return [...nets].sort(compareNets);
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
  // signal nets) so every row stays discoverable. Name-column badge above
  // stays static text.
  // Board-controlled strings are HTML-escaped (net/ref names come from the file).
  const safeName = escapeHtml(net.name);
  const pwrCtrl = net.is_power
    ? `<button type="button" class="pwr-toggle is-on" aria-pressed="true" aria-label="Unmark ${safeName} as power" title="${pwrTitle}">${pwr}</button>`
    : (net.is_ground
      ? `<button type="button" class="pwr-toggle is-ground" aria-pressed="false" aria-label="Mark ${safeName} as power (override)" title="Ground net (keyword rules) — click to add your power override">${pwr}</button>`
      : `<button type="button" class="pwr-toggle" aria-pressed="false" aria-label="Mark ${safeName} as power" title="${pwrTitle}">&#9889;</button>`);
  const width = (net.width_mm ?? "") === "" ? "" : Number(net.width_mm).toFixed(2);
  const decaps = Array.isArray(net.decap_refs) ? net.decap_refs : [];
  const links = (net.decap_links && typeof net.decap_links === "object") ? net.decap_links : {};
  const decapTxt = decaps.length
    ? `Decap: ${decaps.map(r => (links[r] ? `${r}\u2192${links[r]}` : r)).join(", ")}`
    : "";
  const airD = (net.airwire_len_mm ?? "") === "" ? "" : Number(net.airwire_len_mm).toFixed(1);
  const copperD = (net.copper_len_mm ?? "") === "" ? "" : Number(net.copper_len_mm).toFixed(1);
  const costD = (net.copper_cost_mm ?? "") === "" ? "" : Number(net.copper_cost_mm).toFixed(1);
  const airwire = airD === "" ? "" : String(Math.round(Number(airD)));
  const copper = copperD === "" ? "" : String(Math.round(Number(copperD)));
  const cost = costD === "" ? "" : String(Math.round(Number(costD)));
  const airTitle = `Airwire MST estimate over pads: ${airD === "" ? "n/a" : airD + " mm"} (unrouted difficulty)`;
  const costTitle = (net.via_cost_mm != null)
    ? `Routed cost: copper ${copperD === "" ? "n/a" : copperD + " mm"} + ${net.via_count ?? 0} vias × ${net.via_cost_mm} mm = ${costD === "" ? "n/a" : costD + " mm"}`
    : `Routed cost: copper ${copperD === "" ? "n/a" : copperD + " mm"} + via cost`;
  const padsTitle = `Pads: ${net.pad_count ?? "?"} across ${net.footprint_count ?? "?"} footprints`;
  const tracks = netNum(net.track_count, 0);
  const arcs = netNum(net.arc_count, 0);
  const segsTitle = `Routed segments: ${tracks} straight + ${arcs} arcs`;
  let status = "";
  if (net.failed_reason) {
    const reason = escapeHtml(net.failed_reason);
    const ver = (net.failed_version != null) ? ` (last run, v${net.failed_version})` : " (last run)";
    status = `<span class="net-status net-status-failed" title="Routing failed${ver}: ${reason}">⚠</span>`;
  } else if (net.needs_bridge) {
    const regs = Array.isArray(net.regions) ? net.regions.join(", ") : "";
    status = `<span class="net-status net-status-bridge" title="Spans board regions [${escapeHtml(regs)}] — routed per region, join via a mated-pair bridge (Bridges section)">🌉</span>`;
  } else if (net.multi_region) {
    status = `<span class="net-status net-status-bridged" title="Multi-region net with a declared bridge">🔗</span>`;
  }
  const nameTitle = decapTxt ? `${safeName} — ${decapTxt}` : safeName;
  return `
    <tr class="${selectedNets.has(net.name) ? 'net-selected-row' : ''}" data-net="${safeName}" style="--net-color: ${color};">
      <td class="net-col-select"><input type="checkbox" class="net-col-select" ${selectedNets.has(net.name) ? 'checked' : ''} data-net="${safeName}" title="Select ${safeName} for routing" aria-label="Select ${safeName} for routing"></td>
      <td class="net-col-name" title="${escapeHtml(nameTitle)}">
        <span class="net-swatch" style="background: ${color};"></span>
        ${safeName}${pwr}
      </td>
      <td class="net-col-status">${status}</td>
      <td class="net-col-pads" title="${escapeHtml(padsTitle)}">${net.pad_count ?? ""}</td>
      <td class="net-col-air" title="${escapeHtml(airTitle)}">${airwire}</td>
      <td class="net-col-cost" title="${escapeHtml(costTitle)}">${cost}</td>
      <td class="net-col-segs" title="${escapeHtml(segsTitle)}">${tracks + arcs}</td>
      <td class="net-col-vias">${net.via_count ?? ""}</td>
      <td class="net-col-width">${width}</td>
      <td class="net-col-pwr" data-power-cell="${safeName}" title="${pwrTitle}" style="cursor:pointer;">${pwrCtrl}</td>
      <td class="net-col-protect">
        <label class="chk"><input type="checkbox" class="net-protect-cb" ${net.protected ? 'checked' : ''} data-net="${safeName}" title="Protected nets are routed first and never ripped up"></label>
      </td>
    </tr>
  `;
}

function renderNetsTable(nets) {
  const tbody = el("nets-tbody");
  if (!tbody) return;

  const sorted = getSortedNets(nets || []);
  updateNetSortHeaders();
  tbody.innerHTML = sorted.map(netRowHtml).join("");
  
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
  const refineBtn = el("refine-btn");
  if (refineBtn) {
    refineBtn.disabled = count === 0;
    refineBtn.textContent = count > 0 ? `Refine Order (${count})` : "Refine Order";
  }
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

// Select nets that are not yet done: 2+ pads, no tracks/vias committed,
// not protected. Failed and never-routed nets both have zero committed
// copper (partials are display-only), so one filter covers "remaining".
function selectUnroutedNets() {
  if (!lastNetsByName || lastNetsByName.size === 0) {
    toast("no nets loaded yet");
    return;
  }
  const rows = [...lastNetsByName.values()];
  const copper = n => (n.track_count || 0) + (n.arc_count || 0) + (n.via_count || 0);
  const unrouted = rows
    .filter(n => (n.pad_count || 0) >= 2
      && copper(n) === 0
      && !n.protected)
    .map(n => n.name);
  const skippedProtected = rows
    .filter(n => (n.pad_count || 0) >= 2
      && copper(n) === 0
      && n.protected).length;
  selectedNets = new Set(unrouted);
  syncNetRowSelection();
  toast(unrouted.length
    ? `selected ${unrouted.length} unrouted net${unrouted.length > 1 ? "s" : ""}` +
      (skippedProtected ? ` (skipped ${skippedProtected} protected)` : "")
    : "no unrouted nets found",
    unrouted.length > 0);
}

// Protect all checkbox
on("select-unrouted-btn", "click", selectUnroutedNets);

// Sortable nets-table headers (mirrors the footprints table): click toggles
// asc/desc, re-render keeps the selection (keyed by net name). Clicks on the
// protect-all checkbox/label must not re-sort.
document.querySelectorAll("#nets-table th.sortable").forEach(th => {
  th.addEventListener("click", e => {
    if (e.target.closest("label, input")) return;
    const key = th.dataset.sort;
    if (!key) return;
    if (netSortKey === key) {
      netSortDir = netSortDir === 1 ? -1 : 1;
    } else {
      netSortKey = key;
      netSortDir = 1;
    }
    renderNetsTable([...(lastNetsByName ? lastNetsByName.values() : [])]);
  });
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
