// The fake controller of bench/bench.browser.test.mjs (servers, the bench catalog, runs and
// reports), shared with bench/bench.dom.test.mjs.
import {existsSync, readFileSync} from 'node:fs';
// Real reports (as the controller stores them) make the screenshots realistic; the checks use the short ones below.
export const real = process.env.OPENBEAR_BENCH_SAMPLES && existsSync(process.env.OPENBEAR_BENCH_SAMPLES) ? JSON.parse(readFileSync(process.env.OPENBEAR_BENCH_SAMPLES, 'utf8')) : {};
export const linux = (id, name, region, arch = 'amd64', online = true) => ({id, name, region, node: {exec: true, online, info: {platform: 'linux', arch, ipv4: `198.51.100.${name.length}`}}, profile: {}});
export const SERVERS = [linux('sg', 'OVH.SG', 'SG'), linux('jp', 'RFCHost-JP2.T1', 'JP', 'arm64'), {id: 'win', name: '公司-Windows', region: 'CN', node: {exec: true, info: {platform: 'windows', arch: 'amd64'}}, profile: {}},
  linux('us', 'Misaka-Los Angeles LAX02 一个很长很长的服务器名字', 'US'), linux('idle', 'Idle-DE', 'DE', 'amd64', false),
  {id: 'mac', name: '公司-Mac mini', region: 'CN', node: {exec: true, online: true, caps: {exec: true}, info: {platform: 'darwin', arch: 'arm64'}}, profile: {}},
  {...linux('noexec', 'NoExec-HK', 'HK'), node: {online: true, caps: {exec: false}, info: {platform: 'linux', arch: 'amd64', ipv4: '198.51.100.9'}}}];
export const pageItem = s => ({...s, local: false, ssh: null, usedBy: [], install: null, access: null, traffic: null,
  live: {online: true, connectedAtMs: Date.now() - 3600000, version: '0.14.0', caps: {exec: true, terminal: true}, at: Date.now(), features: [],
    m: {cpu: 1, steal: 0, iowait: 0, mem: {total: 4e9, used: 1e9}, swap: {total: 0, used: 0}, disk: {total: 3e10, used: 3e9}, load: [0, 0, 0], io: {readRate: 0, writeRate: 0},
      net: {rx: 0, tx: 0, rxRate: 0, txRate: 0}, tcp: 1, udp: 1, processes: 50, uptime: 3600, boot: 1, nodeRss: 1e7}},
  profile: {group: '', region: s.region, sort: 0, billing: {price: 0, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
  node: {...s.node, enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now(), online: true, version: '0.14.0', latest: '0.14.0', updateAvailable: false, caps: s.node.caps || {exec: true, terminal: true}, geo: null}});
// As wide as a real report (72 columns), so a phone has to scroll it sideways.
export const IP_REPORT = '\x1b[1m' + '#'.repeat(72) + '\x1b[0m\n' + '      IP质量体检报告：139.99.*.*\n\x1b[36m一、基础信息\x1b[0m\n自治系统号：            AS16276\n服务商：  TikTok   Disney+  Netflix\n状态：   \x1b[32m 解锁 \x1b[0m   \x1b[31m 屏蔽 \x1b[0m  \x1b[33m仅自制\x1b[0m\n';
export const CATALOG = {ok: true,
  categories: [{id: 'ip', name: 'IP 质量'}, {id: 'network', name: '网络质量'}, {id: 'hardware', name: '硬件性能'}, {id: 'media', name: '流媒体与 AI 解锁'}],
  tests: [
    {id: 'ipcheck', name: 'IP.Check.Place', category: 'ip', duration: '1～3 分钟', summary: 'IP 类型与风险。', package: 'ip.check.place', version: 'v0.1.0',
      options: [{key: 'family', label: '协议', type: 'choice', default: '4', choices: [['4', '仅 IPv4'], ['6', '仅 IPv6'], ['46', 'IPv4 + IPv6']]}, {key: 'report', label: '生成在线报告', type: 'bool', default: false, help: '上传生成公开链接'}],
      warnings: [{text: '会用系统包管理器安装缺少的依赖。'}, {text: '结果会上传到 Report.Check.Place。', if: {report: [true]}}]},
    {id: 'netcheck', name: 'Net.Check.Place', category: 'network', duration: '约 8 分钟', summary: '三网延迟与回程。', package: 'net.check.place', version: 'v0.1.0',
      options: [{key: 'family', label: '协议', type: 'choice', default: '4', choices: [['4', '仅 IPv4'], ['6', '仅 IPv6'], ['46', 'IPv4 + IPv6']]},
        {key: 'mode', label: '模式', type: 'choice', default: 'low', choices: [['low', '低数据模式', '不跑三网测速'], ['full', '完整模式', '含三网测速']]}],
      warnings: [{text: '会安装依赖。'}, {text: '三网测速会消耗大量流量，通常在数 GB。', if: {mode: ['full']}}]},
    {id: 'netroute', name: 'Net.Check.Place 回程路由', category: 'network', duration: '约 5 分钟', summary: '回程路由。', package: 'net.check.place', version: 'v0.1.0',
      options: [{key: 'province', label: '省份', type: 'choice', default: '', choices: [['', '北京、上海、广东'], ['北京', '北京'], ['广东', '广东'], ['云南', '云南'], ['新疆', '新疆'], ['西藏', '西藏']]}], warnings: []},
    {id: 'geekbench5', name: 'Geekbench 5', category: 'hardware', duration: '约 5 分钟', summary: 'GB5 跑分。', package: 'geekbench5', version: 'v0.1.0', options: [],
      warnings: [{text: '成绩会上传到 Geekbench 官网。'}]},
    {id: 'unlock', name: 'Service Unlock', category: 'media', duration: '约 1 分钟', summary: '流媒体与 AI 解锁。', package: 'service-unlock', version: 'v0.1.0',
      options: [{key: 'region', label: '检测地区', type: 'choice', default: 'auto', choices: [['auto', '自动'], ['jp', '日本'], ['sea', '东南亚'], ['na', '北美'], ['eu', '欧洲']]}], warnings: []},
  ],
  suite: {name: '综合评测', summary: '四项一起跑。', duration: '约 20～30 分钟', warnings: [{text: '会用系统包管理器安装缺少的依赖（合并）。'}, {text: '压力测试会占满资源。'}], options: [{key: 'family', label: '协议', type: 'choice', default: '4', choices: [['4', '仅 IPv4'], ['46', 'IPv4 + IPv6']]}], tests: ['ipcheck', 'netcheck', 'netroute']},
};
export const T = Date.parse('2026-10-30T15:00:00Z');
export const run = (id, serverId, test, createdMs, extra = {}) => ({id, serverId, test, name: CATALOG.tests.find(t => t.id === test).name, category: CATALOG.tests.find(t => t.id === test).category,
  options: {}, label: '', package: `${CATALOG.tests.find(t => t.id === test).package} v0.1.0`, suite: '', state: 'done', stopping: false, jobId: `j${id}`, exitCode: 0, error: '', detail: '',
  createdMs, startedMs: createdMs + 2000, endedMs: createdMs + 95000, links: [], ...extra});

export function fakeBench() {
  const state = {
    runs: [
      run('r1', 'sg', 'ipcheck', T - 86400000 * 12),
      run('r2', 'sg', 'ipcheck', T - 3600000, {label: 'IPv4 + IPv6', links: ['https://Report.Check.Place/ip/abc']}),
      run('r3', 'sg', 'netcheck', T - 7200000, {label: '低数据模式'}),
      run('r4', 'jp', 'unlock', T - 600000),
      run('r5', 'jp', 'netcheck', T - 60000, {state: 'running', label: '完整模式', endedMs: null}),
      run('r6', 'us', 'netroute', T - 86400000, {state: 'failed', error: 'bench_no_report', exitCode: 7}),
      run('r7', 'sg', 'geekbench5', T - 1800000, {links: ['https://browser.geekbench.com/v5/cpu/1']}),
    ],
    next: 10,
  };
  const sections = r => {
    if (r.test === 'ipcheck') return r.label ? [{label: 'IPv4', ansi: real.ip || IP_REPORT, link: 'https://Report.Check.Place/ip/abc'}, {label: 'IPv6', ansi: IP_REPORT.replace('139.99.*.*', '2402:1f00::*')}]
      : [{label: 'IPv4', ansi: (real.ip || IP_REPORT).replace('屏蔽', '解锁')}];
    if (r.test === 'netcheck') return [{label: 'IPv4', ansi: real.net || '网络质量体检报告\n三网延迟 ⣿⣿⣶ 186\n'}];
    if (r.test === 'unlock') return [{label: '', ansi: real.unlock || 'Netflix   Yes (JP)\n'}];
    if (r.test === 'geekbench5') return [{label: '', ansi: real.gb5 || Array.from({length: 99}, (_, i) => `  Running test ${i}`).join('\n') + '\n', link: 'https://browser.geekbench.com/v5/cpu/1'}];
    return [];
  };
  const detail = r => ({...r, sections: r.state === 'done' ? sections(r) : [], log: r.state === 'failed' ? 'curl: (6) Could not resolve host: zstaticcdn.com\n' : '已安装依赖\n'});
  return {state, handle(method, path, body) {
    if (method === 'GET' && path === '/catalog') return [200, CATALOG];
    if (method === 'GET' && path === '/runs') return [200, {ok: true, runs: state.runs.slice().sort((a, b) => b.createdMs - a.createdMs)}];
    if (method === 'POST' && path === '/runs') {
      const tests = body.suite ? CATALOG.suite.tests : [body.test];
      const created = tests.map((test, i) => run(`n${state.next++}`, body.serverId, test, Date.now() + i, {state: i ? 'queued' : 'running', options: body.options || {}, label: '', endedMs: null}));
      state.runs.push(...created);
      return [200, {ok: true, runs: created}];
    }
    const one = path.match(/^\/runs\/([^/]+)(\/stop)?$/);
    const r = one && state.runs.find(x => x.id === one[1]);
    if (!r) return [404, {ok: false, error: 'bench_run_not_found'}];
    if (one[2]) { r.stopping = true; return [200, {ok: true, run: r}]; }
    if (method === 'DELETE') { state.runs = state.runs.filter(x => x !== r); return [200, {ok: true}]; }
    return [200, {ok: true, run: detail(r)}];
  }};
}
