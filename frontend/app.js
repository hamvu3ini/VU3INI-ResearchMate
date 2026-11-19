// VU3INI ResearchMate — browser front-end (no CDN, no framework; ES modules served locally)
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const panel = () => $("#panel");

const S = {
  steps: [], checklist: [], csl: {}, projects: [], pid: null, project: null, step: 1, sid: null,
  settings: {}, hw: null, matrixComplete: false, cache: {},
};

// ------------------------------------------------------------------ API + UI helpers
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status} ${r.statusText}`);
  return data;
}
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.className = "toast"), err ? 6500 : 3200);
}
async function busy(btn, fn) {
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Working…`;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.innerHTML = old; }
}
const tag = (status) => {
  const cls = { supported: "ok", verified: "ok", "close-paraphrase": "warn", partial: "warn", suspicious: "warn",
    "unverified-offline": "warn", unsupported: "err", mismatch: "err", "not-found": "err", verbatim: "err",
    error: "err", warning: "warn", suggestion: "", info: "" }[status] ?? "";
  return `<span class="tag ${cls}">${esc(status)}</span>`;
};
const engineTag = (r) => r ? `<span class="tag ${r.engine === "native" ? "ok" : ""}" title="native = vendored tool ran; builtin = ResearchMate's offline implementation">${esc(r.tool || "")} · ${esc(r.engine)}</span>` : "";
const note = (r) => (r && r.note ? `<div class="notice small">${esc(r.note)}</div>` : "");
const words = (t) => (t || "").trim().split(/\s+/).filter(Boolean).length;

// ------------------------------------------------------------------ header: theme, network, hardware, about
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#theme-btn").textContent = t === "dark" ? "☀" : "☾";
  $("#theme-btn").title = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  try { localStorage.setItem("rm-theme", t); } catch (e) {}
}
$("#theme-btn").addEventListener("click", async () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(t);
  api("/api/settings", { method: "PUT", body: { theme: t } }).catch(() => {});
});

async function pollNetwork() {
  try {
    const { online } = await api("/api/network");
    const p = $("#net-pill");
    p.className = "pill " + (online ? "online" : "offline");
    p.textContent = online ? "● Online" : "● Offline";
    p.title = online ? "Internet reachable — RefChecker, Zotero sync and CSL fetches available"
                     : "Offline — all local tools work; online reference verification is paused";
  } catch (e) { /* server restarting */ }
}

const MODE_HELP = {
  auto: "Benchmarks this PC and picks GPU, hybrid layer-split or CPU automatically.",
  gpu: "Everything supported on the GPU (n_gpu_layers = -1). VRAM guard warns before OOM.",
  cpu: "CUDA hidden, 0 GPU layers — leaves the GPU free for other applications.",
  hybrid: "Splits model layers between VRAM and RAM and runs GPU + CPU task lanes in parallel.",
};
function renderHw(snap) {
  S.hw = snap;
  const plan = snap.plan;
  $("#hw-label").textContent = plan.label;
  $("#hw-modes").innerHTML = Object.entries(snap.modes).map(([k, v]) =>
    `<label class="mode"><input type="radio" name="hwmode" value="${k}" ${snap.mode === k ? "checked" : ""}>
     <span><strong>${esc(v)}</strong><small>${esc(MODE_HELP[k])}</small></span></label>`).join("");
  $("#hw-size").value = plan.model_size_gb;
  $("#hw-layers").value = plan.total_layers;
  const ngl = $("#hw-ngl");
  ngl.max = plan.total_layers;
  ngl.value = plan.n_gpu_layers < 0 ? plan.total_layers : plan.n_gpu_layers;
  $("#hw-ngl-out").textContent = plan.n_gpu_layers < 0 ? "all" : plan.n_gpu_layers;
  $("#hw-slider-wrap").style.display = plan.effective === "hybrid" || snap.mode === "hybrid" ? "" : "none";
  $("#hw-reason").textContent = plan.reason;
  $("#hw-warn").innerHTML = (plan.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  $$("input[name=hwmode]").forEach((r) => r.addEventListener("change", () => setMode(r.value)));
}
async function setMode(mode, extra = {}) {
  try {
    const snap = await api("/api/hardware/mode", { body: { mode, model_size_gb: +$("#hw-size").value || undefined,
      model_layers: +$("#hw-layers").value || undefined, ...extra } });
    renderHw(snap);
    toast(`Compute: ${snap.plan.label}`);
  } catch (e) { toast(e.message, true); }
}
$("#hw-pill").addEventListener("click", (e) => {
  const m = $("#hw-menu"); m.hidden = !m.hidden; e.currentTarget.setAttribute("aria-expanded", String(!m.hidden));
});
document.addEventListener("click", (e) => { if (!e.target.closest(".hw-wrap")) $("#hw-menu").hidden = true; });
$("#hw-ngl").addEventListener("input", (e) => ($("#hw-ngl-out").textContent = e.target.value));
$("#hw-ngl").addEventListener("change", (e) => setMode(S.hw.mode === "auto" ? "hybrid" : S.hw.mode, { n_gpu_layers: +e.target.value }));
$("#hw-ngl-auto").addEventListener("click", () => setMode(S.hw.mode));
$("#hw-size").addEventListener("change", () => setMode(S.hw.mode));
$("#hw-layers").addEventListener("change", () => setMode(S.hw.mode));
$("#hw-reprobe").addEventListener("click", () => setMode(S.hw.mode, { reprobe: true }));

async function pollTelemetry() {
  try {
    const t = await api("/api/hardware/telemetry");
    const gpu = (t.gpus || []).map((g) => `GPU ${Math.round(g.util)}% · ${g.vram_used_gb}/${g.vram_total_gb} GB`).join(" | ");
    $("#hw-tele").textContent = `CPU ${Math.round(t.cpu_percent)}% · RAM ${t.ram_used_gb}/${t.ram_total_gb} GB${gpu ? " · " + gpu : ""}`;
  } catch (e) { /* ignore */ }
}

$("#help-btn").addEventListener("click", openAbout);
$("#about-close").addEventListener("click", () => $("#about-dialog").close());
async function openAbout() {
  const d = $("#about-dialog");
  $("#about-body").innerHTML = `<div class="empty"><span class="spin"></span></div>`;
  d.showModal();
  try {
    const a = await api("/api/about");
    const hw = a.hardware;
    const gpus = hw.gpus.length ? hw.gpus.map((g) => `${esc(g.name)} — ${g.vram_total_gb} GB (${g.backend}${g.integrated ? ", integrated" : ""})`).join("<br>") : "None detected";
    $("#about-body").innerHTML = `
      <h3>Hardware</h3>
      <table><tr><th>CPU</th><td>${esc(hw.cpu_model)} — ${hw.physical_cores} cores / ${hw.logical_cores} threads ${hw.avx512 ? "· AVX-512" : hw.avx2 ? "· AVX2" : ""}</td></tr>
      <tr><th>RAM</th><td>${hw.ram_total_gb} GB (${hw.ram_available_gb} GB free)</td></tr>
      <tr><th>GPU(s)</th><td>${gpus}</td></tr>
      <tr><th>Active profile</th><td><strong>${esc(a.plan.label)}</strong><br><span class="small muted">${esc(a.plan.reason)}</span></td></tr>
      <tr><th>OS / Python</th><td>${esc(a.os)} · Python ${esc(a.python)}</td></tr>
      <tr><th>Network</th><td>${a.online ? "Online" : "Offline"}</td></tr>
      <tr><th>Data folder</th><td><code>${esc(a.data_dir)}</code></td></tr></table>
      <h3 style="margin-top:1rem">Bundled tools</h3>
      <table><tr><th>Tool</th><th>Engine</th><th>Version / status</th></tr>
      ${a.tools.map((t) => `<tr><td><strong>${esc(t.name)}</strong><br><span class="small muted">${esc(t.purpose)}</span></td>
        <td>${t.native_available ? '<span class="tag ok">native</span>' : '<span class="tag">built-in</span>'}</td>
        <td class="small">${esc(t.native_version || "offline fallback active")}</td></tr>`).join("")}</table>
      <p class="small muted" style="margin-top:1rem">“native” means the vendored tool (binary, JAR, service or package) is present and used.
      “built-in” means ResearchMate’s own offline implementation of the same check is used instead.</p>`;
  } catch (e) { $("#about-body").innerHTML = `<div class="notice err">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ projects + steps nav
async function loadProjects(selectId) {
  S.projects = (await api("/api/projects")).projects;
  const sel = $("#project-select");
  sel.innerHTML = S.projects.length ? S.projects.map((p) => `<option value="${p.id}">${esc(p.title)}</option>`).join("")
                                    : `<option value="">No papers yet</option>`;
  const id = selectId || S.pid || (S.projects[0] && S.projects[0].id);
  if (id) { sel.value = id; await openProject(+id); } else { S.project = null; renderSteps(); renderWelcome(); }
}
$("#project-select").addEventListener("change", (e) => e.target.value && openProject(+e.target.value));
$("#new-project-btn").addEventListener("click", newProject);
async function newProject() {
  const title = prompt("Working title of the new paper:");
  if (!title) return;
  const p = await api("/api/projects", { body: { title } });
  S.step = 1;
  await loadProjects(p.id);
}
async function openProject(pid) {
  S.pid = pid;
  try { localStorage.setItem("rm-pid", pid); } catch (e) {}
  await refreshProject();
  if (!S.sid || !S.project.sections.some((s) => s.id === S.sid)) S.sid = S.project.sections[0]?.id ?? null;
  showStep(S.step);
}
async function refreshProject() {
  S.project = await api(`/api/projects/${S.pid}`);
  const written = S.project.matrix.filter((r) => r.words);
  S.matrixComplete = written.length > 0 && written.every((r) => r.complete);
  renderSteps();
}
function renderSteps() {
  const done = new Set(S.project?.steps_done || []);
  const groups = { 1: "Plan & sources", 4: "Section loop (repeat per section)", 10: "Whole paper", 11: "Global checks", 19: "Finish" };
  $("#steps").innerHTML = S.steps.map((st) => `${groups[st.n] ? `<div class="group">${groups[st.n]}</div>` : ""}
    <button class="step ${done.has(st.n) ? "done" : ""} ${S.step === st.n ? "active" : ""}" data-n="${st.n}">
      <span class="num">${done.has(st.n) ? "✓" : st.n}</span><span class="label">${esc(st.title)}</span>
      <span class="lock">${st.global && !S.matrixComplete ? "🔒" : ""}</span></button>`).join("");
  $$("#steps .step").forEach((b) => b.addEventListener("click", () => showStep(+b.dataset.n)));
}
function renderWelcome() {
  panel().innerHTML = `<div class="card" style="max-width:640px;margin:3rem auto;text-align:center">
    <img src="/assets/logo.png" alt="" style="width:120px;border-radius:16px" onerror="this.src='/assets/logo-fallback.svg'">
    <h2>Welcome to VU3INI ResearchMate</h2>
    <p class="muted">A 21-step, human-in-the-loop research-paper workflow running entirely on your PC.</p>
    <button class="btn" id="welcome-new">Start a new paper</button></div>`;
  $("#welcome-new").addEventListener("click", newProject);
}

function head(st) {
  return `<div class="panel-head"><div class="stepno">${st.n}</div><div><h2>${esc(st.title)}</h2><p>${esc(st.desc || "")}</p></div></div>
  ${st.global && !S.matrixComplete ? `<div class="notice warn">🔒 Global checks unlock after Step 10 — every written section must be edited, grounded and have no pending revisions. <a href="#" data-goto="10">Open the completion matrix</a></div>` : ""}
  ${st.options ? `<div class="explainer-title">What each tool option does</div><div class="explainer">${st.options.map((o) =>
    `<div class="opt"><h4>${esc(o.tool)}</h4><p>${esc(o.does)}</p><p class="muted"><strong>Best for:</strong> ${esc(o.best)}</p></div>`).join("")}</div>` : ""}`;
}
function sectionPicker() {
  return `<label class="field" style="max-width:360px">Section
    <select id="sec-pick">${S.project.sections.map((s) => `<option value="${s.id}" ${s.id === S.sid ? "selected" : ""}>${esc(s.name)} (${words(s.content)} words)</option>`).join("")}</select></label>`;
}
const curSection = () => S.project.sections.find((s) => s.id === S.sid);
function bindCommon() {
  $("#sec-pick")?.addEventListener("change", (e) => { S.sid = +e.target.value; showStep(S.step); });
  $$("[data-goto]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showStep(+a.dataset.goto); }));
}
async function showStep(n) {
  S.step = n;
  if (!S.project) return renderWelcome();
  renderSteps();
  const st = S.steps.find((x) => x.n === n);
  try { await (PANELS[st.panel] || (() => {}))(st); } catch (e) { panel().innerHTML = head(st) + `<div class="notice err">${esc(e.message)}</div>`; }
  bindCommon();
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ shared renderers
function issuesList(issues, content, sectionId) {
  if (!issues.length) return `<p class="muted">✓ No issues found.</p>`;
  return issues.slice(0, 200).map((i, k) => {
    const ctx = content ? content.slice(Math.max(0, i.start - 30), Math.min(content.length, i.end + 30)) : "";
    const rep = (i.replacements || [])[0];
    return `<div class="issue ${esc(i.severity)}"><strong>${esc(i.message)}</strong> <span class="tag">${esc(i.rule)}</span>
      ${ctx ? `<div class="ctx">…${esc(ctx)}…</div>` : ""}
      ${rep !== undefined && rep !== null && sectionId ? `<button class="btn sm ghost" data-fix="${sectionId}:${i.start}:${i.end}:${k}" data-rep="${esc(rep)}">Apply “${esc(rep || "(delete)")}”</button>` : ""}</div>`;
  }).join("") + (issues.length > 200 ? `<p class="muted">…and ${issues.length - 200} more.</p>` : "");
}
function bindFixes(rerun) {
  $$("[data-fix]").forEach((b) => b.addEventListener("click", async () => {
    const [sid, start, end] = b.dataset.fix.split(":").map(Number);
    const sec = S.project.sections.find((s) => s.id === sid);
    const content = sec.content.slice(0, start) + b.dataset.rep + sec.content.slice(end);
    await api(`/api/sections/${sid}`, { method: "PUT", body: { content } });
    await refreshProject();
    toast("Fix applied");
    rerun();
  }));
}
function highlight(text, ranges) {
  const rs = [...ranges].sort((a, b) => a.start - b.start);
  let out = "", pos = 0;
  for (const r of rs) {
    if (r.start < pos) continue;
    out += esc(text.slice(pos, r.start)) + `<mark class="${r.cls || ""}" title="${esc(r.title || "")}">${esc(text.slice(r.start, r.end))}</mark>`;
    pos = r.end;
  }
  return out + esc(text.slice(pos));
}

// ------------------------------------------------------------------ Step 2: Zotero + Better BibTeX live sync (auto-provisioned)
const Z_STAGES = [["checking", "Find Zotero"], ["installing", "Install Zotero (silent)"], ["starting", "Start Zotero"],
  ["bbt", "Install Better BibTeX add-on"], ["indexing", "Better BibTeX ready"], ["ready", "Live sync ready"]];
async function zoteroCard() {
  clearTimeout(S.zTimer);
  const card = () => $("#z-card");
  if (!card()) return;
  let st;
  try { st = await api("/api/zotero/setup/status"); } catch (e) { card().innerHTML = `<h3>Zotero + Better BibTeX</h3><div class="notice err">${esc(e.message)}</div>`; return; }
  if (st.ready) return zoteroReady();
  card().innerHTML = `<h3>Zotero + Better BibTeX</h3>
    <p class="small muted">Live sync needs Zotero running with the Better BibTeX add-on. ResearchMate is setting this up for you:
    install Zotero if missing → start it → add Better BibTeX → wait until it is ready.</p>
    <ol class="small" id="z-stages"></ol><div class="meter" style="margin:.5rem 0"><span id="z-bar" style="width:0%"></span></div>
    <p class="small" id="z-msg">Checking…</p>
    <div class="row"><button class="btn sm ghost" id="z-retry" hidden>Retry</button>
    <span class="small muted">Meanwhile you can import a .bib file on the right.</span></div>`;
  const paint = (s) => {
    const j = s.job, idx = Z_STAGES.findIndex(([k]) => k === (j.stage === "error" ? j.failed_stage : j.stage));
    if (!$("#z-stages")) return;
    $("#z-stages").innerHTML = Z_STAGES.map(([k, label], i) => {
      const mark = j.stage === "error" && i === Math.max(idx, 0) ? "✗" : i < idx || j.stage === "ready" ? "✓" : i === idx ? "…" : "○";
      const note = k === "installing" && i < idx && !j.log.some((l) => l.startsWith("Zotero not found")) ? " <span class='muted'>(already installed)</span>"
        : k === "bbt" && i < idx && !j.log.some((l) => l.startsWith("Closing Zotero")) ? " <span class='muted'>(already installed)</span>" : "";
      return `<li>${mark} ${esc(label)}${note}</li>`;
    }).join("");
    $("#z-bar").style.width = (j.progress < 0 ? 100 : Math.round(j.progress * 100)) + "%";
    $("#z-bar").style.opacity = j.progress < 0 ? ".45" : "1";
    $("#z-msg").innerHTML = j.error ? `<span style="color:var(--err)">${esc(j.error)}</span>` : esc(j.message || "");
    $("#z-retry").hidden = j.stage !== "error";
  };
  const poll = async () => {
    if (S.step !== 2 || !$("#z-stages")) return;
    try {
      const s = await api("/api/zotero/setup/status");
      paint(s);
      if (s.ready || s.job.stage === "ready") { toast("Zotero + Better BibTeX ready"); return zoteroReady(); }
      if (s.job.stage === "error") return;
    } catch (e) { /* busy */ }
    S.zTimer = setTimeout(poll, 1500);
  };
  const begin = async () => {
    try { paint(await api("/api/zotero/setup", { body: {} })); } catch (e) { toast(e.message, true); }
    poll();
  };
  $("#z-retry").addEventListener("click", begin);
  paint(st);
  if (!st.job.done && st.job.stage !== "idle") poll(); else begin();
}
async function zoteroReady() {
  const c = $("#z-card");
  if (!c) return;
  const [{ collections }, last] = await Promise.all([api("/api/zotero/collections").catch(() => ({ collections: [] })),
    api(`/api/projects/${S.pid}/bib/zotero`).catch(() => ({ collection: "" }))]);
  c.innerHTML = `<h3>Zotero + Better BibTeX <span class="tag ok">running</span></h3>
    <label class="field">Collection${collections.length ? `<select id="z-col"><option value="">Whole library</option>${collections.map((x) =>
      `<option value="${esc(x.path)}" ${x.path === last.collection ? "selected" : ""}>${esc(x.path)}</option>`).join("")}</select>`
      : `<input id="z-col" value="${esc(last.collection)}" placeholder="Collection path, e.g. PhD/Chapter 2 (blank = whole library)">`}</label>
    <div class="row"><button class="btn" id="z-sync">Sync from Zotero</button>
    <span class="small muted">Stable Better BibTeX citation keys; re-sync any time you add papers.</span></div>`;
  $("#z-sync").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await api(`/api/projects/${S.pid}/bib/zotero`, { body: { collection: $("#z-col").value } });
    toast(`Synced ${r.entries} entries from Zotero`); await refreshProject(); showStep(2);
  }));
}

// ------------------------------------------------------------------ the 21 panels
const PANELS = {
  async outline(st) {
    const p = S.project;
    panel().innerHTML = head(st) + `<div class="notice">✍️ Generative AI is disabled in this step: the question, hypothesis and outline must be your own words.</div>
    <div class="card"><label class="field">Paper title<input id="o-title" value="${esc(p.title)}"></label>
      <label class="field">Research question<textarea id="o-q" rows="2">${esc(p.question)}</textarea></label>
      <label class="field">Hypothesis<textarea id="o-h" rows="2">${esc(p.hypothesis)}</textarea></label>
      <label class="field">Outline (one point per line)<textarea id="o-o" rows="9">${esc(p.outline)}</textarea></label>
      <label class="field" style="max-width:320px">Target journal citation style (CSL)<select id="o-csl">${Object.entries(S.csl).map(([k, v]) => `<option value="${k}" ${p.target_csl === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <button class="btn" id="o-save">Save outline</button></div>
    <div class="card"><h3>Sections</h3><p class="muted small">${p.sections.map((s) => esc(s.name)).join(" · ")}</p>
      <div class="row"><input id="o-sec" placeholder="Add a section, e.g. Related Work"><button class="btn ghost" id="o-add">Add section</button></div></div>`;
    $("#o-save").addEventListener("click", (e) => busy(e.target, async () => {
      await api(`/api/projects/${S.pid}/outline`, { method: "PUT", body: { title: $("#o-title").value, question: $("#o-q").value,
        hypothesis: $("#o-h").value, outline: $("#o-o").value, target_csl: $("#o-csl").value } });
      await refreshProject(); await loadProjects(S.pid); toast("Outline saved");
    }));
    $("#o-add").addEventListener("click", async () => {
      if (!$("#o-sec").value.trim()) return;
      await api(`/api/projects/${S.pid}/sections`, { body: { name: $("#o-sec").value } });
      await refreshProject(); showStep(1);
    });
  },

  async sources(st) {
    const bib = await api(`/api/projects/${S.pid}/bib`);
    panel().innerHTML = head(st) + `<div class="grid2">
      <div class="card" id="z-card"><h3>Zotero + Better BibTeX</h3><p class="muted small"><span class="spin"></span> Checking Zotero…</p></div>
      <div class="card"><h3>Import .bib</h3>
        <input type="file" id="b-file" accept=".bib,.txt"><textarea id="b-text" rows="5" class="mono" placeholder="…or paste BibTeX here"></textarea>
        <div class="row" style="margin-top:.5rem"><label class="small"><input type="checkbox" id="b-append"> Append to existing</label><span class="spacer"></span><button class="btn" id="b-import">Import</button></div></div></div>
    <div class="card"><h3>Bibliography (${bib.entries.length})</h3>${bib.entries.length ? `<table><tr><th>Key</th><th>Title</th><th>Year</th></tr>${bib.entries.map((e) => `<tr><td><code>${esc(e.key)}</code></td><td>${esc(e.title)}</td><td>${esc(e.year)}</td></tr>`).join("")}</table>` : `<p class="muted">No entries yet.</p>`}</div>
    <div class="card"><h3>Source PDFs</h3><div class="row"><input type="file" id="s-files" multiple accept=".pdf,.txt,.md"><button class="btn" id="s-up">Upload</button></div>
      <table style="margin-top:.6rem"><tr><th>File</th><th>Parsed by</th><th>Indexed</th><th></th></tr>
      ${S.project.sources.map((s) => `<tr><td>${esc(s.filename)}</td><td>${esc(s.parser || "—")}</td><td>${s.indexed ? "✓" : "—"}</td><td><button class="btn sm ghost" data-del="${s.id}">Remove</button></td></tr>`).join("")}</table></div>`;
    $("#b-file").addEventListener("change", async (e) => { $("#b-text").value = await e.target.files[0].text(); });
    $("#b-import").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/bib/import`, { body: { bib: $("#b-text").value, append: $("#b-append").checked } });
      toast(`${r.entries} entries${r.errors.length ? ` · ${r.errors.length} parse problems (see Step 15)` : ""}`);
      await refreshProject(); showStep(2);
    }));
    zoteroCard();
    $("#s-up").addEventListener("click", (e) => busy(e.target, async () => {
      const fd = new FormData();
      [...$("#s-files").files].forEach((f) => fd.append("files", f));
      if (![...fd.keys()].length) throw new Error("Choose files first");
      await api(`/api/projects/${S.pid}/sources`, { body: fd }); await refreshProject(); showStep(2);
    }));
    $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/projects/${S.pid}/sources/${b.dataset.del}`, { method: "DELETE" }); await refreshProject(); showStep(2);
    }));
  },

  async index(st) {
    panel().innerHTML = head(st) + `<div class="card"><p>${S.project.sources.length} source file(s). Parsing runs on CPU workers in parallel;
      embeddings use <strong>${esc(S.hw?.plan.embed_device || "cpu")}</strong>.</p><button class="btn" id="i-run">Parse & index all sources</button><div id="i-out"></div></div>`;
    $("#i-run").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/index`, { body: {} });
      $("#i-out").innerHTML = `<p>${engineTag(r.index)} ${r.index.passages} passages indexed from ${r.index.sources} sources (device: ${esc(r.index.device)}).</p>
        <table><tr><th>Source</th><th>Engine</th><th>Sections</th><th>Note</th></tr>${r.parsed.map((p) => `<tr><td>#${p.id}</td><td>${esc(p.engine)}</td><td>${p.sections}</td><td class="small">${esc(p.note || p.error || "")}</td></tr>`).join("")}</table>`;
      await refreshProject();
    }));
  },

  async draft(st) {
    const sec = curSection();
    clearTimeout(S.setupTimer);
    let models = { models: [], backend: null, default_model: "llama3.1:8b" };
    let setup = null;
    try { [models, setup] = await Promise.all([api("/api/llm/models"), api("/api/llm/setup/status")]); } catch (e) {}
    const ready = models.backend === "llamacpp" || (setup && setup.ready);
    const preferred = S.settings.llm_model || models.default_model;
    panel().innerHTML = head(st) + sectionPicker() + (ready ? "" : `<div class="card" id="llm-setup">
        <h3>Setting up the local LLM</h3>
        <p class="small muted">Step 4 needs <strong>Ollama</strong> with <code>${esc(setup?.default_model || "llama3.1:8b")}</code>. ResearchMate is doing this for you:
        install Ollama if missing → <code>ollama serve</code> → <code>ollama pull ${esc(setup?.default_model || "llama3.1:8b")}</code> (≈ 4.9 GB, first time only).</p>
        <ol class="small" id="llm-stages"></ol>
        <div class="meter" style="margin:.5rem 0"><span id="llm-bar" style="width:0%"></span></div>
        <p class="small" id="llm-msg">Checking…</p>
        <div class="row"><button class="btn sm ghost" id="llm-retry" hidden>Retry setup</button>
        <span class="small muted">You can also skip drafting and write the section yourself in <a href="#" data-goto="5">Step 5</a>.</span></div></div>`) +
      `<div class="card">
      ${ready ? `<p>Local LLM: <span class="tag ok">${esc(models.backend)}</span> · compute: <strong>${esc(S.hw?.plan.label)}</strong></p>` : ""}
      <label class="field" style="max-width:360px">Model<select id="d-model">${models.models.map((m) => `<option ${m === preferred || (!S.settings.llm_model && m.startsWith(preferred)) ? "selected" : ""}>${esc(m)}</option>`).join("") || "<option value=''>—</option>"}</select></label>
      <label class="field">Your instructions for this section<textarea id="d-ins" rows="3" placeholder="Key points, length, tone…"></textarea></label>
      <button class="btn" id="d-go" ${ready ? "" : "disabled"}>Draft “${esc(sec?.name)}”</button>
      ${sec?.content ? `<p class="small muted">Drafting replaces the current ${words(sec.content)} words of this section.</p>` : ""}</div><div id="d-out"></div>`;
    if (!ready) {
      const STAGES = [["checking", "Check for Ollama"], ["installing", "Install Ollama (silent, per-user)"],
        ["starting", "Start ollama serve"], ["pulling", `Pull ${setup?.default_model || "llama3.1:8b"}`], ["ready", "Ready to draft"]];
      const paint = (s) => {
        const j = s.job, idx = STAGES.findIndex(([k]) => k === (j.stage === "error" ? j.failed_stage : j.stage));
        $("#llm-stages").innerHTML = STAGES.map(([k, label], i) => {
          const skipped = k === "installing" && i < idx && !j.log.some((l) => l.startsWith("Ollama not found"));
          const mark = j.stage === "error" && i === Math.max(idx, 0) ? "✗" : i < idx || j.stage === "ready" ? "✓" : i === idx ? "…" : "○";
          const bundled = k === "installing" && j.log.some((l) => l.startsWith("Using the Ollama bundled"));
          return `<li>${mark} ${esc(label)}${bundled ? " <span class='muted'>(bundled with ResearchMate)</span>" : skipped ? " <span class='muted'>(already installed)</span>" : ""}</li>`;
        }).join("");
        const pct = j.progress < 0 ? 100 : Math.round(j.progress * 100);
        $("#llm-bar").style.width = pct + "%";
        $("#llm-bar").style.opacity = j.progress < 0 ? ".45" : "1";
        $("#llm-msg").innerHTML = j.error ? `<span style="color:var(--err)">${esc(j.error)}</span>` : esc(j.message || "");
        $("#llm-retry").hidden = j.stage !== "error";
      };
      const poll = async () => {
        if (S.step !== 4 || !$("#llm-setup")) return;
        try {
          const s = await api("/api/llm/setup/status");
          paint(s);
          if (s.ready || s.job.stage === "ready") { toast(`Local LLM ready — ${s.default_model}`); S.settings.llm_model ||= s.default_model; return showStep(4); }
          if (s.job.stage === "error") return;
        } catch (e) { /* server busy */ }
        S.setupTimer = setTimeout(poll, 1500);
      };
      const begin = async () => {
        try { paint(await api("/api/llm/setup", { body: { model: setup?.default_model || "llama3.1:8b" } })); } catch (e) { toast(e.message, true); }
        poll();
      };
      $("#llm-retry").addEventListener("click", begin);
      if (setup) paint(setup);
      if (setup && !setup.job.done && setup.job.stage !== "idle") poll(); else begin();
    }
    $("#d-go").addEventListener("click", (e) => busy(e.target, async () => {
      if (sec?.content && !confirm("Replace the current section text with a new AI draft?")) return;
      const model = $("#d-model").value;
      if (model) api("/api/settings", { method: "PUT", body: { llm_model: model } });
      const r = await api(`/api/sections/${S.sid}/draft`, { body: { instructions: $("#d-ins").value, model } });
      await refreshProject();
      $("#d-out").innerHTML = `<div class="card"><p class="small muted">Logged to AI-Use Log · ${esc(r.model)} · ${esc(r.hardware)} · ${r.seconds}s</p>
        <div class="mirror">${esc(r.section.content)}</div><p><a href="#" data-goto="5">Continue to Step 5 — edit it yourself →</a></p></div>`;
      bindCommon();
    }));
  },

  async edit(st) {
    const sec = curSection();
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card">
      <div class="row small muted"><span id="e-wc">${words(sec.content)} words</span><span class="spacer"></span>
        <span>Human edit ratio</span><div class="meter" style="width:160px"><span style="width:${Math.round(sec.human_edit_ratio * 100)}%"></span></div><strong>${Math.round(sec.human_edit_ratio * 100)}%</strong></div>
      <textarea class="editor" id="e-text">${esc(sec.content)}</textarea>
      <div class="row"><button class="btn" id="e-save">Save my edits</button><span class="muted small">Cite with Pandoc keys, e.g. [@smith2020].</span></div></div>`;
    $("#e-text").addEventListener("input", (e) => ($("#e-wc").textContent = `${words(e.target.value)} words`));
    $("#e-save").addEventListener("click", (e) => busy(e.target, async () => {
      await api(`/api/sections/${S.sid}`, { method: "PUT", body: { content: $("#e-text").value, mark_edited: true } });
      await refreshProject(); toast("Saved — section marked as edited"); showStep(5);
    }));
  },

  async ground(st) {
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card"><button class="btn" id="g-run">Check claims against sources</button><div id="g-out"></div></div>`;
    $("#g-run").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/sections/${S.sid}/ground`, { body: {} });
      await refreshProject();
      $("#g-out").innerHTML = `<p>${engineTag(r)} <span class="tag ok">${r.summary.supported} supported</span> <span class="tag warn">${r.summary.partial} partial</span> <span class="tag err">${r.summary.unsupported} unsupported</span></p>${note(r)}
        ${r.claims.map((c) => `<div class="issue ${c.status === "unsupported" ? "error" : c.status === "partial" ? "warning" : "info"}">${tag(c.status)} ${esc(c.claim)}
          ${c.evidence ? `<div class="evidence">📄 ${esc(c.evidence.source)} — ${esc(c.evidence.section)}: “${esc(c.evidence.passage.slice(0, 280))}…” (score ${c.score}${c.numbers_match ? "" : ", numbers differ"})</div>` : ""}</div>`).join("")}
        <p><a href="#" data-goto="7">Fix unsupported claims in Step 7 →</a></p>`;
      bindCommon();
    }));
  },

  async claims(st) {
    const q = await api(`/api/sections/${S.sid}/claims`);
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card">
      ${q.summary ? `<p>${q.queue.length} claim(s) need attention.</p>` : `<div class="notice">Run Step 6 for this section first.</div>`}
      ${q.queue.map((c, i) => `<div class="issue ${c.status === "unsupported" ? "error" : "warning"}">${tag(c.status)} ${esc(c.claim)}
        ${c.evidence ? `<div class="evidence">Closest evidence — ${esc(c.evidence.source)}: “${esc(c.evidence.passage.slice(0, 240))}…”</div>` : ""}
        <div class="row" style="margin-top:.4rem"><button class="btn sm ghost" data-goto="5">Revise in editor</button><button class="btn sm danger" data-rm="${i}">Remove sentence</button></div></div>`).join("")}
      <div class="row" style="margin-top:.8rem"><button class="btn ghost" id="c-rerun">Re-run grounding</button><button class="btn ok" id="c-done">Mark claims resolved</button></div></div>`;
    $$("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/sections/${S.sid}/claims/remove`, { body: { claim: q.queue[+b.dataset.rm].claim } }); await refreshProject(); toast("Sentence removed — re-run grounding"); showStep(7); }
      catch (e) { toast(e.message, true); }
    }));
    $("#c-rerun").addEventListener("click", (e) => busy(e.target, async () => { await api(`/api/sections/${S.sid}/ground`, { body: {} }); await refreshProject(); showStep(7); }));
    $("#c-done").addEventListener("click", async () => { await api(`/api/sections/${S.sid}/claims/resolve`, { body: {} }); await refreshProject(); toast("Claims marked resolved"); });
  },

  async revise(st) {
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card">
      <div class="row"><label><input type="radio" name="rv" value="xtragpt" checked> XtraGPT (clarity & flow)</label>
      <label><input type="radio" name="rv" value="paperdebugger"> PaperDebugger (logic & methods)</label></div>
      <label class="field" style="margin-top:.6rem">Optional instruction<input id="rv-ins" placeholder="e.g. tighten the second paragraph"></label>
      <button class="btn" id="rv-go">Propose revision</button>
      <p class="small muted">Runs on the GPU lane while Harper + Vale pre-lint the section on CPU workers in parallel (${esc(S.hw?.plan.label)}).</p></div><div id="rv-out"></div>`;
    $("#rv-go").addEventListener("click", (e) => busy(e.target, async () => {
      const engine = $("input[name=rv]:checked").value;
      const r = await api(`/api/sections/${S.sid}/revise`, { body: { engine, instruction: $("#rv-ins").value } });
      await refreshProject();
      $("#rv-out").innerHTML = `<div class="card"><p>${engineTag({ tool: engine === "xtragpt" ? "XtraGPT" : "PaperDebugger", engine: r.engine })} model ${esc(r.model)} · ${r.revision.hunks.length} proposed change(s)</p>
        <h3>Notes</h3><ul>${r.revision.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
        <p class="small muted">Parallel CPU lane: Harper ${r.parallel.harper?.issues ?? "–"} issues · Vale ${r.parallel.vale?.issues ?? "–"} issues</p>
        ${r.revision.hunks.length ? `<a class="btn" href="#" data-goto="9">Review changes in Step 9 →</a>` : ""}</div>`;
      bindCommon();
    }));
  },

  async diff(st) {
    const { revisions } = await api(`/api/sections/${S.sid}/revisions`);
    const rev = revisions.find((r) => r.status === "pending");
    if (!rev) { panel().innerHTML = head(st) + sectionPicker() + `<div class="notice">No pending revision for this section. <a href="#" data-goto="8">Request one in Step 8</a>.</div>`; return; }
    const dec = {};
    const render = () => {
      $("#hunks").innerHTML = rev.hunks.map((h) => `<div class="hunk ${dec[h.id] || ""}"><div class="small"><span class="muted">…${esc(h.context_before.slice(-60))}</span>${h.before ? `<del>${esc(h.before)}</del>` : ""}${h.after ? `<ins>${esc(h.after)}</ins>` : ""}<span class="muted">${esc(h.context_after.slice(0, 60))}…</span></div>
        <div class="row"><button class="btn sm ok" data-h="${h.id}" data-d="accept">Accept</button><button class="btn sm danger" data-h="${h.id}" data-d="reject">Reject</button></div></div>`).join("");
      $$("[data-h]").forEach((b) => b.addEventListener("click", () => { dec[b.dataset.h] = b.dataset.d; render(); }));
      $("#d-count").textContent = `${Object.values(dec).filter((v) => v === "accept").length} accepted · ${Object.values(dec).filter((v) => v === "reject").length} rejected · ${rev.hunks.length - Object.keys(dec).length} undecided (kept original)`;
    };
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card"><p>${esc(rev.engine)} proposal · ${rev.hunks.length} change(s)</p>
      <div class="diff-grid"><div><h4>Original</h4><div class="diff-pane card" id="pa"></div></div><div><h4>Revised</h4><div class="diff-pane card" id="pb"></div></div></div>
      <div class="row"><button class="btn sm ghost" id="acc-all">Accept all</button><button class="btn sm ghost" id="rej-all">Reject all</button><span class="spacer"></span><span class="small muted" id="d-count"></span></div>
      <div id="hunks"></div><button class="btn" id="d-apply" style="margin-top:.6rem">Apply decisions</button></div>`;
    $("#pa").innerHTML = sideBySide(rev.original, rev.hunks, "a");
    $("#pb").innerHTML = sideBySide(rev.original, rev.hunks, "b");
    render();
    $("#acc-all").addEventListener("click", () => { rev.hunks.forEach((h) => (dec[h.id] = "accept")); render(); });
    $("#rej-all").addEventListener("click", () => { rev.hunks.forEach((h) => (dec[h.id] = "reject")); render(); });
    $("#d-apply").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/revisions/${rev.id}/apply`, { body: { decisions: dec } });
      await refreshProject(); toast(`Applied: ${r.accepted} accepted, ${r.rejected} rejected`); showStep(10);
    }));
  },

  async matrix(st) {
    const { matrix, complete } = await api(`/api/projects/${S.pid}/matrix`);
    await refreshProject();
    const d = (v) => `<span class="dot ${v ? "on" : ""}"></span>`;
    panel().innerHTML = head(st) + `<div class="card">${complete ? `<div class="notice">✅ All written sections are complete — global checks (Steps 11–21) are unlocked.</div>` : ""}
      <table><tr><th>Section</th><th>Words</th><th>Drafted</th><th>Edited</th><th>Grounded</th><th>Claims</th><th>Revised</th><th>Diffs</th><th>Human edits</th><th>Done</th><th></th></tr>
      ${matrix.map((r) => `<tr><td><strong>${esc(r.name)}</strong></td><td>${r.words}</td><td>${d(r.drafted)}</td><td>${d(r.edited)}</td><td>${d(r.grounded)}</td><td>${d(r.claims_fixed)}</td><td>${d(r.revised)}</td><td>${d(r.diffs_resolved)}</td><td>${Math.round(r.human_edit_ratio * 100)}%</td><td>${r.complete ? "✅" : r.words ? "…" : "—"}</td>
        <td><button class="btn sm ghost" data-open="${r.id}">Open</button></td></tr>`).join("")}</table></div>`;
    $$("[data-open]").forEach((b) => b.addEventListener("click", () => { S.sid = +b.dataset.open; showStep(5); }));
  },

  async grammar(st) {
    panel().innerHTML = head(st) + `<div class="card"><div class="row"><label><input type="radio" name="ge" value="harper" checked> Harper</label><label><input type="radio" name="ge" value="languagetool"> LanguageTool</label><button class="btn" id="gr-go">Check all sections</button></div></div><div id="gr-out"></div>`;
    const run = async () => {
      const engine = $("input[name=ge]:checked").value;
      const r = await api(`/api/projects/${S.pid}/grammar`, { body: { engine } });
      await refreshProject();
      $("#gr-out").innerHTML = r.sections.map((s) => { const sec = S.project.sections.find((x) => x.id === s.section_id);
        return `<div class="card"><h3>${esc(s.name)} ${engineTag(s.result)} <span class="tag">${s.result.issues.length} issues</span></h3>${note(s.result)}${issuesList(s.result.issues, sec.content, s.section_id)}</div>`; }).join("");
      bindFixes(() => run().catch((e) => toast(e.message, true)));
    };
    $("#gr-go").addEventListener("click", (e) => busy(e.target, run));
  },

  async style(st) {
    panel().innerHTML = head(st) + `<div class="card"><div class="row">
      <label>Engine <select id="sy-e"><option value="both">Vale + Turnitout-Humanizer</option><option value="vale">Vale only</option><option value="humanizer">Turnitout-Humanizer only</option></select></label>
      <label>House style <select id="sy-h">${["APA", "IEEE", "Nature"].map((h) => `<option ${S.settings.house_style === h ? "selected" : ""}>${h}</option>`).join("")}</select></label>
      <button class="btn" id="sy-go">Lint all sections</button></div></div><div id="sy-out"></div>`;
    const run = async () => {
      const house = $("#sy-h").value;
      api("/api/settings", { method: "PUT", body: { house_style: house } });
      const r = await api(`/api/projects/${S.pid}/style`, { body: { engine: $("#sy-e").value, house_style: house } });
      await refreshProject();
      S.cache.style = r;
      const byId = {};
      (r.vale || []).forEach((v) => (byId[v.section_id] = { ...byId[v.section_id], name: v.name, vale: v.result }));
      (r.humanizer || []).forEach((v) => (byId[v.section_id] = { ...byId[v.section_id], name: v.name, hum: v.result }));
      $("#sy-out").innerHTML = Object.entries(byId).map(([sid, x]) => { const sec = S.project.sections.find((s) => s.id === +sid);
        return `<div class="card"><h3>${esc(x.name)}</h3>
        ${x.hum ? `<div class="row">${engineTag(x.hum)} <strong>Machine-cadence score ${x.hum.score}/100</strong> — ${esc(x.hum.verdict)}</div>
          <div class="meter" style="margin:.4rem 0 .6rem"><span style="width:${x.hum.score}%;background:${x.hum.score > 60 ? "var(--err)" : x.hum.score > 30 ? "var(--warn)" : "var(--ok)"}"></span></div>
          <p class="small muted">Burstiness ${x.hum.burstiness} · mean ${x.hum.mean_sentence_words ?? "–"} words/sentence · type-token ratio ${x.hum.type_token_ratio ?? "–"}${x.hum.perplexity ? ` · perplexity ${x.hum.perplexity} (${esc(x.hum.device)})` : ""}</p>
          ${(x.hum.flags || []).map((f) => `<div class="issue warning">${esc(f.message)}<div class="ctx">${esc(sec.content.slice(f.start, f.end).slice(0, 220))}</div></div>`).join("")}` : ""}
        ${x.vale ? `<h4 style="margin-top:.8rem">${engineTag(x.vale)} ${x.vale.issues.length} style issues</h4>${note(x.vale)}${issuesList(x.vale.issues, sec.content, +sid)}` : ""}
        <a href="#" data-goto="13">Rewrite flagged passages →</a></div>`; }).join("");
      bindFixes(() => run().catch((e) => toast(e.message, true))); bindCommon();
    };
    $("#sy-go").addEventListener("click", (e) => busy(e.target, run));
  },

  async rewrite(st) {
    const sec = curSection();
    panel().innerHTML = head(st) + sectionPicker() + `<div class="card"><h4>Flagged passages</h4><div class="mirror" id="rw-mirror"></div></div>
      <div class="card"><textarea class="editor" id="rw-text">${esc(sec.content)}</textarea>
      <div class="row small" id="rw-stats"></div><button class="btn" id="rw-save">Save rewrite</button></div>`;
    let t;
    const analyse = async () => {
      const text = $("#rw-text").value;
      const a = await api("/api/analyze", { body: { text } });
      const ranges = [...(a.humanizer.flags || []).map((f) => ({ start: f.start, end: f.end, title: f.message })),
        ...a.vale.issues.map((i) => ({ start: i.start, end: i.end, cls: "err", title: i.message }))];
      $("#rw-mirror").innerHTML = highlight(text, ranges) || "<span class='muted'>Nothing flagged.</span>";
      $("#rw-stats").innerHTML = `<span>Flesch ${a.readability.flesch}</span><span>Grade ${a.readability.grade}</span><span>Burstiness ${a.humanizer.burstiness}</span>
        <span>Machine-cadence <strong>${a.humanizer.score}</strong>/100</span><span>${a.vale.issues.length} style flags</span>`;
    };
    $("#rw-text").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => analyse().catch(() => {}), 600); });
    $("#rw-save").addEventListener("click", (e) => busy(e.target, async () => {
      await api(`/api/sections/${S.sid}`, { method: "PUT", body: { content: $("#rw-text").value } });
      await api(`/api/projects/${S.pid}/steps/13`, { body: { done: true } });
      await refreshProject(); toast("Rewrite saved — re-run grounding for this section if claims changed");
    }));
    analyse().catch((e) => toast(e.message, true));
  },

  async refcheck(st) {
    panel().innerHTML = head(st) + `<div class="card"><button class="btn" id="rc-go">Verify ${S.project.bib_entries} references</button><div id="rc-out"></div></div>`;
    $("#rc-go").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/refcheck`, { body: {} });
      await refreshProject();
      $("#rc-out").innerHTML = `${r.online ? "" : `<div class="notice warn">Offline — references could not be verified. Connect to the internet and run again.</div>`}
        <p>${Object.entries(r.summary).map(([k, v]) => `${tag(k)} ${v}`).join(" ")}</p>
        <table><tr><th>Key</th><th>Status</th><th>Details</th></tr>${r.references.map((x) => `<tr><td><code>${esc(x.key)}</code></td><td>${tag(x.status)}</td><td class="small">${esc(x.message)}${x.source ? ` <span class="muted">(${esc(x.source)})</span>` : ""}</td></tr>`).join("")}</table>`;
    }));
  },

  async citations(st) {
    panel().innerHTML = head(st) + `<div class="card"><button class="btn" id="ct-go">Run CiteScan + check-citations</button><div id="ct-out"></div></div>`;
    const run = async () => {
      const r = await api(`/api/projects/${S.pid}/citations`, { body: {} });
      await refreshProject();
      const cs = r.citescan, cc = r.check_citations;
      $("#ct-out").innerHTML = `<div class="grid2"><div><h3>CiteScan ${cs.parity ? '<span class="tag ok">1-to-1 parity</span>' : ""}</h3>
          <p><strong>Cited but missing from .bib:</strong> ${cs.missing_in_bib.map((k) => `<code>${esc(k)}</code>`).join(", ") || "none"}</p>
          <p><strong>Unused .bib entries:</strong> ${cs.unused_in_bib.map((k) => `<code>${esc(k)}</code>`).join(", ") || "none"}</p>
          ${cs.unused_in_bib.length ? `<button class="btn danger" id="ct-clean">Remove ${cs.unused_in_bib.length} unused entries</button>` : ""}</div>
        <div><h3>check-citations ${cc.valid ? '<span class="tag ok">valid</span>' : '<span class="tag err">errors</span>'}</h3>
          ${cc.problems.length ? `<table><tr><th>Key</th><th>Line</th><th>Problem</th></tr>${cc.problems.map((p) => `<tr><td><code>${esc(p.key)}</code></td><td>${p.line}</td><td>${tag(p.severity)} ${esc(p.message)}</td></tr>`).join("")}</table>` : "<p class='muted'>✓ No problems.</p>"}</div></div>`;
      $("#ct-clean")?.addEventListener("click", async (e) => {
        if (!confirm("Remove all .bib entries that are not cited in the manuscript?")) return;
        const c = await api(`/api/projects/${S.pid}/bib/clean`, { body: {} });
        toast(`Removed ${c.removed.length} entries`); run();
      });
    };
    $("#ct-go").addEventListener("click", (e) => busy(e.target, run));
  },

  async overlap(st) {
    panel().innerHTML = head(st) + `<div class="card"><button class="btn" id="ov-go">Check overlap with ${S.project.sources.length} sources</button><div id="ov-out"></div></div>`;
    $("#ov-go").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/overlap`, { body: {} });
      await refreshProject();
      $("#ov-out").innerHTML = `<p>${r.sentences_checked} sentences checked · ${r.flagged.length} flagged</p>` + (r.flagged.map((f) =>
        `<div class="issue ${f.kind === "verbatim" ? "error" : "warning"}">${tag(f.kind)} ${Math.round(f.containment * 100)}% overlap with <strong>${esc(f.source)}</strong><br>${esc(f.sentence)}
         <div class="evidence">Matched: “${esc(f.matched_words)}”</div><div class="small">${esc(f.advice)}</div></div>`).join("") || "<p class='muted'>✓ No significant overlap.</p>");
    }));
  },

  async consistency(st) {
    panel().innerHTML = head(st) + `<div class="card"><button class="btn" id="cs-go">Run Paper Analysis Utility</button><div id="cs-out"></div></div>`;
    $("#cs-go").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/consistency`, { body: {} });
      await refreshProject();
      $("#cs-out").innerHTML = `<p>${r.consistent ? '<span class="tag ok">consistent</span>' : '<span class="tag err">inconsistencies found</span>'}</p>
        <p class="small muted">Abstract numbers: ${r.abstract_numbers.join(", ") || "—"} · Results numbers: ${r.results_numbers.join(", ") || "—"}</p>
        ${r.issues.map((i) => `<div class="issue ${i.severity}">${tag(i.kind)} ${esc(i.message)}</div>`).join("") || "<p class='muted'>✓ No issues.</p>"}`;
    }));
  },

  async disclosure(st) {
    const d = await api(`/api/projects/${S.pid}/disclosure`);
    await refreshProject();
    panel().innerHTML = head(st) + `<div class="card"><h3>AI-Use Disclosure Statement</h3><textarea id="ds-text" rows="7">${esc(d.statement)}</textarea>
      <div class="row"><button class="btn" id="ds-copy">Copy statement</button><span class="muted small">Human edit share: ${d.human_edit_percent}% · models: ${esc(d.models.join(", ") || "none")}</span></div></div>
      <div class="card"><h3>Audit log (${d.log.length})</h3>${d.log.length ? `<table><tr><th>When</th><th>Step</th><th>Section</th><th>Tool</th><th>Model</th><th>Hardware</th><th>Words</th></tr>
      ${d.log.map((l) => `<tr><td>${esc(l.when)}</td><td>${l.step}</td><td>${esc(l.section)}</td><td>${esc(l.tool)}</td><td>${esc(l.model)}</td><td class="small">${esc(l.hardware)}</td><td>${l.words}</td></tr>`).join("")}</table>` : "<p class='muted'>No generative-AI use recorded.</p>"}</div>`;
    $("#ds-copy").addEventListener("click", () => { navigator.clipboard.writeText($("#ds-text").value).then(() => toast("Copied")); });
  },

  async export(st) {
    const ex = await api(`/api/projects/${S.pid}/exports`);
    panel().innerHTML = head(st) + `<div class="card"><div class="row">
      <label>CSL style <select id="ex-csl">${Object.entries(S.csl).map(([k, v]) => `<option value="${k}" ${S.project.target_csl === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <button class="btn ghost" id="ex-prev">Live preview (Typst)</button><span class="spacer"></span>
      <label><input type="checkbox" class="ex-f" value="docx" checked> DOCX</label><label><input type="checkbox" class="ex-f" value="tex" checked> LaTeX</label><label><input type="checkbox" class="ex-f" value="pdf" checked> PDF</label>
      <button class="btn" id="ex-go">Export (Pandoc + citeproc)</button></div><div id="ex-out"></div>
      <h4 style="margin-top:1rem">Exported files</h4><div id="ex-files">${ex.files.map((f) => `<a href="/api/files/${S.pid}/${encodeURIComponent(f)}" download>${esc(f)}</a>`).join(" · ") || "<span class='muted'>none yet</span>"}</div></div>
      <div id="ex-preview"></div>`;
    $("#ex-prev").addEventListener("click", (e) => busy(e.target, async () => {
      const r = await api(`/api/projects/${S.pid}/preview`, { body: { csl: $("#ex-csl").value } });
      $("#ex-preview").innerHTML = `<div class="card"><p>${engineTag(r)}</p>${note(r)}${r.format === "svg" ? r.pages.map((p) => `<div class="svg-page">${p}</div>`).join("") : `<iframe class="preview-frame" sandbox="" id="ex-frame"></iframe>`}</div>`;
      if (r.format !== "svg") $("#ex-frame").srcdoc = r.html;
    }));
    $("#ex-go").addEventListener("click", (e) => busy(e.target, async () => {
      const formats = $$(".ex-f:checked").map((c) => c.value);
      const r = await api(`/api/projects/${S.pid}/export`, { body: { formats, csl: $("#ex-csl").value } });
      await refreshProject();
      $("#ex-out").innerHTML = `<p>Version <strong>v${esc(r.version)}</strong></p>` + r.results.map((x) => x.ok
        ? `<div class="issue info">${engineTag(x)} <a href="/api/files/${S.pid}/${encodeURIComponent(x.filename)}" download>${esc(x.filename)}</a>${x.note ? `<div class="small muted">${esc(x.note)}</div>` : ""}</div>`
        : `<div class="issue error">${esc(x.error)}</div>`).join("");
      const ex2 = await api(`/api/projects/${S.pid}/exports`);
      $("#ex-files").innerHTML = ex2.files.map((f) => `<a href="/api/files/${S.pid}/${encodeURIComponent(f)}" download>${esc(f)}</a>`).join(" · ");
    }));
  },

  async read(st) {
    const key = `rm-check-${S.pid}`;
    let checked = [];
    try { checked = JSON.parse(localStorage.getItem(key) || "[]"); } catch (e) {}
    panel().innerHTML = head(st) + `<div class="grid2" style="grid-template-columns:1fr 300px"><div class="card reader"><h1 style="text-align:center">${esc(S.project.title)}</h1>
      ${S.project.sections.filter((s) => s.content).map((s) => `<h2>${esc(s.name)}</h2><div contenteditable="true" data-sec="${s.id}">${esc(s.content).replace(/\n/g, "<br>")}</div>`).join("")}</div>
      <div class="card" style="align-self:start;position:sticky;top:80px"><h3>Pre-submission checklist</h3>
      ${S.checklist.map((c, i) => `<label class="small" style="display:flex;gap:.4rem;margin:.35rem 0"><input type="checkbox" data-ck="${i}" ${checked.includes(i) ? "checked" : ""}>${esc(c)}</label>`).join("")}
      <button class="btn" id="rd-done" style="margin-top:.6rem">Read-through complete</button></div></div>`;
    $$("[data-sec]").forEach((el) => el.addEventListener("blur", async () => {
      const content = el.innerText.replace(/ /g, " ");
      const sec = S.project.sections.find((s) => s.id === +el.dataset.sec);
      if (content.trim() === sec.content.trim()) return;
      await api(`/api/sections/${el.dataset.sec}`, { method: "PUT", body: { content } }); await refreshProject(); toast(`Saved ${sec.name}`);
    }));
    $$("[data-ck]").forEach((c) => c.addEventListener("change", () => {
      const v = $$("[data-ck]:checked").map((x) => +x.dataset.ck);
      try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) {}
    }));
    $("#rd-done").addEventListener("click", async () => { await api(`/api/projects/${S.pid}/steps/20`, { body: { done: true } }); await refreshProject(); toast("Step 20 complete"); });
  },

  async submit(st) {
    panel().innerHTML = head(st) + `<div class="card"><h3>Figures</h3><div class="row"><input type="file" id="sb-fig" multiple accept="image/*,.pdf,.eps,.svg,.tif,.tiff"><button class="btn ghost" id="sb-up">Add figures</button></div>
      <h3 style="margin-top:1rem">Bundle</h3><p class="muted">Contains the latest exported manuscript (or a fresh DOCX + LaTeX export), the cleaned .bib, figures, the AI-Use Disclosure and your checklist.</p>
      <button class="btn" id="sb-go">Build Submission Bundle</button><div id="sb-out"></div></div>`;
    $("#sb-up").addEventListener("click", (e) => busy(e.target, async () => {
      const fd = new FormData(); [...$("#sb-fig").files].forEach((f) => fd.append("files", f));
      const r = await api(`/api/projects/${S.pid}/figures`, { body: fd }); toast(`${r.figures.length} figure(s) stored`);
    }));
    $("#sb-go").addEventListener("click", (e) => busy(e.target, async () => {
      let checked = []; try { checked = JSON.parse(localStorage.getItem(`rm-check-${S.pid}`) || "[]"); } catch (er) {}
      const r = await api(`/api/projects/${S.pid}/submit`, { body: { checked } });
      await refreshProject();
      $("#sb-out").innerHTML = `<div class="notice">📦 <a href="${r.url}" download><strong>${esc(r.bundle)}</strong></a><ul>${r.contents.map((c) => `<li><code>${esc(c)}</code></li>`).join("")}</ul></div>`;
    }));
  },
};

function sideBySide(original, hunks, side) {
  // rebuild panes from hunks: walk original, marking 'before' (side a) or 'after' (side b)
  let out = "", pos = 0;
  for (const h of hunks) {
    out += esc(original.slice(pos, h.a_start));
    if (side === "a") out += h.before ? `<del>${esc(h.before)}</del>` : "";
    else out += h.after ? `<ins>${esc(h.after)}</ins>` : "";
    pos = h.a_end;
  }
  return out + esc(original.slice(pos));
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    const [wf, settings, hw] = await Promise.all([api("/api/workflow"), api("/api/settings"), api("/api/hardware/profile")]);
    S.steps = wf.steps; S.checklist = wf.checklist; S.csl = wf.csl_styles; S.settings = settings;
    let localTheme = null; try { localTheme = localStorage.getItem("rm-theme"); } catch (e) {}
    applyTheme(localTheme || settings.theme || "light");
    renderHw(hw);
    let pid = null; try { pid = +localStorage.getItem("rm-pid") || null; } catch (e) {}
    await loadProjects(pid);
  } catch (e) { panel().innerHTML = `<div class="notice err">Could not reach the ResearchMate server: ${esc(e.message)}</div>`; }
  pollNetwork(); pollTelemetry();
  setInterval(pollNetwork, 20000); setInterval(pollTelemetry, 3000);
})();
