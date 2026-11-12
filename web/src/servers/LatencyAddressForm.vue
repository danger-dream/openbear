<script setup>
import { reactive, ref, watch } from 'vue';
import { noAutofill } from './serverHelpers.js';
import { checkAddress } from './latencyTargets.js';

// One of the user's own 延迟 addresses, added or changed: its name, how it is probed (the settings' way,
// ICMP, a TCPing to a port, or the time an HTTP(S) request takes to be answered), where to, and with
// askGroup whether it is in China or abroad. Emits save with what the settings keep (no id). Not a form of its
// own, since it sits in others: Enter in a box saves it, not theirs.
const props = defineProps({item: {type: Object, default: null}, group: {type: String, default: 'intl'}, askGroup: Boolean, submit: {type: String, default: '添加'}});
const emit = defineEmits(['save', 'cancel']);
const MODES = [['', '跟随设置'], ['icmp', 'ICMP'], ['tcp', 'TCP'], ['http', 'HTTP']];
const draft = reactive({label: '', mode: '', host: '', port: '443', url: '', group: 'intl'});
const error = ref('');
watch(() => props.item, item => {
  Object.assign(draft, {label: item?.label || '', mode: item?.mode || '', host: item && item.mode !== 'http' ? item.host : '',
    port: item?.port ? String(item.port) : '443', url: item?.url || '', group: item?.group || props.group});
  error.value = '';
}, {immediate: true});
function hostTyped() {
  // host:port pasted into the host box: the port goes to its own.
  const m = /^([^:[\]]+|\[[^\]]+\]):(\d{1,5})$/.exec(draft.host.trim());
  if (m && draft.mode !== 'icmp') Object.assign(draft, {host: m[1], port: m[2]});
}
function save() {
  const {error: wrong, value} = checkAddress(draft);
  error.value = wrong;
  if (!wrong) emit('save', {...value, group: draft.group});
}
</script>
<template>
  <div class="sv-addr-form" role="group" :aria-label="item ? `修改 ${item.label}` : '添加地址'" @keydown.enter.prevent="save">
    <input v-model="draft.label" v-bind="noAutofill" class="sv-addr-name" maxlength="24" aria-label="名称" placeholder="名称" />
    <select v-model="draft.mode" class="sv-addr-mode" aria-label="协议"><option v-for="[value, name] in MODES" :key="value" :value="value">{{ name }}</option></select>
    <select v-if="askGroup" v-model="draft.group" class="sv-addr-mode" aria-label="位置"><option value="cn">国内</option><option value="intl">国外</option></select>
    <input v-if="draft.mode === 'http'" v-model="draft.url" v-bind="noAutofill" class="sv-addr-where" maxlength="2048" inputmode="url" aria-label="网址" placeholder="https://example.com/health" />
    <template v-else>
      <input v-model="draft.host" v-bind="noAutofill" class="sv-addr-where" maxlength="270" aria-label="主机" placeholder="主机，例如 nas.example.com" @blur="hostTyped" />
      <input v-if="draft.mode !== 'icmp'" v-model="draft.port" v-bind="noAutofill" class="sv-lib-port" type="number" inputmode="numeric" min="1" max="65535" aria-label="端口" placeholder="端口" />
    </template>
    <span class="sv-lib-edit-acts"><button type="button" class="ix-text-button" @click="emit('cancel')">取消</button><button type="button" class="ix-text-button" data-primary @click="save">{{ submit }}</button></span>
    <small v-if="error" class="ob-bad" role="alert">{{ error }}</small>
    <small v-else-if="draft.mode === ''" class="instance-muted">跟随设置：按「延迟」里选的 TCPing 或 ICMP 测。</small>
    <small v-else-if="draft.mode === 'http'" class="instance-muted">测从发出请求到收到响应头的时间，不跟随跳转。</small>
  </div>
</template>
