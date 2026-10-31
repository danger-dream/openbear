// Display rules for a server's addresses and its network tests (ping, tcping, trace); no requests here.
import { entrySegment, HOP_VIA, hopsOf, isSite, msText, transportLabel } from './mesh/meshHelpers.js';
import { ENTRY_VIA, punchedRelayLink } from './mesh/meshGraph.js';

// ---- addresses (GET /{id}/net/addresses) ----
export const IFACE_KINDS = {mesh: '组网', tunnel: '隧道'};
// [tone, label]; 'other' gets no tag. A link-local IPv4 says the interface got no address from DHCP.
export const SCOPES = {public: ['', '公网'], private: ['', '私有'], mesh: ['info', '组网'], cgnat: ['', 'CGNAT'], linklocal: ['warn', '链路本地']};
// Interfaces with their addresses and tags, and the public exits that sit on no interface (behind NAT).
// The mesh interface also carries the controller's address for the machine (control): listed, but quiet.
export function addressList(reply) {
  const list = (Array.isArray(reply?.interfaces) ? reply.interfaces : []).filter(i => i && Array.isArray(i.addrs) && i.addrs.length).map(i => ({
    name: String(i.name || ''), up: i.up !== false, kind: i.kind || 'other', mac: i.mac || '',
    addrs: i.addrs.filter(a => a?.ip).map(a => {
      const tags = [], control = a.scope === 'mesh' && Boolean(reply?.meshAddress) && a.ip !== reply.meshAddress;
      if (control) return {ip: String(a.ip), text: Number.isInteger(a.prefixLen) ? `${a.ip}/${a.prefixLen}` : String(a.ip), tags: [['', '控制']], control: true};
      if (a.egress) tags.push(['ok', '公网出口']);
      // The interface already says it is the mesh one.
      const scope = SCOPES[a.scope];
      if (scope && !(a.egress && a.scope === 'public') && !(a.scope === 'mesh' && i.kind === 'mesh')) tags.push(scope);
      if (a.family === 6 || String(a.ip).includes(':')) tags.push(['', 'IPv6']);
      return {ip: String(a.ip), text: Number.isInteger(a.prefixLen) ? `${a.ip}/${a.prefixLen}` : String(a.ip), tags, control: false};
    }).sort((x, y) => Number(x.control) - Number(y.control)),
  })).filter(i => i.addrs.length);
  const onCard = new Set(list.flatMap(i => i.addrs.map(a => a.ip)));
  const exits = [reply?.publicIPv4, reply?.publicIPv6].filter(ip => ip && !onCard.has(ip));
  // The exit services abroad see, when a proxy sends traffic abroad another way than home.
  const proxy = reply?.publicIPv4Proxy && reply.publicIPv4Proxy !== reply.publicIPv4 ? String(reply.publicIPv4Proxy) : '';
  return {interfaces: list, exits, proxy, line: proxy ? String(reply.publicIPv4 || '') : '', count: list.reduce((n, i) => n + i.addrs.length, 0)};
}
// The first `limit` addresses (0: all), interfaces kept whole up to that point.
export function firstAddresses(interfaces, limit) {
  if (!limit) return interfaces;
  const out = [];
  let left = limit;
  for (const i of interfaces) {
    if (left <= 0) break;
    out.push({...i, addrs: i.addrs.slice(0, left)});
    left -= i.addrs.length;
  }
  return out;
}

// ---- the test form ----
export const PROBE_MODES = [['ping', 'Ping'], ['tcping', 'TCPing'], ['trace', '路由追踪']];
export const FAMILIES = [[0, '自动'], [4, 'IPv4'], [6, 'IPv6']];
export const countRange = mode => mode === 'trace' ? [1, 10, 3] : [1, 100, 10];
export const countLabel = mode => mode === 'trace' ? '轮数' : '次数';
const RANGE = {intervalMs: [200, 10000, 1000], timeoutMs: [200, 10000, 2000]};
export const DEFAULTS = {intervalMs: 1000, timeoutMs: 2000};
const whole = v => /^\d+$/.test(String(v).trim()) ? Number(String(v).trim()) : NaN;
// A pasted URL keeps its host: the scheme, path and IPv6 brackets go. A port stays and is reported.
export function cleanTarget(raw) {
  let t = String(raw ?? '').trim().replace(/^[a-z][a-z0-9+.-]*:\/\//i, '');
  t = t.replace(/[/?#].*$/, '');
  const bracket = /^\[([^\]]+)\](?::(\d+))?$/.exec(t);
  if (bracket) return {host: bracket[1], port: bracket[2] || ''};
  const hostPort = /^([^:]+):(\d+)$/.exec(t);
  return hostPort ? {host: hostPort[1], port: hostPort[2]} : {host: t, port: ''};
}
// The request for POST /{id}/net/probes, or what is wrong with each field: {body, errors}.
const literalFamily = host => /^\d{1,3}(\.\d{1,3}){3}$/.test(host) ? 4 : host.includes(':') ? 6 : 0;
export function probeRequest(form) {
  const errors = {}, mode = form.mode, {host, port: typedPort} = cleanTarget(form.target);
  if (!host) errors.target = '请填写目标';
  // The controller takes ASCII names only: an internationalised one goes in as punycode (xn--…).
  else if (/[^\x00-\x7f]/.test(host)) errors.target = '中文域名请换成 xn-- 开头的 Punycode 形式，或直接填 IP';
  else if (!/^[A-Za-z0-9._:%-]+$/.test(host)) errors.target = '只填主机名或 IP，例如 example.com 或 1.1.1.1';
  else if (host.length > 253) errors.target = '最长 253 个字符';
  else if (typedPort && mode !== 'tcping') errors.target = '不用带端口，只填主机名或 IP';
  else if (typedPort && String(form.port ?? '').trim() && Number(form.port) !== Number(typedPort)) errors.target = '目标里的端口和“端口”不一致';
  const body = {mode, target: host, family: Number(form.family) || 0};
  // An address of the other family can never be reached that way (the controller turns it down: field family).
  if (!errors.target && body.family && literalFamily(host) && literalFamily(host) !== body.family) errors.family = fieldError('family', mode);
  if (mode === 'tcping') {
    const port = whole(String(form.port ?? '').trim() || typedPort);
    if (!String(form.port ?? '').trim() && !typedPort) errors.port = '请填写端口';
    else if (!(port >= 1 && port <= 65535)) errors.port = '端口应为 1–65535';
    else body.port = port;
  }
  const [lo, hi] = countRange(mode);
  if (String(form.count ?? '').trim()) {
    const n = whole(form.count);
    if (!(n >= lo && n <= hi)) errors.count = `${countLabel(mode)}应为 ${lo}–${hi}`;
    else body.count = n;
  }
  for (const key of ['intervalMs', 'timeoutMs']) {
    if (!String(form[key] ?? '').trim()) continue;
    const n = whole(form[key]), [a, b] = RANGE[key];
    if (!(n >= a && n <= b)) errors[key] = `应为 ${a}–${b} 毫秒`;
    else body[key] = n;
  }
  return {body, errors};
}
// invalid_probe names the field the controller turned down.
export function fieldError(field, mode) {
  const [lo, hi] = countRange(mode);
  return {target: '目标不正确：只填主机名或 IP', port: '端口应为 1–65535', count: `${countLabel(mode)}应为 ${lo}–${hi}`, intervalMs: '应为 200–10000 毫秒',
    timeoutMs: '应为 200–10000 毫秒', family: '协议和目标地址不符：IPv4 地址选“IPv4”或“自动”，IPv6 同理', mode: '测试方式不正确', maxHops: '最大跳数应为 1–64'}[field] || '';
}

// ---- results (GET /{id}/net/probes/{pid}) ----
export const PROBE_STATES = {running: ['info', '测试中'], done: ['ok', '已完成'], stopped: ['off', '已停止'], error: ['bad', '出错']};
// One result's error: the known ones in words, anything else is the system's short text as it is.
// ttl exceeded: a router on the way answered "time exceeded" (the packet ran out of hops).
const FAILURES = {timeout: ['timeout', '超时'], refused: ['refused', '拒绝'], unreachable: ['unreachable', '不可达'], 'ttl exceeded': ['ttl', '中途超时']};
const failure = r => FAILURES[String(r?.error || '').trim().toLowerCase()];
export const resultText = r => r?.ok ? msText(r.rttMs) || '—' : failure(r)?.[1] || String(r?.error || '失败');
export const failureKind = r => r?.ok ? '' : failure(r)?.[0] || 'other';
// The whole test failing (state error): a known prefix in words, the reason after it in small print.
const PROBE_ERRORS = {dns_failed: '解析域名失败', icmp_unavailable: '这台机器上无法发送 ping'};
export function probeErrorText(raw) {
  const text = String(raw || '').trim(), m = /^([a-z_]+):\s*(.*)$/is.exec(text) || /^([a-z_]+)$/i.exec(text);
  const title = m && PROBE_ERRORS[m[1].toLowerCase()];
  return title ? {title, detail: (m[2] || '').trim()} : {title: text, detail: ''};
}
// A trace hop not tried yet (a running trace lists up to the highest hop with an answer; the ones below may still be at sent 0).
export const hopPending = h => !(Number(h?.sent) > 0);
export const pctText = v => Number.isFinite(v) ? `${Math.round(v * 10) / 10}%` : '—';
// [label, value] for the summary line; null before the first result.
export function summaryParts(s) {
  if (!s || typeof s !== 'object') return null;
  const ms = v => Number.isFinite(v) ? msText(v) : '—';
  return [['发送', String(s.sent ?? 0)], ['收到', String(s.received ?? 0)], ['丢包', pctText(s.lossPct)], ['最小', ms(s.minMs)], ['平均', ms(s.avgMs)], ['最大', ms(s.maxMs)], ['抖动', ms(s.jitterMs)]];
}
// Addresses that are not public have no place to look up; they say what they are instead.
export const GEO_SCOPES = {private: '私有地址', cgnat: 'CGNAT', mesh: '组网', linklocal: '链路本地', other: ''};
// geoSkipped: over the server's lookup quota, so never asked for; shown as 未查询.
export const geoPending = a => a?.scope === 'public' && !a.geo && !a.geoSkipped;
export const hopsWaitingForGeo = hops => (Array.isArray(hops) ? hops : []).some(h => (h?.addrs || []).some(geoPending));
// AS number and operator. A missing AS number (null, 0) or a private one (what a lookup gives for a backbone like CN2
// it does not know, with "Unknown" as the operator) is left out; with no operator either, the country's name.
const realAsn = asn => { const n = Number(asn); return Number.isInteger(n) && n > 0 && !(n >= 64512 && n <= 65534) && !(n >= 4200000000 && n <= 4294967294) && n !== 23456 ? n : 0; };
const realOrg = org => { const t = String(org ?? '').trim(); return /^(unknown|n\/a|-)$/i.test(t) ? '' : t; };
export const geoText = geo => {
  if (!geo) return '';
  const asn = realAsn(geo.asn), org = realOrg(geo.org);
  return [asn ? `AS${asn}` : '', org || (asn ? '' : String(geo.countryName || geo.country || '').trim())].filter(Boolean).join(' ');
};

// ---- the mesh path (meshPath) ----
// How a hop is reached from the one before, in the words the board puts on that line:
// an entry by LAN or internet once punched, else its transport; a relay link punched, or its transport.
export function hopWay(work, data, a, b, via) {
  const members = work?.members || [], find = id => members.find(m => m.serverId === id);
  if (via === 'relay') {
    if (punchedRelayLink(work, a, b)) return '打洞';
    const pair = work?.relayLinks?.mode === 'custom' ? (work.relayLinks.pairs || []).find(p => (p.a === a && p.b === b) || (p.a === b && p.b === a)) : null;
    return pair?.transport ? transportLabel(pair.transport) : HOP_VIA.relay;
  }
  if (via === 'entry') {
    const seg = entrySegment(data, a, b);
    if (seg && ENTRY_VIA[seg.via]) return ENTRY_VIA[seg.via];
    for (const [m, relay] of seg ? [[seg.member, seg.relay]] : [[a, b], [b, a]]) {
      const c = (find(m)?.entry?.candidates || []).find(x => x.relay === relay && (!seg || x.id === seg.id));
      if (c) return isSite(find(relay)) ? '打洞' : transportLabel(c.transport);
    }
    return HOP_VIA.entry;
  }
  return HOP_VIA[via] || '';
}
// The machines the traffic passes ("家 → TOY（打洞 2.4 ms）→ …"), the total, and the tail behind a gateway.
// name(id) names a machine; target is the address tested.
export function meshPathView(path, {work = null, data = null, name = id => id, target = ''} = {}) {
  if (!path || typeof path !== 'object') return null;
  const hops = hopsOf(path.hops), dst = path.dst || '';
  const fill = hops.map((h, i) => !i ? null : h.rttMs === 0 ? (h.via === 'entry' ? entrySegment(data, hops[i - 1].node, h.node)?.rttMs ?? null : null) : h.rttMs);
  const segments = hops.map((h, i) => ({node: h.node, name: name(h.node), way: i ? hopWay(work, data, hops[i - 1].node, h.node, h.via) : '', ms: i ? msText(fill[i]) : ''}));
  const broken = !hops.length || hops.at(-1).node !== dst;
  let total = Number.isFinite(path.rttMs) ? path.rttMs : null;
  if (total !== null && hops.slice(1).some(h => h.rttMs === 0)) total = fill.slice(1).every(v => v !== null) ? fill.slice(1).reduce((x, v) => x + v, 0) : null;
  const gateway = path.kind === 'gateway' && !broken && target ? {target, note: '经网关，局域网'} : null;
  const part = s => [s.way, s.ms].filter(Boolean).join(' ');
  // No space between a closing full-width bracket and the arrow: "TOY（打洞 2.4 ms）→ VLINE".
  const text = [...segments.map((s, i) => i && part(s) ? `${s.name}（${part(s)}）` : s.name), ...(gateway ? [`${target}（${gateway.note}）`] : [])]
    .reduce((out, p) => !out ? p : `${out}${out.endsWith('）') ? '' : ' '}→ ${p}`, '');
  const through = [...segments.map(s => s.name), ...(gateway ? [target] : [])].join(' → ');
  return {kind: path.kind || 'member', dst, subnet: path.subnet || '', hops, segments, gateway, broken, stale: Boolean(path.stale), total, text, through,
    totalText: broken ? '' : total === null ? '总延迟未知' : `总延迟约 ${msText(total)}`,
    brokenText: broken ? (hops.length ? `在 ${name(hops.at(-1).node)} 之后断开，现在到不了 ${name(dst)}` : '现在没有可用的路径') : ''};
}

// ---- target suggestions ----
// {value, note}: the other servers' public addresses; for a mesh member, the other members' mesh
// addresses too (first when the test is started from the mesh page).
export function targetOptions(servers, selfId, members, {meshFirst = false} = {}) {
  const live = (members || []).filter(m => !m.removing);
  const self = live.find(m => m.serverId === selfId);
  const mesh = self ? live.filter(m => m.serverId !== selfId && m.address).map(m => ({value: m.address, note: `${m.name} · 组网`})) : [];
  const pub = (servers || []).filter(s => s?.id !== selfId).flatMap(s => [s.node?.info?.ipv4, s.node?.info?.ipv6].filter(Boolean).map(ip => ({value: ip, note: s.name})));
  const seen = new Set();
  return (meshFirst ? [...mesh, ...pub] : [...pub, ...mesh]).filter(o => !seen.has(o.value) && seen.add(o.value));
}
