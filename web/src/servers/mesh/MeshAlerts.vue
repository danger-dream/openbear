<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import { safeError } from '../serverHelpers.js';
import MeshFr from './MeshFr.vue';

// The mesh's server reminders by type (settings.reminders.mesh), saved as each is switched: they are the
// controller's, not the mesh's, so no draft and no revision. One off is still listed, not sent.
const ALERTS = [
  ['standby', '备用入口都连不上', '成员的备用入口全部连不上，当前入口一断就没处可切。'],
  ['entry', '连不上任何入口', '成员一个入口都连不上，持续 1 分钟以上。'],
  ['flap', '频繁切换入口', '成员 5 分钟内切换入口 3 次以上。'],
  ['exit', '出口地址不一致', '不同观测点看到的出口地址不同，打洞不会用它的公网地址。'],
  ['memory', '数据面内存超限', '节点组网服务的内存超过上限。'],
  ['apply', '配置下发失败', '应用时某个节点没有收下新配置。'],
  ['upgrade', '组网服务升级失败', '节点的组网服务升级后没起来，已回退。'],
  ['missing', '组网服务不见了', '节点上的组网服务被卸载或重装过。'],
  ['keys', '密钥和别的机器相同', '加入组网时上报的密钥和已有机器的一样，没有安装。'],
];
const state = reactive(Object.fromEntries(ALERTS.map(([key]) => [key, true])));
const loaded = ref(false), busy = ref('');
const allOn = computed(() => ALERTS.every(([key]) => state[key]));

onMounted(async () => {
  try {
    Object.assign(state, (await Api.serverSettings()).settings?.reminders?.mesh || {});
    loaded.value = true;
  } catch (error) {
    ElMessage.error(safeError(error));
  }
});
async function put(change) {
  const before = {...state};
  Object.assign(state, change);
  busy.value = Object.keys(change).length > 1 ? '*' : Object.keys(change)[0];
  try {
    Object.assign(state, ok(await Api.saveServerSettings({reminders: {mesh: change}})).settings.reminders.mesh);
  } catch (error) {
    Object.assign(state, before);
    ElMessage.error(safeError(error));
  } finally {
    busy.value = '';
  }
}
</script>
<template>
  <section class="m3-dsec m3-settings mesh-alerts" aria-labelledby="mesh-alerts-title">
    <h3 id="mesh-alerts-title">通知
      <button type="button" class="ix-text-button" :disabled="!loaded || allOn || Boolean(busy)" @click="put(Object.fromEntries(ALERTS.map(([key]) => [key, true])))">全部打开</button></h3>
    <p class="m3-hint">组网出问题时发到 Telegram 的服务器提醒，按类型开关。切换后立即生效，不进草稿、不用应用；关掉的类型仍记在服务器设置的「最近提醒」里，只是不推送。</p>
    <MeshFr v-for="[key, label, hint] in ALERTS" :key="key" :label="label" :sub="hint" :hint="hint">
      <button type="button" class="m3-sw" role="switch" :aria-checked="state[key]" :aria-label="`${label}提醒`" :disabled="!loaded || Boolean(busy)" @click="put({[key]: !state[key]})">
        <i></i><span>{{ state[key] ? '推送' : '不推送' }}</span></button>
    </MeshFr>
  </section>
</template>
