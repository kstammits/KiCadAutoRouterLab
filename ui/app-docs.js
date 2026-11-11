// app-docs.js — manual viewer (markdown render + /api/docs/* loading).

function renderInline(s) {
  let out = escapeHtml(s);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return out;
}

function renderMarkdown(md) {
  // Minimal subset: headings, fenced code, - lists, tables (as tables),
  // paragraphs. Anything else falls back to escaped paragraphs.
  const lines = md.split("\n");
  let html = "";
  let inCode = false;
  let inList = false;
  const flushList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      flushList();
      html += inCode ? "</code></pre>" : "<pre><code>";
      inCode = !inCode;
      continue;
    }
    if (inCode) { html += escapeHtml(line) + "\n"; continue; }
    const h = line.match(/^(#{1,3})\s+(.*)/);
    if (h) {
      flushList();
      const lvl = h[1].length;
      html += `<h${lvl}>${renderInline(h[2])}</h${lvl}>`;
      continue;
    }
    // GFM table row: | a | b |  (header + --- separator + rows)
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushList();
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map(c => `<td>${renderInline(c.trim())}</td>`).join("");
      html += `<table><tr>${cells(line).replace(/<td>/g, "<th>").replace(/<\/td>/g, "</th>")}</tr>`;
      i += 2; // skip separator, then consume body rows
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        html += `<tr>${cells(lines[i])}</tr>`;
        i++;
      }
      html += "</table>";
      i--;
      continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${renderInline(li[1])}</li>`;
      continue;
    }
    if (!line.trim()) { flushList(); continue; }
    flushList();
    html += `<p>${renderInline(line)}</p>`;
  }
  flushList();
  if (inCode) html += "</code></pre>";
  return html;
}

let docsLoaded = false;
let docsByName = {};

// User-facing guides vs dev references, so first-time evaluators know
// what to read. Mirrors the Learn More table in README.md.
function docsKind(name) {
  if (name === "README.md" || name === "docs/autorouting_glossary.md") return "user guide";
  return "dev reference";
}

function stripDocsFrontMatter(md) {
  if (!md.startsWith("---\n") && !md.startsWith("---\r\n")) return md;
  const lines = md.split("\n");
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") return lines.slice(i + 1).join("\n");
  }
  return md;
}

async function loadDocsList() {
  const sel = docsSelect;
  const diag = el("docs-diag");
  if (docsLoaded && sel.options.length) { loadDocContent(sel.value); return; }
  try {
    const data = await apiJson("/api/docs/list");
    sel.innerHTML = "";
    docsByName = {};
    for (const d of (data.docs || [])) {
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = `${d.title} [${docsKind(d.name)}] (${d.name})`;
      sel.appendChild(opt);
      docsByName[d.name] = d;
    }
    docsLoaded = true;
    if (sel.options.length) {
      sel.value = [...sel.options].some(o => o.value === "README.md") ? "README.md" : sel.options[0].value;
      await loadDocContent(sel.value);
    }
  } catch (err) {
    diag.textContent = `docs failed: ${err.message}`;
  }
}

async function loadDocContent(name) {
  const docEl = el("docs-content");
  const diag = el("docs-diag");
  if (!name) return;
  if (diag) diag.textContent = "loading…";
  try {
    const data = await apiJson(`/api/docs/content?name=${encodeURIComponent(name)}`);
    if (docEl) docEl.innerHTML = renderMarkdown(stripDocsFrontMatter(data.markdown || ""));
    const rl = el("docs-rendered-link");
    if (rl) {
      if (data.rendered_url) {
        rl.style.display = "";
        rl.href = data.rendered_url;
        rl.textContent = data.rendered_stale ? "rendered (stale) ↗" : "rendered ↗";
        rl.title = data.rendered_stale
          ? "Pre-rendered HTML is older than the source — re-run scripts/regen_*.sh"
          : "Pre-rendered HTML with figures and math";
      } else {
        rl.style.display = "none";
      }
    }
    // Rewrite relative .md links to stay inside the allowlisted viewer
    docEl?.querySelectorAll("a").forEach(a => {
      const href = a.getAttribute("href") || "";
      if (/^[^:/?#]+\.md(#.*)?$/i.test(href) || href.startsWith("docs/")) {
        const target = href.split("#")[0].replace(/^\.\//, "");
        a.addEventListener("click", (e) => {
          e.preventDefault();
          const sel = docsSelect;
          if (!sel) return;
          const match = [...sel.options].find(o => o.value === target || o.value.endsWith("/" + target) || target.endsWith(o.value));
          if (match) { sel.value = match.value; loadDocContent(match.value); }
        });
      }
    });
    if (diag) diag.textContent = data.truncated ? "truncated (size cap)" : `${data.title} · ${docsKind(name)}`;
  } catch (err) {
    if (diag) diag.textContent = `docs failed: ${err.message}`;
  }
}

docsSelect?.addEventListener("change", (e) => {
  loadDocContent(e.target.value);
});

on("docs-open-tab-btn", "click", () => {
  const name = docsSelect?.value;
  if (name) window.open(`/docs.html?doc=${encodeURIComponent(name)}`, "_blank", "noopener");
});
