// app-diagnostics.js — GND islands, impedance, bridges (report-only).
// Needs core (el/api/toast/svg/overlay scaffolds) at load; reads selection + viewer at runtime.

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
