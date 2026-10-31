// The DDNS page's fake controller, shared by ddns.dom.test.mjs and ddns.browser.test.mjs.
// The catalog as the controller sends it (contract §3/§4): tiers, credential fields of every kind, record options.
export const PROVIDERS = [
  {type: 'cloudflare', name: 'Cloudflare', tier: 'A', fields: [{key: 'api_token', label: 'API Token', kind: 'password', hint: '需要 Zone.DNS 编辑权限'}],
    options: [{key: 'proxied', label: 'Cloudflare 代理', kind: 'bool'}], caps: {ttlMin: 60, ttlMax: 86400, ttlAuto: true, delete: true}, docs: 'https://developers.cloudflare.com/'},
  {type: 'dnspod', name: 'DNSPod', tier: 'A', fields: [{key: 'token', label: 'Token', kind: 'password'}], options: [{key: 'line', label: '解析线路', kind: 'select', options: ['默认', '电信', '联通']}], caps: {ttlMin: 600}},
  {type: 'aliyun', name: '阿里云', tier: 'B', fields: [{key: 'access_key_id', label: 'AccessKey ID', kind: 'text'}, {key: 'access_key_secret', label: 'AccessKey Secret', kind: 'password'}],
    options: [{key: 'line', label: '解析线路', kind: 'select', options: ['default:默认', 'telecom:电信', 'unicom:联通']}], caps: {ttlMin: 600, ttlMax: 86400}},
  {type: 'rfc2136', name: 'RFC 2136', tier: 'C', fields: [{key: 'server', label: 'DNS 服务器', kind: 'text'}, {key: 'algorithm', label: 'TSIG 算法', kind: 'select', options: ['hmac-sha256', 'hmac-sha512']},
    {key: 'port', label: '端口', kind: 'number', optional: true}, {key: 'tcp', label: '使用 TCP', kind: 'bool'}, {key: 'secret', label: 'TSIG 密钥', kind: 'password'}, {key: 'note', label: '备注', kind: 'textarea', optional: true}], options: [], caps: {}},
  {type: 'webhook', name: '通用回调', tier: 'C', fields: [{key: 'url', label: '回调地址', kind: 'text'}], options: [], caps: {read: false}},
];
export const NODES = [
  {serverId: 'home', name: '家里 NAS', region: 'CN', online: true, platform: 'linux', ddns: true, exec: true, v4: '198.51.100.17', v6: '2001:db8:1f0::51'},
  {serverId: 'hk', name: '香港 HK1', region: 'HK', online: true, platform: 'linux', ddns: true, exec: false, v4: '203.0.113.42', v6: ''},
  {serverId: 'la', name: 'Los Angeles', region: 'US', online: false, platform: 'linux', ddns: true, exec: true, v4: '198.51.100.76', v6: ''},
  {serverId: 'old', name: '法兰克福旧机', region: 'DE', online: true, platform: 'linux', ddns: false, exec: true, v4: '192.0.2.9', v6: ''},
];
const ACCOUNTS = [
  {id: 'a_cf', type: 'cloudflare', alias: '主账号', zones: ['example.com', 'example.net'], state: 'ok', error: '', secret: {id: 12, name: 'ddns/Cloudflare-主账号'}, settings: {endpoint: '', proxy: '', ttl: null}, records: 4, issues: 2},
  {id: 'a_ali', type: 'aliyun', alias: '阿里云', zones: ['example.cn'], state: 'ok', error: '', secret: {id: 13, name: 'ddns/阿里云'}, settings: {endpoint: '', proxy: '', ttl: 600}, records: 1, issues: 0},
  {id: 'a_dp', type: 'dnspod', alias: 'DNSPod', zones: [], state: 'error', error: '认证失败：令牌无效或已过期', errorCode: 'auth_failed', secret: {id: 14, name: 'ddns/DNSPod'}, settings: {endpoint: '', proxy: '', ttl: null}, records: 1, issues: 1},
];
const src = (serverId, method = 'auto', extra = {}) => ({kind: 'node', serverId, method, methodAuto: method === 'auto', resolvedMethod: method === 'auto' ? 'stun' : method,
  params: {urls: [], iface: '', bindIface: '', stun: [], cmd: '', v6Select: {mode: 'first', value: '', allowTemporary: false}, intervalS: null}, fixed: '', ...extra});
const T0 = Date.now() - 3600000;
const rec = (id, accountId, name, type, extra = {}) => ({id, accountId, zone: name.split('.').slice(-2).join('.'), name, type, source: src('home'), backup: null, ttl: null, options: {},
  policy: {multi: 'one', allowPrivate: false, onDrift: 'revert'}, enabled: true, desired: '198.51.100.17', remote: '198.51.100.17', remoteAt: T0, providerRecordId: 'x',
  state: 'ok', stateText: '正常', error: '', errorCode: '', failCount: 0, changedAt: T0, syncedAt: T0, checkedAt: T0, activeSource: 'primary', readable: true, ...extra});
const RECORDS = () => [
  rec('r1', 'a_cf', 'home.example.com', 'A', {options: {proxied: false}, backup: {serverId: 'hk', afterS: 300}}),
  rec('r2', 'a_cf', 'home.example.com', 'AAAA', {desired: '2001:db8:1f0::51', remote: '2001:db8:1f0::51', state: 'syncing', stateText: '正在同步'}),
  rec('r3', 'a_cf', 'hk.example.net', 'A', {source: src('hk', 'nic'), desired: '203.0.113.42', remote: '203.0.113.99', state: 'drift', stateText: '被外部改动，已改回'}),
  rec('r4', 'a_cf', 'la.example.com', 'A', {source: src('la', 'url'), desired: '198.51.100.76', remote: '198.51.100.76', state: 'offline', stateText: '节点离线'}),
  rec('r5', 'a_ali', 'nas.example.cn', 'A', {state: 'paused', stateText: '已停用', enabled: false}),
  rec('r6', 'a_dp', 'old.example.org', 'A', {source: src('old', 'url'), desired: '192.0.2.9', remote: '', state: 'failed', stateText: '认证失败', error: '令牌无效或已过期', errorCode: 'auth_failed'}),
];

// The controller's /api/ddns, as far as the page uses it.
export function fakeDdns() {
  const state = {accounts: JSON.parse(JSON.stringify(ACCOUNTS)), records: RECORDS(), next: 10};
  const view = () => ({ok: true, accounts: state.accounts, records: state.records, providers: PROVIDERS, nodes: NODES, issues: 3});
  const preview = body => {
    const rows = [];
    for (const name of body.names) for (const type of body.types) {
      const zone = name.split('.').slice(-2).join('.');
      const accounts = state.accounts.filter(a => a.zones.includes(zone)).map(a => ({id: a.id, alias: a.alias, type: a.type}));
      const value = type === 'A' ? NODES.find(n => n.serverId === body.serverId)?.v4 : '2001:db8:1f0::51';
      const base = {name, type, zone, accounts, accountId: body.accountIds?.[name] || accounts[0]?.id || '', remote: null, remoteCount: 0, value, error: '', errorCode: ''};
      if (!accounts.length) rows.push({...base, action: 'error', error: '没有能管理这个域的服务商账户', errorCode: 'zone_not_found'});
      else if (name.startsWith('take')) rows.push({...base, action: 'takeover', remote: '192.0.2.200', remoteCount: name.startsWith('take2') ? 2 : 1});
      else if (name.startsWith('same')) rows.push({...base, action: 'same', remote: value});
      else if (state.records.some(r => r.name === name && r.type === type)) rows.push({...base, action: 'exists'});
      else rows.push({...base, action: 'new'});
    }
    return {ok: true, detected: {v4: {value: '198.51.100.17', method: 'stun', auto: body.source.method === 'auto', notes: [{level: 'info', text: '节点在 NAT 后，网卡上没有公网地址'}]}, v6: body.types.includes('AAAA') ? {value: '2001:db8:1f0::51', method: 'nic', auto: true, notes: []} : null}, rows};
  };
  return {state, handle(method, path, body, query) {
    if (method === 'GET' && path === '') return [200, view()];
    if (method === 'POST' && path === '/accounts/test') return body.credentials?.api_token === 'bad' ? [400, {ok: false, error: 'auth_failed', message: '认证失败：令牌无效'}] : [200, {ok: true, zones: ['example.org', 'example.dev'], writable: true}];
    if (method === 'POST' && path === '/accounts') {
      const account = {id: `a_${state.next++}`, type: body.type, alias: body.alias, zones: ['example.org'], state: 'ok', error: '', secret: {id: 99, name: 'x'}, settings: body.settings, records: 0, issues: 0};
      state.accounts.push(account);
      return [200, {ok: true, account}];
    }
    const acct = path.match(/^\/accounts\/([^/]+)(\/check)?$/);
    if (acct && method === 'POST') return [200, {ok: true, zones: ['example.com'], writable: null}];
    if (acct && method === 'PATCH') { const a = state.accounts.find(x => x.id === acct[1]); Object.assign(a, {alias: body.alias, settings: body.settings}); return [200, {ok: true, account: a}]; }
    if (acct && method === 'DELETE') {
      if (state.records.some(r => r.accountId === acct[1]) && query.get('force') !== '1') return [409, {ok: false, error: 'account_in_use', message: '这个账户下还有解析'}];
      state.accounts = state.accounts.filter(a => a.id !== acct[1]); state.records = state.records.filter(r => r.accountId !== acct[1]);
      return [200, {ok: true}];
    }
    if (method === 'POST' && path === '/preview') return [200, preview(body)];
    if (method === 'POST' && path === '/records') {
      const records = body.rows.map(r => rec(`r${state.next++}`, r.accountId, r.name, r.type, {source: {...src(body.serverId), ...body.source}, state: 'pending', stateText: '等待同步'}));
      state.records.push(...records);
      return [200, {ok: true, records, skipped: []}];
    }
    const one = path.match(/^\/records\/([^/]+)(\/sync|\/events)?$/);
    if (one) {
      const r = state.records.find(x => x.id === one[1]);
      if (one[2] === '/events') return [200, {ok: true, events: [{id: 3, at: Date.now() - 600000, kind: 'drift', text: '服务商上的值被改成 203.0.113.99，已改回', detail: {}},
        {id: 2, at: Date.now() - 86400000 * 3, kind: 'write', text: '写入 198.51.100.17', detail: {}}, {id: 1, at: Date.now() - 86400000 * 400, kind: 'create', text: '创建解析', detail: {}}]}];
      if (one[2] === '/sync') { Object.assign(r, {state: 'ok', stateText: '正常', syncedAt: Date.now()}); return [200, {ok: true, record: r}]; }
      if (method === 'PATCH') { Object.assign(r, body); if ('enabled' in body) Object.assign(r, body.enabled ? {state: 'pending', stateText: '等待同步'} : {state: 'paused', stateText: '已停用'}); return [200, {ok: true, record: r}]; }
      if (method === 'DELETE') { state.records = state.records.filter(x => x.id !== one[1]); return [200, {ok: true}]; }
    }
    if (method === 'POST' && path === '/diagnose') return [200, {ok: true, results: [{method: 'stun', value: '198.51.100.17', detail: '42 ms'}, {method: 'url', value: '198.51.100.17'}, {method: 'nic', value: '', error: '网卡上只有内网地址 192.168.1.10'}],
      interfaces: [{name: 'eth0', default: true, v4: ['192.168.1.10'], v6: [{addr: '2001:db8:1f0::51'}, {addr: '2001:db8:1f0::abcd', temporary: true}]}], recommend: 'stun', notes: [{level: 'warn', text: '节点在 NAT 后'}]}];
    if (method === 'POST' && path === '/import/preview') return [200, {ok: true, accounts: [{type: 'cloudflare', alias: 'ddns-go', mergeInto: 'a_cf'}, {type: 'aliyun', alias: 'ddns-go 阿里云', mergeInto: null}],
      records: [{name: 'blog.example.com', type: 'A', method: 'url', action: 'new', remote: null, value: '198.51.100.17'}, {name: 'take.example.com', type: 'A', method: 'url', action: 'takeover', remote: '192.0.2.200', value: '198.51.100.17'},
        {name: 'home.example.com', type: 'A', method: 'nic', action: 'exists', remote: '198.51.100.17', value: '198.51.100.17'}],
      unsupported: [{item: 'Webhook 通知', reason: 'OpenBear 用自己的事件提醒'}], warnings: ['配置里有 2 个 DNS 服务商，导入为 2 个账户']}];
    if (method === 'POST' && path === '/import') return [200, {ok: true, accounts: [], records: [rec('r_imp', 'a_cf', 'blog.example.com', 'A')], skipped: body.confirmTakeover ? [] : [{name: 'take.example.com', type: 'A', reason: 'takeover_not_confirmed'}]}];
    return [404, {ok: false, error: 'not_found', message: '没有这个接口'}];
  }};
}

// Every /api request of the page: the DDNS endpoints and the secret store entries it offers.
// Returns [status, json].
export function handleApi(ddns, method, path, body, searchParams) {
  if (path.startsWith('/api/ddns')) return ddns.handle(method, path.slice('/api/ddns'.length), body, searchParams);
  if (path === '/api/memory/secrets') return [200, {ok: true, items: [{id: 12, name: 'ddns/Cloudflare-主账号', note: '动态域名服务商凭据'}, {id: 31, name: 'ddns/Cloudflare-备用', note: '动态域名服务商凭据'}, {id: 40, name: 'github', note: 'CI'}]}];
  if (path === '/api/memory/secrets/31') return [200, {ok: true, item: {id: 31, name: 'ddns/Cloudflare-备用', kv: [{key: 'API_TOKEN', value: 'xxx'}, {key: 'email', value: 'a@b'}]}}];
  return [200, {ok: true}];
}
