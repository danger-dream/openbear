import test from 'node:test';
import assert from 'node:assert/strict';
import { meshCodeText, relayInUse, routedLinks } from './meshHelpers.js';

test('mesh error codes read in Chinese, with the node reason after a colon', () => {
  assert.equal(meshCodeText('mesh_apply_running'), '正在应用，请稍后再试');
  assert.equal(meshCodeText('capability_disabled'), '这台服务器没有开启执行命令');
  assert.equal(meshCodeText('invalid_config: self.address outside network.prefix'), '节点拒绝了配置：self.address outside network.prefix');
  assert.equal(meshCodeText('apply_timeout: waiting for config.json'), '节点在规定时间内没有完成应用：waiting for config.json');
  assert.equal(meshCodeText('upgrade_not_ready'), '组网服务升级后没有就绪，已回退');
  // Unknown codes and free text are left to the caller.
  assert.equal(meshCodeText('something_else'), '');
  assert.equal(meshCodeText('c1 -> c2: 3 probes timed out'), '');
  assert.equal(meshCodeText(''), '');
});

test('data plane phrases and path failures read in Chinese', async () => {
  const {eventDetail} = await import('./meshHelpers.js');
  assert.equal(meshCodeText('ownership propagation pending: 2 acknowledgements'), '入口归属正在同步，还在等 2 台确认');
  assert.equal(meshCodeText('no ready entry'), '没有可用的入口');
  assert.equal(meshCodeText('entry state not saved'), '入口状态没有保存下来');
  const members = [{serverId: 'lax', name: 'LAX'}];
  assert.equal(eventDetail({kind: 'path.failed', detail: 'link:lax: too many paths'}, null, members), '到 LAX 的中转链路：同时尝试的线路太多');
  assert.equal(eventDetail({kind: 'path.failed', detail: 'c2: certificate rejected'}, {entry: {candidates: [{id: 'c1'}, {id: 'c2', relay: 'lax', transport: 'tls'}]}}, members), '第 2 个入口（LAX · TLS）：证书被拒绝');
});

test('punched direct links: the worse end wins, labels, reasons, events and codes', async () => {
  const {directLabel, directLine, directLive, directReason, errorMessage, eventDetail, eventLabel, eventTone} = await import('./meshHelpers.js');
  const peer = (id, direct, rttMs = null) => ({id, kind: 'direct', rttMs, direct});
  const now = Date.now();
  assert.equal(directLive([], [], 'a', 'b'), null);
  const up = directLive([peer('b', {state: 'direct', via: 'lan', since: now - 90000, attempts: 1, lastError: ''}, 3.2)], [peer('a', {state: 'direct', via: 'lan'}, 4.6)], 'a', 'b');
  assert.equal(directLabel(up), '直连 · 局域网 · 4.6 ms');
  assert.equal(directLabel({state: 'direct', via: 'public', rttMs: 40}), '直连 · 公网 · 40 ms');
  const mixed = directLive([peer('b', {state: 'direct', via: 'public'})], [peer('a', {state: 'punching', attempts: 3})], 'a', 'b');
  assert.deepEqual([mixed.state, mixed.from, mixed.differ, directLabel(mixed), directLabel(mixed, true)], ['punching', 'b', true, '打洞中', '打洞中 · 第 3 次尝试']);
  const relayed = directLive([peer('b', {state: 'relayed', lastError: 'punch timeout'})], null, 'a', 'b');
  assert.deepEqual([directLabel(relayed), directLabel(relayed, true)], ['走中转', '走中转 · 打洞未成功，会定时重试']);
  assert.match(directReason({state: 'relayed', lastError: 'no reflector'}), /没有可用来打洞的中转/);
  assert.match(directReason({state: 'relayed', lastError: 'direct path lost'}), /直连线路中断/);
  assert.equal(directLine({state: 'relayed', since: now - 300000, attempts: 4, lastError: 'punch timeout'}, now), '走中转 · 已 5 分钟 · 最近错误：打洞超时');
  const members = [{serverId: 'office', name: 'Office'}];
  assert.deepEqual(['direct.up', 'direct.down', 'direct.failed'].map(eventLabel), ['直连打通', '直连断开', '打洞失败']);
  assert.equal(eventDetail({kind: 'direct.up', detail: 'office via lan'}, null, members), '和 Office 直连，经由局域网');
  assert.equal(eventDetail({kind: 'direct.up', detail: 'office via configured'}, null, members), '和 Office 直连，经由对外地址');
  // Only two ends both behind NAT punch: with one reachable from outside it is a plain connection.
  const {needsPunch} = await import('./meshHelpers.js');
  const reach = (verdicts) => ({nodes: Object.fromEntries(Object.entries(verdicts).map(([id, [verdict, publicAddr]]) => [id, {summary: {reachability: {verdict, publicAddr}}}]))});
  assert.equal(needsPunch(reach({a: ['public', '192.0.2.222:51997'], b: ['public', '192.0.2.84:51999']}), 'a', 'b'), false);
  assert.equal(needsPunch(reach({a: ['public', '192.0.2.222:51997'], b: ['punchable', '192.0.2.117:51999']}), 'a', 'b'), false);
  assert.equal(needsPunch(reach({a: ['punchable', '192.0.2.34:1024'], b: ['symmetric', '192.0.2.34:51999']}), 'a', 'b'), false);
  assert.equal(needsPunch(reach({a: ['punchable', '192.0.2.117:51999'], b: ['symmetric', '192.0.2.34:51999']}), 'a', 'b'), true);
  assert.equal(needsPunch(reach({a: ['punchable', '192.0.2.117:51999']}), 'a', 'b'), null);
  const connecting = {state: 'punching', attempts: 2};
  assert.deepEqual([directLabel(connecting, false, false), directLabel(connecting, true, false), directLabel(connecting, true, null)], ['连接中', '连接中 · 第 2 次尝试', '打洞中 · 第 2 次尝试']);
  assert.equal(directReason({state: 'relayed', lastError: 'punch timeout'}, false), '没有连上，会定时重试');
  assert.equal(eventDetail({kind: 'direct.down', detail: 'office: direct path lost'}, null, members), '和 Office 的直连断开：直连线路中断，先走中转');
  assert.equal(eventDetail({kind: 'direct.failed', detail: 'office: punch timeout'}, null, members), '和 Office 打洞没有成功：打洞超时，先走中转');
  assert.deepEqual(['direct.up', 'direct.down', 'direct.failed'].map(eventTone), ['ok', 'bad', 'bad']);
  assert.equal(errorMessage({path: 'directLinks[0]', error: 'udp_not_listening', message: 'both ends must listen on udp'}), '两端都要开启 UDP 监听才能直连');
  assert.equal(errorMessage({path: 'directLinks[0]', error: 'punch_no_reflector'}), '没有可用来打洞的中转（需要至少一台开启 UDP 监听的中转）');
});

test('reachability and routing: verdicts, candidate health, relay link measures, new codes and warnings', async () => {
  const h = await import('./meshHelpers.js');
  assert.deepEqual(['public', 'punchable', 'symmetric', 'no_udp', 'unknown', 'odd'].map(v => h.verdictBadge({verdict: v})[1]), ['公网可达', '可打洞', '对称型 NAT', 'UDP 不通', '未知', '未知']);
  assert.equal(h.verdictBadge(h.reachOf({nodes: {a: {summary: {reachability: null}}}}, 'a')), null);
  assert.equal(h.recommendedMode({verdict: 'public'}), 'public');
  assert.equal(h.recommendedMode({verdict: 'unknown'}), 'site');
  assert.equal(h.recommendedMode(null), '');
  // entryHealth is a list; an empty one is nothing reported.
  const data = {nodes: {mac: {summary: {entryHealth: [{id: 'm1', state: 'active'}, {id: 'm2', state: 'failing', lastError: 'timeout', lastOkAt: 0}]}}}};
  const mac = {serverId: 'mac', entry: {mode: 'failover', candidates: [{id: 'm1', relay: 'toy'}, {id: 'm2', relay: 'lax'}]}}, members = [mac, {serverId: 'lax', name: 'LAX'}, {serverId: 'toy', name: 'TOY'}];
  assert.deepEqual(h.nodeAlerts(data, mac, members).map(a => [a.code, a.text]), [['mesh_standby_unhealthy', 'LAX 备用：超时']]);
  assert.deepEqual(h.nodeAlerts({nodes: {mac: {summary: {entryHealth: []}}}}, mac, members), []);
  assert.equal(h.candLine({state: 'failing', lastError: 'tls pin mismatch', lastOkAt: 0}), '连不上：TLS 证书指纹不符 · 从没连上过');
  // Relay links: the worse end; a negative cost (bias) is a cost, null is unreachable.
  const routing = (links) => ({summary: {routing: links === null ? null : {mode: 'dynamic', links, routes: []}}});
  const r = {nodes: {a: routing([{peer: 'b', state: 'up', rttMs: 3, jitterMs: 0.2, loss: 0, cost: -7}]), b: routing(null)}};
  assert.equal(h.costText(h.relayMetric(r, 'a', 'b')), '-7');
  assert.equal(h.costText({state: 'up', cost: null}), '不可用');
  assert.equal(h.relayMetric({nodes: {a: routing(null), b: routing(null)}}, 'a', 'b'), null);
  // Codes from the delivered controller.
  assert.equal(h.errorMessage({error: 'punch_needs_udp', message: 'x'}), '站点中转只能经 UDP 打洞接入或连接，传输方式请选 UDP');
  assert.equal(h.errorMessage({error: 'invalid_relay_mode'}), '中转类型只能是公网中转或站点中转');
  assert.equal(h.errorMessage({error: 'invalid_bias'}), '偏好须是 -10000～10000 的整数（毫秒）');
  assert.equal(h.errorMessage({error: 'invalid_stun'}), 'STUN 服务器须是“主机:端口”，例如 stun.example.com:3478，最多 8 个');
  assert.equal(h.errorMessage({path: 'relayLinks.pairs[3]', error: 'udp_not_listening'}), '两台站点中转都要开启 UDP 监听才能打洞连接');
  assert.equal(h.errorMessage({path: 'members[4].entry.candidates[0]', error: 'udp_not_listening'}), '要开启 UDP 监听才能经打洞接入站点中转');
  assert.equal(h.errorMessage({path: 'directLinks[0]', error: 'udp_not_listening'}), '两端都要开启 UDP 监听才能直连');
  for (const code of ['mesh_site_relay_needs_public_link', 'mesh_entry_needs_bootstrap', 'mesh_punch_impossible', 'mesh_standby_unhealthy', 'mesh_exit_inconsistent'])
    assert.ok(/[\u4e00-\u9fff]/.test(h.errorMessage({error: code, message: 'english'})), code);
  // Warnings: shaped like errors, read by path; missing on older controllers.
  const warned = {warnings: [{path: 'relayLinks.pairs[3]', error: 'mesh_punch_impossible', message: 'x'}]};
  assert.deepEqual(h.warningsUnder(warned, 'relayLinks.pairs[3]'), ['两端都是对称型 NAT，打洞基本不会成功，会一直经中转绕行']);
  assert.deepEqual(h.warningsUnder(warned, 'relayLinks.pairs[1]'), []);
  assert.deepEqual(h.warningsUnder({}, 'directLinks[0]'), []);
});

test('routes and gateway networks: rows from GET /mesh/routes, who gets each network', async () => {
  const { exportsOf, exportText, routeRows } = await import('./meshHelpers.js');
  const members = [{serverId: 'sg', name: 'SG'}, {serverId: 'home', name: 'Home'}, {serverId: 'toy', name: 'TOY'}, {serverId: 'mac', name: 'Mac'}];
  const h = (node, via, rttMs) => ({node, via, rttMs});
  // As the controller sends it: the source first (via '', rttMs null), the rest in applied order.
  const rows = routeRows({ok: true, routes: [
    {dst: 'toy', hops: [h('mac', '', null), h('toy', 'lan', 4)], rttMs: 4, stale: false},
    {dst: 'sg', hops: [h('mac', '', null), h('toy', 'lan', 4), h('sg', 'relay', 75.2)], rttMs: null, stale: true},
    {dst: 'home', hops: [h('mac', '', null), h('toy', 'lan', 4)], rttMs: null, stale: true}],
    subnets: [{subnet: '10.10.10.0/24', gateway: 'home', hops: [h('mac', '', null), h('toy', 'lan', 4), h('home', 'relay', 2.6)], rttMs: 6.6, stale: false}]}, 'mac', members);
  assert.deepEqual(rows.routes.map(r => [r.title, r.through, r.rtt, r.stale, r.broken]),
    [['SG', '经 TOY', '延迟未知', true, false], ['Home', '在 TOY 之后断开', '', true, true], ['TOY', '直达 · 局域网', '4 ms', false, false]]);
  assert.deepEqual(rows.subnets.map(r => [r.title, r.through, r.rtt, r.gateway]), [['10.10.10.0/24', '经 TOY → Home', '6.6 ms', 'home']]);
  // Plain ids as hops work too; an older controller gives no list.
  assert.equal(routeRows({routes: [{dst: 'sg', hops: ['mac', 'sg'], rttMs: 80}]}, 'mac', members).routes[0].through, '直达');
  assert.equal(routeRows(null, 'mac', members), null);
  assert.equal(routeRows({ok: true}, 'mac', members), null);
  // exports: by gateway and network; an empty list means nobody, a missing field says nothing.
  const data = {exports: [{gateway: 'home', subnet: '10.10.10.0/24', nodes: ['sg', 'mac']}, {gateway: 'home', subnet: '192.168.50.0/24', nodes: []}]};
  assert.equal(exportText(exportsOf(data, 'home', '10.10.10.0/24'), members), '下发给：SG、Mac');
  assert.equal(exportText(exportsOf(data, 'home', '192.168.50.0/24'), members), '没有机器能访问（要在访问规则的目标里写上这个网段）');
  assert.equal(exportsOf({}, 'home', '10.10.10.0/24'), null);
});

test('real-data review: events read in Chinese, routine records, cold standbys, moot punch warnings, routes, rule wording', async () => {
  const h = await import('./meshHelpers.js');
  const members = [{serverId: 'sg', name: 'SG'}, {serverId: 'toy', name: 'TOY'}, {serverId: 'home', name: 'Home'}, {serverId: 'vm', name: 'VM'},
    {serverId: 'mac', name: 'Mac', entry: {mode: 'failover', candidates: [{id: 'c1', relay: 'toy', transport: 'udp'}, {id: 'c2', relay: 'sg', transport: 'tls'}, {id: 'c3', relay: 'vm', transport: 'tcp'}]}},
    {serverId: 'win', name: 'Win', entry: {mode: 'failover', candidates: [{id: 'c1', relay: 'toy', transport: 'udp'}]}}];
  const mac = members[4];
  const keys = h.peerKeys({nodes: [{id: 'vm', name: 'VM', publicKey: 'ZXMTabcdefghijklmnopWgU=', controlKey: '7q/1abcdefghijklmnop9ZzM='}]});
  const ev = (kind, detail) => h.eventDetail({kind, detail}, mac, members, keys);
  assert.equal(ev('control.error', 'peer(7q/1…9ZzM) - Failed to send handshake initiation: use of closed network connection (7 similar not shown since the last one)'),
    '向 VM 发起握手失败：连接已经关闭（之后 1 分钟内还有 7 条相同的没有记录）');
  assert.equal(ev('route.changed', 'toy: toy -> home (better)'), '到 TOY：直达 → 经 Home（新线路更快）');
  assert.equal(ev('route.changed', 'toy: none -> toy (better)'), '到 TOY：无路可走 → 直达（新线路更快）');
  assert.equal(ev('owner.changed', 'mac -> toy seq=43'), 'Mac 现在经 TOY 接入');
  assert.equal(ev('config.applied', 'revision 52'), '第 52 版');
  assert.equal(ev('start', 'obmesh0 up, revision 38, 6 peers, 0 learned addresses restored'), '第 38 版配置，6 个对端');
  assert.equal(ev('path.ready', 'c3'), '第 3 个入口（VM · TCP）');
  assert.equal(ev('path.ready', 'link:sg'), '到 SG 的中转链路');
  assert.equal(ev('entry.switch', 'c2 -> c1: sustained degradation or stable preferred path'), '第 2 个入口（SG · TLS） → 第 1 个入口（TOY · UDP）：在用的入口持续变差，或排在前面的入口已经稳定');
  assert.equal(ev('entry.prepare', 'c2 seq=42: active path failed'), '第 2 个入口（SG · TLS）：在用的入口断开');
  assert.equal(ev('entry.committed', 'c1 seq=43'), '第 1 个入口（TOY · UDP）');
  assert.equal(ev('entry.flapping', 'at least 3 entry switches in 5 minutes'), '5 分钟内切换了至少 3 次入口');
  assert.equal(ev('direct.chosen', 'toy via public 203.0.113.4:51998, 2.3 ms'), '和 TOY 经公网 203.0.113.4:51998，2.3 ms');
  assert.deepEqual(['config.applied', 'owner.changed', 'control.error', 'path.ready', 'entry.flapping', 'start'].map(h.eventLabel), ['应用配置', '接入点变更', '隧道报错', '线路可用', '入口频繁切换', '组网服务启动']);
  const closed = {kind: 'control.error', detail: 'peer(x…y) - Failed to send data packets: use of closed network connection'};
  assert.equal(h.eventTone('control.error'), 'bad');
  // Routine: a standby's trial getting through, hot or cold, and the tunnel noting its connection closed as the trial
  // ends; not the entry in use getting ready, not a link between relays, never a failure.
  const health = {c1: {id: 'c1', state: 'active'}, c2: {id: 'c2', state: 'ready'}, c3: {id: 'c3', state: 'untested'}};
  assert.deepEqual([closed, {kind: 'path.ready', detail: 'c3'}, {kind: 'path.ready', detail: 'c2'}, {kind: 'path.ready', detail: 'c1'}, {kind: 'path.ready', detail: 'link:sg'},
    {kind: 'path.ready', detail: 'direct:win'}, {kind: 'owner.ack', detail: 'sg member=mac seq=3'}, {kind: 'path.failed', detail: 'c3: probe timeout'},
    {kind: 'control.error', detail: 'peer(x…y) - Failed to send handshake initiation: write udp: i/o timeout'}].map(e => h.routineEvent(e, mac, health)),
  [true, true, true, false, false, true, true, false, false]);
  // A cold standby: tried now and then.
  assert.equal(h.candLine({state: 'untested', lastOkAt: 1000 - 120000}, 1000), '冷备 · 定时试连，上次成功 2 分钟前');
  assert.equal(h.candLine({state: 'untested'}, 1000), '冷备 · 定时试连');
  // The same split into a state and the rest, for the entry lists.
  assert.deepEqual(h.candParts({state: 'untested'}, 1000), {label: '冷备', detail: '定时试连'});
  assert.deepEqual(h.candParts({state: 'failing', lastError: 'tls pin mismatch', lastOkAt: 0}), {label: '连不上', detail: 'TLS 证书指纹不符 · 从没连上过'});
  assert.deepEqual(h.candParts(null), {label: '', detail: ''});
  // Punch warnings that the live state contradicts are left out: through, or both behind one NAT.
  const data = {members, directLinks: [{a: 'mac', b: 'win', transport: 'udp'}],
    nodes: {mac: {summary: {direct: {win: {state: 'direct', via: 'lan'}}, reachability: {verdict: 'symmetric', publicAddr: '198.51.100.7:51999'}, entryHealth: [{id: 'c1', relay: 'toy', state: 'active', via: 'lan'}]}},
      win: {summary: {reachability: {verdict: 'symmetric', publicAddr: '198.51.100.7:1024'}, entryHealth: [{id: 'c1', relay: 'toy', state: 'punching', via: ''}]}},
      toy: {summary: {reachability: {verdict: 'symmetric', publicAddr: '198.51.100.7:45997'}}}},
    warnings: [{path: 'directLinks[0]', error: 'mesh_punch_impossible', message: 'x'}, {path: 'members[4].entry.candidates[0]', error: 'mesh_punch_impossible', message: 'y'},
      {path: 'members[4].listen.udp', error: 'mesh_udp_port_shared', message: '建议换端口'}]};
  assert.deepEqual(h.warningItems(data, 'members[4]'), [{code: 'mesh_udp_port_shared', text: '建议换端口'}]);
  assert.deepEqual(h.warningsUnder(data, 'directLinks[0]'), []);
  // Different exits and nothing through: the prediction stands, in the entry's own words.
  data.nodes.toy.summary.reachability.publicAddr = '203.0.113.9:45997';
  data.warnings.push({path: 'members[5].entry.candidates[0]', error: 'mesh_punch_impossible', message: 'z'});
  assert.deepEqual(h.warningsUnder(data, 'members[5]'), ['两端都是对称型 NAT，打洞基本不会成功，这个入口很可能用不上']);
  // Routes: a relay's unmeasured entry hop (0 ms) takes the member's measure, and says LAN.
  const rows = h.routeRows({routes: [{dst: 'mac', hops: [{node: 'toy', via: '', rttMs: null}, {node: 'mac', via: 'entry', rttMs: 0}], rttMs: 0}]}, 'toy', members,
    {nodes: {mac: {summary: {rttMs: 7.1, entryHealth: [{id: 'c1', relay: 'toy', state: 'active', via: 'lan'}]}}}});
  assert.deepEqual([rows.routes[0].through, rows.routes[0].rtt], ['直达 · 局域网', '7.1 ms']);
  assert.equal(h.routeRows({routes: [{dst: 'mac', hops: [{node: 'toy', via: '', rttMs: null}, {node: 'mac', via: 'entry', rttMs: 0}], rttMs: 0}]}, 'toy', members).routes[0].rtt, '延迟未知');
  // Rules: machines counted as 台; the same machines on both sides reach each other.
  const all = members.map(m => m.serverId);
  const mutual = {src: {members: all, tags: [], prefixes: []}, dst: {members: [...all].reverse(), tags: [], subnets: []}, proto: 'icmp', ports: []};
  assert.equal(h.ruleSentence(mutual, members), '允许 SG、TOY 等 6 台 之间互相访问 ICMP（ping）');
  assert.equal(h.ruleShort(mutual, members), 'SG、TOY 等 6 台 互通 · ping');
  assert.equal(h.ruleSentence({src: {members: ['home']}, dst: {members: ['mac'], subnets: []}, proto: 'tcp', ports: [[22, 22]]}, members), '允许 Home 访问 Mac 的 TCP 22');
  assert.equal(h.sideText({members: ['sg'], tags: ['a', 'b'], prefixes: ['10.0.0.0/24']}, members, 'prefixes'), 'SG、标签 a 等 4 项');
});

test('rules for all members: sides, sentences and the board label; a side with nothing covers nobody', async () => {
  const h = await import('./meshHelpers.js');
  const members = [{serverId: 'a', name: 'A'}, {serverId: 'b', name: 'B'}];
  const all = {all: true, members: [], tags: [], prefixes: []}, allDst = {all: true, members: [], tags: [], subnets: []};
  const ping = {src: all, dst: allDst, proto: 'icmp', ports: []};
  assert.equal(h.ruleShort(ping, members), '所有成员 → 所有成员 · ping');
  assert.equal(h.ruleSentence(ping, members), '允许 所有成员 访问 所有成员 的 ICMP（ping）');
  assert.equal(h.mutualRule(ping), false);
  // Networks still add to all members.
  assert.equal(h.sideText({...allDst, subnets: ['10.10.10.0/24']}, members, 'subnets'), '所有成员、10.10.10.0/24');
  assert.deepEqual(h.sideParts(all, members, 'prefixes'), [{kind: 'all', value: 'all', text: '所有成员'}]);
  assert.equal(h.ruleShort({src: {members: ['a'], tags: [], prefixes: []}, dst: allDst, proto: 'tcp', ports: [[22, 22]]}, members), 'A → 所有成员 · TCP 22');
  // Nothing chosen and not all: the rule covers nobody, and says so.
  assert.equal(h.sideText({members: [], tags: [], prefixes: []}, members, 'prefixes'), '（未选择）');
  assert.deepEqual([h.sideEmpty({all: false, members: []}), h.sideEmpty(all), h.sideEmpty({members: ['a']})], [true, false, false]);
});

test('event rows: repeats with their first time, a run of route changes as one row, the original kept aside', async () => {
  const h = await import('./meshHelpers.js');
  const members = [{serverId: 'berlin', name: 'Berlin'}, {serverId: 'home', name: 'Home'}, {serverId: 'sg', name: 'SG'},
    {serverId: 'mac', name: 'Mac', entry: {candidates: [{id: 'c1', relay: 'home', transport: 'udp'}, {id: 'c3', relay: 'sg', transport: 'tcp'}]}}];
  const mac = members[3], H = 3600000, now = 10 * H;
  // The controller folds repeats: repeat and firstAt.
  const [one] = h.eventRows([{kind: 'path.ready', detail: 'c3', at: now, firstAt: now - 2 * H, repeat: 37}], mac, members);
  assert.deepEqual([one.label, one.text, one.repeat, one.firstAt, one.raw], ['线路可用', '第 2 个入口（SG · TCP）', 37, now - 2 * H, '']);
  // Two that read the same (sequence numbers left out) fold too, their counts added.
  const same = h.eventRows([{kind: 'entry.committed', detail: 'c1 seq=68', at: now}, {kind: 'entry.committed', detail: 'c1 seq=67', at: now - H, repeat: 2, firstAt: now - 3 * H}], mac, members);
  assert.deepEqual(same.map(r => [r.text, r.repeat, r.firstAt]), [['第 1 个入口（Home · UDP）', 3, now - 3 * H]]);
  // A relay flapping between two ways to Berlin, newest first: one row, its changes inside.
  const flips = [['home', 'berlin'], ['berlin', 'home'], ['home', 'berlin'], ['berlin', 'home']].map(([a, b], i) => ({kind: 'route.changed', detail: `berlin: ${a} -> ${b} (better)`, at: now - i * 60000}));
  const rows = h.eventRows([...flips, {kind: 'route.changed', detail: 'sg: sg -> none (down)', at: now - 5 * 60000}, {kind: 'config.applied', detail: 'revision 9', at: now - H},
    {kind: 'route.changed', detail: 'home: none -> home (better)', at: now - 2 * H}], null, members);
  assert.equal(rows.length, 3);
  assert.deepEqual([rows[0].label, rows[0].text, rows[0].repeat, rows[0].items.length, rows[0].tone, rows[0].firstAt],
    ['换路', '到 Berlin：直达 ⇄ 经 Home 来回 4 次，现在直达；到 SG：直达 → 无路可走', 5, 5, 'bad', now - 5 * 60000]);
  assert.equal(rows[0].items[0].text, '到 Berlin：经 Home → 直达（新线路更快）');
  // One route change alone stays a row of its own.
  assert.deepEqual([rows[2].text, rows[2].items], ['到 Home：无路可走 → 直达（新线路更快）', undefined]);
});

test('every event kind the data plane and the controller record reads in Chinese, with names, without ids', async () => {
  const h = await import('./meshHelpers.js');
  const id = n => `${n}0000000-1111-2222-3333-444444444444`;
  const members = [{serverId: id(1), name: 'Berlin'}, {serverId: id(2), name: 'EdgeES', entry: {candidates: [{id: 'c1', relay: id(1), transport: 'udp'}]}}, {serverId: 'local', name: 'KS-MAIN'}];
  const node = members[1], keys = h.peerKeys({nodes: [{id: id(1), name: 'Berlin', publicKey: 'E0pTabcdefghijklmnopSN0k='}]});
  const v = (kind, detail, m = node) => { const x = h.eventView({kind, detail}, m, members, keys); return [x.label, x.text, x.raw ? 'raw' : '']; };
  const cases = [
    ['control.error', 'peer(E0pT…SN0k) - Failed to send handshake initiation: write udp 0.0.0.0:51999->[::ffff:192.0.2.203]:51999: address ::ffff:192.0.2.203: non-IPv4 address',
      ['隧道报错', '向 Berlin 发起握手失败：地址 192.0.2.203 被写成了 IPv6 形式（::ffff:192.0.2.203），从 IPv4 端口发不出去', 'raw']],
    ['control.error', 'peer(zzzz…zzzz) - Failed to derive keypair: bad', ['隧道报错', '和 一个对端 协商密钥失败', 'raw']],
    ['path.error', `${id(1)}: data warm: write udp 0.0.0.0:51999->[::ffff:192.0.2.203]:51999: address ::ffff:192.0.2.203: non-IPv4 address`,
      ['线路出错', '准备到 Berlin 的线路时出错：地址 192.0.2.203 被写成了 IPv6 形式（::ffff:192.0.2.203），从 IPv4 端口发不出去', 'raw']],
    ['path.error', `${id(1)}: punch: sendmsg: no buffer space available`, ['线路出错', '向 Berlin 打洞时出错：系统发送缓冲区已满', 'raw']],
    ['path.error', `${id(1)}: dial: something odd`, ['线路出错', '连接 Berlin 的指定地址失败', 'raw']],
    ['path.failed', `direct:${id(1)}: punch timeout`, ['线路不通', '和 Berlin 的直连线路：打洞超时', '']],
    ['path.failed', 'gone1: probe timeout', ['线路不通', '一个已删除的入口：探测超时', '']],
    ['path.ready', `direct:${id(1)}/next`, ['线路可用', '和 Berlin 的更快的直连线路', '']],
    ['path.security', 'TLS peer verification failed: x509', ['证书不符', '对方的 TLS 证书和配置里的不一致，没有连上', 'raw']],
    ['owner.ack', `${id(1)} member=local seq=1`, ['接入确认', 'Berlin 已确认 KS-MAIN 经 EdgeES 接入', '']],
    ['owner.unconfirmed', `${id(1)} member=${id(2)} seq=10: lease expired without ACK`, ['接入未确认', 'Berlin 一直没有确认 EdgeES 经 EdgeES 接入', '']],
    ['owner.conflict', id(2), ['接入冲突', 'EdgeES 的接入点有冲突：另一台中转也说它经自己接入，等它重新确认', '']],
    ['owner.error', `${id(2)} seq=4: commit failed: no path to peer: use of closed network connection`, ['接入记录出错', '让 EdgeES 经 EdgeES 接入失败：连接已经关闭', 'raw']],
    ['owner.error', `${id(2)} seq=4 refresh: stale ownership notice`, ['接入记录出错', '续期 EdgeES 的接入失败', 'raw']],
    ['owner.changed', `local -> ${id(1)} seq=1`, ['接入点变更', 'KS-MAIN 现在经 Berlin 接入', '']],
    ['entry.unavailable', 'no ready entry', ['没有可用入口', '没有可用的入口', '']],
    ['entry.prepare', 'c1 seq=12: initial', ['准备切换入口', '第 1 个入口（Berlin · UDP）：首次选定', '']],
    ['state.error', 'open /var/lib/x: permission denied', ['状态没保存', '入口状态没能保存到磁盘：系统不允许（权限或防火墙限制）', 'raw']],
    ['route.error', 'route update failed', ['系统路由出错', '更新系统路由失败：路由更新失败', '']],
    ['route.error', 'exit status 2: RTNETLINK answers: File exists', ['系统路由出错', '更新系统路由失败', 'raw']],
    ['routing.error', `advertisement from ${id(1)}: advert link is invalid`, ['选路出错', '收到 Berlin 的路由通告有误，已忽略', 'raw']],
    ['routing.error', 'advertisement exceeds 1200 bytes (compact 1100)', ['选路出错', '要通告的路由太多，超过了大小上限，有的路由没有通告出去', 'raw']],
    ['route.skipped', "gateway subnets [10.10.10.0/24 10.20.0.0/16] overlap this machine's networks; not routed into the mesh", ['网段没走组网', '网关网段 10.10.10.0/24、10.20.0.0/16 和本机所在的网络重叠，本机不经组网访问它们', '']],
    ['route.skipped', "gateway subnets [10.10.10.0/24] no longer overlap this machine's networks; routed into the mesh", ['网段改走组网', '网关网段 10.10.10.0/24 不再和本机所在的网络重叠，已改为经组网访问', '']],
    ['direct.better', `${id(1)} via lan 10.0.0.5:51999: 0.8 ms, link 12.4 ms`, ['发现更快的直连', '和 Berlin 经局域网 10.0.0.5:51999 只要 0.8 ms（现在的线路 12.4 ms），准备换过去', '']],
    ['direct.better', `${id(1)}: not ready`, ['放弃更快的直连', '和 Berlin 的更快线路没能准备好，继续用现在的线路', '']],
    ['direct.switched', `${id(1)} via lan udp://10.0.0.5:51999`, ['换用更快的直连', '和 Berlin 换用经局域网 10.0.0.5:51999 的直连', '']],
    ['config.error', 'revision 53 not applied: listen udp :51999: bind: address already in use', ['配置没生效', '第 53 版配置没能应用：端口已被别的程序占用', 'raw']],
    ['config.error', 'waiting for config.json', ['配置没生效', '还没有收到配置', '']],
    ['config.applied', 'revision 52, the last applied; revision 53 not applied: something', ['沿用上一版配置', '第 53 版没能应用，先沿用第 52 版', 'raw']],
    ['firewall.restored', 'OBMESH-FWD jump in FORWARD', ['防火墙规则已恢复', '组网的防火墙规则被别的程序改动了，已经自动恢复', 'raw']],
    ['start', 'obmesh0 up, revision 65, 5 peers, 2 learned addresses restored', ['组网服务启动', '第 65 版配置，5 个对端，恢复了 2 个记下的对端地址', '']],
    ['upgrade.failed', 'data plane upgrade to build c4ec3ff88e29 did not get ready; rolled back', ['组网服务升级失败，已回退', '新版本启动后没有就绪，已回到原来的版本', 'raw']],
    ['link.suppressed', `${id(1)}: penalty 1200`, ['线路暂停使用', '到 Berlin 的中转链路反复断开，暂停用于选路', '']],
    ['route.changed', `${id(9)}: ${id(1)} -> none (down)`, ['换路', '到 已移出组网的机器：经 Berlin → 无路可走（原线路断开）', '']],
    ['something.new', 'raw words', ['其他事件', '', 'raw']],
  ];
  for (const [kind, detail, want] of cases) assert.deepEqual(v(kind, detail), want, `${kind}: ${detail}`);
  // No id, key or sequence number in any text.
  for (const [kind, detail] of cases) assert.doesNotMatch(v(kind, detail)[1], /[0-9a-f]{8}-|seq=|E0pT|\bc1\b/, kind);
});

test('a member whose mesh service is gone: its state, its rollout step, its event and the reinstall errors', async () => {
  const { eventView, FINISHED, nodeState, PROGRESS, reinstallErrorText } = await import('./meshHelpers.js');
  const data = {nodes: {a: {state: 'missing', error: 'mesh_service_missing'}}};
  assert.deepEqual(nodeState(data, 'a'), ['bad', '组网服务不见了']);
  assert.equal(meshCodeText('mesh_service_missing'), '组网服务不见了（可能被卸载或重装过）');
  // Skipped by a rollout, not failed: it counts as finished.
  assert.deepEqual(PROGRESS.missing, ['warn', '组网服务不见了，已跳过']);
  assert.ok(FINISHED.has('missing'));
  // A node whose data plane cannot read the configs is skipped and does not hold back the rollout.
  assert.deepEqual(PROGRESS.outdated, ['warn', '组网服务版本旧，已跳过，更新后补发']);
  assert.ok(FINISHED.has('outdated'));
  const gone = eventView({kind: 'mesh.missing', detail: ''}, {serverId: 'a', name: 'EdgeES'}, []);
  assert.equal(gone.label, '组网服务不见了');
  assert.equal(gone.tone, 'bad');
  assert.match(gone.text, /重新安装组网服务/);
  assert.equal(gone.raw, '');
  const back = eventView({kind: 'mesh.reinstall', detail: ''}, {serverId: 'a', name: 'EdgeES'}, []);
  assert.deepEqual([back.label, back.tone], ['重新安装组网服务', 'ok']);
  // The controller's own words: which member's key it reported, kept whole.
  const copied = eventView({kind: 'mesh.key_conflict', detail: '上报的端到端密钥和「Berlin」的相同，没有采用。多半是从那台机器的系统快照或模板克隆出来的'},
    {serverId: 'a', name: 'EdgeES'}, []);
  assert.deepEqual([copied.label, copied.tone], ['上报了其他成员的密钥', 'bad']);
  assert.match(copied.raw, /「Berlin」/);
  assert.equal(reinstallErrorText('node_offline'), '节点离线，连上后才能重新安装');
  assert.equal(reinstallErrorText('mesh_member_not_found'), '这台还不是已应用的网格成员，不能重新安装组网服务');
  assert.equal(reinstallErrorText('mesh_apply_running'), '正在应用别的更改，等它完成后再重新安装');
  assert.equal(reinstallErrorText('capability_disabled'), '这台服务器的节点关闭了命令执行，没法远程安装组网服务');
  assert.equal(reinstallErrorText('node_update_required'), '节点版本过旧，先更新节点再重新安装');
  // mesh.install's own errors read as everywhere else.
  assert.equal(reinstallErrorText('mesh_install_invalid'), '');
  assert.equal(meshCodeText('mesh_install_invalid'), '节点返回的组网密钥不正确，安装没有完成');
});

test('older peer events name the peer instead of showing the original', async () => {
  const { eventView } = await import('./meshHelpers.js');
  const members = [{serverId: 'berlin', name: 'Berlin'}];
  assert.deepEqual([eventView({kind: 'peer.reconnect', detail: 'berlin'}, {}, members).text, eventView({kind: 'peer.reconnect', detail: 'berlin'}, {}, members).raw], ['和 Berlin 重连', '']);
  assert.equal(eventView({kind: 'peer.down', detail: 'berlin: i/o timeout'}, {}, members).text, '和 Berlin 断开：超时');
});

test('a line\'s record reads the controller summary: availability in percent, latency, downs and the time with data', async () => {
  const { availabilityText, lineFacts, lineEnd, trendRttText } = await import('./meshHelpers.js');
  // availability is 0–100, never shown as 100% while some time was down.
  assert.equal(availabilityText(95.42, 3960), '95.4%');
  assert.equal(availabilityText(99.5, 432), '99.5%');
  assert.equal(availabilityText(99.99, 9), '99.99%');
  assert.equal(availabilityText(100, 2), '> 99.99%');
  assert.equal(availabilityText(100, 0), '100%');
  assert.equal(availabilityText(null, 0), '');
  const s = {availability: 95.42, rttMs: {avg: 151.7, min: 138.2, max: 212.9, p95: 171.4}, lossRate: 0.006, downs: 2, downSeconds: 450, knownSeconds: 86400};
  assert.deepEqual(lineFacts(s, false, '24h'), [['可用率', '95.4%', 'warn'], ['平均延迟', '152 ms'], ['P95 延迟', '171 ms'], ['最小 / 最大', '138 ms / 213 ms'], ['丢包', '0.6%', ''], ['断开', '2 次，共 7 分钟', 'bad']]);
  // Part of the range without data: availability is over the rest, and said so.
  assert.deepEqual(lineFacts({...s, knownSeconds: 21600}, false, '24h').at(-1), ['有记录的时间', '6 小时 / 1 天']);
  // Nothing known: no availability and no downs, not "never down".
  assert.deepEqual(lineFacts({availability: null, rttMs: {avg: null, min: null, max: null, p95: null}, lossRate: null, downs: 0, downSeconds: 0, knownSeconds: 0}, false, '1h'), []);
  // A cold standby: tries, failed tries, and the time it was tried.
  assert.deepEqual(lineFacts({availability: 83.33, rttMs: {avg: 167.2, min: 160, max: 185, p95: 183}, lossRate: 0, downs: 1, downSeconds: 60, knownSeconds: 360}, true, '24h').filter(([l]) => /试连|抽测/.test(l)),
    [['试连成功率', '83.3%', 'warn'], ['试连失败', '1 次', 'warn'], ['有抽测的时间', '6 分钟']]);
  // An end without a local address (a standby entry not connected) says only where it goes.
  assert.equal(lineEnd({local: '', public: '', remote: '198.51.100.6:51999', dialer: true}, 'udp').route, '经 UDP 连到 198.51.100.6:51999');
  const members = [{serverId: 'lax', name: 'LAX'}, {serverId: 'berlin', name: 'Berlin'}];
  assert.equal(trendRttText({kind: 'entry', peers: ['lax']}, members), '到 LAX（入口）的延迟');
  assert.equal(trendRttText({kind: 'links', peers: ['berlin', 'lax']}, members), '到 Berlin、LAX 的中转链路平均延迟');
  assert.equal(trendRttText({kind: 'links', peers: []}, members), '到其他中转的链路平均延迟');
});

test('networks written by hand are checked as the controller reads them: rule networks and the mesh prefix', async () => {
  const {ruleNet, meshPrefixProblem} = await import('./meshHelpers.js');
  // A rule takes a network or one address, the address as its own /32.
  assert.deepEqual(ruleNet(' 10.0.0.0/24 '), {net: '10.0.0.0/24', problem: ''});
  assert.deepEqual(ruleNet('10.0.0.5'), {net: '10.0.0.5/32', problem: ''});
  assert.deepEqual(ruleNet('10.0.0.5/32'), {net: '10.0.0.5/32', problem: ''});
  assert.deepEqual(ruleNet('10.0.0.5/24'), {net: '', problem: '10.0.0.5/24 不是网段的起始地址，应写成 10.0.0.0/24'});
  for (const bad of ['010.0.0.0/8', '10.0.0.0/33', '10.0.0.256', '10.0.0', 'lan', '10.0.0.0/'])
    assert.equal(ruleNet(bad).problem, '写成 IP 或网段，例如 10.0.0.5 或 10.0.0.0/24', bad);
  // The mesh prefix: a network of /8 to /22, no address bits past the prefix.
  assert.equal(meshPrefixProblem('100.96.0.0/16'), '');
  assert.equal(meshPrefixProblem('10.0.0.0/8'), '');
  assert.equal(meshPrefixProblem('100.96.0.1/16'), '100.96.0.1/16 不是网段的起始地址，应写成 100.96.0.0/16');
  assert.equal(meshPrefixProblem('100.96.0.0/24'), '前缀长度在 8～22 之间，例如 100.96.0.0/16');
  assert.equal(meshPrefixProblem('10.0.0.0/7'), '前缀长度在 8～22 之间，例如 100.96.0.0/16');
  for (const bad of ['100.96.0.0', '010.96.0.0/16', '100.96.0.0/40', ''])
    assert.equal(meshPrefixProblem(bad), '写成网段，例如 100.96.0.0/16', bad);
});

test('a re-dialed TCP path reads as which path, why and the ports it moved between', async () => {
  const { eventView, eventTone } = await import('./meshHelpers.js');
  const members = [{serverId: 'home', name: '家'}, {serverId: 'sg', name: 'SG'}];
  const member = {serverId: 'win', name: 'Windows', entry: {candidates: [{id: 'c1', relay: 'home', transport: 'udp'}, {id: 'c2', relay: 'sg', transport: 'tcp'}]}};
  const link = eventView({kind: 'link.redial', detail: 'link:sg reason=rtt rtt=157.1ms baseline=74.0ms slow=100% port=49178->48046'}, member, members);
  assert.deepEqual([link.label, link.text, link.raw, link.tone], ['换端口重连', '到 SG 的中转链路持续变差（延迟 157 ms，平时 74 ms），已换个端口重新连接（49178 → 48046）', '', 'warn']);
  const entry = eventView({kind: 'link.redial', detail: 'c2 relay=sg reason=slow rtt=76.0ms baseline=74.0ms slow=18% port=50111->50112'}, member, members);
  assert.equal(entry.text, '第 2 个入口（SG · TCP）持续变差（18% 的探测变慢或超时，平时 74 ms），已换个端口重新连接（50111 → 50112）');
  const failed = eventView({kind: 'link.redial.failed', detail: 'link:sg reason=rtt rtt=160.0ms baseline=74.0ms slow=95% error=dial tcp 192.0.2.79:52000: i/o timeout'}, member, members);
  assert.deepEqual([failed.label, failed.text, failed.raw], ['换端口重连失败', '到 SG 的中转链路持续变差（延迟 160 ms，平时 74 ms），换端口重连没有成功：超时，继续用原来的连接', '']);
  assert.equal(eventTone('link.redial.failed'), 'bad');
  const odd = eventView({kind: 'link.redial.failed', detail: 'link:sg reason=rtt rtt=160.0ms baseline=74.0ms slow=95% error=something new'}, member, members);
  assert.equal(odd.raw, 'link:sg reason=rtt rtt=160.0ms baseline=74.0ms slow=95% error=something new');
});

test('a relay link is in use when either end routes through it; without routes the line says', () => {
  const routes = list => ({summary: {routing: {mode: 'dynamic', links: [], routes: list.map(([dest, nextHop]) => ({dest, nextHop}))}}});
  const data = {nodes: {sg: routes([['home', 'home'], ['vline', 'home']]), home: routes([['sg', 'sg'], ['vline', 'vline']]), vline: routes([['sg', 'home']]), old: {summary: {routing: {links: [], routes: []}}}}};
  const routed = routedLinks(data);
  assert.deepEqual([...routed.reporting].sort(), ['home', 'sg', 'vline']);
  assert.equal(relayInUse(routed, null, 'sg', 'home'), true);
  assert.equal(relayInUse(routed, null, 'vline', 'home'), true, 'home sends to vline over it');
  assert.equal(relayInUse(routed, null, 'sg', 'vline'), false, 'both go round by home');
  // Neither end reports routes (an older node, static routing): the line's state and role.
  assert.equal(relayInUse(routed, null, 'old', 'x'), null);
  assert.equal(relayInUse(routed, {state: 'up', role: 'active'}, 'old', 'x'), true);
  assert.equal(relayInUse(routed, {state: 'standby', role: 'standby'}, 'old', 'x'), false);
  assert.equal(relayInUse(routed, {state: 'down', role: 'active'}, 'old', 'x'), null);
});

test('signature, end-to-end and entry security events read in Chinese with what to do', async () => {
  const h = await import('./meshHelpers.js');
  const members = [{serverId: 'r1', name: 'Berlin'}, {serverId: 'r2', name: 'LAX'}, {serverId: 'm1', name: '公司'}, {serverId: 'm2', name: '家'}];
  const keys = h.peerKeys({nodes: [{id: 'm2', name: '家', e2eKey: 'E2Eeabcdefghijklmnop9ZzM='}]});
  const v = (kind, detail) => { const x = h.eventView({kind, detail}, members[2], members, keys); return [x.label, x.text, x.raw ? 'raw' : '', x.tone]; };

  assert.deepEqual(v('sign.rejected', 'offer in the name of m2 from r1: bad signature (3 more since the last one)'),
    ['签名校验失败', 'Berlin 转来的以 家 名义的打洞地址签名不对，已丢弃（之后 1 分钟内还有 3 条）。偶尔一次可能是对方刚重装、新密钥还没下发到这里；持续出现说明转发它的机器可能被篡改，检查那台机器。', '', 'bad']);
  assert.match(v('sign.rejected', 'commit in the name of m1 from m1: bad signature')[1], /^公司 发来的接入声明签名不对，已丢弃。/);
  assert.match(v('sign.rejected', 'owner notice in the name of m1/r2 from r1: bad signature')[1], /^Berlin 转来的公司 经 LAX 接入的接入通告签名不对/);
  assert.match(v('sign.rejected', 'routing advertisement in the name of its origin from r2: bad signature')[1], /^LAX 转来的路由通告签名不对/);
  assert.match(v('sign.rejected', 'conflict claim in the name of m1 from r1: bad signature')[1], /^Berlin 回复的接入冲突附带的声明不是本机签的，已忽略/);
  assert.equal(v('sign.rejected', 'something new')[2], 'raw');

  assert.deepEqual(v('owner.claim_expired', '2 stored claims without valid signatures dropped'),
    ['旧接入记录作废', '作废了 2 条没有有效签名的接入记录，相关成员会自动重新接入。升级后或成员重装后出现一次属于正常。', '', 'off']);
  assert.deepEqual(v('e2e.exhausted', 'all 64 end-to-end sessions in use in the last 2m0s; packets for m2 dropped (5 refused since the last report)'),
    ['端到端会话已满', '64 个端到端会话都在用，发往 家 的包已丢弃，不会改用明文，上次记录以来又拒绝了 5 次。经常出现说明这台机器同时通信的对端太多。', '', 'bad']);
  assert.match(v('e2e.exhausted', 'more than 20 new end-to-end sessions a second; packets for m2 dropped')[1], /^每秒新建的端到端会话超过 20 个，发往 家 的包已丢弃/);
  assert.deepEqual(v('e2e.error', 'peer(E2Ee…9ZzM) - Failed to send handshake initiation: no buffer space available').slice(0, 2),
    ['端到端加密出错', '向 家 发起握手失败：系统发送缓冲区已满']);
  assert.match(v('e2e.error', 'session with m2: something odd')[1], /^建立和 家 的端到端会话失败，发往它的包会被丢弃$/);

  const suspect = 'r1 answers probes but 3 end-to-end probes through it to r2 in a row went unanswered; ';
  assert.deepEqual(v('entry.suspect', suspect + 'moving off it'),
    ['入口疑似丢包', '入口 Berlin 回应探测，但经它发往 LAX 的端到端探测连续 3 次没有回应，可能只丢业务流量。已换到备用入口。', '', 'bad']);
  assert.match(v('entry.suspect', suspect + 'reported only (fixed mode)')[1], /固定入口模式只提醒、不切换；请检查这台中转。$/);
  assert.equal(v('entry.suspect', suspect + 'reported only (fixed mode)')[3], 'warn');
  assert.match(v('entry.suspect', suspect + 'reported only: r2 is not a ready candidate to tell the entry from it')[1], /没有切换：LAX 不是就绪的备用入口/);

  assert.deepEqual(v('reach.mismatch', 'reflected [198.51.100.9:4000] not offered; relays saw 203.0.113.7 by r1,r2'),
    ['出口地址对不上', '反射器报的出口地址 198.51.100.9:4000 和中转在已认证线路上看到的对不上，没有用来打洞（Berlin、LAX 看到 203.0.113.7）。持续出现说明某个反射器谎报地址，或这台机器有多条出口。', '', 'warn']);
  assert.match(v('reach.mismatch', 'reflected [] not offered; relays saw 203.0.113.7 by r1; 203.0.113.8 by r2')[1], /^各中转看到的出口地址不一致（Berlin 看到 203\.0\.113\.7；LAX 看到 203\.0\.113\.8）/);

  assert.deepEqual(v('routing.unknown_link', 'r1 advertises links the mesh does not have, ignored: [m1 r2]'),
    ['通告了不存在的链路', 'Berlin 的路由通告里有组网里没有的链路 Berlin—公司、Berlin—LAX，已忽略，不会经它们选路。刚改过中转链路时偶尔出现属于正常；持续出现说明这台中转的配置没更新，或被篡改，检查那台机器。', '', 'warn']);
  assert.equal(v('routing.unknown_link', 'something new')[2], 'raw');
  assert.ok(h.SECURITY_EVENTS.includes('routing.unknown_link'));
  assert.deepEqual(v('routing.clock', "r2's clock is 2m5s off this relay's"),
    ['中转时钟不准', 'LAX 的时钟比这里快 2 分 5 秒。不影响选路和签名校验，但会让两边的日志和事件时间对不上，建议检查这两台机器的时间同步（NTP）。', '', 'warn']);
  assert.match(v('routing.clock', "r2's clock is -1h0m0s off this relay's")[1], /^LAX 的时钟比这里慢 1 小时。/);
  assert.match(v('routing.clock', "r2's clock is -1m30s off this relay's")[1], /^LAX 的时钟比这里慢 1 分 30 秒。/);
  assert.equal(v('routing.clock', 'odd')[2], 'raw');
});

test('a member shows its end-to-end sessions, rejected signatures and latest security event', async () => {
  const {securityStats} = await import('./meshHelpers.js');
  const members = [{serverId: 'r1', name: 'Berlin'}, {serverId: 'm1', name: '公司'}];
  const status = {counters: {droppedPlain: 1}, e2e: {sessions: 64, limit: 64, droppedExhausted: 4, droppedQueue: 0, droppedSource: 0, droppedBad: 2},
    security: {signing: {verified: 120, rejected: 5, byHop: {r1: 5}}, claims: {expired: 0}}};
  const events = [{kind: 'peer.up', detail: 'r1', at: 30}, {kind: 'sign.rejected', detail: 'offer in the name of m2 from r1: bad signature', at: 20},
    {kind: 'owner.claim_expired', detail: '1 stored claims without valid signatures dropped', at: 10}];
  const [e2e, signing, last] = securityStats(status, events, members[1], members);
  assert.deepEqual([e2e.label, e2e.value, e2e.cls, e2e.note, e2e.wide], ['端到端会话', '64 / 64', 'ob-warn', '丢弃 7 个包：会话已满 4、无法解密 2、未加密 1', true]);
  assert.deepEqual([signing.label, signing.value, signing.note, signing.title], ['验签拒绝', '5 次', '来自 Berlin 5 次', '已验证 120 次']);
  assert.deepEqual([last.label, last.value, last.at, last.noteCls, last.wide], ['最近安全事件', '签名校验失败', 20, 'ob-bad', true]);

  const quiet = securityStats({e2e: {sessions: 2, limit: 64}, security: {signing: {verified: 9, rejected: 0}, claims: {expired: 0}}}, [], members[1], members);
  assert.deepEqual(quiet.map(s => [s.value, s.note, s.wide]), [['2 / 64', '', false], ['0 次', '', false]]);
  // A node that reports neither shows none.
  assert.deepEqual(securityStats({}, [], members[1], members), []);
});

test('host firewalls a Linux node found, what they may not let through, and the automatic instance rules', async () => {
  const {autoRuleWords, firewallIssue, firewallKinds} = await import('./meshHelpers.js');
  assert.equal(firewallKinds('ufw,docker'), 'ufw · Docker');
  assert.equal(firewallKinds('iptables,firewalld,nftables'), 'iptables · firewalld · nftables');
  // macOS says "on"; nothing found or unknown kinds read as nothing.
  assert.equal(firewallKinds('on'), '');
  assert.equal(firewallKinds(''), '');
  assert.equal(firewallKinds('bogus'), '');
  assert.equal(firewallIssue({code: 'firewalld_zone', detail: 'work'}).text, '组网网卡在 firewalld 的 work 区域');
  assert.match(firewallIssue({code: 'firewalld_zone', detail: 'work'}).detail, /手动指定到 work 区域的，组网没有改动它/);
  assert.match(firewallIssue({code: 'nftables_failed', detail: 'inet filter input: Error'}).detail, /^inet filter input: Error。/);
  assert.match(firewallIssue({code: 'iptables_nft', detail: 'ip filter INPUT'}).detail, /（ip filter INPUT）/);
  assert.equal(firewallIssue({code: 'docker_failed', detail: 'x'}).text, '没能放行发往容器的组网连接');
  assert.deepEqual(firewallIssue({code: 'later_code', detail: 'd'}), {text: '主机防火墙：later_code', detail: 'd'});
  const missing = firewallIssue({code: 'firewall_tool_missing', detail: 'firewalld: firewall-cmd'});
  assert.equal(missing.text, '找不到 firewall-cmd，没能调整 firewalld');
  assert.match(missing.detail, /firewalld 看起来在用，但组网服务在 PATH 和 \/usr\/sbin、\/sbin 等目录里都找不到 firewall-cmd/);
  assert.equal(firewallIssue({code: 'firewall_tool_missing', detail: 'ufw: iptables'}).text, '找不到 iptables，没能调整 ufw');
  const members = [{serverId: 'gw', name: '网关机'}, {serverId: 'm2', name: 'EdgeHost'}];
  assert.deepEqual(autoRuleWords({gateway: 'gw', serverId: 'm2', port: 18970}, members), ['允许', '网关机', '访问', 'EdgeHost', 'TCP 18970']);
});
