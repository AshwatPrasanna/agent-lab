"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const fmtDur = (s) => (s == null ? "–" : s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s` : `${Math.round(s)}s`);
const fmtK = (n) => (n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n));

async function api(path, opts = {}) {
  const o = { ...opts, headers: { "X-Agent-Lab": "ui", ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; o.method = o.method || "POST"; delete o.json; }
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let d = data && data.detail !== undefined ? data.detail : data;
    if (d && d.errors) d = d.errors.join("\n");
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return data;
}

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "jobs") loadJobs();
  if (name === "topology" && (!Topo.cfg || !Topo.dirty)) Topo.load();
  if (name === "run") { loadRunTopo(); loadPriorList(); }
  if (name === "settings") loadSettings();
  try { localStorage.setItem("tab", name); } catch (e) {}
  loadStatus();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------------------------------------------------------------- status */
async function loadStatus(refresh = false) {
  const s = await api("/api/status" + (refresh ? "?refresh=true" : "")).catch(() => null);
  const bar = $("#statusbar");
  if (!s) { bar.innerHTML = `<span class="pill bad">server unreachable</span>`; return; }
  const pills = [];
  if (!s.path) pills.push(`<span class="pill bad">Claude Code not found</span>`);
  else {
    const old = s.version && cmpVer(s.version, s.min_recommended) < 0;
    pills.push(`<span class="pill ${old ? "warn" : "ok"}" title="${esc(s.path)}">Claude Code ${esc(s.version || "?")}${old ? " · run `claude update`" : ""}</span>`);
    pills.push(s.logged_in ? `<span class="pill ok">signed in${s.auth_method ? " · " + esc(s.auth_method) : ""}</span>` : `<span class="pill bad">not signed in: run \`claude\` once</span>`);
  }
  pills.push(`<span class="pill ${s.ntfy_configured ? "ok" : "warn"}">${s.ntfy_configured ? "ntfy on" : "ntfy not set"}</span>`);
  if (s.code_stale) pills.unshift(`<span class="pill warn restart-pill" title="Agent Lab's files changed since it started">⟳ Update ready: restart</span>`);
  const pend = s.pending_permissions || [];
  if (pend.length) pills.unshift(`<span class="pill bad perm-pill" data-job="${esc(pend[0].job)}">✋ ${pend.length} permission request${pend.length === 1 ? "" : "s"} waiting</span>`);
  bar.innerHTML = pills.join("");
  const pp = $(".perm-pill", bar); if (pp) pp.onclick = (e) => { e.stopPropagation(); showTab("jobs"); openJob(pp.dataset.job); };
  const rp = $(".restart-pill", bar); if (rp) rp.onclick = (e) => { e.stopPropagation(); restartServer(); };
  bar.title = "Click to re-check";
  setAutoAllow(!!s.auto_allow_permissions);
}
$("#statusbar").addEventListener("click", () => loadStatus(true));
setInterval(() => loadStatus(), 10000);
function setAutoAllow(on) {
  const cb = $("#autoallow-cb"); if (document.activeElement !== cb) cb.checked = on;
  $("#autoallow").classList.toggle("on", on);
}
$("#autoallow-cb").addEventListener("change", async (e) => {
  const on = e.target.checked;
  try { await api("/api/settings", { json: { auto_allow_permissions: on } }); setAutoAllow(on); loadStatus(); if (currentJob) pollJob(); }
  catch (err) { alert(err.message); e.target.checked = !on; }
});
async function restartServer(manual = false) {
  if (!confirm(manual ? "Restart Agent Lab? This takes a few seconds." : "Restart Agent Lab to load the updated code? This takes a few seconds.")) return;
  try { await api("/api/admin/restart", { json: {} }); }
  catch (e) { alert(e.message); return; }
  $("#statusbar").innerHTML = `<span class="pill warn">restarting…</span>`;
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try { const st = await fetch("/api/status").then((r) => r.json()); if (!st.code_stale && (!manual || i > 3)) { location.reload(); return; } } catch (e) {}
  }
  alert("Agent Lab didn't come back. Check its Terminal window.");
}
function cmpVer(a, b) { const x = a.split(".").map(Number), y = b.split(".").map(Number); for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0); } return 0; }

/* ---------------------------------------------------------------- new job */
async function loadRunTopo() {
  const v = await api("/api/config").catch(() => null);
  if (!v) return;
  const c = v.config, orch = c.nodes.find((n) => n.role === "orchestrator");
  const workers = c.nodes.filter((n) => n.role !== "orchestrator").map((n) => `${n.name} (${n.model})`);
  $("#run-topo").innerHTML = `Uses topology <b>v${v.version}</b>: orchestrator on <b>${esc(orch.model)}</b>` +
    (workers.length ? `, subagents ${esc(workers.join(", "))}` : ", no subagents") + `. <a href="#" id="go-topo">edit</a>`;
  $("#go-topo").onclick = (e) => { e.preventDefault(); showTab("topology"); };
}
let priorPick = new Set();
async function loadPriorList() {
  const jobs = (await api("/api/jobs").catch(() => [])).filter((j) => j.kind !== "selftest" && !ACTIVE.includes(j.status));
  const el = $("#prior-list");
  el.innerHTML = jobs.length ? jobs.map((j) => `<label class="check prior-item"><input type="checkbox" value="${j.id}" ${priorPick.has(j.id) ? "checked" : ""}>
      <span class="grow">${esc(j.title)}</span><span class="status ${j.status}">${statusLabel(j.status)}</span><span class="muted small">${fmtTime(j.created)}</span></label>`).join("")
    : `<p class="muted small">No finished jobs yet.</p>`;
  $$("input", el).forEach((cb) => cb.onchange = () => { cb.checked ? priorPick.add(cb.value) : priorPick.delete(cb.value); });
  $("#prior-all").onchange = () => { el.classList.toggle("disabled", $("#prior-all").checked); };
  el.classList.toggle("disabled", $("#prior-all").checked);
}
async function startFrom(job, mode) {
  priorPick = new Set([job.id]);
  $("#prior-all").checked = false;
  const f = $("#job-form");
  if (mode === "slurm") {
    const st = await api("/api/settings").catch(() => ({}));
    const cluster = (st.cluster_name || "").trim() || "our Slurm cluster";
    f.elements.title.value = `${job.title} (Slurm version)`;
    f.elements.spec_text.value = `Turn the experiment in prior/${job.id}/ ("${job.title}"), which was prototyped and tested at toy scale on this Mac, into a full-scale version for ${cluster}.

Deliverables:
- slurm/job.sbatch: a batch script with sensible resource requests (GPU type and count, CPUs, memory, time), environment setup (modules or a uv/conda env), logging, and checkpoint/resume if the run is long. Leave partition and account as clearly marked placeholders unless the earlier job's files specify them.
- Any config or code changes needed for the full run: CUDA, full dataset, larger batch and step counts. Keep the toy/local path working.
- SLURM.md: how to copy the code to the cluster, get the data there, submit the job, monitor it, and fetch results.
- Re-run the tests and the toy run locally to confirm nothing broke.

Don't submit anything and don't connect to the cluster: only prepare the files.`;
  }
  showTab("run");
  f.elements.spec_text.focus();
}
$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target), msg = $("#job-form-msg");
  if (!fd.get("file") || !fd.get("file").name) fd.delete("file");
  const pr = $("#prior-all").checked ? "all" : [...priorPick].join(",");
  if (pr) fd.set("prior", pr);
  msg.className = "msg"; msg.textContent = "Starting…";
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    e.target.reset(); msg.textContent = ""; priorPick = new Set();
    showTab("jobs"); openJob(job.id);
  } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
});

/* ---------------------------------------------------------------- topology editor */
const NW = 176, NH = 62;
const Topo = {
  cfg: null, version: null, sel: null, edgeType: "delegate", dirty: false, meta: null, drag: null, link: null,

  async load() {
    this.meta = this.meta || (await api("/api/meta"));
    const v = await api("/api/config");
    this.cfg = structuredClone(v.config); this.version = v.version; this.sel = null; this.setDirty(false);
    const ps = await api("/api/presets");
    $("#preset-select").innerHTML = ps.map((p) => `<option value="${p.key}">${esc(p.label)}</option>`).join("");
    this.render(); this.loadVersions();
  },
  setDirty(d) { this.dirty = d; $("#dirty").textContent = d ? "unsaved changes" : this.version ? `v${this.version} (current)` : ""; },
  node(id) { return this.cfg.nodes.find((n) => n.id === id); },

  svgPoint(evt) {
    const svg = $("#canvas"), pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  },

  render() { this.renderGraph(); this.renderInspector(); },
  renderGraph() {
    const ng = $("#nodes"), eg = $("#edges");
    eg.innerHTML = this.cfg.edges.map((e, i) => {
      const a = this.node(e.from), b = this.node(e.to);
      if (!a || !b) return "";
      const rev = this.cfg.edges.some((f) => f.from === e.to && f.to === e.from);
      const d = edgePath(a, b, rev ? 28 : 0);
      const sel = this.sel && this.sel.kind === "edge" && this.sel.i === i;
      return `<g class="edge ${e.type} ${sel ? "sel" : ""}" data-edge="${i}"><path class="hit" d="${d}"/><path class="line" d="${d}" marker-end="url(#arrow-${e.type})"/></g>`;
    }).join("");
    ng.innerHTML = this.cfg.nodes.map((n) => {
      const sel = this.sel && this.sel.kind === "node" && this.sel.id === n.id;
      const orch = n.role === "orchestrator";
      return `<g class="node ${orch ? "orch" : ""} ${sel ? "sel" : ""}" data-node="${esc(n.id)}" transform="translate(${n.x},${n.y})">
        <rect class="body" width="${NW}" height="${NH}"/>
        <text x="14" y="25" font-weight="600">${esc(n.name)}</text>
        <text class="meta" x="14" y="44">${orch ? (this.cfg.require_delegation ? "delegates only · " : "orchestrator · ") : ""}${esc(n.model)} · ${(n.tools || []).length} tools</text>
        <circle class="port" cx="${NW}" cy="${NH / 2}" r="7" data-port="${esc(n.id)}"/>
      </g>`;
    }).join("");
  },

  renderInspector() {
    const el = $("#inspector");
    const s = this.sel;
    if (s && s.kind === "node") {
      const n = this.node(s.id);
      if (!n) { this.sel = null; return this.renderInspector(); }
      const orch = n.role === "orchestrator";
      el.innerHTML = `<h3>${orch ? "Orchestrator" : "Worker subagent"}</h3>
        ${orch ? `<label class="check req"><input type="checkbox" id="req-deleg" ${this.cfg.require_delegation ? "checked" : ""}>
          <span><b>Require delegation</b><br><span class="muted small">The orchestrator only plans, reads and delegates. A hook blocks it from running commands or writing code, and the job page shows any blocked attempts.</span></span></label>` : ""}
        <label>Name <input data-f="name" value="${esc(n.name)}"></label>
        <label>Model <select data-f="model">${this.meta.models.map((m) => `<option ${m === n.model ? "selected" : ""}>${m}</option>`).join("")}</select></label>
        <label>Description${orch ? "" : " (Claude uses this to decide when to delegate)"} <textarea data-f="description" rows="2">${esc(n.description)}</textarea></label>
        <div><b>Tools</b>${orch ? ' <span class="muted small">(these apply to the whole session, subagents included. To stop the orchestrator doing the work itself, use Require delegation)</span>' : ""}</div>
        <div class="tools">${this.meta.tools.map((t) => `<label><input type="checkbox" data-tool="${t}" ${(n.tools || []).includes(t) ? "checked" : ""}> ${t}</label>`).join("")}</div>
        <label>System prompt <textarea data-f="prompt" rows="14">${esc(n.prompt)}</textarea></label>
        ${orch ? "" : `<button class="danger" id="del-node">Delete node</button>`}`;
      $$("[data-f]", el).forEach((inp) => inp.addEventListener("input", () => {
        const f = inp.dataset.f;
        n[f] = f === "name" ? inp.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") : inp.value;
        if (f === "name" && inp.value !== n.name) inp.value = n.name;
        this.setDirty(true); if (f === "name" || f === "model") this.renderGraphOnly();
      }));
      $$("[data-tool]", el).forEach((cb) => cb.addEventListener("change", () => {
        n.tools = $$("[data-tool]", el).filter((c) => c.checked).map((c) => c.dataset.tool);
        this.setDirty(true); this.renderGraphOnly();
      }));
      const dn = $("#del-node"); if (dn) dn.onclick = () => this.deleteSel();
      const rq = $("#req-deleg"); if (rq) rq.onchange = () => { this.cfg.require_delegation = rq.checked; this.setDirty(true); this.renderGraphOnly(); };
    } else if (s && s.kind === "edge") {
      const e = this.cfg.edges[s.i];
      el.innerHTML = `<h3>Edge</h3>
        <p><b>${esc(this.node(e.from).name)}</b> ${e.type === "delegate" ? "→" : "⇢"} <b>${esc(this.node(e.to).name)}</b></p>
        <label>Type <select id="edge-type-sel">
          <option value="delegate" ${e.type === "delegate" ? "selected" : ""}>Delegate: parent can spawn child as a subagent</option>
          <option value="handoff" ${e.type === "handoff" ? "selected" : ""}>Handoff: output of A is passed to B as input</option>
        </select></label>
        <div class="row"><button id="edge-flip">Reverse direction</button><button class="danger" id="edge-del">Delete edge</button></div>`;
      $("#edge-type-sel").onchange = (ev) => { e.type = ev.target.value; this.setDirty(true); this.render(); };
      $("#edge-flip").onclick = () => { [e.from, e.to] = [e.to, e.from]; this.setDirty(true); this.render(); };
      $("#edge-del").onclick = () => this.deleteSel();
    } else {
      el.innerHTML = `<h3>Lab-wide instructions</h3>
        <p class="muted small">Every agent gets these: the orchestrator through its system prompt, and each subagent through an appended prompt. The self-improvement step can edit them too.</p>
        <textarea id="base-prompt" rows="26">${esc(this.cfg.base_prompt)}</textarea>`;
      $("#base-prompt").addEventListener("input", (ev) => { this.cfg.base_prompt = ev.target.value; this.setDirty(true); });
    }
  },
  renderGraphOnly() { this.renderGraph(); },

  deleteSel() {
    const s = this.sel; if (!s) return;
    if (s.kind === "node") {
      const n = this.node(s.id); if (n.role === "orchestrator") return;
      this.cfg.nodes = this.cfg.nodes.filter((x) => x.id !== s.id);
      this.cfg.edges = this.cfg.edges.filter((e) => e.from !== s.id && e.to !== s.id);
    } else this.cfg.edges.splice(s.i, 1);
    this.sel = null; this.setDirty(true); this.render();
  },

  addNode() {
    let k = 1; while (this.cfg.nodes.some((n) => n.name === "worker-" + k)) k++;
    const id = "n" + Math.random().toString(36).slice(2, 8);
    this.cfg.nodes.push({ id, name: "worker-" + k, role: "worker", model: "opus", description: "Describe when the orchestrator should use this agent.",
      prompt: "You are a specialist. Describe the role, inputs and expected outputs here.", tools: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"], x: 560 + (k % 3) * 30, y: 40 + (k % 5) * 70 });
    this.sel = { kind: "node", id }; this.setDirty(true); this.render();
  },

  async save() {
    $("#topo-errors").textContent = "";
    try {
      const v = await api("/api/config", { json: { config: this.cfg } });
      this.version = v.version; this.setDirty(false); this.loadVersions(); loadRunTopo();
    } catch (e) { $("#topo-errors").textContent = e.message; }
  },

  async loadVersions() {
    const vs = await api("/api/versions");
    $("#versions").innerHTML = vs.map((v) => `<div class="version">
        <b>v${v.version}</b>
        <span class="chip ${v.source} ${v.current ? "current" : ""}">${v.current ? "current" : esc(v.source)}</span>
        <span class="grow">${esc(v.summary || "")}${v.job ? ` <a href="#" data-job="${esc(v.job)}" class="muted small">job</a>` : ""}</span>
        <span class="muted small">${fmtTime(v.created)}</span>
        ${v.current ? "" : `<button class="small" data-restore="${v.version}">Restore</button>`}
      </div>`).join("");
    $$("[data-restore]").forEach((b) => b.onclick = async () => {
      if (this.dirty && !confirm("Discard unsaved changes?")) return;
      await api(`/api/versions/${b.dataset.restore}/restore`, { method: "POST" }); this.load(); loadRunTopo();
    });
    $$("#versions [data-job]").forEach((a) => a.onclick = (e) => { e.preventDefault(); showTab("jobs"); openJob(a.dataset.job); });
  },
};

function edgePath(a, b, bend) {
  const ac = { x: a.x + NW / 2, y: a.y + NH / 2 }, bc = { x: b.x + NW / 2, y: b.y + NH / 2 };
  const p1 = clipToRect(ac, bc, a), p2 = clipToRect(bc, ac, b);
  if (!bend) return `M${p1.x},${p1.y} L${p2.x},${p2.y}`;
  const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2, dx = p2.x - p1.x, dy = p2.y - p1.y, L = Math.hypot(dx, dy) || 1;
  const cx = mx - (dy / L) * bend, cy = my + (dx / L) * bend;
  return `M${p1.x},${p1.y} Q${cx},${cy} ${p2.x},${p2.y}`;
}
function clipToRect(from, to, r) {
  const dx = to.x - from.x, dy = to.y - from.y, hw = NW / 2 + 3, hh = NH / 2 + 3;
  const s = Math.min(Math.abs(dx) > 1e-6 ? hw / Math.abs(dx) : Infinity, Math.abs(dy) > 1e-6 ? hh / Math.abs(dy) : Infinity);
  return { x: from.x + dx * s, y: from.y + dy * s };
}

(function wireCanvas() {
  const svg = $("#canvas");
  svg.addEventListener("pointerdown", (evt) => {
    const port = evt.target.closest("[data-port]");
    const nodeEl = evt.target.closest("[data-node]");
    const edgeEl = evt.target.closest("[data-edge]");
    const p = Topo.svgPoint(evt);
    if (port) {
      Topo.link = { from: port.dataset.port, x: p.x, y: p.y }; svg.setPointerCapture(evt.pointerId); evt.preventDefault(); return;
    }
    if (nodeEl) {
      const n = Topo.node(nodeEl.dataset.node);
      Topo.drag = { id: n.id, dx: p.x - n.x, dy: p.y - n.y, moved: false };
      Topo.sel = { kind: "node", id: n.id }; Topo.render(); svg.setPointerCapture(evt.pointerId); return;
    }
    if (edgeEl) { Topo.sel = { kind: "edge", i: +edgeEl.dataset.edge }; Topo.render(); return; }
    Topo.sel = null; Topo.render();
  });
  svg.addEventListener("pointermove", (evt) => {
    const p = Topo.svgPoint(evt);
    if (Topo.drag) {
      const n = Topo.node(Topo.drag.id);
      n.x = Math.max(0, Math.min(1000 - NW, Math.round(p.x - Topo.drag.dx)));
      n.y = Math.max(0, Math.min(520 - NH, Math.round(p.y - Topo.drag.dy)));
      Topo.drag.moved = true;
      const g = $(`[data-node="${CSS.escape(n.id)}"]`); g.setAttribute("transform", `translate(${n.x},${n.y})`);
      Topo.renderEdgesOnly();
    } else if (Topo.link) {
      const a = Topo.node(Topo.link.from);
      $("#temp-edge").setAttribute("d", `M${a.x + NW},${a.y + NH / 2} L${p.x},${p.y}`);
    }
  });
  svg.addEventListener("pointerup", (evt) => {
    if (Topo.drag) { if (Topo.drag.moved) Topo.setDirty(true); Topo.drag = null; }
    if (Topo.link) {
      $("#temp-edge").setAttribute("d", "");
      const p = Topo.svgPoint(evt);
      const target = Topo.cfg.nodes.find((n) => p.x >= n.x && p.x <= n.x + NW && p.y >= n.y && p.y <= n.y + NH);
      const from = Topo.link.from; Topo.link = null;
      if (target && target.id !== from) {
        const t = Topo.edgeType;
        if (t === "delegate" && target.role === "orchestrator") { $("#topo-errors").textContent = "Nobody can delegate to the orchestrator."; return; }
        if (!Topo.cfg.edges.some((e) => e.from === from && e.to === target.id && e.type === t)) {
          Topo.cfg.edges.push({ from, to: target.id, type: t });
          Topo.sel = { kind: "edge", i: Topo.cfg.edges.length - 1 }; Topo.setDirty(true); Topo.render();
        }
      }
    }
  });
  document.addEventListener("keydown", (e) => {
    if (!$("#tab-topology").classList.contains("active")) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); Topo.deleteSel(); }
  });
})();
Topo.renderEdgesOnly = function () {
  $$("#edges [data-edge]").forEach((g) => {
    const e = this.cfg.edges[+g.dataset.edge], a = this.node(e.from), b = this.node(e.to);
    const rev = this.cfg.edges.some((f) => f.from === e.to && f.to === e.from);
    const d = edgePath(a, b, rev ? 28 : 0); $$("path", g).forEach((p) => p.setAttribute("d", d));
  });
};
$("#btn-add-node").onclick = () => Topo.addNode();
$("#btn-delete").onclick = () => Topo.deleteSel();
$("#btn-save").onclick = () => Topo.save();
$("#btn-discard").onclick = () => { if (!Topo.dirty || confirm("Discard unsaved changes?")) Topo.load(); };
$("#btn-load-preset").onclick = async () => {
  if (Topo.dirty && !confirm("Replace your unsaved changes with this preset?")) return;
  Topo.cfg = await api("/api/presets/" + $("#preset-select").value); Topo.sel = null; Topo.setDirty(true); Topo.render();
};
$$("#edge-type button").forEach((b) => b.onclick = () => {
  Topo.edgeType = b.dataset.et; $$("#edge-type button").forEach((x) => x.classList.toggle("active", x === b));
});
window.addEventListener("beforeunload", (e) => { if (Topo.dirty) { e.preventDefault(); e.returnValue = ""; } });

/* ---------------------------------------------------------------- jobs */
const ACTIVE = ["running", "queued", "paused", "cancelling"];
const statusLabel = (s) => ({ timed_out: "timed out" }[s] || s);
let currentJob = null, jobItems = [], pollTimer = null, jobSubtab = "activity";
const panelKeys = {}, drafts = {};
async function loadJobs() {
  const jobs = await api("/api/jobs");
  $("#job-list").innerHTML = jobs.length ? jobs.map((j) => `<div class="job-item ${j.id === currentJob ? "sel" : ""}" data-id="${j.id}">
      <div class="t">${esc(j.title)}${j.pending_permissions ? ` <span class="badge">${j.pending_permissions} waiting for you</span>` : ""}</div>
      <div class="m"><span class="status ${j.status}">${statusLabel(j.status)}</span> · ${fmtTime(j.created)} · config v${j.config_version}</div></div>`).join("")
    : `<p class="muted">No jobs yet.</p>`;
  $$(".job-item").forEach((el) => el.onclick = () => openJob(el.dataset.id));
}
async function openJob(id) {
  currentJob = id; jobItems = []; jobSubtab = "activity";
  for (const k in panelKeys) delete panelKeys[k];
  clearTimeout(pollTimer);
  try { history.replaceState(null, "", "#job=" + id); } catch (e) {}
  $("#job-detail").innerHTML = `<p class="muted">Loading…</p>`;
  await pollJob(true); loadJobs();
}
async function pollJob(first = false) {
  if (!currentJob) return;
  clearTimeout(pollTimer);
  const id = currentJob;
  let data;
  try { data = await api(`/api/jobs/${id}?since=${jobItems.length}`); } catch (e) { $("#job-detail").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  if (id !== currentJob) return;
  jobItems.push(...data.items);
  renderJob(data, first);
  const j = data.job;
  const live = ACTIVE.includes(j.status) || (j.improvement && j.improvement.status === "running") || (j.diagnosis && j.diagnosis.status === "running");
  if (live) pollTimer = setTimeout(() => pollJob(), 1500);
  else if (!first) loadJobs();
}
function renderJob(data, first) {
  const j = data.job, m = data.metrics || {}, el = $("#job-detail");
  const active = ACTIVE.includes(j.status);
  if (first || !$("#jd-head")) {
    el.innerHTML = `<div id="jd-head"></div><div id="jd-perms"></div>
      <div class="subtabs"><button data-st="activity">Activity</button><button data-st="result">Result</button><button data-st="files">Files</button></div>
      <div id="jd-body"></div><div id="jd-improve"></div>`;
    $$(".subtabs button", el).forEach((b) => b.onclick = () => { jobSubtab = b.dataset.st; renderSub(j); });
  }
  const tok = m.tokens || {};
  const pauseBtn = j.status === "running" ? `<button id="btn-pause">⏸ Pause</button>` : j.status === "paused" ? `<button class="primary" id="btn-resume">▶ Resume</button>` : "";
  $("#jd-head").innerHTML = `<div class="row"><h2 class="grow" style="margin:0">${esc(j.title)}</h2>
      ${pauseBtn}${active && j.status !== "cancelling" ? `<button class="danger" id="btn-cancel">Cancel</button>` : ""}</div>
    <div class="muted small"><span class="status ${j.status}">${statusLabel(j.status)}</span> · started ${fmtTime(j.started)} · topology v${j.config_version}
      ${j.time_limit_min ? ` · time limit ${j.time_limit_min} min` : ""} · <code>${esc(j.id)}</code></div>
    ${(j.prior || []).length ? `<div class="small builds-on">Builds on: ${j.prior.map((id) => `<a href="#job=${id}" data-open="${id}">${esc(id)}</a>`).join(", ")} <span class="muted">(copies in ./prior/)</span></div>` : ""}
    ${!active && j.kind !== "selftest" ? `<div class="row next-steps"><button id="btn-use">Use in a new job</button><button id="btn-slurm">Make a Slurm version →</button></div>` : ""}
    ${j.status_note ? `<p class="msg">${esc(j.status_note)}…</p>` : ""}
    ${j.status === "paused" ? `<p class="msg">Paused: the agent and every process it started are frozen, and the time limit is on hold. After a long pause the agent may need to retry its last request.</p>` : ""}
    ${j.error && j.status !== "done" ? `<p class="msg err">${esc(String(j.error).slice(0, 800))}</p>` : ""}
    ${j.notify_error ? `<p class="small muted">ntfy: ${esc(j.notify_error)}</p>` : ""}
    <div class="stats">
      ${stat("active time", fmtDur(m.active_s ?? m.duration_s))}${stat("turns", m.num_turns ?? "–")}${stat("tool calls", m.tool_calls ?? 0)}
      ${stat("errors", m.errors ?? 0)}${stat("delegations", (m.delegations || []).length)}
      ${stat("output tokens", fmtK(tok.output_tokens))}${stat("cache-read tokens", fmtK(tok.cache_read_input_tokens))}
      ${m.est_cost_usd != null ? stat("API-equivalent $", m.est_cost_usd.toFixed(2)) : ""}
    </div>
    ${j.require_delegation ? delegationLine(j, m) : ""}
    ${j.selftest ? selftestTable(j.selftest) : ""}`;
  const act = (id, path, confirmMsg) => { const b = $(id); if (b) b.onclick = async () => {
    if (confirmMsg && !confirm(confirmMsg)) return;
    b.disabled = true; await api(`/api/jobs/${j.id}/${path}`, { method: "POST" }).catch((e) => alert(e.message)); pollJob(); }; };
  act("#btn-cancel", "cancel", "Stop this job and every process it started?");
  act("#btn-pause", "pause"); act("#btn-resume", "resume");
  const bu = $("#btn-use"); if (bu) bu.onclick = () => startFrom(j, "use");
  const bs = $("#btn-slurm"); if (bs) bs.onclick = () => startFrom(j, "slurm");
  $$("[data-open]", $("#jd-head")).forEach((a) => a.onclick = (e) => { e.preventDefault(); openJob(a.dataset.open); });
  renderPerms(j, data.permissions || []);
  renderSub(j);
  renderImprove(j, active);
}
function renderPerms(j, perms) {
  const el = $("#jd-perms"), pending = perms.filter((p) => p.status === "pending"), done = perms.filter((p) => p.status !== "pending");
  const key = perms.map((p) => p.id + p.status).join("|");
  if (panelKeys.perms === key) return;
  if (document.activeElement && el.contains(document.activeElement)) return;
  panelKeys.perms = key;
  loadStatus();
  if (!perms.length) { el.innerHTML = ""; return; }
  el.innerHTML = pending.map((p) => `<div class="perm" data-rid="${p.id}">
      <div><b>✋ Permission needed</b> · <span class="muted">${esc(p.agent || "agent")} wants to use</span> <b>${esc(p.tool_name)}</b>
        <span class="muted small">· asked ${fmtTime(p.created)}</span></div>
      <pre>${esc(p.summary)}</pre>
      <div class="row"><input class="grow perm-note" placeholder="Optional note to the agent (e.g. why you denied it)">
        <button class="danger" data-d="deny">Deny</button><button class="primary" data-d="allow">Allow once</button></div>
    </div>`).join("") +
    (done.length ? `<details class="perm-log" ${done.some((p) => p.auto) ? "open" : ""}><summary class="small muted">${done.length} earlier permission request${done.length === 1 ? "" : "s"}</summary>
      ${done.map((p) => `<div class="small"><span class="${p.status === "allowed" ? "ok" : "bad"}">${p.auto ? "auto-allowed" : p.status}</span> · ${esc(p.tool_name)}: <code>${esc(p.summary.slice(0, 160))}</code>${p.message ? ` · “${esc(p.message)}”` : ""}</div>`).join("")}</details>` : "");
  $$(".perm", el).forEach((card) => $$("[data-d]", card).forEach((b) => b.onclick = async () => {
    $$("button", card).forEach((x) => (x.disabled = true));
    try { await api(`/api/jobs/${j.id}/permissions/${card.dataset.rid}/decide`, { json: { behavior: b.dataset.d, message: $(".perm-note", card).value } }); }
    catch (e) { alert(e.message); }
    panelKeys.perms = null; pollJob(); loadStatus();
  }));
}
function delegationLine(j, m) {
  const g = m.guard || {}, n = (m.delegations || []).length, done = !ACTIVE.includes(j.status);
  const warn = done && n === 0;
  let html = `<div class="deleg ${warn ? "warn" : ""}">⇄ <b>Delegation required</b> · ${n} delegation${n === 1 ? "" : "s"}`;
  if (!m.guard) html += ` · <span class="bad">guard hook hasn't reported yet${done ? ": it may not have loaded" : ""}</span>`;
  else html += ` · ${g.blocked} blocked orchestrator action${g.blocked === 1 ? "" : "s"} · ${g.subagent_calls} subagent tool calls checked`;
  if (warn) html += ` · <span class="bad">the orchestrator never delegated</span>`;
  if (g.blocked_samples && g.blocked_samples.length) html += `<details><summary class="small">blocked attempts</summary><pre>${esc(g.blocked_samples.map((b) => b.tool + ": " + b.input).join("\n"))}</pre></details>`;
  return html + `</div>`;
}
const stat = (k, v) => `<div class="stat"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`;
function selftestTable(st) {
  return `<p class="${st.passed ? "ok" : "bad"}">${st.passed ? "✓ Sandbox self-test passed" : "✗ Sandbox self-test found a problem"}</p>
    <table class="checks"><tr><th>check</th><th>expected</th><th>got</th><th>evidence</th></tr>
    ${st.checks.map((c) => `<tr><td>${esc(c.id)}</td><td>${c.expected}</td><td class="${c.ok ? "ok" : "bad"}">${esc(c.got)}</td><td class="small">${esc(c.evidence)}</td></tr>`).join("")}</table>`;
}
const agentColor = (a) => { if (!a || a === "orchestrator") return "#2f5bd3"; let h = 0; for (const ch of a) h = (h * 31 + ch.charCodeAt(0)) % 360; return `hsl(${h} 55% 38%)`; };
const kindIcon = { tool: "⚙", delegate: "↳", error: "✗", result: "★", text: "", system: "•", raw: "" };
function renderSub(j) {
  $$(".subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.st === jobSubtab));
  const body = $("#jd-body");
  if (jobSubtab === "activity") {
    const atBottom = !$(".log", body) || $(".log", body).scrollHeight - $(".log", body).scrollTop - $(".log", body).clientHeight < 40;
    body.innerHTML = `<div class="log">${jobItems.map((it) => `<div class="it ${it.kind}"><div class="tm">${it.t}s</div>
      <div class="ag" style="color:${agentColor(it.agent)}">${esc(it.agent)}</div><div class="tx">${kindIcon[it.kind] ? kindIcon[it.kind] + " " : ""}${esc(it.kind === "text" && it.text.length > 1500 ? it.text.slice(0, 1500) + " …" : it.text)}</div></div>`).join("") || `<p class="muted">Waiting for the first events…</p>`}</div>`;
    const log = $(".log", body); if (atBottom) log.scrollTop = log.scrollHeight;
  } else if (jobSubtab === "result") {
    body.innerHTML = j.result ? `<pre class="view">${esc(j.result)}</pre>` : `<p class="muted">No final result yet.</p>`;
  } else if (jobSubtab === "files" && !$(".files", body)) {
    body.innerHTML = `<div class="files"><div class="list" id="flist">Loading…</div><div id="fview"><p class="muted">Pick a file.</p></div></div>`;
    api(`/api/jobs/${j.id}/files`).then((fs) => {
      $("#flist").innerHTML = fs.map((f) => `<div data-p="${esc(f.path)}" title="${f.size} bytes">${esc(f.path)}</div>`).join("") || "empty";
      $$("#flist [data-p]").forEach((d) => d.onclick = async () => {
        const p = d.dataset.p, url = `/api/jobs/${j.id}/file?path=${encodeURIComponent(p)}`;
        if (/\.(pdf|png|jpe?g|gif|svg)$/i.test(p)) { $("#fview").innerHTML = `<a href="${url}" target="_blank">open ${esc(p)} ↗</a>`; return; }
        const r = await api(url); $("#fview").innerHTML = `<pre class="view">${esc(r.text)}${r.truncated ? "\n… (truncated)" : ""}</pre>`;
      });
    });
  }
}
function renderImprove(j, active) {
  const el = $("#jd-improve");
  if (active || j.kind === "selftest") { el.innerHTML = ""; panelKeys.imp = null; return; }
  const dg = j.diagnosis, imp = j.improvement;
  const key = JSON.stringify([dg && dg.status, dg && dg.n, imp && imp.status, imp && imp.n]);
  if (panelKeys.imp === key) return;
  panelKeys.imp = key;
  let body = "";
  if (!dg) body = `<p class="muted small">This run hasn't been analyzed yet.</p><div class="row"><button class="primary" id="dg-go">Analyze this run</button></div>`;
  else if (dg.status === "running") body = `<div class="imp-msg">🔎 Analyzing this run: finding what slowed it down or made it fail, and drafting an improvement…</div>`;
  else if (dg.status === "failed") body = `<div class="imp-msg"><span class="bad">Analysis failed:</span> ${esc(dg.error)}</div><div class="row"><button id="dg-go">Try again</button></div>`;
  else {
    const issues = (dg.issues || []).map((i) => `<li><b>${esc(i.title)}</b> <span class="muted small">· ${esc(i.cost)}${i.fixable_by_prompts ? "" : " · outside the agents' control"}</span>
        <div class="small muted">${esc(i.evidence)}</div></li>`).join("");
    const busy = imp && imp.status === "running";
    body = `<p><b>${esc(dg.headline)}</b></p>${issues ? `<ul class="issues">${issues}</ul>` : ""}
      <label>Improvement request <span class="muted small">(drafted from this run. Edit it, then submit, or leave it)</span>
        <textarea id="imp-text" rows="8"></textarea></label>
      <div class="row end"><button id="dg-go">Re-analyze</button><button class="primary" id="imp-go" ${busy ? "disabled" : ""}>Submit improvement</button></div>`;
  }
  let card = "";
  if (imp && imp.status === "running") card = `<div class="imp-msg">✍️ Rewriting the prompts from your request…</div>`;
  else if (imp && imp.status === "failed") card = `<div class="imp-msg"><span class="bad">Improvement failed:</span> ${esc(imp.error)}</div>`;
  else if (imp && ["applied", "undone", "no_change"].includes(imp.status)) {
    const head = imp.status === "applied" ? `✨ <b>Config updated to v${imp.version}</b>` : imp.status === "undone" ? `↩ <b>Undone</b> (now v${imp.undo_version})` : `<b>No changes made</b>`;
    card = `<div class="imp-msg ${imp.status === "undone" ? "undone" : ""}">${head}: ${esc(imp.summary)}
      ${(imp.changes || []).map((c) => `<details><summary><b>${esc(c.target)}</b> ${esc(c.field)}: ${esc(c.reason)}</summary><pre>${esc(c.new_text)}</pre></details>`).join("")}
      ${imp.status === "applied" ? `<div class="row end"><button id="imp-undo">Undo</button></div>` : ""}</div>`;
  }
  el.innerHTML = `<div class="improve"><h3>Self-improve</h3>${body}${card}</div>`;
  const ta = $("#imp-text");
  if (ta) {
    const dk = j.id + ":" + dg.n;
    ta.value = drafts[dk] !== undefined ? drafts[dk] : dg.draft || "";
    ta.addEventListener("input", () => (drafts[dk] = ta.value));
  }
  const go = $("#dg-go"); if (go) go.onclick = async () => { await api(`/api/jobs/${j.id}/analyze`, { method: "POST" }).catch((e) => alert(e.message)); pollJob(); };
  const sub = $("#imp-go"); if (sub) sub.onclick = async () => {
    if (!ta.value.trim()) { alert("The improvement request is empty."); return; }
    try { await api(`/api/jobs/${j.id}/improve`, { json: { request: ta.value } }); pollJob(); } catch (e) { alert(e.message); }
  };
  const u = $("#imp-undo"); if (u) u.onclick = async () => { await api(`/api/jobs/${j.id}/improve/undo`, { method: "POST" }).catch((e) => alert(e.message)); Topo.cfg = null; pollJob(); };
}

/* ---------------------------------------------------------------- settings */
const NOTIFY_KINDS = ["done", "failed", "timed_out", "cancelled"];
async function loadSettings() {
  const s = await api("/api/settings"), f = $("#settings-form");
  for (const k of ["ntfy_server", "ntfy_topic", "ntfy_token", "max_turns", "improver_model", "time_limit_min", "permission_timeout_min", "cluster_name"]) f.elements[k].value = s[k] ?? "";
  for (const k of NOTIFY_KINDS) f.elements["on_" + k].checked = (s.notify_on || []).includes(k);
  f.elements.auto_analyze.checked = s.auto_analyze !== false;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, msg = $("#settings-msg"), num = (k) => (f.elements[k].value ? +f.elements[k].value : null);
  const body = { ntfy_server: f.elements.ntfy_server.value.trim() || "https://ntfy.sh", ntfy_topic: f.elements.ntfy_topic.value.trim(),
    ntfy_token: f.elements.ntfy_token.value, improver_model: f.elements.improver_model.value.trim(),
    max_turns: num("max_turns"), cluster_name: f.elements.cluster_name.value.trim(), time_limit_min: num("time_limit_min"), permission_timeout_min: num("permission_timeout_min"),
    auto_analyze: f.elements.auto_analyze.checked, notify_on: NOTIFY_KINDS.filter((k) => f.elements["on_" + k].checked) };
  try { await api("/api/settings", { json: body }); msg.className = "msg"; msg.textContent = "Saved."; loadStatus(); }
  catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
});
$("#btn-test-notify").onclick = async () => {
  const msg = $("#settings-msg");
  try { await api("/api/settings/test-notify", { method: "POST" }); msg.className = "msg"; msg.textContent = "Sent. Check your phone. (Save the settings first if you just changed them.)"; }
  catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
};
$("#btn-selftest").onclick = async () => {
  try { const j = await api("/api/selftest", { method: "POST" }); showTab("jobs"); openJob(j.id); } catch (e) { alert(e.message); }
};

$("#btn-restart").onclick = () => restartServer(true);
$("#btn-shutdown").onclick = async () => {
  if (!confirm("Stop Agent Lab? Open the Agent Lab icon on your Desktop to start it again.")) return;
  try { await api("/api/admin/shutdown", { json: {} }); }
  catch (e) { alert(e.message); return; }
  document.body.innerHTML = `<div class="card narrow" style="margin-top:60px"><h2>Agent Lab has stopped.</h2><p class="muted">Open the Agent Lab icon on your Desktop to start it again.</p></div>`;
};

/* ---------------------------------------------------------------- boot */
let startTab = "run"; try { startTab = localStorage.getItem("tab") || "run"; } catch (e) {}
const hashJob = (location.hash.match(/^#job=([a-z0-9-]+)$/) || [])[1];
if (hashJob) { showTab("jobs"); openJob(hashJob); } else showTab(startTab);
