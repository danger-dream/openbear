// The fake controller of mesh.forms.browser.test.mjs (a mesh with gateway networks and tags),
// shared with mesh.forms.dom.test.mjs.
export const GB = 1024 ** 3;
export function server(id, name, {region = 'DE'} = {}) {
  const info = {hostname: id, os: 'Debian GNU/Linux 12', kernel: '6.1', arch: 'amd64', cpu: 'EPYC', cores: 2, virtualization: 'kvm', memTotal: 2 * GB, swapTotal: 0, diskTotal: 20 * GB, ipv4: `198.51.100.${id.length}`, ipv6: '', platform: 'linux'};
  const m = {cpu: 5, steal: 0, iowait: 0, mem: {total: 2 * GB, used: GB / 2}, swap: {total: 0, used: 0}, disk: {total: 20 * GB, used: 4 * GB}, load: [0.1, 0.1, 0.1],
    io: {readRate: 0, writeRate: 0}, net: {rx: 0, tx: 0, rxRate: 1000, txRate: 1000}, tcp: 10, udp: 2, processes: 90, uptime: 86400, boot: 1, nodeRss: 10e6};
  return {id, name, local: false, ssh: null, usedBy: [], region, install: null, access: null,
    live: {online: true, connectedAtMs: Date.now() - 3600000, version: '0.13.1', caps: {exec: true, terminal: true}, at: Date.now(), m, features: ['mesh']},
    profile: {group: '', region, sort: 0, billing: {price: 5, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
    node: {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now(), online: true, version: '0.13.1', latest: '0.13.1', updateAvailable: false, caps: {exec: true, terminal: true}, info, geo: null},
    traffic: null};
}
export const member = (serverId, name, role, extra = {}) => ({serverId, name, platform: 'linux', role, address: '', publicEndpoint: '', publicEndpointAuto: `198.51.100.${serverId.length}`,
  listen: {udp: role === 'relay' ? 51999 : 0, tcp: 0, tls: 0}, tags: [], gateway: null, uploadLimitMbps: null, entry: role === 'relay' ? null : {mode: 'fixed', candidates: [], autoReturn: true}, removing: false, ...extra});
export const VIEW_KEYS = ['name', 'platform', 'publicEndpointAuto', 'subnetSuggestions'];

// The controller's mesh API, as much of it as the settings forms use: a draft checked on PUT
// (errors with paths and messages), warnings for ports another program holds, and the LAN
// networks each member's machine is on (members[].subnetSuggestions).
export function fakeMesh() {
  const doc = {
    settings: {prefix: '100.96.0.0/16', controlPrefix: '100.96.255.0/24', mtu: 1400, ports: {udp: 51999, tcp: 52000, tls: 52001}, health: {}},
    members: [member('berlin', 'Berlin', 'relay', {address: '100.96.0.1', tags: ['公司', '欧洲'], listen: {udp: 51999, tcp: 52000, tls: 0}}),
      member('lax', 'LAX', 'relay', {address: '100.96.0.2', tags: ['美国'], gateway: {subnets: ['10.20.0.0/16'], masquerade: true}}),
      member('home', 'Home', 'member', {address: '100.96.0.3', tags: ['家'], listen: {udp: 51999, tcp: 52000, tls: 0}})],
    relayLinks: {mode: 'full', pairs: []}, directLinks: [], rules: [],
  };
  const suggestions = {home: ['192.168.50.0/24', '10.20.5.0/24'], lax: ['10.20.0.0/16'], berlin: []};
  const nodes = Object.fromEntries(doc.members.map(m => [m.serverId, {state: 'ready', appliedRevision: 3, error: '', summary: {entry: {}, ready: true, peers: 2, rttMs: null}}]));
  const warnings = [{path: 'members[2].listen.udp', error: 'mesh_port_in_use', message: 'Home 的 UDP 端口 51999 已被 dnsmasq（PID 812）占用，组网服务无法监听它。请换一个端口，或先停掉占用它的程序'}];
  const applied = JSON.parse(JSON.stringify(doc));
  function validate(d) {
    const errors = [];
    d.members.forEach((m, i) => {
      // Something only the controller knows, said in its own words.
      if (m.address === '100.96.0.99') errors.push({path: `members[${i}].address`, error: 'address_reserved', message: '这个地址给主控的网关留着，请换一个'});
      if (m.gateway && !m.gateway.subnets.length) errors.push({path: `members[${i}].gateway.subnets`, error: 'invalid_subnets', message: '网关至少要有一个网段，最多 32 个'});
    });
    return errors;
  }
  const view = () => ({ok: true, ...JSON.parse(JSON.stringify(doc)), members: doc.members.map(m => ({...JSON.parse(JSON.stringify(m)), subnetSuggestions: suggestions[m.serverId] || []})),
    applied: {revision: 3, at: Date.now()}, draft: {dirty: JSON.stringify(doc) !== JSON.stringify(applied), changes: []}, nodes, eligible: [],
    apply: {running: false, revision: 3, progress: []}, errors: [], warnings: doc.members[2].listen.udp === 51999 ? JSON.parse(JSON.stringify(warnings)) : [], layout: {berlin: {x: 0, y: 0}, lax: {x: 300, y: 0}, home: {x: 150, y: 220}}, installing: []});
  return {
    doc, warnings,
    handle(method, path, body) {
      if (method === 'GET' && path === '') return [200, view()];
      if (method === 'PUT' && path === '/draft') {
        const errors = validate(body);
        if (errors.length) return [400, {ok: false, error: 'mesh_invalid', errors}];
        // The controller drops what only the page shows (VIEW_KEYS in mesh_model.py).
        Object.assign(doc, JSON.parse(JSON.stringify(body)));
        doc.members.forEach(m => { for (const k of VIEW_KEYS) if (k !== 'name' && k !== 'platform' && k !== 'publicEndpointAuto') delete m[k]; });
        return [200, view()];
      }
      if (method === 'PUT' && path === '/layout') return [200, {ok: true, layout: {}}];
      if (method === 'GET' && path.startsWith('/members/') && !path.endsWith('/trend')) {
        const id = path.split('/').pop();
        return [200, {ok: true, member: doc.members.find(m => m.serverId === id), node: nodes[id], status: null, events: []}];
      }
      return [404, {ok: false, error: 'not_found'}];
    },
  };
}
