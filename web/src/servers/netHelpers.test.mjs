import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as h from './netHelpers.js';

test('addresses: tags by scope and egress, interface kinds, exits that sit on no interface, the first few', () => {
  const reply = {interfaces: [
    {name: 'eth0', up: true, mac: '52:54:00:12:34:56', kind: 'ethernet', addrs: [{ip: '10.10.10.251', prefixLen: 24, family: 4, scope: 'private', egress: false},
      {ip: '203.0.113.5', prefixLen: 24, family: 4, scope: 'public', egress: true}, {ip: '2001:db8::10', prefixLen: 64, family: 6, scope: 'public', egress: false}]},
    {name: 'obmesh0', up: true, mac: '', kind: 'mesh', addrs: [{ip: '100.96.255.3', prefixLen: 24, family: 4, scope: 'mesh', egress: false}, {ip: '100.96.0.3', prefixLen: 16, family: 4, scope: 'mesh', egress: false}]},
    {name: 'tun0', up: false, mac: '', kind: 'tunnel', addrs: [{ip: '100.64.1.2', prefixLen: 10, family: 4, scope: 'cgnat'}, {ip: '169.254.3.4', prefixLen: 16, family: 4, scope: 'linklocal'}]},
    {name: 'empty', up: true, kind: 'other', addrs: []},
  ], publicIPv4: '198.51.100.7', publicIPv6: '', meshAddress: '100.96.0.3'};
  const list = h.addressList(reply);
  assert.deepEqual(list.interfaces.map(i => i.name), ['eth0', 'obmesh0', 'tun0'], 'an interface without addresses is left out');
  assert.equal(list.count, 7);
  const tags = list.interfaces.flatMap(i => i.addrs.map(a => `${a.text}=${a.tags.map(t => t[1]).join('+')}`));
  assert.deepEqual(tags, ['10.10.10.251/24=私有', '203.0.113.5/24=公网出口', '2001:db8::10/64=公网+IPv6', '100.96.0.3/16=', '100.96.255.3/24=控制', '100.64.1.2/10=CGNAT', '169.254.3.4/16=链路本地']);
  assert.equal(list.interfaces[2].up, false);
  assert.deepEqual(list.interfaces[1].addrs.map(a => a.control), [false, true], 'the control address last, marked');
  assert.deepEqual(h.addressList({...reply, meshAddress: ''}).interfaces[1].addrs.map(a => a.tags.length), [0, 0], 'without a mesh address there is no telling which is which');
  assert.deepEqual(list.exits, ['198.51.100.7'], 'an exit not on any interface (behind NAT) is listed on its own');
  assert.deepEqual(h.addressList({...reply, publicIPv4: '203.0.113.5'}).exits, []);
  assert.deepEqual(h.firstAddresses(list.interfaces, 4).map(i => `${i.name}:${i.addrs.length}`), ['eth0:3', 'obmesh0:1']);
  assert.equal(h.firstAddresses(list.interfaces, 4)[1].addrs[0].ip, '100.96.0.3');
  assert.deepEqual(h.firstAddresses(list.interfaces, 2).map(i => `${i.name}:${i.addrs.length}`), ['eth0:2']);
  assert.equal(h.firstAddresses(list.interfaces, 0), list.interfaces);
  assert.deepEqual(h.addressList(null), {interfaces: [], exits: [], proxy: '', line: '', count: 0});
  // Behind a proxy that sends traffic abroad elsewhere: the line services in China see, and the exit.
  const split = h.addressList({...reply, publicIPv4: '192.0.2.34', publicIPv4Proxy: '192.0.2.92'});
  assert.deepEqual([split.exits, split.proxy, split.line], [['192.0.2.34'], '192.0.2.92', '192.0.2.34']);
  assert.deepEqual([list.proxy, list.line], ['', ''], 'no proxy, nothing more to say');
  assert.equal(h.addressList({...reply, publicIPv4Proxy: '198.51.100.7'}).proxy, '', 'the same address is no proxy');
});

test('the test form: a pasted URL keeps its host, tcping needs a port, ranges per mode', () => {
  assert.deepEqual(h.cleanTarget(' https://example.com/path?q=1 '), {host: 'example.com', port: ''});
  assert.deepEqual(h.cleanTarget('[2001:db8::1]:443'), {host: '2001:db8::1', port: '443'});
  assert.deepEqual(h.cleanTarget('2001:db8::1'), {host: '2001:db8::1', port: ''});
  assert.deepEqual(h.cleanTarget('example.com:8443'), {host: 'example.com', port: '8443'});
  const req = form => h.probeRequest({mode: 'ping', target: '', port: '', family: 0, count: '', intervalMs: '', timeoutMs: '', ...form});
  assert.deepEqual(req({}).errors, {target: '请填写目标'});
  assert.deepEqual(req({target: 'example.com'}), {body: {mode: 'ping', target: 'example.com', family: 0}, errors: {}});
  assert.equal(req({target: 'a b'}).errors.target, '只填主机名或 IP，例如 example.com 或 1.1.1.1');
  assert.equal(req({target: '例子.中国'}).errors.target, '中文域名请换成 xn-- 开头的 Punycode 形式，或直接填 IP', 'the controller takes ASCII names only');
  assert.equal(req({target: 'xn--fsqu00a.xn--fiqs8s'}).errors.target, undefined);
  assert.match(req({target: '2001:db8::1', family: 4}).errors.family, /协议和目标地址不符/, 'an IPv6 address with IPv4 chosen');
  assert.match(req({target: '1.1.1.1', family: 6}).errors.family, /协议和目标地址不符/);
  assert.deepEqual([req({target: '1.1.1.1', family: 4}), req({target: '[2001:db8::1]', family: 6}), req({target: 'example.com', family: 6}), req({target: '2001:db8::1', family: 0})].map(r => r.errors.family), [undefined, undefined, undefined, undefined]);
  assert.equal(req({target: 'example.com:80'}).errors.target, '不用带端口，只填主机名或 IP');
  assert.equal(req({target: 'x'.repeat(254)}).errors.target, '最长 253 个字符');
  assert.deepEqual(req({mode: 'tcping', target: 'example.com'}).errors, {port: '请填写端口'});
  assert.deepEqual(req({mode: 'tcping', target: 'example.com', port: '70000'}).errors, {port: '端口应为 1–65535'});
  assert.deepEqual(req({mode: 'tcping', target: 'example.com:443'}).body, {mode: 'tcping', target: 'example.com', family: 0, port: 443}, 'a port typed into the target is taken');
  assert.equal(req({mode: 'tcping', target: 'example.com:443', port: '80'}).errors.target, '目标里的端口和“端口”不一致');
  assert.deepEqual(req({mode: 'trace', target: '1.1.1.1', count: '11'}).errors, {count: '轮数应为 1–10'});
  assert.deepEqual(req({target: '1.1.1.1', count: '100', intervalMs: '200', timeoutMs: '10000', family: 6}).body, {mode: 'ping', target: '1.1.1.1', family: 6, count: 100, intervalMs: 200, timeoutMs: 10000});
  assert.deepEqual(req({target: '1.1.1.1', count: '0', intervalMs: '100', timeoutMs: 'x'}).errors, {count: '次数应为 1–100', intervalMs: '应为 200–10000 毫秒', timeoutMs: '应为 200–10000 毫秒'});
  assert.equal(h.fieldError('port', 'tcping'), '端口应为 1–65535');
  assert.equal(h.fieldError('count', 'trace'), '轮数应为 1–10');
  assert.equal(h.fieldError('nonsense', 'ping'), '');
});

test('results: failures by kind, the summary with one decimal below 10 ms, dns errors, places', () => {
  assert.equal(h.resultText({ok: true, rttMs: 4.56}), '4.6 ms');
  assert.equal(h.resultText({ok: true, rttMs: 167.4}), '167 ms');
  assert.equal(h.resultText({ok: false, error: 'timeout'}), '超时');
  assert.equal(h.resultText({ok: false, error: 'refused'}), '拒绝');
  assert.equal(h.resultText({ok: false, error: 'unreachable'}), '不可达');
  assert.equal(h.resultText({ok: false, error: 'ttl exceeded'}), '中途超时', 'a router on the way answered time exceeded');
  assert.equal(h.resultText({ok: false, error: 'connection reset'}), 'connection reset');
  assert.deepEqual([{ok: true}, {ok: false, error: 'refused'}, {ok: false, error: 'ttl exceeded'}, {ok: false, error: 'boom'}].map(h.failureKind), ['', 'refused', 'ttl', 'other']);
  assert.deepEqual([{sent: 0, lossPct: 0}, {}, {sent: 3, lossPct: 100}].map(h.hopPending), [true, true, false], 'a hop not tried yet is not 0% loss');
  assert.equal(h.summaryParts(null), null);
  assert.deepEqual(h.summaryParts({sent: 3, received: 2, lossPct: 33.33, minMs: 4.04, avgMs: 12.3, maxMs: 20.6, jitterMs: null}).map(p => p.join(' ')),
    ['发送 3', '收到 2', '丢包 33.3%', '最小 4 ms', '平均 12 ms', '最大 21 ms', '抖动 —']);
  assert.deepEqual(h.probeErrorText('dns_failed: lookup nope.invalid: no such host'), {title: '解析域名失败', detail: 'lookup nope.invalid: no such host'});
  assert.deepEqual(h.probeErrorText('icmp_unavailable: socket: operation not permitted'), {title: '这台机器上无法发送 ping', detail: 'socket: operation not permitted'});
  assert.deepEqual(h.probeErrorText('dns_failed'), {title: '解析域名失败', detail: ''});
  assert.deepEqual(h.probeErrorText('socket: operation not permitted'), {title: 'socket: operation not permitted', detail: ''}, 'anything else as it is');
  assert.equal(h.geoText({country: 'JP', countryName: 'Japan', asn: 2914, org: 'NTT America, Inc.'}), 'AS2914 NTT America, Inc.');
  assert.equal(h.geoText({country: 'JP', countryName: 'Japan', asn: 0, org: ''}), 'Japan');
  // The controller's fallback lookup for backbones such as CN2 and CTGNet: no AS number, maybe an operator.
  assert.equal(h.geoText({country: 'CN', countryName: 'China', asn: null, org: 'Chinatelecom Next Carrying Network backbone'}), 'Chinatelecom Next Carrying Network backbone');
  assert.equal(h.geoText({country: 'CN', countryName: 'China', asn: null, org: ''}), 'China');
  assert.equal(h.geoText({country: 'CN', asn: null, org: ''}), 'CN', 'no country name: its code');
  // What the old lookup gave for them: a private AS number and "Unknown".
  assert.equal(h.geoText({country: 'CN', countryName: 'China', asn: 64512, org: 'Unknown'}), 'China');
  assert.equal(h.geoText({country: 'US', countryName: 'United States', asn: 4200000001, org: 'Example'}), 'Example');
  for (const geo of [{country: 'CN', countryName: 'China', asn: null, org: null}, {country: 'CN', countryName: 'China', asn: 0, org: 'unknown'}, {country: 'CN', countryName: 'China', asn: '', org: ' '}])
    assert.doesNotMatch(h.geoText(geo), /AS0|ASnull|AS\b|Unknown|null|undefined/i, JSON.stringify(geo));
  assert.equal(h.geoPending({scope: 'public', geo: null}), true);
  assert.equal(h.geoPending({scope: 'private', geo: null}), false);
  assert.equal(h.hopsWaitingForGeo([{addrs: []}, {addrs: [{scope: 'public', geo: {country: 'US'}}]}]), false);
  assert.equal(h.hopsWaitingForGeo([{addrs: [{scope: 'public', geo: null}]}]), true);
  // Over the server's lookup quota: never asked for, so nothing to wait for; the page says 未查询.
  assert.equal(h.geoPending({scope: 'public', geo: null, geoSkipped: true}), false);
  assert.equal(h.hopsWaitingForGeo([{addrs: [{scope: 'public', geo: null, geoSkipped: true}]}]), false);
  const view = fs.readFileSync(new URL('./NetProbe.vue', import.meta.url), 'utf8');
  assert.equal((view.match(/a\.geoSkipped \? '未查询' : geoPending\(a\) \? '未查到'/g) || []).length, 2, 'both the phone list and the desktop table');
});

// home (site relay) → toy (site relay, punched) → vm (public relay, custom TCP pair) → lax (member, entry over UDP; the relay's side says 0 ms).
const work = {members: [
  {serverId: 'home', name: '家', role: 'relay', relayMode: 'site', address: '100.96.0.3'},
  {serverId: 'toy', name: 'TOY', role: 'relay', relayMode: 'site', address: '100.96.0.6'},
  {serverId: 'vm', name: 'VLINE', role: 'relay', address: '100.96.0.7'},
  {serverId: 'lax', name: 'LAX02', role: 'member', address: '100.96.0.2', entry: {mode: 'fixed', candidates: [{id: 'c1', relay: 'vm', transport: 'udp'}]}},
  {serverId: 'mac', name: 'Mac', role: 'member', address: '100.96.0.5', entry: {mode: 'fixed', candidates: [{id: 'm1', relay: 'toy', transport: 'udp'}]}},
  {serverId: 'gone', name: 'Gone', role: 'member', address: '100.96.0.9', removing: true},
], relayLinks: {mode: 'custom', pairs: [{a: 'home', b: 'toy', transport: 'udp'}, {a: 'toy', b: 'vm', transport: 'tcp'}]}};
const data = {nodes: {lax: {summary: {rttMs: 0.6, entryHealth: [{id: 'c1', state: 'active', relay: 'vm', via: ''}]}},
  mac: {summary: {rttMs: 3.1, entryHealth: [{id: 'm1', state: 'active', relay: 'toy', via: 'lan'}]}}}};
const name = id => work.members.find(m => m.serverId === id)?.name || id;

test('mesh path: each hop in the words of its line on the board, the gateway tail, broken and stale', () => {
  assert.equal(h.hopWay(work, data, 'home', 'toy', 'relay'), '打洞');
  assert.equal(h.hopWay(work, data, 'toy', 'vm', 'relay'), 'TCP');
  assert.equal(h.hopWay({...work, relayLinks: {mode: 'full'}}, data, 'toy', 'vm', 'relay'), '中转链路');
  assert.equal(h.hopWay(work, data, 'vm', 'lax', 'entry'), 'UDP');
  assert.equal(h.hopWay(work, data, 'toy', 'mac', 'entry'), '局域网');
  assert.equal(h.hopWay(work, data, 'mac', 'home', 'direct'), '直连');
  const path = {kind: 'member', dst: 'lax', subnet: null, rttMs: 170.2, stale: false,
    hops: [{node: 'home', via: '', rttMs: null}, {node: 'toy', via: 'relay', rttMs: 2.4}, {node: 'vm', via: 'relay', rttMs: 167.2}, {node: 'lax', via: 'entry', rttMs: 0}]};
  const v = h.meshPathView(path, {work, data, name, target: '100.96.0.2'});
  assert.equal(v.text, '家 → TOY（打洞 2.4 ms）→ VLINE（TCP 167 ms）→ LAX02（UDP 0.6 ms）');
  assert.equal(v.through, '家 → TOY → VLINE → LAX02');
  assert.equal(v.totalText, '总延迟约 170 ms', 'an unmeasured entry is filled from the member');
  assert.equal(v.broken, false);
  const gw = h.meshPathView({kind: 'gateway', dst: 'home', subnet: '10.10.10.0/24', rttMs: 2.4, stale: true,
    hops: [{node: 'toy', via: '', rttMs: null}, {node: 'home', via: 'relay', rttMs: 2.4}]}, {work, data, name, target: '10.10.10.1'});
  assert.equal(gw.text, 'TOY → 家（打洞 2.4 ms）→ 10.10.10.1（经网关，局域网）');
  assert.equal(gw.through, 'TOY → 家 → 10.10.10.1');
  assert.equal(gw.stale, true);
  assert.equal(gw.subnet, '10.10.10.0/24');
  const broken = h.meshPathView({...path, hops: path.hops.slice(0, 2)}, {work, data, name});
  assert.equal(broken.brokenText, '在 TOY 之后断开，现在到不了 LAX02');
  assert.equal(broken.totalText, '');
  assert.equal(h.meshPathView({...path, hops: []}, {work, data, name}).brokenText, '现在没有可用的路径');
  assert.equal(h.meshPathView({...path, rttMs: null}, {work, data, name}).totalText, '总延迟未知');
  assert.equal(h.meshPathView(null), null);
});

test('target suggestions: other servers public addresses; mesh addresses for a member, first on the mesh page', () => {
  const servers = [{id: 'home', name: '家', node: {info: {ipv4: '42.0.0.1', ipv6: ''}}}, {id: 'lax', name: 'LAX02', node: {info: {ipv4: '203.0.113.2', ipv6: '2001:db8::2'}}},
    {id: 'nas', name: 'NAS', node: null}];
  const opts = h.targetOptions(servers, 'home', work.members);
  assert.deepEqual(opts.map(o => `${o.value}|${o.note}`), ['203.0.113.2|LAX02', '2001:db8::2|LAX02', '100.96.0.6|TOY · 组网', '100.96.0.7|VLINE · 组网', '100.96.0.2|LAX02 · 组网', '100.96.0.5|Mac · 组网']);
  assert.equal(h.targetOptions(servers, 'home', work.members, {meshFirst: true})[0].value, '100.96.0.6');
  assert.deepEqual(h.targetOptions(servers, 'nas', work.members).map(o => o.value), ['42.0.0.1', '203.0.113.2', '2001:db8::2'], 'not a member: no mesh addresses');
});
