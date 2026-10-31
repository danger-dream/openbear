// The mesh board's fake controller (servers list, live, metrics and the mesh API), shared by the
// mesh browser tests (testing/meshBrowser.mjs) and the happy-dom tests (testing/meshDom.mjs).
export const GB = 1024 ** 3;
export function server(id, name, {region = 'DE', online = true, platform = 'linux'} = {}) {
  const info = {hostname: id, os: 'Debian GNU/Linux 12', kernel: '6.1', arch: 'amd64', cpu: 'EPYC', cores: 2, virtualization: 'kvm', memTotal: 2 * GB, swapTotal: 0, diskTotal: 20 * GB, ipv4: `198.51.100.${id.length}`, ipv6: '', platform};
  const m = {cpu: 5, steal: 0, iowait: 0, mem: {total: 2 * GB, used: GB / 2}, swap: {total: 0, used: 0}, disk: {total: 20 * GB, used: 4 * GB}, load: [0.1, 0.1, 0.1],
    io: {readRate: 0, writeRate: 0}, net: {rx: 0, tx: 0, rxRate: 1000, txRate: 1000}, tcp: 10, udp: 2, processes: 90, uptime: 86400, boot: 1, nodeRss: 10e6};
  return {id, name, local: false, ssh: null, usedBy: [], region, install: null, access: null,
    live: online ? {online: true, connectedAtMs: Date.now() - 3600000, version: '0.13.1', caps: {exec: true, terminal: true}, at: Date.now(), m, features: ['mesh']} : null,
    profile: {group: '', region, sort: 0, billing: {price: 5, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
    node: {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now(), online, version: '0.13.1', latest: '0.13.1', updateAvailable: false, caps: {exec: true, terminal: true}, info, geo: null},
    traffic: null};
}
export const SERVERS = () => [server('berlin', 'Berlin'), server('lax', 'LAX', {region: 'US'}), server('home', 'Home', {region: 'CN'}),
  server('mac', '公司-Mac', {region: 'CN', platform: 'darwin'}), server('win', 'Win-Box', {online: false, platform: 'windows'}), server('old', 'Old-VPS', {region: 'JP'})];
export const member = (serverId, name, role, extra = {}) => ({serverId, name, platform: 'linux', role, address: '', publicEndpoint: '', publicEndpointAuto: `198.51.100.${serverId.length}`,
  listen: {udp: role === 'relay' ? 51999 : 0, tcp: 0, tls: 0}, tags: [], gateway: null, uploadLimitMbps: null, entry: role === 'relay' ? null : {mode: 'fixed', candidates: [], autoReturn: true}, removing: false, ...extra});

// A small stand-in for the controller's mesh API: a draft, validation with
// paths, installs that take a moment, a board layout, trends, and a rollout
// that advances one step per GET. `old` leaves out what newer controllers add.
export function fakeMesh({failing = '', upgrades = false, old = false, punch = false, legacyDirect = false, reach = false, noPath = false, routes = false, oldRoutes = false, noisy = false, gone = '', lines = false, allSubnets = false, secure = false, firewall = false} = {}) {
  if (routes) reach = true;
  const doc = {
    settings: {prefix: '100.96.0.0/16', mtu: 1400, ports: {udp: 51999, tcp: 52000, tls: 52001}, health: {}},
    members: [member('berlin', 'Berlin', 'relay', {address: '100.96.0.1'}), member('lax', 'LAX', 'relay', {address: '100.96.0.2', listen: {udp: 51999, tcp: 52000, tls: 0}}),
      member('home', 'Home', 'member', {address: '100.96.0.3', tags: ['家'], entry: {mode: 'failover', autoReturn: true, candidates: [{id: 'c1', relay: 'berlin', transport: 'udp'}, {id: 'c2', relay: 'lax', transport: 'tcp'}]}})],
    relayLinks: {mode: 'full', pairs: []}, directLinks: [], rules: [],
  };
  // A rule for every member and every gateway network: never an arrow, listed in the legend.
  const ruleSide = (o = {}) => ({all: false, allSubnets: false, members: [], tags: [], prefixes: [], subnets: [], ...o});
  if (allSubnets) doc.rules.push({id: 'r-all', action: 'allow', src: ruleSide({all: true, allSubnets: true}), dst: ruleSide({all: true}), proto: 'icmp', ports: [], note: ''});
  // Lines as the controller reports them: the entry in use, a cold standby that one end has not
  // reported, and a punched relay link with public mappings.
  const LINES = [
    {id: 'entry:home|lax|tcp', kind: 'entry', a: 'home', b: 'lax', transport: 'tcp', punch: false, state: 'up', role: 'active', rttMs: 152.3, lossRate: 0.004,
      ends: {a: {local: '10.0.0.5:40112', public: '', remote: '198.51.100.3:52000', dialer: true}, b: {local: '198.51.100.3:52000', public: '', remote: '203.0.113.7:40112', dialer: false}}},
    // A standby entry not connected: no local address, only where it would go.
    {id: 'entry:home|berlin|udp', kind: 'entry', a: 'home', b: 'berlin', transport: 'udp', punch: false, state: 'cold', role: 'cold', rttMs: null, lossRate: null,
      ends: {a: {local: '', public: '203.0.113.7:61022', remote: '198.51.100.6:51999', dialer: true}, b: null}},
    {id: 'relay:berlin|lax', kind: 'relay', a: 'berlin', b: 'lax', transport: 'udp', punch: true, state: 'up', role: 'active', rttMs: 141.8, lossRate: 0,
      ends: {a: {local: '192.168.10.2:51999', public: '203.0.113.20:40001', remote: '203.0.113.30:40002', dialer: true}, b: {local: '172.16.0.9:51999', public: '203.0.113.30:40002', remote: '203.0.113.20:40001', dialer: false}}},
  ];
  const STEP = {'1h': 120000, '24h': 2880000, '3d': 8640000, '7d': 20160000, '15d': 43200000, '30d': 86400000};
  // GET /mesh/lines/:id/trend as the controller answers it (mesh_lines.py): line, summary with
  // availability in percent, and a point per bucket, every value null where nothing was sampled.
  const lineTrend = (id, range) => {
    const l = LINES.find(x => x.id === id), cold = l.role === 'cold', relay = l.kind === 'relay', step = STEP[range] || STEP['24h'], now = Date.now();
    const none = at => ({at, rttMs: null, lossRate: null, upRatio: null, downs: null, role: null});
    // The relay link over the last hour: nothing recorded at all.
    const empty = relay && range === '1h';
    const series = Array.from({length: 30}, (_, i) => {
      const at = now - (30 - i) * step;
      if (empty) return none(at);
      // Cold: only every fifth point was tried; the rest have no sample (null), not time down.
      if (cold) return i % 5 === 0 ? {at, rttMs: 160 + i, lossRate: 0, upRatio: i === 15 ? 0 : 1, downs: i === 15 ? 1 : 0, role: 'cold'} : none(at);
      // The relay link was only recorded for the last quarter of the day.
      if (relay && i < 22) return none(at);
      const down = i === (relay ? 26 : 18), part = !relay && i === 9;
      return {at, rttMs: down ? null : 140 + (i % 6) * 5, lossRate: down ? null : part ? 0.08 : (i % 4) * 0.002, upRatio: down ? 0 : part ? 0.6 : 1, downs: down || part ? 1 : 0, role: 'active'};
    });
    const summary = empty ? {availability: null, rttMs: {avg: null, min: null, max: null, p95: null}, lossRate: null, downs: 0, downSeconds: 0, knownSeconds: 0}
      : cold ? {availability: 83.33, rttMs: {avg: 167.2, min: 160, max: 185, p95: 183}, lossRate: 0, downs: 1, downSeconds: 60, knownSeconds: 360}
      : relay ? {availability: 98.75, rttMs: {avg: 142.4, min: 139.8, max: 166.1, p95: 160.2}, lossRate: 0.001, downs: 1, downSeconds: 270, knownSeconds: 21600}
      : {availability: 95.42, rttMs: {avg: 151.7, min: 138.2, max: 212.9, p95: 171.4}, lossRate: 0.006, downs: 2, downSeconds: 450, knownSeconds: 86400};
    return {ok: true, line: {id, kind: l.kind, a: l.a, b: l.b, transport: l.transport, current: true}, range, summary, series};
  };
  // Punch: four members behind NAT (no address from outside) with UDP direct links in each state.
  const live = {}, punchEvents = [];
  if (punch) {
    const nat = {publicEndpoint: '', publicEndpointAuto: '', listen: {udp: 51999, tcp: 0, tls: 0}};
    doc.members[0].listen = {udp: 51999, tcp: 52000, tls: 0};
    doc.members[1].listen = {udp: 0, tcp: 52000, tls: 0};
    Object.assign(doc.members[2], nat, {entry: {mode: 'failover', autoReturn: true, candidates: [{id: 'c1', relay: 'berlin', transport: 'tcp'}, {id: 'c2', relay: 'lax', transport: 'tcp'}]}});
    doc.members.push(member('office', 'Office', 'member', {...nat, address: '100.96.0.5', entry: {mode: 'fixed', autoReturn: true, candidates: [{id: 'o1', relay: 'lax', transport: 'tcp'}]}}),
      member('cafe', 'Cafe', 'member', {...nat, address: '100.96.0.6', entry: {mode: 'fixed', autoReturn: true, candidates: []}}),
      member('nas', 'NAS', 'member', {...nat, listen: {udp: 0, tcp: 0, tls: 0}, address: '100.96.0.7', entry: {mode: 'fixed', autoReturn: true, candidates: []}}));
    doc.directLinks = [{a: 'home', b: 'office', transport: 'udp', dialer: 'a'}, {a: 'office', b: 'cafe', transport: 'udp', dialer: 'a'}, {a: 'home', b: 'cafe', transport: 'udp', dialer: 'a'}];
    const now = Date.now(), d = (id, direct, extra = {}) => ({id, kind: 'direct', active: true, transport: 'udp', endpoint: '', rxBytes: 1e6, txBytes: 5e5, lastHandshakeUnixNano: String((now - 8000) * 1e6), rttMs: null, direct, ...extra});
    const lan = {state: 'direct', via: 'lan', since: now - 12 * 60000, attempts: 1, lastError: ''};
    const relayed = {state: 'relayed', via: '', since: now - 5 * 60000 - 2000, attempts: 4, lastError: 'punch timeout'};
    live.home = [d('office', lan, {endpoint: '192.168.1.20:51999', rttMs: 3.2}), d('cafe', relayed)];
    live.office = [d('home', lan, {endpoint: '192.168.1.10:51999', rttMs: 2.8}), d('cafe', {state: 'direct', via: 'public', since: now - 40000, attempts: 2, lastError: ''}, {endpoint: '203.0.113.9:40211', rttMs: 38})];
    live.cafe = [d('office', {state: 'punching', via: '', since: now - 15000, attempts: 3, lastError: 'punch timeout'}), d('home', relayed)];
    punchEvents.push({seq: 30, at: now - 12 * 60000, kind: 'direct.up', detail: 'office via lan'}, {seq: 31, at: now - 9 * 60000, kind: 'direct.down', detail: 'cafe: direct path lost'},
      {seq: 32, at: now - 5 * 60000, kind: 'direct.failed', detail: 'cafe: punch timeout'});
  }
  // Reach: the target topology of the third batch. SG and LAX are public relays, Home and TOY site relays
  // (behind NAT) that dial them and punch to each other; Mac and Win enter through TOY with a public standby.
  const status = {}, reachEvents = [], now = Date.now();
  const noisyEvents = {
    home: [{seq: 90, at: now - 60000, firstAt: now - 2 * 3600000 - 60000, repeat: 37, kind: 'path.ready', detail: 'c1'},
      {seq: 91, at: now - 2 * 60000, firstAt: now - 3 * 3600000 - 60000, repeat: 12, kind: 'path.failed', detail: 'c1: probe timeout'},
      {seq: 92, at: now - 5 * 60000, kind: 'control.error', detail: 'peer(E0pT…SN0k) - Failed to send handshake initiation: write udp 0.0.0.0:51999->[::ffff:192.0.2.203]:51999: address ::ffff:192.0.2.203: non-IPv4 address'},
      {seq: 93, at: now - 6 * 60000, kind: 'owner.ack', detail: 'lax member=home seq=12'}],
    lax: [['home', 'berlin'], ['berlin', 'home'], ['home', 'berlin'], ['berlin', 'home']].map(([a, b], i) => ({seq: 70 - i, at: now - (i + 1) * 60000, kind: 'route.changed', detail: `berlin: ${a} -> ${b} (better)`}))
      .concat([{seq: 60, at: now - 3600000, kind: 'config.applied', detail: 'revision 12'}, {seq: 59, at: now - 2 * 3600000, kind: 'route.skipped', detail: "gateway subnets [10.10.10.0/24] overlap this machine's networks; not routed into the mesh"}]),
  };
  if (reach) {
    const site = extra => ({relayMode: 'site', publicEndpoint: '', publicEndpointAuto: '', listen: {udp: 51999, tcp: 0, tls: 0}, ...extra});
    doc.settings.reachability = {stun: []};
    doc.members = [member('sg', 'SG', 'relay', {address: '100.96.0.1', relayMode: 'public', publicEndpointAuto: '192.0.2.79', listen: {udp: 51999, tcp: 52000, tls: 52001}}),
      member('lax', 'LAX', 'relay', {address: '100.96.0.2', publicEndpointAuto: '192.0.2.137', listen: {udp: 51999, tcp: 52000, tls: 0}}),
      member('home', 'Home', 'relay', site({address: '100.96.0.3'})), member('toy', 'TOY', 'relay', site({address: '100.96.0.4'})),
      member('mac', '公司-Mac', 'member', {address: '100.96.0.5', publicEndpointAuto: '', entry: {mode: 'failover', autoReturn: true, candidates: [{id: 'm1', relay: 'toy', transport: 'udp'}, {id: 'm2', relay: 'lax', transport: 'tcp'}]}}),
      member('win', 'Win-Box', 'member', {address: '100.96.0.6', publicEndpointAuto: '', entry: {mode: 'failover', autoReturn: true, candidates: [{id: 'w1', relay: 'toy', transport: 'udp'}, {id: 'w2', relay: 'sg', transport: 'tcp'}]}}),
      member('lab', 'Lab', 'member', {address: '100.96.0.7', publicEndpointAuto: '', entry: {mode: 'fixed', autoReturn: true, candidates: [{id: 'l1', relay: 'sg', transport: 'udp'}]}})];
    doc.relayLinks = {mode: 'custom', pairs: [{a: 'home', b: 'sg', transport: 'udp', cost: 1, dialer: 'a'}, {a: 'home', b: 'lax', transport: 'udp', cost: 1, dialer: 'a'},
      {a: 'toy', b: 'sg', transport: 'udp', cost: 1, dialer: 'a'}, {a: 'home', b: 'toy', transport: 'udp', cost: 1, dialer: 'a'}]};
    const observers = addr => [{id: 'sg', kind: 'relay', addr, ok: true}, {id: 'stun.miwifi.com:3478', kind: 'stun', addr, ok: true}, {id: 'stun.l.google.com:19302', kind: 'stun', addr: '', ok: false}];
    const r = (verdict, publicAddr, extra = {}) => ({checkedAt: now - 3 * 60000, verdict, publicAddr, consistent: true, mapping: 'stable', filtering: 'port', observers: observers(publicAddr), ...extra});
    Object.assign(status, {
      sg: {reachability: r('public', '192.0.2.79:51999', {filtering: 'open'})}, lax: {reachability: r('public', '192.0.2.137:51999', {filtering: 'open'})},
      home: {reachability: r('punchable', '192.0.2.117:51999'), peers: [{id: 'toy', kind: 'relay', active: true, transport: 'udp', endpoint: '192.0.2.34:51999', rxBytes: 9e6, txBytes: 8e6,
        lastHandshakeUnixNano: String((now - 9000) * 1e6), rttMs: 2.5, direct: {state: 'direct', via: 'public', since: now - 42 * 60000, attempts: 2, lastError: ''}}]},
      toy: {reachability: r('punchable', '192.0.2.34:51999'), peers: [{id: 'home', kind: 'relay', active: true, transport: 'udp', endpoint: '192.0.2.117:51999', rxBytes: 8e6, txBytes: 9e6,
        lastHandshakeUnixNano: String((now - 9000) * 1e6), rttMs: 2.6, direct: {state: 'direct', via: 'public', since: now - 42 * 60000, attempts: 2, lastError: ''}}]},
      // Mac: the LAX standby has never connected (UDP to most foreign addresses is dropped at the office).
      mac: {reachability: r('punchable', '192.0.2.34:51999'), entry: {candidate: 'm1', relay: 'toy', transport: 'udp', switches: 0,
        candidates: [{id: 'm1', relay: 'toy', transport: 'udp', state: 'active', since: now - 30 * 60000, lastOkAt: now, lastError: ''},
          {id: 'm2', relay: 'lax', transport: 'tcp', state: 'failing', since: now - 6 * 3600000, lastOkAt: 0, lastError: 'timeout'}]}},
      // Win: a proxy rewrites the exit; the observers disagree.
      win: {reachability: r('unknown', '192.0.2.42:51999', {consistent: false, mapping: 'unknown', filtering: 'unknown',
        observers: [{id: 'sg', kind: 'relay', addr: '192.0.2.34:51999', ok: true}, {id: 'stun.miwifi.com:3478', kind: 'stun', addr: '192.0.2.42:51999', ok: true}, {id: 'stun.l.google.com:19302', kind: 'stun', addr: '', ok: false}]}),
        entry: {candidate: 'w2', relay: 'sg', transport: 'tcp', switches: 1, candidates: [{id: 'w1', relay: 'toy', transport: 'udp', state: 'punching', since: now - 20000, lastOkAt: 0, lastError: ''},
          {id: 'w2', relay: 'sg', transport: 'tcp', state: 'active', since: now - 3600000, lastOkAt: now, lastError: ''}]}},
      lab: {reachability: r('punchable', '192.0.2.117:40022')},
    });
    // Routes (the fourth batch): both office machines are on TOY over the LAN, their public standbys only wait;
    // Home is a gateway for two networks, one of them given to LAX by a rule.
    if (routes) {
      const via = v => oldRoutes ? {} : {via: v};
      status.mac.entry.candidates[0] = {...status.mac.entry.candidates[0], ...via('lan')};
      status.win.entry = {candidate: 'w1', relay: 'toy', transport: 'udp', switches: 0, candidates: [{id: 'w1', relay: 'toy', transport: 'udp', state: 'active', since: now - 50 * 60000, lastOkAt: now, lastError: '', ...via('lan')},
        {id: 'w2', relay: 'sg', transport: 'tcp', state: 'ready', since: now - 50 * 60000, lastOkAt: now, lastError: '', ...via('')}]};
      status.lab.entry = {candidate: 'l1', relay: 'sg', transport: 'udp', switches: 0, candidates: [{id: 'l1', relay: 'sg', transport: 'udp', state: 'active', since: now - 3600000, lastOkAt: now, lastError: '', ...via('')}]};
      // The controller fills in TOY's LAN address for the office machines; the board does not show it, edits keep it.
      doc.members[4].entry.candidates[0].endpoint = 'udp://10.20.20.7:51998';
      doc.members[2].gateway = {subnets: ['10.10.10.0/24', '192.168.50.0/24'], masquerade: true};
      doc.rules = [{id: 'r1', src: {members: ['lax'], tags: [], prefixes: []}, dst: {members: [], tags: [], subnets: ['10.10.10.0/24']}, proto: 'any', ports: [], note: ''}];
    }
    reachEvents.push({seq: 40, at: now - 20 * 60000, kind: 'reach.changed', detail: 'unknown -> punchable'}, {seq: 41, at: now - 15 * 60000, kind: 'link.suppressed', detail: 'lax'},
      {seq: 42, at: now - 10 * 60000, kind: 'route.changed', detail: 'toy: sg -> toy (better)'});
  }
  let applied = JSON.parse(JSON.stringify(doc)), revision = 12, rollout = null, installing = [];
  const layout = old ? null : reach ? {sg: {x: 130, y: 0}, lax: {x: 560, y: 0}, home: {x: 0, y: 210}, toy: {x: 430, y: 210}, mac: {x: 250, y: 430}, win: {x: 600, y: 430}, lab: {x: 860, y: 210}} : punch ? {berlin: {x: 60, y: 0}, lax: {x: 360, y: 0}, home: {x: -90, y: 230}, office: {x: 510, y: 230}, cafe: {x: 210, y: 420}, nas: {x: -90, y: 420}}
    : {berlin: {x: 0, y: 0}, lax: {x: 300, y: 0}, home: {x: 150, y: 220}};
  const nodes = {berlin: {state: 'ready', appliedRevision: 12, error: '', summary: {entry: {}, ready: true, peers: 2, rttMs: null}},
    lax: {state: 'ready', appliedRevision: 12, error: '', summary: {entry: {}, ready: true, peers: 2, rttMs: null}},
    home: {state: 'ready', appliedRevision: 12, error: '', summary: {entry: {candidate: 'c2', relay: 'lax', transport: 'tcp', switches: 2}, ready: true, peers: 1, rttMs: 152.3,
      ...(old ? {} : {memory: {rssBytes: 230 * 2 ** 20, limitBytes: 256 * 2 ** 20, level: 'near'}})}}};
  if (punch) for (const id of ['office', 'cafe', 'nas']) nodes[id] = {state: 'ready', appliedRevision: 12, error: '', summary: {entry: {}, ready: true, peers: 2, rttMs: 60}};
  // With lines: Home's first entry is a cold standby (untested), the second in use.
  if (lines) nodes.home.summary.entryHealth = [{id: 'c2', state: 'active'}, {id: 'c1', state: 'untested'}];
  // Newer controllers summarise each member's punched peers in GET /mesh: {peer: {state, via, rttMs}}, {} without any.
  if (punch && !legacyDirect) for (const [id, n] of Object.entries(nodes)) n.summary.direct = Object.fromEntries((live[id] || []).map(p => [p.id, {state: p.direct.state, via: p.direct.via, rttMs: p.rttMs}]));
  if (reach) {
    for (const k of Object.keys(nodes)) delete nodes[k];
    const link = (peer, state, rttMs, jitterMs, loss, cost) => ({peer, state, rttMs, jitterMs, loss, cost, penalty: state === 'suppressed' ? 2600 : 0});
    const routing = {
      sg: [link('home', 'up', 79.4, 1.2, 0, 82), link('toy', 'up', 75.2, 0.9, 0, 77)],
      lax: [link('home', 'suppressed', 182, 46, 0.3, null)],
      home: [link('sg', 'up', 79.4, 1.2, 0, 82), link('lax', 'up', 171, 31, 0.1, 333), link('toy', 'up', 2.5, 0.2, 0, 2.9)],
      toy: [link('home', 'up', 2.6, 0.2, 0, 3), link('sg', 'up', 75.2, 0.9, 0, 77)]};
    const summaryOf = id => {
      const s = status[id], {observers, mapping, ...reachability} = s.reachability;
      return {entry: s.entry ? {candidate: s.entry.candidate, relay: s.entry.relay, transport: s.entry.transport, switches: s.entry.switches} : {}, ready: true, peers: 3,
        rttMs: {mac: 6.2, win: 81, lab: 79}[id] ?? null, reachability, entryHealth: s.entry ? s.entry.candidates : [],
        routing: routing[id] ? {mode: 'dynamic', links: routing[id], routes: []} : null,
        direct: Object.fromEntries((s.peers || []).filter(p => p.direct).map(p => [p.id, {state: p.direct.state, via: p.direct.via, rttMs: p.rttMs}]))};
    };
    for (const m of doc.members) nodes[m.serverId] = {state: 'ready', appliedRevision: 12, error: '', summary: summaryOf(m.serverId)};
    nodes.mac.alerts = [{code: 'mesh_standby_unhealthy'}];
    nodes.win.alerts = [{code: 'mesh_exit_inconsistent'}];
  }
  // GET /mesh/path answers from here; tests change it to show a new route.
  let pathOf = (src, dst) => src === 'home' && dst === 'mac' ? {hops: [{node: 'home', via: '', rttMs: null}, {node: 'toy', via: 'relay', rttMs: 2.6}, {node: 'mac', via: 'lan', rttMs: 6.1}], rttMs: 8.7, stale: false}
    : {hops: [{node: src, via: '', rttMs: null}, {node: dst, via: 'relay', rttMs: null}], rttMs: null, stale: true};
  // GET /mesh/routes: where each node's traffic goes. Mac's as the office sees it; the rest one relay hop away.
  const hop = (node, via, rttMs) => ({node, via, rttMs});
  const ROUTES = {
    mac: {routes: [{dst: 'sg', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0), hop('sg', 'relay', null)], rttMs: null, stale: true},
      {dst: 'lax', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0), hop('home', 'relay', 2.6), hop('lax', 'relay', 171)], rttMs: 177.6, stale: false},
      {dst: 'home', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0), hop('home', 'relay', 2.6)], rttMs: 6.6, stale: false},
      {dst: 'toy', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0)], rttMs: 4.0, stale: false},
      {dst: 'win', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0), hop('win', 'lan', 4.1)], rttMs: 8.1, stale: false},
      {dst: 'lab', hops: [hop('mac', '', null), hop('toy', 'lan', 4.0), hop('sg', 'relay', 75.2)], rttMs: null, stale: true}], subnets: []},
    lax: {subnets: [{subnet: '10.10.10.0/24', gateway: 'home', hops: [hop('lax', '', null), hop('home', 'relay', 171)], rttMs: 171, stale: false}]},
  };
  const routesOf = src => ({routes: ROUTES[src]?.routes || doc.members.filter(m => m.serverId !== src).map(m => ({dst: m.serverId, hops: [hop(src, '', null), hop(m.serverId, 'relay', 40)], rttMs: 40, stale: false})),
    subnets: ROUTES[src]?.subnets || []});
  if (routes) pathOf = (src, dst) => {
    const r = routesOf(src), hit = r.routes.find(x => x.dst === dst) || r.subnets.find(x => x.gateway === dst);
    return {hops: hit.hops, rttMs: hit.rttMs, stale: hit.stale};
  };
  // exports: who may reach each gateway network by the draft's rules (an empty side is everyone).
  const exportsNow = () => doc.members.filter(g => g.gateway && !g.removing).flatMap(g => g.gateway.subnets.map(subnet => ({gateway: g.serverId, subnet,
    nodes: doc.members.filter(m => m.serverId !== g.serverId && !m.removing && doc.rules.some(r => r.dst.subnets.includes(subnet)
      && (!r.src.members.length && !r.src.tags.length && !r.src.prefixes.length || r.src.members.includes(m.serverId) || r.src.tags.some(t => m.tags.includes(t))))).map(m => m.serverId)})));
  // Warnings block nothing: two symmetric NATs will not punch.
  const symmetric = id => nodes[id]?.summary?.reachability?.verdict === 'symmetric';
  const site = id => doc.members.some(m => m.serverId === id && m.role === 'relay' && m.relayMode === 'site');
  const warnings = () => (doc.relayLinks.pairs || []).map((p, i) => [p, `relayLinks.pairs[${i}]`]).filter(([p]) => site(p.a) && site(p.b))
    .concat((doc.directLinks || []).map((p, i) => [p, `directLinks[${i}]`]))
    .filter(([p]) => symmetric(p.a) && symmetric(p.b)).map(([, path]) => ({path, error: 'mesh_punch_impossible', message: 'both ends are behind symmetric NAT'}));
  for (const n of Object.values(nodes)) Object.assign(n, {dataVersion: '0.13.1', upgrade: {state: '', error: '', at: 0}});
  if (upgrades) {
    doc.members[2].platform = 'darwin';
    nodes.lax.upgrade.state = 'upgrading';
    nodes.berlin.upgrade.state = 'waiting';
    Object.assign(nodes.home, {dataVersion: '0.13.0', upgrade: {state: '', error: 'upgrade_not_ready', at: Date.now() - 180000}});
  }
  // Gone: the member's mesh service was removed (the node installed again over it). Rollouts skip it until it is reinstalled.
  let reinstallOutcome = '';
  const goneEvents = [];
  if (gone) {
    Object.assign(nodes[gone], {state: 'missing', error: 'mesh_service_missing'});
    goneEvents.push({seq: 19, at: now - 4 * 60000, kind: 'mesh.missing', detail: ''});
  }
  const eligible = () => [['mac', '公司-Mac', ''], ['win', 'Win-Box', '节点离线'], ['old', 'Old-VPS', '节点版本过旧，需要先更新']]
    .filter(([id]) => !doc.members.some(m => m.serverId === id && !m.removing)).map(([serverId, name, reason]) => ({serverId, name, ok: !reason, reason}));
  function changes() {
    const out = [];
    for (const m of doc.members) {
      const before = applied.members.find(a => a.serverId === m.serverId);
      if (m.removing) out.push({kind: 'member.remove', text: `移除成员 ${m.name}`});
      else if (!before) out.push({kind: 'member.add', text: `新增成员 ${m.name}`});
      else if (JSON.stringify(before.entry) !== JSON.stringify(m.entry)) out.push({kind: 'member.update', text: `修改 ${m.name} 的入口`});
      else if (JSON.stringify(before) !== JSON.stringify(m)) out.push({kind: 'member.update', text: `修改 ${m.name}`});
    }
    // The controller words the change itself; the page shows its text as it is.
    const everyone = doc.rules.filter(r => r.src?.all && r.dst?.all && !applied.rules.some(a => a.id === r.id));
    if (everyone.length) out.push(...everyone.map(r => ({kind: 'rule.add', text: `新增访问规则：所有成员 → 所有成员 · ${r.proto === 'icmp' ? 'ICMP' : r.proto.toUpperCase()}`})));
    else if (JSON.stringify(applied.rules) !== JSON.stringify(doc.rules)) out.push({kind: 'rules', text: '修改访问规则'});
    if (JSON.stringify(applied.directLinks) !== JSON.stringify(doc.directLinks)) out.push({kind: 'links', text: '修改直连'});
    if (JSON.stringify(applied.settings) !== JSON.stringify(doc.settings)) out.push({kind: 'settings', text: '修改网格设置'});
    return out;
  }
  function validate(d) {
    const errors = [];
    d.members.forEach((m, i) => (m.entry?.candidates || []).forEach((c, j) => {
      const relay = d.members.find(x => x.serverId === c.relay);
      if (!(relay?.listen?.[c.transport] > 0)) errors.push({path: `members[${i}].entry.candidates[${j}]`, error: 'mesh_entry_listen', message: `${relay?.name} 没有开启 ${c.transport.toUpperCase()} 监听`});
    }));
    // Punched direct links: UDP on both ends, and a relay listening on UDP to learn the outside addresses from.
    const udp = id => d.members.find(m => m.serverId === id)?.listen?.udp > 0;
    (d.directLinks || []).forEach((p, i) => {
      if ((p.transport || 'udp') !== 'udp') return;
      if (!udp(p.a) || !udp(p.b)) errors.push({path: `directLinks[${i}]`, error: 'udp_not_listening', message: 'both ends must listen on udp'});
      if (!d.members.some(m => m.role === 'relay' && m.listen?.udp > 0)) errors.push({path: `directLinks[${i}]`, error: 'punch_no_reflector', message: 'no relay listens on udp'});
    });
    // Site relays: a link to a public relay; entries through one also need a public relay to start from.
    const site = id => d.members.some(m => m.serverId === id && m.role === 'relay' && m.relayMode === 'site');
    const publicRelay = id => d.members.some(m => m.serverId === id && m.role === 'relay' && m.relayMode !== 'site');
    d.members.forEach((m, i) => {
      if (site(m.serverId) && d.relayLinks?.mode === 'custom' && !(d.relayLinks.pairs || []).some(p => (p.a === m.serverId && publicRelay(p.b)) || (p.b === m.serverId && publicRelay(p.a))))
        errors.push({path: `members[${i}].relayMode`, error: 'mesh_site_relay_needs_public_link', message: 'site relay needs a link to a public relay'});
      const c = m.role === 'relay' ? [] : m.entry?.candidates || [];
      if (c.some(x => site(x.relay)) && !c.some(x => publicRelay(x.relay))) errors.push({path: `members[${i}].entry.candidates`, error: 'mesh_entry_needs_bootstrap', message: 'entry needs a public relay'});
    });
    return errors;
  }
  function step() {
    if (!rollout?.running) return;
    const p = rollout.progress.find(x => x.state === 'pending' || x.state === 'applying');
    if (!p) { rollout.running = false; return; }
    if (p.state === 'pending' && nodes[p.serverId]?.state === 'missing') { p.state = 'missing'; p.error = 'mesh_service_missing'; }
    else if (p.state === 'pending') p.state = 'applying';
    else if (p.serverId === failing) { p.state = 'failed'; p.error = 'invalid_config: self.address outside network.prefix'; }
    else { p.state = 'done'; nodes[p.serverId] = {...(nodes[p.serverId] || {}), state: 'ready', appliedRevision: rollout.revision, error: '', summary: nodes[p.serverId]?.summary || {entry: {}, ready: true, peers: 1, rttMs: 80}}; }
    if (!rollout.progress.some(x => x.state === 'pending' || x.state === 'applying')) rollout.running = false;
  }
  const view = () => ({ok: true, ...JSON.parse(JSON.stringify(doc)), applied: {revision, at: Date.now()}, draft: {dirty: JSON.stringify(doc) !== JSON.stringify(applied), changes: changes()},
    nodes, eligible: eligible(), apply: rollout ? JSON.parse(JSON.stringify(rollout)) : {running: false, revision, progress: []}, errors: [], ...(reach ? {warnings: warnings()} : {}), ...(routes && !oldRoutes ? {exports: exportsNow()} : {}), ...(lines ? {lines: LINES} : {}),
    // The gateway on Berlin reaching an instance on Home's mesh address, as the controller adds it.
    ...(firewall ? {autoRules: [{kind: 'instance', proto: 'tcp', serverId: 'home', gateway: 'berlin', instanceId: 'i-edge', name: 'Edge-Fra', port: 18970, src: '100.96.0.1/32', dst: '100.96.0.3/32'}]} : {}),
    ...(old ? {} : {layout: JSON.parse(JSON.stringify(layout)), installing: JSON.parse(JSON.stringify(installing))})});
  const trend = range => Array.from({length: 30}, (_, i) => ({at: Date.now() - (30 - i) * 120000, rttMs: i === 20 ? null : 140 + (i % 7) * 6, timeoutRate: i === 20 ? 1 : 0, switches: i === 20 ? 1 : 0,
    reconnects: i === 12 ? 1 : 0, rssBytes: 200e6 + i * 1e6, cpuPercent: 2 + (i % 5) * 0.4, rxBytes: 1e6 * i, txBytes: 5e5 * i, range}));
  return {
    doc, nodes, layout, failing: id => { failing = id; }, setPath: fn => { pathOf = fn; }, setReinstall: outcome => { reinstallOutcome = outcome; },
    async handle(method, path, body, query = '') {
      if (method === 'GET' && path === '') { step(); return [200, view()]; }
      if (method === 'GET' && path === '/path') {
        if (old || noPath) return [404, {ok: false, error: 'not_found'}];
        const q = new URLSearchParams(query), src = q.get('src'), dst = q.get('dst');
        if (!src || !dst || src === dst) return [400, {ok: false, error: 'invalid_mesh_path'}];
        if (![src, dst].every(id => applied.members.some(m => m.serverId === id))) return [404, {ok: false, error: 'mesh_member_not_found'}];
        const r = pathOf(src, dst);
        return r.status ? [r.status, {ok: false, error: r.error}] : [200, {ok: true, ...r}];
      }
      if (method === 'GET' && path === '/routes') {
        if (!routes || oldRoutes) return [404, {ok: false, error: 'not_found'}];
        const src = new URLSearchParams(query).get('src');
        if (!src) return [400, {ok: false, error: 'invalid_mesh_path'}];
        if (!applied.members.some(m => m.serverId === src)) return [404, {ok: false, error: 'mesh_member_not_found'}];
        return [200, {ok: true, ...routesOf(src)}];
      }
      if (method === 'PUT' && path === '/draft') {
        const errors = validate(body);
        if (errors.length) return [400, {ok: false, error: 'mesh_invalid', errors}];
        Object.assign(doc, JSON.parse(JSON.stringify(body)));
        doc.members.forEach((m, i) => { if (!m.address) m.address = `100.96.0.${10 + i}`; });
        return [200, view()];
      }
      if (method === 'PUT' && path === '/layout') {
        if (old) return [404, {ok: false, error: 'not_found'}];
        for (const [id, p] of Object.entries(body.positions)) { if (p) layout[id] = {x: p.x + 0.25, y: p.y}; else delete layout[id]; }
        return [200, {ok: true, layout: JSON.parse(JSON.stringify(layout))}];
      }
      if (method === 'POST' && path === '/members') {
        installing = body.serverIds.map(serverId => ({serverId, name: eligible().find(e => e.serverId === serverId)?.name, platform: 'darwin', startedAt: Date.now()}));
        await new Promise(resolve => setTimeout(resolve, 1500));
        installing = [];
        const items = body.serverIds.map(id => id === 'mac' ? {serverId: id, ok: true, error: '', reason: ''} : {serverId: id, ok: false, error: 'node_offline', reason: '节点离线'});
        for (const i of items.filter(x => x.ok)) {
          doc.members.push(member(i.serverId, '公司-Mac', 'member', {platform: 'darwin', address: '100.96.0.4', entry: {mode: 'fixed', autoReturn: true, candidates: []}}));
          nodes[i.serverId] = {state: 'installed', appliedRevision: 0, error: '', summary: {entry: {}, ready: false, peers: 0, rttMs: null}};
        }
        return [200, {...view(), items}];
      }
      // Reinstall: installs while the request is open (the node shows installing), then reissues the applied document.
      if (method === 'POST' && /^\/members\/[\w-]+\/reinstall$/.test(path)) {
        const id = path.split('/')[2];
        if (!applied.members.some(m => m.serverId === id)) return [404, {ok: false, error: 'mesh_member_not_found'}];
        if (reinstallOutcome) return [409, {ok: false, error: reinstallOutcome}];
        nodes[id] = {...nodes[id], state: 'installing', error: ''};
        await new Promise(resolve => setTimeout(resolve, 1500));
        nodes[id] = {...nodes[id], state: 'installed', error: ''};
        goneEvents.unshift({seq: 19, at: Date.now(), kind: 'mesh.reinstall', detail: ''});
        revision++;
        rollout = {running: true, revision, progress: applied.members.map(m => ({serverId: m.serverId, state: 'pending', error: ''}))};
        return [200, view()];
      }
      if (method === 'DELETE' && path.startsWith('/members/')) { doc.members.find(m => m.serverId === path.split('/').pop()).removing = true; return [200, view()]; }
      if (method === 'POST' && path === '/apply') {
        revision++;
        applied = JSON.parse(JSON.stringify(doc));
        rollout = {running: true, revision, progress: doc.members.map(m => ({serverId: m.serverId, state: 'pending', error: ''}))};
        return [200, view()];
      }
      if (method === 'POST' && path === '/discard') { Object.assign(doc, JSON.parse(JSON.stringify(applied))); return [200, view()]; }
      if (method === 'POST' && path === '/rollback') { revision++; rollout = {running: true, revision, progress: doc.members.map(m => ({serverId: m.serverId, state: 'pending', error: ''}))}; failing = ''; return [200, view()]; }
      if (method === 'GET' && /^\/members\/[\w-]+\/trend$/.test(path)) return old ? [404, {ok: false, error: 'not_found'}] : [200, {ok: true, range: '1h', points: trend('1h'), ...(lines ? {rtt: /\/(berlin|lax)\//.test(path) ? {kind: 'links', peers: [path.includes('/berlin/') ? 'lax' : 'berlin']} : {kind: 'entry', peers: ['lax']}} : {})}];
      if (method === 'GET' && /^\/lines\/[^/]+\/trend$/.test(path)) {
        if (!lines) return [404, {ok: false, error: 'not_found'}];
        const id = decodeURIComponent(path.split('/')[2]);
        return LINES.some(l => l.id === id) ? [200, lineTrend(id, new URLSearchParams(query).get('range'))] : [404, {ok: false, error: 'mesh_line_not_found'}];
      }
      if (method === 'GET' && path.startsWith('/members/')) {
        const id = path.split('/').pop();
        // Events as the controller sends them: repeats merged, a relay's route flapping, raw tunnel errors.
        if (noisy && noisyEvents[id]) return [200, {ok: true, member: doc.members.find(m => m.serverId === id), node: nodes[id], status: {ready: true, error: '', peers: [],
          ...(id === 'home' ? {entry: {candidate: 'c2', relay: 'lax', transport: 'tcp', candidates: [{id: 'c2', state: 'active'}, {id: 'c1', state: 'ready'}]}} : {})}, events: noisyEvents[id],
          config: {nodes: [{id: 'berlin', name: 'Berlin', publicKey: 'E0pTabcdefghijklmnopqrSN0k='}]}}];
        if (reach) return [200, {ok: true, member: doc.members.find(m => m.serverId === id), node: nodes[id], status: {ready: true, error: '', peers: [], ...status[id]},
          events: id === 'home' ? reachEvents : []}];
        return [200, {ok: true, member: doc.members.find(m => m.serverId === id), node: nodes[id],
          status: punch && id !== 'home' ? (live[id] ? {ready: true, error: '', peers: live[id]} : null) : id === 'home' ? {ready: true, error: '', entry: {candidate: 'c2', relay: 'lax', transport: 'tcp', switches: 2},
            ...(old ? {} : {resources: {cpuPercent: 3.4}, skippedRoutes: ['10.8.0.0/16'], hostFirewall: 'on'}),
            // A Linux member's host firewalls, and a zone the user chose for the mesh device.
            ...(firewall ? {hostFirewall: 'ufw,docker,firewalld', hostFirewallIssues: [{code: 'firewalld_zone', detail: 'work'}, {code: 'docker_failed', detail: 'iptables: No chain/target/match by that name.'}]} : {}),
            // End-to-end sessions all in use, and signatures a relay forwarded that did not hold.
            ...(secure ? {counters: {droppedPlain: 1}, e2e: {sessions: 64, limit: 64, innerMtu: 1348, droppedExhausted: 4, droppedQueue: 0, droppedSource: 0, droppedBad: 2},
              security: {signing: {verified: 120, rejected: 5, byHop: {berlin: 5}}, claims: {expired: 0}}} : {}),
            peers: [{id: 'lax', kind: 'entry', active: true, transport: 'tcp', endpoint: '198.51.100.3:52000', localAddr: '10.0.0.5:40112', lastHandshakeUnixNano: String((Date.now() - 20000) * 1e6), rxBytes: 5e6, txBytes: 2e6, rttMs: 152.3, reconnects: 2},
              {id: 'berlin', kind: 'entry', active: false, transport: 'udp', endpoint: '198.51.100.6:51999', lastHandshakeUnixNano: '0', rxBytes: 0, txBytes: 0, rttMs: null}, ...(live.home || [])]} : null,
          events: id === 'home' ? [...(gone === 'home' ? goneEvents : []), {seq: 17, at: Date.now() - 120000, kind: 'peer.reconnect', detail: 'berlin'}, {seq: 18, at: Date.now() - 60000, kind: 'path.failed', detail: 'c1: probe timeout'},
            ...(upgrades ? [{seq: 18, at: Date.now() - 30000, kind: 'upgrade.failed', detail: 'data plane upgrade did not get ready; rolled back'}] : []), ...punchEvents,
            ...(secure ? [{seq: 19, at: Date.now() - 300000, kind: 'sign.rejected', detail: 'offer in the name of lax from berlin: bad signature'},
              {seq: 20, at: Date.now() - 30000, kind: 'entry.suspect', detail: 'lax answers probes but 3 end-to-end probes through it to berlin in a row went unanswered; reported only (fixed mode)'}] : [])] : []}];
      }
      return [404, {ok: false, error: 'not_found'}];
    },
  };
}
