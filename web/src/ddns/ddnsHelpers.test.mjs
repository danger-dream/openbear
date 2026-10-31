import test from 'node:test';
import assert from 'node:assert/strict';
import * as h from './ddnsHelpers.js';

const NODES = [{serverId: 'home', name: '家', region: 'CN', online: true, ddns: true, exec: true, v4: '198.51.100.17', v6: '2001:db8::51'},
  {serverId: 'la', name: 'LA', region: 'US', online: false, ddns: true, exec: false, v4: '198.51.100.76', v6: ''}];
const record = (extra = {}) => ({id: 'r1', accountId: 'a_cf', zone: 'example.com', name: 'home.example.com', type: 'A',
  source: {kind: 'node', serverId: 'home', method: 'auto', methodAuto: true, resolvedMethod: 'stun', params: {}, fixed: ''}, backup: null, ttl: null, options: {},
  policy: {multi: 'one', allowPrivate: false, onDrift: 'revert'}, enabled: true, desired: '198.51.100.17', remote: '198.51.100.17', state: 'ok', stateText: '正常', activeSource: 'primary', ...extra});

test('states: color groups and what needs attention', () => {
  const groups = {ok: 'ok', syncing: 'busy', pending: 'busy', drift: 'warn', fighting: 'warn', failed: 'bad', node_outdated: 'bad', conn_unavailable: 'bad',
    offline: 'idle', paused: 'idle', no_address: 'idle'};
  for (const [state, tone] of Object.entries(groups)) assert.equal(h.stateTone(state), tone, state);
  assert.equal(h.stateTone('something_new'), 'idle');
  assert.deepEqual(Object.keys(groups).filter(state => h.needsAttention({state})), ['drift', 'fighting', 'failed', 'node_outdated', 'conn_unavailable', 'offline', 'no_address'],
    'busy states settle by themselves and paused is the owner\'s choice');
});

test('records: source text, filtering by account, attention, search and server', () => {
  assert.deepEqual(h.sourceParts(record(), NODES), {node: '家', how: 'STUN', backup: false}, 'auto shows what it settled on');
  assert.equal(h.sourceParts(record({source: {kind: 'node', serverId: 'home', method: 'nic', params: {}}, policy: {allowPrivate: true}}), NODES).how, '网卡（内网）');
  assert.deepEqual(h.sourceParts(record({backup: {serverId: 'la', afterS: 300}, activeSource: 'backup'}), NODES), {node: 'LA', how: '备用 · STUN', backup: true});
  assert.equal(h.sourceParts(record({source: {kind: 'fixed', fixed: '192.0.2.1'}}), NODES).node, '固定值');
  assert.equal(h.sourceParts(record({source: {kind: 'node', serverId: 'gone', method: 'url'}}), NODES).node, '已删除的节点');
  const list = [record(), record({id: 'r2', accountId: 'a_ali', name: 'office.example.cn', state: 'drift'}), record({id: 'r3', source: {kind: 'node', serverId: 'la', method: 'nic'}, name: 'la.example.net', desired: '198.51.100.76', remote: '', state: 'offline'}),
    record({id: 'r4', name: 'paused.example.com', state: 'paused'}), record({id: 'r5', name: 'b.example.com', source: {kind: 'node', serverId: 'home', method: 'url'}, backup: {serverId: 'la', afterS: 300}})];
  const ids = view => h.filterRecords(list, view, {nodes: NODES}).map(r => r.id);
  assert.deepEqual(ids({}), ['r1', 'r2', 'r3', 'r4', 'r5']);
  assert.deepEqual(ids({account: 'a_ali'}), ['r2']);
  assert.deepEqual(ids({filter: 'issue'}), ['r2', 'r3']);
  assert.deepEqual(ids({query: 'LA'}), ['r3'], 'by node name; r5 only has LA as a backup');
  assert.deepEqual(ids({query: '198.51.100.76'}), ['r3'], 'by address');
  assert.deepEqual(ids({server: 'la'}), ['r3', 'r5'], 'a machine as source or backup');
  assert.equal(h.currentValue(record({remote: ''})), '198.51.100.17', 'the wanted value when the provider cannot be read');
});

test('preview: what will be saved, takeovers waiting for a tick, errors', () => {
  const rows = [{name: 'a.example.com', type: 'A', action: 'new'}, {name: 'b.example.com', type: 'A', action: 'same'}, {name: 'c.example.com', type: 'A', action: 'takeover'},
    {name: 'c.example.com', type: 'AAAA', action: 'takeover'}, {name: 'd.example.com', type: 'A', action: 'exists'}];
  assert.deepEqual(h.previewCounts(rows), {save: 2, pending: 2, errors: 0, skipped: 1});
  assert.deepEqual(h.previewCounts(rows, new Set(['c.example.com A'])), {save: 3, pending: 1, errors: 0, skipped: 1});
  assert.deepEqual(h.previewCounts([...rows, {name: 'bad', type: 'A', action: 'error'}]).errors, 1);
});

test('the record form: defaults, request bodies, and back from a record', () => {
  const form = h.emptyForm('home', NODES[0]);
  assert.deepEqual(form.types, ['A', 'AAAA'], 'a node with a global IPv6 suggests AAAA too');
  assert.deepEqual(h.emptyForm('la', NODES[1]).types, ['A']);
  form.names = ' www.example.com \n\n nas.example.cn\n';
  assert.deepEqual(h.previewBody(form, {'www.example.com': 'a_cf'}), {serverId: 'home', names: ['www.example.com', 'nas.example.cn'], types: ['A', 'AAAA'],
    source: {method: 'auto', params: {urls: [], iface: '', bindIface: '', stun: [], cmd: '', v6Select: {mode: 'first', value: '', allowTemporary: false}, intervalS: null}},
    accountIds: {'www.example.com': 'a_cf'}});
  // A line chosen goes with the preview: it decides which record at the provider is found.
  assert.deepEqual(h.previewBody({...form, options: {line: 'telecom', proxied: ''}}).options, {line: 'telecom'});
  // Only the chosen method's parameters carry values.
  Object.assign(form, {method: 'url', urls: 'https://api.ipify.org\n https://4.icanhazip.com ', bindIface: 'eth0', stun: 'stun.x:3478', iface: 'eth1', intervalS: 120});
  assert.deepEqual(h.formParams(form), {urls: ['https://api.ipify.org', 'https://4.icanhazip.com'], iface: '', bindIface: 'eth0', stun: [], cmd: '',
    v6Select: {mode: 'first', value: '', allowTemporary: false}, intervalS: 120});
  Object.assign(form, {ttl: 600, options: {proxied: 'on', line: '', other: 'off'}, multi: 'all', allowPrivate: true, onDrift: 'alert', backupId: 'la', backupAfterS: 900});
  const rows = [{name: 'www.example.com', type: 'A', accountId: 'a_cf', action: 'new'}, {name: 'www.example.com', type: 'AAAA', accountId: 'a_cf', action: 'takeover'},
    {name: 'nas.example.cn', type: 'A', accountId: 'a_ali', action: 'takeover'}, {name: 'nas.example.cn', type: 'AAAA', accountId: 'a_ali', action: 'same'},
    {name: 'old.example.cn', type: 'A', accountId: 'a_ali', action: 'exists'}, {name: 'x', type: 'A', accountId: '', action: 'error'}];
  const body = h.createBody(form, rows, new Set(['nas.example.cn A']), {'www.example.com': 'a_cf2'});
  assert.deepEqual(body.rows, [{name: 'www.example.com', type: 'A', accountId: 'a_cf2', confirmTakeover: false},
    {name: 'nas.example.cn', type: 'A', accountId: 'a_ali', confirmTakeover: true}, {name: 'nas.example.cn', type: 'AAAA', accountId: 'a_ali', confirmTakeover: true}],
    'unconfirmed takeovers, managed and failing rows stay out; a same-value row goes without a question');
  assert.equal(body.serverId, 'home');
  assert.deepEqual(body.source.kind, 'node');
  assert.deepEqual(body.backup, {serverId: 'la', afterS: 900});
  assert.equal(body.ttl, 600);
  assert.deepEqual(body.options, {proxied: true, other: false}, '"保持原样" is not sent');
  assert.deepEqual(body.policy, {multi: 'all', allowPrivate: true, onDrift: 'alert'});
  // Edit: a record to the form and back gives the same settings.
  const rec = record({ttl: 300, options: {proxied: false, line: 'telecom'}, backup: {serverId: 'la', afterS: 3600}, policy: {multi: 'one', allowPrivate: false, onDrift: 'revert'},
    source: {kind: 'node', serverId: 'home', method: 'stun', methodAuto: false, params: {urls: [], iface: '', bindIface: '', stun: ['stun.cloudflare.com:3478'], cmd: '',
      v6Select: {mode: 'prefix', value: '2001:db8:', allowTemporary: true}, intervalS: 60}}});
  const back = h.recordToForm(rec);
  assert.equal(back.method, 'stun');
  assert.deepEqual(back.options, {proxied: 'off', line: 'telecom'});
  assert.deepEqual(h.patchBody(back), {source: {kind: 'node', serverId: 'home', method: 'stun', params: rec.source.params}, backup: {serverId: 'la', afterS: 3600},
    ttl: 300, options: {proxied: false, line: 'telecom'}, policy: rec.policy});
  assert.equal(h.recordToForm(record()).method, 'auto', 'a method chosen automatically stays automatic');
  const fixed = h.patchBody(h.recordToForm(record({source: {kind: 'fixed', serverId: '', method: 'auto', params: {}, fixed: '192.0.2.1'}, backup: {serverId: 'la', afterS: 300}})));
  assert.equal(fixed.source.kind, 'fixed');
  assert.equal(fixed.source.fixed, '192.0.2.1');
  assert.equal(fixed.backup, null, 'a fixed value has no backup source');
});

const CF = {type: 'cloudflare', name: 'Cloudflare', tier: 'A', fields: [{key: 'api_token', label: 'API Token', kind: 'password'}, {key: 'zone_id', label: 'Zone ID', kind: 'text', optional: true}],
  options: [{key: 'proxied', label: '代理', kind: 'bool'}], caps: {ttlMin: 60, ttlMax: 86400, ttlAuto: true}, docs: 'https://example.com/docs'};
const RFC = {type: 'rfc2136', name: 'RFC 2136', tier: 'C', fields: [{key: 'server', label: 'DNS 服务器', kind: 'text'}, {key: 'algorithm', label: 'TSIG 算法', kind: 'select', options: ['hmac-sha256', 'hmac-sha512']},
  {key: 'port', label: '端口', kind: 'number', optional: true}, {key: 'tcp', label: '使用 TCP', kind: 'bool'}, {key: 'secret', label: 'TSIG 密钥', kind: 'password'}], options: [], caps: {}};
const ALI = {type: 'aliyun', name: '阿里云', tier: 'B', fields: [], options: [{key: 'line', label: '解析线路', kind: 'select', options: ['default:默认', 'telecom:电信']}]};

test('providers: tiers, search, marks, choices', () => {
  const groups = h.providerGroups([RFC, ALI, CF]);
  assert.deepEqual(groups.map(g => [g.tier, g.items.map(p => p.type)]), [['A', ['cloudflare']], ['B', ['aliyun']], ['C', ['rfc2136']]]);
  assert.deepEqual(h.providerGroups([RFC, ALI, CF], 'cloud').map(g => g.tier), ['A']);
  assert.deepEqual(h.providerGroups([RFC, ALI, CF], '阿里').map(g => g.items[0].type), ['aliyun']);
  assert.deepEqual(h.providerMark(CF), {text: 'Cl', color: '#f38020'});
  assert.equal(h.providerMark(ALI).text, '阿');
  assert.equal(h.providerMark({type: 'unknown', name: 'X'}).color, '#71717a');
  assert.deepEqual(h.choice('telecom:电信'), {value: 'telecom', label: '电信'});
  assert.deepEqual(h.choice('hmac-sha256'), {value: 'hmac-sha256', label: 'hmac-sha256'});
});

test('provider accounts: credentials typed in or from the secret store, missing fields, check results', () => {
  const creds = h.emptyCredentials(RFC);
  assert.deepEqual(creds, {server: '', algorithm: 'hmac-sha256', port: '', tcp: false, secret: ''});
  assert.deepEqual(h.missingFields(RFC, {mode: 'fill', credentials: creds}), ['DNS 服务器', 'TSIG 密钥'], 'optional and on/off fields are never missing');
  const filled = {...creds, server: ' 10.0.0.53:53 ', secret: 'c2VjcmV0', port: '5353', tcp: true};
  assert.deepEqual(h.accountBody({type: 'rfc2136', alias: ' 内网 BIND ', mode: 'fill', credentials: filled, settings: {endpoint: '', proxy: ' http://127.0.0.1:7890 ', ttl: '600'}}, RFC),
    {type: 'rfc2136', alias: '内网 BIND', settings: {endpoint: '', proxy: 'http://127.0.0.1:7890', ttl: 600},
      credentials: {server: '10.0.0.53:53', algorithm: 'hmac-sha256', port: 5353, tcp: true, secret: 'c2VjcmV0'}});
  assert.equal(h.accountBody({type: 'cloudflare', alias: '', mode: 'fill', credentials: {api_token: 't'}, settings: {}}, CF).alias, 'Cloudflare', 'no alias: the provider name');
  // The secret store: keys matched by name, a reference sent instead of values.
  const map = h.guessMap(CF.fields, ['API_TOKEN', 'note']);
  assert.deepEqual(map, {api_token: 'API_TOKEN', zone_id: ''});
  assert.deepEqual(h.missingFields(CF, {mode: 'vault', secretId: '', map}), ['凭据条目']);
  assert.deepEqual(h.missingFields(CF, {mode: 'vault', secretId: '12', map}), []);
  assert.deepEqual(h.missingFields(CF, {mode: 'vault', secretId: '12', map: {api_token: ''}}), ['API Token']);
  assert.deepEqual(h.accountBody({type: 'cloudflare', alias: '主账号', mode: 'vault', secretId: '12', map, settings: {}}, CF),
    {type: 'cloudflare', alias: '主账号', settings: {endpoint: '', proxy: '', ttl: null}, secretRef: {secretId: 12, map: {api_token: 'API_TOKEN'}}});
  assert.equal('credentials' in h.accountBody({type: 'cloudflare', alias: 'a', mode: 'keep', settings: {}}, CF), false, 'keeping the stored credentials sends none');
  // Only the entries this page saved are offered; the controller refuses any other.
  const vault = [{id: 1, name: 'ddns/Cloudflare-主账号', note: h.SECRET_NOTE}, {id: 2, name: 'ddns/手建', note: ''}, {id: 3, name: 'github', note: h.SECRET_NOTE}];
  assert.deepEqual(h.ddnsSecrets(vault).map(s => s.id), [1]);
  assert.deepEqual(h.ddnsSecrets(null), []);
  // A socks proxy saved before is pointed out; http(s) and none are fine.
  assert.equal(h.legacyProxy('socks5://127.0.0.1:1080'), true);
  for (const ok of ['', '  ', 'http://127.0.0.1:7890', 'HTTPS://p:8443']) assert.equal(h.legacyProxy(ok), false, ok);
  assert.deepEqual(h.checkText({zones: ['example.org', 'example.dev'], writable: true}), ['ok', '连接成功：可管理 2 个域 example.org、example.dev，具备写权限。']);
  assert.equal(h.checkText({zones: [], writable: false})[0], 'warn');
  assert.match(h.checkText({zones: ['a.org'], writable: null})[1], /无法提前确认写权限/);
});

test('times, TTL and error texts', () => {
  const now = new Date(2026, 9, 29, 14, 0).getTime();
  assert.equal(h.shortTime(new Date(2026, 9, 29, 13, 40).getTime(), now), '13:40');
  assert.equal(h.shortTime(new Date(2026, 9, 21, 9, 30).getTime(), now), '10-21 09:30');
  assert.equal(h.shortTime(new Date(2025, 6, 23, 7, 38).getTime(), now), '2025-07-23');
  assert.equal(h.shortTime(0, now), '—');
  assert.equal(h.ttlText(null, CF, {settings: {ttl: null}}), '自动');
  assert.equal(h.ttlText(null, RFC, {settings: {ttl: 600}}), '600 秒');
  assert.equal(h.ttlText(120, RFC, null), '2 分钟');
  assert.equal(h.ttlText(3600, RFC, null), '1 小时');
  assert.equal(h.errorMessage({response: {status: 400, data: {error: 'invalid', message: '域名格式不对'}}}), '域名格式不对');
  assert.equal(h.errorMessage({response: {status: 404, data: {}}}), '主控还没有这个接口，请确认已更新到支持动态域名的版本');
  assert.equal(h.errorMessage({code: 'ECONNABORTED'}), '请求超时，稍后重试');
  assert.equal(h.eventTone('drift'), 'warn');
  assert.equal(h.eventTone('fail'), 'bad');
  assert.equal(h.eventTone('write'), 'ok');
  assert.equal(h.eventTone('check'), '');
});
