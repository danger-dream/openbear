// The running mesh of mesh.clarity.browser.test.mjs (six relays, ten relay links, six members),
// shared with mesh.clarity.dom.test.mjs.
export const GB = 1024 ** 3;
export function server(id, name, region, platform = 'linux') {
  const info = {hostname: id, os: 'Debian GNU/Linux 12', kernel: '6.1', arch: 'amd64', cpu: 'EPYC', cores: 2, virtualization: 'kvm', memTotal: 2 * GB, swapTotal: 0, diskTotal: 20 * GB, ipv4: '', ipv6: '', platform};
  return {id, name, local: false, ssh: null, usedBy: [], region, install: null, access: null,
    live: {online: true, connectedAtMs: Date.now() - 3600000, version: '0.13.1', caps: {exec: true, terminal: true}, at: Date.now(), m: null, features: ['mesh']},
    profile: {group: '', region, sort: 0, billing: {price: 5, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
    node: {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now(), online: true, version: '0.13.1', latest: '0.13.1', updateAvailable: false, caps: {exec: true, terminal: true}, info, geo: null}, traffic: null};
}
// [id, name, region, role, relayMode, address, x, y]
export const NODES = [
  ['ks', 'KS-MAIN', 'SG', 'member', '', 10, 20, 60], ['sea', 'SeaHost-西雅图', 'US', 'member', '', 11, 320, 60], ['lax02', 'Relay-Los Angeles LAX02', 'US', 'member', '', 2, 620, 60],
  ['sg', 'Relay-SG.PREMIUM', 'SG', 'relay', 'public', 1, 20, 260], ['rex', 'Rex.LAX', 'US', 'relay', 'public', 12, 320, 260], ['vline', 'VLINE-US.LA.TRI', 'US', 'relay', 'public', 7, 620, 260],
  ['home', '家-临时服务器', 'CN', 'relay', 'site', 3, 20, 490], ['toy', 'TOY', 'CN', 'relay', 'site', 6, 320, 490], ['mac', '公司-Mac mini', 'CN', 'member', '', 5, 620, 490],
  ['berlin', 'Relay-Berlin', 'DE', 'relay', 'public', 8, 20, 690], ['win', '公司-Windows', 'CN', 'member', '', 4, 620, 690],
  ['edgees', 'Edge-ES-9u', 'ES', 'member', '', 9, 20, 860],
];
// The relay links: [a, b, transport, rtt as measured]. TOY and Home punch theirs.
export const PAIRS = [['home', 'sg', 'tcp', 78], ['home', 'toy', 'udp', 38], ['vline', 'sg', 'udp', 169], ['home', 'vline', 'tcp', 176], ['toy', 'vline', 'tcp', 175],
  ['home', 'berlin', 'tcp', 169], ['toy', 'berlin', 'tcp', 165], ['home', 'rex', 'tcp', 172], ['toy', 'rex', 'tcp', 174], ['rex', 'sg', 'udp', 181]];
// Where each relay sends traffic for each other relay (summary.routing.routes, dest → nextHop): nothing
// goes over Rex–SG, Rex–TOY or TOY–Berlin.
export const ROUTES = {
  sg: {home: 'home', toy: 'home', berlin: 'home', vline: 'vline', rex: 'home'},
  vline: {home: 'home', toy: 'toy', berlin: 'home', sg: 'sg', rex: 'home'},
  rex: {home: 'home', toy: 'home', berlin: 'home', sg: 'home', vline: 'home'},
  home: {toy: 'toy', berlin: 'berlin', vline: 'vline', sg: 'sg', rex: 'rex'},
  toy: {home: 'home', berlin: 'home', vline: 'vline', sg: 'home', rex: 'home'},
  berlin: {home: 'home', toy: 'home', vline: 'home', sg: 'home', rex: 'home'},
};
// Entries: [relay, transport, state] in order; active is in use, ready a hot standby, untested a cold one.
export const ENTRIES = {
  ks: [['sg', 'udp', 'active']], sea: [['vline', 'udp', 'active'], ['rex', 'udp', 'ready']], lax02: [['vline', 'udp', 'active'], ['rex', 'udp', 'ready']],
  mac: [['toy', 'udp', 'active', 'lan'], ['sg', 'tls', 'ready'], ['vline', 'tcp', 'untested']], win: [['toy', 'udp', 'active', 'lan'], ['sg', 'tcp', 'ready'], ['vline', 'tcp', 'untested']],
  edgees: [['berlin', 'udp', 'active']],
};
export const SYMMETRIC = new Set(['toy', 'mac', 'win']);

// GET /mesh for that mesh. broken: TOY–Berlin goes down (nothing is routed over it) and Mac's second entry fails.
export function clarityMesh({broken = false} = {}) {
  const now = Date.now(), members = [], nodes = {}, lines = [], layout = {};
  const rtt = Object.fromEntries(PAIRS.map(([a, b, , ms]) => [[a, b].sort().join('|'), ms]));
  for (const [id, name, , role, relayMode, n, x, y] of NODES) {
    const entries = ENTRIES[id] || [];
    members.push({serverId: id, name, platform: id === 'mac' ? 'darwin' : id === 'win' ? 'windows' : 'linux', role, ...(relayMode ? {relayMode} : {}), address: `100.96.0.${n}`, publicEndpoint: '',
      publicEndpointAuto: relayMode === 'public' ? `198.51.100.${n}` : '', listen: role === 'relay' ? {udp: relayMode === 'site' ? 51998 : 51999, tcp: 52000, tls: id === 'sg' ? 52001 : 0} : {udp: 0, tcp: 0, tls: 0},
      tags: [], gateway: null, uploadLimitMbps: null, removing: false,
      entry: role === 'relay' ? {mode: 'fixed', candidates: [], autoReturn: true} : {mode: entries.length > 1 ? 'failover' : 'fixed', autoReturn: true, candidates: entries.map(([relay, transport], i) => ({id: `c${i + 1}`, relay, transport}))}});
    layout[id] = {x, y};
    const health = entries.map(([relay, transport, state, via], i) => ({id: `c${i + 1}`, relay, transport, state: broken && id === 'mac' && i === 1 ? 'failing' : state, since: now - 3600000, lastOkAt: now,
      lastError: broken && id === 'mac' && i === 1 ? 'timeout' : '', via: via || ''}));
    const links = role === 'relay' ? PAIRS.filter(p => p[0] === id || p[1] === id).map(([a, b]) => {
      const peer = a === id ? b : a, down = broken && [a, b].sort().join('|') === 'berlin|toy';
      return {peer, state: down ? 'down' : 'up', rttMs: rtt[[a, b].sort().join('|')], jitterMs: 1, loss: 0, cost: rtt[[a, b].sort().join('|')], penalty: 0};
    }) : [];
    const direct = id === 'home' ? {toy: {state: 'direct', via: 'public', rttMs: 38}} : id === 'toy' ? {home: {state: 'direct', via: 'public', rttMs: 38}}
      : id === 'mac' ? {win: {state: 'direct', via: 'lan', rttMs: 82}} : id === 'win' ? {mac: {state: 'direct', via: 'lan', rttMs: 82}} : {};
    nodes[id] = {state: 'ready', appliedRevision: 82, error: '', summary: {
      entry: entries.length ? {candidate: 'c1', relay: entries[0][0], transport: entries[0][1], switches: 0} : {}, ready: true, peers: 3,
      rttMs: role === 'relay' ? null : {ks: 1.3, sea: 28, lax02: 0.6, mac: 5.9, win: 0.3, edgees: 48}[id], entryHealth: health,
      reachability: {verdict: SYMMETRIC.has(id) ? 'symmetric' : 'public', publicAddr: `203.0.113.${n}:51999`, consistent: true, filtering: 'port', checkedAt: now},
      routing: ROUTES[id] ? {mode: 'dynamic', links, routes: Object.entries(ROUTES[id]).map(([dest, nextHop]) => ({dest, nextHop, cost: 1, path: [id, nextHop], since: now, switches: 0}))} : null,
      direct}};
    entries.forEach(([relay, transport, state], i) => lines.push({id: `entry:${id}|${relay}|${transport}`, kind: 'entry', a: id, b: relay, transport, punch: relay === 'toy',
      state: state === 'active' ? 'up' : state === 'ready' ? 'standby' : 'cold', role: state === 'active' ? 'active' : state === 'ready' ? 'standby' : 'cold', rttMs: state === 'untested' ? null : 20 + i, lossRate: 0, ends: {a: null, b: null}}));
  }
  for (const [a, b, transport] of PAIRS) {
    const key = [a, b].sort().join('|'), down = broken && key === 'berlin|toy';
    lines.push({id: `relay:${key}`, kind: 'relay', a, b, transport, punch: key === 'home|toy', state: down ? 'down' : 'up', role: 'active', rttMs: down ? null : rtt[key], lossRate: 0, ends: {a: null, b: null}});
  }
  lines.push({id: 'direct:mac|win', kind: 'direct', a: 'mac', b: 'win', transport: 'udp', punch: true, state: 'up', role: 'active', rttMs: 82, lossRate: 0, ends: {a: null, b: null}});
  const view = {ok: true, settings: {prefix: '100.96.0.0/16', mtu: 1400, ports: {udp: 51999, tcp: 52000, tls: 52001}, health: {}},
    members, relayLinks: {mode: 'custom', pairs: PAIRS.map(([a, b, transport]) => ({a, b, transport, cost: 1, dialer: 'a', bias: 0}))},
    directLinks: [{a: 'mac', b: 'win', transport: 'udp', dialer: 'a'}], rules: [], applied: {revision: 82, at: now - 3600000}, draft: {dirty: false, changes: []},
    nodes, installing: [], layout, exports: [], eligible: [], apply: {running: false, revision: 82, progress: []}, lines, errors: [], warnings: []};
  return view;
}
