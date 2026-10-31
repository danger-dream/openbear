<script setup>
import { computed, reactive, ref, watch } from 'vue';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import AccessPicker from './AccessPicker.vue';
import SuggestInput from './SuggestInput.vue';
import { Close } from '@element-plus/icons-vue';
import RegionPicker from './RegionPicker.vue';
import { billingResetDay, cycleLabel, cycleOptions, cycleStart, noAutofill, safeError, trafficMode, trafficModes, trafficTypes } from './serverHelpers.js';

const props = defineProps({server: {type: Object, default: null}, groups: {type: Array, default: () => []}, tags: {type: Array, default: () => []}, regions: {type: Array, default: () => []},
  settings: {type: Object, default: () => ({accessPoints: []})}});
const emit = defineEmits(['saved', 'cancel']);
const GB = 1024 ** 3, TB = 1024 ** 4;
const CURRENCIES = ['$', '¥', '€', '£', 'HK$'];
const p = props.server?.profile || {}, b = p.billing || {}, t = p.traffic || {};
const limit = t.limit || 0;
const form = reactive({
  name: props.server?.name || '', group: p.group || '', tags: [...(p.tags || [])], provider: p.provider || '', providerUrl: p.providerUrl || '', region: p.region || '', remark: p.remark || '',
  price: b.price || '', currency: b.currency || '$', cycle: b.cycle ?? 30, startedAt: b.startedAt || '', expiresAt: b.expiresAt || '', autoRenew: !!b.autoRenew,
  limit: limit ? (limit % TB === 0 ? limit / TB : Math.round(limit / GB * 100) / 100) : '', unit: limit && limit % TB === 0 ? 'TB' : 'GB',
  // Traffic not set up yet counts both directions; one set up before keeps what it counted (the larger one when unset).
  mode: trafficMode(t), type: t.type || (Object.keys(t).length ? 'max' : 'sum'), resetDay: t.resetDay || '', unitPrice: t.price || '',
  access: {primary: p.access?.primary || '', backup: p.access?.backup || ''},
});
const local = computed(() => Boolean(props.server?.local));
const limited = computed(() => form.mode === 'billing' || form.mode === 'fixed');
const modeHint = computed(() => {
  const day = billingResetDay({expiresAt: form.expiresAt, startedAt: form.startedAt});
  const from = form.expiresAt ? '按到期日推算' : form.startedAt ? '按开始日期推算' : '填写到期或开始日期后按它推算';
  return {billing: `每月 ${day} 号重置，${from}；年付、季付等也是每月重置一次。`, fixed: '每月这一天开始新的流量周期。',
    metered: `本期用量乘以单价，每月 ${day} 号结算，${from}。`, unlimited: `只统计用量（每月 ${day} 号重新计算），不设额度，也不发流量提醒。`}[form.mode];
});
// Refusals of a node that is online and kept its old address; the edit can still be saved for later.
const SWITCH_REFUSALS = new Set(['new_url_unreachable', 'invalid_url', 'config_write_failed']);
const busy = ref(false), error = ref(''), refused = ref(false);
defineExpose({busy});
const detected = computed(() => props.server?.node?.geo?.country || '');
const cycles = computed(() => cycleOptions.some(([v]) => v === Number(form.cycle)) ? cycleOptions : [...cycleOptions, [Number(form.cycle), cycleLabel(Number(form.cycle))]]);
// A start date left empty follows the expiry and the cycle (one cycle before); typing one stops that.
const startAuto = ref(!b.startedAt);
watch(() => [form.expiresAt, form.cycle], () => { if (startAuto.value) form.startedAt = cycleStart(form.expiresAt, Number(form.cycle)); }, {immediate: true});
function startTyped() { startAuto.value = !form.startedAt; }
// Tags: Enter, a comma or leaving the field adds what was typed.
const tagDraft = ref('');
const tagChoices = computed(() => props.tags.filter(tag => !form.tags.includes(tag)));
function addTag() {
  for (const tag of tagDraft.value.split(/[,，]/).map(v => v.trim()).filter(Boolean)) {
    if (!form.tags.includes(tag) && form.tags.length < 10) form.tags.push(tag.slice(0, 20));
  }
  tagDraft.value = '';
}
function tagKey(event) { if (event.key === ',' || event.key === '，') { event.preventDefault(); addTag(); } }
function removeTag(tag) { form.tags = form.tags.filter(t => t !== tag); }

function payload() {
  const amount = Number(form.limit) || 0;
  return {name: form.name.trim(), profile: {
    group: form.group.trim(), tags: form.tags, provider: form.provider.trim(), providerUrl: form.providerUrl.trim(), region: form.region.trim().toUpperCase(), remark: form.remark.trim(),
    billing: {price: Number(form.price) || 0, currency: form.currency.trim(), cycle: Number(form.cycle), startedAt: form.startedAt, expiresAt: form.expiresAt, autoRenew: form.autoRenew},
    traffic: {mode: form.mode, type: form.type, limit: limited.value ? Math.round(amount * (form.unit === 'TB' ? TB : GB)) : 0,
      resetDay: form.mode === 'fixed' ? Number(form.resetDay) || 0 : 0, price: form.mode === 'metered' ? Number(form.unitPrice) || 0 : 0},
    ...(local.value ? {} : {access: {...form.access}}),
  }};
}
async function save(later = false) {
  if (busy.value) return;
  addTag();
  error.value = ''; refused.value = false; busy.value = true;
  try {
    const data = later ? {...payload(), accessApply: 'later'} : payload();
    const result = ok(await (props.server ? Api.updateServer(props.server.id, data) : Api.createServer(data)));
    emit('saved', result.item, result.accessPush, result.accessPending);
  } catch (e) {
    refused.value = Boolean(props.server) && SWITCH_REFUSALS.has(e?.response?.data?.error);
    error.value = refused.value ? `${safeError(e)}这次的修改都没有保存。` : safeError(e);
  } finally { busy.value = false; }
}
</script>
<template>
  <form class="instance-form" autocomplete="off" @submit.prevent="save()">
    <div class="ob-fields" :class="{busy}">
      <div class="ob-field"><span>名称</span>
        <div class="ob-inline">
          <input v-model="form.name" v-bind="noAutofill" required maxlength="60" placeholder="例如：Tokyo-01" aria-label="服务器名称" />
          <RegionPicker v-model="form.region" :detected="detected" :used="regions" />
        </div>
      </div>
      <div class="ob-field"><span>分组</span>
        <div class="ob-ctl"><SuggestInput v-model="form.group" :options="groups" maxlength="40" placeholder="可不填" aria-label="分组" /></div>
      </div>
      <div class="ob-field"><span>标签</span>
        <div class="ob-ctl">
          <div class="sv-chips-edit sv-tags-edit">
            <span v-for="tag in form.tags" :key="tag" class="sv-chip">{{ tag }}<button type="button" :aria-label="`删除标签 ${tag}`" @click="removeTag(tag)"><el-icon><Close /></el-icon></button></span>
            <input v-if="form.tags.length < 10" v-model="tagDraft" v-bind="noAutofill" list="sv-tag-choices" maxlength="40" :placeholder="form.tags.length ? '再加一个' : '例如：独服、建站专用'" aria-label="添加标签"
              @keydown.enter.prevent="addTag" @keydown="tagKey" @blur="addTag" />
            <datalist id="sv-tag-choices"><option v-for="tag in tagChoices" :key="tag" :value="tag" /></datalist>
          </div>
          <small>分组之外的标记，可以加多个；回车或逗号添加。</small>
        </div>
      </div>
      <div class="ob-field"><span>商家</span>
        <div class="ob-inline sv-merchant">
          <input v-model="form.provider" v-bind="noAutofill" maxlength="60" placeholder="商家名称，可不填" aria-label="商家" />
          <input v-model="form.providerUrl" v-bind="noAutofill" type="url" inputmode="url" maxlength="300" placeholder="续费网址，例如 https://…" aria-label="商家网址" />
        </div>
      </div>
      <div v-if="!local && settings.accessPoints?.length" class="ob-field"><span>接入点</span>
        <div class="ob-ctl"><AccessPicker v-model="form.access" :points="settings.accessPoints" :default-id="settings.defaultAccess" :mesh="settings.meshAccess || null" :mesh-eligible="Boolean(server?.access?.meshEligible)" :server-mesh-reason="server?.access?.meshReason || ''" /><small>节点连回 OpenBear 用的地址。修改后，在线的节点会先确认能连上新地址再切换。</small></div>
      </div>
      <fieldset>
        <legend>账单</legend>
        <div class="ob-fields">
          <div class="ob-field"><span>价格</span>
            <div class="ob-inline">
              <SuggestInput v-model="form.currency" :options="CURRENCIES" class="sv-currency" maxlength="8" aria-label="币种" />
              <input v-bind="noAutofill" v-model="form.price" type="number" inputmode="decimal" min="0" step="0.01" placeholder="0" aria-label="价格" />
              <select v-model.number="form.cycle" aria-label="计费周期"><option v-for="[value, label] in cycles" :key="value" :value="value">{{ label }}</option></select>
            </div>
          </div>
          <div class="ob-field"><span>日期</span>
            <div class="ob-inline sv-dates">
              <label>开始<input v-bind="noAutofill" v-model="form.startedAt" type="date" aria-label="开始日期" @input="startTyped" /></label>
              <label>到期<input v-bind="noAutofill" v-model="form.expiresAt" type="date" :disabled="form.cycle === -1" aria-label="到期日期" /></label>
            </div>
          </div>
          <div v-if="startAuto && form.startedAt" class="ob-field"><span></span><small class="instance-muted sv-start-auto">开始日期按到期日往前推一个周期自动填写，可以改。</small></div>
          <div class="ob-field"><span></span><label class="ob-check"><input v-model="form.autoRenew" type="checkbox" :disabled="form.cycle <= 0" />自动续费：到期后把到期日顺延一个周期</label></div>
        </div>
      </fieldset>
      <fieldset>
        <legend>流量</legend>
        <div class="ob-fields">
          <div class="ob-field"><span>方式</span>
            <div class="ob-ctl">
              <div class="ob-radios sv-radios" role="radiogroup" aria-label="流量方式">
                <label v-for="[value, label] in trafficModes" :key="value"><input v-model="form.mode" type="radio" name="sv-traffic-mode" :value="value" />{{ label }}</label>
              </div>
              <small>{{ modeHint }}</small>
            </div>
          </div>
          <div v-if="form.mode !== 'unlimited'" class="ob-field"><span>计费</span>
            <div class="ob-ctl">
              <div class="ob-radios sv-radios" role="radiogroup" aria-label="计算方式">
                <label v-for="[value, label, hint] in trafficTypes" :key="value" :title="hint"><input v-model="form.type" type="radio" name="sv-traffic-type" :value="value" />{{ label }}<small v-if="hint && value !== 'sum'" class="instance-muted">{{ hint }}</small></label>
              </div>
            </div>
          </div>
          <div v-if="form.mode === 'fixed'" class="ob-field"><span>重置日</span>
            <div class="ob-inline sv-reset"><small>每月</small><input v-model="form.resetDay" v-bind="noAutofill" class="sv-day" type="number" inputmode="numeric" min="1" max="31" required placeholder="1" aria-label="每月重置日" /><small>号</small></div>
          </div>
          <div v-if="limited" class="ob-field"><span>额度</span>
            <div class="ob-inline">
              <input v-model="form.limit" v-bind="noAutofill" type="number" inputmode="decimal" min="0" step="any" placeholder="不填只统计用量" aria-label="流量额度" />
              <select v-model="form.unit" class="sv-unit" aria-label="单位"><option>GB</option><option>TB</option></select>
            </div>
          </div>
          <div v-if="form.mode === 'metered'" class="ob-field"><span>单价</span>
            <div class="ob-inline">
              <input v-model="form.unitPrice" v-bind="noAutofill" type="number" inputmode="decimal" min="0" step="any" placeholder="0.075" aria-label="每 GB 单价" />
              <small class="sv-unit-label">{{ form.currency || '' }}/GB</small>
            </div>
          </div>
        </div>
      </fieldset>
      <label class="ob-field"><span>备注</span><textarea v-model="form.remark" rows="2" maxlength="2000" aria-label="备注" /></label>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}<button v-if="refused" type="button" class="ix-text-button" :disabled="busy" @click="save(true)">只保存，节点下次连上时再切换</button></p>
    <div class="instance-form-footer">
      <span v-if="!server" class="ob-hint">保存后在详情页安装节点，开始上报运行状态。</span>
      <el-button :disabled="busy" @click="emit('cancel')">取消</el-button>
      <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
    </div>
  </form>
</template>
