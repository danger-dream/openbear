<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { Api } from '../api.js';
import ServerSelect from './ServerSelect.vue';
import { defaultAccessUrl, needsMapping, nodeReady, ok, safeError } from './instanceHelpers.js';

const props = defineProps({servers: {type: Array, required: true}, gateway: {type: Object, default: null}});
const emit = defineEmits(['done', 'cancel', 'servers-changed']);
const current = props.gateway?.enabled ? props.gateway : null;
const form = reactive({serverId: current?.serverId || 'local', port: current?.port || 18962, publicUrl: current?.publicUrl || ''});
const busy = ref(false), error = ref(''), check = ref({state: 'idle'}), ip = ref(''), urlEdited = ref(Boolean(current));
defineExpose({busy});

const server = computed(() => props.servers.find(s => s.id === form.serverId));
const portValid = computed(() => Number.isInteger(form.port) && form.port >= 1 && form.port <= 65535);
const mapping = computed(() => form.publicUrl && needsMapping(form.publicUrl, ip.value, form.port));
const ready = computed(() => check.value.state === 'free' && form.publicUrl && !busy.value);
let ticket = 0, timer;

async function refresh() {
  const mine = ++ticket;
  check.value = {state: portValid.value ? 'checking' : 'invalid'};
  if (!portValid.value) return;
  try {
    const [port, suggest] = await Promise.all([Api.checkInstanceGatewayPort(form.serverId, form.port), Api.suggestInstanceGateway(form.serverId, form.port)]);
    if (mine !== ticket) return;
    ip.value = suggest.publicIp || '';
    if (!urlEdited.value) form.publicUrl = defaultAccessUrl(ip.value, form.port);
    check.value = port.free === false ? {state: 'used', by: port.usedBy} : {state: port.free === null ? 'unknown' : 'free'};
    if (port.free === null) check.value = {state: 'free', unknown: true};
  } catch (e) { if (mine === ticket) check.value = {state: 'error', message: safeError(e)}; }
}
function schedule() { clearTimeout(timer); timer = setTimeout(refresh, 350); }
watch(() => form.serverId, refresh);
watch(() => form.port, schedule);
function editUrl(event) { form.publicUrl = event.target.value; urlEdited.value = true; }
function resetUrl() { urlEdited.value = false; form.publicUrl = defaultAccessUrl(ip.value, form.port); }

async function submit() {
  if (!ready.value) return;
  error.value = ''; busy.value = true;
  try { emit('done', ok(await Api.enableInstanceGateway({serverId: form.serverId, port: form.port, publicUrl: form.publicUrl.trim()}))); }
  catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
onMounted(refresh);
</script>
<template>
  <form class="instance-form" autocomplete="off" @submit.prevent="submit">
    <p class="ob-lead">所有实例共用一个访问地址，使用者用自己的账号登录后进入各自的实例。</p>
    <div class="ob-fields">
      <div class="ob-field"><span>运行位置</span>
        <div class="ob-ctl">
          <ServerSelect v-model="form.serverId" :servers="servers" @added="emit('servers-changed')" />
          <small>作为独立服务运行，主控重启不影响登录和使用。网关放在主控本机最安全。</small>
        </div>
      </div>
      <div class="ob-field"><span>端口</span>
        <div class="ob-ctl">
          <div class="ob-inline">
            <input v-model.number="form.port" class="mono ob-port" type="number" inputmode="numeric" min="1" max="65535" required aria-label="网关端口" :class="{invalid: ['used', 'invalid'].includes(check.state)}" />
            <small v-if="check.state === 'checking'">正在检查…</small>
            <small v-else-if="check.state === 'free'" class="ob-ok">✓ {{ check.unknown ? '无法确认占用情况，启用时会再检查' : (server?.local ? '本机' : server?.name) + '上可用' }}</small>
            <small v-else-if="check.state === 'used'" class="ob-bad">✗ 已被 {{ check.by || '其他程序' }} 占用</small>
            <small v-else-if="check.state === 'invalid'" class="ob-bad">✗ 请输入 1–65535 之间的端口</small>
            <small v-else-if="check.state === 'error'" class="ob-bad">✗ {{ check.message }}</small>
          </div>
          <small>{{ server?.local || nodeReady(server) ? '浏览器从这个端口访问；主控管理网关另用一个只在服务器本机监听的端口。' : '这台服务器没有在线节点，浏览器访问和主控管理共用这一个端口。' }}</small>
        </div>
      </div>
      <div class="ob-field"><span>访问地址</span>
        <div class="ob-ctl">
          <input :value="form.publicUrl" class="mono" type="url" required placeholder="http://域名或IP:端口" aria-label="访问地址" @input="editUrl" />
          <small>{{ ip ? `默认是${server?.local ? '本机' : server?.name}的外网地址 + 端口，可以改成域名或其他地址。` : '没有查到外网 IP，请手动填写。' }}<button v-if="urlEdited && ip" type="button" class="instance-link" @click="resetUrl">恢复默认</button></small>
          <p v-if="mapping" class="ob-notice">请自行把 {{ form.publicUrl.replace(/^https?:\/\//, '') }} 映射到 {{ ip }}:{{ form.port }}（域名解析、反向代理或端口转发），OpenBear 不处理这一步；映射好之前，这个地址暂时打不开。</p>
        </div>
      </div>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer">
      <span class="ob-hint">{{ server?.local ? `将在本机安装并启动网关服务，监听 ${form.port} 端口。` : `将通过${nodeReady(server) ? '服务器节点' : ' SSH '}在${server?.name || '所选服务器'}上安装并启动网关，监听 ${form.port} 端口。` }}</span>
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!ready">{{ current ? '保存' : '启用' }}</el-button>
    </div>
  </form>
</template>
