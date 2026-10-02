// Stylised Toronto: drives laid out as routes on the city's tilted grid. Not geographic.
const NS = "http://www.w3.org/2000/svg";
const W = 1600, H = 900, CX = 800, CY = 450;
// Routes are walked inside this window; the viewBox is then fitted to the routes and the panel.
const VIEW = { x: 380, y: -120, w: 840, h: 1020 };
const TILT = -16.7;            // Toronto's street grid runs ~16.7° west of true north
const CELL = 34;
const ROUTE_TINTS = ["#8fb4ff", "#6fe3d6", "#c7a6ff", "#ffad80", "#ffd76e", "#9fe39a"];
const ANCHORS = [[520, 60], [600, 500], [880, 0], [1060, 440], [1110, 180], [800, 280]];

const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};

function mulberry32(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rad = (TILT * Math.PI) / 180;
const toScreen = (u, v) => [CX + u * Math.cos(rad) - v * Math.sin(rad), CY + u * Math.sin(rad) + v * Math.cos(rad)];
const toGrid = (x, y) => {
  const dx = x - CX, dy = y - CY;
  return [dx * Math.cos(rad) + dy * Math.sin(rad), -dx * Math.sin(rad) + dy * Math.cos(rad)];
};

// Shoreline y for a given x (gentle WSW-ENE lakeshore with a downtown bay).
const shoreY = (x) => 712 + 26 * Math.sin((x + 140) / 260) - 0.045 * (x - 800) - 18 * Math.exp(-(((x - 760) / 170) ** 2));

// Routes stay inside the visible window with a margin, so slice-cropping never hides a node.
function inCity([x, y]) {
  return x > VIEW.x + 70 && x < VIEW.x + VIEW.w - 70 && y > VIEW.y + 80 && y < shoreY(x) - 40;
}

function walkRoute(index) {
  const rnd = mulberry32(9157 + index * 7919);
  const [gx, gy] = toGrid(...ANCHORS[index]);
  let u = Math.round(gx / CELL), v = Math.round(gy / CELL);
  const dirs = [[1, 0], [0, 1], [-1, 0], [0, -1]];
  let d = Math.floor(rnd() * 4);
  const cells = [[u, v]];
  const seen = new Set([`${u},${v}`]);
  let guard = 0;
  while (cells.length < 26 && guard++ < 400) {
    if (rnd() < 0.28) d = (d + (rnd() < 0.5 ? 1 : 3)) % 4;
    let moved = false;
    for (const turn of [0, 1, 3, 2]) {
      const nd = (d + turn) % 4;
      const nu = u + dirs[nd][0], nv = v + dirs[nd][1];
      const key = `${nu},${nv}`;
      if (!inCity(toScreen(nu * CELL, nv * CELL)) || seen.has(key)) continue;
      u = nu; v = nv; d = nd; moved = true;
      cells.push([u, v]); seen.add(key);
      break;
    }
    if (!moved) break;
  }
  return cells.map(([a, b]) => toScreen(a * CELL, b * CELL));
}

function roundedPath(pts, r = 11) {
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [px, py] = pts[i - 1], [x, y] = pts[i], [nx, ny] = pts[i + 1];
    const l1 = Math.hypot(x - px, y - py), l2 = Math.hypot(nx - x, ny - y);
    const a = [x - ((x - px) / l1) * Math.min(r, l1 / 2), y - ((y - py) / l1) * Math.min(r, l1 / 2)];
    const b = [x + ((nx - x) / l2) * Math.min(r, l2 / 2), y + ((ny - y) / l2) * Math.min(r, l2 / 2)];
    d += ` L${a[0].toFixed(1)},${a[1].toFixed(1)} Q${x.toFixed(1)},${y.toFixed(1)} ${b[0].toFixed(1)},${b[1].toFixed(1)}`;
  }
  const last = pts[pts.length - 1];
  return d + ` L${last[0].toFixed(1)},${last[1].toFixed(1)}`;
}

function spaced(pts, n) {
  const seg = [];
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const l = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    seg.push(l); total += l;
  }
  const out = [];
  for (let k = 0; k < n; k++) {
    let t = (total * k) / (n - 1), i = 0;
    while (i < seg.length - 1 && t > seg[i]) { t -= seg[i]; i++; }
    const f = Math.min(1, t / seg[i]);
    out.push([pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f]);
  }
  return out;
}

function drawBase(svg) {
  const defs = el("defs", {}, svg);
  defs.innerHTML = `
    <filter id="glow" x="-200%" y="-200%" width="500%" height="500%">
      <feGaussianBlur stdDeviation="3.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="bloom" x="-300%" y="-300%" width="700%" height="700%">
      <feGaussianBlur stdDeviation="9" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <linearGradient id="lake" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#0b1a2a" stop-opacity=".95"/><stop offset="1" stop-color="#03070d"/>
    </linearGradient>
    <radialGradient id="cityglow" cx=".5" cy=".55" r=".6">
      <stop offset="0" stop-color="#16263a" stop-opacity=".55"/><stop offset="1" stop-color="#04060b" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="landclip"><path id="landpath"/></clipPath>`;

  el("rect", { x: -400, y: -600, width: W + 800, height: H + 1200, fill: "url(#cityglow)" }, svg);

  const L = -700, R = W + 700, TOP = -1200, BOTTOM = H + 1200;
  let shore = `M${L},${shoreY(L).toFixed(1)}`;
  for (let x = L + 20; x <= R; x += 20) shore += ` L${x},${shoreY(x).toFixed(1)}`;
  defs.querySelector("#landpath").setAttribute("d", `${shore} L${R},${TOP} L${L},${TOP} Z`);

  const grid = el("g", { class: "grid", "clip-path": "url(#landclip)" }, svg);
  const g = el("g", { transform: `rotate(${TILT} ${CX} ${CY})` }, grid);
  for (let i = -40; i <= 40; i++) {
    const major = i % 5 === 0;
    el("line", { x1: CX + i * CELL, y1: -1800, x2: CX + i * CELL, y2: 2200, class: major ? "gl major" : "gl" }, g);
    el("line", { x1: -1400, y1: CY + i * CELL, x2: 3000, y2: CY + i * CELL, class: major ? "gl major" : "gl" }, g);
  }
  // A few long arterials, heavier than the grid.
  for (const i of [-12, -3, 0, 7, 14]) el("line", { x1: CX + i * CELL, y1: -1800, x2: CX + i * CELL, y2: 2200, class: "arterial" }, g);
  for (const i of [-10, -7, -1, 5]) el("line", { x1: -1400, y1: CY + i * CELL, x2: 3000, y2: CY + i * CELL, class: "arterial" }, g);

  el("path", { d: "M262,-400 C220,-200 300,-100 262,-20 C300,120 210,230 268,360 S330,560 282,705", class: "river" }, svg);
  el("path", { d: "M1068,-400 C1120,-260 1020,-140 1068,-20 C1010,110 1110,190 1052,320 S1130,470 1046,585 S1012,650 1030,690", class: "river" }, svg);

  el("path", { d: `${shore} L${R},${BOTTOM} L${L},${BOTTOM} Z`, fill: "url(#lake)" }, svg);
  el("path", { d: shore, class: "shore" }, svg);
  const lake = el("g", { class: "lake-lines" }, svg);
  for (let k = 0; k < 12; k++) {
    let d = "";
    for (let x = L; x <= R; x += 40) d += `${d ? " L" : "M"}${x},${(shoreY(x) + 26 + k * 24).toFixed(1)}`;
    el("path", { d, style: `animation-delay:${-k * 1.7}s` }, lake);
  }
  el("path", { d: "M640,776 q40,-18 92,-10 q36,6 70,-4 q24,-7 46,4 q-30,16 -70,14 q-50,-2 -92,10 q-30,6 -46,-14 z", class: "island" }, svg);
  el("path", { d: "M872,770 q22,-8 44,-2 q-16,10 -44,2 z", class: "island" }, svg);

  const label = el("text", { x: CX, y: 752, class: "lake-label", "text-anchor": "middle" }, svg);
  label.textContent = "LAKE ONTARIO";

  const compass = el("g", { class: "compass", transform: `translate(${VIEW.x + VIEW.w - 44} ${VIEW.y + 52}) rotate(${TILT})` }, svg);
  el("line", { x1: 0, y1: 18, x2: 0, y2: -22 }, compass);
  el("path", { d: "M0,-30 L5,-18 L-5,-18 Z" }, compass);
  const cn = el("text", { x: 0, y: 36, "text-anchor": "middle" }, compass);
  cn.textContent = "GRID N";
}

export function buildCity(svg, sources, { onHover } = {}) {
  let box = null;
  // Scale the routes as large as the panel allows (aspect preserved), clear of the panel label and legend.
  const fit = () => {
    const { width, height } = svg.getBoundingClientRect();
    if (!width || !height) return;
    if (!box) {
      const xs = [...nodes.values()].map((n) => n.x), ys = [...nodes.values()].map((n) => n.y);
      if (!xs.length) return;
      const x0 = Math.min(...xs) - 30, y0 = Math.min(...ys) - 40;
      box = { x: x0, y: y0, w: Math.max(...xs) + 30 - x0, h: Math.max(...ys) + 34 - y0 };
    }
    const panel = svg.closest(".panel");
    const top = (panel?.querySelector(".panel-label")?.offsetHeight || 0) + 6;
    const bottom = Math.max(0, (panel?.querySelector(".legend")?.offsetHeight || 0) - 24);
    const side = 12;
    const availW = Math.max(80, width - 2 * side), availH = Math.max(80, height - top - bottom);
    const s = Math.min(availW / box.w, availH / box.h);
    const x = box.x - (side + (availW - box.w * s) / 2) / s;
    const y = box.y - (top + (availH - box.h * s) / 2) / s;
    svg.setAttribute("viewBox", `${x.toFixed(1)} ${y.toFixed(1)} ${(width / s).toFixed(1)} ${(height / s).toFixed(1)}`);
    svg.querySelector(".compass")?.setAttribute("transform",
      `translate(${(x + (width - 34) / s).toFixed(1)} ${(y + (top + 44) / s).toFixed(1)}) rotate(${TILT})`);
  };
  svg.setAttribute("viewBox", `${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.innerHTML = "";
  drawBase(svg);

  const routesG = el("g", { class: "routes" }, svg);
  const nodesG = el("g", { class: "nodes" }, svg);
  const threadG = el("g", { class: "thread" }, svg);
  const marksG = el("g", { class: "marks" }, svg);
  const nodes = new Map();

  sources.drives.forEach((drive, i) => {
    const tint = ROUTE_TINTS[i % ROUTE_TINTS.length];
    const pts = walkRoute(i);
    const d = roundedPath(pts);
    const route = el("g", { class: "route", style: `--tint:${tint}` }, routesG);
    el("path", { d, class: "route-line" }, route);
    const pathEl = el("path", { d, class: "route-flow", id: `route-${drive.drive_set}` }, route);
    const pulse = el("circle", { r: 3, class: "traffic" }, route);
    const motion = el("animateMotion", { dur: `${22 + i * 3.5}s`, repeatCount: "indefinite", rotate: "auto" }, pulse);
    el("mpath", { href: `#route-${drive.drive_set}` }, motion);

    const [sx, sy] = pts[0];
    const tag = el("g", { class: "drive-tag", transform: `translate(${sx.toFixed(1)} ${sy.toFixed(1)})`, style: `--tint:${tint}` }, routesG);
    el("circle", { r: 5.5 }, tag);
    const nearRight = sx > VIEW.x + VIEW.w - 170;
    const t = el("text", { x: nearRight ? -11 : 11, y: -11, "text-anchor": nearRight ? "end" : "start" }, tag);
    t.textContent = `DRIVE ${drive.drive_set.replace("set", "")}`;

    const positions = spaced(pts, 30);
    const byIndex = new Map(drive.chunks.map((c) => [c.chunk_index, c]));
    positions.forEach(([x, y], k) => {
      const chunk = byIndex.get(k) || { chunk_index: k, caption: "", streets: [] };
      const key = `${drive.drive_set}:${k}`;
      const n = el("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 3, class: "node", style: `--tint:${tint}` }, nodesG);
      n.dataset.key = key;
      nodes.set(key, { el: n, x, y, drive: drive.drive_set, chunk, tint });
      n.addEventListener("mouseenter", (ev) => onHover?.(nodes.get(key), ev));
      n.addEventListener("mouseleave", () => onHover?.(null));
    });
  });
  new ResizeObserver(fit).observe(svg);
  const legend = svg.closest(".panel")?.querySelector(".legend");
  if (legend) new ResizeObserver(fit).observe(legend);

  const api = {
    nodes,
    node: (drive, chunk) => nodes.get(`${drive}:${chunk}`),
    clear() {
      for (const n of nodes.values()) {
        n.el.classList.remove("cand", "pool", "chosen", "dim");
        n.el.style.removeProperty("--s");
      }
      threadG.innerHTML = "";
      marksG.innerHTML = "";
      svg.classList.remove("has-cut", "is-playing");
    },
    scanFlicker(on) {
      svg.classList.toggle("scanning", on);
    },
    setCandidates(list) {
      const max = Math.max(...list.map((c) => c.score), 0.01);
      const min = Math.min(...list.map((c) => c.score), 0);
      list.forEach((c, i) => {
        const n = api.node(c.drive_set, c.chunk_index);
        if (!n) return;
        const s = (c.score - min) / Math.max(0.0001, max - min);
        setTimeout(() => {
          n.el.classList.add("cand");
          n.el.style.setProperty("--s", (0.35 + 0.65 * s).toFixed(2));
        }, 18 * i);
      });
    },
    setPool(pool) {
      pool.forEach((c, i) => {
        const n = api.node(c.drive_set, c.chunk_index);
        if (n) setTimeout(() => n.el.classList.add("pool"), 400 + 60 * i);
      });
    },
    setCut(shots) {
      threadG.innerHTML = "";
      marksG.innerHTML = "";
      svg.classList.add("has-cut");
      const pts = shots.map((s) => api.node(s.drive_set, s.chunk_index)).filter(Boolean);
      for (const n of nodes.values()) { n.el.classList.remove("chosen"); n.el.classList.add("dim"); }
      let d = "";
      pts.forEach((p, i) => {
        p.el.classList.remove("dim");
        p.el.classList.add("chosen");
        if (i === 0) { d = `M${p.x.toFixed(1)},${p.y.toFixed(1)}`; return; }
        const q = pts[i - 1];
        const mx = (q.x + p.x) / 2, my = (q.y + p.y) / 2;
        const len = Math.hypot(p.x - q.x, p.y - q.y);
        const nx = -(p.y - q.y) / (len || 1), ny = (p.x - q.x) / (len || 1);
        const bend = Math.min(140, len * 0.32) * (i % 2 ? 1 : -1);
        d += ` Q${(mx + nx * bend).toFixed(1)},${(my + ny * bend).toFixed(1)} ${p.x.toFixed(1)},${p.y.toFixed(1)}`;
      });
      if (pts.length > 1) {
        el("path", { d, class: "thread-glow" }, threadG);
        const line = el("path", { d, class: "thread-line" }, threadG);
        const L = line.getTotalLength();
        line.style.strokeDasharray = `${L}`;
        line.style.strokeDashoffset = `${L}`;
        requestAnimationFrame(() => { line.style.strokeDashoffset = "0"; });
      }
      const placed = [];
      const OFFSETS = [[0, -22], [0, 36], [30, 7], [-30, 7], [24, -18], [-24, -18], [24, 30], [-24, 30]];
      const labelAt = (p) => {
        const others = [...placed, ...pts.filter((q) => q !== p).map((q) => [q.x, q.y])];
        const clear = ([dx, dy]) => others.every(([x, y]) => Math.hypot(p.x + dx - x, p.y + dy - 6 - y) > 24);
        return OFFSETS.find(clear) || OFFSETS[0];
      };
      pts.forEach((p, i) => {
        const [lx, ly] = labelAt(p);
        placed.push([p.x + lx, p.y + ly - 6]);
        const m = el("g", { class: "mark", transform: `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`,
                            style: `animation-delay:${0.25 + i * 0.28}s` }, marksG);
        m.dataset.index = String(i + 1);
        m.dataset.key = `${p.drive}:${p.chunk.chunk_index}`;
        el("circle", { r: 20, class: "mark-halo" }, m);
        el("circle", { r: 13, class: "mark-ring" }, m);
        el("circle", { r: 4.6, class: "mark-core" }, m);
        const label = el("text", { x: lx, y: ly, "text-anchor": "middle" }, m);
        label.textContent = String(i + 1).padStart(2, "0");
      });
    },
    light(drive, chunk) {
      api.node(drive, chunk)?.el.classList.add("pool");
    },
    focus(drive, chunk) {
      for (const n of nodes.values()) n.el.classList.remove("focus");
      marksG.querySelectorAll(".mark").forEach((m) => m.classList.remove("focus"));
      if (drive == null) return;
      const n = api.node(drive, chunk);
      n?.el.classList.add("focus");
      const mark = [...marksG.querySelectorAll(".mark")].find((m) => m.dataset.key === `${drive}:${chunk}`);
      mark?.classList.add("focus");
    },
    setAmbient(on) {
      if (on) svg.unpauseAnimations(); else svg.pauseAnimations();
    },
    setPlaying(index) {
      svg.classList.toggle("is-playing", index != null);
      let active = null;
      marksG.querySelectorAll(".mark").forEach((m) => {
        const on = Number(m.dataset.index) === index;
        m.classList.toggle("now", on);
        if (on) active = m;
      });
      // The pulse is an HTML overlay so it animates on the compositor without repainting the SVG.
      const frame = svg.parentElement;
      let pulse = frame.querySelector(".map-pulse");
      if (!pulse) { pulse = document.createElement("div"); pulse.className = "map-pulse"; frame.appendChild(pulse); }
      pulse.classList.toggle("on", !!active);
      if (active) {
        const r = active.querySelector(".mark-core").getBoundingClientRect(), f = frame.getBoundingClientRect();
        pulse.style.left = `${r.left + r.width / 2 - f.left}px`;
        pulse.style.top = `${r.top + r.height / 2 - f.top}px`;
      }
    },
  };
  return api;
}
