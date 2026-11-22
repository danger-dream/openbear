<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessageBox } from 'element-plus';
import { MeshApi } from './meshApi.js';
import { act, applyBlocked, mesh, meshErrorText, revert, unsaved } from './meshStore.js';
import { errorMessage, FINISHED, nameOf, PROGRESS } from './meshHelpers.js';
import MeshNodeName from './MeshNodeName.vue';

// Above the board while there is something to apply, being applied, or failed.
// As `panel` (nothing selected): always shown, with the whole list.
const props = defineProps({panel: Boolean});
const emit = defineEmits(['apply', 'member']);
const expanded = ref(false), busy = ref(''), showProgress = ref(false);
const data = computed(() => mesh.data);
const members = computed(() => mesh.work?.members || []);
const progress = computed(() => data.value?.apply?.progress || []);
const running = computed(() => Boolean(data.value?.apply?.running));
const failed = computed(() => progress.value.filter(p => p.state === 'failed'));
const offline = computed(() => progress.value.filter(p => p.state === 'offline'));
// Members whose mesh service is gone: every rollout skips them until they are reinstalled.
const missing = computed(() => members.value.filter(m => !m.removing && data.value?.nodes?.[m.serverId]?.state === 'missing').map(m => ({serverId: m.serverId, state: 'missing'})));
const changes = computed(() => data.value?.draft?.changes || []);
const dirty = computed(() => Boolean(data.value?.draft?.dirty));
const local = computed(() => mesh.rejected || unsaved());
const recovery = computed(() => data.value?.apply?.recovery || '');
const recoveryText = computed(() => recovery.value === 'restoring' ? '正在恢复旧网络，等待节点重连确认；草稿已保留。' : recovery.value === 'restored' ? '这次变更未提交，已恢复旧网络；草稿已保留，可调整后重试。' : '');
const preparing = computed(() => mesh.applying || Boolean(data.value?.apply?.preparing));
const kind = computed(() => running.value ? 'running' : preparing.value ? 'preparing' : failed.value.length && !dirty.value && !local.value && !recovery.value ? 'failed'
  : dirty.value || (local.value && (!mesh.conflict || props.panel)) || recovery.value === 'restoring' ? 'dirty' : missing.value.length ? 'missing' : offline.value.length ? 'offline' : '');
const done = computed(() => progress.value.filter(p => FINISHED.has(p.state)).length);
const applying = computed(() => progress.value.filter(p => p.state === 'applying'));
// What the controller is doing before the rollout starts, as the last poll saw it.
const PHASES = {waiting: '正在等待其他组网操作完成', facts: '正在读取各节点的最新状态', tls: '正在检查中继的 TLS 证书',
  check: '正在校验设置和端口', build: '正在生成新配置'};
const now = ref(Date.now());
let clock = null;
watch(() => ['preparing', 'running'].includes(kind.value), on => {
  clearInterval(clock);
  clock = on ? setInterval(() => { now.value = Date.now(); }, 1000) : null;
  now.value = Date.now();
  if (!on) showProgress.value = false;
}, {immediate: true});
onBeforeUnmount(() => clearInterval(clock));
// Known only for an apply started on this page.
const elapsed = computed(() => mesh.applyStartedAt ? `已用 ${Math.max(0, Math.round((now.value - mesh.applyStartedAt) / 1000))} 秒` : '');
const withElapsed = text => [text, elapsed.value].filter(Boolean).join(' · ');
const list = (items, n = 2) => items.slice(0, n).join('；') + (items.length > n ? ` 等 ${items.length} 项` : '');
const title = computed(() => ({
  running: `正在应用 ${done.value}/${progress.value.length}`,
  preparing: '正在准备应用',
  failed: `${failed.value.length} 台失败`,
  dirty: changes.value.length ? `有 ${changes.value.length} 项更改未应用` : local.value ? '有修改还没保存' : '有更改未应用',
  offline: `${offline.value.length} 台离线，连上后自动补发`,
  missing: `${missing.value.length} 台的组网服务不见了`,
})[kind.value]);
const summary = computed(() => {
  if (kind.value === 'running') {
    const names = applying.value.map(p => nameOf(members.value, p.serverId));
    return withElapsed(names.length ? `正在下发到 ${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 台` : ''}` : '');
  }
  if (kind.value === 'preparing') return withElapsed(PHASES[data.value?.apply?.preparing?.phase] || '正在检查各节点、生成新配置，完成后开始下发');
  if (kind.value === 'failed') return list(failed.value.map(p => `${nameOf(members.value, p.serverId)}：${meshErrorText(p.error) || '原因未知'}`));
  if (kind.value === 'offline') return offline.value.map(p => nameOf(members.value, p.serverId)).join('、');
  if (kind.value === 'missing') {
    const names = missing.value.map(p => nameOf(members.value, p.serverId));
    return `${names.slice(0, 3).join('、')}${names.length > 3 ? ` 等 ${names.length} 台` : ''}：下发时跳过，重新安装组网服务后恢复`;
  }
  if (kind.value === 'dirty') return recoveryText.value || list(changes.value.map(c => c.text));
  return '';
});
// Where an error points, for the expanded list: members[2].entry… → that member.
function place(e) {
  const path = e?.path || '';
  const member = /^members\[(\d+)\]/.exec(path);
  if (member) return {member: members.value[Number(member[1])]?.serverId, text: members.value[Number(member[1])]?.name || '成员'};
  const rpair = /^relayLinks\.pairs\[(\d+)\]/.exec(path);
  if (rpair) {
    const p = mesh.work?.relayLinks?.pairs?.[Number(rpair[1])];
    if (p) {
      const target = ['endpoint_missing', 'transport_not_listening', 'tls_fingerprint_missing'].includes(e?.error)
        ? (p.dialer === 'b' ? p.a : p.b) : p.a;
      return {member: target, text: `中转线路 ${nameOf(members.value, p.a)} ⇄ ${nameOf(members.value, p.b)}`};
    }
  }
  const dpair = /^directLinks\[(\d+)\]/.exec(path);
  if (dpair) {
    const p = mesh.work?.directLinks?.[Number(dpair[1])];
    if (p) return {member: p.a, text: `直连 ${nameOf(members.value, p.a)} ⇄ ${nameOf(members.value, p.b)}`};
  }
  const rule = /^rules\[(\d+)\]/.exec(path);
  if (rule) return {text: `访问规则第 ${Number(rule[1]) + 1} 条`};
  const limit = /^limits\[(\d+)\]/.exec(path);
  if (limit) return {text: `连接限制第 ${Number(limit[1]) + 1} 条`};
  const fpath = /^paths\[(\d+)\]/.exec(path);
  if (fpath) return {text: `自建路线第 ${Number(fpath[1]) + 1} 条`};
  const lrule = /^lineRules\[(\d+)\]/.exec(path);
  if (lrule) return {text: `线路规则第 ${Number(lrule[1]) + 1} 条`};
  if (/^relayLinks/.test(path)) return {text: '中转之间的连接'};
  if (/^directLinks/.test(path)) return {text: '直连'};
  return {text: '组网设置'};
}
const problems = computed(() => mesh.errors.map(e => ({...place(e), message: errorMessage(e)})));
const hasDetails = computed(() => Boolean(problems.value.length || changes.value.length));
watch(hasDetails, ok => { if (!ok) expanded.value = false; });
const canApply = computed(() => dirty.value && !applyBlocked() && !busy.value && recovery.value !== 'restoring');
const pct = computed(() => progress.value.length ? Math.round(done.value / progress.value.length * 100) : 0);

async function confirm(text, heading, button) {
  try { await ElMessageBox.confirm(text, heading, {confirmButtonText: button, cancelButtonText: '取消', type: 'warning'}); return true; } catch { return false; }
}
async function run(name, call) {
  busy.value = name;
  try { await act(call); } finally { busy.value = ''; }
}
async function discard() {
  if (!await confirm(`放弃${changes.value.length ? ` ${changes.value.length} 项` : ''}未应用的更改，回到已应用的版本？`, '放弃更改', '放弃')) return;
  expanded.value = false;
  if (dirty.value) await run('discard', () => MeshApi.discard()); else revert();
}
async function rollback() {
  if (await confirm('把上一个已应用的版本作为草稿并立即应用？这次失败的版本会被替换。', '回退到上一版', '回退')) await run('rollback', () => MeshApi.rollback());
}
function apply() { expanded.value = false; emit('apply'); }
</script>
<template>
  <div v-if="panel" class="mesh-changes" :data-tone="['failed', 'missing'].includes(kind) ? 'bad' : kind && kind !== 'offline' ? 'warn' : undefined">
    <p v-if="!kind" class="instance-muted">没有未应用的更改{{ data?.applied?.revision ? `，已应用第 ${data.applied.revision} 版` : '' }}。</p>
    <template v-else>
      <strong>{{ title }}</strong>
      <p v-if="recoveryText" class="instance-muted">{{ recoveryText }}</p>
      <ul class="mesh-bar-list">
        <template v-if="kind === 'dirty'">
          <li v-for="(p, i) in problems" :key="'e' + i" data-tone="bad">
            <button v-if="p.member" type="button" class="ix-text-button" @click="emit('member', p.member)">{{ p.text }}</button><span v-else>{{ p.text }}</span>：{{ p.message }}
          </li>
          <li v-for="(c, i) in changes" :key="i">{{ c.text }}</li>
        </template>
        <li v-for="p in kind === 'failed' ? failed : kind === 'offline' ? offline : kind === 'missing' ? missing : kind === 'running' ? progress : []" :key="p.serverId" :data-tone="p.state === 'failed' ? 'bad' : p.state === 'missing' ? 'warn' : undefined">
          <button type="button" class="ix-text-button" @click="emit('member', p.serverId)"><MeshNodeName :id="p.serverId" plain /></button>：{{ p.state === 'failed' ? meshErrorText(p.error) || '原因未知' : PROGRESS[p.state]?.[1] || p.state }}
        </li>
      </ul>
    </template>
  </div>
  <div v-else-if="kind" class="mesh-bar" :data-kind="kind" :data-tone="['failed', 'missing'].includes(kind) ? 'bad' : kind === 'offline' ? 'off' : 'warn'" role="status" aria-live="polite">
    <div class="mesh-bar-main">
      <div class="mesh-bar-text" :class="{'has-note': mesh.errors.length || mesh.saving}">
        <strong :title="title">{{ title }}</strong>
        <span v-if="summary && !(expanded && kind === 'dirty')" class="mesh-bar-summary" :title="summary">{{ summary }}</span>
        <span v-if="mesh.errors.length" class="mesh-bar-problem mesh-bar-note">{{ mesh.errors.length }} 处设置需要修改，{{ mesh.rejected ? '改好之前不会保存' : '改好之前不能应用' }}</span>
        <span v-else-if="mesh.saving" class="mesh-bar-summary mesh-bar-note">正在保存…</span>
      </div>
      <div v-if="kind === 'running'" class="mesh-bar-actions">
        <i class="sv-bar" :style="{'--v': pct + '%'}" aria-hidden="true"></i>
        <el-button :aria-expanded="showProgress" @click="showProgress = !showProgress">{{ showProgress ? '收起' : '查看进度' }}</el-button>
      </div>
      <div v-else-if="kind === 'preparing'" class="mesh-bar-actions">
        <el-button type="primary" loading disabled>应用中</el-button>
      </div>
      <div v-else-if="kind === 'failed'" class="mesh-bar-actions">
        <el-button :loading="busy === 'rollback'" :disabled="Boolean(busy)" @click="rollback">回退到上一版</el-button>
        <el-button type="primary" :disabled="Boolean(busy)" @click="emit('apply')">重试</el-button>
      </div>
      <div v-else-if="kind === 'missing'" class="mesh-bar-actions">
        <el-button @click="emit('member', missing[0].serverId)">查看{{ missing.length > 1 ? `「${nameOf(members, missing[0].serverId)}」` : '' }}</el-button>
      </div>
      <div v-else-if="kind === 'dirty'" class="mesh-bar-actions">
        <el-button v-if="hasDetails" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起' : '查看全部' }}</el-button>
        <el-button :loading="busy === 'discard'" :disabled="Boolean(busy)" @click="discard">放弃</el-button>
        <el-button type="primary" :disabled="!canApply" @click="apply">应用</el-button>
      </div>
    </div>
    <ul v-if="showProgress && kind === 'running'" class="mesh-bar-list">
      <li v-for="p in progress" :key="p.serverId" :data-tone="p.state === 'failed' ? 'bad' : undefined">
        <button type="button" class="ix-text-button" @click="emit('member', p.serverId)"><MeshNodeName :id="p.serverId" plain /></button>：{{ p.state === 'failed' ? meshErrorText(p.error) || '原因未知' : PROGRESS[p.state]?.[1] || p.state }}
      </li>
    </ul>
    <ul v-if="expanded && kind === 'dirty' && hasDetails" class="mesh-bar-list">
      <li v-for="(p, i) in problems" :key="'e' + i" data-tone="bad">
        <button v-if="p.member" type="button" class="ix-text-button" @click="emit('member', p.member)">{{ p.text }}</button><span v-else>{{ p.text }}</span>：{{ p.message }}
      </li>
      <li v-for="(c, i) in changes" :key="i">{{ c.text }}</li>
    </ul>
  </div>
</template>
