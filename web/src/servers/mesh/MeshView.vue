<script setup>
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, provide, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { MOBILE_VIEWPORT_QUERY } from '../../mobileViewport.js';
import { MeshApi } from './meshApi.js';
import { act, mesh, refresh } from './meshStore.js';
import MeshBoard from './MeshBoard.vue';
import { MESH_VISIBLE, usePoll } from './usePoll.js';
import './mesh.css';

// The mesh tab: loading and polling around the board.
const props = defineProps({active: Boolean, items: {type: Array, default: () => []}, focus: {type: Object, default: null}});
const emit = defineEmits(['open-server']);
const phone = ref(false), shown = ref(true);
let media;
// The mesh is on screen: its tab is shown and the servers page is not put away. Every reading kept
// fresh under it (the panels, the board's paths and punched ends) stops while it is not, and catches up at once.
const visible = computed(() => props.active && shown.value);
provide(MESH_VISIBLE, visible);

const data = computed(() => mesh.data);
const fast = computed(() => Boolean(data.value?.apply?.running) || mesh.installing || Boolean(data.value?.installing?.length));
const poll = usePoll(() => refresh(), {interval: () => fast.value ? 1000 : 5000, active: visible});
const tick = () => poll.run();

async function applyNow() {
  const result = await act(() => MeshApi.apply());
  if (result) poll.schedule();
}
function visibility() { if (!document.hidden) void tick(); }
watch(fast, () => poll.schedule());
watch(() => data.value?.apply?.running, (now, before) => {
  if (!before || now) return;
  const progress = data.value?.apply?.progress || [], failed = progress.filter(p => p.state === 'failed').length;
  const missing = progress.filter(p => p.state === 'missing').length;
  if (failed) ElMessage.warning(`${failed} 台没有应用成功，原因显示在画板的节点上`);
  else if (missing) ElMessage.warning(`已应用到 ${progress.filter(p => p.state === 'done').length} 台；${missing} 台的组网服务不见了，已跳过`);
  else ElMessage.success(`已应用到 ${progress.filter(p => p.state === 'done').length} 台`);
});
function syncPhone() { phone.value = media.matches; }
onMounted(() => {
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener?.('change', syncPhone);
  document.addEventListener('visibilitychange', visibility);
  void tick();
});
onActivated(() => { shown.value = true; });
onDeactivated(() => { shown.value = false; });
onBeforeUnmount(() => { media?.removeEventListener?.('change', syncPhone); document.removeEventListener('visibilitychange', visibility); });
</script>
<template>
  <div class="mesh" :class="{'is-phone': phone}">
    <p v-if="!data && mesh.loadError" role="alert" class="instance-error mesh-state">读取组网失败：{{ mesh.loadError }} <button type="button" class="instance-link" @click="tick">重试</button></p>
    <p v-else-if="mesh.unavailable" class="instance-muted mesh-state">主控还不支持组网，更新 OpenBear 后再试。</p>
    <p v-else-if="!data || !mesh.work" class="instance-muted mesh-state" role="status">正在读取…</p>
    <MeshBoard v-else :key="phone ? 'phone' : 'desktop'" :phone="phone" :active="active" :items="items" :focus="focus" @apply="applyNow" @open-server="emit('open-server', $event)" />
  </div>
</template>
