<script setup>
import { computed, ref } from 'vue';
import { ElMessageBox } from 'element-plus';
import { MeshApi } from './meshApi.js';
import { act, mesh, meshErrorText, revert, unsaved } from './meshStore.js';
import { errorMessage, FINISHED, nameOf, PROGRESS } from './meshHelpers.js';

// Above the board while there is something to apply, being applied, or failed.
// As `panel` (nothing selected): always shown, with the whole list.
const props = defineProps({panel: Boolean});
const emit = defineEmits(['apply', 'member']);
const expanded = ref(false), busy = ref('');
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
const kind = computed(() => running.value ? 'running' : failed.value.length && !dirty.value && !local.value && !recovery.value ? 'failed'
  : dirty.value || local.value || recovery.value === 'restoring' ? 'dirty' : missing.value.length ? 'missing' : offline.value.length ? 'offline' : '');
const done = computed(() => progress.value.filter(p => FINISHED.has(p.state)).length);
const applying = computed(() => progress.value.find(p => p.state === 'applying'));
const list = (items, n = 2) => items.slice(0, n).join('；') + (items.length > n ? ` 等 ${items.length} 项` : '');
const title = computed(() => ({
  running: `正在应用 ${done.value}/${progress.value.length}`,
  failed: `${failed.value.length} 台失败`,
  dirty: changes.value.length ? `有 ${changes.value.length} 项更改未应用` : '有更改未应用',
  offline: `${offline.value.length} 台离线，连上后自动补发`,
  missing: `${missing.value.length} 台的组网服务不见了`,
})[kind.value]);
const summary = computed(() => {
  if (kind.value === 'running') return applying.value ? `正在下发到 ${nameOf(members.value, applying.value.serverId)}` : '';
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
function place(path) {
  const member = /^members\[(\d+)\]/.exec(path || '');
  if (member) return {member: members.value[Number(member[1])]?.serverId, text: members.value[Number(member[1])]?.name || '成员'};
  const rule = /^rules\[(\d+)\]/.exec(path || '');
  if (rule) return {text: `访问规则第 ${Number(rule[1]) + 1} 条`};
  if (/^relayLinks/.test(path || '')) return {text: '中转之间的连接'};
  if (/^directLinks/.test(path || '')) return {text: '直连'};
  return {text: '组网设置'};
}
const problems = computed(() => mesh.errors.map(e => ({...place(e.path), message: errorMessage(e)})));
const canApply = computed(() => dirty.value && !local.value && !mesh.saving && !mesh.errors.length && !busy.value && recovery.value !== 'restoring');
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
          <button type="button" class="ix-text-button" @click="emit('member', p.serverId)">{{ nameOf(members, p.serverId) }}</button>：{{ p.state === 'failed' ? meshErrorText(p.error) || '原因未知' : PROGRESS[p.state]?.[1] || p.state }}
        </li>
      </ul>
    </template>
  </div>
  <div v-else-if="kind" class="mesh-bar" :data-kind="kind" :data-tone="['failed', 'missing'].includes(kind) ? 'bad' : kind === 'offline' ? 'off' : 'warn'" role="status" aria-live="polite">
    <div class="mesh-bar-main">
      <div class="mesh-bar-text" :class="{'has-note': mesh.errors.length || mesh.saving}">
        <strong :title="title">{{ title }}</strong>
        <span v-if="summary" class="mesh-bar-summary" :title="summary">{{ summary }}</span>
        <span v-if="mesh.errors.length" class="mesh-bar-problem mesh-bar-note">{{ mesh.errors.length }} 处设置需要修改，{{ mesh.rejected ? '改好之前不会保存' : '改好之前不能应用' }}</span>
        <span v-else-if="mesh.saving" class="mesh-bar-summary mesh-bar-note">正在保存…</span>
      </div>
      <div v-if="kind === 'running'" class="mesh-bar-actions"><i class="sv-bar" :style="{'--v': pct + '%'}" aria-hidden="true"></i></div>
      <div v-else-if="kind === 'failed'" class="mesh-bar-actions">
        <el-button :loading="busy === 'rollback'" :disabled="Boolean(busy)" @click="rollback">回退到上一版</el-button>
        <el-button type="primary" :disabled="Boolean(busy)" @click="emit('apply')">重试</el-button>
      </div>
      <div v-else-if="kind === 'missing'" class="mesh-bar-actions">
        <el-button @click="emit('member', missing[0].serverId)">查看{{ missing.length > 1 ? `「${nameOf(members, missing[0].serverId)}」` : '' }}</el-button>
      </div>
      <div v-else-if="kind === 'dirty'" class="mesh-bar-actions">
        <el-button :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起' : '查看全部' }}</el-button>
        <el-button :loading="busy === 'discard'" :disabled="Boolean(busy)" @click="discard">放弃</el-button>
        <el-button type="primary" :disabled="!canApply" @click="apply">应用</el-button>
      </div>
    </div>
    <ul v-if="expanded && kind === 'dirty'" class="mesh-bar-list">
      <li v-for="(p, i) in problems" :key="'e' + i" data-tone="bad">
        <button v-if="p.member" type="button" class="ix-text-button" @click="emit('member', p.member)">{{ p.text }}</button><span v-else>{{ p.text }}</span>：{{ p.message }}
      </li>
      <li v-for="(c, i) in changes" :key="i">{{ c.text }}</li>
    </ul>
  </div>
</template>
