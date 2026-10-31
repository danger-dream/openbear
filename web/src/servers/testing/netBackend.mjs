// The server network tests' fake controller (servers, live, the mesh and the net API) and page
// helpers, shared by the browser tests (testing/netBrowser.mjs) and the happy-dom tests (testing/netDom.mjs).
import assert from 'node:assert/strict';

export const GB = 1024 ** 3;
export function server(id, name, ipv4, {region = 'US', online = true, platform = 'linux', ipv6 = ''} = {}) {
  const info = {hostname: id, os: 'Debian GNU/Linux 12', kernel: '6.1', arch: 'amd64', cpu: 'EPYC', cores: 2, virtualization: 'kvm', memTotal: 2 * GB, swapTotal: 0, diskTotal: 20 * GB, ipv4, ipv6, platform};
  const m = {cpu: 5, steal: 0, iowait: 0, mem: {total: 2 * GB, used: GB / 2}, swap: {total: 0, used: 0}, disk: {total: 20 * GB, used: 4 * GB}, load: [0.1, 0.1, 0.1],
    io: {readRate: 0, writeRate: 0}, net: {rx: 0, tx: 0, rxRate: 1000, txRate: 1000}, tcp: 10, udp: 2, processes: 90, uptime: 86400, boot: 1, nodeRss: 10e6};
  return {id, name, local: false, ssh: null, usedBy: [], region, install: null, access: null,
    live: online ? {online: true, connectedAtMs: Date.now() - 3600000, version: '0.14.0', caps: {exec: false, terminal: false}, at: Date.now(), m, features: ['mesh']} : null,
    profile: {group: '', region, sort: 0, billing: {price: 5, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
    node: {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now() - (online ? 0 : 3600000), online, version: '0.14.0', latest: '0.14.0', updateAvailable: false, caps: {exec: false, terminal: false}, info, geo: null},
    traffic: null};
}
export const SERVERS = () => [server('home', '家', '192.0.2.117', {region: 'CN'}), server('toy', 'TOY', '192.0.2.34', {region: 'CN'}),
  server('vm', 'VLINE', '203.0.113.7', {ipv6: '2001:db8:7::7'}), server('lax', 'LAX02', '45.76.0.2'), server('pub', 'Pub-SG', '198.51.100.20', {region: 'SG'}),
  server('old', 'Old-VPS', '198.51.100.30', {region: 'JP'}), server('win', 'Win-Box', '198.51.100.40', {online: false, platform: 'windows'})];

// The mesh: home and toy are site relays (punched to each other), vm a public relay (TCP from toy), lax a member entering vm by UDP.
export function fakeMesh() {
  const member = (serverId, name, role, address, extra = {}) => ({serverId, name, platform: 'linux', role, address, publicEndpoint: '', publicEndpointAuto: '', listen: {udp: 51999, tcp: 52000, tls: 0},
    tags: [], gateway: null, uploadLimitMbps: null, entry: role === 'relay' ? null : {mode: 'fixed', candidates: [], autoReturn: true}, removing: false, ...extra});
  const doc = {settings: {prefix: '100.96.0.0/16', mtu: 1400, ports: {udp: 51999, tcp: 52000, tls: 52001}, health: {}},
    members: [member('home', '家', 'relay', '100.96.0.3', {relayMode: 'site', gateway: {subnets: ['10.10.10.0/24'], masquerade: true}}), member('toy', 'TOY', 'relay', '100.96.0.6', {relayMode: 'site'}),
      member('vm', 'VLINE', 'relay', '100.96.0.7'), member('lax', 'LAX02', 'member', '100.96.0.2', {entry: {mode: 'fixed', autoReturn: true, candidates: [{id: 'c1', relay: 'vm', transport: 'udp'}]}})],
    relayLinks: {mode: 'custom', pairs: [{a: 'home', b: 'toy', transport: 'udp', cost: 3, dialer: 'a', bias: 0}, {a: 'toy', b: 'vm', transport: 'tcp', cost: 170, dialer: 'a', bias: 0}]}, directLinks: [], rules: []};
  const summary = extra => ({ready: true, peers: 2, rttMs: null, entry: {}, ...extra});
  const nodes = {home: {state: 'ready', appliedRevision: 5, error: '', summary: summary({})}, toy: {state: 'ready', appliedRevision: 5, error: '', summary: summary({})},
    vm: {state: 'ready', appliedRevision: 5, error: '', summary: summary({})},
    lax: {state: 'ready', appliedRevision: 5, error: '', summary: summary({rttMs: 0.6, entry: {candidate: 'c1', relay: 'vm', transport: 'udp'}, entryHealth: [{id: 'c1', state: 'active', relay: 'vm', via: '', since: Date.now() - 3600000}]})}};
  const layout = {vm: {x: 0, y: 0}, toy: {x: 320, y: 0}, home: {x: 640, y: 0}, lax: {x: 0, y: 220}};
  const hop = (node, via, rttMs) => ({node, via, rttMs});
  const PATHS = {'home>lax': {hops: [hop('home', '', null), hop('toy', 'relay', 2.4), hop('vm', 'relay', 167.2), hop('lax', 'entry', 0)], rttMs: 169.6, stale: false},
    'lax>home': {hops: [hop('lax', '', null), hop('vm', 'entry', 0.6), hop('toy', 'relay', 167.2), hop('home', 'relay', 2.4)], rttMs: 170.2, stale: true}};
  const pathOf = (src, dst) => PATHS[`${src}>${dst}`] || {hops: [hop(src, '', null), hop(dst, 'relay', 40)], rttMs: 40, stale: false};
  const view = () => ({ok: true, ...JSON.parse(JSON.stringify(doc)), applied: {revision: 5, at: Date.now()}, draft: {dirty: false, changes: []}, nodes, eligible: [], apply: {running: false, revision: 5, progress: []},
    errors: [], warnings: [], exports: [{gateway: 'home', subnet: '10.10.10.0/24', nodes: ['lax']}], layout, installing: []});
  return {doc, pathOf, handle(method, path, query) {
    if (method === 'GET' && path === '') return [200, view()];
    const q = new URLSearchParams(query);
    if (method === 'GET' && path === '/path') return [200, {ok: true, ...pathOf(q.get('src'), q.get('dst'))}];
    if (method === 'GET' && path === '/routes') return [200, {ok: true, routes: doc.members.filter(m => m.serverId !== q.get('src')).map(m => ({dst: m.serverId, ...pathOf(q.get('src'), m.serverId)})), subnets: []}];
    if (method === 'GET' && /\/trend$/.test(path)) return [404, {ok: false, error: 'not_found'}];
    if (method === 'GET' && path.startsWith('/members/')) { const id = path.split('/').pop(); return [200, {ok: true, member: doc.members.find(m => m.serverId === id), node: nodes[id], status: {ready: true, error: '', peers: []}, events: []}]; }
    return [404, {ok: false, error: 'not_found'}];
  }};
}

// The controller's network endpoints (contract §3): addresses, and probes that advance one step per read.
export const GEO = {'203.0.113.1': {country: 'JP', countryName: 'Japan', asn: 2914, org: 'NTT America, Inc.'}, '203.0.113.9': {country: 'JP', countryName: 'Japan', asn: 2914, org: 'NTT America, Inc.'},
  '198.51.100.77': {country: 'JP', countryName: 'Japan', asn: 2516, org: 'KDDI CORPORATION'}, '93.184.216.34': {country: 'US', countryName: 'United States', asn: 15133, org: 'Edgecast Inc.'}};
export const ADDRESSES = {
  home: {interfaces: [
    {name: 'eth0', up: true, mac: '52:54:00:12:34:56', kind: 'ethernet', addrs: [{ip: '10.10.10.251', prefixLen: 24, family: 4, scope: 'private', egress: false}, {ip: '2001:db8:10::251', prefixLen: 64, family: 6, scope: 'public', egress: false}]},
    {name: 'obmesh0', up: true, mac: '', kind: 'mesh', addrs: [{ip: '100.96.0.3', prefixLen: 16, family: 4, scope: 'mesh', egress: false}, {ip: '100.96.255.3', prefixLen: 24, family: 4, scope: 'mesh', egress: false}]},
    {name: 'wg0', up: false, mac: '', kind: 'tunnel', addrs: [{ip: '100.64.12.5', prefixLen: 10, family: 4, scope: 'cgnat', egress: false}]},
    {name: 'eth1', up: true, mac: '52:54:00:aa:bb:cc', kind: 'ethernet', addrs: [{ip: '169.254.20.7', prefixLen: 16, family: 4, scope: 'linklocal', egress: false}]}],
    publicIPv4: '192.0.2.117', publicIPv6: '', publicIPv4Proxy: '192.0.2.92', meshAddress: '100.96.0.3'},
  vm: {interfaces: [{name: 'eth0', up: true, mac: '52:54:00:00:00:07', kind: 'ethernet', addrs: [{ip: '203.0.113.7', prefixLen: 24, family: 4, scope: 'public', egress: true}, {ip: '2001:db8:7::7', prefixLen: 64, family: 6, scope: 'public', egress: true}]},
    {name: 'obmesh0', up: true, mac: '', kind: 'mesh', addrs: [{ip: '100.96.0.7', prefixLen: 16, family: 4, scope: 'mesh', egress: false}, {ip: '100.96.255.7', prefixLen: 24, family: 4, scope: 'mesh', egress: false}]}], publicIPv4: '203.0.113.7', publicIPv6: '2001:db8:7::7', meshAddress: '100.96.0.7'},
  lax: {interfaces: [{name: 'ens3', up: true, mac: '52:54:00:00:00:02', kind: 'ethernet', addrs: [{ip: '45.76.0.2', prefixLen: 23, family: 4, scope: 'public', egress: true}, {ip: '10.8.0.2', prefixLen: 24, family: 4, scope: 'private', egress: false},
    {ip: '2001:db8:2::2', prefixLen: 64, family: 6, scope: 'public', egress: false}]}, {name: 'obmesh0', up: true, mac: '', kind: 'mesh', addrs: [{ip: '100.96.0.2', prefixLen: 16, family: 4, scope: 'mesh', egress: false}, {ip: '100.96.255.2', prefixLen: 24, family: 4, scope: 'mesh', egress: false}]},
    {name: 'tailscale0', up: true, mac: '', kind: 'tunnel', addrs: [{ip: '100.101.7.9', prefixLen: 32, family: 4, scope: 'cgnat', egress: false}, {ip: 'fd7a:115c:a1e0::9', prefixLen: 128, family: 6, scope: 'private', egress: false}]}],
    publicIPv4: '45.76.0.2', publicIPv6: '', meshAddress: '100.96.0.2'},
};
export const MESH_ADDR = {'100.96.0.3': 'home', '100.96.0.6': 'toy', '100.96.0.7': 'vm', '100.96.0.2': 'lax'};
export function fakeNet(mesh) {
  const probes = new Map();
  let n = 0;
  const round1 = v => Math.round(v * 10) / 10;
  function pingResults(p) {
    const out = [];
    for (let seq = 1; seq <= Math.min(p.reads, p.count); seq++) {
      const fail = p.target === 'far.example' ? {2: 'ttl exceeded', 3: 'no route to host'}[seq] : p.mode === 'tcping' ? {2: 'refused', 4: 'unreachable'}[seq] : seq % 4 === 3 ? 'timeout' : '';
      out.push({seq, at: p.startedAt + seq * 1000, ok: !fail, rttMs: fail ? null : round1(seq === 1 ? 4.56 : 12 + seq * 0.37), ttl: fail || p.mode === 'tcping' ? 0 : 54, error: fail || ''});
    }
    return out;
  }
  function summary(results) {
    if (!results.length) return null;
    const ok = results.filter(r => r.ok).map(r => r.rttMs), d = ok.slice(1).map((v, i) => Math.abs(v - ok[i]));
    return {sent: results.length, received: ok.length, lossPct: round1((results.length - ok.length) / results.length * 100), minMs: ok.length ? Math.min(...ok) : null,
      avgMs: ok.length ? round1(ok.reduce((a, b) => a + b, 0) / ok.length) : null, maxMs: ok.length ? Math.max(...ok) : null, jitterMs: d.length ? round1(d.reduce((a, b) => a + b, 0) / d.length) : null};
  }
  function hops(p) {
    const addr = (ip, scope, name = '') => ({ip, name, scope, geo: scope === 'public' && p.reads >= 4 ? GEO[ip] || null : null});
    // cn2.example: backbones the lookup knows by name only (no AS number), two addresses on a hop, loss, a long name.
    if (p.target === 'cn2.example') {
      const geo = g => p.reads >= 4 ? g : null, pub = (ip, g, name = '') => ({ip, name, scope: 'public', geo: geo(g)});
      const hop = (ttl, a, ms, lossPct = 0, reached = false) => ({ttl, addrs: a, sent: 3, received: a.length ? Math.round(3 * (1 - lossPct / 100)) : 0, lossPct: a.length ? lossPct : 100,
        lastMs: ms, minMs: ms === null ? null : round1(ms * 0.97), avgMs: ms, maxMs: ms === null ? null : round1(ms * 1.05), reached});
      return [
        hop(1, [addr('192.168.1.1', 'private', 'router.lan')], 0.6),
        hop(2, [pub('59.43.246.85', {country: 'CN', countryName: 'China', asn: null, org: 'Chinatelecom Next Carrying Network backbone'}), pub('59.43.247.9', {country: 'CN', countryName: 'China', asn: null, org: ''})], 38.4, 33.3),
        hop(3, [], null),
        hop(4, [pub('69.194.171.42', {country: 'US', countryName: 'United States', asn: null, org: ''}, 'ae12-0.r01.losangeles.ctgnet.backbone.example.net')], 152.7),
        hop(5, [pub('93.184.216.34', GEO['93.184.216.34'])], 158.2, 0, true)].slice(0, p.reads <= 1 ? 2 : 5);
    }
    // gap.example: hop 2 not tried yet while it runs (sent 0, lossPct 0); hop 4 a router answering "unreachable" (listed, not the end).
    if (p.target === 'gap.example') {
      const seen = (ttl, a, ms) => ({ttl, addrs: a, sent: 1, received: 1, lossPct: 0, lastMs: ms, minMs: ms, avgMs: ms, maxMs: ms, reached: false});
      const two = p.state === 'running' ? {ttl: 2, addrs: [], sent: 0, received: 0, lossPct: 0, lastMs: null, minMs: null, avgMs: null, maxMs: null, reached: false} : seen(2, [addr('10.20.0.1', 'private')], 1.2);
      return [seen(1, [addr('10.10.10.1', 'private', 'gateway.lan')], 0.4), two, seen(3, [addr('100.64.0.1', 'cgnat')], 3.1), ...(p.state === 'running' ? [] : [seen(4, [addr('192.0.2.254', 'private')], 9.8)])];
    }
    const all = [
      {ttl: 1, addrs: [addr('10.10.10.1', 'private', 'gateway.lan')], lastMs: 0.4, minMs: 0.3, avgMs: 0.4, maxMs: 0.6},
      {ttl: 2, addrs: [], lastMs: null, minMs: null, avgMs: null, maxMs: null},
      {ttl: 3, addrs: [addr('100.64.0.1', 'cgnat')], lastMs: 3.1, minMs: 2.9, avgMs: 3.0, maxMs: 3.1},
      {ttl: 4, addrs: [addr('203.0.113.1', 'public', 'ae-1.r20.tokyjp05.jp.bb.gin.ntt.net'), addr('203.0.113.9', 'public')], lastMs: 21.4, minMs: 20.9, avgMs: 21.2, maxMs: 21.4},
      {ttl: 5, addrs: [addr('198.51.100.77', 'public', 'core.example.jp')], lastMs: 48.2, minMs: 47.9, avgMs: 48.0, maxMs: 48.3},
      {ttl: 6, addrs: [addr('93.184.216.34', 'public')], lastMs: 151.0, minMs: 150.2, avgMs: 150.7, maxMs: 151.3, reached: true}];
    const rounds = Math.min(p.reads, p.count);
    return all.slice(0, p.reads <= 1 ? 3 : p.reads === 2 ? 5 : 6).map(h => ({...h, sent: rounds, received: h.addrs.length ? rounds : 0, lossPct: h.addrs.length ? 0 : 100, reached: Boolean(h.reached)}));
  }
  function view(p) {
    const trace = p.mode === 'trace', results = trace ? [] : pingResults(p);
    if (p.state === 'running' && !p.fail && (trace ? p.reads >= p.count + 3 : results.length >= p.count)) Object.assign(p, {state: 'done', endedAt: Date.now()});
    const meshPath = p.mesh ? {kind: p.mesh.kind, dst: p.mesh.dst, subnet: p.mesh.subnet, ...mesh.pathOf(p.sid, p.mesh.dst)} : null;
    const h = trace ? hops(p) : [];
    return {ok: true, id: p.id, mode: p.mode, requestedMode: p.requestedMode, state: p.state, error: p.state === 'error' ? p.fail : '', target: p.target,
      ip: p.fail.startsWith('dns') ? '' : p.ip, family: p.fail.startsWith('dns') ? 0 : p.ip.includes(':') ? 6 : 4, port: p.port || 0, startedAt: p.startedAt, endedAt: p.endedAt || 0,
      results, summary: trace ? null : summary(results), hops: h, reached: h.some(x => x.reached), meshPath};
  }
  // A restarted controller has lost what it noted about the tests: the node's mode, no mesh path.
  const restart = () => { for (const p of probes.values()) Object.assign(p, {requestedMode: p.mode, mesh: null}); };
  return {probes, restart, handle(method, sid, rest, body) {
    if (method === 'GET' && rest === '/addresses') {
      if (sid === 'old') return [409, {ok: false, error: 'node_upgrade_required'}];
      if (sid === 'win') return [409, {ok: false, error: 'node_offline'}];
      return [200, {ok: true, ...(ADDRESSES[sid] || {interfaces: [{name: 'eth0', up: true, mac: '52:54:00:00:00:99', kind: 'ethernet', addrs: [{ip: '172.16.0.9', prefixLen: 24, family: 4, scope: 'private', egress: false}]}], publicIPv4: '', publicIPv6: '', meshAddress: ''})}];
    }
    if (method === 'POST' && rest === '/probes') {
      if (sid === 'old') return [409, {ok: false, error: 'node_upgrade_required'}];
      if (body.mode === 'tcping' && !body.port) return [400, {ok: false, error: 'invalid_probe', field: 'port'}];
      if (body.target === 'busy.example') return [409, {ok: false, error: 'probe_busy'}];
      if (body.target === 'v6only.example' && body.family === 4) return [400, {ok: false, error: 'invalid_probe', field: 'family'}];
      const member = mesh.doc.members.find(m => m.serverId === sid);
      const dst = MESH_ADDR[body.target];
      const hit = member && (dst ? {kind: 'member', dst, subnet: null} : sid === 'lax' && body.target.startsWith('10.10.10.') ? {kind: 'gateway', dst: 'home', subnet: '10.10.10.0/24'} : null);
      const id = `p${++n}`, mode = hit && body.mode === 'trace' ? 'ping' : body.mode;
      probes.set(id, {id, sid, mode, requestedMode: body.mode, target: body.target, ip: /^[\d.:a-f]+$/.test(body.target) ? body.target : body.target === 'example.com' ? '93.184.216.34' : '198.51.100.99',
        port: body.port, count: body.count || (mode === 'trace' ? 3 : 10), reads: 0, state: 'running', startedAt: Date.now(), mesh: hit, fail: {'nxdomain.invalid': 'dns_failed: lookup nxdomain.invalid: no such host', 'noicmp.example': 'icmp_unavailable: socket: operation not permitted'}[body.target] || '', body});
      return [200, {ok: true, id}];
    }
    const pid = rest.split('/')[2], p = probes.get(pid);
    if (!p || p.sid !== sid) return [404, {ok: false, error: 'not_found'}];
    if (method === 'GET') {
      if (p.state === 'running') p.reads++;
      if (p.fail && p.reads >= 1) Object.assign(p, {state: 'error', endedAt: Date.now()});
      else if (p.state !== 'running' && p.mode === 'trace') p.reads++;
      return [200, view(p)];
    }
    if (method === 'DELETE') { if (p.state === 'running') Object.assign(p, {state: 'stopped', endedAt: Date.now()}); return [200, {ok: true}]; }
    return [404, {ok: false, error: 'not_found'}];
  }};
}

export const addresses = page => page.locator('section.net-addresses').first();
export const probe = page => page.locator('section.net-probe').first();
export const press = (h, locator) => h.phone ? locator.tap() : locator.click();
export async function waitFor(check, what, ms = 8000) {
  for (let i = 0; i < ms / 100; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 100)); }
  assert.fail(`timed out: ${what}`);
}
export async function run(h, {mode = 'Ping', target, port = '', count = '', root = probe(h.page)} = {}) {
  await press(h, root.getByRole('radio', {name: mode, exact: true}));
  await root.getByRole('combobox', {name: '目标'}).fill(target);
  // Tab closes the suggestions and moves on (Escape would also close the mesh panel when they are not open).
  await root.getByRole('combobox', {name: '目标'}).press('Tab');
  await root.page().locator('.ob-suggest-list').waitFor({state: 'hidden'});
  if (port) await root.getByRole('textbox', {name: '端口'}).fill(port);
  if (count) {
    if (!(await root.locator('details.net-advanced').evaluate(el => el.open))) await press(h, root.locator('details.net-advanced > summary'));
    await root.getByRole('textbox', {name: mode === '路由追踪' ? '轮数' : '次数'}).fill(count);
  }
  const before = h.net.probes.size;
  await press(h, root.locator('.net-go'));
  await waitFor(() => h.net.probes.size > before, 'a probe to start');
  return `p${h.net.probes.size}`;
}
export const rowTexts = async root => (await root.locator('.net-results > li').allInnerTexts()).map(text => text.replace(/\s+/g, ' ').trim());
