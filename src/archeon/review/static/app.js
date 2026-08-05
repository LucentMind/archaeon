const nav = document.getElementById("nav");
const cards = document.getElementById("cards");
let state = { component: null, cluster: null, focus: -1, list: [] };

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function bar(row) {
  const seg = (k) => row[k] ? `<div class="${k}" style="flex:${row[k]}"></div>` : "";
  return `<div class="bar">${seg("verified")}${seg("contested")}${seg("unrecovered")}${seg("rejected")}</div>`;
}

async function showComponents() {
  state.component = state.cluster = null;
  const comps = await api("/api/components");
  nav.innerHTML = "<h3>Components</h3>" + comps.map((c) =>
    `<button class="tile" data-comp="${encodeURIComponent(c.component)}">
       ${c.component} <span class="meta">(${c.total})</span>${bar(c)}</button>`).join("");
  nav.querySelectorAll("[data-comp]").forEach((b) =>
    b.onclick = () => showClusters(decodeURIComponent(b.dataset.comp)));
  cards.innerHTML = "<p class='hint'>Pick a component.</p>";
}

async function showClusters(component) {
  state.component = component; state.cluster = null;
  const cl = await api("/api/clusters?component=" + encodeURIComponent(component));
  nav.innerHTML = `<button class="tile" id="back">← components</button>
    <h3>${component}</h3>` + cl.map((c) =>
    `<button class="tile" data-cl="${encodeURIComponent(c.cluster)}">
       ${c.cluster} <span class="meta">(${c.total})${c.clustered ? " ●" : ""}</span>${bar(c)}</button>`).join("");
  document.getElementById("back").onclick = showComponents;
  nav.querySelectorAll("[data-cl]").forEach((b) =>
    b.onclick = () => showCards(component, decodeURIComponent(b.dataset.cl)));
}

async function showCards(component, cluster) {
  state.component = component; state.cluster = cluster;
  state.list = await api(`/api/claims?component=${encodeURIComponent(component)}&cluster=${encodeURIComponent(cluster)}`);
  renderCards();
}

async function showQueue() {
  state.component = "__queue__";
  state.list = await api("/api/queue");
  nav.innerHTML = "<h3>Queue</h3><p class='hint'>impact × uncertainty; contested first.</p>";
  renderCards();
}

function grammar(spec) {
  if (!spec) return "";
  if (spec.mode === "prose") return `<p>${spec.text}</p>`;
  if (spec.mode === "table")
    return `<table class="grammar">` +
      spec.rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("") + `</table>`;
  if (spec.mode === "mermaid") {
    const pre = `<pre class="mermaid">${spec.src}</pre>`;
    if (window.mermaid) queueMicrotask(() => window.mermaid.run());
    return pre + `<div class="meta">${spec.caption || ""}</div>`;
  }
  return "";
}

function renderCards() {
  state.focus = state.list.length ? 0 : -1;
  cards.innerHTML = state.list.map((c, i) => {
    if (c.broken)
      return `<div class="card broken" data-i="${i}"><b>${c.id}</b> — parse error: ${c.error}</div>`;
    const ev = (c.evidence || []).map((e) =>
      `<div class="evidence">${e.ref}\n${e.excerpt || ""}</div>`).join("");
    const ce = (c.counter_evidence || []).length
      ? `<div class="counter">⚠ ${c.counter_evidence.join("; ")}</div>` : "";
    return `<div class="card ${c.bucket}" data-i="${i}">
      <div class="meta">${c.id} · ${c.type} · ${c.status} · conf ${c.confidence}</div>
      <p class="statement">${c.statement}</p>
      ${grammar(c.render)}${ev}${ce}</div>`;
  }).join("") || "<p class='hint'>No claims here.</p>";
  updateFocus();
  cards.querySelectorAll("[data-i]").forEach((el) =>
    el.onclick = () => { state.focus = +el.dataset.i; updateFocus(); });
}

function updateFocus() {
  cards.querySelectorAll(".card").forEach((el) =>
    el.classList.toggle("focus", +el.dataset.i === state.focus));
}

async function act(status, statement) {
  const c = state.list[state.focus];
  if (!c || c.broken) return;
  const body = { status, version: c.version };
  if (statement != null) body.statement = statement;
  const r = await fetch("/api/claims/" + encodeURIComponent(c.id), {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body) });
  if (r.status === 409) { alert("This claim changed on disk. Reloading."); return reloadList(); }
  if (!r.ok) { alert("Write failed: " + (await r.text())); return; }
  const out = await r.json();
  c.version = out.version; c.status = status;
  if (statement != null) c.statement = statement;
  c.bucket = status === "rejected" ? "rejected"
           : status === "expert_accepted" ? "verified" : c.bucket;
  renderCards();
}

function reloadList() {
  if (state.component === "__queue__") return showQueue();
  if (state.cluster != null) return showCards(state.component, state.cluster);
  return showComponents();
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || state.focus < 0) return;
  if (e.key === "a") act("expert_accepted");
  else if (e.key === "r") act("rejected");
  else if (e.key === "e") {
    const c = state.list[state.focus];
    const next = prompt("Edit statement:", c ? c.statement : "");
    if (next != null) act("expert_accepted", next);
  }
});

document.getElementById("tab-browse").onclick = (e) => {
  setTab(e.target); showComponents();
};
document.getElementById("tab-queue").onclick = (e) => {
  setTab(e.target); showQueue();
};
function setTab(btn) {
  document.querySelectorAll("header button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
}

showComponents();
