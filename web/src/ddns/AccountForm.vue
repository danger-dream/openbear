<script setup>
import { computed, reactive, ref, watch } from 'vue';
import { ChevronRight } from '@lucide/vue';
import { Api } from '../api.js';
import { noAutofill } from '../servers/serverHelpers.js';
import { DdnsApi } from './ddnsApi.js';
import { accountBody, checkText, choice, ddnsSecrets, emptyCredentials, errorMessage, guessMap, legacyProxy, missingFields, providerGroups, providerMark, providerOf, TTLS } from './ddnsHelpers.js';

// 添加服务商, and a provider account's settings (account given): pick the provider, then its credentials —
// typed in, or a ddns/ entry already in the secret store with its keys mapped to the provider's fields.
const props = defineProps({providers: {type: Array, required: true}, account: {type: Object, default: null}, records: {type: Number, default: 0}});
const emit = defineEmits(['saved', 'removed', 'cancel']);
const edit = computed(() => Boolean(props.account));
const query = ref(''), type = ref(props.account?.type || '');
const form = reactive({alias: props.account?.alias || '', mode: props.account ? 'keep' : 'fill', credentials: {}, secretId: '', map: {},
  settings: {endpoint: props.account?.settings?.endpoint || '', proxy: props.account?.settings?.proxy || '', ttl: props.account?.settings?.ttl ?? null}});
const secrets = ref(null), secretKeys = ref([]), secretError = ref('');
const busy = ref(''), result = ref(null), error = ref(''), removing = ref(false), force = ref(false);
const advanced = ref(Boolean(props.account?.settings?.endpoint || props.account?.settings?.proxy || props.account?.settings?.ttl));

const groups = computed(() => providerGroups(props.providers, query.value));
const provider = computed(() => type.value ? providerOf(props.providers, type.value) : null);
const missing = computed(() => provider.value ? missingFields(provider.value, form) : ['服务商']);
const fieldId = key => `dd-cred-${key}`;
if (provider.value) form.credentials = emptyCredentials(provider.value);

function pick(next) {
  type.value = next;
  const p = providerOf(props.providers, next);
  form.credentials = emptyCredentials(p);
  if (!edit.value) form.alias = p.name;
  form.map = guessMap(p.fields, secretKeys.value);
  result.value = null; error.value = '';
}
function change() { type.value = ''; result.value = null; error.value = ''; }
async function loadSecrets() {
  if (secrets.value) return;
  try { secrets.value = ddnsSecrets((await Api.secrets(false)).items); }
  catch { secrets.value = []; secretError.value = '读不到凭据库'; }
}
watch(() => form.mode, mode => { if (mode === 'vault') void loadSecrets(); result.value = null; });
// Only the key names are used; the values stay in the store.
watch(() => form.secretId, async id => {
  secretKeys.value = [];
  if (!id) return;
  try {
    const data = await Api.secret(id);
    secretKeys.value = (data.item?.kv || []).map(x => String(x?.key || '')).filter(Boolean);
  } catch { secretError.value = '读不到这条凭据'; }
  form.map = guessMap(provider.value?.fields || [], secretKeys.value);
});

const body = () => accountBody({type: type.value, ...form}, provider.value);
async function test() {
  busy.value = 'test'; error.value = ''; result.value = null;
  try {
    const b = body();
    // Kept credentials are tried with the settings as edited here, not as saved.
    const data = edit.value && form.mode === 'keep' ? await DdnsApi.checkAccount(props.account.id, b.settings)
      : await DdnsApi.testAccount({type: b.type, credentials: b.credentials, secretRef: b.secretRef, settings: b.settings});
    result.value = checkText(data);
  } catch (e) { result.value = ['bad', errorMessage(e, '连接失败')]; }
  finally { busy.value = ''; }
}
async function save() {
  busy.value = 'save'; error.value = '';
  try {
    if (edit.value) {
      const {alias, settings, credentials, secretRef} = body();
      const data = await DdnsApi.updateAccount(props.account.id, {alias, settings, ...(credentials ? {credentials} : {}), ...(secretRef ? {secretRef} : {})});
      emit('saved', {account: data.account, message: '已保存'});
    } else {
      const data = await DdnsApi.addAccount(body());
      emit('saved', {account: data.account, message: `已添加服务商 ${provider.value.name}`});
    }
  } catch (e) { error.value = errorMessage(e, '保存失败'); }
  finally { busy.value = ''; }
}
async function remove() {
  busy.value = 'remove'; error.value = '';
  try {
    await DdnsApi.deleteAccount(props.account.id, force.value);
    emit('removed', {message: '已删除服务商账户'});
  } catch (e) {
    if (e?.response?.data?.error === 'account_in_use') { removing.value = true; error.value = '这个账户下还有解析，勾选下面的选项后才能删除'; }
    else error.value = errorMessage(e, '删除失败');
  } finally { busy.value = ''; }
}
defineExpose({busy});
</script>
<template>
  <div class="instance-form dd-form">
    <template v-if="!provider">
      <input v-model="query" v-bind="noAutofill" type="search" class="dd-provider-search" placeholder="搜索服务商" aria-label="搜索服务商" />
      <div class="dd-pgroups">
        <section v-for="g in groups" :key="g.tier" class="dd-pgroup">
          <h5>{{ g.tier }} 档 · {{ g.label }}</h5>
          <div class="dd-pgrid">
            <button v-for="p in g.items" :key="p.type" type="button" class="dd-pcard" :data-type="p.type" @click="pick(p.type)">
              <span class="dd-logo" :style="{background: providerMark(p).color}" aria-hidden="true">{{ providerMark(p).text }}</span><span class="ix-ellipsis">{{ p.name }}</span>
            </button>
          </div>
        </section>
        <p v-if="!groups.length" class="instance-empty">{{ providers.length ? '没有匹配的服务商' : '主控没有返回服务商列表' }}</p>
      </div>
    </template>
    <template v-else>
      <div class="dd-picked">
        <span class="dd-logo" :style="{background: providerMark(provider).color}" aria-hidden="true">{{ providerMark(provider).text }}</span>
        <b>{{ provider.name }}</b>
        <button v-if="!edit" type="button" class="ix-text-button" @click="change">更换</button>
        <span class="ob-grow" />
        <a v-if="provider.docs" class="dd-link" :href="provider.docs" target="_blank" rel="noopener noreferrer">如何创建最小权限凭据 ↗</a>
      </div>
      <label class="dd-field"><span>名称</span>
        <input v-model="form.alias" v-bind="noAutofill" placeholder="用来区分同一家服务商的多个账户" /></label>

      <div v-if="edit" class="dd-field"><span>凭据</span>
        <div class="dd-choices" role="radiogroup" aria-label="凭据">
          <label><input v-model="form.mode" type="radio" value="keep" />保持现有{{ account.secret?.name ? `（${account.secret.name}）` : '' }}</label>
          <label><input v-model="form.mode" type="radio" value="fill" />重新填写</label>
          <label><input v-model="form.mode" type="radio" value="vault" />使用已保存的动态域名凭据</label>
        </div>
      </div>
      <label v-else class="ob-check dd-vault-switch"><input type="checkbox" :checked="form.mode === 'vault'" @change="form.mode = $event.target.checked ? 'vault' : 'fill'" />
        <span>使用已保存的动态域名凭据<small class="instance-muted">不勾选时，填写的凭据会存进凭据库，命名为 ddns/{{ provider.name }}-{{ form.alias || provider.name }}</small></span></label>

      <template v-if="form.mode === 'fill'">
        <div v-for="f in provider.fields" :key="f.key" class="dd-field">
          <label v-if="f.kind === 'bool'" class="ob-check"><input v-model="form.credentials[f.key]" type="checkbox" /><span>{{ f.label }}<small v-if="f.hint" class="instance-muted">{{ f.hint }}</small></span></label>
          <template v-else>
            <label :for="fieldId(f.key)">{{ f.label }}{{ f.optional ? '（可选）' : '' }}</label>
            <select v-if="f.kind === 'select'" :id="fieldId(f.key)" v-model="form.credentials[f.key]">
              <option v-for="o in (f.options || []).map(choice)" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <textarea v-else-if="f.kind === 'textarea'" :id="fieldId(f.key)" v-model="form.credentials[f.key]" v-bind="noAutofill" class="mono" rows="3" spellcheck="false" autocapitalize="off"></textarea>
            <input v-else-if="f.kind === 'number'" :id="fieldId(f.key)" v-model="form.credentials[f.key]" v-bind="noAutofill" type="number" inputmode="numeric" />
            <input v-else :id="fieldId(f.key)" v-model="form.credentials[f.key]" v-bind="noAutofill" :type="f.kind === 'password' ? 'password' : 'text'"
              :autocomplete="f.kind === 'password' ? 'new-password' : 'off'" autocapitalize="off" spellcheck="false" :class="{mono: f.kind === 'password'}" />
            <small v-if="f.hint" class="instance-muted">{{ f.hint }}</small>
          </template>
        </div>
      </template>
      <template v-else-if="form.mode === 'vault'">
        <label class="dd-field"><span>凭据条目</span>
          <select v-model="form.secretId" :disabled="!secrets">
            <option value="">{{ !secrets ? '正在读取凭据库…' : secrets.length ? '选择凭据条目' : '还没有已保存的动态域名凭据' }}</option>
            <option v-for="s in secrets || []" :key="s.id" :value="String(s.id)">{{ s.name }}</option>
          </select>
          <small v-if="secretError" class="instance-error">{{ secretError }}</small>
          <small v-else class="instance-muted">只列出动态域名保存的凭据（ddns/ 开头），凭据库里其他条目不能引用</small></label>
        <div v-if="form.secretId" class="dd-map">
          <span class="instance-muted">字段对应（左边是 {{ provider.name }} 需要的，右边是凭据条目里的键）</span>
          <label v-for="f in provider.fields" :key="f.key" class="dd-map-row"><span>{{ f.label }}{{ f.optional ? '（可选）' : '' }}</span>
            <select v-model="form.map[f.key]" :aria-label="`${f.label} 对应的键`">
              <option value="">{{ f.optional || f.kind === 'bool' ? '不使用' : '选择键' }}</option>
              <option v-for="k in secretKeys" :key="k" :value="k">{{ k }}</option>
            </select></label>
        </div>
      </template>

      <div v-if="result" class="dd-note" :data-tone="result[0]" role="status">{{ result[1] }}</div>

      <details class="dd-adv" :open="advanced" @toggle="advanced = $event.target.open">
        <summary><ChevronRight :size="15" class="dd-adv-chev" aria-hidden="true" />高级选项<span class="instance-muted">（API 端点、代理、默认 TTL）</span></summary>
        <div class="dd-adv-grid">
          <label class="dd-field dd-full"><span>API 端点</span><input v-model="form.settings.endpoint" v-bind="noAutofill" class="mono" placeholder="默认使用官方地址" autocapitalize="off" /></label>
          <label class="dd-field"><span>代理</span><input v-model="form.settings.proxy" v-bind="noAutofill" class="mono" placeholder="例如 http://127.0.0.1:7890" autocapitalize="off" />
            <small v-if="legacyProxy(form.settings.proxy)" class="instance-error">只支持 http 和 https 代理，不再支持 socks；改成 http(s) 代理或清空后才能保存和同步</small></label>
          <label class="dd-field"><span>默认 TTL</span><select v-model="form.settings.ttl"><option v-for="[v, l] in TTLS" :key="String(v)" :value="v">{{ l }}</option></select></label>
        </div>
      </details>

      <div v-if="edit && removing" class="dd-note" data-tone="bad">
        <p>删除后，OpenBear 不再管理这个账户{{ records ? `下的 ${records} 条解析` : '' }}；服务商上的记录保持原样。</p>
        <label v-if="records" class="ob-check"><input v-model="force" type="checkbox" /><span>同时停止管理这 {{ records }} 条解析</span></label>
      </div>
    </template>

    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer dd-foot">
      <template v-if="edit">
        <el-button v-if="!removing" type="danger" plain @click="removing = true">删除账户</el-button>
        <el-button v-else type="danger" :loading="busy === 'remove'" :disabled="records > 0 && !force" @click="remove">确认删除</el-button>
      </template>
      <span v-if="provider && missing.length" class="dd-save-note">还需填写：{{ missing.join('、') }}</span>
      <span v-else class="ob-grow" />
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button :disabled="!provider || missing.length > 0 || Boolean(busy)" :loading="busy === 'test'" @click="test">测试连接</el-button>
      <el-button type="primary" :disabled="!provider || missing.length > 0 || Boolean(busy)" :loading="busy === 'save'" data-testid="dd-account-save" @click="save">保存</el-button>
    </div>
  </div>
</template>
