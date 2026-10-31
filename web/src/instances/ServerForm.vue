<script setup>
import { onBeforeUnmount, reactive, ref } from 'vue';
import { Api } from '../api.js';
import { HOST_PATTERN, HOST_TITLE, USER_PATTERN, USER_TITLE, ok, safeError } from './instanceHelpers.js';
import SshAuth from './SshAuth.vue';

const props = defineProps({server: {type: Object, default: null}});
const emit = defineEmits(['saved', 'cancel']);
const editing = props.server;
const form = reactive({name: editing?.name || '', host: editing?.host || '', port: editing?.port || 22, username: editing?.username || 'root',
  auth: editing ? (editing.auth === 'upload' ? 'upload' : editing.auth) : 'key'});
const login = ref(null);
const busy = ref(false), testing = ref(false), error = ref(''), testResult = ref(null);
defineExpose({busy});

const credentials = () => login.value?.credentials() || {};
function payload() {
  return {name: form.name.trim(), host: form.host.trim(), port: Number(form.port), username: form.username.trim(), auth: form.auth, ...credentials()};
}
async function test() {
  if (testing.value) return;
  error.value = ''; testResult.value = null; testing.value = true;
  try { testResult.value = ok(await Api.testInstanceServer(payload())); }
  catch (e) { testResult.value = {failed: safeError(e)}; }
  finally { testing.value = false; }
}
async function save() {
  if (busy.value) return;
  error.value = '';
  const body = payload();
  if (editing && form.auth === editing.auth && !Object.keys(credentials()).some(k => body[k])) {
    for (const k of ['password', 'privateKey', 'keyRef', 'saveKey']) delete body[k];
  }
  busy.value = true;
  try {
    const result = ok(await (editing ? Api.updateInstanceServer(editing.id, body) : Api.createInstanceServer(body)));
    login.value?.clear();
    emit('saved', result.item);
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
onBeforeUnmount(() => login.value?.clear());
</script>
<template>
  <form class="instance-form" autocomplete="off" @submit.prevent="save">
    <div class="ob-fields" :class="{busy}">
      <label class="ob-field"><span>名称</span><input v-model="form.name" required maxlength="60" placeholder="例如：东京服务器" aria-label="服务器名称" /></label>
      <div class="ob-field"><span>地址</span>
        <div class="ob-ctl">
          <div class="ob-inline"><input v-model="form.host" required maxlength="253" :pattern="HOST_PATTERN" :title="HOST_TITLE" class="mono" placeholder="IP 或域名" aria-label="服务器地址" /><input v-model.number="form.port" class="mono ob-port" type="number" inputmode="numeric" min="1" max="65535" required aria-label="SSH 端口" /></div>
          <small>主控通过这个地址连接服务器，可以是公网 IP，也可以是 WG 等内网地址。</small>
        </div>
      </div>
      <label class="ob-field"><span>用户</span><input v-model="form.username" required maxlength="64" :pattern="USER_PATTERN" :title="USER_TITLE" class="mono" aria-label="SSH 用户" /></label>
      <SshAuth ref="login" v-model="form.auth" :editing="editing" @error="error = $event" />
      <div class="ob-field"><span></span>
        <div class="ob-inline">
          <el-button :loading="testing" :disabled="busy" @click="test">测试连接</el-button>
          <small v-if="testResult?.failed" class="ob-bad">✗ {{ testResult.failed }}</small>
          <small v-else-if="testResult" class="ob-ok">✓ 连接成功{{ testResult.os ? ' · ' + testResult.os : '' }}{{ testResult.docker ? ' · Docker ' + testResult.docker : ' · 未安装 Docker' }}{{ testResult.publicIp ? ' · 外网 IP ' + testResult.publicIp : '' }}</small>
        </div>
      </div>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer">
      <span class="ob-hint">保存前会自动测试一次；没有 Docker 时，部署会先安装。</span>
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
    </div>
  </form>
</template>
