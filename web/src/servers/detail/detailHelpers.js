// Colours, ranges and names the detail page's sections share. Colours are theme roles, so both themes hold.
export const COLORS = {
  cpu: 'var(--ob-blue)', mem: 'var(--ob-violet)', swap: 'var(--ob-orange)', steal: 'var(--ob-danger)', iowait: 'var(--ob-warning)', load: 'var(--ob-info)',
  rx: 'var(--ob-success)', tx: 'var(--ob-blue)', read: 'var(--ob-violet)', write: 'var(--ob-orange)', tcp: 'var(--ob-blue)', udp: 'var(--ob-violet)',
};
export const PALETTE = ['var(--ob-blue)', 'var(--ob-violet)', 'var(--ob-orange)', 'var(--ob-success)', 'var(--ob-danger)', 'var(--ob-info)', 'var(--ob-warning)', '#14b8a6', '#ec4899'];
// 电信 blue, 联通 red, 移动 green, as the carriers' own colours go; anything else from the palette.
export const CARRIER_COLORS = {ct: 'var(--ob-blue)', cu: 'var(--ob-danger)', cm: 'var(--ob-success)'};
export const OTHER_COLORS = ['var(--ob-violet)', 'var(--ob-orange)', '#14b8a6', 'var(--ob-warning)', '#ec4899', 'var(--ob-info)'];

export const METRIC_RANGES = [['live', '实时'], ['1h', '1 小时'], ['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天'], ['90d', '90 天'], ['1y', '1 年']];
export const LATENCY_RANGES = [['1h', '1 小时'], ['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天']];
export const DISK_RANGES = [['7d', '7 天'], ['30d', '30 天'], ['90d', '90 天'], ['all', '全部']];
export const rangeName = (list, key) => (list.find(([k]) => k === key) || [])[1] || '';

export const pct = v => `${Math.round(v)}%`;
export const ms = v => v >= 100 || v === 0 ? `${Math.round(v)} ms` : `${Number(v).toFixed(1)} ms`;
export const lossText = v => v == null ? '—' : v === 0 ? '0%' : v < 1 ? `${v.toFixed(1)}%` : `${Math.round(v)}%`;
// A card's latency and loss on hover: what the figure is, over which targets (the summary's group), then the strip.
const PING_OVER = {三网: '三网', 国内: '国内地址', 国外: '国外地址', 出口: '出口', 其他地址: '三网以外的地址', 全部: '所有地址'};
export const pingTitle = (p, kind) => `${PING_OVER[p?.group] || p?.group || ''}最近 10 分钟的${kind === 'loss' ? '丢包率' : '平均延迟'}。下面 30 格是最近 30 分钟，每格 1 分钟。`;
// Latency as people read it: up to 80 ms good, up to 180 fair, beyond poor; loss from 1% and 5%.
export const latencyTone = v => v == null ? null : v <= 80 ? 'ok' : v <= 180 ? 'warn' : 'bad';
export const lossTone = v => v == null ? null : v < 1 ? 'ok' : v < 5 ? 'warn' : 'bad';

// How much of a sample's probes were lost, one scale for the detail page's lanes and the list's strips:
// none lost, a little (under 2%), some (under 20%), much, all of it (断连), and no sample at all.
export const LOSS_LEVELS = [['ok', '无丢包'], ['low', '< 2%'], ['mid', '2%～20%'], ['high', '20% 以上'], ['down', '全部丢失（断连）'], ['none', '没有数据']];
export const lossLevel = v => v == null ? 'none' : v === 0 ? 'ok' : v < 2 ? 'low' : v < 20 ? 'mid' : v < 100 ? 'high' : 'down';
// The colours of the list's latency and loss strips over the last half hour, a minute each.
const STRIP = {ok: 'rgb(var(--ob-success-rgb) / .55)', low: 'rgb(var(--ob-warning-rgb) / .6)', mid: 'var(--ob-orange)', warn: 'rgb(var(--ob-warning-rgb) / .8)',
  high: 'rgb(var(--ob-danger-rgb) / .7)', bad: 'rgb(var(--ob-danger-rgb) / .8)', down: 'var(--ob-danger)', none: 'rgb(var(--ob-border-rgb) / .16)'};
// A minute in which nothing came back has no latency: it is shown as the cut it was.
const latencyLevel = c => !c ? 'none' : c[1] === 100 ? 'down' : c[0] == null ? 'none' : latencyTone(c[0]);
// Minutes laid out on whole device pixels: each as wide as the others, what is left over spread over the
// gaps (no two then differ by more than a pixel). Too narrow for a gap beside a minute two pixels wide,
// the minutes run on without gaps. [[left, right]] in device pixels.
export function stripCells(devicePx, dpr = 1, n = 30) {
  if (n < 1 || devicePx < n) return null;
  const fit = gap => Math.floor((devicePx - (n - 1) * gap) / n);
  let gap = Math.max(1, Math.floor(1.5 * dpr)), cell = fit(gap);
  while (gap > 1 && cell < 2 * gap) cell = fit(--gap);
  if (n < 2 || cell < 2) return Array.from({length: n}, (_, i) => [Math.round(i * devicePx / n), Math.round((i + 1) * devicePx / n)]);
  const spare = devicePx - n * cell - (n - 1) * gap, out = [];
  for (let i = 0, x = 0; i < n; i++) {
    out.push([x, x + cell]);
    x += cell + gap + Math.floor((i + 1) * spare / (n - 1)) - Math.floor(i * spare / (n - 1));
  }
  return out;
}
// The minutes at their places (stripCells), from `lead` device pixels in: a path per colour.
export function stripPaths(cells, kind, places, lead = 1) {
  const level = kind === 'loss' ? c => lossLevel(c?.[1]) : latencyLevel, by = new Map();
  places.forEach(([a, b], i) => {
    const fill = STRIP[level(cells[i])];
    by.set(fill, `${by.get(fill) || ''}M${a + lead} 0h${b - a}v1h${a - b}z`);
  });
  return [...by].map(([fill, d]) => ({fill, d}));
}
// Before a strip's width is known: by percentages, runs of minutes alike merged, the mask making the gaps.
export function stripBackground(cells, kind) {
  const level = kind === 'loss' ? c => lossLevel(c?.[1]) : latencyLevel;
  const n = cells.length, stops = [];
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i < n && level(cells[i]) === level(cells[start])) continue;
    const color = STRIP[level(cells[start])];
    stops.push(`${color} ${(start / n * 100).toFixed(2)}% ${(i / n * 100).toFixed(2)}%`);
    start = i;
  }
  return `linear-gradient(90deg, ${stops.join(', ')})`;
}

// The four summary cards' line icons on a 24 grid, drawn with currentColor.
export const ICONS = {
  cpu: ['M8 5h8a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3z', 'M10 10h4v4h-4z', 'M9.5 2v3M14.5 2v3M9.5 19v3M14.5 19v3M2 9.5h3M2 14.5h3M19 9.5h3M19 14.5h3'],
  mem: ['M4.5 7h15A1.5 1.5 0 0 1 21 8.5v7a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 15.5v-7A1.5 1.5 0 0 1 4.5 7z', 'M7 10v4M10.3 10v4M13.7 10v4M17 10v4', 'M6 17v2.5M10 17v2.5M14 17v2.5M18 17v2.5'],
  disk: ['M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'M3 14h18', 'M7 17h4', 'M16.5 17h.01'],
  net: ['M7.5 20V4M3.5 8l4-4 4 4', 'M16.5 4v16M12.5 16l4 4 4-4'],
};
// The list card's cells and status marks, the same way.
export const CELL_ICONS = {
  cpu: ICONS.cpu, mem: ICONS.mem, disk: ICONS.disk,
  traffic: ['M4 20h16', 'M7 16v-5M12 16V6M17 16v-8'],
  rx: ['M12 4v15M6 13l6 6 6-6'], tx: ['M12 20V5M6 11l6-6 6 6'],
  rxSum: ['M12 3v11M7.5 9.5 12 14l4.5-4.5', 'M5 20h14'], txSum: ['M12 14V3M7.5 7.5 12 3l4.5 4.5', 'M5 20h14'],
  latency: ['M3 12h4l3-7 4 14 3-7h4'], loss: ['M3 12h3M10.5 12h3M18 12h3', 'M12 5v2M12 17v2'],
  expiry: ['M5 6h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z', 'M8 3.5v4M16 3.5v4M4 11h16'],
  uptime: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17z', 'M12 7.5V12l3 2'],
  update: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17z', 'M12 16V8.5M8.8 11.5 12 8.3l3.2 3.2'],
  steal: ['M4.5 17a7.5 7.5 0 1 1 15 0', 'M12 17l3.5-4.5', 'M12 17h.01'],
  // BGP 上游: one network reaching two.
  bgp: ['M5.5 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4z', 'M18.5 3.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4z', 'M18.5 16.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4z', 'M7.4 11.1l9.2-4.7M7.4 12.9l9.2 4.7'],
};
