// A server's 延迟 targets as app/servers/latency.py works them out, for its editor: the default list it
// follows (by where it is and its 三网回程: 'three' without the carriers found ordinary), what each is
// called, another server's target ids (s:<id> by ICMP, s:<id>:tcp:<port>, s:<id>:http:<port>; @v6 or @mesh
// after the id for its public IPv6 or its mesh address), and the user's own addresses checked as the settings
// take them.
export const MAX_TARGETS = 16;
export const GROUP_NAMES = {domestic: '国内服务器', optimized: '海外 · 优化线路', plain: '海外 · 普通线路'};
export const PROVINCE_ID = /^[a-z]{2}-(ct|cu|cm)$/;
const SERVER_ID = /^s:([A-Za-z0-9-]{1,40})(?:@(v6|mesh))?(?::(tcp|http):(\d{1,5}))?$/;
export const VIA_NAMES = {v6: 'IPv6', mesh: '组网'};
const CUSTOM_ID = /^u-[0-9a-f]{8}$/;
const HOST = /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;
const IPV4 = /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/;

export function serverTarget(id) {
  const m = SERVER_ID.exec(id || '');
  return m ? {server: m[1], via: m[2] || '', mode: m[3] || 'icmp', port: Number(m[4] || 0)} : null;
}

export function serverTargetId(server, mode, port, via = '') {
  const at = via ? `@${via}` : '';
  return mode === 'icmp' ? `s:${server}${at}` : `s:${server}${at}:${mode}:${port}`;
}

// A server's address as its target is sent: its public IPv4 (else IPv6), its public IPv6, or its mesh address.
export function serverAddress(item, via = '') {
  const info = item?.node?.info || {};
  return via === 'v6' ? info.ipv6 || '' : via === 'mesh' ? item?.meshAddress || '' : info.ipv4 || info.ipv6 || '';
}

// The addresses a server can be measured at, [{via, name, ip}]; its mesh one only from a server on the mesh.
export function serverAddresses(item, onMesh) {
  const info = item?.node?.info || {};
  return [...info.ipv4 ? [{via: '', name: '公网 IPv4', ip: info.ipv4}] : [],
    ...info.ipv6 ? [{via: info.ipv4 ? 'v6' : '', name: '公网 IPv6', ip: info.ipv6}] : [],
    ...onMesh && item?.meshAddress ? [{via: 'mesh', name: '组网', ip: item.meshAddress}] : []];
}

export function plainCarriers(route, carriers) {
  const results = route?.results || {};
  return new Set(carriers.map(c => c.id).filter(id => {
    const tiers = ['bj', 'sh', 'gd'].map(city => results[`${city}-${id}`]?.tier || '');
    return tiers.includes('plain') && !tiers.some(t => t === 'gold' || t === 'silver');
  }));
}

// {group, ids, leftOut}: the default list a server follows, spelt out, and the carriers its 'three' leaves out.
export function defaultTargets(latency, catalog, domestic, route) {
  const carriers = catalog?.carriers || [], provinces = catalog?.provinces || [];
  const plain = plainCarriers(route, carriers);
  const group = domestic ? 'domestic' : carriers.length && plain.size === carriers.length ? 'plain' : 'optimized';
  const code = provinces.find(p => p.name === latency?.province)?.code;
  const skip = group === 'optimized' ? plain : new Set();
  const three = code ? carriers.filter(c => !skip.has(c.id)).map(c => `${code}-${c.id}`) : [];
  const ids = [...new Set((latency?.defaults?.[group] || []).flatMap(id => id === 'three' ? three : [id]))];
  return {group, ids, leftOut: group === 'optimized' ? carriers.filter(c => plain.has(c.id)).map(c => c.name) : []};
}

const MODE_NAMES = {icmp: 'ICMP', tcp: 'TCP', http: 'HTTP'};
export function targetLabel(id, {catalog, latency, names} = {}) {
  const extra = [...(catalog?.extras || []), ...(latency?.custom || [])].find(e => e.id === id);
  if (extra) return extra.label;
  if (CUSTOM_ID.test(id)) return '已删除的地址';
  const server = serverTarget(id);
  if (server) return `${names?.[server.server] || '已删除的服务器'}${server.via ? ` · ${VIA_NAMES[server.via]}` : ''}${server.mode === 'icmp' ? '' : ` · ${MODE_NAMES[server.mode]} ${server.port}`}`;
  if (id.startsWith('c:')) return id.slice(2);
  const [code, carrier] = id.split('-');
  return `${(catalog?.provinces || []).find(p => p.code === code)?.name || code}${(catalog?.carriers || []).find(c => c.id === carrier)?.name || carrier}`;
}

// How one of the user's own addresses is probed, for its row: ICMP, TCP host:port, an HTTP URL, or the settings' way.
export function addressWay(item) {
  if (item.mode === 'http') return {way: 'HTTP', where: item.url};
  if (item.mode === 'icmp') return {way: 'ICMP', where: item.host};
  return {way: item.mode === 'tcp' ? 'TCP' : '', where: `${item.host.includes(':') ? `[${item.host}]` : item.host}:${item.port}`};
}

function localHost(host) {
  return host === 'localhost' || host === '::1' || host === '::' || /^(127\.|0\.0\.0\.0$)/.test(host);
}
const validHost = host => HOST.test(host) || IPV4.test(host) || (host.includes(':') && /^[0-9a-f:.]+$/i.test(host));

// {error, value}: one of the user's own addresses as typed (label, mode '' for the settings' way, icmp,
// tcp or http, host, port, url), checked as app/servers/latency.py _clean_custom does.
export function checkAddress(draft) {
  const label = (draft.label || '').trim(), mode = draft.mode || '';
  if (!label) return {error: '填一个名称'};
  if (label.length > 24) return {error: '名称最多 24 个字'};
  if (mode === 'http') {
    let u = null;
    try { u = new URL((draft.url || '').trim()); } catch { /* checked below */ }
    if (!u || !['http:', 'https:'].includes(u.protocol)) return {error: '网址以 http:// 或 https:// 开头，例如 https://example.com/health'};
    const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    if (u.username || u.password) return {error: '网址里不能带用户名和密码'};
    if (!validHost(host)) return {error: '网址里的主机不对'};
    if (localHost(host)) return {error: '不能是本机地址'};
    return {error: '', value: {label, mode, url: (draft.url || '').trim(), host, port: Number(u.port) || (u.protocol === 'https:' ? 443 : 80)}};
  }
  const host = (draft.host || '').trim().toLowerCase().replace(/^\[|\]$/g, '');
  if (!validHost(host)) return {error: '主机填域名或 IP 地址，例如 nas.example.com'};
  if (localHost(host)) return {error: '不能是本机地址'};
  if (mode === 'icmp') return {error: '', value: {label, mode, host, port: 0}};
  const port = Number(draft.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return {error: '端口应为 1–65535'};
  return {error: '', value: {label, ...(mode ? {mode} : {}), host, port}};
}

export function newAddressId() {
  return `u-${Array.from(crypto.getRandomValues(new Uint8Array(4)), b => b.toString(16).padStart(2, '0')).join('')}`;
}
