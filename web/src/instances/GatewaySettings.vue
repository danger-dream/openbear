<script setup>
import { computed, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Api } from '../api.js';
import { flag } from '../servers/serverHelpers.js';
import { errorText, gatewayLastUpdate, gatewayUpdateReasons, ok, relativeTime, safeError } from './instanceHelpers.js';

const props = defineProps({gateway: {type: Object, required: true}, servers: {type: Array, required: true}, instanceCount: {type: Number, default: 0}, updating: {type: Boolean, default: false}});
const emit = defineEmits(['changed', 'edit', 'cancel', 'update']);
const busy = ref(false), error = ref(''), proxies = ref((props.gateway.clientIp?.trustedProxies || []).join('\n'));
defineExpose({busy});
const server = computed(() => props.servers.find(s => s.id === props.gateway.serverId));
const observed = computed(() => props.gateway.observed);
const manual = computed(() => props.gateway.clientIp?.source === 'header');
// Earlier gateways report only proxies they trust and omit the flag.
const untrusted = computed(() => props.gateway.clientIp?.source !== 'socket' && observed.value?.trusted === false);
const detection = computed(() => {
  if (props.gateway.clientIp?.source === 'socket') return ['只使用连接地址', '不读取任何代理请求头。'];
  if (untrusted.value) return ['发现未信任的代理',
    `最近一次请求经 ${observed.value.peer} 转发，带有 ${observed.value.header}（${relativeTime(observed.value.atMs)}）。这个地址还没有被信任，登录限制按它的地址计算。确认是你自己的反向代理后再信任。`];
  if (manual.value) return ['已信任的代理', `来自 ${props.gateway.clientIp.trustedProxies.join('、')} 的请求会读取代理请求头中的真实 IP；本机和 Cloudflare 照常自动识别。`];
  if (observed.value) return [`自动识别 · 经${observed.value.peer.startsWith('127.') || observed.value.peer === '::1' ? '本机' : observed.value.trusted ? ' Cloudflare ' : '内网或 Cloudflare'}反向代理访问`,
    `最近一次请求来自 ${observed.value.peer}，已从 ${observed.value.header} 读取真实 IP（${relativeTime(observed.value.atMs)}），登录限制按真实 IP 计算。`];
  return ['自动识别 · 直接访问', '没有发现反向代理，登录限制按连接地址计算。默认只信任本机的反向代理和 Cloudflare 的 CF-Connecting-IP；内网里的其他代理第一次出现时，会在这里请你确认。'];
});

const updatePending = computed(() => props.updating || Boolean(props.gateway.updating));
const lastUpdate = computed(() => gatewayLastUpdate(props.gateway));
function setAutoUpdate(event) {
  const value = event.target.checked;
  return run(async () => {
    try { return await Api.setInstanceGatewayAutoUpdate(value); }
    catch (e) { event.target.checked = !value; throw e; }
  });
}

async function run(fn) {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { emit('changed', ok(await fn())); }
  catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
function saveProxies() {
  return run(() => Api.setInstanceGatewayProxies(proxies.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)));
}
function trustObserved() {
  const list = [...(props.gateway.clientIp?.trustedProxies || []), observed.value.peer];
  return run(async () => {
    const result = await Api.setInstanceGatewayProxies(list);
    proxies.value = (result?.clientIp?.trustedProxies || list).join('\n');
    return result;
  });
}
async function rotate() {
  try {
    await ElMessageBox.confirm('将重新生成网关的管理密钥，以及每个实例的访问密钥和管理密钥。所有使用者的登录会话都会失效，需要重新登录。暂时连不上的实例会继续用旧密钥，恢复连接后自动更换。',
      '更换密钥', {confirmButtonText: '更换', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  await run(async () => {
    const result = await Api.rotateInstanceGatewayKeys();
    ElMessage.success(result.pendingInstances ? `已更换，${result.pendingInstances} 个实例暂时连不上，恢复后自动更换` : '密钥已全部更换');
    return result;
  });
}
async function disable() {
  try {
    await ElMessageBox.confirm('停用后所有使用者都无法登录，直到重新启用。账号、实例和数据都会保留。', '停用访问网关', {confirmButtonText: '停用', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  await run(() => Api.disableInstanceGateway());
}
</script>
<template>
  <div class="instance-form">
    <dl class="instance-facts ob-facts">
      <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="gateway.connected ? 'ok' : 'bad'">{{ gateway.connected ? '运行中' : '无法连接' }}</span><span class="instance-muted">{{ instanceCount }} 个实例</span><span v-if="!gateway.connected && gateway.error" class="instance-muted">{{ errorText(gateway.error) }}</span></dd></div>
      <div><dt>访问地址</dt><dd class="mono">{{ gateway.publicUrl }} <button type="button" class="instance-link" :disabled="busy" @click="emit('edit')">修改</button></dd></div>
      <div><dt>运行位置</dt><dd class="ob-stack"><span><span v-if="server" class="ss-flag" aria-hidden="true">{{ flag(server.region) || '🖥' }}</span>{{ server?.local ? '本机' : server?.name || '未知服务器' }} · 端口 {{ gateway.port }}</span><span v-if="!server?.local" class="instance-muted">网关放在主控本机最安全。</span></dd></div>
      <div><dt>版本</dt><dd class="ob-stack">
        <span><span class="ob-dot" :data-tone="gateway.updateAvailable ? 'warn' : gateway.build ? 'ok' : 'off'">{{ gateway.updateAvailable ? '访问网关有新版本' : gateway.build ? '已是最新' : '尚未确认' }}</span><span v-if="gateway.build" class="instance-muted mono ix-build" :title="gateway.build">构建 {{ gateway.build.slice(0, 12) }}</span></span>
        <span v-if="gateway.updateAvailable" class="instance-muted">{{ gatewayUpdateReasons(gateway) }}更新时只重启网关，账号和登录状态都保留。</span>
        <span v-if="lastUpdate" class="instance-muted">{{ lastUpdate }}</span>
        <span v-if="gateway.updateAvailable"><el-button :loading="updatePending" :disabled="busy" @click="emit('update')">{{ updatePending ? '正在更新' : '更新网关' }}</el-button></span>
        <label class="ob-switch ix-auto-update"><input type="checkbox" :checked="gateway.autoUpdate" :disabled="busy" @change="setAutoUpdate" /><span>自动更新<small>{{ gateway.autoUpdate ? '发现新版本时自动更新，结果会发通知；失败后不反复重试。' : '已关闭：有新版本时只提示，由你点「更新网关」。' }}</small></span></label>
      </dd></div>
      <div><dt>客户端 IP</dt><dd class="ob-stack"><span>{{ detection[0] }}</span><span class="instance-muted">{{ detection[1] }}</span><span v-if="untrusted"><el-button :loading="busy" @click="trustObserved">信任这个代理</el-button></span></dd></div>
    </dl>
    <details class="ob-adv" :open="manual">
      <summary>识别不对？手动指定代理</summary>
      <div class="ob-field"><span>代理地址</span>
        <div class="ob-ctl">
          <textarea v-model="proxies" rows="2" class="mono" placeholder="每行一个 IP 或网段，例如 10.0.0.1" aria-label="代理地址" spellcheck="false" />
          <small>本机的反向代理和 Cloudflare 不用填写；其他代理（内网或其他服务器上）填在这里，留空则只信任这两种。来自这些地址的请求会依次读取 CF-Connecting-IP、True-Client-IP、X-Real-IP、X-Forwarded-For、Forwarded。</small>
          <div><el-button :loading="busy" @click="saveProxies">保存</el-button></div>
        </div>
      </div>
    </details>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer">
      <el-button class="ob-danger" :disabled="busy" @click="disable">停用网关…</el-button>
      <el-button :disabled="busy" @click="rotate">更换密钥…</el-button>
      <span class="ob-grow"></span>
      <el-button type="primary" :disabled="busy" @click="emit('cancel')">关闭</el-button>
    </div>
  </div>
</template>
