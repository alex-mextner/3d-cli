// app.js — the 3d web SPA. Vanilla ES modules + three.js (CDN via import-map).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const api = (p) => fetch(p).then((r) => (r.ok ? r : Promise.reject(new Error(`${p} -> ${r.status}`))));

const state = {
  project: null,
  scad: null,
  pending: {},          // constant name -> new value (not yet applied)
  meshObj: null,
  bboxHelper: null,
  axesHelper: null,
  renderSse: null,
};

// ---------------------------------------------------------------------------
// three.js viewer
// ---------------------------------------------------------------------------
const viewer = (() => {
  const el = $("#viewer");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
  camera.position.set(120, -160, 120);
  camera.up.set(0, 0, 1); // OpenSCAD is Z-up
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  el.appendChild(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const key = new THREE.DirectionalLight(0xffffff, 0.9);
  key.position.set(1, -1, 2);
  scene.add(key);
  const grid = new THREE.GridHelper(400, 20, 0x30363d, 0x21262d);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  function resize() {
    const w = el.clientWidth, h = el.clientHeight || 400;
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);
  const ro = new ResizeObserver(resize); ro.observe(el);

  function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); }
  resize(); loop();

  function clearMesh() {
    if (state.meshObj) { scene.remove(state.meshObj); state.meshObj.geometry?.dispose(); }
    if (state.bboxHelper) { scene.remove(state.bboxHelper); state.bboxHelper = null; }
    state.meshObj = null;
  }

  function loadSTL(url) {
    return new Promise((res, rej) => {
      new STLLoader().load(url, (geo) => {
        clearMesh();
        geo.computeVertexNormals();
        geo.computeBoundingBox();
        const mat = new THREE.MeshStandardMaterial({
          color: 0x6fa8ff, metalness: 0.1, roughness: 0.6,
          wireframe: $("#lyr-wire").checked, flatShading: false,
        });
        const mesh = new THREE.Mesh(geo, mat);
        scene.add(mesh);
        state.meshObj = mesh;
        // frame the camera on the bbox
        const bb = geo.boundingBox, c = new THREE.Vector3(); bb.getCenter(c);
        const size = new THREE.Vector3(); bb.getSize(size);
        const d = Math.max(size.x, size.y, size.z) || 50;
        controls.target.copy(c);
        camera.position.set(c.x + d * 1.4, c.y - d * 1.6, c.z + d * 1.2);
        camera.near = d / 100; camera.far = d * 50; camera.updateProjectionMatrix();
        applyLayers();
        res(mesh);
      }, undefined, rej);
    });
  }

  function applyLayers() {
    // axes
    if ($("#lyr-axes").checked && !state.axesHelper) {
      state.axesHelper = new THREE.AxesHelper(60); scene.add(state.axesHelper);
    } else if (!$("#lyr-axes").checked && state.axesHelper) {
      scene.remove(state.axesHelper); state.axesHelper = null;
    }
    // bbox
    if (state.meshObj && $("#lyr-bbox").checked && !state.bboxHelper) {
      state.bboxHelper = new THREE.BoxHelper(state.meshObj, 0xd29922); scene.add(state.bboxHelper);
    } else if (state.bboxHelper && !$("#lyr-bbox").checked) {
      scene.remove(state.bboxHelper); state.bboxHelper = null;
    }
    // wireframe
    if (state.meshObj) state.meshObj.material.wireframe = $("#lyr-wire").checked;
  }

  function setColor(hex) { if (state.meshObj) state.meshObj.material.color.set(hex); }

  return { loadSTL, applyLayers, clearMesh, setColor };
})();

["lyr-axes", "lyr-bbox", "lyr-wire"].forEach((id) =>
  $(`#${id}`).addEventListener("change", viewer.applyLayers));

// ---------------------------------------------------------------------------
// projects
// ---------------------------------------------------------------------------
async function loadProjects() {
  const { projects, project_root } = await api("/api/projects").then((r) => r.json());
  $("#root-label").textContent = project_root;
  const ul = $("#projects"); ul.innerHTML = "";
  for (const p of projects) {
    const li = document.createElement("li");
    const b = document.createElement("b"); b.textContent = p.name;
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = `${p.rel} · ${p.scad_files.length} scad${p.spec ? " · spec" : ""}${p.constants ? " · constants" : ""}`;
    li.append(b, meta);
    li.onclick = () => selectProject(p, li);
    ul.appendChild(li);
  }
  if (projects[0]) selectProject(projects[0], ul.firstChild);
}

async function selectProject(p, li) {
  state.project = p; state.pending = {};
  $$("#projects li").forEach((x) => x.classList.remove("active"));
  li?.classList.add("active");
  // scad selector
  const sel = $("#scad-select"); sel.innerHTML = "";
  for (const f of p.scad_files) {
    const o = document.createElement("option"); o.value = f;
    o.textContent = f.replace(p.path + "/", ""); sel.appendChild(o);
  }
  sel.value = p.primary_scad || p.scad_files[0] || "";
  state.scad = sel.value;
  sel.onchange = () => { state.scad = sel.value; loadModel(); };
  await Promise.all([loadModel(), loadConstants(), loadSpec(), loadColors(), loadAnimations(), loadOverlays()]);
}

// ---------------------------------------------------------------------------
// model render
// ---------------------------------------------------------------------------
function defineQuery() {
  return Object.entries(state.pending).map(([k, v]) => `&D=${encodeURIComponent(k + "=" + v)}`).join("");
}

async function loadModel() {
  if (!state.scad) return;
  const st = $("#render-status"); st.textContent = "rendering…"; st.className = "status busy";
  try {
    const url = `/api/model.stl?path=${encodeURIComponent(state.scad)}${defineQuery()}&_=${Date.now()}`;
    await viewer.loadSTL(url);
    st.textContent = "rendered"; st.className = "status ok";
  } catch (e) {
    st.textContent = "render failed: " + e.message; st.className = "status err";
  }
}

// debounced re-render driven by SSE (param-change live update)
let rerenderTimer = null;
function scheduleRerender() {
  clearTimeout(rerenderTimer);
  rerenderTimer = setTimeout(() => {
    if (state.renderSse) state.renderSse.close();
    const url = `/api/render-sse?path=${encodeURIComponent(state.scad)}${defineQuery()}`;
    const st = $("#render-status");
    state.renderSse = new EventSource(url);
    state.renderSse.addEventListener("render", (ev) => {
      const d = JSON.parse(ev.data);
      if (d.phase === "start") { st.textContent = "re-rendering…"; st.className = "status busy"; }
      else if (d.phase === "done") {
        // rebuild the model URL from our own encoded state — never trust the raw path
        // echoed back in d.url (a .scad path may contain & or #).
        const reload = `/api/model.stl?path=${encodeURIComponent(state.scad)}${defineQuery()}&_=${Date.now()}`;
        viewer.loadSTL(reload).then(() => { st.textContent = "live"; st.className = "status ok"; });
        state.renderSse.close();
      } else if (d.phase === "error") { st.textContent = d.error.split("\n")[0]; st.className = "status err"; state.renderSse.close(); }
    });
  }, 350);
}

// ---------------------------------------------------------------------------
// constants editor — Figma-like scrubbers
// ---------------------------------------------------------------------------
async function loadConstants() {
  const list = $("#constants-list"); list.innerHTML = "";
  const path = state.project.constants;
  if (!path) { list.innerHTML = '<p class="empty">No constants.scad in this project.</p>'; return; }
  const { params } = await api(`/api/constants?path=${encodeURIComponent(path)}`).then((r) => r.json());
  for (const p of params) {
    const row = document.createElement("div"); row.className = "const-row";
    const numeric = p.type === "number" || p.type === "integer";
    const name = document.createElement("div"); name.className = "const-name"; name.textContent = p.name;
    const val = document.createElement(numeric ? "div" : "input");
    val.className = "scrub"; val.textContent = p.value; val.dataset.name = p.name; val.dataset.base = p.value;
    if (numeric) { val.tabIndex = 0; attachScrubber(val, p); }
    else { val.value = p.value; val.readOnly = true; }
    const meta = document.createElement("div");
    meta.innerHTML = `<span class="const-type">${p.type}</span> <span class="const-desc">${p.description || ""}${p.range ? " [" + p.range + "]" : ""}</span>`;
    row.append(name, val, meta); list.appendChild(row);
  }
}

function attachScrubber(el, p) {
  const isInt = p.type === "integer";
  let range = null;
  if (p.range) { const m = p.range.split(":").map(Number); range = m.length === 3 ? { min: m[0], step: m[1], max: m[2] } : { min: m[0], max: m[1] }; }
  const baseStep = isInt ? 1 : 0.1;

  let dragging = false, startX = 0, startVal = 0;
  const read = () => parseFloat(el.textContent);
  const write = (v) => {
    if (range) v = Math.min(range.max, Math.max(range.min, v));
    v = isInt ? Math.round(v) : Math.round(v * 1000) / 1000;
    el.textContent = String(v);
    const changed = String(v) !== el.dataset.base;
    el.classList.toggle("changed", changed);
    if (changed) state.pending[p.name] = String(v); else delete state.pending[p.name];
    scheduleRerender();
  };
  // step modifiers: Shift = fine (×0.1), Alt = coarse (×10)
  const stepFor = (e) => baseStep * (e.shiftKey ? 0.1 : e.altKey ? 10 : 1) * (range && range.step ? range.step / baseStep : 1);

  el.addEventListener("mousedown", (e) => {
    dragging = true; startX = e.clientX; startVal = read(); e.preventDefault();
    el.requestPointerLock?.();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = (e.movementX !== undefined && document.pointerLockElement === el) ? e.movementX : e.clientX - startX;
    if (document.pointerLockElement === el) startVal = read();
    write(startVal + dx * stepFor(e) * (document.pointerLockElement === el ? 1 : 1));
    if (document.pointerLockElement !== el) startVal = read(), startX = e.clientX;
  });
  window.addEventListener("mouseup", () => { if (dragging) { dragging = false; document.exitPointerLock?.(); } });
  // keyboard arrows also scrub (accessibility)
  el.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp") { write(read() + stepFor(e)); e.preventDefault(); }
    else if (e.key === "ArrowDown") { write(read() - stepFor(e)); e.preventDefault(); }
  });
  // wheel scrub
  el.addEventListener("wheel", (e) => { e.preventDefault(); write(read() + (e.deltaY < 0 ? 1 : -1) * stepFor(e)); }, { passive: false });
}

$("#apply-constants").onclick = async () => {
  const st = $("#apply-status");
  if (!state.project?.constants || !Object.keys(state.pending).length) { st.textContent = "nothing to apply"; return; }
  st.textContent = "saving…"; st.className = "status busy";
  const r = await fetch("/api/constants/apply", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: state.project.constants, changes: state.pending }),
  }).then((r) => r.json());
  st.textContent = `applied ${Object.keys(r.applied).length} value(s)`; st.className = "status ok";
  state.pending = {};
  loadConstants();
};

// ---------------------------------------------------------------------------
// spec / colors / animations / overlays
// ---------------------------------------------------------------------------
async function loadSpec() {
  const el = $("#spec-html");
  if (!state.project.spec) { el.innerHTML = '<p class="empty">No SPEC.md.</p>'; return; }
  el.innerHTML = await api(`/api/spec?path=${encodeURIComponent(state.project.spec)}`).then((r) => r.text());
}

async function loadColors() {
  const list = $("#colors-list"); list.innerHTML = "";
  const { colors, yaml } = await api(`/api/colors?path=${encodeURIComponent(state.project.path)}`).then((r) => r.json());
  $("#colors-status").textContent = yaml ? `3d.yaml` : "viewer-only (no 3d.yaml)";
  const entries = Object.entries(colors).length ? Object.entries(colors) : [["model", "#6fa8ff"]];
  for (const [part, col] of entries) {
    const row = document.createElement("div"); row.className = "colors-row";
    const inp = document.createElement("input"); inp.type = "color"; inp.value = toHex(col);
    inp.oninput = () => viewer.setColor(inp.value);
    inp.dataset.part = part;
    const lbl = document.createElement("span"); lbl.textContent = part;
    row.append(inp, lbl); list.appendChild(row);
  }
}
function toHex(c) { return /^#/.test(c) ? c : "#6fa8ff"; }

$("#save-colors").onclick = async () => {
  const colors = {}; $$("#colors-list input[type=color]").forEach((i) => (colors[i.dataset.part] = i.value));
  const r = await fetch("/api/colors", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: state.project.path, colors }),
  }).then((r) => r.json());
  $("#colors-status").textContent = r.ok ? "saved to 3d.yaml" : (r.reason || "saved (viewer-only)");
};

async function loadAnimations() {
  const el = $("#anim-list"); el.innerHTML = "";
  const { animations } = await api(`/api/animations?path=${encodeURIComponent(state.project.path)}`).then((r) => r.json());
  if (!animations.length) { el.innerHTML = '<p class="empty">No animations found (mp4/webm/gif).</p>'; return; }
  for (const a of animations) {
    const wrap = document.createElement("div");
    const p = document.createElement("p"); p.textContent = a.split("/").pop();
    const src = `/api/animation?path=${encodeURIComponent(a)}`;
    const media = document.createElement(/\.gif$/i.test(a) ? "img" : "video");
    media.src = src;
    if (media.tagName === "VIDEO") media.controls = true; else media.style.maxWidth = "100%";
    wrap.append(p, media);
    el.appendChild(wrap);
  }
}

async function loadOverlays() {
  // analytical-layer PNGs (silhouette / score / collision / debug renders) — the server
  // scans previews/ AND match/ AND verify/, so always query regardless of previews_dir.
  const el = $("#overlay-list"); el.innerHTML = "";
  clearCompare();
  const r = await fetch(`/api/overlays?path=${encodeURIComponent(state.project.path)}`);
  if (!r.ok) { el.innerHTML = '<span class="dim">no overlays</span>'; return; }
  const { overlays } = await r.json();
  if (!overlays?.length) { el.innerHTML = '<span class="dim">no PNG overlays in previews/, match/, verify/</span>'; return; }
  for (const o of overlays) {
    const fig = document.createElement("figure");
    const img = document.createElement("img");
    img.src = `/api/preview?path=${encodeURIComponent(o)}`;
    img.title = o;
    const cap = document.createElement("figcaption");
    cap.textContent = o.split("/").pop();
    fig.append(img, cap);
    fig.onclick = () => pickOverlay(o, fig, img.src);
    el.appendChild(fig);
  }
}

// before/after compare of two analytical layers: click sets A, next click sets B.
const cmp = { a: null, b: null };
function pickOverlay(path, fig, src) {
  if (!cmp.a) { cmp.a = { path, fig, src }; }
  else if (!cmp.b && fig !== cmp.a.fig) { cmp.b = { path, fig, src }; }
  else { clearCompare(); cmp.a = { path, fig, src }; }
  renderCompare();
}
function renderCompare() {
  $$("#overlay-list figure").forEach((f) => f.classList.remove("sel-a", "sel-b"));
  cmp.a?.fig.classList.add("sel-a");
  cmp.b?.fig.classList.add("sel-b");
  const box = $("#compare");
  if (!cmp.a) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("#cmp-a").src = cmp.a.src;
  $("#cmp-a-name").textContent = cmp.a.path.split("/").pop();
  if (cmp.b) {
    $("#cmp-b").src = cmp.b.src;
    $("#cmp-b-name").textContent = cmp.b.path.split("/").pop();
    $("#cmp-b-wrap").style.display = "";
  } else {
    $("#cmp-b-name").textContent = "(click a 2nd layer)";
    $("#cmp-b-wrap").style.display = "none";
  }
  syncCompareWidth();
}
function syncCompareWidth() {
  const a = $("#cmp-a");
  const w = a.clientWidth || 520;
  $("#cmp-b-wrap").style.setProperty("--cmp-w", w + "px");
  applyWipe();
}
function applyWipe() {
  const pct = $("#cmp-wipe").value;
  $("#cmp-b-wrap").style.width = pct + "%";
}
function clearCompare() {
  cmp.a = cmp.b = null;
  $("#compare").classList.add("hidden");
  $$("#overlay-list figure").forEach((f) => f.classList.remove("sel-a", "sel-b"));
}
$("#cmp-wipe").oninput = applyWipe;
$("#cmp-clear").onclick = clearCompare;
$("#cmp-a").onload = syncCompareWidth;
window.addEventListener("resize", syncCompareWidth);

// ---------------------------------------------------------------------------
// agents — discovery list + live SSE feed
// ---------------------------------------------------------------------------
async function loadAgents() {
  const { sessions } = await api("/api/agents").then((r) => r.json());
  const ul = $("#agents"); ul.innerHTML = "";
  let active = 0;
  for (const s of sessions.slice(0, 60)) {
    if (s.active) active++;
    const li = document.createElement("li");
    const proj = s.project ? s.project.split("/").pop() : "—";
    const src = escapeHtml(s.source);
    li.innerHTML = `<span class="dot ${s.active ? "active" : ""}"></span><span class="src-tag src-${src}">${src}</span> <span class="lbl"></span><span class="meta"></span>`;
    li.querySelector(".lbl").textContent = " " + (s.label || "").slice(0, 22);
    li.querySelector(".meta").textContent = `proj: ${proj} · ${s.events} ev`;
    ul.appendChild(li);
  }
  $("#agent-count").textContent = active;
}

function startAgentFeed() {
  const feed = $("#feed");
  const es = new EventSource("/api/agents/sse");
  es.addEventListener("agent", (ev) => {
    const d = JSON.parse(ev.data);
    if (d.phase) return; // connected marker
    const allowed = $$(".src-filter:checked").map((c) => c.value);
    if (!allowed.includes(d.source)) return;
    const div = document.createElement("div");
    div.className = `event ${d.kind}${d.is_error ? " err" : ""}`;
    const src = escapeHtml(d.source), kind = escapeHtml(d.kind), role = escapeHtml(d.role || "");
    const path = d.paths?.[0] ? `<span class="epath">${escapeHtml(d.paths[0].split("/").slice(-2).join("/"))}</span>` : "";
    div.innerHTML = `<div class="ehead"><span class="src-tag src-${src}">${src}</span> <b>${kind}</b> ${role} ${path}</div><div class="etext">${escapeHtml(d.text).slice(0, 400)}</div>`;
    feed.prepend(div);
    while (feed.children.length > 200) feed.lastChild.remove();
  });
  es.onerror = () => {/* EventSource auto-reconnects */};
}
function escapeHtml(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

// ---------------------------------------------------------------------------
// tabs + boot
// ---------------------------------------------------------------------------
$$(".tab").forEach((t) => t.onclick = () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#tab-${t.dataset.tab}`).classList.add("active");
});
$("#refresh").onclick = () => { loadProjects(); loadAgents(); };

loadProjects();
loadAgents();
startAgentFeed();
setInterval(loadAgents, 8000);
