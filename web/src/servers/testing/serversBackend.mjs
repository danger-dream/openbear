// The fake backend of the servers page: fixtures and the /api answers, shared by the browser
// tests (servers.*.browser.test.mjs, through page.route) and the happy-dom tests
// (servers.*.dom.test.mjs, through testing/fakeNetwork.mjs). Test-only.
import BGP from './bgpSamples.json' with {type: 'json'};
import UNLOCK_FULL from './unlockFullSample.json' with {type: 'json'};

// Dates in the page's zone (settings.timezone below), or the days left are off by one for part of each day.
export const ZONE = 'Asia/Shanghai';
export const day = n => new Intl.DateTimeFormat('en-CA', {timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(Date.now() + n * 86400000));
export const GB = 1024 ** 3;
export function sample(cpu, mem, disk, steal = 0.4) {
  return {cpu, steal, iowait: 1.2, mem: {total: 2 * GB, used: mem * 2 * GB / 100}, swap: {total: 0, used: 0}, disk: {total: 20 * GB, used: disk * 20 * GB / 100}, load: [0.5, 0.4, 0.3],
    io: {readRate: 52000, writeRate: 1300000}, net: {rx: 9e12, tx: 4e12, rxRate: 1250000, txRate: 310000}, tcp: 120, udp: 8, processes: 310, uptime: 864000, boot: 1, nodeRss: 11.8 * 1024 * 1024};
}
export function server(id, name, {group = '', region = 'SG', cpu = 10, mem = 30, disk = 40, steal, online = true, node = true, terminal = true, exec = true, outdated = false, price = 9, currency = '$', cycle = 30, expires = 40, limit = 500, used = 120, ssh = null, local = false} = {}) {
  const info = {hostname: id, os: 'Debian GNU/Linux 12 (bookworm)', kernel: '6.1.0-26-amd64', arch: 'amd64', cpu: 'AMD EPYC 7B13', cores: 2, virtualization: 'kvm', memTotal: 2 * GB, swapTotal: 0, diskTotal: 20 * GB, ipv4: '198.51.100.' + id.length, ipv6: ''};
  const version = outdated ? '0.13.0' : '0.13.1';
  const live = node && online ? {online: true, connectedAtMs: Date.now() - 3600000, version, caps: {exec, terminal}, at: Date.now(), m: sample(cpu, mem, disk, steal)} : null;
  return {id, name, local, ssh, usedBy: [], region, install: null, live,
    profile: {group, region, sort: 0, billing: {price, currency, cycle, startedAt: '', expiresAt: day(expires), autoRenew: false}, traffic: {limit: limit * GB, type: 'max', resetDay: 0}},
    node: node ? {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now() - (online ? 0 : 7200000), online, version, latest: '0.13.1', updateAvailable: outdated && online,
      caps: {exec, terminal}, info, geo: null} : null,
    traffic: node ? {periodStart: day(-10), resetDay: 1, rx: used * GB, tx: used * GB / 3, type: 'max', used: used * GB, limit: limit * GB} : null};
}
export function fixtures() {
  return [
    server('local', 'Edge-SG.Game', {group: '测试机', cpu: 12.5, mem: 41, disk: 82, price: 375.91, cycle: 365, expires: 159, limit: 10240, used: 2560, local: true}),
    server('srv-hotel', 'Hotel-HK', {group: 'Tier 1', region: 'HK', node: false, expires: 2, ssh: {host: '203.0.113.116', port: 27920, username: 'root', auth: 'key', keyLabel: 'ssh-key / 私钥', status: 'unchecked'}}),
    server('srv-papa', 'Papa-HK.T1', {group: 'Tier 1', region: 'HK', cpu: 64, mem: 13, disk: 39, steal: 23, outdated: true, price: 37.79, currency: '¥', expires: 8, limit: 500, used: 500}),
    server('srv-golf', 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断', {group: '测试机', region: 'JP', online: false, price: 112, cycle: 1095, expires: 727}),
    server('srv-delta', 'Delta-JP', {group: '流媒体', region: 'JP', cpu: 3, mem: 17, disk: 23, outdated: true, price: 198, currency: '¥', cycle: 365, expires: 52, limit: 750, used: 4}),
    server('srv-bravo', 'Bravo-FRA', {region: 'DE', cpu: 1, mem: 60, disk: 91, terminal: false, exec: false, price: 112, currency: '€', cycle: 365, expires: 343, limit: 0, used: 30}),
  ];
}
export function points(n, step) {
  const end = Math.floor(Date.now() / 1000), t = [];
  for (let i = n; i > 0; i--) if (i !== 20 && i !== 21) t.push(end - i * step);
  const wave = (a, b) => t.map((_, i) => a + b * Math.sin(i / 5));
  return {t, cpu: wave(20, 10), steal: wave(2, 1), iowait: wave(1, 1), mem: wave(40, 2), swap: t.map(() => 0), disk: wave(80, 1), load: wave(0.5, 0.2), rxRate: wave(1e6, 5e5),
    txRate: wave(3e5, 1e5), ioRead: wave(5e4, 4e4), ioWrite: wave(1e6, 8e5), tcp: wave(120, 20), udp: wave(8, 2), proc: wave(300, 5)};
}
// Traffic per hour (48), day (31) or month (12), the last still counting.
export function trafficStats(unit) {
  const count = {hour: 48, day: 31, month: 12}[unit], now = Math.floor(Date.now() / 1000), step = {hour: 3600, day: 86400, month: 86400 * 30}[unit];
  return {ok: true, unit, points: Array.from({length: count}, (_, i) => {
    const k = i - count + 1, scale = {hour: 2 * GB, day: 40 * GB, month: 900 * GB}[unit], wave = 0.6 + 0.4 * Math.sin(i / 2.3);
    return {t: now - now % step + k * step, label: '', rx: Math.round(scale * wave * (i === count - 1 ? 0.4 : 1)), tx: Math.round(scale * wave * 0.32 * (i === count - 1 ? 0.4 : 1))};
  })};
}
export function diskStats(range) {
  const now = Math.floor(Date.now() / 1000), step = {'7d': 7200, '30d': 14400, '90d': 43200, all: 43200}[range], n = {'7d': 84, '30d': 180, '90d': 180, all: 200}[range];
  const t = Array.from({length: n}, (_, i) => now - now % step - (n - 1 - i) * step);
  return {ok: true, range, step, sampledAt: now - now % 3600, t,
    partitions: [{path: '/', fs: 'ext4', total: 40 * GB, used: 31 * GB}, {path: '/data', fs: 'xfs', total: 200 * GB, used: 58 * GB}, {path: '/boot', fs: 'vfat', total: 0.5 * GB, used: 0.06 * GB}],
    series: {'/': t.map((_, i) => 62 + i * 15 / n), '/data': t.map((_, i) => 22 + 7 * Math.sin(i / 9)), '/boot': t.map(() => 12)}};
}
const FIXED_TARGETS = ['yn-ct', 'yn-cu', 'yn-cm', 'cloudflare', 'google', 's:srv-papa:http:80', 'gd-ct'];
// added: [id, label] of targets on the server's own list the fixtures above do not have, measured since
// just now (no data yet).
export function latencyStats(range, stopped = false, added = []) {
  const now = Math.floor(Date.now() / 1000), step = {'1h': 60, '24h': 300, '7d': 3600, '30d': 14400}[range], n = {'1h': 60, '24h': 288, '7d': 168, '30d': 180}[range];
  // Nothing measured for six samples near the end (the node was down); all targets cut for four before that.
  const t = Array.from({length: n}, (_, i) => now - now % step - (n - 1 - i) * step).filter((_, i) => i < n - 40 || i > n - 34);
  const cut = i => i >= n - 52 && i < n - 48;
  const one = (id, label, base, jitter, lossAt, heavy = 0) => ({id, label, current: true, host: `${id}.example.cn`,
    avg: t.map((_, i) => cut(i) ? null : Math.round((base + jitter * Math.sin(i / 7) + (i % 23 === 0 ? jitter * 3 : 0)) * 10) / 10),
    loss: t.map((_, i) => cut(i) ? 100 : heavy && i % 5 === 0 ? heavy : i % lossAt === 0 ? 2.5 : 0)});
  const kinds = {'yn-ct': 'three', 'yn-cu': 'three', 'yn-cm': 'three', cloudflare: 'intl', google: 'intl'};
  return {ok: true, range, step, start: now - n * step, end: now, t, supported: true, group: '三网',
    targets: [one('yn-ct', '云南电信', 182, 12, 37), one('yn-cu', '云南联通', 96, 6, 51), one('yn-cm', '云南移动', 238, 28, 17, 33.3), one('cloudflare', 'Cloudflare', 3.2, 0.6, 400), one('google', 'Google DNS', 4.1, 0.8, 400),
      // An HTTP target its node is too old for (nothing yet), then one taken off the list a while ago (已停测), as the controller orders them.
      ...stopped ? [{...one('s:srv-papa:http:80', 'Papa-HK.T1 · HTTP 80', 0, 0, 1), avg: t.map(() => null), loss: t.map(() => null), update: true},
        {...one('gd-ct', '广东电信', 210, 10, 60), current: false, host: ''}] : [],
      ...added.map(([id, label]) => ({...one(id, label, 0, 0, 1), avg: t.map(() => null), loss: t.map(() => null)}))]
      .map(target => ({...target, kind: kinds[target.id] || 'three', primary: kinds[target.id] === 'three'}))};
}
// The board's half hour per measured server: Delta steady and far, Papa near with a loss now and then,
// the local one near with a gap where it was away.
export function latencySummary(items) {
  const now = Math.floor(Date.now() / 1000), start = now - now % 60 - 1800, shape = {
    'srv-delta': i => [182 + 9 * Math.sin(i / 3), 0], 'srv-papa': i => [38 + 4 * Math.sin(i), i % 9 === 4 ? 6 : i % 7 === 2 ? 0.8 : 0], local: i => i > 20 && i < 24 ? null : [24 + (i % 5), 0]};
  const out = {};
  for (const item of items) {
    const f = shape[item.id];
    if (!f || !item.live) continue;
    const cells = Array.from({length: 30}, (_, i) => f(i)), recent = cells.slice(-10).filter(Boolean);
    out[item.id] = {cells, group: item.id === 'srv-papa' ? '国外' : '三网', avg: recent.length ? Math.round(recent.reduce((s, c) => s + c[0], 0) / recent.length * 10) / 10 : null,
      loss: recent.length ? Math.round(recent.reduce((s, c) => s + c[1], 0) / recent.length * 10) / 10 : null};
  }
  return {ok: true, start, step: 60, items: out};
}
export const IP_QUALITY = {ok: true, previousAtMs: Date.now() - 4 * 86400000, pending: null,
  changes: [{kind: 'factor', name: '代理', from: 0, to: 1}, {kind: 'media', name: 'Prime Video', from: '屏蔽', to: '解锁 JP'}],
  report: {runId: 'r1', atMs: Date.now() - 2 * 3600000, family: 'IPv4', asn: '2914', org: 'NTT America, Inc.', kind: '原生IP',
    location: {lat: 35.6895, lon: 139.6917, city: '东京', subdivision: 'Tokyo', country: 'JP', countryName: '日本', registered: 'US', registeredName: '美国', timezone: 'Asia/Tokyo'},
    usage: {IPinfo: '机房', ipregistry: '机房', ipapi: '机房', AbuseIPDB: '机房', IP2LOCATION: '家宽'}, company: {IPinfo: '机房', ipregistry: '机房'},
    scores: [{source: 'IP2LOCATION', value: 0, level: 'low'}, {source: 'SCAMALYTICS', value: 34, level: 'mid'}, {source: 'ipapi', value: 0.4, level: 'low'}, {source: 'AbuseIPDB', value: 0, level: 'low'},
      {source: 'IPQS', value: 75, level: 'high'}, {source: 'DBIP', value: null, level: null}],
    risk: {value: 75, level: 'high', sources: 5},
    factors: [['Proxy', '代理', 1, 7], ['VPN', 'VPN', 0, 6], ['Tor', 'Tor', 0, 7], ['Server', '机房', 5, 6], ['Abuser', '滥用', 0, 6], ['Robot', '机器人', 0, 4]]
      .map(([key, name, flagged, total]) => ({key, name, flagged, total, by: flagged ? ['IPQS', 'ipapi', 'IPinfo', 'ipregistry', 'IP2LOCATION'].slice(0, flagged) : []})),
    media: [{key: 'Netflix', name: 'Netflix', status: '解锁', region: 'JP', type: '原生'}, {key: 'ChatGPT', name: 'ChatGPT', status: '解锁', region: 'JP', type: ''}],
    mail: {port25: false, listed: 0, marked: 12, checked: 423},
    purity: {value: 68, label: '一般'},
    scenes: [{key: 'tiktok', name: 'TikTok', value: 6, label: '一般', platform: 'TikTok', status: ''}, {key: 'social', name: '社媒运营', value: 3, label: '较差', platform: 'Reddit', status: '屏蔽'},
      {key: 'ai', name: 'AI 应用', value: 9, label: '极佳', platform: 'ChatGPT', status: '解锁'}]}};
// What the controller reads of an address's network and DNS (app/servers/ip_intel.py), and its ports once probed.
export const IP_INTEL = {ok: true, ip: '198.51.100.9', bogon: false, rdns: 'v198-51-100-9.example.net', asn: 2914,
  httpbl: {state: 'ok', listed: false},
  network: {space: {prefixes: 1205, ips: 7340032}, rdap: {name: 'NTT-COMMUNICATIONS-2914', org: 'NTT America, Inc.', registered: '1994-07-01'},
    peeringdb: {name: 'NTT Global IP Network', type: 'NSP', traffic: '100+Tbps', scope: 'Global'}, radar: {state: 'ok', human: 67.3, bot: 32.7}},
  ports: null};
export const PORT_SCAN = {atMs: Date.now(), ip: '198.51.100.9', checked: 31, fromSelf: false, open: [{port: 22, name: 'SSH'}, {port: 80, name: 'HTTP'}, {port: 443, name: 'HTTPS'}, {port: 6379, name: 'Redis'}]};
// A background job's output: long enough that the panel first shows only its tail.
export const JOB_LOG = Array.from({length: 2400}, (_, i) => `[${String(i + 1).padStart(4, '0')}] building chunk ${i + 1} of 2400 ……… ok\n`).join('');
export const REMINDERS = {enabled: true, time: '10:00', expiryDays: [7, 3, 1, 0], trafficPercents: [80, 90, 100], autoRenew: true};
export const ACCESS = [{id: 'ap-global', name: '海外直连', url: 'https://node.example.com'}, {id: 'ap-cn', name: '国内中转', url: 'https://node-cn.example.com:20443'}];
export const STEPS = [['check', '连接并检查系统'], ['download', '下载节点程序'], ['enroll', '注册节点'], ['service', '启动服务'], ['online', '等待上线']];
// Machines that already run a node: one connected here as another server, one of a controller we do not know.
export const CLASHES = {
  '203.0.113.66': {error: 'machine_has_node', message: '这台机器已经作为「Papa-HK.T1」接入，先删除那台或直接在它上面重新安装', existing: {nodeId: 'srv-papa', name: 'Papa-HK.T1'}},
  '203.0.113.77': {error: 'machine_has_other_node', message: '这台机器上已经装有其他节点（7f3c9a2e-1b4d-4c6e-9f00-2a7b5c1d8e43，可能属于别的主控），确认要替换时用强制安装',
    existing: {nodeId: '7f3c9a2e-1b4d-4c6e-9f00-2a7b5c1d8e43', name: ''}},
};
// The check stage stops on a clash: that step fails with the message, the rest stay pending.
export const clashed = (install, clash) => ({...install, state: 'failed', error: clash.error, message: clash.message, existing: clash.existing,
  steps: install.steps.map(x => x.key === 'check' ? {...x, state: 'failed', detail: clash.message} : {...x, state: 'pending'})});
// Each poll of a server being installed advances one step.
export function advance(install) {
  const steps = install.steps, i = steps.findIndex(x => x.state !== 'done');
  if (i < 0) return install;
  if (steps[i].state === 'running') steps[i] = {...steps[i], state: 'done', ...(steps[i].key === 'check' ? {detail: 'Debian 12 · amd64 · systemd'} : {})};
  else steps[i] = {...steps[i], state: 'running'};
  const finished = steps.every(x => x.state === 'done');
  return {...install, steps, state: finished ? 'done' : 'running'};
}
export const WIN = {'/': [{name: 'C:', type: 'dir', dir: true, size: 85 * GB, mode: '0777', mtime: 0}, {name: 'E:', type: 'dir', dir: true, size: 200 * GB, mode: '0777', mtime: 0}],
  'C:/': [{name: 'Users', type: 'dir', dir: true, size: 0, mode: '0777', mtime: 1792800000}, {name: 'backup.zip', type: 'file', size: 2048, mode: '0666', mtime: 1792800000}],
  'C:/Users': [{name: 'admin', type: 'dir', dir: true, size: 0, mode: '0777', mtime: 1792800000}]};
export const FILES = {'/root': [{name: 'app', type: 'dir', dir: true, size: 4096, mode: '0755', mtime: 1792800000}, {name: '.bashrc', type: 'file', size: 571, mode: '0644', mtime: 1792700000},
  {name: 'backup.tar.gz', type: 'file', size: 52428800, mode: '0600', mtime: 1792790000}], '/root/app': [{name: 'config.yml', type: 'file', size: 1200, mode: '0644', mtime: 1792800500}]};
export const FX = {base: 'CNY', date: '2026-10-25', rates: {CNY: 1, USD: 7.1, EUR: 8.25, GBP: 9.5}};
// 延迟监测 as the controller describes it (a few provinces of its 31), and the metrics file's tables.
const INTL = ['cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram'];
const LISTS = {domestic: ['three', 'ali-dns', 'tencent-dns'], optimized: ['three', ...INTL], plain: [...INTL]};
export const LATENCY = {enabled: true, province: '云南', intervalS: 10, method: 'tcp', card: 'auto', custom: [{id: 'u-0a1b2c3d', label: '家里 NAS', host: 'nas.example.com', port: 443, group: 'cn'}],
  defaults: structuredClone(LISTS)};
const PROVINCES = [['北京', 'bj'], ['上海', 'sh'], ['广东', 'gd'], ['云南', 'yn']];
export const LATENCY_CATALOG = {provinces: PROVINCES.map(([name, code]) => ({name, code})),
  carriers: [{id: 'ct', name: '电信'}, {id: 'cu', name: '联通'}, {id: 'cm', name: '移动'}],
  extras: [['ali-dns', '阿里 DNS', '223.5.5.5', 53, 'cn', 'dns'], ['tencent-dns', '腾讯 DNS', '119.29.29.29', 53, 'cn', 'dns'], ['114-dns', '114 DNS', '114.114.114.114', 53, 'cn', 'dns'],
    ['deepseek', 'DeepSeek', 'api.deepseek.com', 443, 'cn', 'ai'], ['kimi', 'Kimi', 'api.moonshot.cn', 443, 'cn', 'ai'],
    ['baidu', '百度', 'www.baidu.com', 443, 'cn', 'site'], ['qq', '腾讯', 'www.qq.com', 443, 'cn', 'site'], ['bilibili', '哔哩哔哩', 'www.bilibili.com', 443, 'cn', 'site'],
    ['cloudflare', 'Cloudflare', '1.1.1.1', 443, 'intl', 'dns'], ['google', 'Google DNS', '8.8.8.8', 443, 'intl', 'dns'], ['quad9', 'Quad9', '9.9.9.9', 443, 'intl', 'dns'],
    ['openai', 'OpenAI', 'api.openai.com', 443, 'intl', 'ai'], ['claude', 'Claude', 'api.anthropic.com', 443, 'intl', 'ai'], ['gemini', 'Gemini', 'generativelanguage.googleapis.com', 443, 'intl', 'ai'],
    ['grok', 'Grok', 'api.x.ai', 443, 'intl', 'ai'], ['github', 'GitHub', 'github.com', 443, 'intl', 'service'], ['telegram', 'Telegram', 'api.telegram.org', 443, 'intl', 'service'],
    ['netflix', 'Netflix', 'www.netflix.com', 443, 'intl', 'service']]
    .map(([id, label, host, port, group, kind]) => ({id, label, host, port, group, kind})), defaultLists: LISTS, maxTargets: 16, maxCustom: 32,
  directory: Object.fromEntries(PROVINCES.map(([, code]) => [code, Object.fromEntries(['ct', 'cu', 'cm'].map(c => [c, `${code}-${c}-v4.ip.zstaticcdn.com:80`]))])),
  directoryRefreshS: 86400, directoryUpdatedAtMs: Date.now() - 5 * 3600000, directoryCheckedAtMs: Date.now() - 2 * 3600000, directoryError: ''};
export const STORAGE = {retention: {minuteDays: 31, hourDays: 400}, bytes: 182 * 1024 ** 2, freeBytes: 6 * 1024 ** 2, tables: {
  minute: {rows: 503583, bytes: 66 * 1024 ** 2, days: 12.3}, ping_minute: {rows: 260000, bytes: 18 * 1024 ** 2, days: 2.1},
  hour: {rows: 8460, bytes: 1.5 * 1024 ** 2, days: 12.3}, ping_hour: {rows: 4300, bytes: 0.4 * 1024 ** 2, days: 2.1}, disk: {rows: 900, bytes: 0.1 * 1024 ** 2, days: 0.8},
  day: {rows: 520, bytes: 0.05 * 1024 ** 2, days: 12}}};
// Exits' addresses for a front: Papa's line inside the provider plus a public one behind NAT, Delta's public one on its card.
export const ADDRS = {
  'srv-papa': {interfaces: [{name: 'eth0', up: true, kind: 'ethernet', addrs: [{ip: '10.0.10.246', prefixLen: 24, family: 4, scope: 'private', egress: false}]},
    {name: 'obmesh0', up: true, kind: 'mesh', addrs: [{ip: '100.96.0.20', prefixLen: 16, family: 4, scope: 'mesh', egress: false}]}], publicIPv4: '203.0.113.38'},
  'srv-delta': {interfaces: [{name: 'ens3', up: true, kind: 'ethernet', addrs: [{ip: '198.51.100.227', prefixLen: 24, family: 4, scope: 'public', egress: true},
    {ip: '10.77.17.46', prefixLen: 24, family: 4, scope: 'private', egress: false}, {ip: 'fe80::1', prefixLen: 64, family: 6, scope: 'linklocal', egress: false}]}], publicIPv4: '198.51.100.227'},
};

// One page's backend. handle(request) answers /api requests: request is {url: URL, path, method,
// query, body (parsed JSON or null), raw (Buffer or null)}; the answer is {status, json} or
// {status, headers, body}. external(request) answers the public exchange-rate sources.
// openTerminal(send) is one terminal WebSocket: it greets, and returns the handler of a message.
export function createServersBackend({tweak = null, fx = FX, release = null, recheck = null, mesh = null, refuseMesh = []} = {}) {
  const calls = [], terminal = {sockets: 0, input: [], control: []}, uploads = [], saved = [], fileJobs = {};
    const items = fixtures();
    tweak?.(items);
    let commands = [{id: 1, name: '磁盘', command: 'df -h /', sort: 0}], settings = {accessPoints: ACCESS.map(p => ({...p})), defaultAccess: 'ap-global', timezone: ZONE, reminders: REMINDERS, ...(mesh ? {meshAccess: mesh} : {}),
      retention: {minuteDays: 31, hourDays: 400}, latency: {...LATENCY}, latencyCatalog: LATENCY_CATALOG}, job = null, storage = structuredClone(STORAGE);
    let codes = [{id: 'rc1', name: '新开的 VPS', group: 'Tier 1', access: {}, active: true, uses: 2, maxUses: 0, expiresAtMs: 0, createdAtMs: Date.now() - 86400000, lastUsedAtMs: Date.now() - 7200000, revokedAtMs: 0,
      command: 'curl -fsSL https://node.example.com/api/server-nodes/v1/install.sh | sh -s -- --url https://node.example.com --code rc1secret'}], batchJob = null, batchPolls = 0;
    // An IP.Check.Place run started from the detail page: under way for two more reads, then a fresh report.
    const ipRuns = {}, unlockRuns = {};
    const bgJobs = [
      {id: 'mh2k3j9a1b2c', command: 'cd /srv/app && ' + 'npm run build -- --mode production --verbose '.repeat(6) + '&& node scripts/deploy.js', cwd: '', shell: '/bin/bash', state: 'running', exitCode: null,
        startedAtMs: Date.now() - 754000, endedAtMs: null, durationMs: 754000},
      {id: 'mh2k1a0b9c8d', command: 'apt-get update && apt-get -y upgrade', cwd: '/root', shell: '/bin/bash', state: 'exited', exitCode: 0, startedAtMs: Date.now() - 7200000, endedAtMs: Date.now() - 7080000, durationMs: 120000},
      {id: 'mh2k0z9y8x7w', command: 'python3 train.py', cwd: '', shell: '/bin/sh', state: 'interrupted', exitCode: null, startedAtMs: Date.now() - 90000000, endedAtMs: null, durationMs: null},
    ];
    for (const i of items) if (!i.local) i.access = {primary: ACCESS[i.profile.group === 'Tier 1' ? 1 : 0], backup: i.profile.group === 'Tier 1' ? ACCESS[0] : null};
    // With the built-in mesh access point: each server says whether it can pick it, and what its choice lacks.
    const pointOf = id => id === 'ap-mesh' ? settings.meshAccess : ACCESS.find(a => a.id === id);
    const meshAccessOf = (choice, eligible, reason = '') => {
      const primary = pointOf(choice?.primary) || ACCESS[0], backup = pointOf(choice?.backup) || null;
      return {primary, backup, meshEligible: eligible, meshReason: eligible ? '' : reason || 'not_member',
        warnings: primary.id === 'ap-mesh' && !backup ? ['mesh_no_backup'] : []};
    };
    if (mesh) for (const i of items) if (!i.local) i.access = i.profile.access ? meshAccessOf(i.profile.access, Boolean(i.meshEligible), i.meshReason)
      : {...i.access, meshEligible: Boolean(i.meshEligible), meshReason: i.meshEligible ? '' : i.meshReason || 'not_member', warnings: []};
  const external = ({url}) => {
    if (!/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//.test(url.href)) return null;
    // Exchange rates come straight from public sources; none answers without fx.
    return fx ? {status: 200, headers: {'content-type': 'application/json', 'access-control-allow-origin': '*'},
      body: JSON.stringify({result: 'success', time_last_update_unix: Date.parse(`${fx.date}T00:00:00Z`) / 1000,
        rates: Object.fromEntries(Object.entries(fx.rates).map(([code, value]) => [code, 1 / value]))})} : {status: 503, body: ''};
  };
  const openTerminal = send => {
    terminal.sockets++;
    send(JSON.stringify({type: 'status', state: 'open'}));
    send(Buffer.from('root@fixture:~# '));
    return message => {
      if (typeof message === 'string') { terminal.control.push(JSON.parse(message)); return; }
      const text = Buffer.from(message).toString();
      terminal.input.push(text);
      send(Buffer.from(text.includes('\r') ? `\r\nran: ${JSON.stringify(text)}\r\nneedle-line here\r\nroot@fixture:~# ` : text));
    };
  };
  async function handle({url, path, method, body, raw, query}) {
    calls.push({path, method, body, query});
      const id = path.split('/')[3];
      const item = items.find(i => i.id === id);
      let data = {ok: true};
      // A detail page follows its install through the list: a clash shows there at the check.
      if (path === '/api/servers' && method === 'GET') for (const i of items) if (i.clash && i.install?.state === 'running') i.install = clashed(i.install, i.clash);
      // As the controller: a check switched off, or a server in mainland China, has its results kept, not listed.
      if (path === '/api/servers' && method === 'GET') data = {ok: true, settings, node: release || {available: true, version: '0.13.1', source: 'local', latest: '', checkedAtMs: 0, error: ''},
        items: items.map(i => i.node ? {...i, node: {...i.node, ...Object.fromEntries(['unlock', 'route'].filter(k => settings.checks?.[k] === false || i.region === 'CN').map(k => [k, null]))}} : i)};
      else if (path.startsWith('/api/servers/mesh')) return {status: 404, json: {ok: false, error: 'not_found'}};
      else if (path === '/api/servers/node-release/check') data = {ok: true, release: recheck || {available: true, version: '1.0.2', source: 'github', latest: '1.0.2', incompatible: '', checkedAtMs: Date.now(), error: ''}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.filter(i => i.live).map(i => [i.id, {...i.live, at: Date.now(), m: {...i.live.m, net: {...i.live.m.net, rxRate: 1e6 + Math.random() * 6e5}}}]))};
      else if (path === '/api/servers/commands' && method === 'GET') data = {ok: true, items: commands};
      else if (path === '/api/servers/commands' && method === 'POST') { commands = [...commands, {id: commands.length + 1, ...body, sort: commands.length}]; data = {ok: true, items: commands}; }
      else if (path === '/api/servers/settings' && method === 'PUT') {
        // As the controller does: what latency says changes, the rest of it stays.
        settings = {...settings, ...body, ...(body.latency ? {latency: {...settings.latency, ...body.latency}} : {})};
        data = {ok: true, settings};
      }
      else if (path === '/api/servers/storage' && method === 'GET') data = {ok: true, ...storage, retention: settings.retention};
      else if (path === '/api/servers/storage/compact') { storage = {...storage, bytes: storage.bytes - storage.freeBytes, freeBytes: 0}; data = {ok: true, ...storage, retention: settings.retention}; }
      else if (path === '/api/servers/latency/refresh') {
        // A directory the test marks unreachable fails, kept with why; otherwise it is fetched again.
        const failed = settings.latencyCatalog.unreachable;
        settings.latencyCatalog = {...settings.latencyCatalog, directoryCheckedAtMs: Date.now(), directoryError: failed ? 'ClientConnectorError: Cannot connect to host www.zstaticcdn.com:443' : '',
          ...(failed ? {} : {directoryUpdatedAtMs: Date.now()})};
        data = {ok: true, refreshed: !failed, changed: false, ...settings.latencyCatalog};
      }
      else if (path === '/api/servers/latency' && method === 'GET') data = latencySummary(items);
      else if (path === '/api/servers/bench/runs' && method === 'POST') {
        ipRuns[body.serverId] = 2;
        data = {ok: true, runs: [{id: 'run-ip', serverId: body.serverId, test: body.test, state: 'queued'}]};
      }
      else if (path === '/api/instance-servers/ssh-keys') data = {ok: true, items: [{secret: 'ssh-key', field: '私钥', type: 'ed25519', usable: true, fingerprint: 'SHA256:abc', encrypted: false}]};
      else if (path === '/api/servers' && method === 'POST') {
        const created = server('srv-new' + items.length, body.name, {group: body.profile?.group || '', node: false, ssh: body.ssh ? {host: body.ssh.host, port: body.ssh.port, username: body.ssh.username, auth: body.ssh.auth, status: 'ok'} : null});
        created.access = {primary: ACCESS.find(a => a.id === body.profile?.access?.primary) || ACCESS[0], backup: ACCESS.find(a => a.id === body.profile?.access?.backup) || null};
        items.push(created); data = {ok: true, item: created};
      } else if (path.endsWith('/install') && method === 'POST') {
        item.install = {state: 'running', startedAtMs: Date.now(), steps: STEPS.map(([key, label]) => ({key, label, state: 'pending'}))};
        item.clash = !body?.force && CLASHES[item.ssh?.host] || null;
        data = {ok: true, install: item.install};
      }
      else if (path === '/api/servers/access-points/test') data = body.url.includes('broken') ? {ok: false, error: 'access_path_not_forwarded'} : {ok: true, latencyMs: body.url.includes('-cn') ? 48 : 186};
      else if (path === '/api/servers/registration-codes' && method === 'GET') data = {ok: true, items: codes};
      else if (path === '/api/servers/registration-codes' && method === 'POST') {
        const code = {id: 'rc' + (codes.length + 1), name: body.name, group: body.group || '', access: body.access || {}, active: true, uses: 0, maxUses: body.maxUses, expiresAtMs: body.expiresInDays ? Date.now() + body.expiresInDays * 86400000 : 0,
          createdAtMs: Date.now(), lastUsedAtMs: 0, revokedAtMs: 0, command: `curl -fsSL https://node.example.com/api/server-nodes/v1/install.sh | sh -s -- --url https://node.example.com --code new${codes.length}`};
        codes = [code, ...codes]; data = {ok: true, item: code};
      } else if (path.startsWith('/api/servers/registration-codes/') && method === 'DELETE') { codes = codes.map(c => c.id === path.split('/').pop() ? {...c, active: false, revokedAtMs: Date.now(), command: ''} : c); data = {ok: true, items: codes}; }
      else if (path === '/api/servers/batch-install' && method === 'POST') {
        batchJob = {id: 'bi1', createdAtMs: Date.now(), done: false, items: body.servers.map(sv => ({...sv, state: 'pending'}))}; batchPolls = 0; data = {ok: true, job: batchJob};
        // A clashing machine gets its server record, then stops at the check.
        for (const x of batchJob.items.filter(x => CLASHES[x.host])) {
          const created = server('srv-b' + items.length, x.name, {node: false, ssh: {host: x.host, port: x.port, username: 'root', auth: 'key', status: 'ok'}});
          created.install = clashed({state: 'running', steps: STEPS.map(([key, label]) => ({key, label, state: 'pending'}))}, CLASHES[x.host]);
          items.push(created); x.serverId = created.id;
        }
      }
      else if (path === '/api/servers/batch-install/bi1') {
        batchPolls++;
        batchJob = {...batchJob, items: batchJob.items.map((x, i) => batchPolls < 2 ? {...x, state: i === 0 ? 'connecting' : 'pending'}
          : x.host === '192.0.2.99' ? {...x, state: 'failed', error: 'ssh_connect_failed'} : {...x, state: batchPolls < 3 && i ? 'installing' : 'done', install: {steps: [{key: 'download', label: '下载节点程序', state: 'running'}]}})};
        // Clashing machines: failed with their install as it is now (a forced one goes on step by step).
        batchJob.items = batchJob.items.map(x => {
          if (!x.serverId || batchPolls < 2) return x;
          const own = items.find(i => i.id === x.serverId);
          if (own.install?.state === 'running') own.install = advance(own.install);
          return {...x, state: 'failed', error: CLASHES[x.host].error, install: own.install};
        });
        batchJob.done = batchPolls >= 3; data = {ok: true, job: batchJob};
      } else if (path === '/api/servers/reminders') data = {ok: true, items: [{serverId: 'srv-papa', kind: 'traffic', text: '「Papa-HK.T1」本期流量已用 100%', status: 'enqueued', createdAtMs: Date.now() - 3600000},
        ...[8, 3, 1].map((d, i) => ({serverId: 'srv-hotel', kind: 'expiry', text: `「Hotel-HK」还有 ${d} 天到期`, status: i ? 'enqueued' : 'suppressed', createdAtMs: Date.now() - (i + 2) * 86400000})),
        {serverId: 'srv-delta', kind: 'renewed', text: '「Delta-JP」已自动顺延到 ' + day(52), status: 'enqueued', createdAtMs: Date.now() - 6 * 86400000},
        {serverId: 'srv-papa', kind: 'node_update', text: '「Papa-HK.T1」节点已更新到 0.13.1', status: 'failed', createdAtMs: Date.now() - 7 * 86400000},
        {serverId: '', kind: 'test', text: '这是一条测试提醒', status: 'enqueued', createdAtMs: Date.now() - 9 * 86400000}]};
      else if (path === '/api/servers/reminders/test') data = {ok: true, status: 'enqueued'};
      else if (path === '/api/servers/order' && method === 'PUT') {
        items.sort((a, b) => body.ids.indexOf(a.id) - body.ids.indexOf(b.id));
        items.forEach((i, index) => { i.profile.sort = index; });
        data = {ok: true, items};
      } else if (path === '/api/servers/node-url') data = {ok: true, items: items.filter(i => i.live && !i.local).map(i => ({serverId: i.id, ok: i.id !== 'srv-bravo', changed: true, backupReachable: true, ...(i.id === 'srv-bravo' ? {error: 'new_url_unreachable'} : {})}))};
      else if (path === '/api/servers/node-update') {
        const outdated = items.filter(i => i.node?.updateAvailable);
        for (const i of outdated) Object.assign(i.node, {version: '0.13.1', updateAvailable: false});
        data = {ok: true, items: outdated.map(i => ({serverId: i.id, ok: true, changed: true, from: '0.13.0', to: '0.13.1', ...(i.updateResult || {})}))};
      } else if (path === '/api/servers/exec' && method === 'POST') {
        job = {id: 'job1', command: body.command, timeout: body.timeout, createdAtMs: Date.now(), done: false, items: body.serverIds.map(serverId => ({serverId, state: 'running'}))};
        data = {ok: true, job};
      } else if (path === '/api/servers/exec/job1') {
        job = {...job, done: true, items: job.items.map(i => i.serverId === 'srv-delta' ? {serverId: i.serverId, state: 'done', ok: false, exitCode: 1, stderr: 'df: /data: No such file', durationMs: 40}
          : {serverId: i.serverId, state: 'done', ok: true, exitCode: 0, stdout: `Filesystem Size Used\n/dev/vda1 20G 8G (${i.serverId})\n`, durationMs: 120})};
        data = {ok: true, job};
      } else if (/\/jobs$/.test(path) && method === 'GET') data = {ok: true, jobs: bgJobs};
      else if (/\/jobs\/[\w]+$/.test(path) && method === 'GET') {
        const found = bgJobs.find(j => j.id === path.split('/').pop()), q = url.searchParams, limit = Number(q.get('limit') || 65536);
        const start = q.has('offset') ? Number(q.get('offset')) : Math.max(0, JOB_LOG.length - limit), text = JOB_LOG.slice(start, start + limit);
        data = {ok: true, job: found, log: {data: text, offset: start, next: start + text.length, size: JOB_LOG.length, first: 0}};
      } else if (/\/jobs\/[\w]+\/stop$/.test(path)) {
        const found = bgJobs.find(j => j.id === path.split('/')[5]);
        Object.assign(found, {state: 'stopped', exitCode: 143, endedAtMs: Date.now(), durationMs: Date.now() - found.startedAtMs});
        data = {ok: true, stopped: true, job: found};
      } else if (path.endsWith('/browser') && method === 'GET') data = {ok: true, browser: item.browserState || {enabled: Boolean(item.node?.caps?.browser), online: Boolean(item.live)}};
      else if (path.endsWith('/browser') && method === 'PUT') {
        item.node.caps = {...item.node.caps, browser: body.enabled};
        if (item.live) item.live.caps = {...item.live.caps, browser: body.enabled};
        item.browserState = body.enabled ? {enabled: true, online: true, state: 'stopped', source: '', product: '', headless: false, endpoint: body.endpoint, download: body.download, epoch: ''} : {enabled: false, online: true};
        data = {ok: true, browser: item.browserState};
      } else if (path.endsWith('/browser/restart')) { item.browserState = {...item.browserState, state: 'ready', epoch: 'e2'}; data = {ok: true, browser: item.browserState}; }
      else if (path.endsWith('/desktop/config') && method === 'GET') {
        const plat = item.node?.info?.platform || item.ssh?.platform || 'linux';
        const defaultPort = plat === 'darwin' ? 5900 : 3389;
        const savedCfg = item.desktopConfig || {};
        data = {
          ok: true,
          platform: plat,
          protocol: plat === 'darwin' ? 'vnc' : 'rdp',
          port: savedCfg.port || defaultPort,
          customPort: savedCfg.port || 0,
          defaultPort,
          username: savedCfg.username || item.ssh?.username || (plat === 'darwin' ? 'admin' : 'Administrator'),
          password: savedCfg.password || '',
          domain: savedCfg.domain || '',
          hasSaved: Boolean(savedCfg.username || savedCfg.password || savedCfg.port),
          source: savedCfg.password ? 'desktop' : (item.ssh?.username ? 'ssh' : 'none'),
        };
      }
      else if (path.endsWith('/desktop/config') && method === 'PUT') {
        item.desktopConfig = body.clear ? {} : {port: body.port || 0, username: body.username || '', password: body.password || '', domain: body.domain || ''};
        const plat = item.node?.info?.platform || item.ssh?.platform || 'linux';
        const defaultPort = plat === 'darwin' ? 5900 : 3389;
        const savedCfg = item.desktopConfig;
        data = {
          ok: true,
          platform: plat,
          protocol: plat === 'darwin' ? 'vnc' : 'rdp',
          port: savedCfg.port || defaultPort,
          customPort: savedCfg.port || 0,
          defaultPort,
          username: savedCfg.username || (plat === 'darwin' ? 'admin' : 'Administrator'),
          password: savedCfg.password || '',
          domain: savedCfg.domain || '',
          hasSaved: Boolean(savedCfg.username || savedCfg.password || savedCfg.port),
          source: savedCfg.password ? 'desktop' : 'none',
        };
      }
      else if (path.endsWith('/processes') && method === 'GET') {
        const sort = url.searchParams.get('sort');
        if (item.node?.version === 'old') return {status: 409, json: {ok: false, error: 'node_update_required'}};
        const rows = (item.processes || PROCESSES).filter(p => !(item.ended || []).includes(p.pid));
        const key = {cpu: p => p.cpu, mem: p => p.rss, io: p => (p.readRate || 0) + (p.writeRate || 0), conn: p => p.conns || 0, net: p => p.conns || 0}[sort];
        data = {ok: true, sort, sorts: ['cpu', 'mem', 'io', 'conn', 'net'], count: 214, processes: [...rows].sort((a, b) => key(b) - key(a)).slice(0, 10),
          ...(sort === 'net' ? {interfaces: [{name: 'eth0', rxRate: 2.4e6, txRate: 8.1e5}, {name: 'docker0', rxRate: 3e5, txRate: 1.2e5}, {name: 'br-71aa55b7cfe3', rxRate: 0, txRate: 0}],
            netns: [{name: 'immich_server', pids: 3, rxRate: 2.9e5, txRate: 1.1e5}]} : {})};
      } else if (path.endsWith('/processes/kill') && method === 'POST') {
        // A first, gentle try at the stubborn one leaves it running; force ends it.
        const exited = body.force || body.pid !== 4242;
        if (exited) item.ended = [...item.ended || [], body.pid];
        data = {ok: true, pid: body.pid, exited};
      } else if (path.endsWith('/details') && method === 'GET') data = {ok: true, details: DETAILS};
      else if (path.endsWith('/addresses') && method === 'GET') {
        if (!item?.live) return {status: 409, json: {ok: false, error: 'node_offline'}};
        data = {ok: true, meshAddress: '', publicIPv6: '', ...(ADDRS[id] || {interfaces: [], publicIPv4: ''})};
      }
      else if (path.endsWith('/route') && method === 'POST') {
        if (settings.checks?.route === false) return {status: 409, json: {ok: false, error: 'route_check_disabled'}};
        item.node.route = {...item.node.route || {ip: item.node.info.ipv4, results: {}}, at: Date.now()};
        data = {ok: true, route: item.node.route};
      }
      else if (path.endsWith('/unlock') && method === 'POST') {
        // A check finds the same as before, only now.
        item.node.unlock = {...item.node.unlock || {ips: [item.node.info.ipv4, item.node.info.ipv6 || ''], v4: {}}, at: Date.now()};
        // The full check follows, under way for the next two looks.
        unlockRuns[id] = 2;
        data = {ok: true, unlock: item.node.unlock};
      }
      else if (path.endsWith('/unlock-full') && method === 'GET') {
        // A real report from Spain (unlock_full.fold), three hours old; a node that cannot run it has the nine.
        const unable = (item.node?.info?.platform || 'linux') !== 'linux' ? 'platform' : item.node?.caps?.exec === false ? 'exec' : '';
        if (unlockRuns[id] > 0 && --unlockRuns[id] === 0) item.unlockFullAt = Date.now();
        data = {ok: true, off: false, running: unlockRuns[id] > 0, unable, error: null, failedAt: null,
          full: unable || item.unlockFull === null ? null : {...UNLOCK_FULL, at: item.unlockFullAt || Date.now() - 3 * 3600000, source: 'auto'}};
      }
      else if (path.endsWith('/metrics')) {
        const range = url.searchParams.get('range'), after = Number(url.searchParams.get('after') || 0);
        const step = {live: 3, '1h': 60, '24h': 300, '7d': 3600, '30d': 14400, '90d': 43200, '1y': 172800}[range];
        const all = points(range === 'live' ? 200 : 60, step), keep = all.t.map((t, i) => (after ? t > after : range !== 'live' || t > all.t.at(-1) - 600) ? i : -1).filter(i => i >= 0);
        data = {ok: true, range, step, points: Object.fromEntries(Object.entries(all).map(([k, v]) => [k, keep.map(i => v[i])]))};
      }
      else if (path.endsWith('/traffic') && method === 'GET') data = trafficStats(url.searchParams.get('unit'));
      else if (path.endsWith('/disks') && method === 'GET') data = diskStats(url.searchParams.get('range'));
      else if (path.endsWith('/bgp') || path.endsWith('/bgp/refresh')) {
        // Real RIS shapes: OVH with one upstream, a small network three steps from its Tier 1s (Delta), Google over IPv6 (Papa).
        const family = Number(url.searchParams.get('family') || body?.family || 4), v6 = item.id === 'srv-papa';
        const sample = family === 6 ? BGP.google : item.id === 'srv-delta' ? BGP.prime : BGP.ovh;
        data = {ok: true, family, ip: family === 6 ? (v6 ? '2a0e::20' : '') : item.node?.info?.ipv4 || '45.32.100.1', families: v6 ? [4, 6] : [4], error: ''};
        data = family === 6 && !v6 ? {...data, state: 'no_address'} : {...data, ...sample, atMs: Date.now() - (path.endsWith('/refresh') ? 0 : 3 * 3600000)};
      }
      else if (path.endsWith('/latency') && method === 'GET') data = item.node?.version === 'old' ? {...latencyStats(url.searchParams.get('range')), supported: false, targets: []} : latencyStats(url.searchParams.get('range'), Boolean(item.latencyStopped),
        (item.profile.latency?.targets || []).filter(id => !FIXED_TARGETS.includes(id)).map(id => [id, items.find(x => id.startsWith(`s:${x.id}`))?.name || id]));
      else if (path.endsWith('/ip-intel') && method === 'GET') data = item.ipIntel === null ? {ok: true, ip: '', ports: null} : {...IP_INTEL, ports: item.ports || null};
      else if (path.endsWith('/ip-intel/ports') && method === 'POST') { item.ports = {...PORT_SCAN, atMs: Date.now()}; data = {ok: true, ports: item.ports}; }
      else if (path.endsWith('/ip-quality') && method === 'GET') {
        // ipQuality: null for no run yet, or the report's fields to replace.
        const base = item.ipQuality === null ? {ok: true, report: null, changes: [], previousAtMs: 0, pending: null}
          : item.ipQuality ? {...IP_QUALITY, report: {...IP_QUALITY.report, ...item.ipQuality}} : IP_QUALITY;
        if (ipRuns[id] > 0) { ipRuns[id]--; data = {...base, pending: {runId: 'run-ip', state: 'running', startedMs: Date.now()}}; }
        else if (ipRuns[id] === 0) { delete ipRuns[id]; item.ipQuality = undefined; data = {...IP_QUALITY, report: {...IP_QUALITY.report, runId: 'run-ip', atMs: Date.now()}}; }
        else data = base;
      }
      else if (path.endsWith('/renew')) { item.profile.billing.expiresAt = day(70); data = {ok: true, item}; }
      else if (path.endsWith('/traffic/calibration')) {
        const t = item.traffic;
        if (method === 'PUT' && 'remaining' in body && !t.limit) return {status: 409, json: {ok: false, error: 'traffic_limit_required'}};
        const used = method === 'DELETE' ? t.rx + t.tx : 'used' in body ? body.used : t.limit - body.remaining;
        item.traffic = {...t, used, calibrated: method === 'DELETE' ? null : {atMs: Date.now(), used}};
        data = {ok: true, item};
      }
      else if (path.endsWith('/node/update')) { Object.assign(item.node, {version: '0.13.1', updateAvailable: false}); data = item.updateResult || {ok: true, changed: true, from: '0.13.0', to: '0.13.1'}; }
      else if (path.endsWith('/node') && method === 'DELETE') { const uninstalled = url.searchParams.get('uninstall') === '1'; item.node = null; item.live = null; item.traffic = null; data = {ok: true, uninstalled, item}; }
      else if (path.endsWith('/join') && method === 'POST') {
        const target = item.node.joinPending.serverId;
        if (body.action === 'keep') { item.node.joinPending = null; data = {ok: true, item}; }
        else {
          items.splice(items.indexOf(item), 1);
          data = body.action === 'takeover' ? {ok: true, serverId: target, item: items.find(i => i.id === target)} : {ok: true};
        }
      }
      else if (path.endsWith('/files/download')) return {status: 200, headers: {'content-type': 'application/octet-stream', 'content-disposition': "attachment; filename*=UTF-8''config.yml"}, body: 'port: 8080\n'};
      else if (path.endsWith('/files/upload')) {
        const target = url.searchParams.get('path');
        if (target.endsWith('/.bashrc') && url.searchParams.get('overwrite') !== '1') return {status: 409, json: {ok: false, error: 'file_exists'}};
        uploads.push({path: target, size: raw.length, overwrite: url.searchParams.get('overwrite') === '1'});
        if (raw.length < 4096) saved.push({path: target, text: raw.toString()});
        data = {ok: true, path: target, size: raw.length};
      } else if (path.endsWith('/files/op') && method === 'POST') {
        const id = `fj${Object.keys(fileJobs).length + 1}`;
        fileJobs[id] = body.name === 'exists' ? {id, op: body.op, state: 'failed', error: 'file_exists', detail: ''} : {id, op: body.op, state: 'done', error: '', detail: ''};
        data = {ok: true, job: {id, op: body.op, state: 'running'}};
      } else if (/\/files\/op\/fj\d+$/.test(path)) data = {ok: true, job: fileJobs[path.split('/').pop()]};
      else if (path.endsWith('/files') && item.node?.info?.platform === 'windows') data = {ok: true, path: url.searchParams.get('path'), entries: WIN[url.searchParams.get('path')] || [], truncated: false};
      else if (path.endsWith('/files') && item.node?.info?.platform === 'darwin') {
        return {status: 403, json: {ok: false, error: 'permission_denied'}};
      } else if (path.endsWith('/files')) data = {ok: true, path: url.searchParams.get('path'), entries: FILES[url.searchParams.get('path')] || [], truncated: false};
      else if (/^\/api\/servers\/[\w-]+$/.test(path) && method === 'GET' && item.install?.state === 'running') {
        item.install = item.clash && item.install.steps[0].state === 'running' ? clashed(item.install, item.clash) : advance(item.install);
        if (item.install.state === 'done') Object.assign(item, {node: server(item.id, item.name).node, live: server(item.id, item.name).live, traffic: server(item.id, item.name).traffic});
        data = {ok: true, item};
      } else if (/^\/api\/servers\/[\w-]+$/.test(path) && method === 'GET') data = {ok: true, item: {...item, terminalLog: [{startedAtMs: Date.now() - 3600000, endedAtMs: Date.now() - 3000000, ip: '192.0.2.1', reason: 'exited'}],
        execLog: [{source: 'ai', command: 'systemctl status nginx --no-pager', startedAtMs: Date.now() - 600000, durationMs: 80, exitCode: 0, outcome: 'ok',
            conversationUuid: 'conv-nginx', conversationTitle: '排查 nginx 502：上游超时与反向代理配置核对（含访问日志和错误日志分析）'},
          {source: 'batch', command: 'df -h /data', startedAtMs: Date.now() - 900000, durationMs: 40, exitCode: 1, outcome: 'failed'}]}};
      else if (/^\/api\/servers\/[\w-]+$/.test(path) && method === 'PATCH') {
        const moved = JSON.stringify(body.profile.access) !== JSON.stringify(item.profile.access);
        const choice = body.profile.access || {};
        if (refuseMesh.includes(item.id) && [choice.primary, choice.backup].includes('ap-mesh')) {
          return {status: 400, json: {ok: false, error: 'mesh_access_requires_member'}};
        }
        if (moved && item.live && item.id === 'srv-delta' && body.profile.access?.primary === 'ap-cn' && !body.accessApply) {
          return {status: 409, json: {ok: false, error: 'new_url_unreachable'}};
        }
        if (body.name !== undefined) item.name = body.name;
        item.profile = {...item.profile, ...body.profile};
        if (body.accessApply === 'later') {
          item.access = {primary: ACCESS.find(a => a.id === body.profile.access.primary), backup: ACCESS.find(a => a.id === body.profile.access.backup) || null, pending: true};
          data = {ok: true, item, accessPending: true};
        } else {
          if (mesh) item.access = meshAccessOf(choice, Boolean(item.access?.meshEligible), item.access?.meshReason);
          data = {ok: true, item, ...(moved && item.live ? {accessPush: {serverId: item.id, ok: true, changed: true, backupReachable: true}} : {})};
        }
      } else if (path.endsWith('/enrollment')) {
        data = {ok: true, code: 'Zm9vYmFy', issuedAtMs: Date.now(), expiresAtMs: Date.now() + 1800000, command: 'curl -fsSL https://node.example.com/api/server-nodes/v1/install.sh | sh -s -- --url https://node.example.com --code Zm9vYmFy --key mc4CAQ', localCommand: '',
          windowsCommand: "[Net.ServicePointManager]::SecurityProtocol=3072; & ([scriptblock]::Create((irm 'https://node.example.com/api/server-nodes/v1/install.ps1'))) -Url 'https://node.example.com' -Code 'Zm9vYmFy'"};
        setTimeout(() => { Object.assign(item, {node: server(item.id, item.name).node}); item.node.enrolledAtMs = Date.now(); }, 1500);
        setTimeout(() => { item.live = server(item.id, item.name).live; }, 3000);
      }
    return {status: 200, json: data};
  }
  return {calls, terminal, uploads, saved, items, handle, external, openTerminal, get settings() { return settings; }};
}

// The busiest processes and the machine in detail, as host.top and host.details return them.
export const PROCESSES = [
  {pid: 1355, name: 'python', user: 'root', command: '/srv/gateway/venv/bin/python -u /srv/gateway/server.py --workers 4 --listen 0.0.0.0:8080 --config /etc/gateway/config.yaml', cpu: 23.4, rss: 1.5 * GB, writeRate: 3.2e6, conns: 18},
  {pid: 4242, name: 'clickhouse-server', user: 'clickhouse', command: '/usr/bin/clickhouse-server --config=/etc/clickhouse-server/config.xml', cpu: 9.01, rss: 2.1 * GB, readRate: 8.4e6, writeRate: 1.1e6, conns: 6},
  {pid: 988, name: 'xray', user: 'nobody', command: '/usr/local/bin/xray run -c /etc/xray/config.json', cpu: 4.2, rss: 64 * 1024 ** 2, conns: 1204},
  {pid: 2808, name: 'valkey-server', user: 'valkey', command: 'valkey-server *:6379', cpu: 0.5, rss: 11 * 1024 ** 2, conns: 76},
  {pid: 77, name: 'kworker/u16:2-events_unbound', user: 'root', command: '', cpu: 0.2, rss: 0},
];
export const DETAILS = {hostname: 'alpha', os: 'Debian GNU/Linux 13 (trixie)', kernel: '6.12.95+deb13-amd64', arch: 'amd64', virtualization: 'kvm', boot: 1784539893,
  timezone: 'Asia/Shanghai', dns: ['1.1.1.1', '8.8.8.8'], memTotal: 64 * GB, swapTotal: 0,
  cpu: {model: 'AMD EPYC 9654 96-Core Processor', vendor: 'AuthenticAMD', sockets: 1, cores: 8, threads: 16, mhz: 2400, maxMhz: 3700, cache: 'L1d 32K · L1i 32K · L2 1M · L3 32M', flags: ['AES', 'AVX', 'AVX2', 'AVX-512', 'SSE4.2', 'AMD-V']},
  board: {vendor: 'QEMU', product: 'Standard PC (Q35 + ICH9, 2009)', bios: 'SeaBIOS 1.16.3（04/01/2014）'},
  disks: [{name: 'vda', model: '', size: 200 * GB, kind: 'virtual'}, {name: 'vdb', model: '', size: 1024 * GB, kind: 'virtual'}],
  mounts: [{path: '/', fs: 'ext4', device: '/dev/vda1', total: 197 * GB, used: 81 * GB}, {path: '/data/a/very/long/mount/point/for/backups', fs: 'xfs', device: '/dev/vdb1', total: 1000 * GB, used: 712 * GB}],
  interfaces: [{name: 'eth0', mac: '52:54:00:12:34:56', mtu: 1500, up: true, speed: 10000, addrs: ['203.0.113.7/24', '2001:db8::7/64'], rx: 9.2e12, tx: 1.7e13},
    {name: 'docker0', mac: '02:42:ac:11:00:01', mtu: 1500, up: false, addrs: ['172.17.0.1/16']}]};

// Fixture changes the scenarios share.
export const many = items => { for (let i = 1; i <= 14; i++) items.push(server(`srv-x${i}`, `Extra-${String(i).padStart(2, '0')}`, {group: ['线路', '国内', ''][i % 3], region: ['US', 'TW', 'AE', 'ES', 'CN'][i % 5]})); };
export const uninstalledLast = items => items.push(...items.splice(items.findIndex(i => i.id === 'srv-hotel'), 1));
// Where each node stands against the node build the controller picked (node.update).
export const TARGET = {version: '1.2.0', source: 'github', build: 'a1b2c3d4e5f6'};
export const RELEASE = {available: true, version: '1.2.0', source: 'github', latest: '1.2.0', incompatible: '', lacking: '', checkedAtMs: Date.now() - 3600000, error: '',
  prerelease: false, autoUpdate: false, localProblem: '', required: []};
export function versions(items) {
  const set = (id, node, update) => Object.assign(items.find(i => i.id === id).node, node, {update: {missing: [], protocolOk: true, withdrawn: false, target: TARGET, ...update}});
  set('local', {version: '1.2.0', build: 'a1b2c3d4e5f6', source: 'release', updateAvailable: false}, {state: 'current', updatable: false});
  set('srv-papa', {version: '1.0.0', build: '9f8e7d6c5b4a', source: 'release', updateAvailable: true}, {state: 'required', updatable: true, missing: ['mesh', 'net']});
  set('srv-delta', {version: '1.1.0', build: '0123456789ab', source: 'release', updateAvailable: true}, {state: 'available', updatable: true});
  set('srv-golf', {version: '1.2.0', build: 'ffeeddccbbaa', source: 'release', updateAvailable: false}, {state: 'mismatch', updatable: true});
  set('srv-bravo', {version: '1.3.0-beta.1', build: '5a5a5a5a5a5a', source: 'prerelease', updateAvailable: false}, {state: 'ahead', updatable: false});
  items.find(i => i.id === 'srv-delta').updateResult = {ok: true, changed: true, confirmed: false, state: 'unconfirmed', from: '1.1.0', to: '1.2.0', build: 'a1b2c3d4e5f6'};
}
// The controller's built-in mesh access point ('ap-mesh'): read-only in settings, offered to applied members only.
export const MESH_ON = {id: 'ap-mesh', name: '组网', url: 'http://100.96.0.1:18961', available: true, reason: ''};
export const onMesh = (primary, backup = null, eligible = true) => items => {
  for (const i of items) if (i.id === 'srv-delta' || i.id === 'srv-bravo') i.meshEligible = true;
  items.find(i => i.id === 'srv-papa').meshReason = 'node_outdated';
  const delta = items.find(i => i.id === 'srv-delta');
  delta.profile.access = {primary, backup: backup || ''};
  delta.meshEligible = eligible;
};

// Locator helpers; they work on a Playwright page and on the happy-dom page alike.
export const card = (page, name) => page.locator('.pb-card').filter({has: page.locator('.pb-name', {hasText: name})});
export const orders = h => h.calls.filter(c => c.path === '/api/servers/order');
export const meshOption = select => select.locator('option[value="ap-mesh"]');
export const off = option => option.evaluate(el => el.disabled);
export const fact = (detail, label) => detail.locator('.ix-facts > div').filter({has: detail.page().locator('dt', {hasText: new RegExp(`^${label}$`)})}).locator('dd');
