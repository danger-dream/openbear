<script setup>
import { reactive, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import { noAutofill, safeError } from '../serverHelpers.js';
import DdnsSheet from '../../ddns/DdnsSheet.vue';

// 校准: this period's traffic as the provider's panel shows it, used or (with a limit) remaining.
const props = defineProps({modelValue: Boolean, server: {type: Object, required: true}, phone: Boolean});
const emit = defineEmits(['update:modelValue', 'changed']);
const UNITS = {GB: 1024 ** 3, TB: 1024 ** 4};
const calib = reactive({kind: 'used', amount: '', unit: 'GB', error: '', busy: false});
function fill() {
  const t = props.server.traffic, value = calib.kind === 'used' ? t.used : Math.max(0, t.limit - t.used);
  calib.amount = String(Math.round(value / UNITS[calib.unit] * 100) / 100);
}
watch(() => props.modelValue, open => { if (open && props.server.traffic) { Object.assign(calib, {kind: 'used', unit: 'GB', error: ''}); fill(); } }, {immediate: true});
async function save() {
  const value = Number(calib.amount);
  if (calib.amount === '' || !(value >= 0)) { calib.error = '请填写流量数值。'; return; }
  calib.busy = true; calib.error = '';
  try {
    ok(await Api.calibrateServerTraffic(props.server.id, {[calib.kind]: Math.round(value * UNITS[calib.unit])}));
    emit('update:modelValue', false);
    emit('changed');
    ElMessage.success('已校准本期流量');
  } catch (e) { calib.error = safeError(e); }
  finally { calib.busy = false; }
}
</script>
<template>
  <DdnsSheet :model-value="modelValue" :phone="phone" width="420px" title="校准本期流量" :sub="server.name" :busy="calib.busy" form @update:model-value="v => emit('update:modelValue', v)">
    <form v-if="server.traffic" class="sv-calibrate" autocomplete="off" @submit.prevent="save">
      <div class="ob-inline">
        <select v-if="server.traffic.limit" v-model="calib.kind" class="sv-calibrate-kind" aria-label="校准方式" @change="fill"><option value="used">已用</option><option value="remaining">剩余</option></select>
        <span v-else class="sv-calibrate-kind">已用</span>
        <input v-model="calib.amount" v-bind="noAutofill" type="number" inputmode="decimal" min="0" step="any" :aria-label="calib.kind === 'used' ? '本期已用' : '本期剩余'" />
        <select v-model="calib.unit" class="sv-unit" aria-label="单位"><option>GB</option><option>TB</option></select>
      </div>
      <small>填服务商面板上的数值，之后在它上面继续累加节点统计的用量；下个周期从零开始。</small>
      <p v-if="calib.error" role="alert" class="instance-error">{{ calib.error }}</p>
      <div class="sv-calibrate-actions">
        <el-button :disabled="calib.busy" @click="emit('update:modelValue', false)">取消</el-button>
        <el-button type="primary" native-type="submit" :loading="calib.busy">校准</el-button>
      </div>
    </form>
  </DdnsSheet>
</template>
