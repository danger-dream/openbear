// Display rules and small editing helpers for the mesh page; no requests here.
export const TRANSPORTS = [['udp', 'UDP'], ['tcp', 'TCP'], ['tls', 'TLS']];
export const transportLabel = value => Object.fromEntries(TRANSPORTS)[value] || String(value || '').toUpperCase();
export const PROTOCOLS = [['any', '任意协议'], ['tcp', 'TCP'], ['udp', 'UDP'], ['icmp', 'ICMP（ping）']];

const NODE_STATES = {installing: ['warn', '安装中'], installed: ['warn', '已安装，未就绪'], ready: ['ok', '就绪'], degraded: ['warn', '异常'],
  offline: ['bad', '离线'], failed: ['bad', '失败'], cleanup: ['off', '待清理'], missing: ['bad', '组网服务不见了']};
// The node says its mesh service is gone (uninstalled, or the node installed again over it): nothing goes to it until it is reinstalled.
export const MISSING_TEXT = '组网服务不见了（可能被卸载或重装过）';
// [tone, label] of a member's data plane as last reported.
export function nodeState(data, id) {
  const node = data?.nodes?.[id];
  return NODE_STATES[node?.state] || ['off', '未应用'];
}
// The controller upgrades each machine's mesh service after a node update; relays queue one at a time.
export const UPGRADES = {waiting: '排队升级', upgrading: '升级中'};
export const upgradeLabel = node => UPGRADES[node?.upgrade?.state] || '';
export const PROGRESS = {pending: ['off', '待下发'], applying: ['warn', '下发中'], done: ['ok', '已应用'], failed: ['bad', '失败'], offline: ['off', '离线，连上后补发'],
  missing: ['warn', '组网服务不见了，已跳过'], outdated: ['warn', '组网服务版本旧，已跳过，更新后补发']};
export const progressOf = (data, id) => (data?.apply?.progress || []).find(p => p.serverId === id) || null;
export const FINISHED = new Set(['done', 'failed', 'offline', 'missing', 'outdated']);

export const EDITABLE = {settings: () => ({}), members: () => [], relayLinks: () => ({mode: 'full', pairs: []}), directLinks: () => [], rules: () => []};
// The part of GET /mesh that PUT /mesh/draft takes back.
export function draftOf(state) {
  return JSON.parse(JSON.stringify(Object.fromEntries(Object.entries(EDITABLE).map(([key, empty]) => [key, state?.[key] ?? empty()]))));
}
export const uid = prefix => prefix + Math.random().toString(36).slice(2, 8);
export function move(list, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= list.length) return false;
  list.splice(target, 0, list.splice(index, 1)[0]);
  return true;
}

// Codes from the mesh endpoints, rollouts and node replies. A node's code may
// carry its reason after a colon ("invalid_config: self.address outside …").
const MESH_ERRORS = {
  mesh_access_move_failed: '迁出接入点未成功，已保留旧网络和成员，没有下发配置或卸载',
  mesh_change_requires_online_nodes: '重编号需要所有受影响节点在线并支持确认提交，请先连接或更新节点；旧网络未修改',
  mesh_change_recovering: '正在恢复旧网络，尚未收到全部节点的恢复确认，请等待节点重连',
  mesh_change_unconfirmed: '新网络未获全部节点确认，已撤回这次变更；未确认节点会按期限自主恢复旧配置',
  mesh_change_interrupted: '确认提交被中断，已撤回这次变更并恢复旧配置',
  mesh_change_pending: '节点正在等待组网变更确认，暂不能同时修改配置',
  mesh_change_not_prepared: '节点未就绪，未提交这次组网变更',
  mesh_apply_running: '正在应用，请稍后再试', mesh_no_previous_revision: '没有可回退的版本', mesh_member_not_found: '这台服务器不在组网里',
  mesh_invalid: '有设置需要修改', invalid_draft: '草稿格式不正确', invalid_mesh_members: '选择的服务器不正确：一次 1–50 台，不能重复',
  server_not_found: '服务器不存在或已被删除', mesh_install_invalid: '节点返回的组网密钥不正确，安装没有完成',
  mesh_key_in_use: '这台机器的组网密钥和另一台成员的相同，多半是克隆出来的：删掉它组网目录里的 keys 后再试',
  capability_disabled: '这台服务器没有开启执行命令', mesh_not_installed: '这台服务器上的组网组件没有安装',
  invalid_config: '节点拒绝了配置', apply_timeout: '节点在规定时间内没有完成应用', upgrade_not_ready: '组网服务升级后没有就绪，已回退',
  mesh_not_applied: '节点没有确认新配置', node_offline: '节点离线', node_connection_lost: '和节点的连接中断了',
  node_not_enrolled: '这台服务器还没有安装节点', node_update_required: '节点版本过旧，需要先更新',
  platform_unsupported: '这台服务器的系统不支持组网', unsupported_platform: '这台服务器的系统不支持组网',
  certificate_failed: '中转的 TLS 证书生成失败', service_failed: '组网服务没能启动',
  executable_unknown: '节点找不到自己的程序文件，没法安装组网组件', invalid_mesh_layout: '布局数据不正确', invalid_range: '时间范围不正确', invalid_params: '节点不接受这个请求的参数', node_error: '节点返回了错误',
  udp_not_listening: '两端都要开启 UDP 监听才能直连', punch_no_reflector: '没有可用来打洞的中转（需要至少一台开启 UDP 监听的中转）',
  mesh_site_relay_needs_public_link: '站点中转至少要连一台公网中转，用来交换打洞地址',
  mesh_entry_needs_bootstrap: '入口里有站点中转时，还要有一个公网中转入口：先经它连上，再和站点中转打洞',
  mesh_punch_impossible: '两端都是对称型 NAT，打洞基本不会成功，会一直经中转绕行',
  mesh_standby_unhealthy: '备用入口都连不上：在用的入口断开时，没有可以切换过去的入口',
  mesh_exit_inconsistent: '出口被代理改写：各观测点看到的出口地址不一致，这台机器的公网地址不可靠',
  punch_needs_udp: '站点中转只能经 UDP 打洞接入或连接，传输方式请选 UDP', invalid_relay_mode: '中转类型只能是公网中转或站点中转',
  invalid_bias: '偏好须是 -10000～10000 的整数（毫秒）', invalid_stun: 'STUN 服务器须是“主机:端口”，例如 stun.example.com:3478，最多 8 个',
  invalid_mesh_path: '路径的起点和终点要是两台不同的服务器',
  mesh_service_missing: MISSING_TEXT,
};
// POST /mesh/members/:id/reinstall: the same codes, said for putting the service back.
const REINSTALL_ERRORS = {mesh_member_not_found: '这台还不是已应用的网格成员，不能重新安装组网服务', node_offline: '节点离线，连上后才能重新安装',
  capability_disabled: '这台服务器的节点关闭了命令执行，没法远程安装组网服务', node_update_required: '节点版本过旧，先更新节点再重新安装',
  mesh_apply_running: '正在应用别的更改，等它完成后再重新安装',
  mesh_key_in_use: '这台机器的组网密钥和另一台成员的相同，多半是克隆出来的：删掉它组网目录里的 keys 后再重新安装'};
export const reinstallErrorText = code => REINSTALL_ERRORS[code] || '';
// Draft checks and alerts whose wording is ours, whatever the server's message says.
const OWN_WORDING = new Set(['udp_not_listening', 'punch_no_reflector', 'mesh_site_relay_needs_public_link', 'mesh_entry_needs_bootstrap', 'mesh_punch_impossible',
  'mesh_standby_unhealthy', 'mesh_exit_inconsistent', 'punch_needs_udp', 'invalid_relay_mode', 'invalid_bias', 'invalid_stun']);
// udp_not_listening also covers links between site relays and entries punched to one; the path tells which.
const UDP_BY_PLACE = [[/^relayLinks\./, '两台站点中转都要开启 UDP 监听才能打洞连接'], [/\.entry\./, '要开启 UDP 监听才能经打洞接入站点中转']];
// An entry that cannot be punched is not "relayed around": it just goes unused.
const PUNCH_BY_PLACE = [[/\.entry\./, '两端都是对称型 NAT，打洞基本不会成功，这个入口很可能用不上']];
export const codeText = code => MESH_ERRORS[code] || '';
// Phrases the data plane reports as its error (no code); the rest after a colon is kept.
const DATA_PLANE = [
  [/^no ready entry$/i, () => '没有可用的入口'],
  [/^ownership conflict requires higher sequence$/i, () => '入口归属有冲突，要等更新的归属序号才能接管'],
  [/^ownership propagation pending: (\d+) acknowledgements?$/i, n => `入口归属正在同步，还在等 ${n} 台确认`],
  [/^route update failed$/i, () => '路由更新失败'],
  [/^entry state not saved$/i, () => '入口状态没有保存下来'],
  [/^entry certificate rejected$/i, () => '入口的证书被拒绝'],
];
export function dataPlaneText(raw) {
  const value = String(raw || '').trim();
  for (const [pattern, text] of DATA_PLANE) {
    const whole = pattern.exec(value);
    if (whole) return text(whole[1]);
    const head = new RegExp(pattern.source.replace(/\$$/, ':\\s*(.+)$'), 'i').exec(value);
    if (head) return `${text(head[1])}：${head.slice(-1)[0]}`;
  }
  return '';
}
// The Chinese text for a known code, with the node's reason appended; '' when unknown.
export function meshCodeText(raw) {
  const plane = dataPlaneText(raw);
  if (plane) return plane;
  const value = String(raw || '').trim(), split = value.indexOf(':');
  const code = split > 0 ? value.slice(0, split).trim() : value, detail = split > 0 ? value.slice(split + 1).trim() : '';
  const text = MESH_ERRORS[code];
  return text ? (detail ? `${text}：${detail}` : text) : '';
}

// PUT /mesh/draft rejects with errors [{path: 'members[2].entry.candidates[0]', message}].
export function errorMessage(e) {
  if (e?.error === 'udp_not_listening') { const hit = UDP_BY_PLACE.find(([re]) => re.test(e.path || '')); if (hit) return hit[1]; }
  if (e?.error === 'mesh_punch_impossible') { const hit = PUNCH_BY_PLACE.find(([re]) => re.test(e.path || '')); if (hit) return hit[1]; }
  return (OWN_WORDING.has(e?.error) && MESH_ERRORS[e.error]) || e?.message || e?.text || e?.error || '这里的设置不正确';
}
export const errorsAt = (errors, path) => (errors || []).filter(e => e.path === path).map(errorMessage);
export const errorsUnder = (errors, prefix) => (errors || []).filter(e => e.path === prefix || e.path?.startsWith(prefix + '.') || e.path?.startsWith(prefix + '['));
// GET /mesh warnings: shaped like errors, but they block neither saving nor applying (mesh_punch_impossible).
// A punch warning is a prediction: once the line is through, or both ends leave by the same
// public address (one NAT, so they meet on the LAN), it no longer holds and is left out.
export const warningItems = (data, prefix) => errorsUnder(Array.isArray(data?.warnings) ? data.warnings : [], prefix)
  .filter(w => !(w.error === 'mesh_punch_impossible' && punchMoot(data, w.path))).map(w => ({code: w.error || '', text: errorMessage(w)}));
export const warningsUnder = (data, prefix) => warningItems(data, prefix).map(w => w.text);
const hostOf = addr => { const s = String(addr || '').trim(); return s.startsWith('[') ? s.slice(1, s.indexOf(']')) : s.replace(/:\d+$/, ''); };
// Both ends seen at one public address: behind the same NAT.
export function sameExit(data, a, b) {
  const x = hostOf(reachOf(data, a)?.publicAddr), y = hostOf(reachOf(data, b)?.publicAddr);
  return Boolean(x) && x === y;
}
const directUp = (data, a, b) => [[a, b], [b, a]].some(([x, y]) => data?.nodes?.[x]?.summary?.direct?.[y]?.state === 'direct');
export function punchMoot(data, path) {
  const link = /^directLinks\[(\d+)\]/.exec(path || '') || /^relayLinks\.pairs\[(\d+)\]/.exec(path || '');
  if (link) {
    const l = (path.startsWith('direct') ? data?.directLinks : data?.relayLinks?.pairs)?.[Number(link[1])];
    return Boolean(l) && (directUp(data, l.a, l.b) || sameExit(data, l.a, l.b));
  }
  const entry = /^members\[(\d+)\]\.entry\.candidates\[(\d+)\]/.exec(path || '');
  const m = entry && data?.members?.[Number(entry[1])], c = m?.entry?.candidates?.[Number(entry[2])];
  if (!c) return false;
  const h = entryHealth(data, m.serverId)[c.id];
  return Boolean(h && (['active', 'ready'].includes(h.state) || h.via)) || sameExit(data, m.serverId, c.relay);
}

export const isRelay = m => m?.role === 'relay';
// A relay behind NAT (relayMode 'site') announces no address: it dials the public
// relays and punches to other site relays and to its members. Relays without the field are public.
export const isSite = m => isRelay(m) && m?.relayMode === 'site';
export const RELAY_MODES = [['public', '公网中转', '有别人能直接连到的地址，成员和其他中转直接连它'],
  ['site', '站点中转', '在 NAT 后，不对外宣告地址：由它拨号连公网中转，和其他站点中转、本地成员之间打洞']];
// What the reachability check suggests; '' before there is a verdict.
export const recommendedMode = reach => reach?.verdict ? (reach.verdict === 'public' ? 'public' : 'site') : '';
export const listens = (m, transport) => Number(m?.listen?.[transport]) > 0;
export const publicAddress = m => m?.publicEndpoint || m?.publicEndpointAuto || '';
export const nameOf = (members, id) => members?.find(m => m.serverId === id)?.name || id || '';

// The member list's second line: where the member enters the mesh, and whether it is on a backup.
export function entryLine(member, members, summary) {
  const first = member.entry?.candidates?.[0];
  if (!first) return {text: '未设置入口', tone: 'warn'};
  const now = summary?.entry;
  const candidate = now?.candidate && member.entry.candidates.find(c => c.id === now.candidate);
  if (now?.candidate && now.candidate !== first.id) {
    const relay = candidate?.relay || now.relay, transport = now.transport || candidate?.transport;
    return {text: `当前 ${nameOf(members, relay)} · ${transportLabel(transport)}（备用）`, tone: 'warn'};
  }
  return {text: `入口 ${nameOf(members, first.relay)} · ${transportLabel(first.transport)}`, tone: ''};
}
export const rtt = summary => Number.isFinite(summary?.rttMs) ? msText(summary.rttMs) : '';
// Milliseconds with one decimal below 10 (2.5 ms), whole above.
export const msText = v => Number.isFinite(v) ? `${v < 10 ? Math.round(v * 10) / 10 : Math.round(v)} ms` : '';

// Reachability (summary.reachability; status.reachability adds mapping and observers).
export const VERDICTS = {public: ['ok', '公网可达'], punchable: ['info', '可打洞'], symmetric: ['warn', '对称型 NAT'], no_udp: ['bad', 'UDP 不通'], unknown: ['off', '未知']};
export const VERDICT_NOTES = {public: '别的节点可以直接连到它', punchable: '在 NAT 后，可以和别的节点打洞直连', symmetric: '发往不同目标时出口端口不同，和外面的机器很难打洞，会经中转绕行；同一局域网里的机器不受影响',
  no_udp: '没有任何观测点回应 UDP：打洞和 UDP 入口都用不了，只能用 TCP/TLS 入口', unknown: '还没有测出结论'};
export const MAPPINGS = {stable: '端口固定', varies: '端口随目标变化', unknown: '未知'};
export const FILTERINGS = {open: '不过滤，谁都能连进来', address: '只放行连过的地址', port: '只放行连过的地址和端口', unknown: '没法测'};
export function reachOf(data, id, status = null) {
  const r = status?.reachability || data?.nodes?.[id]?.summary?.reachability;
  return r && typeof r === 'object' && !Array.isArray(r) ? r : null;
}
// [tone, label] for the node's badge; null without a verdict (older controllers).
export function verdictBadge(reach) {
  if (!reach) return null;
  return VERDICTS[reach.verdict] || VERDICTS.unknown;
}

// Entry candidates as the member reports them: status.entry.candidates, or summary.entryHealth.
// untested: a cold standby, not kept connected and tried now and then (health.coldRetryMs).
export const CAND_STATES = {active: ['ok', '在用'], ready: ['ok', '热备可用'], failing: ['bad', '连不上'], punching: ['info', '打洞中'], untested: ['off', '冷备']};
const CAND_ERRORS = {timeout: '超时', refused: '连接被拒绝', 'tls pin mismatch': 'TLS 证书指纹不符', 'no handshake': '没有握手', unreachable: '网络不可达', 'punch timeout': '打洞超时'};
export const candErrorText = raw => CAND_ERRORS[String(raw || '').trim().toLowerCase()] || String(raw || '').trim();
// {candidateId: {state, since, lastOkAt, lastError}}; {} when nothing is reported.
export function entryHealth(data, id, status = null) {
  const raw = status?.entry?.candidates || data?.nodes?.[id]?.summary?.entryHealth;
  const list = Array.isArray(raw) ? raw : Array.isArray(raw?.candidates) ? raw.candidates : raw && typeof raw === 'object' ? Object.entries(raw).map(([cid, v]) => ({id: cid, ...v})) : [];
  return Object.fromEntries(list.filter(c => c?.id && CAND_STATES[c.state]).map(c => [c.id, c]));
}
// One line for a candidate's health: 连不上：超时 · 上次成功 5 分钟前.
export function candLine(h, now = Date.now()) {
  if (!h) return '';
  const parts = [CAND_STATES[h.state][1] + (h.state === 'failing' && h.lastError ? `：${candErrorText(h.lastError)}` : '')];
  if (h.state === 'failing') parts.push(h.lastOkAt ? `上次成功 ${durationText(now - h.lastOkAt)}前` : '从没连上过');
  else if (h.state === 'untested') parts.push(h.lastOkAt ? `定时试连，上次成功 ${durationText(now - h.lastOkAt)}前` : '定时试连');
  else if (h.since) parts.push(`已 ${durationText(now - h.since)}`);
  return parts.join(' · ');
}
// The same line as a state and the rest, for a tag and a note: {label: '连不上', detail: '超时 · 从没连上过'}.
export function candParts(h, now = Date.now()) {
  if (!h) return {label: '', detail: ''};
  const [head, ...rest] = candLine(h, now).split(' · ');
  const split = head.indexOf('：');
  return split < 0 ? {label: head, detail: rest.join(' · ')} : {label: head.slice(0, split), detail: [head.slice(split + 1), ...rest].join(' · ')};
}
// The candidates a member would switch to, when every one of them is failing: [{c, h}].
export function deadStandbys(member, health) {
  const list = member?.entry?.candidates || [];
  if (member?.entry?.mode !== 'failover' || list.length < 2) return [];
  const active = list.find(c => health[c.id]?.state === 'active') || list[0];
  const standbys = list.filter(c => c !== active);
  return standbys.every(c => health[c.id]?.state === 'failing') ? standbys.map(c => ({c, h: health[c.id]})) : [];
}
export const standbyText = (dead, members) => dead.map(({c, h}) => `${nameOf(members, c.relay)} 备用：${candErrorText(h.lastError) || '连不上'}`).join('；');
// Alerts on a node: what the controller reports (nodes[id].alerts, strings or {code}) plus
// what the board can tell itself. [{code, text}], the worst first.
export function nodeAlerts(data, member, members, status = null) {
  const id = member?.serverId, node = data?.nodes?.[id], out = new Map();
  for (const a of [...(Array.isArray(node?.alerts) ? node.alerts : []), ...(Array.isArray(node?.summary?.alerts) ? node.summary.alerts : [])]) {
    const code = typeof a === 'string' ? a : a?.code || a?.error || a?.kind;
    if (code) out.set(code, {code, text: MESH_ERRORS[code] || (typeof a === 'object' && (a.message || a.detail)) || code});
  }
  const dead = deadStandbys(member, entryHealth(data, id, status));
  if (dead.length) out.set('mesh_standby_unhealthy', {code: 'mesh_standby_unhealthy', text: standbyText(dead, members)});
  else if (out.has('mesh_standby_unhealthy') && !Object.keys(entryHealth(data, id, status)).length) out.get('mesh_standby_unhealthy').text = MESH_ERRORS.mesh_standby_unhealthy;
  if (reachOf(data, id, status)?.consistent === false && !out.has('mesh_exit_inconsistent')) out.set('mesh_exit_inconsistent', {code: 'mesh_exit_inconsistent', text: MESH_ERRORS.mesh_exit_inconsistent});
  const rank = code => ({mesh_standby_unhealthy: 0, mesh_exit_inconsistent: 1}[code] ?? 2);
  return [...out.values()].sort((a, b) => rank(a.code) - rank(b.code));
}
export const ALERT_BADGES = {mesh_standby_unhealthy: ['bad', '备用入口不可用'], mesh_exit_inconsistent: ['warn', '出口被代理改写']};

// Links between relays as each end measures them (summary.routing.links).
export const LINK_STATES = {up: ['ok', '正常'], down: ['bad', '断开'], suppressed: ['warn', '线路不稳，暂停使用']};
const LINK_RANK = {down: 0, suppressed: 1, up: 2};
// The worse of the two ends' reports; null when neither reports.
export function relayMetric(data, a, b) {
  const report = (self, other) => {
    const links = data?.nodes?.[self]?.summary?.routing?.links;
    const l = Array.isArray(links) ? links.find(x => x?.peer === other && LINK_STATES[x.state]) : null;
    return l ? {...l, from: self} : null;
  };
  const ends = [report(a, b), report(b, a)].filter(Boolean);
  if (!ends.length) return null;
  const cost = e => (e.state === 'up' && Number.isFinite(e.cost) ? e.cost : Infinity);
  const worst = [...ends].sort((x, y) => (LINK_RANK[x.state] - LINK_RANK[y.state]) || (cost(y) - cost(x)))[0];
  return {...worst, ends, differ: ends.length > 1 && (ends[0].state !== ends[1].state)};
}
// Which links between relays carry traffic, by the routes the relays report (summary.routing.routes):
// a link is in use when either end sends something to the other end through it (nextHop).
// {reporting: the nodes that report routes, used: 'a|b' pairs, sorted}.
export function routedLinks(data) {
  const reporting = new Set(), used = new Set();
  for (const [id, node] of Object.entries(data?.nodes || {})) {
    const routes = node?.summary?.routing?.routes;
    if (!Array.isArray(routes) || !routes.length) continue;
    reporting.add(id);
    for (const r of routes) if (r?.nextHop && r.nextHop !== id) used.add([id, r.nextHop].sort().join('|'));
  }
  return {reporting, used};
}
// Whether a link between relays is in use: true, false, or null when nothing says. By the routes when
// either end reports them, else by the line's state (GET /mesh lines[]).
export function relayInUse(routed, line, a, b) {
  if (routed.reporting.has(a) || routed.reporting.has(b)) return routed.used.has([a, b].sort().join('|'));
  if (!line) return null;
  if (line.state === 'up') return line.role ? line.role === 'active' : true;
  return ['standby', 'cold', 'unused'].includes(line.state) ? false : null;
}
export const lossText = v => Number.isFinite(v) ? `${Math.round(v * 1000) / 10}%` : '';
export const costText = m => m.state !== 'up' || !Number.isFinite(m.cost) ? '不可用' : String(Math.round(m.cost * 10) / 10);

// GET /mesh/path hops: how each hop is reached from the one before.
export const HOP_VIA = {direct: '直连', entry: '入口', relay: '中转链路', lan: '局域网'};
// Hops as GET /mesh/path and /mesh/routes give them ({node, via, rttMs}, or plain ids): [{node, via, rttMs}].
export function hopsOf(raw) {
  return (Array.isArray(raw) ? raw : []).map(h => typeof h === 'string' ? {node: h, via: '', rttMs: null} : h?.node ? {node: h.node, via: h.via || '', rttMs: Number.isFinite(h.rttMs) ? h.rttMs : null} : null).filter(Boolean);
}

// An entry hop between a member and the relay it is entering by: the member's own report says
// whether that went over the LAN, and measures it when the relay's side says 0 (not measured).
// {id, via, rttMs, member, relay} of the candidate in use; null when neither end says.
export function entrySegment(data, a, b) {
  for (const [m, relay] of [[a, b], [b, a]]) {
    const h = Object.values(entryHealth(data, m)).find(x => x.state === 'active' && x.relay === relay);
    if (h) return {id: h.id, via: h.via || '', member: m, relay, rttMs: Number.isFinite(data?.nodes?.[m]?.summary?.rttMs) && data.nodes[m].summary.rttMs > 0 ? data.nodes[m].summary.rttMs : null};
  }
  return null;
}
// GET /mesh/routes?src=: where the node's traffic actually goes, one row per destination.
// {key, dst, subnet, gateway, hops, title, through, rtt, stale, broken}; the members' rows in
// board order, then the gateway networks. null when the controller gives no list.
export function routeRows(reply, src, members, data = null) {
  if (!reply || typeof reply !== 'object' || !Array.isArray(reply.routes)) return null;
  const order = id => { const i = members.findIndex(m => m.serverId === id); return i < 0 ? members.length : i; };
  const entryOf = (a, b) => entrySegment(data, a, b);
  const row = (r, end, extra) => {
    const hops = hopsOf(r.hops), broken = !hops.length || hops.at(-1).node !== end, stale = Boolean(r.stale);
    // The machines passed on the way: every hop after the source, the destination itself left out.
    const passed = hops.slice(1).filter(h => h.node !== extra.dst).map(h => nameOf(members, h.node));
    const first = hops[1], entry = first?.via === 'entry' ? entryOf(hops[0].node, first.node) : null;
    const how = first?.via === 'lan' || entry?.via === 'lan' ? ' · 局域网' : first?.via === 'direct' ? ' · 直连' : '';
    const through = broken ? (hops.length > 1 ? `在 ${nameOf(members, hops.at(-1).node)} 之后断开` : '现在到不了')
      : passed.length ? `经 ${passed.join(' → ')}` : `直达${how}`;
    // A segment of exactly 0 ms is one the reporting side did not measure.
    let total = Number.isFinite(r.rttMs) ? r.rttMs : null;
    if (total !== null && hops.slice(1).some(h => h.rttMs === 0)) {
      const fill = hops.slice(1).map((h, i) => h.rttMs === 0 && h.via === 'entry' ? entryOf(hops[i].node, h.node)?.rttMs ?? null : h.rttMs);
      total = fill.every(v => v !== null) ? fill.reduce((x, v) => x + v, 0) : null;
    }
    return {...extra, hops, broken, stale, through, rttMs: total, rtt: broken ? '' : total !== null ? msText(total) : '延迟未知'};
  };
  const routes = reply.routes.filter(r => r?.dst && r.dst !== src).map(r => row(r, r.dst, {key: `dst:${r.dst}`, dst: r.dst, title: nameOf(members, r.dst)}))
    .sort((a, b) => order(a.dst) - order(b.dst));
  const subnets = (Array.isArray(reply.subnets) ? reply.subnets : []).filter(s => s?.subnet && s.gateway)
    .map(s => row(s, s.gateway, {key: `net:${s.subnet}`, dst: '', subnet: s.subnet, gateway: s.gateway, title: s.subnet}));
  return {routes, subnets};
}

// GET /mesh exports: who gets each gateway network (by the draft's rules). null without
// the field (an older controller); [] when nobody may reach it.
export function exportsOf(data, gateway, subnet) {
  if (!Array.isArray(data?.exports)) return null;
  const hit = data.exports.find(x => x?.gateway === gateway && x.subnet === subnet);
  return Array.isArray(hit?.nodes) ? hit.nodes.filter(id => id && id !== gateway) : [];
}
export const exportText = (ids, members) => ids.length ? `下发给：${ids.map(id => nameOf(members, id)).join('、')}` : '没有机器能访问（要在访问规则的目标里写上这个网段）';

// UDP direct links: both ends send to each other at once, over the LAN, the configured and the
// reported addresses; each end reports status.peers[].direct = {state, via, since, attempts,
// lastError} for the other end. Only two ends that are both behind NAT really punch.
export const isPunched = link => (link?.transport || 'udp') === 'udp';
export const DIRECT_STATES = {direct: '直连中', punching: '正在打洞', relayed: '走中转'};
// false: one end is reachable from outside, or both are behind one NAT; true: both are behind
// NAT; null: not known yet (the wording stays that of punching).
export function needsPunch(data, a, b) {
  if (sameExit(data, a, b)) return false;
  const verdicts = [a, b].map(id => reachOf(data, id)?.verdict);
  if (verdicts.includes('public')) return false;
  return verdicts.every(v => v && v !== 'unknown') ? true : null;
}
export const directStateText = (state, punch = true) => state === 'punching' && punch === false ? '正在连接' : DIRECT_STATES[state];
const DIRECT_RANK = {relayed: 0, punching: 1, direct: 2};
export const DIRECT_VIA = {lan: '局域网', public: '公网', configured: '对外地址', routed: '经网关转发的内网地址'};
const VIA_SHORT = {lan: '局域网', public: '公网', configured: '对外地址'};
const DIRECT_ERRORS = {'no reflector': '没有可用来打洞的中转', 'punch timeout': '打洞超时', 'direct path lost': '直连线路中断'};
export const directErrorText = raw => DIRECT_ERRORS[String(raw || '').trim().toLowerCase()] || String(raw || '').trim();
export function durationText(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时${Math.floor(s % 3600 / 60) ? ` ${Math.floor(s % 3600 / 60)} 分` : ''}`;
  return `${Math.floor(s / 86400)} 天`;
}
// The link as the worse of its two ends reports it; null before either reports.
export function directLive(peersA, peersB, a, b) {
  const report = (peers, self, other) => {
    const p = (peers || []).find(x => x.id === other && x.direct && DIRECT_STATES[x.direct.state]);
    return p ? {...p.direct, from: self, rttMs: Number.isFinite(p.rttMs) ? p.rttMs : null} : null;
  };
  const ends = [report(peersA, a, b), report(peersB, b, a)].filter(Boolean);
  if (!ends.length) return null;
  const worst = [...ends].sort((x, y) => DIRECT_RANK[x.state] - DIRECT_RANK[y.state])[0];
  const rtts = ends.map(e => e.rttMs).filter(v => v !== null);
  return {...worst, rttMs: rtts.length ? Math.max(...rtts) : null, ends, differ: ends.length > 1 && ends[0].state !== ends[1].state};
}
// Why the link is not direct (or how), in a few words; punch as needsPunch says.
export function directReason(live, punch = true) {
  if (!live) return '';
  if (live.state === 'direct') return `直连中，经由${DIRECT_VIA[live.via] || '直接线路'}`;
  if (live.state === 'punching') return `${directStateText('punching', punch)}${live.attempts ? `，第 ${live.attempts} 次尝试` : ''}`;
  const why = String(live.lastError || '').trim().toLowerCase();
  if (why === 'no reflector') return '没有可用来打洞的中转，暂时走中转';
  if (why === 'direct path lost') return `直连线路中断，先走中转，会重新${punch === false ? '连接' : '打洞'}`;
  if (why === 'punch timeout') return punch === false ? '没有连上，会定时重试' : '打洞未成功，会定时重试';
  return why ? `${directErrorText(live.lastError)}，先走中转，会定时重试` : `暂时没${punch === false ? '连上' : '打通'}，先走中转，会定时重试`;
}
// The link's label on the board; `long` adds the reason (when selected).
export function directLabel(live, long = false, punch = true) {
  if (live.state === 'direct') return ['直连', VIA_SHORT[live.via], rtt(live)].filter(Boolean).join(' · ');
  const short = live.state === 'punching' ? (punch === false ? '连接中' : '打洞中') : '走中转';
  return long ? `${short} · ${directReason(live, punch).replace(/^正在(打洞|连接)，?/, '')}`.replace(/ · $/, '') : short;
}
// One line for a peer in the member's peer list.
export function directLine(d, now = Date.now()) {
  if (!d || !DIRECT_STATES[d.state]) return '';
  const parts = [DIRECT_STATES[d.state]];
  if (d.state === 'direct' && DIRECT_VIA[d.via]) parts.push(`经由${DIRECT_VIA[d.via]}`);
  if (d.state === 'punching' && d.attempts) parts.push(`第 ${d.attempts} 次尝试`);
  if (d.since) parts.push(`已 ${durationText(now - d.since)}`);
  if (d.lastError && d.state !== 'direct') parts.push(`最近错误：${directErrorText(d.lastError)}`);
  return parts.join(' · ');
}

// Rules read as one sentence: 允许 [来源] 访问 [目标] 的 [协议/端口].
// all: every member, those joining later included; it stands instead of members and tags,
// networks still add to it. A side with nothing at all covers nobody.
export const ALL_MEMBERS = '所有成员';
export const ALL_GATEWAY_NETS = '所有网关网段';
export const sideEmpty = side => !side?.all && !side?.allSubnets && !['members', 'tags', 'prefixes', 'subnets'].some(k => side?.[k]?.length);
export function sideParts(side, members, nets = 'prefixes') {
  // allSubnets (every gateway network, those added later included) adds to the rest, right after all members.
  const every = side?.allSubnets ? [{kind: 'allSubnets', value: 'allSubnets', text: ALL_GATEWAY_NETS}] : [];
  if (side?.all) return [{kind: 'all', value: 'all', text: ALL_MEMBERS}, ...every, ...(side[nets] || []).map(net => ({kind: nets, value: net, text: net}))];
  return [...every, ...(side?.members || []).map(id => ({kind: 'members', value: id, text: nameOf(members, id)})),
    ...(side?.tags || []).map(tag => ({kind: 'tags', value: tag, text: `标签 ${tag}`})),
    ...(side?.[nets] || []).map(net => ({kind: nets, value: net, text: net}))];
}
export function sideText(side, members, nets) {
  const list = sideParts(side, members, nets), parts = list.map(p => p.text);
  if (!parts.length) return '（未选择）';
  return parts.length > 3 ? `${parts.slice(0, 2).join('、')} 等 ${parts.length} ${list.every(p => p.kind === 'members') ? '台' : '项'}` : parts.join('、');
}
// Both sides name the same machines, tags and networks: they may all reach each other.
const sorted = list => [...(list || [])].sort().join('|');
// All members on both sides reads as 所有成员 → 所有成员, not as a list reaching each other.
export const mutualRule = rule => !rule?.src?.all && !rule?.dst?.all && Boolean(rule?.src?.allSubnets) === Boolean(rule?.dst?.allSubnets) && ['members', 'tags'].every(k => sorted(rule?.src?.[k]) === sorted(rule?.dst?.[k])) && sorted(rule?.src?.prefixes) === sorted(rule?.dst?.subnets)
  && Boolean(rule?.src?.allSubnets || rule?.src?.members?.length || rule?.src?.tags?.length || rule?.src?.prefixes?.length);
// The rule as one sentence: [lead, source, verb, target, tail]; the board and lists bold the names.
export function ruleWords(rule, members) {
  if (mutualRule(rule)) return ['允许', sideText(rule.src, members, 'prefixes'), '之间互相访问', '', protoText(rule)];
  return ['允许', sideText(rule.src, members, 'prefixes'), '访问', sideText(rule.dst, members, 'subnets'), protoText(rule)];
}
export const ruleSentence = (rule, members) => { const [lead, src, verb, dst, what] = ruleWords(rule, members); return dst ? `${lead} ${src} ${verb} ${dst} 的 ${what}` : `${lead} ${src} ${verb} ${what}`; };
export const ruleShort = (rule, members) => mutualRule(rule) ? `${sideText(rule.src, members, 'prefixes')} 互通 · ${rule.proto === 'icmp' ? 'ping' : protoText(rule).replace('所有端口', '全部')}`
  : `${sideText(rule.src, members, 'prefixes')} → ${sideText(rule.dst, members, 'subnets')} · ${rule.proto === 'icmp' ? 'ping' : protoText(rule).replace('所有端口', '全部')}`;
// The automatic rule for an instance on a mesh address, in the words of the user's rules (ruleWords).
export const autoRuleWords = (a, members) => ['允许', nameOf(members, a.gateway), '访问', nameOf(members, a.serverId), `TCP ${a.port}`];

// A Linux node's host firewalls (status.hostFirewall), and what they may not let through.
const FIREWALL_KINDS = {ufw: 'ufw', iptables: 'iptables', docker: 'Docker', firewalld: 'firewalld', nftables: 'nftables'};
export const firewallKinds = value => value && value !== 'on' ? value.split(',').map(k => FIREWALL_KINDS[k]).filter(Boolean).join(' · ') : '';
const SKIPPED = '即使组网规则允许，这些连接也进不来。';
export function firewallIssue(issue) {
  const d = issue?.detail || '';
  switch (issue?.code) {
  case 'firewalld_zone': return {text: `组网网卡在 firewalld 的 ${d} 区域`, detail: `obmesh0 是在这台机器上手动指定到 ${d} 区域的，组网没有改动它；这个区域不放行的连接，${SKIPPED}需要时把它移到 trusted 区域，或在 ${d} 区域里放行。`};
  case 'firewalld_failed': return {text: '没能调整 firewalld', detail: `组网的连接可能被 firewalld 拦下：${d}`};
  case 'nftables_failed': return {text: '没能在 nftables 里放行组网', detail: `${d}。这条链拦截的连接，${SKIPPED}`};
  case 'docker_failed': return {text: '没能放行发往容器的组网连接', detail: `DOCKER-USER 里拦截的连接，${SKIPPED}${d}`};
  case 'iptables_legacy': return {text: 'iptables 新旧两套规则并存', detail: `这台的 iptables 用 nf_tables，但旧版（legacy）表里还有规则，组网没有处理那一套；其中拦截的连接，${SKIPPED}`};
  case 'firewall_tool_missing': {
    // "firewalld: firewall-cmd": a firewall that shows signs of running, its program nowhere to be found.
    const [kind, tool] = d.split(':').map(x => x.trim());
    const name = FIREWALL_KINDS[kind] || kind;
    return {text: `找不到 ${tool || '防火墙程序'}，没能调整 ${name}`,
      detail: `这台机器上的 ${name} 看起来在用，但组网服务在 PATH 和 /usr/sbin、/sbin 等目录里都找不到 ${tool}，没有为组网放行。${SKIPPED}装好 ${tool} 或确认它的位置后，重启组网服务即可。`};
  }
  case 'iptables_nft': return {text: 'iptables 新旧两套规则并存', detail: `这台的 iptables 是旧版（legacy），但 nftables 里还有 iptables-nft 的规则（${d}），组网没有处理那一套；其中拦截的连接，${SKIPPED}`};
  default: return {text: `主机防火墙：${issue?.code || '未知问题'}`, detail: d};
  }
}
export const portsText = ports => (ports || []).map(([a, b]) => a === b ? String(a) : `${a}-${b}`).join(', ');
export function protoText(rule) {
  if (rule.proto === 'icmp') return 'ICMP（ping）';
  if (!rule.proto || rule.proto === 'any') return '所有端口';
  return `${rule.proto.toUpperCase()} ${rule.ports?.length ? portsText(rule.ports) : '所有端口'}`;
}
// "22, 8000-8100" → [[22, 22], [8000, 8100]]; empty means every port.
export function parsePorts(text) {
  const ports = [];
  for (const part of String(text || '').split(/[\s,，、]+/).filter(Boolean)) {
    const match = /^(\d{1,5})(?:-(\d{1,5}))?$/.exec(part);
    const a = Number(match?.[1]), b = Number(match?.[2] || match?.[1]);
    if (!match || a < 1 || b > 65535 || a > b) return {ports: null, error: `「${part}」不是有效的端口或端口范围（1–65535，例如 22 或 8000-8100）`};
    ports.push([a, b]);
  }
  return {ports, error: ''};
}

export const HEALTH = [
  ['activeIntervalMs', '在用入口的探测间隔', '毫秒', 1000], ['standbyIntervalMs', '热备入口的探测间隔', '毫秒', 2000], ['coldRetryMs', '其余候选的探测间隔', '毫秒', 30000],
  ['initialTimeoutMs', '起始探测超时', '毫秒', 3000], ['minTimeoutMs', '最短探测超时', '毫秒', 2000], ['maxTimeoutMs', '最长探测超时', '毫秒', 10000],
  ['consecutiveFailures', '连续超时几次算故障', '次', 3], ['readySuccesses', '连续成功几次算可用', '次', 3], ['handshakeGraceMs', '握手宽限', '毫秒', 6000],
  ['returnHealthyMs', '优先入口健康多久后切回', '毫秒', 60000], ['minResidenceMs', '两次切换的最短间隔', '毫秒', 60000],
  ['leaseMs', '归属租约', '毫秒', 30000], ['leaseRefreshMs', '租约刷新间隔', '毫秒', 5000]];

// ---- events: what the data plane (node/mesh) and the controller record, read in Chinese ----
// Names are servers' names; ids, keys and sequence numbers stay out of the text. A detail that
// cannot be read whole keeps its original (raw), shown on demand beside the Chinese summary.
const EVENTS = {'entry.switch': '切换入口', 'entry.return': '切回入口', 'entry.prepare': '准备切换入口', 'entry.committed': '入口切换完成', 'entry.flapping': '入口频繁切换',
  'entry.unavailable': '没有可用入口', 'peer.reconnect': '重连', 'peer.up': '连上', 'peer.down': '断开',
  'owner.changed': '接入点变更', 'owner.ack': '接入确认', 'owner.unconfirmed': '接入未确认', 'owner.conflict': '接入冲突', 'owner.error': '接入记录出错',
  'config.apply': '应用配置', 'config.applied': '应用配置', 'config.error': '配置没生效', 'apply.failed': '应用失败', 'upgrade.failed': '组网服务升级失败，已回退',
  start: '组网服务启动', stop: '组网服务停止', 'control.error': '隧道报错', 'state.error': '状态没保存',
  'path.ready': '线路可用', 'path.failed': '线路不通', 'path.error': '线路出错', 'path.security': '证书不符',
  'direct.up': '直连打通', 'direct.down': '直连断开', 'direct.failed': '打洞失败', 'direct.chosen': '选定直连线路', 'direct.better': '发现更快的直连', 'direct.switched': '换用更快的直连',
  'reach.changed': '可达性变化', 'route.changed': '换路', 'route.error': '系统路由出错', 'route.skipped': '网段没走组网', 'routing.error': '选路出错',
  'link.suppressed': '线路暂停使用', 'link.reused': '线路恢复使用', 'link.redial': '换端口重连', 'link.redial.failed': '换端口重连失败',
  'firewall.restored': '防火墙规则已恢复', error: '错误',
  'sign.rejected': '签名校验失败', 'owner.claim_expired': '旧接入记录作废', 'e2e.error': '端到端加密出错', 'e2e.exhausted': '端到端会话已满',
  'entry.suspect': '入口疑似丢包', 'reach.mismatch': '出口地址对不上', 'routing.unknown_link': '通告了不存在的链路', 'routing.clock': '中转时钟不准',
  'mesh.missing': '组网服务不见了', 'mesh.reinstall': '重新安装组网服务', 'mesh.key_conflict': '上报了其他成员的密钥'};
// What a node says on another's behalf, by what a rejected signature was on.
const SIGNED = {offer: '打洞地址', punch: '打洞状态', commit: '接入声明', 'owner notice': '接入通告', 'routing advertisement': '路由通告'};
// The events about signatures, end-to-end encryption and relays misbehaving; the latest shows in a member's status.
export const SECURITY_EVENTS = ['sign.rejected', 'owner.claim_expired', 'e2e.error', 'e2e.exhausted', 'entry.suspect', 'reach.mismatch', 'routing.unknown_link'];
const ROUTE_REASONS = {down: '原线路断开', better: '新线路更快', shorter: '经过的中转更少', loop: '避免环路'};
export const eventLabel = kind => EVENTS[kind] || '其他事件';
const PATH_REASONS = {'connect timeout': '连接超时', 'connection refused': '连接被拒绝', 'certificate rejected': '证书被拒绝', 'too many paths': '同时尝试的线路太多',
  'connect failed': '连接失败', 'probe timeout': '探测超时', 'relay not ready': '中转还没有就绪', 'commit failed': '切换没有生效', 'punch timeout': '打洞超时'};
const SWITCH_REASONS = {initial: '首次选定', 'active path failed': '在用的入口断开', 'sustained degradation or stable preferred path': '在用的入口持续变差，或排在前面的入口已经稳定',
  'entry requested renewed ownership': '入口要求重新确认'};
// Errors from the operating system and Go's network stack, wherever they end a detail.
const SYSTEM_ERRORS = [
  [/use of closed network connection/i, () => '连接已经关闭'],
  [/address (\S+?): non-IPv4 address/i, a => `地址 ${a.replace(/^::ffff:/i, '')} 被写成了 IPv6 形式（${a}），从 IPv4 端口发不出去`],
  [/connection refused/i, () => '连接被拒绝'], [/connection reset by peer/i, () => '连接被对方重置'], [/broken pipe/i, () => '连接已经断开'],
  [/i\/o timeout|deadline exceeded|timed out/i, () => '超时'], [/no route to host/i, () => '没有到对方的路由'], [/network is unreachable/i, () => '网络不可达'],
  [/no such host/i, () => '域名解析失败'], [/operation not permitted|permission denied/i, () => '系统不允许（权限或防火墙限制）'],
  [/message too long/i, () => '数据包太大'], [/no buffer space available/i, () => '系统发送缓冲区已满'], [/address already in use/i, () => '端口已被别的程序占用'],
  [/cannot assign requested address/i, () => '本机没有这个地址'], [/TLS peer verification failed/i, () => 'TLS 证书和配置里的不一致'],
  [/no path to peer/i, () => '还没有到对方的线路'], [/(?:^|:\s*)EOF$/, () => '连接被对方关闭'], [/invalid argument/i, () => '系统拒绝了这个参数'],
];
// '' when the error is not one of them.
export function systemErrorText(raw) {
  const value = String(raw || '').trim();
  for (const [re, text] of SYSTEM_ERRORS) { const m = re.exec(value); if (m) return text(m[1]); }
  return '';
}
// Tunnel errors (wireguard-go), prefixed with the peer's shortened key: "peer(7q/1…9ZzM) - Failed to … : <error>".
const TUNNEL_OPS = [[/^failed to send handshake initiation/i, who => `向 ${who} 发起握手失败`], [/^failed to send handshake response/i, who => `回应 ${who} 的握手失败`],
  [/^failed to send data packets/i, who => `向 ${who} 发送数据失败`], [/^failed to create (initiation|response) message/i, who => `生成和 ${who} 的握手消息失败`],
  [/^failed to derive keypair/i, who => `和 ${who} 协商密钥失败`], [/^failed to create cookie reply/i, who => `回应 ${who} 的防护校验失败`]];
// Routine records, folded away in the event list: a standby entry's periodic trial getting through
// (the one in use is not routine), a punched path getting ready before "直连打通", relays confirming
// a member's entry to each other, and the tunnel noting a standby's connection closed as the trial ends.
export function routineEvent(e, member, health = {}) {
  const detail = String(e?.detail || '').trim();
  if (e?.kind === 'owner.ack') return true;
  if (e?.kind === 'control.error') return /use of closed network connection/i.test(detail);
  if (e?.kind !== 'path.ready' || detail.startsWith('link:')) return false;
  if (detail.startsWith('direct:')) return true;
  const list = member?.entry?.candidates || [];
  const used = Object.values(health).find(h => h?.state === 'active')?.id || list[0]?.id;
  return detail !== used;
}
// The peer a tunnel error names, by its shortened key: {"7q/1…9ZzM": name}, from the node's config.
export function peerKeys(config) {
  const out = {};
  for (const n of Array.isArray(config?.nodes) ? config.nodes : []) for (const k of [n.publicKey, n.controlKey, n.e2eKey]) {
    const key = String(k || '').replace(/=+$/, '');
    if (key.length > 8) out[`${key.slice(0, 4)}…${key.slice(-4)}`] = n.name || n.id;
  }
  return out;
}
const ID_LIKE = /^[0-9a-f]{8}-[0-9a-f]{4}-|^[0-9a-f]{16,}$/i;
// {label, text, raw, tone} for one event. raw: the original detail when the text leaves part of it
// out or could not read it; '' when the text says it all.
export function eventView(e, member, members, keys = {}) {
  const kind = e?.kind || '', detail = String(e?.detail || '').trim();
  const out = (text, raw = '', extra = {}) => ({label: eventLabel(kind), text, raw, tone: eventTone(kind), ...extra});
  // A machine by its name: the controller's own machine by what the page calls it, one no longer in the mesh said so.
  const who = id => { const m = members?.find(x => x.serverId === id); return m?.name || (id === 'local' ? '主控本机' : ID_LIKE.test(id || '') ? '已移出组网的机器' : id || '对端'); };
  const list = member?.entry?.candidates || [];
  // A path: an entry candidate, a link between relays (link:<peer>) or a punched one (direct:<peer>); /next is a faster one being prepared.
  const place = raw => {
    const id = String(raw || '').replace(/\/next$/, ''), next = id !== String(raw || '') ? '更快的' : '';
    if (id.startsWith('link:')) return `到 ${who(id.slice(5))} 的${next}中转链路`;
    if (id.startsWith('direct:')) return `和 ${who(id.slice(7))} 的${next}直连线路`;
    const i = list.findIndex(c => c.id === id);
    return i >= 0 ? `第 ${i + 1} 个入口（${who(list[i].relay)} · ${transportLabel(list[i].transport)}）` : id ? '一个已删除的入口' : '入口';
  };
  // Why something failed: a known system error in Chinese, else the original kept aside.
  const why = (reason, fallback = '原因见原文') => systemErrorText(reason) || dataPlaneText(reason) || meshCodeText(reason) || (reason ? fallback : '');
  const known = reason => Boolean(systemErrorText(reason) || dataPlaneText(reason) || meshCodeText(reason));
  // This machine, where a relay says a member enters by it.
  const self = member?.name ? `经 ${member.name} ` : '经这里';
  const hop = (dest, h) => h === 'none' ? '无路可走' : h === dest ? '直达' : `经 ${who(h)}`;
  let r;
  switch (kind) {
  case 'direct.up':
    return (r = /^(\S+)\s+via\s+(\w+)$/i.exec(detail)) ? out(`和 ${who(r[1])} 直连，经由${DIRECT_VIA[r[2].toLowerCase()] || r[2]}`) : out('打通了一条直连', detail);
  case 'direct.chosen':
    return (r = /^(\S+)\s+via\s+(\w+)\s+(\S+?),\s*([\d.]+)\s*ms$/i.exec(detail)) ? out(`和 ${who(r[1])} 经${DIRECT_VIA[r[2].toLowerCase()] || r[2]} ${r[3]}，${r[4]} ms`) : out('选定了一条直连线路', detail);
  case 'direct.down': case 'direct.failed': {
    const split = detail.indexOf(':'), peer = split > 0 ? detail.slice(0, split).trim() : detail, reason = split > 0 ? detail.slice(split + 1).trim() : '';
    const said = reason ? directErrorText(reason) : '';
    return out(`${kind === 'direct.down' ? `和 ${who(peer)} 的直连断开` : `和 ${who(peer)} 打洞没有成功`}${said ? `：${said}` : ''}，先走中转`, said && said === reason && !DIRECT_ERRORS[reason.toLowerCase()] ? detail : '');
  }
  case 'direct.better':
    if ((r = /^(\S+)\s+via\s+(\w+)\s+(\S+):\s*([\d.]+)\s*ms,\s*link\s*([\d.]+)\s*ms$/i.exec(detail))) return out(`和 ${who(r[1])} 经${DIRECT_VIA[r[2].toLowerCase()] || r[2]} ${r[3]} 只要 ${r[4]} ms（现在的线路 ${r[5]} ms），准备换过去`);
    if ((r = /^(\S+):\s*(.+)$/.exec(detail))) return out(`和 ${who(r[1])} 的更快线路没能准备好，继续用现在的线路`, '', {label: '放弃更快的直连', tone: 'off'});
    return out('发现一条更快的直连线路', detail);
  case 'direct.switched':
    return (r = /^(\S+)\s+via\s+(\w+)\s+(?:\w+:\/\/)?(\S+)$/i.exec(detail)) ? out(`和 ${who(r[1])} 换用经${DIRECT_VIA[r[2].toLowerCase()] || r[2]} ${r[3]} 的直连`) : out('换用了更快的直连', detail);
  case 'reach.changed':
    return (r = /^(\w+)\s*->\s*(\w+)$/.exec(detail)) ? out(`${VERDICTS[r[1]]?.[1] || r[1]} → ${VERDICTS[r[2]]?.[1] || r[2]}`) : out('可达性检测结论变了', detail);
  case 'route.changed':
    // "<dest>: <old next hop> -> <new next hop> (reason)"; the destination itself as next hop is a direct route, none is no route.
    if (!(r = /^([^:\s]+):\s*(\S+)\s*->\s*(\S+)(?:\s*\((\w+)\))?$/.exec(detail))) return out('换了一条路线', detail);
    return out(`到 ${who(r[1])}：${hop(r[1], r[2])} → ${hop(r[1], r[3])}${r[4] ? `（${ROUTE_REASONS[r[4]] || r[4]}）` : ''}`, '', {route: {dest: r[1], from: hop(r[1], r[2]), to: hop(r[1], r[3]), none: r[3] === 'none'}});
  case 'link.suppressed': case 'link.reused': {
    const peer = detail.split(/[\s:]/)[0];
    return out(peer ? `到 ${who(peer)} 的中转链路${kind === 'link.suppressed' ? '反复断开，暂停用于选路' : '稳定下来，恢复选路'}` : '中转链路状态变了', peer ? '' : detail);
  }
  case 'link.redial': case 'link.redial.failed': {
    // "<path>[ relay=<relay>] reason=rtt|slow rtt=<median>ms baseline=<b>ms slow=<share>% port=<old>-><new>"; a failed one ends in error=<error>.
    const id = detail.split(/\s+/)[0], f = Object.fromEntries([...detail.matchAll(/(\w+)=(\S+)/g)].map(m => [m[1], m[2]]));
    const rtt = parseFloat(f.rtt), base = parseFloat(f.baseline), usual = Number.isFinite(base) ? `，平时 ${Math.round(base)} ms` : '';
    const how = f.reason === 'slow' ? `${f.slow ? `${f.slow} 的探测` : '探测'}变慢或超时${usual}` : `延迟 ${Number.isFinite(rtt) ? Math.round(rtt) : '?'} ms${usual}`;
    if (kind === 'link.redial') {
      const port = /^(\d+)->(\d+)$/.exec(f.port || '');
      return out(`${place(id)}持续变差（${how}），已换个端口重新连接${port ? `（${port[1]} → ${port[2]}）` : ''}`);
    }
    const error = (/\berror=(.*)$/.exec(detail) || [])[1] || '';
    return out(`${place(id)}持续变差（${how}），换端口重连没有成功${known(error) ? `：${why(error)}` : ''}，继续用原来的连接`, error && !known(error) ? detail : '');
  }
  case 'config.applied': case 'config.apply':
    if ((r = /^revision\s+(\d+)$/i.exec(detail))) return out(`第 ${r[1]} 版`);
    // The node kept the config it had: "revision 52, the last applied; revision 53 not applied: <error>".
    if ((r = /^revision\s+(\d+),\s*the last applied;\s*revision\s+(\d+)\s+not applied:\s*(.*)$/i.exec(detail)))
      return out(`第 ${r[2]} 版没能应用，先沿用第 ${r[1]} 版${known(r[3]) ? `：${why(r[3])}` : ''}`, detail, {label: '沿用上一版配置', tone: 'warn'});
    return out('应用了新配置', detail);
  case 'config.error':
    if ((r = /^revision\s+(\d+)\s+not applied:\s*(.*)$/i.exec(detail))) return out(`第 ${r[1]} 版配置没能应用${known(r[2]) ? `：${why(r[2])}` : ''}`, detail);
    if (/^reload certificate:/i.test(detail)) return out(`重新加载证书失败${known(detail) ? `：${why(detail)}` : ''}`, detail);
    if (/^waiting for config\.json$/i.test(detail)) return out('还没有收到配置');
    return out(`配置没能生效${known(detail) ? `：${why(detail)}` : ''}`, detail);
  case 'start':
    if (!(r = /revision\s+(\d+),\s*(\d+)\s+peers?(?:,\s*(\d+)\s+learned addresses? restored)?/i.exec(detail))) return out('', detail);
    return out(`第 ${r[1]} 版配置，${r[2]} 个对端${Number(r[3]) > 0 ? `，恢复了 ${r[3]} 个记下的对端地址` : ''}`);
  case 'stop': return out('', detail.toLowerCase() === 'stopped' ? '' : detail);
  case 'upgrade.failed': return out('新版本启动后没有就绪，已回到原来的版本', detail);
  case 'owner.changed':
    // The relay a member now enters by, as the relays learn it: "<member> -> <relay> seq=N".
    return (r = /^(\S+)\s*->\s*(\S+)/.exec(detail)) ? out(`${who(r[1])} 现在经 ${who(r[2])} 接入`) : out('成员的接入点变了', detail);
  case 'owner.ack':
    return (r = /^(\S+)\s+member=(\S+)/.exec(detail)) ? out(`${who(r[1])} 已确认 ${who(r[2])} ${self}接入`) : out('别的中转确认了接入', detail);
  case 'owner.unconfirmed':
    return (r = /^(\S+)\s+member=(\S+)/.exec(detail)) ? out(`${who(r[1])} 一直没有确认 ${who(r[2])} ${self}接入`) : out('别的中转没有确认接入', detail);
  case 'owner.conflict': return out(`${who(detail)} 的接入点有冲突：另一台中转也说它经自己接入，等它重新确认`);
  case 'owner.error':
    if ((r = /^(\S+)\s+seq=\d+\s+refresh:\s*(.*)$/.exec(detail))) return out(`续期 ${who(r[1])} 的接入失败${known(r[2]) ? `：${why(r[2])}` : ''}`, detail);
    if ((r = /^(\S+)\s+seq=\d+:\s*commit failed:\s*(.*)$/.exec(detail))) return out(`让 ${who(r[1])} ${self}接入失败${known(r[2]) ? `：${why(r[2])}` : ''}`, detail);
    if ((r = /^(\S+)\s+seq=\d+:\s*(.*)$/.exec(detail))) return out(`记录 ${who(r[1])} 的接入失败${known(r[2]) ? `：${why(r[2])}` : ''}`, detail);
    return out('记录成员的接入时出错', detail);
  case 'control.error': {
    if (!(r = /^peer\(([^)]+)\)\s*-\s*(.*?)(?:\s*\((\d+) similar not shown since the last one\))?$/i.exec(detail))) return out(`隧道报错${known(detail) ? `：${why(detail)}` : ''}`, detail);
    const peer = keys[r[1]] || '一个对端', split = r[2].indexOf(':'), op = split > 0 ? r[2].slice(0, split) : r[2], reason = split > 0 ? r[2].slice(split + 1).trim() : '';
    const head = TUNNEL_OPS.find(([re]) => re.test(op))?.[1](peer) || `和 ${peer} 之间的隧道出错`;
    const more = r[3] ? `（之后 1 分钟内还有 ${r[3]} 条相同的没有记录）` : '';
    return out(`${head}${known(reason) ? `：${why(reason)}` : ''}${more}`, detail);
  }
  case 'path.ready': return out(place(detail));
  case 'path.failed': {
    const link = /^((?:link|direct):[^:]+):\s*(.*)$/.exec(detail);
    const split = detail.indexOf(':'), id = link ? link[1] : split > 0 ? detail.slice(0, split).trim() : '';
    const reason = (link ? link[2] : split > 0 ? detail.slice(split + 1) : detail).trim();
    const said = PATH_REASONS[reason.toLowerCase()] || (known(reason) ? why(reason) : '');
    return out(`${place(id)}：${said || '原因见原文'}`, said ? '' : detail);
  }
  case 'path.error': {
    // "<peer>: prime|punch|dial|data warm: <error>", or "<peer>: <error>" when a path closes with one.
    if (!(r = /^([^:\s]+):\s*(?:(prime|punch|dial|data warm):\s*)?(.*)$/.exec(detail))) return out(`线路出错${known(detail) ? `：${why(detail)}` : ''}`, detail);
    const stage = {prime: `为和 ${who(r[1])} 打洞做准备时出错`, punch: `向 ${who(r[1])} 打洞时出错`, dial: `连接 ${who(r[1])} 的指定地址失败`, 'data warm': `准备到 ${who(r[1])} 的线路时出错`}[r[2]] || `到 ${who(r[1])} 的线路出错`;
    return out(`${stage}${known(r[3]) ? `：${why(r[3])}` : ''}`, detail);
  }
  case 'path.security': return out('对方的 TLS 证书和配置里的不一致，没有连上', detail);
  case 'entry.unavailable': return out(dataPlaneText(detail) || '没有可用的入口', dataPlaneText(detail) ? '' : detail);
  case 'state.error': return out(`入口状态没能保存到磁盘${known(detail) ? `：${why(detail)}` : ''}`, detail);
  case 'route.error': return out(`更新系统路由失败${known(detail) ? `：${why(detail)}` : ''}`, dataPlaneText(detail) ? '' : detail);
  case 'routing.error':
    if ((r = /^advertisement from (\S+):/.exec(detail))) return out(`收到 ${who(r[1])} 的路由通告有误，已忽略`, detail);
    if (/^advertisement exceeds/i.test(detail)) return out('要通告的路由太多，超过了大小上限，有的路由没有通告出去', detail);
    return out('选路出错', detail);
  case 'route.skipped': {
    // "gateway subnets [a b] overlap this machine's networks; not routed into the mesh", or "… no longer overlap …; routed into the mesh".
    const nets = (/\[([^\]]*)\]/.exec(detail)?.[1] || '').split(/\s+/).filter(Boolean).join('、');
    if (/no longer overlap/i.test(detail)) return out(`网关网段 ${nets} 不再和本机所在的网络重叠，已改为经组网访问`, '', {label: '网段改走组网', tone: 'off'});
    if (/overlap/i.test(detail)) return out(`网关网段 ${nets} 和本机所在的网络重叠，本机不经组网访问它们`, '', {tone: 'warn'});
    return out('网关网段的路由有变化', detail);
  }
  case 'firewall.restored': return out('组网的防火墙规则被别的程序改动了，已经自动恢复', detail);
  case 'sign.rejected': {
    // "<what> in the name of <node> from <peer>: bad signature", a notice's node as <member>/<owner>, an advertisement's as "its origin".
    if (!(r = /^(.+?) in the name of (.+?) from (\S+): bad signature(?: \((\d+) more since the last one\))?$/.exec(detail))) return out('收到一条签名不对的消息，已丢弃', detail);
    const [, what, name, hop, more] = r, again = more ? `（之后 1 分钟内还有 ${more} 条）` : '';
    const advice = '偶尔一次可能是对方刚重装、新密钥还没下发到这里；持续出现说明转发它的机器可能被篡改，检查那台机器。';
    if (what === 'conflict claim') return out(`${who(hop)} 回复的接入冲突附带的声明不是本机签的，已忽略${again}。持续出现说明这台中转可疑。`);
    const owner = /^(\S+)\/(\S+)$/.exec(name);
    const said = name === 'its origin' ? '' : owner ? `${who(owner[1])} 经 ${who(owner[2])} 接入的` : name === hop ? '' : `以 ${who(name)} 名义的`;
    return out(`${who(hop)} ${name === hop ? '发来' : '转来'}的${said}${SIGNED[what] || what}签名不对，已丢弃${again}。${advice}`, SIGNED[what] ? '' : detail);
  }
  case 'owner.claim_expired':
    return (r = /^(\d+) stored claims/.exec(detail)) ? out(`作废了 ${r[1]} 条没有有效签名的接入记录，相关成员会自动重新接入。升级后或成员重装后出现一次属于正常。`)
      : out('作废了没有有效签名的接入记录', detail);
  case 'e2e.exhausted': {
    const more = (r = /\((\d+) refused since the last report\)$/.exec(detail)) ? `，上次记录以来又拒绝了 ${r[1]} 次` : '';
    if ((r = /^all (\d+) end-to-end sessions in use .*?; packets for (\S+) dropped/.exec(detail)))
      return out(`${r[1]} 个端到端会话都在用，发往 ${who(r[2])} 的包已丢弃，不会改用明文${more}。经常出现说明这台机器同时通信的对端太多。`);
    if ((r = /^more than (\d+) new end-to-end sessions a second; packets for (\S+) dropped/.exec(detail)))
      return out(`每秒新建的端到端会话超过 ${r[1]} 个，发往 ${who(r[2])} 的包已丢弃，不会改用明文${more}。`);
    return out('端到端会话已满，有包被丢弃，不会改用明文', detail);
  }
  case 'e2e.error': {
    if ((r = /^session with (\S+?):\s*(.*)$/.exec(detail))) return out(`建立和 ${who(r[1])} 的端到端会话失败${known(r[2]) ? `：${why(r[2])}` : ''}，发往它的包会被丢弃`, detail);
    if (!(r = /^peer\(([^)]+)\)\s*-\s*(.*?)(?:\s*\((\d+) similar not shown since the last one\))?$/i.exec(detail))) return out(`端到端加密层报错${known(detail) ? `：${why(detail)}` : ''}`, detail);
    const peer = keys[r[1]] || '一个对端', split = r[2].indexOf(':'), op = split > 0 ? r[2].slice(0, split) : r[2], reason = split > 0 ? r[2].slice(split + 1).trim() : '';
    const head = TUNNEL_OPS.find(([re]) => re.test(op))?.[1](peer) || `和 ${peer} 之间的端到端加密出错`;
    return out(`${head}${known(reason) ? `：${why(reason)}` : ''}${r[3] ? `（之后 1 分钟内还有 ${r[3]} 条相同的没有记录）` : ''}`, detail);
  }
  case 'entry.suspect': {
    // "<entry> answers probes but <n> end-to-end probes through it to <relay> in a row went unanswered; <action>".
    if (!(r = /^(\S+) answers probes but (\d+) end-to-end probes through it to (\S+) in a row went unanswered; (.*)$/.exec(detail))) return out('入口回应探测，但经它的业务流量可能被丢弃', detail);
    const action = r[4].startsWith('moving off') ? '已换到备用入口。' : /fixed mode/.test(r[4]) ? '固定入口模式只提醒、不切换；请检查这台中转。'
      : r[4].startsWith('reported only:') ? `没有切换：${who(r[4].slice(15).split(/\s/)[0])} 不是就绪的备用入口，分不清是不是入口的问题。` : '';
    return out(`入口 ${who(r[1])} 回应探测，但经它发往 ${who(r[3])} 的端到端探测连续 ${r[2]} 次没有回应，可能只丢业务流量。${action}`, action ? '' : detail, {tone: r[4].startsWith('moving off') ? 'bad' : 'warn'});
  }
  case 'reach.mismatch': {
    // "reflected [<addr> …] not offered; relays saw <ip> by <relay>,<relay>; …".
    if (!(r = /^reflected \[([^\]]*)\] not offered; relays saw (.*)$/.exec(detail))) return out('反射器报的出口地址和中转看到的对不上', detail);
    const seen = r[2].split(/;\s*/).map(s => /^(\S+) by (\S+)$/.exec(s)).filter(Boolean).map(m => `${m[2].split(',').map(who).join('、')} 看到 ${m[1]}`).join('；');
    const dropped = r[1].trim().split(/\s+/).filter(Boolean).join('、');
    return out(`${dropped ? `反射器报的出口地址 ${dropped} 和中转在已认证线路上看到的对不上，没有用来打洞` : '各中转看到的出口地址不一致'}${seen ? `（${seen}）` : ''}。持续出现说明某个反射器谎报地址，或这台机器有多条出口。`);
  }
  case 'routing.unknown_link': {
    // "<origin> advertises links the mesh does not have, ignored: [<peer> …]".
    if (!(r = /^(\S+) advertises links the mesh does not have, ignored: \[([^\]]*)\]$/.exec(detail))) return out('有中转通告了组网里没有的链路，已忽略', detail);
    const links = r[2].trim().split(/\s+/).filter(Boolean).map(p => `${who(r[1])}—${who(p)}`).join('、');
    return out(`${who(r[1])} 的路由通告里有组网里没有的链路${links ? ` ${links}` : ''}，已忽略，不会经它们选路。刚改过中转链路时偶尔出现属于正常；持续出现说明这台中转的配置没更新，或被篡改，检查那台机器。`);
  }
  case 'routing.clock': {
    // "<origin>'s clock is <Go duration> off this relay's", positive when the origin's clock is ahead.
    if (!(r = /^(\S+?)'s clock is (-?)((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?) off this relay's$/.exec(detail)) || !r[3]) return out('有中转的时钟和这里差得较多', detail);
    const parts = /^(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?$/.exec(r[3]);
    const span = [[parts[1], '小时'], [parts[2], '分'], [parts[3], '秒']].filter(([n]) => n && Number(n)).map(([n, u]) => `${Number(n)} ${u}`).join(' ') || '0 秒';
    return out(`${who(r[1])} 的时钟比这里${r[2] ? '慢' : '快'} ${span}。不影响选路和签名校验，但会让两边的日志和事件时间对不上，建议检查这两台机器的时间同步（NTP）。`);
  }
  // Older data planes: "<peer>" or "<peer>: <reason>".
  case 'peer.reconnect': case 'peer.up': case 'peer.down': {
    if (!(r = /^([^:\s]+)(?::\s*(.*))?$/.exec(detail))) return out('', detail);
    const verb = {'peer.reconnect': '重连', 'peer.up': '连上', 'peer.down': '断开'}[kind];
    return out(`和 ${who(r[1])} ${verb}${known(r[2]) ? `：${why(r[2])}` : ''}`, r[2] && !known(r[2]) ? detail : '');
  }
  // Recorded by the controller.
  case 'mesh.missing': return out('可能被卸载或重装过，暂停给它下发配置；点「重新安装组网服务」恢复', detail);
  case 'mesh.reinstall': return out('已经装回，其他成员会收到它的新密钥', detail);
  case 'entry.flapping':
    return (r = /at least (\d+) entry switches in (\d+) minutes?/i.exec(detail)) ? out(`${r[2]} 分钟内切换了至少 ${r[1]} 次入口`) : out('入口切换太频繁', detail);
  case 'entry.switch':
    return (r = /^(\S+)\s*->\s*(\S+?)(?::\s*(.*))?$/.exec(detail)) ? out(`${place(r[1])} → ${place(r[2])}${r[3] ? `：${SWITCH_REASONS[r[3].toLowerCase()] || r[3]}` : ''}`, r[3] && !SWITCH_REASONS[r[3].toLowerCase()] ? detail : '') : out('切换了入口', detail);
  case 'entry.prepare': case 'entry.committed':
    return (r = /^(\S+?)(?:\s+seq=\d+)?(?::\s*(.*))?$/.exec(detail)) ? out(`${place(r[1])}${r[2] ? `：${SWITCH_REASONS[r[2].toLowerCase()] || r[2]}` : ''}`, r[2] && !SWITCH_REASONS[r[2].toLowerCase()] ? detail : '') : out('', detail);
  default:
    if (EVENTS[kind]) return out(known(detail) ? why(detail) : '', known(detail) ? '' : detail);
    return out('', [kind, detail].filter(Boolean).join(': '));
  }
}
export const eventDetail = (e, member, members, keys = {}) => eventView(e, member, members, keys).text;
export const eventTone = kind => ['owner.changed', 'owner.ack', 'config.applied', 'start', 'stop'].includes(kind) ? 'off'
  : ['direct.up', 'direct.switched', 'link.reused', 'path.ready', 'entry.committed', 'mesh.reinstall'].includes(kind) ? 'ok'
  : /error|fail|down|security|unavailable|unconfirmed|conflict|missing|rejected|exhausted/.test(kind || '') ? 'bad'
  : /switch|reconnect|return|suppressed|flapping|redial|route\.changed|reach\.changed|restored|skipped|suspect|mismatch|unknown_link|routing\.clock/.test(kind || '') ? 'warn' : 'off';

// A member's end-to-end sessions, signature checks and latest security event as status rows
// ({label, value, note, noteCls, cls, title, wide, at}); none for what its node does not report.
export function securityStats(status, events, member, members, keys = {}) {
  const rows = [], e2e = status?.e2e, sec = status?.security;
  if (e2e && e2e.limit) {
    const drops = [['会话已满', e2e.droppedExhausted], ['队列满', e2e.droppedQueue], ['来源不符', e2e.droppedSource], ['无法解密', e2e.droppedBad],
      ['未加密', status.counters?.droppedPlain]].filter(([, n]) => n > 0);
    const total = drops.reduce((s, [, n]) => s + n, 0);
    rows.push({label: '端到端会话', value: `${e2e.sessions || 0} / ${e2e.limit}`, cls: e2e.sessions >= e2e.limit ? 'ob-warn' : '',
      note: total ? `丢弃 ${total} 个包：${drops.map(([k, n]) => `${k} ${n}`).join('、')}` : '', noteCls: 'ob-warn', wide: total > 0});
  }
  if (sec?.signing) {
    const rejected = sec.signing.rejected || 0, hops = Object.entries(sec.signing.byHop || {}).sort((a, b) => b[1] - a[1]);
    const from = hops.map(([id, n]) => `${nameOf(members || [], id)} ${n} 次`).join('、');
    const expired = sec.claims?.expired ? `作废旧接入记录 ${sec.claims.expired} 条` : '';
    rows.push({label: '验签拒绝', value: `${rejected} 次`, cls: rejected ? 'ob-warn' : '', title: `已验证 ${sec.signing.verified || 0} 次`,
      note: [rejected && from ? `来自 ${from}` : '', expired].filter(Boolean).join('；'), noteCls: rejected ? 'ob-warn' : '', wide: Boolean(rejected || expired)});
  }
  const last = (events || []).filter(e => SECURITY_EVENTS.includes(e?.kind)).sort((a, b) => (b.at ?? 0) - (a.at ?? 0))[0];
  if (last) {
    const v = eventView(last, member, members, keys);
    rows.push({label: '最近安全事件', value: v.label, note: v.text, noteCls: v.tone === 'bad' ? 'ob-bad' : v.tone === 'warn' ? 'ob-warn' : '', at: last.at, wide: true});
  }
  return rows;
}

// The event list as rows, newest first: events reading the same as the one before fold into it (the
// controller folds repeats of one detail; ids and sequence numbers left out, more read the same), and a
// run of route changes becomes one row saying where each destination went, its changes inside.
// Row: {key, kind, label, text, raw, tone, at, firstAt, repeat, items?}; repeat counts the events in it.
export function eventRows(events, member, members, keys = {}) {
  const rows = [];
  for (const e of events || []) {
    const v = eventView(e, member, members, keys), n = Number(e.repeat) > 1 ? Number(e.repeat) : 1;
    const first = Number.isFinite(e.firstAt) ? e.firstAt : e.at, last = rows.at(-1);
    if (last && last.kind === e.kind && last.text === v.text && last.raw === v.raw) {
      last.repeat += n;
      if (Number.isFinite(first)) last.firstAt = Number.isFinite(last.firstAt) ? Math.min(last.firstAt, first) : first;
      continue;
    }
    rows.push({key: e.key ?? `${e.seq}-${e.at}-${rows.length}`, kind: e.kind, ...v, at: e.at, firstAt: first, repeat: n});
  }
  const out = [];
  for (const row of rows) {
    const last = out.at(-1);
    if (row.route && last && (last.items || last.route)) {
      if (!last.items) out[out.length - 1] = {key: `group-${last.key}`, kind: 'route.changed', label: eventLabel('route.changed'), raw: '', at: last.at, items: [last]};
      out.at(-1).items.push(row);
    } else out.push(row);
  }
  return out.map(row => row.items ? routeGroup(row, members) : row);
}
// A run of route changes, newest first: per destination where it went, oldest change first.
function routeGroup(group, members) {
  const items = group.items, byDest = new Map();
  for (const item of [...items].reverse()) {
    const d = byDest.get(item.route.dest) || {dest: item.route.dest, states: [item.route.from], count: 0, last: 0};
    d.states.push(item.route.to);
    d.count += item.repeat;
    d.last = item.at;
    byDest.set(item.route.dest, d);
  }
  const dests = [...byDest.values()].sort((a, b) => b.last - a.last);
  const name = id => members?.find(m => m.serverId === id)?.name || (id === 'local' ? '主控本机' : ID_LIKE.test(id) ? '已移出组网的机器' : id);
  const line = d => {
    const now = d.states.at(-1), seen = [...new Set(d.states)];
    if (d.count === 1) return `到 ${name(d.dest)}：${d.states[0]} → ${now}`;
    if (seen.length === 2) return `到 ${name(d.dest)}：${seen[0]} ⇄ ${seen[1]} 来回 ${d.count} 次，现在${now}`;
    return `到 ${name(d.dest)}：换了 ${d.count} 次，现在${now}`;
  };
  const shown = dests.slice(0, 3).map(line);
  if (dests.length > 3) shown.push(`另有 ${dests.length - 3} 个目标`);
  const firsts = items.map(i => i.firstAt).filter(Number.isFinite);
  return {...group, text: shown.join('；'), tone: dests.some(d => d.states.at(-1) === '无路可走') ? 'bad' : 'warn',
    repeat: items.reduce((s, i) => s + i.repeat, 0), firstAt: firsts.length ? Math.min(...firsts) : group.at};
}

// ---- lines: GET /mesh lines[] = {id, kind, a, b, transport, state, rttMs, lossRate, ends: {a, b}} ----
// A line is one link as both its ends see it; its id is what the board link maps to (lineIdOf).
export const LINE_STATES = {up: ['ok', '连通'], down: ['bad', '断开'], standby: ['off', '热备'], cold: ['off', '冷备'], unused: ['off', '未启用'], unknown: ['off', '没有数据']};
export const lineOf = (data, id) => (id && Array.isArray(data?.lines) ? data.lines.find(l => l?.id === id) || null : null);
// One end as it reports itself: where it sends from, over what, to where; its public mapping when
// punched; whether it dials. null: this end has not reported.
export function lineEnd(end, transport) {
  if (!end || typeof end !== 'object') return null;
  const dialer = end.dialer === true ? '主动拨号' : end.dialer === false ? '等待对方连接' : '';
  // A standby entry that is not connected has no local address yet: only where it would go.
  const local = end.local || '', via = transportLabel(transport) || '未知方式', remote = end.remote || '未知地址';
  return {route: `${local ? `从 ${local} ` : ''}经 ${via} 连到 ${remote}`, local, via, remote, public: end.public || '', dialer};
}
// The ranges a line's record can be read for.
export const LINE_RANGES = [['1h', '1 小时'], ['24h', '24 小时'], ['3d', '3 天'], ['7d', '7 天'], ['15d', '15 天'], ['30d', '30 天']];
const num = v => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
// The length of each range, in seconds.
export const LINE_RANGE_SECONDS = {'1h': 3600, '24h': 86400, '3d': 3 * 86400, '7d': 7 * 86400, '15d': 15 * 86400, '30d': 30 * 86400};
// summary.availability is a percentage (0–100) of the time with a known state. It never shows
// 100% while some of that time was down (the controller rounds to two decimals).
export function availabilityText(pct, downSeconds) {
  const p = num(pct);
  if (p === null) return '';
  if (p >= 100) return num(downSeconds) > 0 ? '> 99.99%' : '100%';
  return p >= 99 ? `${Math.floor(p * 100 + 1e-6) / 100}%` : `${Math.floor(p * 10 + 1e-6) / 10}%`;
}
// GET /mesh/lines/:id/trend summary: {availability (0–100), rttMs {avg, min, max, p95}, lossRate,
// downs, downSeconds, knownSeconds (time with a known state; 0: nothing known over the range)}.
// Facts as [label, value, tone]; a cold standby's numbers come from its occasional tries.
export function lineFacts(s, cold = false, range = '') {
  if (!s || typeof s !== 'object') return [];
  const rtt = s.rttMs && typeof s.rttMs === 'object' ? s.rttMs : {};
  const downs = num(s.downs), downSeconds = num(s.downSeconds), known = num(s.knownSeconds), span = LINE_RANGE_SECONDS[range] || 0;
  const avail = known === 0 ? null : num(s.availability), min = msText(num(rtt.min)), max = msText(num(rtt.max));
  // Availability is over the time with a known state: say how much that was when it is not the whole range.
  const covered = !known ? '' : cold ? durationText(known * 1000) : span && known < span * 0.95 ? `${durationText(known * 1000)} / ${durationText(span * 1000)}` : '';
  return [
    [cold ? '试连成功率' : '可用率', availabilityText(avail, downSeconds), avail !== null && avail < 99 ? 'warn' : ''],
    ['平均延迟', msText(num(rtt.avg))], ['P95 延迟', msText(num(rtt.p95))],
    ['最小 / 最大', min && max ? `${min} / ${max}` : ''],
    ['丢包', lossText(num(s.lossRate)), num(s.lossRate) > 0.02 ? 'warn' : ''],
    cold ? ['试连失败', downs === null || known === 0 ? '' : downs ? `${downs} 次` : '没有失败', downs ? 'warn' : '']
      : ['断开', downs === null || known === 0 ? '' : downs ? `${downs} 次${downSeconds ? `，共 ${durationText(downSeconds * 1000)}` : ''}` : '没有断开', downs ? 'bad' : ''],
    [cold ? '有抽测的时间' : '有记录的时间', covered],
  ].filter(([label, value]) => label && value);
}
// The member trend's latency is measured to someone (GET /members/:id/trend rtt = {kind, peers}).
export function trendRttText(rtt, members = []) {
  const names = (Array.isArray(rtt?.peers) ? rtt.peers : []).map(id => nameOf(members, id)).filter(Boolean);
  if (rtt?.kind === 'entry') return names.length ? `到 ${names.join('、')}（入口）的延迟` : '到入口的延迟';
  // A relay's: its links to the other relays only.
  if (rtt?.kind === 'links') return names.length ? `到 ${names.join('、')} 的中转链路平均延迟` : '到其他中转的链路平均延迟';
  return '';
}

// ----- member settings, checked as typed against the controller's rules (mesh_model.py) -----
// Dotted IPv4 as the controller reads it (Python ipaddress): four numbers 0–255, no leading zeros.
export function ipv4(text) {
  const parts = String(text ?? '').split('.');
  if (parts.length !== 4 || !parts.every(p => /^(0|[1-9]\d{0,2})$/.test(p) && Number(p) <= 255)) return null;
  return parts.reduce((n, p) => n * 256 + Number(p), 0);
}
const ipText = n => [24, 16, 8, 0].map(s => Math.floor(n / 2 ** s) % 256).join('.');
// 'a.b.c.d/n' → {base, size, bits, host}; host: address bits set past the prefix. null if not a network at all.
export function cidr(text) {
  const m = /^([^/]+)\/(0|[1-9]\d?)$/.exec(String(text ?? '').trim());
  const ip = m ? ipv4(m[1]) : null, bits = m ? Number(m[2]) : NaN;
  if (ip === null || !(bits <= 32)) return null;
  const size = 2 ** (32 - bits), base = Math.floor(ip / size) * size;
  return {base, size, bits, host: ip !== base, text: `${ipText(base)}/${bits}`};
}
const overlaps = (a, b) => a.base < b.base + b.size && b.base < a.base + a.size;
// The control addresses take the mesh prefix's last /24.
export function meshPrefixes(settings) {
  const net = cidr(settings?.prefix || '100.96.0.0/16');
  if (!net || net.host) return {net: null, ctrl: null};
  const given = cidr(settings?.controlPrefix || ''), last = net.base + net.size - 256;
  return {net, ctrl: given && !given.host ? given : {base: last, size: 256, bits: 24, host: false, text: `${ipText(last)}/24`}};
}
// A member's mesh address: empty (assigned on apply), else an IPv4 inside the prefix, not its network
// or broadcast address, not a control address, and no other member's.
export function addressProblem(value, settings, members, self) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  const ip = ipv4(text);
  if (ip === null) return '写成 IPv4 地址，例如 100.96.0.10；留空自动分配';
  const {net, ctrl} = meshPrefixes(settings);
  if (!net) return '';
  if (ip < net.base || ip >= net.base + net.size) return `不在组网网段 ${net.text} 内`;
  if (ip === net.base) return `${text} 是组网网段的网络地址，不能分给成员`;
  if (ip === net.base + net.size - 1) return `${text} 是组网网段的广播地址，不能分给成员`;
  if (ctrl && ip >= ctrl.base && ip < ctrl.base + ctrl.size) return `${ctrl.text} 留作控制地址，请换一个`;
  const other = (members || []).find(m => m !== self && !m.removing && m.serverId !== self?.serverId && ipv4(m.address) === ip);
  return other ? `和 ${other.name || other.serverId} 的地址重复` : '';
}
// A listen port: a whole number 1–65535, not one this member already uses for another transport.
export function portProblem(value, listen, transport) {
  const text = String(value ?? '').trim(), port = Number(text);
  if (!/^\d+$/.test(text) || port < 1 || port > 65535) return '端口在 1–65535 之间';
  const same = TRANSPORTS.find(([t]) => t !== transport && Number(listen?.[t]) === port);
  return same ? `和 ${same[1]} 端口相同` : '';
}
// A network written into a rule, as the controller reads it: a.b.c.d/n with no address bits past the
// prefix, or one address, which stands for itself (/32). {net: the network as stored, problem}.
export function ruleNet(value) {
  const text = String(value ?? '').trim();
  if (ipv4(text) !== null) return {net: `${text}/32`, problem: ''};
  const net = cidr(text);
  if (!net) return {net: '', problem: '写成 IP 或网段，例如 10.0.0.5 或 10.0.0.0/24'};
  if (net.host) return {net: '', problem: `${text} 不是网段的起始地址，应写成 ${net.text}`};
  return {net: net.text, problem: ''};
}
// The mesh's address block: a network with a prefix of 8 to 22 bits (mesh_model.py).
export function meshPrefixProblem(value) {
  const text = String(value ?? '').trim(), net = cidr(text);
  if (!net) return '写成网段，例如 100.96.0.0/16';
  if (net.host) return `${text} 不是网段的起始地址，应写成 ${net.text}`;
  if (net.bits < 8 || net.bits > 22) return '前缀长度在 8～22 之间，例如 100.96.0.0/16';
  return '';
}
// A gateway network: a.b.c.d/n with no address bits past the prefix, clear of the mesh prefix and of
// other gateways' networks. While typing (`typing`), only what is already wrong is said.
export function gatewayNetProblem(value, {settings = null, members = [], self = null, typing = false} = {}) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  if (typing && /^[\d./]*$/.test(text) && !/^\d+\.\d+\.\d+\.\d+\/\d+$/.test(text)) return '';
  const net = cidr(text);
  if (!net) return '写成网段，例如 192.168.1.0/24';
  if (net.host) return `${text} 不是网段的起始地址，应写成 ${net.text}`;
  const mesh = meshPrefixes(settings).net;
  if (mesh && overlaps(net, mesh)) return `和组网网段 ${mesh.text} 重叠`;
  for (const m of members || []) {
    if (m === self || m.removing || m.serverId === self?.serverId) continue;
    const hit = (m.gateway?.subnets || []).find(s => { const o = cidr(s); return o && overlaps(o, net); });
    if (hit) return `和 ${m.name || m.serverId} 的网段 ${hit} 重叠`;
  }
  return '';
}
// GET /mesh warnings at one member's listen ports: {udp: [texts], …, '': [texts for the whole]}.
export function listenWarnings(data, index) {
  const out = {};
  for (const w of Array.isArray(data?.warnings) ? data.warnings : []) {
    const m = /^members\[(\d+)\]\.listen(?:\.(\w+))?$/.exec(w?.path || '');
    if (!m || Number(m[1]) !== index || !['mesh_port_in_use', 'mesh_port_unchecked'].includes(w.error)) continue;
    const text = w.error === 'mesh_port_in_use' ? `${String(w.message || '端口已被其他程序占用').replace(/[。.]$/, '')}。应用时会被拒绝。` : errorMessage(w);
    (out[m[2] || ''] ||= []).push({code: w.error, text});
  }
  return out;
}
