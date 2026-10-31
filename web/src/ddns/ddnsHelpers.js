// Display rules and request bodies for the 动态域名 page; no requests here.

// ---- states ----
// Each record state falls in one display group: the pill color, and whether it asks for attention.
const STATE_TONES = {
  ok: 'ok',
  syncing: 'busy', pending: 'busy',
  drift: 'warn', fighting: 'warn', ambiguous: 'warn',
  failed: 'bad', node_outdated: 'bad', conn_unavailable: 'bad', probe_refused: 'bad',
  offline: 'idle', paused: 'idle', no_address: 'idle',
};
export function stateTone(state) { return STATE_TONES[state] || 'idle'; }
// Paused is the owner's own choice and busy states settle by themselves; everything else needs a look.
export function needsAttention(record) {
  const tone = stateTone(record?.state);
  return tone === 'warn' || tone === 'bad' || (tone === 'idle' && record?.state !== 'paused');
}
// Errors the controller does not retry on its own (contract §1, PERMANENT).
export const PERMANENT_ERRORS = new Set(['auth_failed', 'forbidden', 'zone_not_found', 'bad_request', 'unsupported']);

// ---- names ----
export const METHODS = {auto: '自动', url: '接口', url_cn: '接口（国内）', url_intl: '接口（国外）', nic: '网卡', stun: 'STUN', cmd: '命令', conn: '连接来源', fixed: '固定值'};
export const TIERS = {A: '令牌即可接入', B: '需要签名（AccessKey）', C: '标准协议 / 通用回调'};
// Brand-ish colors for the small letter marks; an unknown type gets a neutral one.
const COLORS = {
  cloudflare: '#f38020', dnspod: '#2f80ed', porkbun: '#ef7878', godaddy: '#1bb5b5', namesilo: '#2463a4', namecom: '#5ab36d', desec: '#c62828',
  duckdns: '#d4a20b', dynv6: '#4a9b4a', gcore: '#ff4c00', vercel: '#111111', digitalocean: '#0080ff', linode: '#00a95c', vultr: '#007bfc',
  hetzner: '#d50c2d', dynadot: '#2a6ebb', spaceship: '#5a4ff3', cloudns: '#1d8ed6', ns1: '#6d3fa0', rainyun: '#3d7eff', dnsla: '#3a7bd5',
  namecheap: '#de3723', aliyun: '#ff6a00', tencent: '#00a3ff', huawei: '#c7000b', baidu: '#2932e1', volc: '#3370ff', route53: '#8c4fff',
  rfc2136: '#5b6472', callback: '#737378',
};
export function providerMark(provider) {
  const name = String(provider?.name || provider?.type || '?');
  const han = /[\u4e00-\u9fa5]/.test(name);
  const text = name.replace(/[^A-Za-z0-9\u4e00-\u9fa5]/g, '').slice(0, han ? 1 : 2) || '?';
  return {text, color: COLORS[provider?.type] || '#71717a'};
}
export function providerOf(providers, type) { return (providers || []).find(p => p.type === type) || {type, name: type, tier: 'A', fields: [], options: [], caps: {}, docs: ''}; }
export function accountLabel(account, providers) {
  if (!account) return '已删除的账户';
  return `${providerOf(providers, account.type).name} · ${account.alias}`;
}
// Providers grouped by tier, those matching the search only.
export function providerGroups(providers, query = '') {
  const q = query.trim().toLowerCase();
  return Object.entries(TIERS).map(([tier, label]) => ({tier, label,
    items: (providers || []).filter(p => (p.tier || 'A') === tier && (!q || p.name.toLowerCase().includes(q) || p.type.includes(q)))}))
    .filter(g => g.items.length);
}
// "code:中文名" is how select options come; a plain value is its own label.
export function choice(option) {
  const text = String(option ?? '');
  const at = text.indexOf(':');
  return at > 0 ? {value: text.slice(0, at), label: text.slice(at + 1)} : {value: text, label: text};
}

// ---- time ----
const pad = n => String(n).padStart(2, '0');
// Today: 13:40; earlier: 10-29 13:40; another year: 2025-10-29.
export function shortTime(ms, now = Date.now()) {
  if (!ms) return '—';
  const d = new Date(Number(ms)), today = new Date(now);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === today.toDateString()) return hm;
  if (d.getFullYear() === today.getFullYear()) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export function ttlText(ttl, provider, account) {
  const value = ttl || account?.settings?.ttl;
  if (!value) return provider?.caps?.ttlAuto ? '自动' : '服务商默认';
  if (value % 3600 === 0) return `${value / 3600} 小时`;
  if (value >= 60 && value % 60 === 0 && value < 600) return `${value / 60} 分钟`;
  return `${value} 秒`;
}

// ---- records ----
export function nodeOf(nodes, id) { return (nodes || []).find(n => n.serverId === id) || null; }
// The method actually used: what "auto" settled on, when it has.
export function methodOf(record) {
  const s = record?.source || {};
  if (s.kind === 'fixed') return 'fixed';
  return s.method === 'auto' || s.methodAuto ? (s.resolvedMethod || 'auto') : (s.method || 'auto');
}
export function sourceParts(record, nodes) {
  const s = record?.source || {};
  if (s.kind === 'fixed') return {node: '固定值', how: s.fixed || '', backup: false};
  const backup = record.activeSource === 'backup' && record.backup?.serverId;
  const node = nodeOf(nodes, backup ? record.backup.serverId : s.serverId);
  const how = METHODS[methodOf(record)] + (record.policy?.allowPrivate ? '（内网）' : '');
  return {node: node?.name || '已删除的节点', how: backup ? `备用 · ${how}` : how, backup: Boolean(backup)};
}
export function currentValue(record) { return record?.remote || record?.desired || '—'; }
export function recordMatches(record, query, {nodes, accounts, providers} = {}) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return true;
  const src = sourceParts(record, nodes);
  const account = (accounts || []).find(a => a.id === record.accountId);
  return [record.name, record.type, src.node, record.desired, record.remote, accountLabel(account, providers)]
    .some(v => String(v || '').toLowerCase().includes(q));
}
export function recordUsesServer(record, serverId) {
  return Boolean(serverId) && (record?.source?.serverId === serverId || record?.backup?.serverId === serverId);
}
export function filterRecords(records, {account = 'all', filter = 'all', query = '', server = ''} = {}, context = {}) {
  return (records || []).filter(r => (account === 'all' || r.accountId === account)
    && (filter === 'all' || needsAttention(r))
    && (!server || recordUsesServer(r, server))
    && recordMatches(r, query, context));
}
export function eventTone(kind) {
  if (['drift', 'recreated', 'offline', 'failover', 'ambiguous', 'hold'].includes(kind)) return 'warn';
  if (kind === 'fail') return 'bad';
  if (['create', 'write', 'takeover', 'recover', 'online', 'failback'].includes(kind)) return 'ok';
  return '';
}
// Whole lines, trimmed, empties dropped: domains, URL and STUN lists.
export function lines(text) { return String(text || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean); }

// ---- the record form (add and edit share it) ----
export const DEFAULT_URLS = ['https://api.ipify.org', 'https://4.icanhazip.com'];
export const DEFAULT_STUN = ['stun.cloudflare.com:3478', 'stun.l.google.com:19302'];
export const INTERVALS = [[null, '默认（网卡 15 秒，接口 / STUN 60 秒）'], [30, '30 秒'], [60, '1 分钟'], [120, '2 分钟'], [300, '5 分钟']];
export const TTLS = [[null, '服务商默认'], [60, '60 秒'], [120, '2 分钟'], [300, '5 分钟'], [600, '10 分钟'], [3600, '1 小时']];
export const FAILOVER = [[300, '5 分钟'], [900, '15 分钟'], [3600, '1 小时']];
export const V6_MODES = [['first', '长期地址里的第一个（推荐）'], ['prefix', '按前缀匹配'], ['regex', '按正则匹配'], ['index', '第 N 个']];

export function emptyForm(serverId = '', node = null) {
  return {serverId, names: '', types: node?.v6 ? ['A', 'AAAA'] : ['A'], method: 'auto', fixed: '',
    urls: '', bindIface: '', iface: '', stun: '', cmd: '', v6Mode: 'first', v6Value: '', v6Temporary: false, intervalS: null,
    ttl: null, options: {}, multi: 'one', allowPrivate: false, onDrift: 'revert', backupId: '', backupAfterS: 300};
}
export function recordToForm(record) {
  const s = record.source || {}, p = s.params || {}, v6 = p.v6Select || {};
  const options = {};
  for (const [key, value] of Object.entries(record.options || {})) options[key] = typeof value === 'boolean' ? (value ? 'on' : 'off') : String(value ?? '');
  return {serverId: s.serverId || '', names: record.name, types: [record.type], method: s.kind === 'fixed' ? 'fixed' : (s.methodAuto ? 'auto' : s.method || 'auto'), fixed: s.fixed || '',
    urls: (p.urls || []).join('\n'), bindIface: p.bindIface || '', iface: p.iface || '', stun: (p.stun || []).join('\n'), cmd: p.cmd || '',
    v6Mode: v6.mode || 'first', v6Value: v6.value || '', v6Temporary: Boolean(v6.allowTemporary), intervalS: p.intervalS ?? null,
    ttl: record.ttl ?? null, options, multi: record.policy?.multi || 'one', allowPrivate: Boolean(record.policy?.allowPrivate), onDrift: record.policy?.onDrift || 'revert',
    backupId: record.backup?.serverId || '', backupAfterS: record.backup?.afterS || 300};
}
// Only the chosen method's parameters carry values; the rest go as empty so the shape stays whole.
export function formParams(form) {
  const m = form.method;
  return {urls: m === 'url' ? lines(form.urls) : [], iface: m === 'nic' ? form.iface.trim() : '', bindIface: ['url', 'stun'].includes(m) ? form.bindIface.trim() : '',
    stun: m === 'stun' ? lines(form.stun) : [], cmd: m === 'cmd' ? form.cmd.trim() : '',
    v6Select: {mode: form.v6Mode, value: form.v6Mode === 'first' ? '' : String(form.v6Value || '').trim(), allowTemporary: Boolean(form.v6Temporary)},
    intervalS: form.intervalS ? Number(form.intervalS) : null};
}
export function formSource(form) {
  if (form.method === 'fixed') return {kind: 'fixed', serverId: '', method: 'auto', params: formParams({...form, method: 'auto'}), fixed: form.fixed.trim()};
  return {kind: 'node', serverId: form.serverId, method: form.method, params: formParams(form)};
}
// Options left at "保持原样" are not sent, so the provider keeps whatever the record has.
export function formOptions(form) {
  const out = {};
  for (const [key, value] of Object.entries(form.options || {})) {
    if (value === 'on') out[key] = true;
    else if (value === 'off') out[key] = false;
    else if (value !== '' && value != null) out[key] = value;
  }
  return out;
}
export function formSettings(form) {
  return {source: formSource(form), backup: form.backupId && form.method !== 'fixed' ? {serverId: form.backupId, afterS: Number(form.backupAfterS) || 300} : null,
    ttl: form.ttl ? Number(form.ttl) : null, options: formOptions(form),
    policy: {multi: form.multi, allowPrivate: Boolean(form.allowPrivate), onDrift: form.onDrift}};
}
export function previewBody(form, accountIds = {}) {
  const source = formSource(form);
  // The line chosen decides which record at the provider the preview finds.
  const options = formOptions(form);
  return {serverId: form.serverId, names: lines(form.names), types: [...form.types], source: {method: source.method, params: source.params}, accountIds: {...accountIds},
    ...(Object.keys(options).length ? {options} : {})};
}
export const rowKey = row => `${row.name} ${row.type}`;
// What a preview will save: new rows, rows already holding the value, and takeovers the owner ticked.
export function previewCounts(rows, confirmed = new Set()) {
  const out = {save: 0, pending: 0, errors: 0, skipped: 0};
  for (const row of rows || []) {
    if (row.action === 'error') out.errors++;
    else if (row.action === 'exists') out.skipped++;
    else if (row.action === 'takeover') { if (confirmed.has(rowKey(row))) out.save++; else out.pending++; }
    else out.save++;
  }
  return out;
}
// A row with the same value already there is taken over without a question: nothing on the provider changes.
export function createBody(form, rows, confirmed = new Set(), accountIds = {}) {
  const chosen = (rows || []).filter(r => r.action === 'new' || r.action === 'same' || (r.action === 'takeover' && confirmed.has(rowKey(r))));
  return {serverId: form.method === 'fixed' ? '' : form.serverId, ...formSettings(form),
    rows: chosen.map(r => ({name: r.name, type: r.type, accountId: accountIds[r.name] || r.accountId, confirmTakeover: r.action !== 'new'}))};
}
export function patchBody(form) { return formSettings(form); }

// ---- provider accounts ----
export function emptyCredentials(provider) {
  const out = {};
  for (const f of provider?.fields || []) out[f.key] = f.default ?? (f.kind === 'bool' ? false : f.kind === 'select' ? choice(f.options?.[0]).value : '');
  return out;
}
// A secret's keys matched to the provider's fields: same key, then the same letters ignoring case and separators.
export function guessMap(fields, keys) {
  const norm = s => String(s).toLowerCase().replace(/[^a-z0-9]/g, '');
  const map = {};
  for (const f of fields || []) {
    const hit = keys.find(k => k === f.key) || keys.find(k => norm(k) === norm(f.key)) || keys.find(k => norm(k) === norm(f.label));
    map[f.key] = hit || '';
  }
  return map;
}
// Only secrets this page saved can back an account (the controller refuses any other vault entry).
export const SECRET_NOTE = '动态域名服务商凭据';
export function ddnsSecrets(items) { return (items || []).filter(s => String(s?.name || '').startsWith('ddns/') && s?.note === SECRET_NOTE); }
// A socks proxy saved before only http(s) ones were taken: syncing stops until it is changed.
export function legacyProxy(proxy) {
  const value = String(proxy || '').trim();
  return Boolean(value) && !/^https?:\/\//i.test(value);
}
export function settingsBody(settings) {
  return {endpoint: String(settings?.endpoint || '').trim(), proxy: String(settings?.proxy || '').trim(), ttl: settings?.ttl ? Number(settings.ttl) : null};
}
// mode: 'fill' sends the typed credentials, 'vault' a reference into the secret store, 'keep' neither.
export function accountBody({type, alias, mode, credentials, secretId, map, settings}, provider) {
  const body = {type, alias: String(alias || '').trim() || provider?.name || type, settings: settingsBody(settings)};
  if (mode === 'vault') body.secretRef = {secretId: Number(secretId), map: Object.fromEntries(Object.entries(map || {}).filter(([, v]) => v))};
  else if (mode === 'fill') {
    const out = {};
    for (const f of provider?.fields || []) {
      const value = credentials?.[f.key];
      if (f.kind === 'bool') out[f.key] = Boolean(value);
      else if (f.kind === 'number') { if (value !== '' && value != null) out[f.key] = Number(value); }
      else if (String(value ?? '').trim() !== '' || f.kind === 'select') out[f.key] = f.kind === 'textarea' ? String(value ?? '') : String(value ?? '').trim();
    }
    body.credentials = out;
  }
  return body;
}
// Required fields still empty: the reason the buttons stay off.
export function missingFields(provider, {mode, credentials, secretId, map}) {
  const required = (provider?.fields || []).filter(f => !f.optional && f.kind !== 'bool');
  if (mode === 'keep') return [];
  if (mode === 'vault') return secretId ? required.filter(f => !map?.[f.key]).map(f => f.label) : ['凭据条目'];
  return required.filter(f => String(credentials?.[f.key] ?? '').trim() === '').map(f => f.label);
}
export function checkText(result) {
  const zones = result?.zones || [];
  const list = zones.length ? `可管理 ${zones.length} 个域 ${zones.slice(0, 6).join('、')}${zones.length > 6 ? ' 等' : ''}` : '没有列出可管理的域';
  if (result?.writable === true) return ['ok', `连接成功：${list}，具备写权限。`];
  if (result?.writable === false) return ['warn', `凭据有效，但没有写权限：${list}。`];
  return ['ok', `连接成功：${list}。这家服务商无法提前确认写权限，第一次写入时会知道。`];
}
export function errorMessage(error, fallback = '操作没有完成') {
  const data = error?.response?.data;
  if (data?.message) return data.message;
  if (error?.code === 'ECONNABORTED') return '请求超时，稍后重试';
  if (error?.response?.status === 404) return '主控还没有这个接口，请确认已更新到支持动态域名的版本';
  if (data?.error) return `${fallback}（${String(data.error).slice(0, 80)}）`;
  return fallback;
}
