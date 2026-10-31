<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { install, mesh, meshErrorText, refresh } from './meshStore.js';
import { entryLine, isRelay, nodeState } from './meshHelpers.js';
import './mesh.css';

// The mesh card on a server's detail page.
const props = defineProps({serverId: {type: String, required: true}, active: Boolean});
const emit = defineEmits(['open']);
const busy = ref(false);
let timer;
const members = computed(() => mesh.work?.members || []);
const member = computed(() => members.value.find(m => m.serverId === props.serverId) || null);
const eligible = computed(() => mesh.data?.eligible?.find(e => e.serverId === props.serverId) || null);
const reason = computed(() => eligible.value ? (eligible.value.ok ? '' : eligible.value.reason || '暂时不能加入') : '未安装节点');
const state = computed(() => nodeState(mesh.data, props.serverId));
const entry = computed(() => member.value && !isRelay(member.value) ? entryLine(member.value, members.value, mesh.data?.nodes?.[props.serverId]?.summary) : null);

async function join() {
  busy.value = true;
  const data = await install([props.serverId]);
  busy.value = false;
  const result = data?.items?.find(i => i.serverId === props.serverId);
  if (result && !result.ok) ElMessage.warning(`没有加入：${result.reason || meshErrorText(result.error) || '原因未知'}`);
  else if (data) { ElMessage.success('已加入草稿，在组网页应用后生效'); emit('open', props.serverId); }
  else if (mesh.error) ElMessage.error(mesh.error);
}
function poll() {
  clearTimeout(timer);
  if (props.active && !document.hidden) void refresh();
  timer = setTimeout(poll, 15000);
}
watch(() => props.active, value => { if (value) poll(); });
onMounted(poll);
onBeforeUnmount(() => clearTimeout(timer));
</script>
<template>
  <section v-if="mesh.data" class="ix-card mesh-card" aria-label="组网">
    <header class="ix-card-head"><h3>组网</h3><span class="ob-grow" />
      <button v-if="member" type="button" class="ix-text-button" @click="emit('open', serverId)">在组网中查看</button>
      <button v-else type="button" class="ix-text-button" :disabled="busy || Boolean(reason)" @click="join">{{ busy ? '正在加入…' : '加入组网' }}</button>
    </header>
    <dl v-if="member" class="ix-facts">
      <div><dt>网格地址</dt><dd class="mono">{{ member.address || '应用后分配' }}</dd></div>
      <div><dt>角色</dt><dd>{{ isRelay(member) ? '中转' : '成员' }}{{ member.gateway ? ' · 网关' : '' }}{{ member.removing ? ' · 待移除' : '' }}</dd></div>
      <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="state[0]">{{ state[1] }}</span></dd></div>
      <div v-if="entry"><dt>入口</dt><dd :class="{'ob-warn': entry.tone === 'warn'}">{{ entry.text }}</dd></div>
    </dl>
    <p v-else class="instance-muted mesh-card-join">未加入组网{{ reason ? `：${reason}` : '' }}</p>
  </section>
</template>
