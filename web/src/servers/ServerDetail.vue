<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Box, ChromeFilled, CopyDocument, Cpu, Film, Guide, Memo, Monitor, MoreFilled } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok, relativeTime, timeLabel } from '../instances/instanceHelpers.js';
import InstallCommand from './InstallCommand.vue';
import InstallProgress from './InstallProgress.vue';
import PlatformMark from './PlatformMark.vue';
import ServerJobs from './ServerJobs.vue';
import BrowserUpdate from './BrowserUpdate.vue';
import {stopWorkConfirmation, stopWorkWarning, stopWorkFailure} from './stopWork.js';
import MeshCard from './mesh/MeshCard.vue';
import DdnsCard from '../ddns/DdnsCard.vue';
import CardIcon from './detail/CardIcon.vue';
import NetAddresses from './NetAddresses.vue';
import NetProbe from './NetProbe.vue';
import RouteHops from './RouteHops.vue';
import HostDetails from './HostDetails.vue';
import DdnsSheet from '../ddns/DdnsSheet.vue';
import BillingCard from './detail/BillingCard.vue';
import SshCard from './detail/SshCard.vue';
import { DETAIL_TABS, detailTab as tab } from './detail/detailTab.js';
import OverviewCards from './detail/OverviewCards.vue';
import TrendsSection from './detail/TrendsSection.vue';
import TrafficSection from './detail/TrafficSection.vue';
import LatencySection from './detail/LatencySection.vue';
import NetDevices from './detail/NetDevices.vue';
import DiskSection from './detail/DiskSection.vue';
import IpQuality from './detail/IpQuality.vue';
import BgpSection from './detail/BgpSection.vue';
import UnlockSection from './detail/UnlockSection.vue';
import CalibrateDialog from './detail/CalibrateDialog.vue';
import { METRIC_RANGES, rangeName } from './detail/detailHelpers.js';
import { every } from './detail/useVisible.js';
import './detail/detail.css';
import { incompatibleText, nodeBuildLine, nodeUpdateText, updateResultText } from './nodeVersions.js';
import { addressNetworks, asnText, bytes, canDesktop as checkDesktop, flag, googleRegionText, hasDesktop as checkHasDesktop, noAutofill, platformOf,
  regionName, ROUTE_CARRIERS, ROUTE_CITIES, routeView, safeError, terminalReason, unlockServices, uptime } from './serverHelpers.js';

const props = defineProps({item: {type: Object, required: true}, settings: {type: Object, required: true}, build: {type: Object, required: true},
  now: {type: Number, required: true}, active: Boolean, servers: {type: Array, default: () => []}, phone: Boolean});
const emit = defineEmits(['edit', 'ssh', 'settings', 'terminal', 'desktop', 'files', 'batch', 'changed', 'removed', 'mesh', 'open']);
const busy = ref(''), error = ref(''), enrollment = ref(null), showInstall = ref(false);
const range = ref('live'), spanSeries = ref(null), recent = ref(null), log = ref({terminal: [], exec: []}), logTab = ref('exec');
const removing = ref(false), uninstall = ref(true), installBrowser = ref(false);
const browser = ref(null), browserOpen = ref(false), browserForm = reactive({mode: 'launch', endpoint: ''});
let alive = true, generation = 0, installTimer, browserTimer, browserRequest = 0;

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
const showDesktop = computed(() => checkHasDesktop(s.value));
const canDesktop = computed(() => state.value === 'online' && checkDesktop(s.value));
const canExec = computed(() => state.value === 'online' && caps.value.exec !== false);
const canJobs = computed(() => canExec.value && Boolean(s.value.live?.features?.includes('jobs')));
const detailsOpen = ref(false), calibOpen = ref(false);
// The machine at a glance: the system, the hardware as tiles, then the node and how it connects. What the
// node tells of it in detail (详细信息) is read once the 系统 tab is shown while it is online.
const info = computed(() => s.value.node?.info || {});
const host = ref(null);
watch(() => tab.value === 'system' && props.active && state.value === 'online' && !host.value, want => {
  if (want) Api.serverDetails(s.value.id).then(r => { host.value = r.details || null; }, () => {});
}, {immediate: true});
const osSub = computed(() => [info.value.kernel, info.value.arch, info.value.virtualization].filter(Boolean).join(' · '));
const DISK_KINDS = {ssd: 'SSD', hdd: 'HDD', nvme: 'NVMe SSD', virtual: '虚拟磁盘'};
const hardware = computed(() => {
  const {cores, memTotal, swapTotal, diskTotal} = info.value, c = host.value?.cpu || {}, disks = host.value?.disks || [];
  const mhz = Math.max(c.mhz || 0, c.maxMhz || 0), line = parts => parts.filter(Boolean).join(' · ');
  return [cores && ['CPU', `${cores} 核`, line([c.threads && c.threads !== cores && `${c.threads} 线程`, mhz && `${(mhz / 1000).toFixed(2)} GHz`]), Cpu],
    memTotal && ['内存', bytes(memTotal), swapTotal ? `交换 ${bytes(swapTotal)}` : '未启用交换', Film],
    diskTotal && ['硬盘', bytes(diskTotal), line([[...new Set(disks.map(d => DISK_KINDS[d.kind]).filter(Boolean))].join('、'), disks.length > 1 && `${disks.length} 块`]), Box],
  ].filter(Boolean);
});
const nodeLine = computed(() => [s.value.node?.version ? `节点 ${s.value.node.version}` : '', live.value?.nodeRss ? `占用 ${bytes(live.value.nodeRss)} 内存` : ''].filter(Boolean).join(' · '));
const systemFacts = computed(() => {
  const node = s.value.node, h = host.value || {};
  if (!node) return [];
  return [['主机名', h.hostname || info.value.hostname], ['已运行', live.value?.uptime ? `${uptime(live.value.uptime)}${h.boot ? ` · ${timeLabel(h.boot * 1000)} 开机` : ''}` : ''],
    ['时区', h.timezone], ['DNS', (h.dns || []).join('、')], ['透明代理', proxyFact(h.proxy)],
    ['IPv4', info.value.ipv4], ['IPv6', info.value.ipv6], ['接入', access.value], ['当前连接', connection.value],
    ['限制', [caps.value.exec === false ? '已关闭命令执行' : '', caps.value.terminal === false ? '已关闭终端' : ''].filter(Boolean).join(' · ')],
    ['构建', buildFact.value], ['安装于', timeLabel(node.enrolledAtMs)]].filter(([, v]) => v);
});
// A transparent proxy taking over the machine's own traffic, which the node routes the mesh and its measurements around.
const PROXY_KINDS = {tun: 'TUN 接管', tproxy: 'TPROXY 接管', redirect: 'TCP 重定向'};
function proxyFact(p) {
  if (!p || !p.kinds?.length && !p.active) return '';
  const kinds = (p.kinds || []).map(k => PROXY_KINDS[k] || k).join('、');
  if (p.error) return `绕开失败：${p.error}`;
  return p.active ? `已绕开${kinds ? `（${kinds}）` : ''}` : `检测到（${kinds}）`;
}
// Under each address: its ASN, operator and country by GeoIP, and where Google places it.
const ipNets = computed(() => Object.fromEntries(addressNetworks(s.value.node, s.value.profile?.region).map(e => [`IPv${e.family}`,
  {line: [asnText(e), regionName(e.country)].filter(Boolean).join(' · '), google: e.google}])));
const unlockView = computed(() => unlockServices(s.value.node, s.value.profile?.region));
function checkUnlock() {
  return run('unlock', async () => { ok(await Api.checkServerUnlock(s.value.id)); emit('changed'); }, '解锁检测完成');
}
// Neither check is for a server in mainland China.
const checksOn = computed(() => {
  const abroad = s.value.region !== 'CN';
  return {unlock: abroad && props.settings.checks?.unlock !== false, route: abroad && props.settings.checks?.route !== false};
});
const routes = computed(() => routeView(s.value.node));
const routesShown = computed(() => Boolean(s.value.node && checksOn.value.route && (routes.value || state.value === 'online')));
const unlockShown = computed(() => Boolean(!front.value && checksOn.value.unlock && (unlockView.value || state.value === 'online')));
// The cell whose hops are shown: in a popover by it, on a phone in a sheet.
const routeOpen = ref('');
const routeCell = computed(() => routes.value?.carriers.flatMap(c => c.cities).find(c => c.key === routeOpen.value) || null);
function routeVisible(key, visible) {
  if (visible) routeOpen.value = key;
  else if (routeOpen.value === key) routeOpen.value = '';
}
const closeRoute = event => { if (event.key === 'Escape') routeOpen.value = ''; };
watch(routeOpen, (open, was) => {
  if (open && !was) document.addEventListener('keydown', closeRoute);
  else if (!open) document.removeEventListener('keydown', closeRoute);
});
onBeforeUnmount(() => document.removeEventListener('keydown', closeRoute));
function checkRoute() {
  return run('route', async () => { ok(await Api.checkServerRoute(s.value.id)); emit('changed'); }, '三网回程检测完成');
}
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
const canSsh = computed(() => s.value.local || s.value.ssh);
const front = computed(() => Boolean(s.value.profile?.front?.exits?.length));
// Which access point the node uses, and whether it fell back to the backup.
const access = computed(() => {
  if (s.value.local) return '本机直连';
  const {primary, backup} = s.value.access || {};
  if (!primary) return '';
  // A front's are its exits (前置机经出口接入), each ready once that exit serves it.
  const name = point => point.serverId ? `经出口 ${point.name}${point.ready ? '' : '，出口接入口未就绪'}` : point.name;
  const parts = [`${name(primary)}（首选）`, backup ? `${name(backup)}（备用）` : ''].filter(Boolean).join(' · ');
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
  if (error === 'browser_unsupported_libc') return '这台机器的系统库是 musl（OpenWrt、Alpine 等），Chrome 不能在上面运行，节点没有下载。可以在“设置”里连接其他机器上已有的浏览器。';
  const exe = /^browser_not_runnable: (.+)$/.exec(error || '')?.[1];
  return exe ? `节点没法运行这台机器上的 ${exe}：Linux 上浏览器由专用账户 openbear-browser 运行，它进不去浏览器所在的目录。`
    + '可以在“设置”里改用下载的测试版 Chrome 或连接已有的浏览器，也可以放开该目录的访问权限后重启浏览器。' : error;
}
const SOURCES = {ai: 'AI', batch: '批量', web: '页面', files: '文件', instance: '实例', gateway: '网关'};
// Runs that went over SSH while the node was offline are logged as `<source>-ssh`.
const sourceLabel = source => source?.endsWith('-ssh') ? `${SOURCES[source.slice(0, -4)] || source.slice(0, -4)} · SSH` : SOURCES[source] || source;
const OUTCOMES = {ok: '成功', failed: '失败', timeout: '超时', refused: '被拒绝', unknown: '结果未知', started: '已启动'};
const openConversation = conversationUuid => window.dispatchEvent(new CustomEvent('openbear:open-conversation', {detail: {conversationUuid}}));
const duration = entry => {
  if (!entry.endedAtMs) return '进行中';
  const minutes = Math.round((entry.endedAtMs - entry.startedAtMs) / 60000);
  return minutes < 1 ? '不到 1 分钟' : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
};

// The last ten minutes (every 3 s) behind the summary cards and the 实时 range: after the first read only
// the samples since the last one held are fetched, and those older than ten minutes dropped.
const LIVE_KEYS = ['cpu', 'steal', 'iowait', 'mem', 'swap', 'disk', 'load', 'rxRate', 'txRate', 'ioRead', 'ioWrite', 'tcp', 'udp', 'proc'];
let liveFor = '', liveBusy = false;
async function loadRecent() {
  if (!s.value.node || liveBusy) return;
  const id = s.value.id, had = liveFor === id ? recent.value : null, after = had?.points.t.at(-1) || 0;
  liveBusy = true;
  try {
    const data = ok(await Api.serverMetrics(id, 'live', after));
    if (!alive || s.value.id !== id) return;
    if (!had) { recent.value = data; liveFor = id; return; }
    if (!data.points.t.length) return;
    const start = data.points.t.at(-1) - 600, keep = had.points.t.findIndex(t => t > start);
    const from = keep < 0 ? had.points.t.length : keep, points = {};
    for (const key of ['t', ...LIVE_KEYS]) points[key] = [...(had.points[key] || []).slice(from), ...(data.points[key] || [])];
    recent.value = {...data, points};
  } catch (e) { if (alive) error.value = safeError(e); }
  finally { liveBusy = false; }
}
// Any other range: a few hundred points the server buckets.
async function loadSeries() {
  const span = range.value, mine = ++generation;
  if (!s.value.node || span === 'live') return;
  try {
    const data = ok(await Api.serverMetrics(s.value.id, span));
    if (alive && mine === generation) spanSeries.value = data;
  } catch (e) { if (alive && mine === generation) error.value = safeError(e); }
}
const series = computed(() => range.value === 'live' ? recent.value : spanSeries.value?.range === range.value ? spanSeries.value : null);
const rangeLabel = computed(() => rangeName(METRIC_RANGES, range.value));
async function loadLog() {
  try {
    const item = ok(await Api.server(s.value.id)).item;
    log.value = {terminal: item.terminalLog || [], exec: item.execLog || []};
  } catch { /* optional */ }
}
watch(range, () => { void loadSeries(); });
watch(() => s.value.node?.enrolledAtMs, () => { liveFor = ''; void loadRecent(); void loadSeries(); });
watch(() => props.active, value => { if (value) { void loadRecent(); void loadSeries(); void loadLog(); } });

async function run(name, fn, success) {
  if (busy.value) return;
  busy.value = name; error.value = '';
  try { const value = await fn(); if (success) ElMessage.success(typeof success === 'function' ? success(value) : success); return value; }
  catch (e) { error.value = stopWorkFailure(e) || safeError(e); }
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
  const request = ++browserRequest;
  clearTimeout(browserTimer);
  if (!s.value.node || !alive) { browser.value = null; return; }
  const id = s.value.id;
  try {
    const result = ok(await Api.serverBrowser(id)).browser;
    if (!alive || s.value.id !== id || request !== browserRequest) return;
    browser.value = result;
  } catch { if (!alive || s.value.id !== id || request !== browserRequest) return; browser.value = null; }
  // A download or start takes a while; follow it until it settles.
  if (alive && props.active && (['installing', 'starting'].includes(browser.value?.state) || ['downloading', 'restarting'].includes(browser.value?.update?.state))) browserTimer = setTimeout(loadBrowser, 3000);
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
  const remote = uninstall.value;
  const result = await run('revoke', async () => ok(await Api.revokeServerNode(s.value.id, remote)));
  if (!result) return;
  removing.value = false;
  const warning = stopWorkWarning(result.work);
  if (warning) ElMessage.warning({message: warning, duration: 10000});
  ElMessage.success(result.note || (result.uninstalled ? '节点已移除，并已从服务器上卸载' : remote ? '节点已移除，但没能在服务器上卸载' : '节点已移除'));
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
  else if (action === 'desktop') emit('desktop', item);
  else if (action === 'edit') emit('edit', item);
  else if (action === 'batch') emit('batch', item);
  else if (action === 'reinstall') { showInstall.value = true; enrollment.value = null; }
  else if (action === 'update') void updateNode();
  else if (action === 'downgrade' && await confirm(`这是降级：「${item.name}」的节点会从 ${item.node.version} 换成更旧的 ${item.node.update.target.version}，节点会重启，打开的终端会断开。`, '降级节点', '降级')) void updateNode(true);
  else if (action === 'revoke') { uninstall.value = state.value === 'online'; removing.value = true; }
  else if (action === 'unssh' && await confirm(`删除「${item.name}」的 SSH 连接信息？节点和监控数据保留。`, '移除 SSH 连接', '移除')) {
    await run('unssh', async () => { ok(await Api.updateInstanceServer(item.id, {name: item.name, auth: 'none'})); emit('changed'); }, '已移除');
  } else if (action === 'delete' && await confirm(`删除「${item.name}」的登记信息、节点和全部监控数据？${stopWorkConfirmation(Boolean(item.live))}`, '删除服务器', '删除')) {
    await run('delete', async () => {
      const result = ok(await Api.deleteServer(item.id));
      const warning = stopWorkWarning(result.work);
      if (warning) ElMessage.warning({message: warning, duration: 10000});
      emit('removed', item.id);
    }, '已删除');
  }
}
// The tabs: the bar sticks to the top once the page is scrolled past it; another tab, picked down there,
// starts at its own top. Refreshed only while shown: the live samples and the hour range each minute while
// a tab of monitoring is (概览, 网络 and 系统 all draw from them), the command log while 工具 is.
const mark = ref(null), stuck = ref(false);
const scroller = () => mark.value?.closest('.sv-scroll');
const past = () => { const area = scroller(); return area && mark.value ? mark.value.getBoundingClientRect().top - area.getBoundingClientRect().top : 0; };
const onScroll = () => { stuck.value = past() < 0; };
function pickTab(key) {
  const above = past();
  tab.value = key;
  if (above < 0) scroller().scrollTop += above;
}
const watching = computed(() => props.active && tab.value !== 'tools');
// What polls without looking whether it is on screen: only while its panel is the one shown.
const shown = key => props.active && tab.value === key;
const stops = [
  every(3000, () => watching.value && state.value === 'online', loadRecent),
  every(60000, () => watching.value && range.value === '1h', loadSeries),
  every(30000, () => props.active && tab.value === 'tools', loadLog),
];
let scrolling = null;
watch(mark, el => {
  scrolling?.removeEventListener('scroll', onScroll);
  scrolling = el?.closest('.sv-scroll') || null;
  scrolling?.addEventListener('scroll', onScroll, {passive: true});
});
onBeforeUnmount(() => scrolling?.removeEventListener('scroll', onScroll));
// A tab restored behind the one in front reads when it comes to the front (the watches above).
onMounted(() => { if (props.active) { void loadRecent(); void loadLog(); void loadBrowser(); } });
onBeforeUnmount(() => { alive = false; stops.forEach(stop => stop()); clearTimeout(installTimer); clearTimeout(browserTimer); });
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
          <el-button v-if="canDesktop" type="primary" @click="emit('desktop', s)">
            <svg class="sv-act-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="2" /><path d="M8 21h8M12 17v4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" /></svg>查看桌面
          </el-button>
          <el-button v-if="canTerminal" :type="canDesktop ? 'default' : 'primary'" @click="emit('terminal', s)">
            <svg class="sv-act-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2" /><path d="m7 9 3 3-3 3m5 0h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>终端
          </el-button>
          <el-button v-if="canExec" @click="emit('files', s)">
            <svg class="sv-act-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" /></svg>文件
          </el-button>
          <el-button v-if="canExec" class="sv-hide-narrow" @click="emit('batch', s)">
            <svg class="sv-act-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" /></svg>执行命令
          </el-button>
          <el-button class="sv-hide-narrow" @click="emit('edit', s)">
            <svg class="sv-act-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>编辑
          </el-button>
          <el-dropdown trigger="click" :disabled="Boolean(busy)" @command="more">
            <el-button class="ix-more" aria-label="更多操作"><el-icon><MoreFilled /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="canDesktop" command="desktop" class="sv-only-narrow">查看桌面</el-dropdown-item>
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
      <p v-else-if="front" class="instance-muted" data-front-install>前置机经出口安装：OpenBear 经出口登录它，把节点程序推送过去，不提供手动安装命令。首选出口要在线。</p>
      <p v-else-if="!settings.accessPoints?.length && !s.local" class="ob-notice ob-row">还没有接入点，服务器上的节点不知道该连哪里。<button type="button" class="ix-text-button" @click="emit('settings')">去设置</button></p>
      <label v-if="!s.local && !front" class="ob-check sv-install-browser"><input v-model="installBrowser" type="checkbox" /><span>同时开启浏览器<small class="instance-muted">主控可以用这台机器的浏览器和网络访问网页；没装 Chrome 时会自动下载</small></span></label>
      <div class="instance-actions">
        <el-button v-if="canSsh" type="primary" :loading="installing" :disabled="Boolean(busy) || installing || !build.available || (!settings.accessPoints?.length && !s.local && !front)" @click="installViaSsh">{{ s.local ? '在本机安装' : front ? '经出口安装' : '通过 SSH 安装' }}</el-button>
        <el-button v-if="!front" :disabled="Boolean(busy) || !build.available || (!settings.accessPoints?.length && !s.local)" @click="generate">{{ enrollment ? '重新生成安装命令' : '生成安装命令' }}</el-button>
        <el-button v-if="!canSsh" :disabled="Boolean(busy)" @click="emit('ssh', s)">登记 SSH 后一键安装</el-button>
      </div>
      <InstallCommand v-if="command" :command="command" :windows-command="windowsCommand" :platform="s.node?.info?.platform || s.ssh?.platform || ''" note />
    </section>

    <template v-if="s.node">
      <div ref="mark" class="dt-tabs-mark" aria-hidden="true" />
      <nav class="dt-tabs" :class="{'is-stuck': stuck}" aria-label="详情分类">
        <div class="sv-ranges" role="tablist">
          <button v-for="[key, label] in DETAIL_TABS" :key="key" type="button" role="tab" :aria-selected="tab === key" :data-tab="key" @click="pickTab(key)">{{ label }}</button>
        </div>
      </nav>
      <div v-show="tab === 'overview'" class="sv-dash" role="tabpanel" aria-label="概览" data-panel="overview">
        <section class="dt-overview" data-part="overview" aria-label="概况">
          <OverviewCards v-if="live" :server="s" :live="live" :recent="recent?.points" :has="has" :platform="platform" :phone="phone" :busy="Boolean(busy)" @calibrate="calibOpen = true" />
          <p v-else class="ix-card instance-muted dt-offline">{{ s.node.lastSeenAtMs ? `离线 · 最后在线 ${relativeTime(s.node.lastSeenAtMs, now)}，下面是之前记录的数据。` : '节点尚未连接，连上后显示实时数据。' }}</p>
        </section>
        <TrendsSection v-model:range="range" :series="series" :has="has" :info="info" :phone="phone" />
        <TrafficSection :server="s" :active="active" :now="now" :timezone="settings.timezone" :phone="phone" :busy="Boolean(busy)" @changed="emit('changed')" @calibrate="calibOpen = true"
          @settings="emit('edit', s, 'traffic')" />
        <DiskSection :server="s" :series="series" :range-label="rangeLabel" :has="has" :active="active" :phone="phone" />
      </div>
      <div v-show="tab === 'net'" class="sv-dash" role="tabpanel" aria-label="网络" data-panel="net">
        <LatencySection :server="s" :settings="settings" :servers="servers" :active="active" :phone="phone" @settings="emit('settings')" @changed="emit('changed')" />
        <div class="ix-grid sv-cards dt-grid">
          <section v-if="routesShown" class="ix-card sv-routes" data-route-detail>
            <header class="ix-card-head"><h3><CardIcon :icon="Guide" tone="violet" />三网回程</h3>
              <span class="instance-muted">{{ routes ? `检测于 ${relativeTime(routes.at, now)} · IPv4` : '还没有检测结果' }}</span><span class="ob-grow" />
              <button v-if="state === 'online'" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="checkRoute">{{ busy === 'route' ? '检测中…' : routes ? '重新检测' : '检测' }}</button>
            </header>
            <p v-if="!routes" class="instance-muted sv-routes-empty">{{ busy === 'route' ? '正在追踪 9 条路由，大约需要一分钟。' : '追踪回北京、上海、广东电信、联通、移动的路由，判断走的是哪种线路。' }}</p>
            <table v-else class="sv-route-grid">
              <thead><tr><th scope="col" aria-label="城市"></th><th v-for="[id, name] in ROUTE_CARRIERS" :key="id" scope="col">{{ name }}</th></tr></thead>
              <tbody>
                <tr v-for="([city, cityName], row) in ROUTE_CITIES" :key="city">
                  <th scope="row">{{ cityName }}</th>
                  <td v-for="c in routes.carriers" :key="c.id">
                    <el-popover :visible="!phone && routeOpen === c.cities[row].key" :disabled="phone" trigger="click" placement="bottom" :width="500"
                      popper-class="instance-ui servers-ui sv-route-popper" @update:visible="v => routeVisible(c.cities[row].key, v)">
                      <template #reference>
                        <button type="button" class="sv-route-cell" :aria-pressed="routeOpen === c.cities[row].key" :data-route-cell="c.cities[row].key"
                          :title="`${c.cities[row].city}${c.cities[row].carrier}：点开看路由`" @click="phone && (routeOpen = c.cities[row].key)">
                          <span class="pb-tag sv-route-tag" :class="{'is-plain': !c.cities[row].line}" :data-tier="c.cities[row].tier || null">{{ c.cities[row].line || (c.cities[row].error ? '失败' : '未知') }}</span>
                          <small>{{ c.cities[row].rttMs != null ? `${Math.round(c.cities[row].rttMs)} ms` : '—' }}</small>
                        </button>
                      </template>
                      <RouteHops v-if="!phone && routeOpen === c.cities[row].key" :cell="c.cities[row]" />
                    </el-popover>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>
          <NetAddresses :server-id="s.id" :online="state === 'online'" :active="shown('net')" card />
          <MeshCard :server-id="s.id" :active="shown('net')" @open="emit('mesh', $event)" />
          <DdnsCard :server-id="s.id" :active="shown('net')" />
        </div>
        <BgpSection :server="s" :active="active" :phone="phone" :now="now" />
        <IpQuality v-if="!front" :server="s" :online="state === 'online'" :active="shown('net')" :now="now" :unlock-shown="unlockShown" :busy="Boolean(busy)"
          :auto="settings.checks?.ipcheck !== false && platform === 'linux' && caps.exec !== false && s.region !== 'CN'" />
        <UnlockSection v-if="unlockShown" :server="s" :unlock="unlockView" :active="shown('net')" :online="state === 'online'" :busy="Boolean(busy)"
          :unlock-busy="busy === 'unlock'" :phone="phone" :now="now" @unlock="checkUnlock" />
        <NetDevices :server="s" :live="live" :series="series" :range-label="rangeLabel" :online="state === 'online'" :active="active" :phone="phone" :platform="platform" />
      </div>
      <div v-show="tab === 'system'" class="sv-dash" role="tabpanel" aria-label="系统" data-panel="system">
        <div class="dt-system">
          <section class="ix-card sv-system" data-part="system">
            <header class="ix-card-head"><h3><CardIcon :icon="Monitor" />系统</h3><span class="instance-muted">{{ nodeLine }}</span><span class="ob-grow" />
              <button v-if="state === 'online'" type="button" class="ix-text-button" @click="detailsOpen = true">详细信息</button>
            </header>
            <div class="sv-os"><strong :title="info.os">{{ info.os || '系统未知' }}</strong><span v-if="osSub" class="instance-muted" :title="osSub">{{ osSub }}</span>
              <span v-if="info.cpu" class="sv-cpu-model">{{ info.cpu }}</span></div>
            <div v-if="hardware.length" class="sv-hw">
              <div v-for="[label, main, sub, icon] in hardware" :key="label" class="sv-hw-item"><span><el-icon><component :is="icon" /></el-icon>{{ label }}</span>
                <strong>{{ main }}</strong><small v-if="sub" :title="sub">{{ sub }}</small></div>
            </div>
            <dl v-if="systemFacts.length" class="ix-facts sv-system-facts">
              <div v-for="[label, value] in systemFacts" :key="label"><dt>{{ label }}</dt>
                <dd v-if="ipNets[label]" class="sv-ip" :data-family="label"><span class="mono ix-ellipsis" :title="value">{{ value }}</span>
                  <small v-if="ipNets[label].line || ipNets[label].google" class="sv-ip-net">
                    <span v-if="ipNets[label].line">{{ ipNets[label].line }}</span>
                    <span v-if="ipNets[label].google" :data-tone="ipNets[label].google === 'CN' ? 'bad' : null">Google 判定 {{ googleRegionText(ipNets[label].google) }}</span>
                  </small></dd>
                <dd v-else class="ix-ellipsis" :title="value">{{ value }}</dd></div>
            </dl>
          </section>
          <div class="dt-system-side">
            <BillingCard :server="s" :now="now" :busy="Boolean(busy)" @edit="emit('edit', s)" @renew="renew" />
            <SshCard v-if="s.ssh || showDesktop" :server="s" :busy="Boolean(busy)" @check="checkSsh" @edit="emit('ssh', s)" @desktop="emit('desktop', s)" />
          </div>
        </div>
      </div>
      <div v-show="tab === 'tools'" class="sv-dash" role="tabpanel" aria-label="工具" data-panel="tools">
        <NetProbe :server-id="s.id" :servers="servers" :online="state === 'online'" :active="active" card />
        <div class="ix-grid sv-cards dt-grid">
          <section class="ix-card" data-part="browser">
            <header class="ix-card-head"><h3><CardIcon :icon="ChromeFilled" />浏览器</h3><span class="ob-grow" />
              <button v-if="caps.browser && state === 'online'" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="restartBrowser">重启</button>
              <button v-if="canExec && (caps.browser || !s.local)" type="button" class="ix-text-button" :disabled="Boolean(busy)" @click="openBrowser">{{ caps.browser ? '设置' : '开启' }}</button>
            </header>
            <p v-if="s.local && !caps.browser" class="instance-muted">本机不需要开启：浏览器工具不指定服务器时，用的就是设置里配置的默认浏览器（CDP 地址）。</p>
            <dl v-else-if="caps.browser" class="ix-facts">
              <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="browserLine[0]">{{ browserLine[1] }}</span></dd></div>
              <div v-for="[label, value] in browserFacts" :key="label"><dt>{{ label }}</dt><dd :class="label === '原因' ? 'sv-browser-error' : 'ix-ellipsis'" :title="value">{{ value }}</dd></div>
            </dl>
            <p v-else class="instance-muted">未开启。开启后，主控可以用这台机器的浏览器、网络和 IP 访问网页，并使用在那个浏览器里登录过的账号。{{ caps.exec === false ? '这台节点关闭了命令执行，要在服务器上重新运行带 --browser 的安装命令来开启。' : '' }}</p>
            <BrowserUpdate v-if="caps.browser && (browser?.chromeVersion || browser?.download || browser?.source === 'download')" :key="s.id"
              :server-id="s.id" :browser="browser" :online="state === 'online'" :supported="Boolean(s.live?.features?.includes('browser.update'))" @changed="loadBrowser" />
          </section>
          <ServerJobs v-if="canJobs" :server-id="s.id" :active="shown('tools')" :now="now" />
          <section v-if="log.exec.length || log.terminal.length" class="ix-card sv-logs" aria-label="命令记录" data-part="logs">
            <header class="ix-card-head">
              <CardIcon :icon="Memo" tone="violet" />
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
                <span class="instance-muted sv-log-meta">{{ OUTCOMES[entry.outcome] || '' }}{{ entry.exitCode != null && entry.outcome === 'failed' ? ` ${entry.exitCode}` : '' }}<button
                  v-if="entry.conversationUuid" type="button" class="instance-link sv-log-origin" :title="`来自会话：${entry.conversationTitle || entry.conversationUuid}`"
                  @click="openConversation(entry.conversationUuid)">{{ entry.conversationTitle || '来源会话' }}</button></span>
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
      </div>
    </template>
    <div v-else class="ix-grid sv-cards dt-grid">
      <BillingCard :server="s" :now="now" :busy="Boolean(busy)" @edit="emit('edit', s)" @renew="renew" />
      <SshCard v-if="s.ssh || showDesktop" :server="s" :busy="Boolean(busy)" @check="checkSsh" @edit="emit('ssh', s)" @desktop="emit('desktop', s)" />
      <MeshCard :server-id="s.id" :active="active" @open="emit('mesh', $event)" />
    </div>

    <el-dialog v-model="browserOpen" :title="caps.browser ? '浏览器设置' : '开启浏览器'" width="460px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body>
      <form class="instance-form" autocomplete="off" @submit.prevent="saveBrowser(true)">
        <div class="ob-radios sv-browser-modes" role="radiogroup" aria-label="浏览器来源">
          <label><input v-model="browserForm.mode" type="radio" value="launch" />由节点启动浏览器</label>
          <label><input v-model="browserForm.mode" type="radio" value="download" />使用下载的测试版 Chrome</label>
          <label><input v-model="browserForm.mode" type="radio" value="endpoint" />连接已有的浏览器</label>
        </div>
        <small v-if="browserForm.mode === 'launch'" class="instance-muted">优先用这台机器上的 Chrome、Edge 或 Chromium，都没有时自动下载 Google 官方的测试版 Chrome。Windows 和 macOS 上窗口显示在当前登录用户的桌面上，服务器上用无头模式。登录状态保存在节点专用的资料目录里。</small>
        <small v-else-if="browserForm.mode === 'download'" class="instance-muted">不用这台机器上已装的浏览器，由节点下载 Google 官方的测试版 Chrome，放在节点目录里。下载后固定使用该版本，可手动检查更新。适合已装的浏览器节点运行不了的情况。</small>
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
      <p v-if="uninstall" class="sv-remove-text">{{ stopWorkConfirmation(state === 'online') }}</p>
      <label class="ob-check">
        <input v-model="uninstall" type="checkbox" />
        <span>同时从服务器上卸载节点程序<small class="instance-muted">{{ state === 'online' ? '停止服务并删除程序、配置和 systemd 单元' : '节点不在线，只能在主控这边移除' }}</small></span>
      </label>
      <template #footer>
        <el-button @click="removing = false">取消</el-button>
        <el-button type="danger" :loading="busy === 'revoke'" @click="removeNode">移除</el-button>
      </template>
    </el-dialog>
    <CalibrateDialog v-if="s.traffic" v-model="calibOpen" :server="s" :phone="phone" @changed="emit('changed')" />
    <DdnsSheet :model-value="phone && Boolean(routeCell)" phone :title="routeCell ? `${routeCell.city}${routeCell.carrier}回程` : ''" @update:model-value="v => { if (!v) routeOpen = ''; }">
      <RouteHops v-if="routeCell" :cell="routeCell" :named="false" />
    </DdnsSheet>
    <DdnsSheet v-model="detailsOpen" :phone="phone" kind="drawer" title="详细信息" :sub="s.name">
      <HostDetails v-if="detailsOpen" :server="s" />
    </DdnsSheet>
  </article>
</template>
