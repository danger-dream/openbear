<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { ElMessageBox } from 'element-plus';
import { relativeTime, timeLabel } from '../../instances/instanceHelpers.js';
import { MeshApi } from './meshApi.js';
import { act, mesh, meshError } from './meshStore.js';

// The revisions kept: when each went out, why and how, what it changed from the one before; any of
// them comes back as a new revision.
defineProps({phone: Boolean});
const emit = defineEmits(['applied']);
const list = ref(null), error = ref(''), busy = ref(0);
const REASONS = {apply: '应用草稿', rollback: '回到旧版本', facts: '自动更新（节点信息变了，比如公网 IP、可达性）', reissue: '重新签发', reinstall: '重新安装组网服务后签发', leave: '成员退出'};
const STATES = {applied: ['ok', '已下发'], confirming: ['warn', '等节点确认'], partial: ['warn', '部分下发（有节点离线）'], failed: ['bad', '下发失败'], pending: ['off', '待下发'], applying: ['warn', '下发中']};
async function load() {
  try { list.value = (await MeshApi.revisions())?.revisions || []; error.value = ''; }
  catch (e) { error.value = e?.response?.status === 404 ? '主控还不支持版本记录，更新 OpenBear 后再试。' : meshError(e); }
}
onMounted(load);
watch(() => mesh.data?.applied?.revision, load);
const dirty = computed(() => Boolean(mesh.data?.draft?.dirty));
async function back(r) {
  try {
    await ElMessageBox.confirm(`把第 ${r.revision} 版作为草稿，并立即作为新版本应用？${dirty.value ? '现在没应用的草稿会被替换。' : ''}这期间新加入的节点会被移除。`, `回到第 ${r.revision} 版`,
      {confirmButtonText: '回到这一版', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  busy.value = r.revision;
  try { if (await act(() => MeshApi.rollback(r.revision))) emit('applied'); } finally { busy.value = 0; void load(); }
}
</script>
<template>
  <section class="m3-pane" aria-label="版本记录">
    <header class="m3-pane-head"><h2>版本记录</h2></header>
    <p class="m3-lead">每次「应用到网格」都会生成一个新版本。改坏了可以回到之前任意一个版本：那个版本的配置会作为新版本重新下发到所有节点。这里保留最近 10 版，以及最近 10 个不是自动重新签发的版本。</p>
    <p v-if="error" class="ob-bad">{{ error }}</p>
    <p v-else-if="!list" class="instance-muted" role="status">正在读取…</p>
    <p v-else-if="!list.length" class="instance-muted">还没有应用过。</p>
    <ol v-else class="m3-rows m3-rows-revs m3-revisions">
      <li v-if="!phone" aria-hidden="true"><div class="m3-row m3-row-head"><span>版本</span><span>时间</span><span>做了什么</span><span></span></div></li>
      <li v-for="r in list" :key="r.revision" :data-current="r.current || undefined">
        <div class="m3-row">
          <span class="mono">第 {{ r.revision }} 版</span>
          <span class="m3-faint"><time :title="relativeTime(r.at, Date.now())">{{ timeLabel(r.at) }}</time></span>
          <span class="m3-row-conn">{{ REASONS[r.reason] || r.reason || '应用' }}<template v-if="r.changes?.length">：{{ r.changes.join('；') }}</template>
            <span v-if="/自动/.test(REASONS[r.reason] || r.reason || '')" class="m3-tag">自动</span>
            <span v-if="r.current" class="m3-tag" data-kind="lan">当前</span>
            <span v-if="r.status && r.status !== 'applied'" class="ob-dot" :data-tone="(STATES[r.status] || ['off'])[0]">{{ (STATES[r.status] || [r.status, r.status])[1] }}</span></span>
          <span><el-button v-if="!r.current" size="small" :loading="busy === r.revision" :disabled="Boolean(busy) || Boolean(mesh.data?.apply?.running)" @click="back(r)">回到这一版</el-button></span>
        </div>
      </li>
    </ol>
  </section>
</template>
