<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Close } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok, relativeTime } from '../instances/instanceHelpers.js';
import { bytes, errorText, MESH_ACCESS_ID, meshAccessReason, noAutofill, safeError } from './serverHelpers.js';
import LatencyLibrary from './LatencyLibrary.vue';

const props = defineProps({settings: {type: Object, required: true}, items: {type: Array, default: () => []}, build: {type: Object, default: () => ({})}});
const emit = defineEmits(['saved', 'cancel', 'guide', 'checked']);
const rules = props.settings.reminders || {};
const form = reactive({points: (props.settings.accessPoints || []).map(p => ({...p})), defaultAccess: props.settings.defaultAccess || '', timezone: props.settings.timezone || 'Asia/Shanghai',
  enabled: rules.enabled !== false, time: rules.time || '10:00', expiryDays: [...(rules.expiryDays || [7, 3, 1, 0])],
  trafficPercents: [...(rules.trafficPercents || [80, 90, 100])], autoRenew: rules.autoRenew !== false,
  unlock: props.settings.checks?.unlock !== false, route: props.settings.checks?.route !== false, ipcheck: props.settings.checks?.ipcheck !== false,
  ipcheckDays: props.settings.ipcheckDays || 7,
  minuteDays: props.settings.retention?.minuteDays ?? 31, hourDays: props.settings.retention?.hourDays ?? 400,
  latency: latencyForm(props.settings.latency), httpbl: '', httpblDrop: false});
// IP 情报's credentials: the http:BL key is kept in the vault (shown only as kept), Radar reads with a 动态域名 account.
const intel = computed(() => props.settings.ipIntel || {httpbl: false, cloudflare: ''});
const httpblKey = () => form.httpbl.trim() ? {ipIntel: {httpblKey: form.httpbl.trim()}} : form.httpblDrop ? {ipIntel: {httpblKey: ''}} : {};
function latencyForm(value = {}) {
  const defaults = value.defaults || {};
  return {enabled: true, province: '云南', intervalS: 10, method: 'tcp', card: 'auto', ...value, custom: (value.custom || []).map(c => ({...c})),
    defaults: {domestic: [...(defaults.domestic || [])], optimized: [...(defaults.optimized || [])], plain: [...(defaults.plain || [])]}};
}
const drafts = reactive({expiryDays: '', trafficPercents: ''});
const TABS = [['access', '接入'], ['reminders', '提醒'], ['checks', '检测'], ['data', '数据'], ['latency', '延迟'], ['library', '监测地址']];
// What a card's latency is worked out over; app/servers/latency.py card_targets.
const CARDS = [['auto', '自动', '测三网的服务器显示三网的延迟，不测三网的（海外普通线路）显示其他地址的。'],
  ['three', '三网', '只算观测地电信、联通、移动。不测三网的服务器改算全部。'],
  ['other', '其他地址', '只算三网以外的地址，比如国外的 DNS 和 AI 平台。只测三网的服务器改算全部。'],
  ['all', '全部', '所有地址一起算。远近不同的地址混在一起，延迟数字只能大致参考。']];
const tab = ref('access');

// 数据: how long each level of the metrics file is kept, what it takes now and would take then.
const MINUTE_DAYS = [7, 14, 31, 60, 90, 180, 365, 0], HOUR_DAYS = [31, 90, 180, 400, 730, 0];
const daysText = days => days === 0 ? '永久' : days % 365 === 0 ? `${days / 365} 年` : `${days} 天`;
const hourChoices = computed(() => HOUR_DAYS.filter(d => form.minuteDays === 0 ? d === 0 : d === 0 || d >= form.minuteDays));
function minuteChanged() { if (!hourChoices.value.includes(form.hourDays)) form.hourDays = form.minuteDays === 0 ? 0 : hourChoices.value.find(d => d) || 0; }
const storage = ref(null), storageError = ref(''), compacting = ref(false);
// Minute rows (metrics, latency), hour rows (metrics, latency, partitions) and the days of traffic.
const LEVELS = [['minute', '分钟级', ['minute', 'ping_minute'], 'minuteDays'], ['hour', '小时级', ['hour', 'ping_hour', 'disk'], 'hourDays'], ['day', '每日流量', ['day'], null]];
const levels = computed(() => {
  const tables = storage.value?.tables || {};
  const out = LEVELS.map(([key, label, names, setting]) => {
    const rows = names.map(n => tables[n] || {bytes: 0, days: 0});
    // Each table at its own pace: latency may have started long after the metrics.
    const used = rows.reduce((n, r) => n + r.bytes, 0), timed = rows.filter(r => r.bytes && r.days >= 1);
    const perDay = timed.length ? timed.reduce((n, r) => n + r.bytes / r.days, 0) : null, keep = setting ? form[setting] : 0;
    const estimate = perDay === null ? '数据不到一天，暂时估算不了' : keep ? `存满约 ${bytes(perDay * keep)}` : `每月约增加 ${bytes(perDay * 30)}`;
    return {key, label, used, estimate, keep: setting ? daysText(keep) : '永久'};
  });
  const total = out.reduce((n, l) => n + l.used, 0);
  return out.map(l => ({...l, share: total ? l.used / total * 100 : 0}));
});
async function loadStorage() {
  storageError.value = '';
  try { storage.value = ok(await Api.serverStorage()); } catch (e) { storageError.value = safeError(e); }
}
async function compact() {
  compacting.value = true;
  try {
    const before = storage.value?.bytes || 0;
    storage.value = ok(await Api.compactServerStorage());
    ElMessage.success(before > storage.value.bytes ? `已清理，释放 ${bytes(before - storage.value.bytes)}` : '已清理，没有可释放的空间');
  } catch (e) { storageError.value = safeError(e); }
  finally { compacting.value = false; }
}

// 延迟: where the three carriers are measured to, how often, how, and what a card shows; 监测地址 what is measured.
const catalog = computed(() => props.settings.latencyCatalog || {provinces: [], extras: []});
function pick(value) {
  tab.value = value;
  if (value === 'data' && !storage.value) void loadStorage();
}
const busy = ref(false), error = ref(''), recent = ref([]), testing = ref(false), moved = ref(null), pushing = ref(false), checking = ref(false);
// The newest few; the rest one click away.
const RECENT_SHOWN = 5;
const allRecent = ref(false);
const shownRecent = computed(() => allRecent.value ? recent.value : recent.value.slice(0, RECENT_SHOWN));
const release = ref(props.build);
const releaseLine = computed(() => {
  const r = release.value || {};
  if (!r.available) return '暂时没有可用的节点版本';
  return r.source === 'local' ? `${r.version}（本地构建）` : `${r.version}（GitHub${r.prerelease ? '，含测试版' : ''}）`;
});
const releaseNote = computed(() => {
  const r = release.value || {};
  const parts = [];
  if (r.checkedAtMs) parts.push(`${relativeTime(r.checkedAtMs, Date.now())}检查过`);
  const http = /^github_http_(\d+)$/.exec(r.error || '')?.[1];
  if (r.error) parts.push(r.error === 'no_compatible_node_release' ? 'GitHub 上暂时没有适合这个主控的节点版本，稍后重试' : r.error === 'github_unreachable' ? '连不上 GitHub'
    : http === '403' || http === '429' ? 'GitHub 限制了访问频率，请稍后再检查' : http ? `GitHub 返回了错误（HTTP ${http}）` : `检查失败（${r.error}）`);
  if (r.incompatible) parts.push(`节点 ${r.incompatible} 需要更新的 OpenBear`);
  if (r.lacking) parts.push(`节点 ${r.lacking} 缺少这个主控需要的功能，已跳过`);
  if (r.localProblem === 'older') parts.push(`本地构建 ${r.localVersion} 比 GitHub 上的版本旧，改用 GitHub 上的版本`);
  else if (r.localProblem) parts.push(`本地构建${r.localProblem === 'protocol' ? '与主控协议不兼容' : '缺少主控需要的功能'}，改用 GitHub 上的版本`);
  if (r.source === 'local' && r.latest && r.latest !== r.version) parts.push(`GitHub 上最新是 ${r.latest}，本地构建优先`);
  return parts.join('；') || '每天自动检查一次 GitHub 上的新版本';
});
// 接收测试版 and 自动更新节点 live in 系统设置 → 版本更新; this only says how they stand.
const switchesNote = computed(() => {
  const r = release.value || {};
  if (!('prerelease' in r)) return '';
  return `${r.prerelease ? '接收测试版' : '只用正式版'} · 自动更新节点${r.autoUpdate ? '已开启' : '已关闭'}，在「系统设置 → 版本更新」中修改`;
});
async function checkRelease() {
  checking.value = true;
  try {
    release.value = ok(await Api.checkNodeRelease()).release;
    emit('checked');
  } catch (e) { error.value = safeError(e); }
  finally { checking.value = false; }
}
const probes = reactive({});
defineExpose({busy});
const remoteOnline = computed(() => props.items.filter(i => i.live && !i.local).length);
const KINDS = {expiry: '到期', expired: '已过期', renewed: '自动续费', traffic: '流量', test: '测试', mesh: '组网', joined: '新服务器接入', node_update: '节点更新'};
const kindText = kind => KINDS[kind] || (kind.startsWith('ddns') ? '动态域名' : kind);
const STATES = {pending: '待发送', enqueued: '已发送', suppressed: '未发送（没有配置 Telegram）', failed: '发送失败', off: '未发送（提醒已关闭）'};

function add(key, low, high) {
  const value = Number(drafts[key]);
  drafts[key] = '';
  if (!Number.isInteger(value) || value < low || value > high || form[key].includes(value) || form[key].length >= 10) return;
  form[key] = [...form[key], value].sort((a, b) => key === 'expiryDays' ? b - a : a - b);
}
function remove(key, value) { form[key] = form[key].filter(v => v !== value); }
const saved = new Set((props.settings.accessPoints || []).map(p => p.id));
function addPoint() {
  const id = 'ap-' + Math.random().toString(16).slice(2, 8);
  form.points.push({id, name: form.points.length ? '' : '默认', url: ''});
  if (!form.defaultAccess) form.defaultAccess = id;
}
function removePoint(point) {
  form.points = form.points.filter(p => p !== point);
  if (form.defaultAccess === point.id) form.defaultAccess = form.points[0]?.id || '';
}
const idOf = point => point.id;
// The built-in mesh access point: shown read-only, never edited, removed, made the default or sent back on save.
const mesh = computed(() => props.settings.meshAccess || null);
const meshReason = computed(() => meshAccessReason(mesh.value));
const meshUsers = computed(() => props.items.filter(i => !i.local && (i.access?.primary?.id === MESH_ACCESS_ID || i.access?.backup?.id === MESH_ACCESS_ID)).length);
const inUse = point => saved.has(point.id) ? props.items.filter(i => !i.local && (i.access?.primary?.id === point.id || i.access?.backup?.id === point.id)).length : 0;
async function probe(point) {
  const url = point.url.trim().replace(/\/+$/, '');
  if (!url) return;
  probes[idOf(point)] = {busy: true};
  try {
    const r = await Api.testAccessPoint(url);
    probes[idOf(point)] = r?.ok ? {ok: true, text: `✓ 可用 · ${r.latencyMs} ms`} : {text: `✗ ${errorText(r?.error)}`};
  } catch (e) { probes[idOf(point)] = {text: `✗ ${safeError(e)}`}; }
}
// Nodes keep their old address until told otherwise; ask only when one of theirs changed.
function addressesChanged(next) {
  const before = Object.fromEntries((props.settings.accessPoints || []).map(p => [p.id, p.url]));
  const after = Object.fromEntries(next.accessPoints.map(p => [p.id, p.url]));
  return Object.keys(before).some(id => before[id] !== after[id]) || next.defaultAccess !== props.settings.defaultAccess;
}
async function save() {
  if (busy.value) return;
  error.value = ''; busy.value = true;
  const points = form.points.map(p => ({id: p.id, name: p.name.trim(), url: p.url.trim().replace(/\/+$/, '')}));
  try {
    const interval = Number(form.latency.intervalS);
    if (!Number.isInteger(interval) || interval < 3 || interval > 600) { tab.value = 'latency'; error.value = '探测间隔应为 3–600 秒的整数。'; return; }
    const result = ok(await Api.saveServerSettings({accessPoints: points, defaultAccess: form.defaultAccess, timezone: form.timezone.trim(), reminders: {
      enabled: form.enabled, time: form.time, expiryDays: form.expiryDays, trafficPercents: form.trafficPercents, autoRenew: form.autoRenew},
      checks: {unlock: form.unlock, route: form.route, ipcheck: form.ipcheck}, ipcheckDays: form.ipcheckDays, retention: {minuteDays: form.minuteDays, hourDays: form.hourDays},
      latency: {...form.latency, intervalS: interval}, ...httpblKey()})).settings;
    if (remoteOnline.value && addressesChanged(result)) moved.value = result;
    else emit('saved', result);
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
async function push() {
  pushing.value = true;
  try {
    const items = ok(await Api.pushServerNodeUrl()).items;
    const failed = items.filter(i => !i.ok).length;
    if (failed) ElMessage.warning(`${items.length - failed} 台已改用新地址，${failed} 台连不上新地址，暂时保持原地址，下次连上主控时再切换`);
    else ElMessage.success(`${items.length} 台节点已改用新地址`);
    emit('saved', moved.value);
  } catch (e) { error.value = safeError(e); }
  finally { pushing.value = false; }
}
async function test() {
  testing.value = true;
  try {
    const result = ok(await Api.testServerReminder());
    ElMessage[result.status === 'enqueued' ? 'success' : 'warning'](result.status === 'enqueued' ? '测试提醒已发出' : STATES[result.status] || '测试提醒没有发出');
    void load();
  } catch (e) { error.value = safeError(e); }
  finally { testing.value = false; }
}
async function load() { try { recent.value = ok(await Api.serverReminders()).items; } catch { /* optional */ } }
onMounted(load);
</script>
<template>
  <!-- No browser checks: a field it rejected behind another tab would only stop the save without a word. -->
  <form class="instance-form sv-settings-form" autocomplete="off" novalidate @submit.prevent="save">
    <div v-if="moved" class="ix-notice sv-moved" data-tone="warn">
      <div>
        <strong>接入点地址已修改</strong>
        <span>在线的 {{ remoteOnline }} 台节点还在用原来的地址。每台会先用新地址完成一次身份验证，成功后才切换，连不上的暂时保持原地址。没切换的和离线的节点，下次连上主控时自动切换。</span>
      </div>
      <div class="instance-actions">
        <el-button :disabled="pushing" @click="emit('saved', moved)">暂不切换</el-button>
        <el-button type="primary" :loading="pushing" @click="push">让在线节点改用新地址</el-button>
      </div>
    </div>
    <div v-else class="ob-fields sv-set" :class="{busy}">
      <div class="sv-set-tabs">
        <div class="sv-ranges" role="tablist" aria-label="设置分类">
          <button v-for="[value, label] in TABS" :key="value" type="button" role="tab" :aria-selected="tab === value" @click="pick(value)">{{ label }}</button>
        </div>
      </div>
      <section v-show="tab === 'access'" class="sv-set-block sv-set-lead ap-list" aria-labelledby="sv-set-access">
        <header class="sv-set-head"><h3 id="sv-set-access">接入点</h3><button type="button" class="ix-text-button" @click="emit('guide')">怎么准备？</button></header>
        <p class="sv-set-note">节点连回 OpenBear 用的地址，每台服务器各选首选和备用。公网地址必须是 https。</p>
        <div v-for="(point, i) in form.points" :key="idOf(point)" class="ap-row">
          <label class="ap-default" :title="form.defaultAccess === idOf(point) ? '默认接入点' : '设为默认'">
            <input v-model="form.defaultAccess" type="radio" :value="idOf(point)" :aria-label="`${point.name || '接入点'}设为默认`" />
          </label>
          <input v-model="point.name" v-bind="noAutofill" maxlength="30" placeholder="名称" :aria-label="`第 ${i + 1} 个接入点名称`" class="ap-name" />
          <input v-model="point.url" v-bind="noAutofill" class="mono ap-url" placeholder="https://node.example.com" :aria-label="`第 ${i + 1} 个接入点地址`" @change="delete probes[idOf(point)]" />
          <span class="ap-acts">
            <button type="button" class="ix-text-button" :disabled="!point.url.trim() || probes[idOf(point)]?.busy" @click="probe(point)">{{ probes[idOf(point)]?.busy ? '测试中…' : '测试' }}</button>
            <button type="button" class="ix-text-button ob-danger" :disabled="Boolean(point.id && inUse(point))" :title="point.id && inUse(point) ? '还有服务器在用，先给它们换一个接入点' : ''" @click="removePoint(point)">删除</button>
          </span>
          <div v-if="form.defaultAccess === idOf(point) || (point.id && inUse(point)) || probes[idOf(point)]?.text" class="ap-meta">
            <span v-if="form.defaultAccess === idOf(point)" class="sv-log-source">默认</span>
            <span v-if="point.id && inUse(point)" class="instance-muted">{{ inUse(point) }} 台在用</span>
            <small v-if="probes[idOf(point)]?.text" :class="probes[idOf(point)].ok ? 'ob-ok' : 'ob-bad'">{{ probes[idOf(point)].text }}</small>
          </div>
        </div>
        <div v-if="mesh" class="ap-row ap-builtin" :data-available="mesh.available || undefined">
          <span class="ap-default" aria-hidden="true" />
          <div class="ap-builtin-line">
            <strong>{{ mesh.name || '组网' }}</strong>
            <span v-if="mesh.available && mesh.url" class="mono" aria-label="组网接入地址">{{ mesh.url }}</span>
            <span v-else class="ob-dot" data-tone="off">暂不可用</span>
          </div>
          <div class="ap-meta">
            <span class="sv-log-source">内置</span>
            <span v-if="meshUsers" class="instance-muted">{{ meshUsers }} 台在用</span>
            <small class="instance-muted">{{ mesh.available ? '组网成员可以把它选作首选或备用，经组网连回主控；不能设为默认。' : `${meshReason}，组网成员暂时不能选它。` }}</small>
          </div>
        </div>
        <button type="button" class="ix-text-button ap-add" :disabled="form.points.length >= 10" @click="addPoint">＋ 添加接入点</button>
      </section>
      <section v-show="tab === 'access'" class="sv-set-block" aria-labelledby="sv-set-general">
        <header class="sv-set-head"><h3 id="sv-set-general">节点与时区</h3></header>
        <div class="ob-field"><span>节点程序</span>
          <div class="ob-ctl">
            <div class="ob-inline sv-release"><span class="mono">{{ releaseLine }}</span><span class="ob-grow" />
              <button type="button" class="ix-text-button" :disabled="checking" @click="checkRelease">{{ checking ? '检查中…' : release?.available ? '检查新版本' : '重试' }}</button>
            </div>
            <small>{{ releaseNote }}</small>
            <small v-if="switchesNote" data-node-switches>{{ switchesNote }}</small>
          </div>
        </div>
        <div class="ob-field"><span>时区</span>
          <div class="ob-ctl"><input v-model="form.timezone" v-bind="noAutofill" class="mono" aria-label="时区" /><small>流量周期和提醒时间都按这个时区计算，例如 Asia/Shanghai。</small></div>
        </div>
      </section>
      <section v-show="tab === 'checks'" class="sv-set-block sv-set-lead" aria-labelledby="sv-set-checks" data-checks>
        <header class="sv-set-head"><h3 id="sv-set-checks">自动检测</h3></header>
        <label class="ob-check"><input v-model="form.unlock" type="checkbox" data-check="unlock" /><span>解锁检测
          <small class="instance-muted">每 6 小时检测 Google、YouTube、OpenAI、Netflix 等 9 个服务能否访问和解锁地区，IP 变了立即重测，结果显示在卡片和详情页。关闭后不再检测，也不显示。</small></span></label>
        <label class="ob-check"><input v-model="form.route" type="checkbox" data-check="route" /><span>三网回程
          <small class="instance-muted">每天凌晨 4 点左右追踪回北京、上海、广东电信、联通、移动的路由，判断 CN2 GIA、9929、CMIN2 等线路；新接入或 IP 变了立即测。只测 IPv4。关闭后不再检测，也不显示。</small></span></label>
        <label class="ob-check"><input v-model="form.ipcheck" type="checkbox" data-check="ipcheck" /><span>IP 质量
          <small class="instance-muted">在服务器上运行 IP.Check.Place：没检测过的补一次，公网 IP 变了立即重测，之后按下面的间隔重测。同时最多 2 台，每台 1～3 分钟；只测 Linux、没关命令执行的服务器，国内服务器和前置机不测。会用系统包管理器安装缺少的依赖（jq、curl、bc、dnsutils 等）。关闭后只在详情页手动检测。</small></span></label>
        <div v-if="form.ipcheck" class="ob-field"><span>重测间隔</span>
          <div class="ob-ctl"><select v-model.number="form.ipcheckDays" aria-label="IP 质量重测间隔">
            <option :value="1">每天</option><option :value="7">每周</option><option :value="15">每半月</option><option :value="30">每月</option></select></div>
        </div>
      </section>
      <section v-show="tab === 'checks'" class="sv-set-block" aria-labelledby="sv-set-intel" data-intel>
        <header class="sv-set-head"><h3 id="sv-set-intel">IP 情报</h3></header>
        <p class="instance-muted sv-set-note">详情页「IP 质量」里的 ASN 信息、反向 DNS 等直接从公开数据源读取，下面两项要用到凭据。</p>
        <div class="ob-field"><span>蜜罐黑名单</span>
          <div class="ob-ctl">
            <div class="ob-inline">
              <input v-model="form.httpbl" v-bind="noAutofill" type="password" :placeholder="intel.httpbl && !form.httpblDrop ? '已保存，留空不改' : '12 个小写字母'" aria-label="http:BL 访问密钥" />
              <button v-if="intel.httpbl && !form.httpblDrop" type="button" class="ix-text-button" @click="form.httpbl = ''; form.httpblDrop = true">清除</button>
            </div>
            <small>Project Honey Pot 的 http:BL 访问密钥，在 projecthoneypot.org 免费注册后取得，保存在凭证库。{{ form.httpblDrop ? '保存后清除。' : '' }}</small>
          </div>
        </div>
        <div class="ob-field"><span>人机流量</span>
          <div class="ob-ctl"><small data-intel-cloudflare>{{ intel.cloudflare ? `用动态域名里的 Cloudflare 账号「${intel.cloudflare}」读取 Cloudflare Radar，是整个 ASN 最近 7 天的比例。`
            : '在动态域名里添加一个 Cloudflare 账号后，用它的 API 令牌读取 Cloudflare Radar。' }}</small></div>
        </div>
      </section>
      <section v-show="tab === 'reminders'" class="sv-set-block sv-set-lead sv-reminders" aria-labelledby="sv-set-reminders">
        <header class="sv-set-head"><h3 id="sv-set-reminders">提醒</h3>
          <button v-if="form.enabled" type="button" class="ix-text-button" :disabled="testing" @click="test">{{ testing ? '发送中…' : '发一条测试提醒' }}</button></header>
        <label class="ob-check"><input v-model="form.enabled" type="checkbox" /><span>通过 Telegram 提醒到期和流量<small class="instance-muted">同一条提醒只发一次</small></span></label>
        <template v-if="form.enabled">
          <div class="ob-field"><span>提醒时间</span><div class="ob-inline"><input v-bind="noAutofill" v-model="form.time" type="time" class="sv-time" aria-label="每天提醒时间" required /><small>每天这个时间检查到期；流量随时检查</small></div></div>
          <div class="ob-field"><span>到期前</span>
            <div class="sv-chips-edit">
              <span v-for="v in form.expiryDays" :key="v" class="sv-chip">{{ v === 0 ? '当天' : `${v} 天` }}<button type="button" :aria-label="`删除 ${v} 天`" @click="remove('expiryDays', v)"><el-icon><Close /></el-icon></button></span>
              <input v-bind="noAutofill" v-model="drafts.expiryDays" type="number" inputmode="numeric" min="0" max="90" placeholder="添加天数" aria-label="添加到期提醒天数" @keydown.enter.prevent="add('expiryDays', 0, 90)" @blur="add('expiryDays', 0, 90)" />
            </div>
          </div>
          <div class="ob-field"><span>流量用到</span>
            <div class="sv-chips-edit">
              <span v-for="v in form.trafficPercents" :key="v" class="sv-chip">{{ v }}%<button type="button" :aria-label="`删除 ${v}%`" @click="remove('trafficPercents', v)"><el-icon><Close /></el-icon></button></span>
              <input v-bind="noAutofill" v-model="drafts.trafficPercents" type="number" inputmode="numeric" min="1" max="100" placeholder="添加百分比" aria-label="添加流量提醒百分比" @keydown.enter.prevent="add('trafficPercents', 1, 100)" @blur="add('trafficPercents', 1, 100)" />
            </div>
          </div>
        </template>
        <label class="ob-check"><input v-model="form.autoRenew" type="checkbox" /><span>标了自动续费的服务器到期后自动顺延一个周期<small class="instance-muted">{{ form.enabled ? '顺延时会提醒一次，方便核对' : '提醒关闭时照常顺延，只是不发提醒' }}</small></span></label>
      </section>
      <section v-if="recent.length" v-show="tab === 'reminders'" class="sv-set-block sv-recent" aria-labelledby="sv-set-recent">
        <header class="sv-set-head"><h3 id="sv-set-recent">最近提醒</h3><span class="instance-muted">共 {{ recent.length }} 条</span></header>
        <ol>
          <li v-for="r in shownRecent" :key="r.createdAtMs + r.text">
            <span class="sv-log-source">{{ kindText(r.kind) }}</span>
            <span class="sv-recent-text">{{ r.text }}</span>
            <span class="instance-muted">{{ STATES[r.status] || r.status }} · {{ relativeTime(r.createdAtMs, Date.now()) }}</span>
          </li>
        </ol>
        <button v-if="recent.length > RECENT_SHOWN" type="button" class="ix-text-button sv-recent-more" @click="allRecent = !allRecent">{{ allRecent ? '收起' : `显示全部 ${recent.length} 条` }}</button>
      </section>
      <section v-show="tab === 'data'" class="sv-set-block sv-set-lead" aria-labelledby="sv-set-retention" data-retention>
        <header class="sv-set-head"><h3 id="sv-set-retention">保留期限</h3></header>
        <p class="sv-set-note">超过期限的数据每小时自动清理。每日流量永久保留，按日、按月的流量统计不受影响。</p>
        <div class="ob-field"><span>分钟级</span>
          <div class="ob-ctl">
            <select v-model.number="form.minuteDays" aria-label="分钟级数据保留" @change="minuteChanged"><option v-for="d in MINUTE_DAYS" :key="d" :value="d">{{ daysText(d) }}{{ d === 31 ? '（默认）' : '' }}</option></select>
            <small>每分钟一条：1 小时和 24 小时走势、延迟明细用它。</small>
          </div>
        </div>
        <div class="ob-field"><span>小时级</span>
          <div class="ob-ctl">
            <select v-model.number="form.hourDays" aria-label="小时级数据保留"><option v-for="d in hourChoices" :key="d" :value="d">{{ daysText(d) }}{{ d === 400 ? '（默认）' : '' }}</option></select>
            <small>每小时一条：7 天以上走势、分区用量历史用它，不会短于分钟级。</small>
          </div>
        </div>
      </section>
      <section v-show="tab === 'data'" class="sv-set-block sv-storage" aria-labelledby="sv-set-storage" data-storage>
        <header class="sv-set-head"><h3 id="sv-set-storage">占用</h3>
          <span v-if="storage" class="instance-muted">共 {{ bytes(storage.bytes) }}</span></header>
        <p v-if="storageError" class="instance-error">{{ storageError }}</p>
        <p v-else-if="!storage" class="instance-muted" role="status">正在读取…</p>
        <template v-else>
          <div class="sv-storage-bar" aria-hidden="true"><i v-for="l in levels" :key="l.key" :data-level="l.key" :style="{width: `${l.share}%`}" /></div>
          <ul class="sv-storage-list">
            <li v-for="l in levels" :key="l.key" :data-level="l.key">
              <span class="sv-storage-name"><i aria-hidden="true" />{{ l.label }}</span>
              <b>{{ bytes(l.used) }}</b>
              <small class="instance-muted">{{ l.key === 'day' ? '永久保留' : `保留 ${l.keep} · ${l.estimate}` }}</small>
            </li>
          </ul>
          <div class="sv-storage-acts">
            <small class="instance-muted">{{ storage.freeBytes ? `有 ${bytes(storage.freeBytes)} 可以还给系统` : '清理后的空间会自动还给系统' }}</small>
            <button type="button" class="ix-text-button" :disabled="compacting" @click="compact">{{ compacting ? '正在清理…' : '立即清理并压缩' }}</button>
          </div>
          <small class="instance-muted">按已保存的期限清理；改了期限先保存，一分钟内会自动按新期限清理。</small>
        </template>
      </section>
      <section v-show="tab === 'latency'" class="sv-set-block sv-set-lead" aria-labelledby="sv-set-latency" data-latency>
        <header class="sv-set-head"><h3 id="sv-set-latency">延迟监测</h3></header>
        <label class="ob-check"><input v-model="form.latency.enabled" type="checkbox" /><span>开启延迟监测
          <small class="instance-muted">节点定时测到下面这些地址的延迟和丢包，结果显示在卡片和详情页。前置机只测到自己出口的延迟。节点需要更新到支持延迟监测的版本。</small></span></label>
        <template v-if="form.latency.enabled">
          <div class="ob-field"><span>观测地</span>
            <div class="ob-ctl">
              <select v-model="form.latency.province" aria-label="观测地"><option v-for="p in catalog.provinces" :key="p.code" :value="p.name">{{ p.name }}</option></select>
              <small>每台服务器测到这个省电信、联通、移动的延迟，节点地址来自 Zstatic CDN。一般选你自己所在的省。</small>
            </div>
          </div>
          <div class="ob-field"><span>探测间隔</span>
            <div class="ob-ctl">
              <div class="ob-inline sv-reset"><input v-model.number="form.latency.intervalS" v-bind="noAutofill" class="sv-day" type="number" inputmode="numeric" min="3" max="600" required aria-label="探测间隔（秒）" /><small>秒测一次</small></div>
              <small>每个地址各自按这个间隔测，最短 3 秒。丢包率按每分钟的次数算，间隔越短越精确。</small>
            </div>
          </div>
          <div class="ob-field"><span>探测方式</span>
            <div class="ob-ctl">
              <div class="ob-radios" role="radiogroup" aria-label="探测方式">
                <label><input v-model="form.latency.method" type="radio" name="sv-latency-method" value="tcp" />TCP 握手<small class="instance-muted">推荐</small></label>
                <label><input v-model="form.latency.method" type="radio" name="sv-latency-method" value="icmp" />ICMP Ping</label>
              </div>
              <small>{{ form.latency.method === 'tcp' ? '建立一次 TCP 连接计时，各系统都能用，不受禁 Ping 影响。' : '节点要有发 ICMP 的权限，有些地址不回 Ping，会显示为丢包。' }}</small>
            </div>
          </div>
          <div class="ob-field"><span>卡片显示</span>
            <div class="ob-ctl">
              <div class="ob-radios" role="radiogroup" aria-label="卡片显示">
                <label v-for="[value, label] in CARDS" :key="value"><input v-model="form.latency.card" type="radio" name="sv-latency-card" :value="value" />{{ label }}</label>
              </div>
              <small>列表卡片和表格里的延迟、丢包按哪一组地址算：{{ CARDS.find(c => c[0] === form.latency.card)?.[2] }}详情页默认也只画这一组。</small>
            </div>
          </div>
          <div class="ob-field"><span>测哪些地址</span>
            <div class="ob-ctl">
              <div class="ob-inline"><button type="button" class="ix-text-button" @click="pick('library')">在「监测地址」里设置</button></div>
              <small>全国三网节点、国内外地址，以及国内服务器、海外优化线路、海外普通线路各自默认测什么。</small>
            </div>
          </div>
        </template>
      </section>
      <section v-show="tab === 'library'" class="sv-set-block sv-set-lead" aria-labelledby="sv-set-library">
        <header class="sv-set-head"><h3 id="sv-set-library">监测地址</h3></header>
        <LatencyLibrary :latency="form.latency" :catalog="catalog" />
      </section>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div v-if="!moved" class="instance-form-footer">
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
    </div>
  </form>
</template>
