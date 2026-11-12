// Lines drawn at right angles (desktop, by choice): each one the cheapest way on a grid from its
// source's border to its target's, round every box in the way. A turn costs as much as a few steps, so
// lines keep straight; a line may run along one that shares an end with it (a trunk, as drawn by hand)
// but keeps out of the lane of one that does not. No requests here.

const DIRS = [[1, 0], [0, 1], [-1, 0], [0, -1]]; // right, down, left, up
const GAP = 3; // between a line's end and its box, as for the curved lines

// Where a line meets a box's border, going out of it in direction d from grid point p.
function border(box, p, d) {
  if (d === 0) return {x: box.x + box.w + GAP, y: p.y};
  if (d === 2) return {x: box.x - GAP, y: p.y};
  if (d === 1) return {x: p.x, y: box.y + box.h + GAP};
  return {x: p.x, y: box.y - GAP};
}

// Corners only: points on one straight run in between are left out.
function corners(points) {
  const out = [];
  for (const p of points) {
    const a = out.at(-2), b = out.at(-1);
    if (b && Math.abs(b.x - p.x) < 0.01 && Math.abs(b.y - p.y) < 0.01) continue;
    if (a && b && ((Math.abs(a.x - b.x) < 0.01 && Math.abs(b.x - p.x) < 0.01) || (Math.abs(a.y - b.y) < 0.01 && Math.abs(b.y - p.y) < 0.01))) out[out.length - 1] = p;
    else out.push(p);
  }
  return out;
}

// The point a share k (0..1) of the way along the line.
export function pointAt(points, k) {
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
  let left = lengths.reduce((s, x) => s + x, 0) * Math.min(1, Math.max(0, k));
  for (let i = 0; i < lengths.length; i++) {
    if (left <= lengths[i] || i === lengths.length - 1) {
      const t = lengths[i] ? Math.min(1, left / lengths[i]) : 0, a = points[i], b = points[i + 1];
      return {x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t};
    }
    left -= lengths[i];
  }
  return points[0] || {x: 0, y: 0};
}
// The middle of the longest run, as a share of the way: where a label reads best.
function longestMiddle(points) {
  const lengths = points.slice(1).map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y)), total = lengths.reduce((s, x) => s + x, 0);
  if (!total) return 0.5;
  const i = lengths.indexOf(Math.max(...lengths));
  return (lengths.slice(0, i).reduce((s, x) => s + x, 0) + lengths[i] / 2) / total;
}

// A right-angled line between two boxes with nothing in the way: out of the side facing the other box,
// across halfway, into the other (while a box is dragged, before the way round is worked out).
export function elbow(a, b) {
  const dx = b.cx - a.cx, dy = b.cy - a.cy;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const s = {x: a.cx + Math.sign(dx || 1) * (a.w / 2 + GAP), y: a.cy}, t = {x: b.cx - Math.sign(dx || 1) * (b.w / 2 + GAP), y: b.cy}, mx = (s.x + t.x) / 2;
    return corners([s, {x: mx, y: s.y}, {x: mx, y: t.y}, t]);
  }
  const s = {x: a.cx, y: a.cy + Math.sign(dy || 1) * (a.h / 2 + GAP)}, t = {x: b.cx, y: b.cy - Math.sign(dy || 1) * (b.h / 2 + GAP)}, my = (s.y + t.y) / 2;
  return corners([s, {x: s.x, y: my}, {x: t.x, y: my}, t]);
}

// A path through the points with its corners rounded.
export function roundedPath(points, r = 6) {
  if (points.length < 2) return '';
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const p = points[i - 1], c = points[i], n = points[i + 1];
    const a = Math.min(r, Math.hypot(c.x - p.x, c.y - p.y) / 2, Math.hypot(n.x - c.x, n.y - c.y) / 2);
    const u = (q, len) => { const l = Math.hypot(q.x - c.x, q.y - c.y) || 1; return {x: c.x + (q.x - c.x) / l * len, y: c.y + (q.y - c.y) / l * len}; };
    const s = u(p, a), e = u(n, a);
    d += ` L ${s.x} ${s.y} Q ${c.x} ${c.y} ${e.x} ${e.y}`;
  }
  const last = points.at(-1);
  return `${d} L ${last.x} ${last.y}`;
}

// A binary heap of state indices by cost.
function heap() {
  const keys = [], vals = [];
  return {
    size: () => keys.length,
    push(v, k) {
      let i = keys.length; keys.push(k); vals.push(v);
      while (i > 0) { const p = (i - 1) >> 1; if (keys[p] <= k) break; keys[i] = keys[p]; vals[i] = vals[p]; i = p; }
      keys[i] = k; vals[i] = v;
    },
    pop() {
      const top = vals[0], k = keys.pop(), v = vals.pop();
      if (keys.length) {
        let i = 0;
        for (;;) {
          const l = 2 * i + 1, r = l + 1;
          let m = i, mk = k;
          if (l < keys.length && keys[l] < mk) { m = l; mk = keys[l]; }
          if (r < keys.length && keys[r] < mk) { m = r; mk = keys[r]; }
          if (m === i) break;
          keys[i] = keys[m]; vals[i] = vals[m]; i = m;
        }
        keys[i] = k; vals[i] = v;
      }
      return top;
    },
  };
}

// lines [{id, source, target}], boxes Map(id -> {x, y, w, h}) of everything on the board. Returns
// Map(id -> {points, labelAt, from, to}): the corners from the source's border to the target's, the
// share of the way where its label goes, and the two boxes it was worked out for (a line whose ends
// have moved since is out of date). A line with no way (or an end not on the board) is left out.
export function routeOrtho(lines, boxes, {step = 12, pad = 10, turn = 3, margin = 160} = {}) {
  const out = new Map(), all = [...boxes.values()];
  if (!all.length) return out;
  const ox = Math.min(...all.map(b => b.x)) - margin, oy = Math.min(...all.map(b => b.y)) - margin;
  const cols = Math.ceil((Math.max(...all.map(b => b.x + b.w)) + margin - ox) / step) + 1, rows = Math.ceil((Math.max(...all.map(b => b.y + b.h)) + margin - oy) / step) + 1;
  const n = cols * rows, X = c => ox + c * step, Y = r => oy + r * step;
  // Grid points inside a box (with room around it) are not for lines.
  const blocked = new Uint8Array(n);
  const span = b => ({c0: Math.ceil((b.x - pad - ox) / step), c1: Math.floor((b.x + b.w + pad - ox) / step), r0: Math.ceil((b.y - pad - oy) / step), r1: Math.floor((b.y + b.h + pad - oy) / step)});
  for (const b of all) {
    const s = span(b);
    for (let r = Math.max(0, s.r0); r <= Math.min(rows - 1, s.r1); r++) for (let c = Math.max(0, s.c0); c <= Math.min(cols - 1, s.c1); c++) blocked[r * cols + c] = 1;
  }
  // The free grid points just outside a box, on each side, within its width or height: where lines leave
  // and arrive, d the way out of the box, off how far from the middle of the side.
  function ports(b) {
    const list = [], mid = {x: b.x + b.w / 2, y: b.y + b.h / 2}, s = span(b);
    const add = (c, r, d, off) => { if (c >= 0 && r >= 0 && c < cols && r < rows && !blocked[r * cols + c]) list.push({cell: r * cols + c, d, off}); };
    for (let c = Math.ceil((b.x + 4 - ox) / step); c <= Math.floor((b.x + b.w - 4 - ox) / step); c++) { add(c, s.r0 - 1, 3, Math.abs(X(c) - mid.x)); add(c, s.r1 + 1, 1, Math.abs(X(c) - mid.x)); }
    for (let r = Math.ceil((b.y + 4 - oy) / step); r <= Math.floor((b.y + b.h - 4 - oy) / step); r++) { add(s.c0 - 1, r, 2, Math.abs(Y(r) - mid.y)); add(s.c1 + 1, r, 0, Math.abs(Y(r) - mid.y)); }
    return list;
  }
  // Lanes taken: per grid point and way (0 across, 1 up and down), the ends of the lines on it.
  const lanes = new Map();
  const g = new Float64Array(n * 4), prev = new Int32Array(n * 4), seen = new Uint8Array(n * 4);
  const order = lines.filter(l => boxes.has(l.source) && boxes.has(l.target) && l.source !== l.target).map(l => {
    const a = boxes.get(l.source), b = boxes.get(l.target);
    return {...l, far: Math.abs(a.x + a.w / 2 - b.x - b.w / 2) + Math.abs(a.y + a.h / 2 - b.y - b.h / 2)};
  }).sort((p, q) => p.far - q.far || (p.id < q.id ? -1 : 1));
  for (const line of order) {
    const from = boxes.get(line.source), to = boxes.get(line.target), starts = ports(from), ends = ports(to);
    if (!starts.length || !ends.length) continue;
    const endAt = new Map(ends.map(e => [e.cell, e]));
    const tx0 = to.x - pad, tx1 = to.x + to.w + pad, ty0 = to.y - pad, ty1 = to.y + to.h + pad;
    const h = cell => { const x = X(cell % cols), y = Y(Math.floor(cell / cols)); return 0.6 * (Math.max(0, tx0 - x, x - tx1) + Math.max(0, ty0 - y, y - ty1)); };
    g.fill(Infinity); seen.fill(0);
    const q = heap(), FINAL = n * 4;
    let finalCost = Infinity, finalFrom = -1;
    for (const s of starts) {
      const st = s.cell * 4 + s.d, cost = s.off * 0.2;
      if (cost < g[st]) { g[st] = cost; prev[st] = -1; q.push(st, cost + h(s.cell)); }
    }
    const step1 = (cell, way) => {
      const used = lanes.get(cell * 2 + way);
      if (!used) return 1;
      let m = 1;
      for (const ends2 of used) m = ends2.includes(line.source) || ends2.includes(line.target) ? Math.min(m, 0.6) : m + 2;
      return m;
    };
    while (q.size()) {
      const st = q.pop();
      if (st === FINAL) break;
      if (seen[st]) continue;
      seen[st] = 1;
      const cell = st >> 2, d = st & 3, here = g[st];
      if (here >= finalCost) break;
      const end = endAt.get(cell);
      if (end) {
        // Into the box: straight on when already heading in, else one more turn.
        const inward = (end.d + 2) % 4, cost = here + (d === inward ? 0 : turn * step) + end.off * 0.2;
        if (cost < finalCost) { finalCost = cost; finalFrom = st; q.push(FINAL, cost); }
      }
      const c = cell % cols, r = (cell - c) / cols;
      for (let nd = 0; nd < 4; nd++) {
        if (nd === (d + 2) % 4) continue;
        const nc = c + DIRS[nd][0], nr = r + DIRS[nd][1];
        if (nc < 0 || nr < 0 || nc >= cols || nr >= rows) continue;
        const next = nr * cols + nc;
        if (blocked[next]) continue;
        const ns = next * 4 + nd, cost = here + step * step1(next, nd % 2) + (nd === d ? 0 : turn * step);
        if (cost < g[ns]) { g[ns] = cost; prev[ns] = st; q.push(ns, cost + h(next)); }
      }
    }
    if (finalFrom < 0) continue;
    const cells = [];
    for (let st = finalFrom; st >= 0; st = prev[st]) cells.push(st);
    cells.reverse();
    const first = cells[0], last = cells.at(-1), startPort = starts.find(s => s.cell === first >> 2), endPort = endAt.get(last >> 2);
    const pts = cells.map(st => ({x: X((st >> 2) % cols), y: Y(Math.floor((st >> 2) / cols))}));
    for (let i = 1; i < cells.length; i++) {
      const way = (cells[i] & 3) % 2, key = (cells[i] >> 2) * 2 + way, list = lanes.get(key) || [];
      list.push([line.source, line.target]); lanes.set(key, list);
    }
    const points = corners([border(from, pts[0], startPort.d), ...pts, border(to, pts.at(-1), endPort.d)]);
    out.set(line.id, {points, labelAt: longestMiddle(points), from: {...from}, to: {...to}});
  }
  return out;
}
