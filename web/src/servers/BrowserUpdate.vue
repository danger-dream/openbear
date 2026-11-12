<script setup>
import {computed, ref, onBeforeUnmount} from 'vue';
import {ElMessageBox} from 'element-plus';
import {Api} from '../api.js';
import {ok} from '../instances/instanceHelpers.js';
import {safeError} from './serverHelpers.js';

const props = defineProps({serverId: String, browser: Object, online: Boolean, supported: Boolean});
const emit = defineEmits(['changed']);
const checking = ref(false), available = ref(''), message = ref(''), error = ref('');
let alive = true;
onBeforeUnmount(() => { alive = false; });
const updating = computed(() => ['downloading', 'restarting'].includes(props.browser?.update?.state));
async function check() {
  if (checking.value || updating.value || !props.online || !props.supported) return;
  checking.value = true; error.value = ''; message.value = '';
  try {
    const {update} = ok(await Api.updateServerBrowser(props.serverId, {check: true}));
    if (!alive) return;
    available.value = update.upToDate ? '' : update.latest;
    message.value = update.upToDate ? '已是最新 Stable' : '';
  } catch (e) { if (alive) error.value = safeError(e); }
  finally { if (alive) checking.value = false; }
}
async function update() {
  if (checking.value || updating.value || !props.online || !props.supported) return;
  checking.value = true; error.value = ''; message.value = '';
  try {
    const restart = ['ready', 'starting'].includes(props.browser?.state);
    if (restart) {
      try { await ElMessageBox.confirm('会重启浏览器，打开的页面会关闭。登录状态保留。', '更新 Chrome', {confirmButtonText: '更新并重启', cancelButtonText: '取消', type: 'warning'}); }
      catch { return; }
    }
    if (!alive) return;
    const {update} = ok(await Api.updateServerBrowser(props.serverId, {restart}));
    if (!alive) return;
    available.value = '';
    message.value = update.started ? '正在下载更新，失败时保留原版本' : '已是最新 Stable';
    emit('changed');
  } catch (e) { if (alive) error.value = safeError(e); }
  finally { if (alive) checking.value = false; }
}
</script>

<template>
  <div class="sv-browser-update">
    <small class="instance-muted">下载后固定使用该版本，可手动检查更新。<template v-if="browser?.chromeVersion">当前下载版本：{{ browser.chromeVersion }}</template></small>
    <div class="sv-browser-update-actions">
      <button type="button" class="ix-text-button" :disabled="checking || updating || !online || !supported" @click="check">检查更新</button>
      <button v-if="available" type="button" class="ix-text-button" :disabled="checking || updating || !online" @click="update">更新到 {{ available }}</button>
      <small v-if="!online" class="instance-muted">节点离线</small>
      <small v-else-if="!supported" class="instance-muted">需要先更新节点</small>
    </div>
    <small v-if="updating" class="instance-muted">{{ browser.update.state === 'restarting' ? '正在重启浏览器…' : '正在下载更新…' }}</small>
    <small v-else-if="browser?.update?.state === 'failed'" role="alert">更新失败，原版本保留：{{ browser.update.error }}</small>
    <small v-else-if="browser?.update?.error" role="alert">已下载，但重启失败：{{ browser.update.error }}</small>
    <small v-else-if="browser?.update?.restartNeeded" class="instance-muted">新版已下载，重启浏览器后生效，现有页面未关闭。</small>
    <small v-else-if="browser?.update?.state === 'done'" class="instance-muted">已更新到 {{ browser.update.version }}</small>
    <small v-else-if="message" class="instance-muted">{{ message }}</small>
    <small v-if="error" role="alert">{{ error }}</small>
  </div>
</template>

<style scoped>
.sv-browser-update { display: grid; gap: 6px; margin-top: 8px; overflow-wrap: anywhere; }
.sv-browser-update-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.sv-browser-update-actions button { min-height: 32px; white-space: normal; text-align: left; }
</style>
