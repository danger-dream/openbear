<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Close } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok, relativeTime } from '../instances/instanceHelpers.js';
import { errorText, MESH_ACCESS_ID, meshAccessReason, noAutofill, safeError } from './serverHelpers.js';

const props = defineProps({settings: {type: Object, required: true}, items: {type: Array, default: () => []}, build: {type: Object, default: () => ({})}});
const emit = defineEmits(['saved', 'cancel', 'guide', 'checked']);
const rules = props.settings.reminders || {};
const form = reactive({points: (props.settings.accessPoints || []).map(p => ({...p})), defaultAccess: props.settings.defaultAccess || '', timezone: props.settings.timezone || 'Asia/Shanghai',
  enabled: rules.enabled !== false, time: rules.time || '10:00', expiryDays: [...(rules.expiryDays || [7, 3, 1, 0])],
  trafficPercents: [...(rules.trafficPercents || [80, 90, 100])], autoRenew: rules.autoRenew !== false});
const drafts = reactive({expiryDays: '', trafficPercents: ''});
const busy = ref(false), error = ref(''), recent = ref([]), testing = ref(false), moved = ref(null), pushing = ref(false), checking = ref(false);
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
  if (r.localProblem) parts.push(`本地构建${r.localProblem === 'protocol' ? '与主控协议不兼容' : '缺少主控需要的功能'}，改用 GitHub 上的版本`);
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
    const result = ok(await Api.saveServerSettings({accessPoints: points, defaultAccess: form.defaultAccess, timezone: form.timezone.trim(), reminders: {
      enabled: form.enabled, time: form.time, expiryDays: form.expiryDays, trafficPercents: form.trafficPercents, autoRenew: form.autoRenew}})).settings;
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
  <form class="instance-form" autocomplete="off" @submit.prevent="save">
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
    <div v-else class="ob-fields" :class="{busy}">
      <fieldset class="ap-list">
        <legend>接入点<button type="button" class="ix-text-button" @click="emit('guide')">怎么准备？</button></legend>
        <p class="instance-muted">节点连回 OpenBear 用的地址。可以添加多个，例如海外直连一个、国内中转一个，每台服务器各选首选和备用。公网地址必须是 https。</p>
        <div v-for="(point, i) in form.points" :key="idOf(point)" class="ap-row">
          <label class="ap-default" :title="form.defaultAccess === idOf(point) ? '默认接入点' : '设为默认'">
            <input v-model="form.defaultAccess" type="radio" :value="idOf(point)" :aria-label="`${point.name || '接入点'}设为默认`" />
          </label>
          <div class="ap-fields">
            <div class="ob-inline">
              <input v-model="point.name" v-bind="noAutofill" maxlength="30" placeholder="名称，例如：国内中转" :aria-label="`第 ${i + 1} 个接入点名称`" class="ap-name" />
              <input v-model="point.url" v-bind="noAutofill" class="mono" placeholder="https://node.example.com" :aria-label="`第 ${i + 1} 个接入点地址`" @change="delete probes[idOf(point)]" />
            </div>
            <div class="ap-meta">
              <span v-if="form.defaultAccess === idOf(point)" class="sv-log-source">默认</span>
              <span v-if="point.id && inUse(point)" class="instance-muted">{{ inUse(point) }} 台在用</span>
              <small v-if="probes[idOf(point)]?.text" :class="probes[idOf(point)].ok ? 'ob-ok' : 'ob-bad'">{{ probes[idOf(point)].text }}</small>
              <span class="ob-grow" />
              <button type="button" class="ix-text-button" :disabled="!point.url.trim() || probes[idOf(point)]?.busy" @click="probe(point)">{{ probes[idOf(point)]?.busy ? '测试中…' : '测试' }}</button>
              <button type="button" class="ix-text-button ob-danger" :disabled="Boolean(point.id && inUse(point))" :title="point.id && inUse(point) ? '还有服务器在用，先给它们换一个接入点' : ''" @click="removePoint(point)">删除</button>
            </div>
          </div>
        </div>
        <div v-if="mesh" class="ap-row ap-builtin" :data-available="mesh.available || undefined">
          <span class="ap-default" aria-hidden="true" />
          <div class="ap-fields">
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
        </div>
        <button type="button" class="ix-text-button ap-add" :disabled="form.points.length >= 10" @click="addPoint">＋ 添加接入点</button>
      </fieldset>
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
      <fieldset>
        <legend>提醒</legend>
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
          <div class="sv-test"><button type="button" class="ix-text-button" :disabled="testing" @click="test">{{ testing ? '发送中…' : '发一条测试提醒' }}</button></div>
        </template>
        <label class="ob-check"><input v-model="form.autoRenew" type="checkbox" /><span>标了自动续费的服务器到期后自动顺延一个周期<small class="instance-muted">{{ form.enabled ? '顺延时会提醒一次，方便核对' : '提醒关闭时照常顺延，只是不发提醒' }}</small></span></label>
      </fieldset>
      <details v-if="recent.length" class="sv-recent">
        <summary>最近的提醒（{{ recent.length }}）</summary>
        <ol>
          <li v-for="r in recent" :key="r.createdAtMs + r.text">
            <span class="sv-log-source">{{ kindText(r.kind) }}</span>
            <span class="sv-recent-text">{{ r.text }}</span>
            <span class="instance-muted">{{ STATES[r.status] || r.status }} · {{ relativeTime(r.createdAtMs, Date.now()) }}</span>
          </li>
        </ol>
      </details>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div v-if="!moved" class="instance-form-footer">
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
    </div>
  </form>
</template>
