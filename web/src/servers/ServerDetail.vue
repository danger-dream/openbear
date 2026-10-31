<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { CopyDocument, MoreFilled } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok, relativeTime, timeLabel } from '../instances/instanceHelpers.js';
import InstallCommand from './InstallCommand.vue';
import InstallProgress from './InstallProgress.vue';
import MetricChart from './MetricChart.vue';
import PlatformMark from './PlatformMark.vue';
import ServerJobs from './ServerJobs.vue';
import MeshCard from './mesh/MeshCard.vue';
import DdnsCard from '../ddns/DdnsCard.vue';
import NetAddresses from './NetAddresses.vue';
import NetProbe from './NetProbe.vue';
import { incompatibleText, nodeBuildLine, nodeUpdateText, updateResultText } from './nodeVersions.js';
import { billingResetDay, bytes, cycleLabel, cycleProgress, expiryText, expiryTone, flag, monthly, noAutofill, percent, platformOf, priceText, projectedTraffic, rate,
  regionName, safeError, terminalReason, tone, trafficTypeText, uptime, trafficMode } from './serverHelpers.js';

const props = defineProps({item: {type: Object, required: true}, settings: {type: Object, required: true}, build: {type: Object, required: true},
  now: {type: Number, required: true}, active: Boolean, servers: {type: Array, default: () => []}});
const emit = defineEmits(['edit', 'ssh', 'settings', 'terminal', 'files', 'batch', 'changed', 'removed', 'mesh', 'open']);
const busy = ref(''), error = ref(''), enrollment = ref(null), showInstall = ref(false);
const range = ref('live'), series = ref(null), log = ref({terminal: [], exec: []}), logTab = ref('exec');
const removing = ref(false), uninstall = ref(true), installBrowser = ref(false);
const browser = ref(null), browserOpen = ref(false), browserForm = reactive({mode: 'launch', endpoint: ''});
const ranges = [['live', '实时'], ['1h', '1 小时'], ['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天']];
let alive = true, generation = 0, poll, livePoll, installTimer, browserTimer;

const s = computed(() => props.item);
const state = computed(() => !s.value.node ? 'none' : s.value.live ? 'online' : 'offline');
const stateTone = {online: 'ok', offline: 'bad', none: 'off'};
const stateLabel = {online: '在线', offline: '离线', none: '未安装节点'};
const live = computed(() => s.value.live?.m || null);
// Load average and steal/iowait exist only on Unix and Linux; macOS disk I/O is not collected.
const platform = computed(() => platformOf(s.value));
const has = computed(() => ({load: platform.value !== 'windows', io: platform.value !== 'darwin', wait: platform.value === 'linux'}));
const address = computed(() => s.value.node?.info?.ipv4 || s.value.node?.info?.ipv6 || s.value.ssh?.host || '');
const caps = computed(() => s.value.node?.caps || {});
const canTerminal = computed(() => state.value === 'online' && caps.value.terminal !== false);
const canExec = computed(() => state.value === 'online' && caps.value.exec !== false);
const canJobs = computed(() => canExec.value && Boolean(s.value.live?.features?.includes('jobs')));
const trafficPct = computed(() => s.value.traffic?.limit ? Math.round(percent(s.value.traffic.used, s.value.traffic.limit)) : null);
const projection = computed(() => projectedTraffic(s.value.traffic, new Date(props.now)));
const billing = computed(() => s.value.profile.billing || {});
const cur = computed(() => billing.value.currency || '');
const progress = computed(() => cycleProgress(billing.value, new Date(props.now)));
const RING = 2 * Math.PI * 26;
const gauges = computed(() => {
  const m = live.value, info = s.value.node?.info || {};
  if (!m) return [];
  return [
    ['CPU', Math.round(m.cpu), `${info.cores || '—'} 核`],
    ['内存', Math.round(percent(m.mem.used, m.mem.total)), `${bytes(m.mem.used)} / ${bytes(m.mem.total)}`],
    ['硬盘', Math.round(percent(m.disk.used, m.disk.total)), `${bytes(m.disk.used)} / ${bytes(m.disk.total)}`],
    ['交换', Math.round(percent(m.swap.used, m.swap.total)), m.swap.total ? `${bytes(m.swap.used)} / ${bytes(m.swap.total)}` : '未启用'],
  ];
});
const tiles = computed(() => {
  const m = live.value;
  if (!m) return [];
  return [
    ['网络', `↑ ${rate(m.net.txRate)}`, `↓ ${rate(m.net.rxRate)}`],
    has.value.io && ['磁盘读写', `读 ${rate(m.io.readRate)}`, `写 ${rate(m.io.writeRate)}`],
    has.value.load && ['负载', m.load.map(v => v.toFixed(2)).join(' · '), '1 / 5 / 15 分钟'],
    ['连接', `TCP ${m.tcp} · UDP ${m.udp}`, `${m.processes} 个进程`],
    has.value.wait && ['CPU 等待', `steal ${m.steal}%`, `iowait ${m.iowait}%`, m.steal >= 20 ? 'bad' : m.steal >= 5 ? 'warn' : ''],
  ].filter(Boolean);
});
// The machine at a glance: the system, the hardware as tiles, then the node and how it connects.
const info = computed(() => s.value.node?.info || {});
const osSub = computed(() => [info.value.kernel, info.value.arch, info.value.virtualization].filter(Boolean).join(' · '));
const hardware = computed(() => [info.value.cores && ['CPU', `${info.value.cores} 核`, info.value.cpu], info.value.memTotal && ['内存', bytes(info.value.memTotal), ''],
  info.value.diskTotal && ['硬盘', bytes(info.value.diskTotal), '']].filter(Boolean));
const nodeLine = computed(() => [s.value.node?.version ? `节点 ${s.value.node.version}` : '', live.value?.nodeRss ? `占用 ${bytes(live.value.nodeRss)} 内存` : ''].filter(Boolean).join(' · '));
const systemFacts = computed(() => {
  const node = s.value.node;
  if (!node) return [];
  return [['IPv4', info.value.ipv4], ['IPv6', info.value.ipv6], ['接入', access.value], ['当前连接', connection.value],
    ['限制', [caps.value.exec === false ? '已关闭命令执行' : '', caps.value.terminal === false ? '已关闭终端' : ''].filter(Boolean).join(' · ')],
    ['构建', buildFact.value], ['安装于', timeLabel(node.enrolledAtMs)]].filter(([, v]) => v);
});
// The build a node runs (first 12 of its sha256) and where it comes from; the update state
// when it says nothing the notice above does not (current, or newer than the target).
const buildFact = computed(() => {
  const node = s.value.node, line = nodeBuildLine(node), u = node?.update || {};
  const note = u.state === 'current' ? '已是目标构建' : u.state === 'ahead' || u.downgrade && !u.updatable ? `比目标 ${u.target?.version || ''} 新，不会自动降级` : '';
  return [line, note].filter(Boolean).join(' · ');
});
const incompatibleLine = computed(() => incompatibleText(s.value.live));
const updateLine = computed(() => nodeUpdateText(s.value.node) || `节点 ${s.value.node?.version} 可以更新到${s.value.node?.newBuild ? '本地的新构建' : ` ${s.value.node?.latest}`}`);
const checkingRelease = ref(false);
async function retryRelease() {
  checkingRelease.value = true;
  try {
    const release = ok(await Api.checkNodeRelease()).release;
    if (!release.available) ElMessage.warning('暂时还是没有可用的节点版本，稍后再试');
    emit('changed');
  } catch (e) { error.value = safeError(e); }
  finally { checkingRelease.value = false; }
}
const perMonth = computed(() => billing.value.cycle !== 30 ? monthly(billing.value) : 0);
const billingFacts = computed(() => {
  const p = s.value.profile || {}, b = billing.value, t = p.traffic || {};
  return [['开始', b.cycle !== -1 ? b.startedAt : ''], ['流量', s.value.traffic ? '' : trafficText(t, trafficTypeText(t.type))], ['商家', p.provider], ['备注', p.remark]].filter(([, v]) => v);
});
function trafficText(t, type) {
  const day = s.value.traffic?.resetDay || t.resetDay || billingResetDay(billing.value), cur = billing.value.currency || '';
  const mode = trafficMode(t);
  if (mode === 'unlimited') return '不限流量';
  if (mode === 'metered') return `按流量计费 · ${cur}${t.price || 0}/GB · ${type} · 每月 ${day} 号结算`;
  return `${t.limit ? `${bytes(t.limit)} · ${type}` : '未设额度'} · 每月 ${day} 号重置`;
}
// Download's share of what went both ways this period.
const splitRx = computed(() => { const t = s.value.traffic; return t && t.rx + t.tx ? Math.round(t.rx * 100 / (t.rx + t.tx)) : 0; });
const canSsh = computed(() => s.value.local || s.value.ssh);
// Which access point the node uses, and whether it fell back to the backup.
const access = computed(() => {
  if (s.value.local) return '本机直连';
  const {primary, backup} = s.value.access || {};
  if (!primary) return '';
  const parts = [`${primary.name}（首选）`, backup ? `${backup.name}（备用）` : ''].filter(Boolean).join(' · ');
  if (s.value.access.pending) return `${parts} · 待生效，节点下次连上时切换`;
  return s.value.node?.onBackup ? `${parts} · 首选连不上，现在走备用` : parts;
});
// The access point the online node is using now, with its address (「组网 · http://100.96.0.1:18961」 when over the mesh).
// Not while a change waits for the node's next connection: which one it uses then is not known here.
const connection = computed(() => {
  if (s.value.local || !live.value || s.value.access?.pending) return '';
  const {primary, backup} = s.value.access || {};
  const point = s.value.node?.onBackup ? backup : primary;
  return point ? [point.name, point.url].filter(Boolean).join(' · ') : '';
});
const installing = computed(() => s.value.install?.state === 'running');
const installCard = computed(() => !s.value.node || showInstall.value || s.value.install && !s.value.node?.online);
const command = computed(() => !enrollment.value ? '' : s.value.local ? enrollment.value.localCommand : enrollment.value.command);
const windowsCommand = computed(() => !enrollment.value || s.value.local ? '' : enrollment.value.windowsCommand || '');
const charts = computed(() => {
  const p = series.value?.points;
  if (!p) return [];
  const pct = v => `${Math.round(v)}%`;
  return [
    {title: 'CPU', series: [{label: 'CPU', values: p.cpu, color: '#3b82f6'}, has.value.wait && {label: 'steal', values: p.steal, color: '#ef4444'}].filter(Boolean),
      format: pct, max: 100},
    {title: '内存', series: [{label: '内存', values: p.mem, color: '#8b5cf6'}], format: pct, max: 100},
    {title: '网络', series: [{label: '↓', values: p.rxRate, color: '#16a34a'}, {label: '↑', values: p.txRate, color: '#f59e0b'}], format: rate},
    has.value.io && {title: '磁盘读写', series: [{label: '读', values: p.ioRead, color: '#0ea5e9'}, {label: '写', values: p.ioWrite, color: '#f97316'}], format: rate},
    has.value.load && {title: '负载', series: [{label: '负载', values: p.load, color: '#64748b'}], format: v => Number(v).toFixed(2)},
    {title: '连接数', series: [{label: 'TCP', values: p.tcp, color: '#ec4899'}, {label: 'UDP', values: p.udp, color: '#14b8a6'}], format: v => String(Math.round(v))},
  ].filter(Boolean);
});
const BROWSER_STATES = {ready: ['ok', '运行中'], installing: ['warn', '正在下载浏览器'], starting: ['warn', '正在启动'],
  stopped: ['off', '未启动，第一次使用时启动'], failed: ['bad', '启动失败']};
const browserLine = computed(() => BROWSER_STATES[browser.value?.state] || (state.value === 'online' ? ['off', '正在读取…'] : ['off', '节点离线']));
const browserFacts = computed(() => {
  const b = browser.value;
  if (!b?.state) return [];
  const source = b.endpoint ? `已有的浏览器（${b.endpoint}）` : {system: '这台机器上安装的浏览器', download: '自动下载的测试版 Chrome'}[b.source] || '';
  return [['浏览器', b.product], ['来源', source], ['窗口', b.state === 'ready' && !b.endpoint ? (b.headless ? '无头模式' : '显示在桌面上') : ''],
    ['进度', b.state === 'installing' && b.downloaded ? `已下载 ${bytes(b.downloaded)}` : ''],
    ['下载', b.downloadsUnavailable ? '这个浏览器在别的机器上，网页下载的文件保存在那台机器，取不回来' : ''], ['原因', browserReason(b.error)]].filter(([, v]) => v);
});
function browserReason(error) {
  if (error?.startsWith('browser_account_unavailable')) return '节点找不到可用的非 root 浏览器账户，已停止启动，不会降级为 root 或关闭沙箱。可以在“设置”里连接已有的浏览器，或在机器上建立可用账户并设置 OPENBEAR_BROWSER_USER；改用下载的测试版不能解决账户问题。';
  if (error?.includes('browser_download_directory_owner_invalid')) return '共享下载目录属于其他账户，节点没有使用或修改它。请管理员核对并调整目录归属后重试。';
  const exe = /^browser_not_runnable: (.+)$/.exec(error || '')?.[1];
  return exe ? `节点没法运行这台机器上的 ${exe}：Linux 上浏览器由专用账户 openbear-browser 运行，它进不去浏览器所在的目录。`
    + '可以在“设置”里改用下载的测试版 Chrome 或连接已有的浏览器，也可以放开该目录的访问权限后重启浏览器。' : error;
}
const SOURCES = {ai: 'AI', batch: '批量', web: '页面', files: '文件', instance: '实例', gateway: '网关'};
// Runs that went over SSH while the node was offline are logged as `<source>-ssh`.
const sourceLabel = source => source?.endsWith('-ssh') ? `${SOURCES[source.slice(0, -4)] || source.slice(0, -4)} · SSH` : SOURCES[source] || source;
const OUTCOMES = {ok: '成功', failed: '失败', timeout: '超时', refused: '被拒绝', unknown: '结果未知', started: '已启动'};
const duration = entry => {
  if (!entry.endedAtMs) return '进行中';
  const minutes = Math.round((entry.endedAtMs - entry.startedAtMs) / 60000);
  return minutes < 1 ? '不到 1 分钟' : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
};

async function loadSeries() {
  const span = range.value, mine = ++generation;
  if (!s.value.node) { series.value = null; return; }
  try {
    const data = ok(await Api.serverMetrics(s.value.id, span));
    if (alive && mine === generation) series.value = data;
  } catch (e) { if (alive && mine === generation) error.value = safeError(e); }
}
async function loadLog() {
  try {
    const item = ok(await Api.server(s.value.id)).item;
    log.value = {terminal: item.terminalLog || [], exec: item.execLog || []};
  } catch { /* optional */ }
}
watch(range, () => { series.value = null; void loadSeries(); });
watch(() => s.value.node?.enrolledAtMs, () => { void loadSeries(); });
watch(() => props.active, value => { if (value) { void loadSeries(); void loadLog(); } });

async function run(name, fn, success) {
  if (busy.value) return;
  busy.value = name; error.value = '';
  try { const value = await fn(); if (success) ElMessage.success(typeof success === 'function' ? success(value) : success); return value; }
  catch (e) { error.value = safeError(e); }
  finally { busy.value = ''; }
}
async function copy(text, label) {
  try { await navigator.clipboard.writeText(text); ElMessage.success(`已复制${label}`); }
  catch { ElMessage.warning('浏览器不允许复制，请手动选择文字'); }
}
function generate() { return run('generate', async () => { enrollment.value = ok(await Api.serverEnrollment(s.value.id, {browser: installBrowser.value})); }); }
// force: replace a node this controller does not know (machine_has_other_node), already confirmed.
function installViaSsh(force = false) {
  return run('install', async () => {
    ok(await Api.installServerNode(s.value.id, {browser: installBrowser.value, ...(force === true ? {force: true} : {})}));
    emit('changed');
    const tick = () => {
      if (!alive) return;
      emit('changed');
      if (s.value.install?.state === 'running' || !s.value.install) installTimer = setTimeout(tick, 2000);
      else if (s.value.install.state === 'done') ElMessage.success('节点已安装');
    };
    installTimer = setTimeout(tick, 2000);
  });
}
function updateNode(downgrade = false) {
  return run('update', async () => {
    const result = ok(await Api.updateServerNode(s.value.id, {downgrade}));
    emit('changed');
    // Confirmed: the node is back on the new build. Otherwise the file was replaced but it was not seen running it.
    const {tone, text} = updateResultText(result);
    ElMessage[tone](text);
    return result;
  });
}
function renew() {
  return run('renew', async () => { ok(await Api.renewServer(s.value.id)); emit('changed'); }, '已顺延一个周期');
}
async function checkSsh() {
  await run('ssh', async () => {
    const result = ok(await Api.checkInstanceServer(s.value.id));
    emit('changed');
    ElMessage[result.item.status === 'unreachable' ? 'warning' : 'success'](result.item.status === 'unreachable' ? '连接失败' : '连接正常');
  });
}
async function loadBrowser() {
  clearTimeout(browserTimer);
  if (!s.value.node) { browser.value = null; return; }
  try { browser.value = ok(await Api.serverBrowser(s.value.id)).browser; } catch { browser.value = null; }
  // A download or start takes a while; follow it until it settles.
  if (alive && props.active && ['installing', 'starting'].includes(browser.value?.state)) browserTimer = setTimeout(loadBrowser, 3000);
}
function openBrowser() {
  browserForm.mode = browser.value?.endpoint ? 'endpoint' : browser.value?.download ? 'download' : 'launch';
  browserForm.endpoint = browser.value?.endpoint || '';
  browserOpen.value = true;
}
function saveBrowser(enabled) {
  return run('browser', async () => {
    const endpoint = enabled && browserForm.mode === 'endpoint' ? browserForm.endpoint.trim() : '';
    browser.value = ok(await Api.setServerBrowser(s.value.id, {enabled, endpoint, download: enabled && browserForm.mode === 'download'})).browser;
    browserOpen.value = false;
    emit('changed');
    setTimeout(loadBrowser, 1500);
  }, enabled ? '浏览器已开启' : '浏览器已关闭');
}
async function restartBrowser() {
  if (!await confirm(`重启「${s.value.name}」的浏览器？上面打开的页面会关闭，登录状态保留。`, '重启浏览器', '重启')) return;
  await run('browser', async () => { browser.value = ok(await Api.restartServerBrowser(s.value.id)).browser; void loadBrowser(); }, '浏览器已重启');
}
watch(() => [s.value.id, caps.value.browser, state.value, props.active], () => { if (props.active) void loadBrowser(); });
async function confirm(text, title, button) {
  try { await ElMessageBox.confirm(text, title, {confirmButtonText: button, cancelButtonText: '取消', type: 'warning'}); return true; } catch { return false; }
}
async function removeNode() {
  const remote = uninstall.value && state.value === 'online';
  const result = await run('revoke', async () => ok(await Api.revokeServerNode(s.value.id, remote)));
  if (!result) return;
  removing.value = false;
  ElMessage.success(result.uninstalled ? '节点已移除，并已从服务器上卸载' : remote ? '节点已移除，但没能在服务器上卸载' : '节点已移除');
  emit('changed');
}
// Joined by a registration code looking like an existing server (same machine id and host name):
// it waits as a server of its own until the owner says which it is.
const joinPending = computed(() => s.value.node?.joinPending || null);
async function settleJoin(action) {
  const item = s.value, other = joinPending.value?.name || '';
  const asks = {
    takeover: [`确认这就是「${other}」？「${other}」改由这台机器的节点连接，原来的节点被断开；「${item.name}」这条记录删除，「${other}」的配置和监控数据保留。`, '接管原服务器', '接管'],
    reject: [`拒绝这台机器接入？「${item.name}」和它的节点会被删除，「${other || '原服务器'}」不受影响。`, '拒绝接入', '拒绝'],
  };
  if (asks[action] && !await confirm(...asks[action])) return;
  const result = await run('join', async () => ok(await Api.settleServerJoin(item.id, action)),
    {takeover: `已由这台机器接管「${other}」`, keep: '已作为新服务器保留', reject: '已拒绝接入'}[action]);
  if (!result) return;
  if (action === 'keep') emit('changed');
  else {
    if (action === 'takeover') emit('open', result.serverId);
    emit('removed', item.id);
  }
}
async function more(action) {
  const item = s.value;
  if (action === 'ssh') emit('ssh', item);
  else if (action === 'edit') emit('edit', item);
  else if (action === 'batch') emit('batch', item);
  else if (action === 'reinstall') { showInstall.value = true; enrollment.value = null; }
  else if (action === 'update') void updateNode();
  else if (action === 'downgrade' && await confirm(`这是降级：「${item.name}」的节点会从 ${item.node.version} 换成更旧的 ${item.node.update.target.version}，节点会重启，打开的终端会断开。`, '降级节点', '降级')) void updateNode(true);
  else if (action === 'revoke') { uninstall.value = state.value === 'online'; removing.value = true; }
  else if (action === 'unssh' && await confirm(`删除「${item.name}」的 SSH 连接信息？节点和监控数据保留。`, '移除 SSH 连接', '移除')) {
    await run('unssh', async () => { ok(await Api.updateInstanceServer(item.id, {name: item.name, auth: 'none'})); emit('changed'); }, '已移除');
  } else if (action === 'delete' && await confirm(`删除「${item.name}」的登记信息、节点和全部监控数据？服务器本身不受影响。`, '删除服务器', '删除')) {
    await run('delete', async () => { ok(await Api.deleteServer(item.id)); emit('removed', item.id); }, '已删除');
  }
}
onMounted(() => {
  void loadSeries(); void loadLog(); void loadBrowser();
  poll = setInterval(() => { if (!document.hidden && props.active) { if (range.value === '1h') void loadSeries(); void loadLog(); } }, 30000);
  livePoll = setInterval(() => { if (!document.hidden && props.active && range.value === 'live' && state.value === 'online') void loadSeries(); }, 3000);
});
onBeforeUnmount(() => { alive = false; clearInterval(poll); clearInterval(livePoll); clearTimeout(installTimer); clearTimeout(browserTimer); });
</script>
<template>
  <article class="sv-detail" :aria-label="s.name + '详情'">
    <header class="sv-detail-head">
      <div class="ix-hero">
        <span class="ix-avatar is-large sv-avatar" :data-tone="stateTone[state]" aria-hidden="true">{{ flag(s.region) || '🖥' }}</span>
        <div class="ix-hero-text">
          <div class="instance-row-heading"><h2>{{ s.name }}<PlatformMark :item="s" /></h2><span class="ob-dot" :data-tone="stateTone[state]">{{ stateLabel[state] }}</span></div>
          <p class="sv-hero-meta">
            <span v-if="regionName(s.region)">{{ regionName(s.region) }}</span>
            <button v-if="address" type="button" class="sv-copy mono" title="复制 IP" @click="copy(address, ' IP')">{{ address }}<el-icon><CopyDocument /></el-icon></button>
            <span v-if="live">已运行 {{ uptime(live.uptime) }}</span>
            <span v-else-if="state === 'offline' && s.node.lastSeenAtMs">最后在线 {{ relativeTime(s.node.lastSeenAtMs, now) }}</span>
            <span v-if="!address && s.local">主控所在服务器</span>
          </p>
          <p v-if="s.profile.group || s.profile.tags?.length" class="sv-hero-tags" aria-label="分组和标签">
            <span v-if="s.profile.group" class="pb-tag sv-group-tag" title="分组">{{ s.profile.group }}</span>
            <span v-for="tag in s.profile.tags || []" :key="tag" class="pb-tag is-plain">{{ tag }}</span>
          </p>
        </div>
        <div class="instance-actions sv-hero-actions">
          <el-button v-if="canTerminal" type="primary" @click="emit('terminal', s)">终端</el-button>
          <el-button v-if="canExec" @click="emit('files', s)">文件</el-button>
          <el-button v-if="canExec" class="sv-hide-narrow" @click="emit('batch', s)">执行命令</el-button>
          <el-button class="sv-hide-narrow" @click="emit('edit', s)">编辑</el-button>
          <el-dropdown trigger="click" :disabled="Boolean(busy)" @command="more">
            <el-button class="ix-more" aria-label="更多操作"><el-icon><MoreFilled /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit" class="sv-only-narrow">编辑…</el-dropdown-item>
                <el-dropdown-item v-if="canExec" command="batch" class="sv-only-narrow">执行命令…</el-dropdown-item>
                <el-dropdown-item v-if="!s.local" command="ssh">{{ s.ssh ? '编辑 SSH 连接…' : '登记 SSH 连接…' }}</el-dropdown-item>
                <el-dropdown-item v-if="s.node?.updateAvailable && caps.exec !== false" command="update">{{ s.node.update?.state === 'mismatch' || s.node.newBuild ? '更新节点到主控选定的构建' : `${s.node.update?.downgrade ? '降级' : '更新'}节点到 ${s.node.update?.target?.version || s.node.latest}` }}</el-dropdown-item>
                <el-dropdown-item v-if="canExec && s.node?.update?.downgrade && !s.node.updateAvailable" command="downgrade">降级节点到 {{ s.node.update.target.version }}…</el-dropdown-item>
                <el-dropdown-item v-if="s.node" command="reinstall">重新安装节点…</el-dropdown-item>
                <el-dropdown-item v-if="s.ssh && !s.usedBy.length" command="unssh" divided>移除 SSH 连接…</el-dropdown-item>
                <el-dropdown-item v-if="s.node" command="revoke" class="ob-danger" :divided="!(s.ssh && !s.usedBy.length)">移除节点…</el-dropdown-item>
                <el-dropdown-item v-if="!s.local" command="delete" class="ob-danger" :disabled="s.usedBy.length > 0">{{ s.usedBy.length ? '删除（正在被实例使用）' : '删除服务器…' }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
    </header>
    <p v-if="error" role="alert" class="instance-error instance-banner">{{ error }}</p>
    <p v-if="incompatibleLine" role="alert" class="instance-error instance-banner" data-node-incompatible>{{ incompatibleLine }}</p>
    <div v-if="s.node?.updateAvailable" class="pb-notice" role="status" :data-node-state="s.node.update?.state">
      <span v-if="caps.exec !== false">{{ updateLine }}，更新时节点会重启，打开的终端会断开。</span>
      <span v-else>{{ updateLine }}；它关闭了命令执行，需要在服务器上重新运行安装命令来更新。</span>
      <span class="ob-grow" />
      <el-button v-if="caps.exec !== false" size="small" :loading="busy === 'update'" @click="updateNode">更新</el-button>
      <el-button v-else size="small" @click="showInstall = true">安装命令</el-button>
    </div>
    <div v-if="joinPending" class="pb-notice sv-join" role="status" data-join-pending>
      <span v-if="joinPending.name">这台机器通过注册码「{{ joinPending.code }}」接入，机器 ID 和主机名与「{{ joinPending.name }}」相同：可能是它重装后重新接入，也可能是从同一模板克隆的另一台。请确认。</span>
      <span v-else>这台机器通过注册码「{{ joinPending.code }}」接入时，和一台已删除的服务器像是同一台。请确认是否保留。</span>
      <span class="ob-grow" />
      <span class="sv-join-actions">
        <el-button v-if="joinPending.name" size="small" type="primary" :loading="busy === 'join'" :disabled="Boolean(busy)" @click="settleJoin('takeover')">接管「{{ joinPending.name }}」</el-button>
        <el-button size="small" :disabled="Boolean(busy)" @click="settleJoin('keep')">作为新服务器保留</el-button>
        <el-button size="small" type="danger" plain :disabled="Boolean(busy)" @click="settleJoin('reject')">拒绝</el-button>
      </span>
    </div>

    <section v-if="installCard" class="ix-card sv-install">
      <header class="ix-card-head"><h3>{{ s.node ? '重新安装节点' : '安装节点' }}</h3><span class="instance-muted">节点主动连回主控，服务器不需要开放任何端口</span></header>
      <InstallProgress v-if="s.install" :install="s.install" :busy="Boolean(busy) || installing" @open="emit('open', $event)" @force="installViaSsh(true)" />
      <p v-if="!build.available" class="ob-notice ob-row" data-node-release="none">暂时没有可用的节点版本，稍后重试。<button type="button" class="ix-text-button" :disabled="checkingRelease" @click="retryRelease">{{ checkingRelease ? '检查中…' : '重试' }}</button></p>
      <p v-else-if="!settings.accessPoints?.length && !s.local" class="ob-notice ob-row">还没有接入点，服务器上的节点不知道该连哪里。<button type="button" class="ix-text-button" @click="emit('settings')">去设置</button></p>
      <label v-if="!s.local" class="ob-check sv-install-browser"><input v-model="installBrowser" type="checkbox" /><span>同时开启浏览器<small class="instance-muted">主控可以用这台机器的浏览器和网络访问网页；没装 Chrome 时会自动下载</small></span></label>
      <div class="instance-actions">
        <el-button v-if="canSsh" type="primary" :loading="installing" :disabled="Boolean(busy) || installing || !build.available || (!settings.accessPoints?.length && !s.local)" @click="installViaSsh">{{ s.local ? '在本机安装' : '通过 SSH 安装' }}</el-button>
        <el-button :disabled="Boolean(busy) || !build.available || (!settings.accessPoints?.length && !s.local)" @click="generate">{{ enrollment ? '重新生成安装命令' : '生成安装命令' }}</el-button>
        <el-button v-if="!canSsh" :disabled="Boolean(busy)" @click="emit('ssh', s)">登记 SSH 后一键安装</el-button>
      </div>
      <InstallCommand v-if="command" :command="command" :windows-command="windowsCommand" :platform="s.node?.info?.platform || s.ssh?.platform || ''" note />
    </section>

    <template v-if="s.node">
      <section class="ix-card">
        <header class="ix-card-head"><h3>实时</h3><span class="instance-muted">{{ live ? '每 3 秒更新' : s.node.lastSeenAtMs ? `离线 · 最后在线 ${relativeTime(s.node.lastSeenAtMs, now)}` : '节点尚未连接' }}</span></header>
        <div v-if="live" class="sv-gauges">
          <div v-for="[label, value, hint] in gauges" :key="label" class="sv-gauge" :data-tone="tone(value)">
            <svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="26" /><circle v-if="value > 0" cx="32" cy="32" r="26" :stroke-dasharray="`${value / 100 * RING} ${RING}`" /></svg>
            <div><span>{{ label }}</span><strong>{{ value }}<small>%</small></strong><small>{{ hint }}</small></div>
          </div>
        </div>
        <div v-if="live" class="sv-tiles">
          <div v-for="[label, main, sub, alarm] in tiles" :key="label" class="sv-tile" :data-tone="alarm || undefined"><span>{{ label }}</span><strong>{{ main }}</strong><small>{{ sub }}</small></div>
        </div>
        <p v-else class="instance-muted">节点在线后显示实时数据。</p>
      </section>

      <section class="ix-card">
        <header class="ix-card-head">
          <h3>走势</h3>
          <div class="sv-ranges" role="tablist" aria-label="时间范围">
            <button v-for="[key, label] in ranges" :key="key" type="button" role="tab" :aria-selected="range === key" @click="range = key">{{ label }}</button>
          </div>
        </header>
        <p v-if="!series" class="instance-muted" role="status">正在读取…</p>
        <p v-else-if="range === 'live' && !series.points.t.length" class="instance-muted">节点在线后，这里显示最近 10 分钟每 3 秒一个点的数据。</p>
        <div v-else class="sv-charts">
          <MetricChart v-for="chart in charts" :key="chart.title" :title="chart.title" :t="series.points.t" :step="series.step" :series="chart.series" :format="chart.format" :max="chart.max || 0" />
        </div>
      </section>
    </template>

    <div class="ix-grid sv-cards">
      <section v-if="s.node" class="ix-card sv-system">
        <header class="ix-card-head"><h3>系统</h3><span class="instance-muted">{{ nodeLine }}</span></header>
        <div class="sv-os"><strong :title="info.os">{{ info.os || '系统未知' }}</strong><span v-if="osSub" class="instance-muted" :title="osSub">{{ osSub }}</span></div>
        <div v-if="hardware.length" class="sv-hw">
          <div v-for="[label, main, sub] in hardware" :key="label" class="sv-hw-item"><span>{{ label }}</span><strong>{{ main }}</strong><small v-if="sub" :title="sub">{{ sub }}</small></div>
        </div>
        <dl v-if="systemFacts.length" class="ix-facts sv-system-facts">
          <div v-for="[label, value] in systemFacts" :key="label"><dt>{{ label }}</dt><dd class="ix-ellipsis" :class="{mono: label === 'IPv4' || label === 'IPv6'}" :title="value">{{ value }}</dd></div>
        </dl>
      </section>
      <section class="ix-card sv-billing">
        <header class="ix-card-head"><h3>账单</h3><span class="ob-grow" />
          <a v-if="s.profile.providerUrl" class="ix-text-button" :href="s.profile.providerUrl" target="_blank" rel="noopener noreferrer" title="打开商家的续费页面">去续费 ↗</a>
          <button v-if="billing.cycle > 0" type="button" class="ix-text-button" :disabled="Boolean(busy)" title="续费后点这里，把到期日顺延一个周期" @click="renew">续费</button>
          <button type="button" class="ix-text-button" @click="emit('edit', s)">编辑</button>
        </header>
        <div v-if="billing.expiresAt && billing.cycle !== -1" class="sv-expiry" :data-tone="expiryTone(billing, new Date(now))">
          <div><strong>{{ expiryText(billing, new Date(now)) }}</strong><span class="instance-muted">{{ billing.expiresAt }} 到期</span></div>
          <i v-if="progress !== null" class="sv-bar" :style="{'--v': progress + '%'}" aria-hidden="true"></i>
        </div>
        <div class="sv-price">
          <strong>{{ priceText(billing) || '未填价格' }}</strong>
          <span v-if="perMonth" class="instance-muted">约 {{ cur }}{{ perMonth.toFixed(2) }}/月</span>
          <span class="ob-grow" />
          <span class="pb-tag is-plain">{{ cycleLabel(billing.cycle ?? 0) }}</span>
          <span class="pb-tag" :class="{'is-plain': !billing.autoRenew}" :data-tone="billing.autoRenew ? 'ok' : null">{{ billing.autoRenew ? '自动续费' : '手动续费' }}</span>
        </div>
        <dl class="ix-facts">
          <div v-for="[label, value] in billingFacts" :key="label"><dt>{{ label }}</dt><dd :class="{'sv-remark': label === '备注'}">{{ value }}</dd></div>
        </dl>
      </section>
      <NetAddresses v-if="s.node" :server-id="s.id" :online="state === 'online'" card />
      <section v-if="s.traffic" class="ix-card">
        <header class="ix-card-head"><h3>本期流量</h3><span class="instance-muted">自 {{ s.traffic.periodStart }} 起 · {{ s.traffic.mode === 'metered' ? '结算' : '重置' }}于每月 {{ s.traffic.resetDay }} 号</span></header>
        <div class="sv-traffic">
          <svg v-if="trafficPct !== null" class="sv-traffic-ring" viewBox="0 0 64 64" :data-tone="tone(trafficPct)" aria-hidden="true">
            <circle cx="32" cy="32" r="26" /><circle cx="32" cy="32" r="26" :stroke-dasharray="`${Math.min(trafficPct, 100) / 100 * RING} ${RING}`" /><text x="32" y="36">{{ trafficPct }}%</text>
          </svg>
          <div class="sv-traffic-main">
            <p class="sv-traffic-used"><strong>{{ bytes(s.traffic.used) }}</strong><span v-if="s.traffic.limit" class="instance-muted"> / {{ bytes(s.traffic.limit) }}</span>
              <span class="pb-tag is-plain">{{ s.traffic.mode === 'unlimited' ? '不限流量' : trafficTypeText(s.traffic.type) }}</span></p>
            <div class="sv-split" :title="`下行 ${bytes(s.traffic.rx)} · 上行 ${bytes(s.traffic.tx)}`">
              <i class="sv-split-bar" aria-hidden="true"><b :style="{width: splitRx + '%'}" /></i>
              <span><i class="is-rx" aria-hidden="true" />下行 {{ bytes(s.traffic.rx) }}</span><span><i class="is-tx" aria-hidden="true" />上行 {{ bytes(s.traffic.tx) }}</span>
            </div>
            <p v-if="s.traffic.mode === 'metered'" class="sv-traffic-note">本期约 {{ cur }}{{ (s.traffic.cost || 0).toFixed(2) }}（{{ cur }}{{ s.traffic.price }}/GB）</p>
            <p v-if="projection" class="sv-traffic-note" :class="{'ob-warn': s.traffic.limit && projection.bytes > s.traffic.limit}">按目前速度，本期约 {{ bytes(projection.bytes) }}{{ s.traffic.mode === 'metered' ? `，约 ${cur}${(projection.bytes / 1024 ** 3 * s.traffic.price).toFixed(2)}` : '' }}{{ s.traffic.limit && projection.bytes > s.traffic.limit ? '，会超出额度' : '' }}</p>
          </div>
        </div>
      </section>
      <section v-if="s.ssh" class="ix-card">
        <header class="ix-card-head"><h3>SSH 连接</h3><span class="ob-grow" />
          <button type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="checkSsh">测试</button>
          <button type="button" class="ix-text-button" @click="emit('ssh', s)">编辑</button>
        </header>
        <dl class="ix-facts">
          <div><dt>地址</dt><dd class="mono ix-ellipsis">{{ s.ssh.username }}@{{ s.ssh.host }}:{{ s.ssh.port }}</dd></div>
          <div><dt>认证</dt><dd class="ix-ellipsis">{{ s.ssh.keyLabel || '密码' }}</dd></div>
          <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="s.ssh.status === 'ok' ? 'ok' : s.ssh.status === 'unreachable' ? 'bad' : 'off'">{{ {ok: '连接正常', unreachable: '无法连接'}[s.ssh.status] || '尚未验证' }}</span></dd></div>
          <div v-if="s.usedBy.length"><dt>使用中</dt><dd>{{ s.usedBy.join('、') }}</dd></div>
        </dl>
      </section>
      <MeshCard :server-id="s.id" :active="active" @open="emit('mesh', $event)" />
      <DdnsCard v-if="s.node" :server-id="s.id" :active="active" />
    </div>
    <NetProbe v-if="s.node" :server-id="s.id" :servers="servers" :online="state === 'online'" :active="active" card />
    <div class="ix-grid sv-cards">
      <section v-if="s.node" class="ix-card">
        <header class="ix-card-head"><h3>浏览器</h3><span class="ob-grow" />
          <button v-if="caps.browser && state === 'online'" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="restartBrowser">重启</button>
          <button v-if="canExec && (caps.browser || !s.local)" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="openBrowser">{{ caps.browser ? '设置' : '开启' }}</button>
        </header>
        <p v-if="s.local && !caps.browser" class="instance-muted">本机不需要开启：浏览器工具不指定服务器时，用的就是设置里配置的默认浏览器（CDP 地址）。</p>
        <dl v-else-if="caps.browser" class="ix-facts">
          <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="browserLine[0]">{{ browserLine[1] }}</span></dd></div>
          <div v-for="[label, value] in browserFacts" :key="label"><dt>{{ label }}</dt><dd :class="label === '原因' ? 'sv-browser-error' : 'ix-ellipsis'" :title="value">{{ value }}</dd></div>
        </dl>
        <p v-else class="instance-muted">未开启。开启后，主控可以用这台机器的浏览器、网络和 IP 访问网页，并使用在那个浏览器里登录过的账号。{{ caps.exec === false ? '这台节点关闭了命令执行，要在服务器上重新运行带 --browser 的安装命令来开启。' : '' }}</p>
      </section>
      <ServerJobs v-if="canJobs" :server-id="s.id" :active="active" :now="now" />
      <section v-if="log.exec.length || log.terminal.length" class="ix-card sv-logs" aria-label="命令记录">
        <header class="ix-card-head">
          <div class="sv-ranges" role="tablist" aria-label="记录">
            <button type="button" role="tab" :aria-selected="logTab === 'exec'" @click="logTab = 'exec'">命令记录</button>
            <button type="button" role="tab" :aria-selected="logTab === 'terminal'" @click="logTab = 'terminal'">终端记录</button>
          </div>
          <span class="instance-muted">只记录命令和时间，不记录输出</span>
        </header>
        <ol v-if="logTab === 'exec'" class="sv-log">
          <li v-for="entry in log.exec" :key="entry.startedAtMs + entry.command" :data-tone="entry.outcome === 'ok' ? 'ok' : entry.outcome === 'failed' || entry.outcome === 'timeout' ? 'bad' : 'off'">
            <span class="sv-log-source">{{ sourceLabel(entry.source) }}</span>
            <code class="sv-log-command" :title="entry.command">{{ entry.command }}</code>
            <span class="instance-muted">{{ OUTCOMES[entry.outcome] || '' }}{{ entry.exitCode != null && entry.outcome === 'failed' ? ` ${entry.exitCode}` : '' }}</span>
            <time :title="timeLabel(entry.startedAtMs)">{{ relativeTime(entry.startedAtMs, now) }}</time>
          </li>
          <li v-if="!log.exec.length" class="instance-muted">还没有通过页面或 AI 执行过命令。</li>
        </ol>
        <ol v-else class="sv-log">
          <li v-for="entry in log.terminal" :key="entry.startedAtMs" :data-tone="entry.endedAtMs ? 'off' : 'ok'">
            <span class="sv-log-source">终端</span>
            <span class="sv-log-command">{{ duration(entry) }}</span>
            <span class="instance-muted">{{ [entry.ip, entry.endedAtMs ? terminalReason(entry.reason) : ''].filter(Boolean).join(' · ') }}</span>
            <time :title="timeLabel(entry.startedAtMs)">{{ relativeTime(entry.startedAtMs, now) }}</time>
          </li>
          <li v-if="!log.terminal.length" class="instance-muted">还没有打开过终端。</li>
        </ol>
      </section>
    </div>

    <el-dialog v-model="browserOpen" :title="caps.browser ? '浏览器设置' : '开启浏览器'" width="460px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body>
      <form class="instance-form" autocomplete="off" @submit.prevent="saveBrowser(true)">
        <div class="ob-radios sv-browser-modes" role="radiogroup" aria-label="浏览器来源">
          <label><input v-model="browserForm.mode" type="radio" value="launch" />由节点启动浏览器</label>
          <label><input v-model="browserForm.mode" type="radio" value="download" />使用下载的测试版 Chrome</label>
          <label><input v-model="browserForm.mode" type="radio" value="endpoint" />连接已有的浏览器</label>
        </div>
        <small v-if="browserForm.mode === 'launch'" class="instance-muted">优先用这台机器上的 Chrome、Edge 或 Chromium，都没有时自动下载 Google 官方的测试版 Chrome。Windows 和 macOS 上窗口显示在当前登录用户的桌面上，服务器上用无头模式。登录状态保存在节点专用的资料目录里。</small>
        <small v-else-if="browserForm.mode === 'download'" class="instance-muted">不用这台机器上已装的浏览器，由节点下载 Google 官方的测试版 Chrome，放在节点目录里，节点更新时一起更新。适合已装的浏览器节点运行不了的情况。</small>
        <template v-else>
          <input v-model="browserForm.endpoint" v-bind="noAutofill" required class="mono" placeholder="http://127.0.0.1:9222 或 ws://…" aria-label="浏览器调试地址" />
          <small class="instance-muted">这台机器上能访问到的 Chrome 调试（CDP）地址，例如自己启动的 Chrome 或指纹浏览器容器。接入后，该浏览器的下载会保存到 OpenBear 的下载目录，以内部编号命名，保留一小时；未接管页面的下载不会被取消。</small>
        </template>
        <div class="instance-form-footer">
          <el-button v-if="caps.browser" type="danger" plain :loading="busy === 'browser'" @click="saveBrowser(false)">关闭浏览器</el-button>
          <span class="ob-grow" />
          <el-button @click="browserOpen = false">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="busy === 'browser'">{{ caps.browser ? '保存' : '开启' }}</el-button>
        </div>
      </form>
    </el-dialog>
    <el-dialog v-model="removing" title="移除节点" width="440px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body>
      <p class="sv-remove-text">移除后「{{ s.name }}」上的节点立即断开，需要重新安装才能再连接。历史监控数据保留。</p>
      <label class="ob-check" :class="{'is-disabled': state !== 'online'}">
        <input v-model="uninstall" type="checkbox" :disabled="state !== 'online'" />
        <span>同时从服务器上卸载节点程序<small class="instance-muted">{{ state === 'online' ? '停止服务并删除程序、配置和 systemd 单元' : '节点不在线，只能在主控这边移除' }}</small></span>
      </label>
      <template #footer>
        <el-button @click="removing = false">取消</el-button>
        <el-button type="danger" :loading="busy === 'revoke'" @click="removeNode">移除</el-button>
      </template>
    </el-dialog>
  </article>
</template>
