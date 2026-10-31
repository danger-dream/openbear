<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { Api } from '../api.js';
import { ok, safeError } from './instanceHelpers.js';

// How to log in over SSH: a key from the vault, a password, or an uploaded key.
const props = defineProps({modelValue: {type: String, default: 'key'}, editing: {type: Object, default: null}});
const emit = defineEmits(['update:modelValue', 'error']);
const keys = ref([]), keyChoice = ref(''), passField = ref(''), fileName = ref('');
const password = ref(''), privateKey = ref(''), saveKey = ref(true);

const auth = computed({get: () => props.modelValue, set: value => emit('update:modelValue', value)});
const usable = computed(() => keys.value.filter(k => k.usable && !k.likelyTls));
const tls = computed(() => keys.value.filter(k => k.usable && k.likelyTls));
const unusable = computed(() => keys.value.filter(k => !k.usable));
const groups = computed(() => {
  const out = {};
  for (const key of usable.value) (out[key.secret] ||= []).push(key);
  return out;
});
const selected = computed(() => keys.value.find(k => keyId(k) === keyChoice.value));
const keyId = key => `${key.secret}\u0000${key.field}`;
const keyLabel = key => [key.field, key.type, key.encrypted ? '有密码' : ''].filter(Boolean).join(' · ');
const reason = key => ({putty_format: 'PuTTY 格式，需先转成 OpenSSH 格式', unsupported_key: '无法解析'})[key.reason] || '无法使用';

async function loadKeys() {
  try {
    keys.value = ok(await Api.instanceSshKeys()).items || [];
    const preferred = usable.value[0] || tls.value[0];
    if (!keyChoice.value && preferred) choose(keyId(preferred));
  } catch (e) { emit('error', safeError(e)); }
}
function choose(value) {
  keyChoice.value = value;
  passField.value = selected.value?.suggestedPassphraseField || selected.value?.passphraseFields?.[0] || '';
}
async function readKey(event) {
  const file = event.target.files?.[0];
  event.target.value = '';
  privateKey.value = ''; fileName.value = '';
  if (!file) return;
  if (file.size > 32768) { emit('error', '私钥文件不能超过 32 KiB。'); return; }
  privateKey.value = await file.text();
  fileName.value = file.name;
}
function credentials() {
  if (auth.value === 'key') {
    const key = selected.value;
    if (!key) return {keyRef: null};
    return {keyRef: {secret: key.secret, field: key.field, ...(key.encrypted && passField.value ? {passphraseField: passField.value} : {})}};
  }
  if (auth.value === 'password') return password.value ? {password: password.value} : {};
  return privateKey.value ? {privateKey: privateKey.value, saveKey: saveKey.value} : {};
}
function clear() { password.value = ''; privateKey.value = ''; }
defineExpose({credentials, clear});
onMounted(loadKeys);
onBeforeUnmount(clear);
</script>
<template>
  <div class="ob-field"><span>认证</span>
    <div class="ob-ctl">
      <div class="ob-radios" role="radiogroup" aria-label="认证方式">
        <label><input v-model="auth" type="radio" value="key" />密钥库中的私钥</label>
        <label><input v-model="auth" type="radio" value="password" />密码</label>
        <label><input v-model="auth" type="radio" value="upload" />上传私钥</label>
      </div>
      <template v-if="auth === 'key'">
        <select :value="keyChoice" aria-label="密钥库中的 SSH 私钥" @change="choose($event.target.value)">
          <option v-if="!keys.length" value="" disabled>密钥库中没有找到 SSH 私钥</option>
          <optgroup v-for="(items, secret) in groups" :key="secret" :label="secret">
            <option v-for="key in items" :key="keyId(key)" :value="keyId(key)">{{ keyLabel(key) }}</option>
          </optgroup>
          <optgroup v-if="tls.length" label="可能不是 SSH 私钥">
            <option v-for="key in tls" :key="keyId(key)" :value="keyId(key)">{{ key.secret }} · {{ keyLabel(key) }} · 同一凭证里有证书，像是网站 HTTPS 私钥</option>
          </optgroup>
          <optgroup v-if="unusable.length" label="无法使用">
            <option v-for="key in unusable" :key="keyId(key)" :value="keyId(key)" disabled>{{ key.secret }} · {{ key.field }} · {{ reason(key) }}</option>
          </optgroup>
        </select>
        <small v-if="editing && editing.auth === 'key' && !keyChoice">当前：{{ editing.keyLabel }}</small>
        <small v-if="selected?.fingerprint" class="mono">指纹 {{ selected.type }} · {{ selected.fingerprint }}{{ selected.comment ? ' · ' + selected.comment : '' }}</small>
        <div v-if="selected?.encrypted" class="ob-inline">
          <select v-model="passField" aria-label="私钥密码所在字段">
            <option v-for="name in selected.passphraseFields" :key="name" :value="name">{{ name }}</option>
          </select>
          <small :class="selected.suggestedPassphraseField === passField ? 'ob-ok' : ''">{{ selected.suggestedPassphraseField === passField ? '✓ 这个字段能解开私钥' : '私钥密码所在字段，保存时验证' }}</small>
        </div>
      </template>
      <input v-else-if="auth === 'password'" v-model="password" type="password" autocomplete="new-password" maxlength="8192" :placeholder="editing?.auth === 'password' ? '已保存，留空保留' : 'SSH 密码'" aria-label="SSH 密码" />
      <template v-else>
        <input type="file" aria-label="私钥文件" @change="readKey" />
        <small v-if="fileName">已选：{{ fileName }}</small>
        <label class="ob-check"><input v-model="saveKey" type="checkbox" />同时保存到密钥库，其他服务器可以直接选</label>
      </template>
    </div>
  </div>
</template>
