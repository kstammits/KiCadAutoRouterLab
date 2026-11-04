// app-context.js — whole-GUI-context save/load for route-attempt review.
//
// One self-contained JSON file captures the app state behind a (possibly
// failed) routing attempt: board bytes as served, both param sets, board
// mode, nets state (protected / power overrides / selection), inlet +
// bridge anchors, and the attempt record (route-diag text, per-net failure
// reasons, display-only partial copper, downsampled debug cost grid).
//
// Browser-mediated both ways: save composes from the GETs the UI already
// calls plus in-memory overlay/diag state and downloads a blob; load posts
// the embedded board through the normal /api/load path, restores the rest
// through their existing POSTs, and injects the view-only parts (partials,
// debug raster, diag text) through the existing renderers. No new server
// endpoints, no server-side paths. Replayed overlays are review-only: they
// never touch STATE.pcb_tree, and the next route run clears them.
//
// Needs core (el/on/api/toast/uploadFile) at load; nets/routing/viewer
// globals read at runtime only.

const CTX_BUNDLE_KIND = "kicad-autorouter-context";
const CTX_BUNDLE_VERSION = 1;
// Single-file cap: PCB text dominates (fixtures are ~1-2 MB). Over it,
// retry with Debug off (drops the raster, keeps partials + reasons).
const CTX_MAX_BYTES = 10 * 1024 * 1024;

function ctxBoardStem(name) {
  const base = String(name || "board").split("/").pop();
  return base.replace(/\.kicad_pcb$/i, "").replace(/[^\w.-]+/g, "_") || "board";
}

// ---- Save ----

async function ctxCollectBundle() {
  if (!lastVersion) throw new Error("no board loaded");
  // Board bytes as served (two-layer mode with inners serves collapsed —
  // record the mode so a reviewer knows what the bytes are).
  const dlRes = await apiFetch("/api/board/download");
  const pcbText = await dlRes.text();
  const boardName = dlRes.headers.get("Content-Disposition")?.split('filename="')[1]?.split('"')[0]
    || (typeof nameEl !== "undefined" && nameEl?.textContent) || "board.kicad_pcb";
  // Params + mode + nets state via the endpoints their panels already use.
  // Each is best-effort independently: a missing panel endpoint must not
  // void the board + attempt record.
  const grab = async (path) => {
    try { return await apiJson(path); } catch { return null; }
  };
  const [routingP, placementP, modeP, inletsP, netsP] = await Promise.all([
    grab("/api/routing/params"),
    grab("/api/placement/params"),
    grab("/api/mode"),
    grab("/api/nets/inlets"),
    grab("/api/nets"),
  ]);
  // Nets state comes from the /api/nets rows (the dedicated protected /
  // power GETs are do_POST dead code — the rows render the same server
  // truth). Prefer the live fetch; fall back to the cached table.
  const netsRows = (netsP?.nets)
    ?? ((typeof lastNetsByName !== "undefined" && lastNetsByName.size)
      ? [...lastNetsByName.values()] : []);
  const protectedNets = netsRows.filter(n => n.protected).map(n => n.name);
  const powerOverride = netsRows.filter(n => n.power_user).map(n => n.name);
  return {
    kind: CTX_BUNDLE_KIND,
    ctx_version: CTX_BUNDLE_VERSION,
    saved_at: new Date().toISOString(),
    board: { name: boardName, for_version: lastVersion, pcb_text: pcbText },
    params: {
      routing: routingP?.params ?? null,
      placement: placementP ?? null,
    },
    mode: { mode: modeP?.mode ?? null },
    nets_state: {
      protected: protectedNets,
      power_override: powerOverride,
      selected: [...(typeof selectedNets !== "undefined" ? selectedNets : [])],
    },
    placement_state: {
      pinned: [...(typeof pinnedUuids !== "undefined" ? pinnedUuids : [])],
      selected: [...(typeof selectedUuids !== "undefined" ? selectedUuids : [])],
    },
    inlets: inletsP ? {
      gnd: inletsP.gnd_list ?? inletsP.gnd ?? [],
      power: inletsP.power ?? {},
      bridges: inletsP.bridges ?? [],
    } : null,
    attempt: {
      diag: (typeof routeDiag !== "undefined" && routeDiag?.textContent) || "",
      est: document.getElementById("route-est")?.textContent || "",
      partials: (typeof partialCopperData !== "undefined" ? partialCopperData : []).map(p => ({
        net: p.net, tracks: p.tracks || [], vias: p.vias || [], arcs: p.arcs || [],
      })),
      debug: (typeof costGridDebug !== "undefined" ? costGridDebug : null),
    },
  };
}

async function ctxSaveContext() {
  const diag = (typeof routeDiag !== "undefined") ? routeDiag : null;
  try {
    const bundle = await ctxCollectBundle();
    const text = JSON.stringify(bundle);
    if (text.length > CTX_MAX_BYTES) {
      throw new Error(
        `context is ${(text.length / 1048576).toFixed(1)} MB (cap ${CTX_MAX_BYTES / 1048576} MB) — ` +
        "retry with Debug off so the cost raster is omitted");
    }
    const stem = ctxBoardStem(bundle.board.name);
    const blob = new Blob([text], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${stem}_ctx_v${bundle.board.for_version}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    if (diag) diag.textContent = `context saved (${(text.length / 1024).toFixed(0)} kB) — board + params + attempt overlays`;
    toast("context saved", true);
  } catch (err) {
    if (diag) diag.textContent = `context save failed: ${err.message}`;
    else toast(`context save failed: ${err.message}`);
  }
}

// ---- Load ----

function ctxValidateBundle(bundle) {
  if (!bundle || typeof bundle !== "object") throw new Error("not a JSON object");
  if (bundle.kind !== CTX_BUNDLE_KIND) throw new Error("not a context bundle (bad kind)");
  if (bundle.ctx_version !== CTX_BUNDLE_VERSION) {
    throw new Error(`unsupported context version ${bundle.ctx_version} (want ${CTX_BUNDLE_VERSION})`);
  }
  if (!bundle.board?.pcb_text) throw new Error("bundle has no board bytes");
  return bundle;
}

async function ctxRestoreServerState(bundle) {
  // Params + mode: existing merge-POSTs (placement merges, routing drops
  // unknown keys server-side, mode validates against MODES).
  if (bundle.params?.routing) {
    await postJSON("/api/routing/params", bundle.params.routing).catch(
      err => toast(`routing params restore skipped: ${err.message}`));
  }
  if (bundle.params?.placement) {
    await apiFetch("/api/placement/params", {
      method: "POST", body: JSON.stringify(bundle.params.placement),
    }).catch(err => toast(`placement params restore skipped: ${err.message}`));
  }
  if (bundle.mode?.mode) {
    await postJSON("/api/mode", { mode: bundle.mode.mode }).catch(
      err => toast(`mode restore skipped: ${err.message}`));
  }
}

async function ctxRestoreNetsState(bundle) {
  const st = bundle.nets_state;
  if (!st) return;
  // Protected + power: authoritative diff both directions against the
  // freshly loaded board (which auto-seeds its own defaults on load).
  // Current sets come from /api/nets rows (same server truth).
  let rows = [];
  try {
    rows = (await apiJson("/api/nets")).nets || [];
  } catch (err) { toast(`nets restore skipped: ${err.message}`); return; }
  try {
    const cur = new Set(rows.filter(n => n.protected).map(n => n.name));
    const want = new Set(st.protected || []);
    for (const net of want) {
      if (!cur.has(net)) await postJSON("/api/nets/protected", { net, protected: true });
    }
    for (const net of cur) {
      if (!want.has(net)) await postJSON("/api/nets/protected", { net, protected: false });
    }
  } catch (err) { toast(`protected restore skipped: ${err.message}`); }
  try {
    const cur = new Set(rows.filter(n => n.power_user).map(n => n.name));
    const want = new Set(st.power_override || []);
    for (const net of want) {
      if (!cur.has(net)) await postJSON("/api/nets/power", { net, power: true });
    }
    for (const net of cur) {
      if (!want.has(net)) await postJSON("/api/nets/power", { net, power: false });
    }
  } catch (err) { toast(`power-override restore skipped: ${err.message}`); }
  // Inlets + bridges: one validated POST; stale refs (edited board) 400 —
  // warn and continue, the copper review still stands.
  if (bundle.inlets) {
    try {
      await postJSON("/api/nets/inlets", {
        gnd: bundle.inlets.gnd ?? [],
        power: bundle.inlets.power ?? {},
        bridges: bundle.inlets.bridges ?? [],
      });
    } catch (err) { toast(`inlets restore skipped: ${err.message}`); }
  }
}

async function ctxRestorePlacementState(bundle) {
  // Footprint pins + placement selection. Bundles predating placement_state
  // simply skip this (nothing to restore). Pin toggle flips state, so diff
  // both directions against the freshly loaded board; stale uuids (edited
  // board) are warned and skipped one by one.
  const st = bundle.placement_state;
  if (!st) return;
  const wantPinned = new Set(st.pinned || []);
  const wantSelected = [...(st.selected || [])];
  try {
    const cur = new Set((await apiJson("/api/placement/pinned")).pinned || []);
    for (const uuid of wantPinned) {
      if (!cur.has(uuid)) {
        try { await postJSON("/api/placement/pin", { uuid }); }
        catch (err) { toast(`pin restore skipped for ${uuid}: ${err.message}`); }
      }
    }
    for (const uuid of cur) {
      if (!wantPinned.has(uuid)) {
        try { await postJSON("/api/placement/pin", { uuid }); }
        catch (err) { toast(`unpin restore skipped for ${uuid}: ${err.message}`); }
      }
    }
  } catch (err) { toast(`pins restore skipped: ${err.message}`); }
  try {
    await postJSON("/api/placement/selection", { selected: wantSelected });
  } catch (err) { toast(`placement selection restore skipped: ${err.message}`); }
}

async function ctxLoadContextFile(file) {
  const diag = (typeof routeDiag !== "undefined") ? routeDiag : null;
  try {
    const bundle = ctxValidateBundle(JSON.parse(await file.text()));
    // 1. Board bytes through the normal load path (resets routing state,
    // undo, DRC cache server-side — the replay starts from these bytes).
    // Take the version from the upload response: the global still holds
    // the previous board's version until the next poll.
    const blob = new Blob([bundle.board.pcb_text], { type: "application/octet-stream" });
    const named = new File([blob], ctxBoardStem(bundle.board.name) + ".kicad_pcb");
    const up = await uploadFile(named);
    if (typeof lastVersion !== "undefined" && up?.version) lastVersion = up.version;
    // 2. Server state through existing endpoints.
    await ctxRestoreServerState(bundle);
    await ctxRestoreNetsState(bundle);
    await ctxRestorePlacementState(bundle);
    // 3. Client refresh from the loaded board.
    if (typeof loadParams === "function") await loadParams().catch(() => {});
    if (typeof loadRoutingParams === "function") await loadRoutingParams().catch(() => {});
    await fetchNets();
    // Placement selection/pins restored above: pull server truth into the
    // client sets and repaint rows + SVG highlights.
    if (typeof loadSelectionLight === "function") await loadSelectionLight().catch(() => {});
    if (typeof fetchFootprints === "function") await fetchFootprints().catch(() => {});
    else if (typeof applySelectionUI === "function") applySelectionUI();
    const avail = new Set((typeof lastNetsByName !== "undefined" ? [...lastNetsByName.keys()] : []));
    if (typeof selectedNets !== "undefined") {
      selectedNets = new Set((bundle.nets_state?.selected || []).filter(n => avail.has(n)));
    }
    if (typeof updateNetSelectionUI === "function") updateNetSelectionUI();
    if (typeof lastVersion !== "undefined" && lastVersion) {
      await fetchAndInjectSVG(lastVersion);
    }
    // 4. View-only replay through existing renderers (never writes copper).
    // The cost raster only paints when the overlay flag is on — a fresh
    // GUI has the box unticked, so tick it here like the user would.
    if (bundle.attempt?.debug) {
      const cgToggle = document.getElementById("cost-grid-toggle");
      if (cgToggle) cgToggle.checked = true;
      if (typeof costGridOverlay !== "undefined") costGridOverlay = true;
    }
    const partials = bundle.attempt?.partials || [];
    if (typeof partialCopperData !== "undefined") {
      partialCopperData = partials.map(p => ({ net: p.net, tracks: p.tracks || [], vias: p.vias || [], arcs: p.arcs || [] }));
      if (typeof renderPartialCopperOverlay === "function") renderPartialCopperOverlay();
    }
    if (bundle.attempt?.debug && typeof showCostGridDebug === "function") {
      showCostGridDebug(bundle.attempt.debug);
    }
    const nTracks = partials.reduce((n, p) => n + (p.tracks || []).length + (p.arcs || []).length, 0);
    const savedDiag = bundle.attempt?.diag || "";
    if (diag) {
      diag.textContent = `[context ${bundle.board.name} v${bundle.board.for_version}` +
        ` — review only, re-route to rebuild] ` +
        (savedDiag || `${partials.length} partial net(s), ${nTracks} partial tracks`);
      diag.title = diag.textContent;
    }
    toast(`context loaded: ${partials.length} partial net(s), ${nTracks} partial tracks (review only)`, true);
    // Land on the Route tab so the diag + overlays are actually visible.
    if (typeof setActivePanel === "function") setActivePanel("route");
  } catch (err) {
    if (diag) diag.textContent = `context load failed: ${err.message}`;
    else toast(`context load failed: ${err.message}`);
  }
}

on("ctx-save-btn", "click", ctxSaveContext);
on("ctx-load-btn", "click", () => document.getElementById("ctx-load-input")?.click());
on("ctx-load-input", "change", async (e) => {
  const f = e.target.files?.[0];
  e.target.value = "";
  if (f) await ctxLoadContextFile(f);
});
