// 评测: the tree of servers, categories, tests and runs; reports (ANSI) as HTML; states and warnings.

export const STATES = {queued: '排队中', preparing: '准备中', running: '运行中', collecting: '收集结果', done: '完成', failed: '失败', stopped: '已停止', interrupted: '中断'};
export const ACTIVE = new Set(['queued', 'preparing', 'running', 'collecting']);
export const PLATFORMS = new Set(['linux/amd64', 'linux/arm64']);
export const isActive = run => ACTIVE.has(run?.state);
export const stateText = run => run.stopping ? '正在停止' : STATES[run.state] || run.state;
export const stateTone = state => ({done: 'ok', failed: 'bad', interrupted: 'bad', stopped: 'idle', queued: 'idle'})[state] || 'busy';

const pad = n => String(n).padStart(2, '0');
// 2026.10.30 23:14, the way a run is named in the tree.
export function runTime(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}.${pad(d.getMonth() + 1)}.${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function took(run, now = Date.now()) {
  if (!run.startedMs) return '';
  const s = Math.max(0, Math.round(((run.endedMs || now) - run.startedMs) / 1000));
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
}

// A server the tests can run on: a Linux node on amd64 or arm64 that runs commands (the page's
// items say so in node.caps, the tool's rows in node.exec).
export function eligible(item) {
  const node = item?.node, info = node?.info || {};
  return Boolean(node && (node.caps?.exec ?? node.exec) !== false && PLATFORMS.has(`${info.platform || 'linux'}/${info.arch || ''}`));
}

// Servers (in the page's order) → categories → tests → runs, newest first. Only what has runs shows.
export function buildTree(servers, runs, catalog) {
  const byServer = new Map();
  for (const run of runs) {
    if (!byServer.has(run.serverId)) byServer.set(run.serverId, []);
    byServer.get(run.serverId).push(run);
  }
  const tests = catalog?.tests || [];
  return servers.filter(s => byServer.has(s.id)).map(server => {
    const own = (byServer.get(server.id) || []).slice().sort((a, b) => b.createdMs - a.createdMs);
    const categories = (catalog?.categories || []).map(category => ({
      ...category,
      tests: tests.filter(t => t.category === category.id).map(test => ({test, runs: own.filter(r => r.test === test.id)})).filter(t => t.runs.length),
    })).filter(c => c.tests.length);
    return {server, eligible: eligible(server), runs: own.length, active: own.filter(isActive).length, categories};
  });
}

// The warnings that apply to a test with these options.
export function warningsFor(test, options) {
  return (test?.warnings || []).filter(w => !w.if || Object.entries(w.if).every(([key, values]) => values.includes(options?.[key])));
}
export function defaultOptions(test) {
  return Object.fromEntries((test?.options || []).map(o => [o.key, o.default]));
}

// ----- ANSI → HTML -----
const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'};
// Characters a terminal gives two cells (East Asian wide and fullwidth, wide emoji).
const WIDE = [[0x1100, 0x115f], [0x2e80, 0x303e], [0x3040, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff], [0xfe30, 0xfe4f], [0xff00, 0xff60],
  [0xffe0, 0xffe6], [0x1f300, 0x1f64f], [0x1f680, 0x1f6ff], [0x1f900, 0x1f9ff], [0x20000, 0x3fffd]];
export const wide = code => WIDE.some(([a, b]) => code >= a && code <= b);
// Reports line columns up the way a terminal does, mixing Latin and CJK text. A browser's fonts
// do not keep that grid, so every non-ASCII character gets a box of one or two cells.
function escapeHtml(text) {
  let out = '';
  for (const ch of text) {
    const code = ch.codePointAt(0);
    if (code < 0x80) out += ESCAPES[ch] || ch;
    else out += `<span class="bn-w${wide(code) ? 2 : 1}">${ch}</span>`;
  }
  return out;
}
function color256(n) {
  if (n < 16) return {index: n};
  if (n < 232) {
    const v = n - 16, step = x => (x ? 55 + x * 40 : 0);
    return {rgb: [step(Math.floor(v / 36)), step(Math.floor(v / 6) % 6), step(v % 6)]};
  }
  const g = 8 + (n - 232) * 10;
  return {rgb: [g, g, g]};
}
function extended(codes, i) {
  if (codes[i + 1] === 5 && codes.length > i + 2) return [color256(codes[i + 2]), i + 2];
  if (codes[i + 1] === 2 && codes.length > i + 4) return [{rgb: codes.slice(i + 2, i + 5).map(v => Math.max(0, Math.min(255, v)))}, i + 4];
  return [null, i];
}
const blank = () => ({fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false});
function apply(state, params) {
  const codes = params === '' ? [0] : params.split(';').map(p => (p === '' ? 0 : Number(p)));
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i];
    if (c === 0) Object.assign(state, blank());
    else if (c === 1) state.bold = true;
    else if (c === 2) state.dim = true;
    else if (c === 3) state.italic = true;
    else if (c === 4) state.underline = true;
    else if (c === 7) state.inverse = true;
    else if (c === 22) state.bold = state.dim = false;
    else if (c === 23) state.italic = false;
    else if (c === 24) state.underline = false;
    else if (c === 27) state.inverse = false;
    else if (c >= 30 && c <= 37) state.fg = {index: c - 30};
    else if (c >= 90 && c <= 97) state.fg = {index: c - 90 + 8};
    else if (c === 39) state.fg = null;
    else if (c >= 40 && c <= 47) state.bg = {index: c - 40};
    else if (c >= 100 && c <= 107) state.bg = {index: c - 100 + 8};
    else if (c === 49) state.bg = null;
    else if (c === 38 || c === 48) {
      const [value, next] = extended(codes, i);
      if (value) state[c === 38 ? 'fg' : 'bg'] = value;
      i = next;
    }
  }
}
function spanFor(state) {
  let fg = state.fg, bg = state.bg;
  if (state.inverse) [fg, bg] = [bg || {index: 'bg'}, fg || {index: 'fg'}];
  const classes = [], styles = [];
  // Bold switches the eight basic colours to their bright versions, as terminals do.
  if (fg && typeof fg.index === 'number' && state.bold && fg.index < 8) fg = {index: fg.index + 8};
  for (const [kind, value] of [['fg', fg], ['bg', bg]]) {
    if (!value) continue;
    if (value.rgb) styles.push(`${kind === 'fg' ? 'color' : 'background-color'}:rgb(${value.rgb.join(',')})`);
    else classes.push(`bn-${kind}-${value.index}`);
  }
  if (state.bold) classes.push('bn-b');
  if (state.dim) classes.push('bn-dim');
  if (state.italic) classes.push('bn-i');
  if (state.underline) classes.push('bn-u');
  if (!classes.length && !styles.length) return '';
  return `<span${classes.length ? ` class="${classes.join(' ')}"` : ''}${styles.length ? ` style="${styles.join(';')}"` : ''}>`;
}
// Colours and styles become spans; every other escape sequence is dropped.
export function ansiHtml(text) {
  const source = String(text || '').replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '').replace(/\r(?!\n)/g, '');
  const state = blank();
  let out = '', open = '';
  const re = /\x1b\[([0-9;?]*)([A-Za-z])/g;
  let last = 0, m;
  const emit = chunk => {
    if (!chunk) return;
    const tag = spanFor(state);
    if (tag !== open) {
      if (open) out += '</span>';
      out += tag;
      open = tag;
    }
    out += escapeHtml(chunk);
  };
  while ((m = re.exec(source))) {
    emit(source.slice(last, m.index).replace(/\x1b/g, ''));
    if (m[2] === 'm' && !m[1].startsWith('?')) apply(state, m[1]);
    last = re.lastIndex;
  }
  emit(source.slice(last).replace(/\x1b/g, ''));
  if (open) out += '</span>';
  return out;
}
export const plainText = text => String(text || '').replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\r(?!\n)/g, '');
