import { buildCity } from "./map.js?v=20261002-final";

const $ = (id) => document.getElementById(id);
const body = document.body;
const EXAMPLES = [
  ["Pedestrians & vehicles", "Show me how pedestrians and vehicles interact at Toronto intersections."],
  ["Cyclists sharing the road", "How do cyclists share the road with cars?"],
  ["Streetcars & tram tracks", "What happens around streetcars and tram tracks?"],
  ["Slowing for road work", "Where do drivers slow down for road work?"],
  ["Streets in the rain", "Show me Toronto streets in the rain"],
];
const STAGES = ["search", "retrieve", "select", "write", "render"];
const RENDER_STEPS = {
  "voicing narration + gathering footage": "Recording the voice-over",
  "timing cuts to the voice": "Timing cuts to the voice",
  "cutting shots": "Cutting six shots",
  "mixing narration": "Mixing picture and voice",
  "poster": "Pulling the poster frame",
  "done": "Final cut ready",
};
const GRID_SLOTS = 16;
const CHECK = '<svg viewBox="0 0 16 16"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let city = null;
let edl = null;
let source = null;
let narrationKey = "";
let raf = 0;
let currentShot = -1;
let run = 0;
let moments = 1080;
let pool = [];
let picksKey = "";
let retrieved = 0;

const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const pad2 = (n) => String(n).padStart(2, "0");
const driveNo = (d) => d.replace("set", "");
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const STREET_RE = /\b((?:[A-Z][a-z]+\s){1,2}(?:Street|Avenue|Road|Boulevard|Drive|Lane|Crescent|Way|Place|Square|St|Ave|Rd)\b(?:\s(?:East|West|North|South|E|W))?)/g;
const streetsIn = (text) => [...new Set((text || "").match(STREET_RE) || [])];
const later = (ms, fn) => { const id = run; setTimeout(() => { if (id === run) fn(); }, ms); };

function setPhase(p) { body.dataset.phase = p; }
function status(title, detail = "") { $("status-line").textContent = title; $("status-detail").textContent = detail; }
function progress(p, seconds = 1.2) {
  const bar = $("progress");
  bar.style.transitionDuration = `${seconds}s`;
  bar.style.width = `${Math.max(0, Math.min(100, p))}%`;
}
function stage(k) {
  body.dataset.stage = k || "";
  const at = k === "done" ? STAGES.length : STAGES.indexOf(k);
  for (const li of document.querySelectorAll("#stages [data-k], #process [data-k]")) {
    const i = STAGES.indexOf(li.dataset.k);
    li.dataset.state = k && i < at ? "done" : i === at ? "active" : "";
  }
}
function evLabel(label, count = "") { $("ev-label").textContent = label; $("ev-count").textContent = count; }

/* ---------- tooltip ---------- */
const tip = $("tooltip");
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.classList.add("on");
  const r = tip.getBoundingClientRect();
  tip.style.left = `${Math.max(16, Math.min(window.innerWidth - r.width - 16, x + 16))}px`;
  tip.style.top = `${Math.max(16, y - r.height - 14)}px`;
}
const hideTip = () => tip.classList.remove("on");

function onNodeHover(node, ev) {
  if (!node) return hideTip();
  const t0 = node.chunk.chunk_index * 30;
  const shot = edl?.shots.find((s) => s.drive_set === node.drive && s.chunk_index === node.chunk.chunk_index);
  const head = `${shot ? `Shot ${pad2(shot.index)} · ` : ""}Drive ${driveNo(node.drive)} · ${fmt(t0)}–${fmt(t0 + 30)}`;
  const caption = shot ? shot.caption : node.chunk.caption;
  const streets = streetsIn(caption).concat(node.chunk.streets || []);
  showTip(`<div class="tt-head">${head}</div>${streets.length ? `<div class="tt-streets">Signs read: ${escapeHtml([...new Set(streets)].join(", "))}</div>` : ""}
    <div class="tt-body">${escapeHtml((caption || "Indexed footage").slice(0, 230))}${caption && caption.length > 230 ? "…" : ""}</div>`,
    ev.clientX, ev.clientY);
}

/* ---------- idle ---------- */
function renderChips() {
  $("chips").innerHTML = "";
  for (const [label, q] of EXAMPLES) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = q;
    b.onclick = () => { $("q").value = q; direct(q); };
    $("chips").appendChild(b);
  }
}

async function renderRecent() {
  try {
    const films = await (await fetch("api/films?limit=6")).json();
    $("recent").innerHTML = "";
    for (const f of films) {
      const b = document.createElement("button");
      b.className = "recent-film";
      b.innerHTML = `<img src="${f.poster}" alt=""><span>${escapeHtml(f.title || f.question)}</span>`;
      b.title = f.question || "";
      b.onclick = () => loadFilm(f.id);
      $("recent").appendChild(b);
    }
    if (body.dataset.phase === "idle") evLabel(films.length ? "Recent cuts" : "Evidence", films.length ? `${films.length} films` : "");
  } catch { /* recent cuts are optional */ }
}

function idleResponse() {
  stage("");
  status("Waiting for a question", "Pick a suggestion or type your own");
  progress(0, 0.1);
  $("narr-text").innerHTML = "";
  $("narr-badge").innerHTML = "";
  $("summary").innerHTML = "";
  $("yolo-line").innerHTML = "";
  $("response").classList.remove("error");
}

function goIdle() {
  run++;
  source?.close();
  stopSync();
  $("film").pause();
  edl = null;
  city?.clear();
  setPhase("idle");
  idleResponse();
  evLabel("Recent cuts");
  $("go").disabled = false;
  history.replaceState(null, "", location.pathname);
  renderRecent();
}

/* ---------- evidence grid ---------- */
function resetGrid() {
  const grid = $("grid");
  grid.className = "grid";
  grid.innerHTML = "";
  for (let i = 0; i < GRID_SLOTS; i++) {
    const t = document.createElement("div");
    t.className = "tile";
    t.innerHTML = `<span class="tile-num"></span><div class="tile-meta"></div>`;
    grid.appendChild(t);
  }
  $("queries").innerHTML = "";
  pool = [];
  picksKey = "";
}

/* ---------- YOLO11 (stored detections only) ---------- */
const YOLO_CLASSES = ["person", "car", "bicycle", "motorcycle", "bus", "truck", "train"];
const YOLO_COLORS = ["#ffd27a", "#8fb4ff", "#9fe39a", "#c7a6ff", "#ffad80", "#6fe3d6", "#ff8f80"];
const yoloCounts = (objects) => Object.fromEntries(YOLO_CLASSES.filter((k) => objects?.[k] > 0).map((k) => [k, objects[k]]));

function yoloBadges(counts, max = 4) {
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, max)
    .map(([k, n]) => `<span>${escapeHtml(k)} <b>${n}</b></span>`).join("");
}

async function loadDetections(t, source) {
  try {
    const r = await fetch(`api/detections?source=${encodeURIComponent(source)}`);
    if (!r.ok) return;
    const det = await r.json();
    if (!det.available || !t.isConnected) return;
    t._det = det;
    const counts = yoloCounts(det.counts);
    if (Object.keys(counts).length) {
      const el = document.createElement("div");
      el.className = "yolo";
      el.title = "YOLO11 · most detections of each class in a single frame";
      el.innerHTML = `<em>YOLO11</em>${yoloBadges(counts)}`;
      t.appendChild(el);
    }
    if (t.classList.contains("chosen")) { startBoxes(t); updateYoloLine(); }
  } catch { /* detections are optional */ }
}

function startBoxes(t) {
  const det = t._det;
  const v = t.querySelector("video");
  if (t._boxing || !det?.frames?.length || !v) return;
  t._boxing = true;
  const cv = document.createElement("canvas");
  cv.className = "yolo-boxes";
  t.insertBefore(cv, t.querySelector(".tile-num"));
  const ctx = cv.getContext("2d");
  const draw = () => {
    if (!t.isConnected || !t.classList.contains("chosen") || body.dataset.phase !== "directing") {
      t._boxing = false; cv.remove(); return;
    }
    const w = t.clientWidth, h = t.clientHeight, dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const vw = v.videoWidth || 16, vh = v.videoHeight || 9;
    const s = Math.max(w / vw, h / vh), dw = vw * s, dh = vh * s, ox = (w - dw) / 2, oy = (h - dh) / 2;
    const frame = det.frames[Math.min(det.frames.length - 1, Math.floor((v.currentTime || 0) * det.fps))] || [];
    ctx.lineWidth = 1.5;
    ctx.font = "500 11px 'JetBrains Mono', monospace";
    for (const [ci, x1, y1, x2, y2] of frame) {
      const X = ox + x1 * dw, Y = oy + y1 * dh, BW = (x2 - x1) * dw, BH = (y2 - y1) * dh;
      ctx.strokeStyle = YOLO_COLORS[ci] + "cc";
      ctx.strokeRect(X, Y, BW, BH);
      if (BW > 46 && BH > 22) {
        ctx.fillStyle = YOLO_COLORS[ci] + "cc";
        ctx.fillText(YOLO_CLASSES[ci], X + 3, Y + 12);
      }
    }
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
}

function updateYoloLine(shots) {
  const totals = {};
  let n = 0;
  const lists = shots
    ? shots.map((s) => s.objects || tileFor(s.clip)?._det?.counts)
    : [...$("grid").querySelectorAll(".tile.chosen")].map((t) => t._det?.counts);
  for (const objects of lists) {
    if (!objects) continue;
    n++;
    for (const [k, v] of Object.entries(yoloCounts(objects))) totals[k] = (totals[k] || 0) + v;
  }
  const parts = Object.entries(totals).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span><b>${v}</b> ${escapeHtml(k)}</span>`);
  $("yolo-line").innerHTML = parts.length
    ? `<em title="Summed over the selected shots: the most detections of each class in any single frame of each shot">YOLO11 evidence</em>${parts.join("")}<span class="yolo-note">detections · peak frame per shot · ${n} shot${n === 1 ? "" : "s"}</span>`
    : "";
}

function fillTile(t, c) {
  t.dataset.clip = c.clip;
  t.dataset.key = `${c.drive_set}:${c.chunk_index}`;
  const v = document.createElement("video");
  Object.assign(v, { muted: true, loop: true, playsInline: true, preload: "auto" });
  v.src = `api/clip?source=${encodeURIComponent(c.source)}#t=2.5`;
  t.prepend(v);
  const at = c.chunk_index * 30 + (c.segment_number - 1) * 5;
  t.querySelector(".tile-meta").innerHTML = `<span><b>D${driveNo(c.drive_set)}</b> ${fmt(at)}</span><em>match ${c.score.toFixed(2)}</em>`;
  t.classList.add("filled");
  t.onmouseenter = () => { v.play().catch(() => {}); city?.focus(c.drive_set, c.chunk_index); };
  t.onmouseleave = () => { if (!t.classList.contains("chosen")) v.pause(); city?.focus(null); };
  loadDetections(t, c.source);
}

function populateEvidence(items) {
  pool = items.slice(0, GRID_SLOTS);
  const tiles = [...$("grid").children];
  tiles.slice(pool.length).forEach((t) => t.remove());
  pool.forEach((c, i) => later(160 * i, () => {
    if (c.source) fillTile(tiles[i], c);
    city?.light(c.drive_set, c.chunk_index);
    evLabel("Evidence · retrieved moments", `${i + 1} / ${pool.length}`);
  }));
}

function tileFor(clip) { return $("grid").querySelector(`.tile[data-clip="${clip}"]`); }

function picksFromNarration(sentences) {
  const byClip = Object.fromEntries(pool.map((c) => [c.clip, c]));
  const seen = new Set();
  const picks = [];
  for (const s of sentences) for (const clip of s.clips || []) {
    if (byClip[clip] && !seen.has(clip)) { seen.add(clip); picks.push(byClip[clip]); }
  }
  return picks;
}

function selectShots(picks) {
  const key = picks.map((p) => p.clip).join(",");
  if (!picks.length || key === picksKey) return;
  picksKey = key;
  const grid = $("grid");
  grid.classList.add("has-pick");
  grid.querySelectorAll(".tile.chosen").forEach((t) => t.classList.remove("chosen"));
  picks.forEach((p, i) => {
    let t = tileFor(p.clip);
    if (!t && p.source) {
      t = document.createElement("div");
      t.className = "tile";
      t.innerHTML = `<span class="tile-num"></span><div class="tile-meta"></div>`;
      grid.appendChild(t);
      fillTile(t, { ...p, score: p.score ?? 0 });
    }
    if (!t) return;
    t.classList.add("chosen");
    t.style.order = String(i);
    t.querySelector(".tile-num").textContent = pad2(i + 1);
    later(220 * i, () => t.querySelector("video")?.play().catch(() => {}));
    startBoxes(t);
  });
  updateYoloLine(picks.some((p) => p.objects) ? picks : null);
  grid.querySelectorAll(".tile:not(.chosen)").forEach((t) => { t.style.order = "99"; t.querySelector("video")?.pause(); });
  city?.setCut(picks);
  evLabel("Six shots selected", "01 – 06");
  later(2200, () => grid.classList.add("cut"));
}

/* ---------- directing ---------- */
function direct(q) {
  q = (q || "").trim();
  if (q.length < 3) return;
  run++;
  source?.close();
  stopSync();
  $("film").pause();
  edl = null;
  city?.clear();
  resetGrid();
  idleResponse();
  narrationKey = "";
  retrieved = 0;
  setPhase("directing");
  stage("search");
  evLabel("Evidence · searching", "");
  status("Reading the question", "planning searches");
  history.replaceState(null, "", location.pathname);

  let finished = false;
  let wroteOnce = false;
  const mine = run;
  const es = source = new EventSource(`api/direct?q=${encodeURIComponent(q)}`);
  const on = (name, fn) => es.addEventListener(name, (e) => {
    if (mine !== run || es !== source) return;
    fn(JSON.parse(e.data));
  });

  on("accepted", () => progress(3));
  on("queued", (d) => status("Waiting for the edit bay", d.message));
  on("expanding", () => { stage("search"); status("Reading the question", "planning searches"); progress(7, 2); });
  on("searching", (d) => {
    stage("search");
    city?.scanFlicker(true);
    status(`Searching ${moments.toLocaleString()} Toronto moments`, `${d.queries.length} searches · caption + visual`);
    d.queries.forEach((qq, i) => later(180 * i, () => {
      const s = document.createElement("span");
      s.innerHTML = `<b>${pad2(i + 1)}</b>${escapeHtml(qq)}`;
      s.title = qq;
      $("queries").appendChild(s);
    }));
    progress(12, 0.8);
    later(900, () => progress(34, 22));
  });
  on("candidates", (d) => {
    stage("retrieve");
    city?.scanFlicker(false);
    city?.setCandidates(d.all);
    populateEvidence(d.pool);
    retrieved = d.count;
    const drives = new Set(d.all.map((c) => c.drive_set)).size;
    status(`${d.count} moments retrieved`, `${drives} drives · ${d.pool.length} shortlisted for the cut`);
    $("map-count").textContent = `${d.all.length} matching`;
    $("summary").innerHTML = `<span><b>${d.count}</b> moments retrieved</span><span><b>${drives}</b> drives</span><span><b>${d.pool.length}</b> shortlisted</span>`;
    progress(40);
  });
  on("directing", (d) => {
    later(Math.min(2600, 160 * pool.length), () => {
      if (wroteOnce) return;
      stage("select");
      status("Selecting 6 shots", `from ${d.pool_size} shortlisted moments`);
    });
    progress(46, 0.8);
    later(900, () => progress(60, 16));
  });
  on("narration", (d) => {
    if (!wroteOnce) {
      wroteOnce = true;
      selectShots(picksFromNarration(d.narration));
      stage("write");
    }
    if (d.title) $("narr-label").textContent = `Voice-over · ${d.title}`;
    renderNarration(d.narration, { animate: true });
    const badge = $("narr-badge");
    if (d.verified) {
      badge.className = "narr-badge ok";
      badge.innerHTML = `${CHECK}every line checked against its shots`;
      status("Narration grounded", "every line verified against its shots");
      progress(70);
    } else {
      badge.className = "narr-badge";
      badge.textContent = "checking against the footage";
      status("Writing narration", "checking every line against its shots");
      progress(63, 0.8);
    }
  });
  on("edl", (d) => {
    edl = d;
    selectShots(d.shots);
    renderNarration(d.narration.sentences, { animate: false });
    applyBadge(d);
    renderSummary(d);
    stage("render");
    status("Rendering", `cut locked · "${d.title}"`);
    progress(75);
  });
  on("rendering", (d) => {
    stage("render");
    status(d.step === "done" ? "Final cut ready" : "Rendering", RENDER_STEPS[d.step] || d.step);
    progress(75 + 25 * d.progress, 1.5);
  });
  on("done", (d) => {
    finished = true;
    source.close();
    edl = d;
    stage("done");
    progress(100, 0.6);
    $("go").disabled = false;
    later(1100, () => enterScreening(d));
  });
  on("error", (d) => {
    finished = true;
    source.close();
    fail(d.message);
  });
  on("superseded", () => { finished = true; es.close(); });
  es.onerror = () => {
    if (finished) return;
    es.close();
    if (mine !== run || es !== source) return;
    fail("Lost the connection to the edit bay.");
  };
}

function fail(message) {
  $("response").classList.add("error");
  city?.scanFlicker(false);
  status("The cut fell through", message);
  progress(0, 0.4);
  $("go").disabled = false;
}

/* ---------- narration + summary ---------- */
function shotRefs(clips) {
  const order = edl ? edl.shots.map((s) => s.clip) : picksKey.split(",");
  return clips.map((c) => order.indexOf(c) + 1).filter((n) => n > 0);
}

function renderNarration(sentences, { animate }) {
  const key = sentences.map((s) => s.text).join(" ") + (edl ? edl.id : "");
  if (key === narrationKey) return;
  narrationKey = key;
  let k = 0;
  const html = sentences.map((s, i) => {
    const words = s.text.split(/\s+/);
    const total = s.text.length || 1;
    let chars = 0;
    const spans = words.map((w) => {
      const start = s.start != null ? s.start + ((s.end - s.start) * chars) / total : 0;
      chars += w.length + 1;
      const delay = animate ? `style="animation-delay:${(k++ * 0.045).toFixed(2)}s"` : 'style="animation-duration:0s"';
      return `<span class="w" data-t="${start.toFixed(2)}" ${delay}>${escapeHtml(w)}</span>`;
    }).join(" ");
    const refs = shotRefs(s.clips || []);
    const sup = refs.length ? `<sup title="Shots ${refs.join(", ")}">${refs.map(pad2).join("·")}</sup>` : "";
    return `<span class="s${s.framing ? " framing" : ""}" data-i="${i}" data-start="${s.start ?? 0}" data-end="${s.end ?? 0}">${spans}</span>${sup}`;
  }).join(" ");
  $("narr-text").innerHTML = html;
}

function applyBadge(d) {
  const badge = $("narr-badge");
  if (d.evidence?.weak) {
    badge.className = "narr-badge weak";
    badge.textContent = "weak match · closest moments the archive holds";
  } else if (d.grounding?.verified) {
    badge.className = "narr-badge ok";
    badge.innerHTML = `${CHECK}every line checked against its shots`;
  } else {
    badge.className = "narr-badge";
    badge.textContent = "partly verified";
  }
}

function renderSummary(d) {
  const drives = new Set(d.shots.map((s) => s.drive_set)).size;
  const found = retrieved || d.sources?.candidates?.length || 0;
  const top = Math.max(...d.shots.map((s) => s.similarity || 0));
  $("summary").innerHTML = `<span><b>${d.shots.length}</b> shots</span><span><b>${drives}</b> drives</span>`
    + (found ? `<span><b>${found}</b> moments retrieved</span>` : "")
    + `<span><b>${d.narration.words}</b>-word voice-over</span>`
    + (top > 0 ? `<span>top match <b>${top.toFixed(2)}</b></span>` : "")
    + `<span>${d.evidence?.weak ? "weak evidence" : "strong evidence"}</span>`;
  updateYoloLine(d.shots);
}

/* ---------- screening ---------- */
function enterScreening(d) {
  edl = d;
  $("q").value = d.question;
  setPhase("screening");
  stage("done");
  $("response").classList.remove("error");
  evLabel("Final cut", d.title || "");
  city?.clear();
  city?.setCandidates(d.sources?.candidates || []);
  city?.setCut(d.shots);
  history.replaceState(null, "", `#film=${d.id}`);

  $("grid").querySelectorAll("video").forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });

  const video = $("film");
  video.pause();
  video.innerHTML = "";
  video.removeAttribute("src");
  video.poster = d.media.poster;
  const track = document.createElement("track");
  Object.assign(track, { kind: "captions", src: d.media.vtt, srclang: "en", label: "Voice-over" });
  video.appendChild(track);
  $("screen").classList.remove("started", "playing");
  preloadFilm(d, video);

  const drives = new Set(d.shots.map((s) => s.drive_set)).size;
  $("slate-kicker").textContent = d.evidence?.weak ? "The closest the city's footage comes" : "A thirty-second answer";
  $("film-title").textContent = d.title || "Untitled cut";
  $("slate-question").textContent = `“${d.question}”`;
  $("film-meta").innerHTML = `<span><b>30 s</b></span><span><b>${d.shots.length}</b> shots</span><span><b>${drives}</b> drives</span>`
    + `<span>${d.grounding?.verified ? "voice-over verified" : "voice-over partly verified"}</span>`;

  const track_ = $("scrub-track");
  track_.innerHTML = "";
  d.shots.forEach((s) => {
    const seg = document.createElement("div");
    seg.className = "seg";
    seg.style.flex = `${s.duration} 1 0`;
    track_.appendChild(seg);
  });

  const strip = $("shotstrip");
  strip.innerHTML = "";
  d.shots.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "shot";
    b.innerHTML = `<img src="${d.media.thumbs[i]}" alt=""><span class="shot-label"><b>${pad2(s.index)}</b><span>D${driveNo(s.drive_set)} · ${fmt(s.chunk_index * 30 + s.in)}</span></span>`;
    b.onclick = () => { video.currentTime = s.t_start + 0.02; play(); };
    b.onmouseenter = () => city?.focus(s.drive_set, s.chunk_index);
    b.onmouseleave = () => city?.focus(null);
    strip.appendChild(b);
  });

  $("narr-label").textContent = `Voice-over · ${d.title || ""}`;
  narrationKey = "";
  renderNarration(d.narration.sentences, { animate: false });
  applyBadge(d);
  renderSummary(d);
  progress(100, 0.1);
  cacheSync();
  currentShot = -1;
  sync();
}

let filmUrl = null;
// Pull the whole film into memory first so playback never waits on the network.
async function preloadFilm(d, video) {
  const id = run;
  filmReady = false;
  $("screen").classList.add("loading");
  status("Loading the final cut", "buffering the full film for smooth playback");
  let src = d.media.video;
  try {
    const r = await fetch(d.media.video);
    if (r.ok) {
      const blob = await r.blob();
      if (id !== run) return;
      if (filmUrl) URL.revokeObjectURL(filmUrl);
      filmUrl = URL.createObjectURL(blob);
      src = filmUrl;
    }
  } catch { /* fall back to streaming the file */ }
  if (id !== run) return;
  video.src = src;
  await new Promise((resolve) => {
    if (video.readyState >= 4) return resolve();
    video.addEventListener("canplaythrough", resolve, { once: true });
    setTimeout(resolve, 10000);
  });
  if (id !== run) return;
  filmReady = true;
  $("screen").classList.remove("loading");
  status("Final cut ready", "press play · space bar toggles playback");
}
let filmReady = false;

function play() {
  if (!filmReady) return;
  const video = $("film");
  $("screen").classList.add("started", "playing");
  video.play().catch(() => $("screen").classList.remove("playing"));
  startSync();
}

function startSync() {
  cancelAnimationFrame(raf);
  const loop = () => { sync(); raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
}
function stopSync() { cancelAnimationFrame(raf); city?.setPlaying(null); }

// Element lists and last-applied state, so each frame only touches what changed.
let syncEls = { segs: [], shots: [], sents: [], words: [] };
let syncState = { started: null, sent: -2, lit: -1, sec: -1 };
function cacheSync() {
  syncEls = {
    segs: [...$("scrub-track").children],
    shots: [...$("shotstrip").children],
    sents: [...$("narr-text").querySelectorAll(".s")].map((el) => ({ el, a: Number(el.dataset.start), b: Number(el.dataset.end) })),
    words: [...$("narr-text").querySelectorAll(".w")].map((el) => ({ el, t: Number(el.dataset.t) })),
  };
  syncState = { started: null, sent: -2, lit: -1, sec: -1 };
}

function sync() {
  if (!edl || body.dataset.phase !== "screening") return;
  const video = $("film");
  const t = video.currentTime || 0;
  const dur = edl.duration;
  const idx = edl.shots.findIndex((s) => t >= s.t_start && t < s.t_end);
  const started = $("screen").classList.contains("started");
  const startedChanged = started !== syncState.started;
  syncState.started = started;
  if (idx !== currentShot || startedChanged) {
    currentShot = idx;
    city?.setPlaying(started && idx >= 0 ? idx + 1 : null);
    syncEls.shots.forEach((r, i) => r.classList.toggle("now", i === idx && started));
    syncEls.segs.forEach((seg, i) => {
      seg.classList.toggle("now", started && i === idx);
      seg.classList.toggle("done", started && i < idx);
    });
    if (idx >= 0) {
      const s = edl.shots[idx];
      const streets = streetsIn(s.caption);
      const where = streets.length ? `signs: ${streets.slice(0, 2).join(", ")}` : "";
      $("shot-credit").innerHTML = `<b>Shot ${pad2(s.index)} / ${pad2(edl.shots.length)}</b>`
        + `Drive ${driveNo(s.drive_set)} · ${fmt(s.chunk_index * 30 + s.in)}–${fmt(s.chunk_index * 30 + s.out)}${where ? ` · ${escapeHtml(where)}` : ""}`;
      if (started) status(`Shot ${pad2(s.index)} of ${pad2(edl.shots.length)}`, `Drive ${driveNo(s.drive_set)} · ${fmt(s.chunk_index * 30 + s.in)}`);
    }
  }
  $("scrub-head").style.transform = `translateX(${(t / dur) * $("scrub-track").offsetWidth}px)`;
  const sec = Math.floor(t);
  if (sec !== syncState.sec) { syncState.sec = sec; $("scrub-time").textContent = `${fmt(t)} / ${fmt(dur)}`; }

  const sent = started ? syncEls.sents.findIndex((s) => t >= s.a && t < s.b + 0.3) : -1;
  if (sent !== syncState.sent || startedChanged) {
    syncState.sent = sent;
    syncEls.sents.forEach((s, i) => {
      s.el.classList.toggle("now", i === sent);
      s.el.classList.toggle("done", started && t >= s.b + 0.3);
    });
    if (sent >= 0) syncEls.sents[sent].el.scrollIntoView({ block: "nearest" });
  }
  let lit = -1;
  if (started) while (lit + 1 < syncEls.words.length && t >= syncEls.words[lit + 1].t) lit++;
  if (lit !== syncState.lit) {
    const [lo, hi] = [Math.min(lit, syncState.lit), Math.max(lit, syncState.lit)];
    for (let i = Math.max(0, lo + 1); i <= hi; i++) syncEls.words[i].el.classList.toggle("lit", i <= lit);
    syncState.lit = lit;
  }
}

async function loadFilm(id) {
  try {
    const d = await (await fetch(`api/films/${id}`)).json();
    if (d?.media) { run++; enterScreening(d); }
  } catch { goIdle(); }
}

/* ---------- wiring ---------- */
function wire() {
  $("ask").addEventListener("submit", (e) => { e.preventDefault(); direct($("q").value || EXAMPLES[0][1]); });
  $("play").addEventListener("click", (e) => { e.stopPropagation(); play(); });
  $("screen").addEventListener("click", () => {
    const v = $("film");
    if (v.paused) play(); else v.pause();
  });
  const video = $("film");
  const filmPlaying = (on) => { body.classList.toggle("film-playing", on); city?.setAmbient(!on); };
  video.addEventListener("pause", () => { $("screen").classList.remove("playing"); filmPlaying(false); });
  video.addEventListener("play", () => { $("screen").classList.add("started", "playing"); filmPlaying(true); });
  video.addEventListener("ended", () => {
    $("screen").classList.remove("started", "playing");
    filmPlaying(false);
    stopSync();
    $("slate-kicker").textContent = "Play it again";
    status("Final cut ready", "press play to watch again");
    currentShot = -1;
    sync();
  });
  $("scrubber").addEventListener("click", (e) => {
    if (!edl) return;
    const r = $("scrub-track").getBoundingClientRect();
    const t = Math.max(0, Math.min(edl.duration - 0.05, ((e.clientX - r.left) / r.width) * edl.duration));
    video.currentTime = t;
    if (!$("screen").classList.contains("started")) play(); else sync();
  });
  $("brand").addEventListener("click", goIdle);
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "Escape") goIdle();
    if (e.key === " " && body.dataset.phase === "screening") { e.preventDefault(); video.paused ? play() : video.pause(); }
  });
}

async function init() {
  wire();
  renderChips();
  idleResponse();
  renderRecent();
  try {
    const sources = await (await fetch("api/sources")).json();
    city = buildCity($("city"), sources, { onHover: onNodeHover });
    const chunks = sources.drives.reduce((n, d) => n + d.chunks.length, 0);
    moments = chunks * 6 || moments;
    const minutes = Math.round((chunks * 30) / 60);
    $("map-count").textContent = `${sources.drives.length} drives · ${minutes} min`;
    $("legend-note").textContent = `Six Toronto drives · ${minutes} minutes of footage, laid out by time along each route. Positions are not geographic.`;
  } catch {
    city = buildCity($("city"), { drives: ["set01", "set02", "set03", "set04", "set05", "set06"].map((d) => ({ drive_set: d, chunks: [] })) },
                     { onHover: onNodeHover });
  }
  const m = location.hash.match(/film=([0-9a-f]{12})/);
  if (m) loadFilm(m[1]);
}

init();
