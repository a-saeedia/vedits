/* vedits editor — CapCut-style timeline editor driven by the same project JSON
   an agent can POST to /api/render. */
"use strict";

const PX_PER_SEC = 30;          // base zoom
let pxPerSec = PX_PER_SEC;
const TRANSITIONS = ["fade", "dissolve", "wipeleft", "wiperight", "slideup", "slidedown", "circleopen", "fadeblack"];

const state = {
  clips: [],        // { id, src, name, w, h, dur, start, end, text, textSize, textColor, speed, transTo, transDur }
  media: [],        // media bin items
  selectedId: null,
  presets: [],
  preset: null,
};

const $ = (s) => document.querySelector(s);
const el = (t, attrs = {}, ...kids) => {
  const e = document.createElement(t);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  });
  kids.forEach((k) => e.appendChild(typeof k === "string" ? document.createTextNode(k) : k));
  return e;
};

/* ---------------- init ---------------- */
async function init() {
  await loadPresets();
  bindEvents();
  initAgentPanel();
}

async function loadPresets() {
  const res = await fetch("/api/presets");
  state.presets = Object.values(await res.json());
  const sel = $("#presetSel");
  sel.innerHTML = "";
  state.presets.forEach((p) => {
    const o = el("option", { value: p.name }, `${p.name} (${p.width}×${p.height})`);
    sel.appendChild(o);
  });
  state.preset = state.presets[0];
  sel.onchange = () => {
    state.preset = state.presets.find((p) => p.name === sel.value);
    updatePreviewMeta();
  };
  // keep pipeline/watch preset selects in sync
  const pipeSel = $("#pipePreset"), watchSel = $("#watchPreset");
  [pipeSel, watchSel].forEach((s) => {
    s.innerHTML = "";
    state.presets.forEach((p) => s.appendChild(el("option", { value: p.name }, p.name)));
  });
  pipeSel.value = "tiktok_1080";
  watchSel.value = "tiktok_1080";
}

function bindEvents() {
  $("#addMediaBtn").onclick = () => $("#mediaFile").click();
  $("#mediaFile").onchange = (e) => handleFiles(e.target.files);
  $("#clearTimeline").onclick = () => { state.clips = []; state.selectedId = null; renderTimeline(); renderInspector(); };
  $("#zoomIn").onclick = () => { pxPerSec = Math.min(120, pxPerSec * 1.4); renderTimeline(); };
  $("#zoomOut").onclick = () => { pxPerSec = Math.max(8, pxPerSec / 1.4); renderTimeline(); };
  $("#previewBtn").onclick = previewRender;
  $("#exportBtn").onclick = () => doRender(false);
  // agent + pipeline
  $("#agentSend").onclick = agentChat;
  $("#agentInput").onkeydown = (e) => { if (e.key === "Enter") agentChat(); };
  $("#pipeRun").onclick = runSmartCut;
  $("#watchToggle").onclick = toggleWatch;
}

/* ---------------- media add ---------------- */
function handleFiles(files) {
  Array.from(files).forEach((file) => addMediaFile(file));
}

async function addMediaFile(file) {
  // Upload to the server so the renderer (ffmpeg, server-side) can read it.
  let src, url;
  try {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const up = await fetch("/api/upload", { method: "POST", body: fd });
    const ud = await up.json();
    if (!ud.ok) throw new Error(ud.error || "upload failed");
    src = ud.src;
    url = ud.url;
  } catch (e) {
    toast("Upload failed: " + e.message, true);
    return;
  }
  const meta = await getVideoMeta(url, file.name);
  const item = { id: "m" + Math.random().toString(36).slice(2, 8), file, url, src, name: file.name, w: meta.w, h: meta.h, dur: meta.dur };
  addMediaItemToPanel(item);
}

function getVideoMeta(url, name) {
  return new Promise((resolve) => {
    const v = document.createElement("video");
    v.preload = "metadata";
    v.src = url;
    v.onloadedmetadata = () => resolve({ w: v.videoWidth, h: v.videoHeight, dur: v.duration || 0 });
    v.onerror = () => resolve({ w: 0, h: 0, dur: 0 });
  });
}

function addMediaItemToPanel(item) {
  state.media.push(item);
  const row = el("div", { class: "media-item", draggable: "true" });
  row.appendChild(el("img", { class: "thumb", src: item.url }));
  const meta = el("div", { class: "meta" });
  meta.appendChild(el("div", { class: "name" }, item.name));
  meta.appendChild(el("div", { class: "spec" }, `${item.w}×${item.h} · ${item.dur.toFixed(1)}s`));
  row.appendChild(meta);
  const addBtn = el("button", { class: "btn small", title: "Add to timeline" }, "＋");
  addBtn.onclick = () => addToTimeline(item);
  row.appendChild(addBtn);

  // drag to drop onto timeline tracks
  row.ondragstart = (e) => { e.dataTransfer.setData("text/plain", item.id); };
  $("#mediaList").appendChild(row);
  window._mediaItems = window._mediaItems || {};
  window._mediaItems[item.id] = item;
}

function addToTimeline(item, at = null) {
  const clip = {
    id: "c" + Math.random().toString(36).slice(2, 8),
    src: item.src,          // server-resolvable path for ffmpeg
    url: item.url,          // served URL for browser preview
    name: item.name,
    dur: item.dur,
    start: 0,
    end: null,          // null = full
    text: "",
    textSize: 48,
    textColor: "white",
    speed: 1.0,
    transTo: null,
    transDur: 0.5,
  };
  if (at == null) state.clips.push(clip);
  else state.clips.splice(at, 0, clip);
  state.selectedId = clip.id;
  renderTimeline();
  renderInspector();
}

/* ---------------- timeline render ---------------- */
function rmsClip(clip) {
  const d = clipOutDur(clip);
  const w = Math.max(24, d * pxPerSec);
  const box = el("div", {
    class: "tl-clip" + (clip.text ? " text-clip" : ""),
    style: `width:${w}px`,
    onclick: () => { state.selectedId = clip.id; renderTimeline(); renderInspector(); },
  });
  const label = el("div", { class: "clip-label" }, `${clip.name}${clip.speed !== 1 ? ` ×${clip.speed}` : ""}`);
  box.appendChild(label);
  if (clip.text) box.appendChild(el("div", { class: "clip-label", style: "color:#f0c08a" }, `“${clip.text}”`));

  // trim handles
  const hl = el("div", { class: "handle l", title: "drag to trim start" });
  hl.addEventListener("pointerdown", (e) => startTrim(e, clip, "l"));
  const hr = el("div", { class: "handle r", title: "drag to trim end / transition" });
  hr.addEventListener("pointerdown", (e) => startTrim(e, clip, "r"));
  box.appendChild(hl); box.appendChild(hr);

  if (clip.transTo) box.appendChild(el("div", { class: "trans-badge" }, `★${clip.transTo}`));

  // drag to reorder
  box.draggable = true;
  const idx = state.clips.indexOf(clip);
  box.ondragstart = (e) => { e.dataTransfer.setData("text/clip", String(idx)); };
  return box;
}

function renderTimeline() {
  // Header with time ruler per track is simplified: single label row.
  const header = $("#trackHeader");
  header.innerHTML = "";
  header.appendChild(el("div", { style: "width:70px" }, ""));
  header.appendChild(el("div", { style: "flex:1" }, `Timeline (${timelineDur().toFixed(1)}s total)`));

  const trackV = $("#trackVideo");
  trackV.innerHTML = "";
  state.clips.forEach((clip) => {
    const box = rmsClip(clip);
    trackV.appendChild(box);
  });
  trackV.ondragover = (e) => e.preventDefault();
  trackV.ondrop = (e) => {
    e.preventDefault();
    const srcIdx = parseInt(e.dataTransfer.getData("text/clip"), 10);
    if (!isNaN(srcIdx)) {
      const [c] = state.clips.splice(srcIdx, 1);
      state.clips.push(c);
      renderTimeline(); renderInspector();
    }
  };

  // text summary track
  const trackT = $("#trackText");
  trackT.innerHTML = "";
  state.clips.filter((c) => c.text).forEach((clip) => {
    const d = clipOutDur(clip);
    const w = Math.max(24, d * pxPerSec);
    trackT.appendChild(el("div", { class: "tl-clip text-clip", style: `width:${w}px` },
      el("div", { class: "clip-label" }, `“${clip.text}”`)));
  });

  $("#previewVideo").style.display = state.clips.length ? "none" : "none";
  $("#previewEmpty").style.display = state.clips.length ? "none" : "flex";
  updatePreviewMeta();
}

/* ---------------- trim / reorder interaction ---------------- */
function startTrim(e, clip, side) {
  e.stopPropagation();
  const startX = e.clientX;
  const origStart = clip.start;
  const origEnd = clip.end;
  const move = (ev) => {
    const dx = (ev.clientX - startX) / pxPerSec;
    if (side === "l") {
      clip.start = Math.max(0, Math.min(clip.dur - 0.1, (origStart ?? 0) + dx));
    } else {
      const base = clip.end != null ? clip.end : clip.dur;
      let ne = base + dx;
      ne = Math.min(clip.dur, Math.max((clip.start ?? 0) + 0.1, ne));
      clip.end = ne;
    }
    renderTimeline();
  };
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

/* ---------------- duration helpers ---------------- */
function clipOutDur(clip) {
  const src = clip.end != null ? clip.end - (clip.start ?? 0) : clip.dur - (clip.start ?? 0);
  return Math.max(0.1, src / clip.speed);
}
function timelineDur() {
  let total = 0;
  state.clips.forEach((c, i) => {
    total += clipOutDur(c);
    if (i + 1 < state.clips.length && c.transTo && c.transDur) total -= c.transDur;
  });
  return total;
}

/* ---------------- inspector ---------------- */
function renderInspector() {
  const body = $("#inspectorBody");
  body.innerHTML = "";
  const clip = state.clips.find((c) => c.id === state.selectedId);
  if (!clip) { body.appendChild(el("em", {}, "Select a clip on the timeline")); return; }

  body.appendChild(el("div", { class: "field", }, el("label", {}, `Name`), el("input", {
    type: "text", value: clip.name, oninput: (e) => { clip.name = e.target.value; renderTimeline(); },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Trim start (s)`), el("input", {
    type: "number", step: "0.1", min: "0", value: clip.start ?? 0,
    onchange: (e) => { clip.start = Math.max(0, parseFloat(e.target.value) || 0); renderTimeline(); },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Trim end (s) — empty = full`), el("input", {
    type: "number", step: "0.1", min: "0", max: clip.dur, value: clip.end ?? "",
    onchange: (e) => { clip.end = e.target.value === "" ? null : Math.min(clip.dur, parseFloat(e.target.value) || clip.dur); renderTimeline(); },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Speed (0.25–4)`), el("input", {
    type: "number", step: "0.05", min: "0.25", max: "4", value: clip.speed,
    onchange: (e) => { clip.speed = Math.min(4, Math.max(0.25, parseFloat(e.target.value) || 1)); renderTimeline(); },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Caption text`), el("input", {
    type: "text", value: clip.text, placeholder: "e.g. “Look at this!”",
    oninput: (e) => { clip.text = e.target.value; renderTimeline(); },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Caption size`), el("input", {
    type: "number", min: "12", max: "200", value: clip.textSize,
    onchange: (e) => { clip.textSize = parseInt(e.target.value) || 48; },
  })));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Transition to NEXT clip`), el("select", {
    onchange: (e) => { clip.transTo = e.target.value || null; renderTimeline(); },
  }, ...(() => {
    const opts = [el("option", { value: "" }, "(none)")];
    TRANSITIONS.forEach((t) => opts.push(el("option", { value: t, selected: clip.transTo === t ? "selected" : null }, t)));
    return opts;
  })())));
  body.appendChild(el("div", { class: "field" }, el("label", {}, `Transition duration (s)`), el("input", {
    type: "number", step: "0.1", min: "0.1", max: "2", value: clip.transDur,
    onchange: (e) => { clip.transDur = parseFloat(e.target.value) || 0.5; },
  })));
  body.appendChild(el("button", {
    class: "btn danger", onclick: () => { removeClip(clip.id); },
  }, "Remove from timeline"));
}

function removeClip(id) {
  state.clips = state.clips.filter((c) => c.id !== id);
  state.selectedId = null;
  renderTimeline(); renderInspector();
}

/* ---------------- preview / export ---------------- */
function buildProject() {
  return {
    preset: state.preset.name,
    fps: state.preset.fps,
    clips: state.clips.map((c) => ({
      id: c.id,
      src: c.src,   // server filesystem path (media/xxx.mp4), NOT the /media/ URL
      start: c.start || undefined,
      end: c.end || undefined,
      text: c.text || undefined,
      text_size: c.textSize,
      text_color: c.textColor,
      speed: c.speed,
      transition_to_next: c.transTo || undefined,
      transition_duration: c.transDur,
    })),
  };
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " error" : "");
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), 4000);
}

async function doRender(preview) {
  if (!state.clips.length) return toast("Add clips to the timeline first", true);
  const proj = buildProject();
  toast("Rendering… this may take a moment");
  try {
    const res = await fetch("/api/render", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(proj),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "render failed");
    if (preview) {
      const v = $("#previewVideo");
      v.src = data.output + "?t=" + Date.now();
      v.style.display = "block";
      $("#previewEmpty").style.display = "none";
      v.play().catch(() => {});
      toast("Preview ready");
    } else {
      showExport(data);
    }
    updatePreviewMeta();
  } catch (e) {
    toast(e.message, true);
  }
}

function previewRender() { doRender(true); }

function showExport(data) {
  const a = document.createElement("a");
  a.href = data.output;
  a.download = "vedits_export.mp4";
  a.click();
  toast("Exported ✓");
}

function updatePreviewMeta() {
  $("#previewMeta").textContent = state.preset
    ? `${state.preset.name} · ${state.preset.width}×${state.preset.height} · ${timelineDur().toFixed(1)}s · ${state.clips.length} clips`
    : "";
}

/* ---------------- agent console ---------------- */
function agentLogLine(cls, text) {
  const log = $("#agentLog");
  const div = el("div", { class: cls }, text);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

let agentBusy = false;
async function agentChat() {
  const input = $("#agentInput");
  const text = input.value.trim();
  if (!text || agentBusy) return;
  agentBusy = true;
  input.value = "";
  agentLogLine("u", "you » " + text);
  const spin = agentLogLine("t", "agent working…");
  try {
    const res = await fetch("/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    spin.remove();
    (data.steps || []).forEach((s) =>
      agentLogLine(s.ok ? "t" : "err", `  ↳ ${s.tool} ${s.ok ? "✓" : "✗"} ` +
        JSON.stringify(s.args).slice(0, 100) + (s.ok ? "" : " → " + (s.result.error || ""))));
    agentLogLine("a", "agent » " + data.reply);
  } catch (e) {
    spin.remove();
    agentLogLine("err", "agent error » " + e.message);
  }
  agentBusy = false;
}

async function initAgentPanel() {
  try {
    const res = await fetch("/api/agent/tools");
    const data = await res.json();
    $("#agentStatus").textContent = `${data.llm} · tools: ${data.tools.join(", ")}`;
  } catch (e) { $("#agentStatus").textContent = "offline"; }
  refreshWatch();
}

/* ---------------- pipeline: smart cut ---------------- */
let pipeBusy = false;
async function runSmartCut() {
  if (pipeBusy) return;
  pipeBusy = true;
  // source: explicit path, else first media item
  let src = $("#pipeSrc").value.trim();
  if (!src && state.media && state.media.length) src = state.media[0].src;
  if (!src) { toast("Give a source path or add media first", true); pipeBusy = false; return; }
  const status = $("#pipeStatus");
  status.innerHTML = "";
  status.appendChild(el("div", { class: "stage" }, "pipeline running… (analyze → plan → render)"));
  try {
    const res = await fetch("/api/pipeline/smartcut", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        src, preset: $("#pipePreset").value,
        min_dur: parseFloat($("#pipeMin").value) || 3,
        max_dur: parseFloat($("#pipeMax").value) || 45,
        captions: $("#pipeCaps").checked,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    status.innerHTML = "";
    (data.log || []).forEach((l) => status.appendChild(el("div", {}, `[${l.stage}] ${l.message}`)));
    const done = el("div", { class: "done" }, data.summary);
    status.appendChild(done);
    (data.outputs_url || []).forEach((u) => {
      const a = el("a", { class: "out-link", href: u, target: "_blank" }, "▶ " + u);
      status.appendChild(a);
    });
    toast("Pipeline done ✓");
  } catch (e) {
    status.appendChild(el("div", { class: "err" }, "error » " + e.message));
    toast(e.message, true);
  }
  pipeBusy = false;
}

/* ---------------- watch folder ---------------- */
async function toggleWatch() {
  const btn = $("#watchToggle");
  const folder = $("#watchFolder").value.trim() || "watch";
  const enabling = btn.textContent === "Enable";
  try {
    const res = await fetch("/api/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, preset: $("#watchPreset").value, enabled: enabling }),
    });
    const data = await res.json();
    btn.textContent = data.enabled ? "Disable" : "Enable";
    showWatch(data);
    if (data.enabled) { toast("Watching " + data.folder); pollWatch(); }
  } catch (e) { toast(e.message, true); }
}

function showWatch(data) {
  const s = $("#watchStatus");
  s.innerHTML = "";
  s.appendChild(el("div", {}, `folder: ${data.folder} · preset: ${data.preset} · ${data.enabled ? "ON" : "OFF"}`));
  if (data.last) s.appendChild(el("div", {}, "last: " + data.last));
  (data.processed || []).forEach((p) => s.appendChild(el("div", {}, "done: " + p)));
}

async function refreshWatch() {
  try {
    const res = await fetch("/api/watch");
    const data = await res.json();
    $("#watchToggle").textContent = data.enabled ? "Disable" : "Enable";
    if (data.folder) $("#watchFolder").value = data.folder;
    showWatch(data);
  } catch (e) { /* server not ready */ }
}

function pollWatch() {
  setTimeout(async () => {
    try {
      const res = await fetch("/api/watch");
      const data = await res.json();
      showWatch(data);
      if (data.enabled) pollWatch();
    } catch (e) { /* ignore */ }
  }, 3000);
}

init();
