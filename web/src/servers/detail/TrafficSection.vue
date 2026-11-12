<script setup>
import { Histogram } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import TrendChart from './TrendChart.vue';
import { COLORS } from './detailHelpers.js';
import { every, useVisible } from './useVisible.js';
import { bytes, dateTime, projectedTraffic, safeError, trafficTypeText } from '../serverHelpers.js';

// 流量统计: this period first (what the cards cannot hold: projection, cost, calibration; with the ways to
// calibrate it and to its settings), then traffic per hour, day or month, the last one still counting.
// Read when scrolled to; refreshed each minute while in view.
const props = defineProps({server: {type: Object, required: true}, active: Boolean, now: {type: Number, required: true}, timezone: {type: String, default: ''}, phone: Boolean, busy: Boolean});
const emit = defineEmits(['changed', 'calibrate', 'settings']);
const UNITS = [['hour', '按小时'], ['day', '按日'], ['month', '按月']];
const KINDS = [['bar', '柱形'], ['line', '折线']];
const SPANS = {hour: '最近 48 小时', day: '最近 31 天', month: '最近 12 个月'};
const el = ref(null), unit = ref('day'), kind = ref('bar'), data = ref(null), error = ref(''), clearing = ref(false);
const {visible, seen} = useVisible(el);
let alive = true, mine = 0;

async function load() {
  const id = ++mine, want = unit.value;
  try {
    const result = ok(await Api.serverTrafficStats(props.server.id, want));
    if (alive && id === mine) { data.value = result; error.value = ''; }
  } catch (e) { if (alive && id === mine) error.value = safeError(e); }
}
watch(() => seen.value && props.active, on => { if (on) void load(); }, {immediate: true});
watch(unit, () => { data.value = null; if (seen.value) void load(); });
const stop = every(60000, () => props.active && visible.value, load);
onBeforeUnmount(() => { alive = false; stop(); });

const points = computed(() => data.value?.unit === unit.value ? data.value.points : []);
const t = computed(() => points.value.map(p => p.t));
const series = computed(() => [{key: 'rx', label: '下行', color: COLORS.rx, values: points.value.map(p => p.rx)},
  {key: 'tx', label: '上行', color: COLORS.tx, values: points.value.map(p => p.tx)}]);
const step = computed(() => ({hour: 3600, day: 86400, month: 86400 * 30})[unit.value]);
// Days and months are already bucketed in the servers' time zone by the backend.
const hourFormat = computed(() => new Intl.DateTimeFormat('en-US', {timeZone: props.timezone || undefined,
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}));
function when(ts, i) {
  const last = i === points.value.length - 1;
  let name = points.value[i]?.label || '';
  if (unit.value === 'hour') {
    const parts = Object.fromEntries(hourFormat.value.formatToParts(new Date(ts * 1000)).map(p => [p.type, p.value]));
    name = `${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`;
  } else {
    const [year, month, day] = name.split('-');
    if (month) name = unit.value === 'day' ? `${Number(month)}/${Number(day)}`
      : Number(month) === 1 ? `${year} 年 1 月` : `${Number(month)} 月`;
  }
  return last ? `${name}（累计中）` : name;
}
const totals = computed(() => points.value.reduce((s, p) => ({rx: s.rx + p.rx, tx: s.tx + p.tx}), {rx: 0, tx: 0}));

const traffic = computed(() => props.server.traffic || null);
const cur = computed(() => props.server.profile?.billing?.currency || '');
const projection = computed(() => projectedTraffic(traffic.value, new Date(props.now)));
const over = computed(() => Boolean(traffic.value?.limit && projection.value && projection.value.bytes > traffic.value.limit));
const period = computed(() => {
  const tr = traffic.value;
  if (!tr) return [];
  return [
    ['已用', `${bytes(tr.used)}${tr.limit ? ` / ${bytes(tr.limit)}` : ''}`, tr.mode === 'unlimited' ? '不限流量' : trafficTypeText(tr.type)],
    ['下行 · 上行', `${bytes(tr.rx)} · ${bytes(tr.tx)}`, ''],
    projection.value && ['预计本期', bytes(projection.value.bytes), over.value ? '会超出额度' : '按目前速度'],
    tr.mode === 'metered' && ['费用', `${cur.value}${(tr.cost || 0).toFixed(2)}`, `${cur.value}${tr.price}/GB${projection.value ? ` · 预计 ${cur.value}${(projection.value.bytes / 1024 ** 3 * tr.price).toFixed(2)}` : ''}`],
  ].filter(Boolean);
});
async function clearCalibration() {
  clearing.value = true;
  try { ok(await Api.clearServerTrafficCalibration(props.server.id)); emit('changed'); ElMessage.success('已取消校准，按节点统计的用量显示'); }
  catch (e) { ElMessage.error(safeError(e)); }
  finally { clearing.value = false; }
}
</script>
<template>
  <section ref="el" class="ix-card dt-section" :class="{'is-waiting': !data && !error}" data-jump="traffic" aria-label="流量统计">
    <header class="ix-card-head">
      <h3><CardIcon :icon="Histogram" tone="success" />流量统计</h3>
      <span class="instance-muted">{{ SPANS[unit] }}</span>
      <span class="ob-grow" />
      <div class="dt-switches">
        <div class="sv-ranges" role="tablist" aria-label="统计单位">
          <button v-for="[key, label] in UNITS" :key="key" type="button" role="tab" :aria-selected="unit === key" @click="unit = key">{{ label }}</button>
        </div>
        <div class="sv-ranges" role="tablist" aria-label="图表样式">
          <button v-for="[key, label] in KINDS" :key="key" type="button" role="tab" :aria-selected="kind === key" @click="kind = key">{{ label }}</button>
        </div>
      </div>
    </header>
    <div v-if="traffic" class="dt-period" data-period-detail>
      <p class="dt-period-title">本期 <span class="instance-muted">自 {{ traffic.periodStart }} 起 · {{ traffic.mode === 'metered' ? '结算' : '重置' }}于每月 {{ traffic.resetDay }} 号</span>
        <span class="ob-grow" />
        <button type="button" class="ix-text-button" :disabled="busy" title="按服务商面板上的数值校准本期流量" @click="emit('calibrate')">校准</button>
        <button type="button" class="ix-text-button" title="流量方式、额度和重置日" @click="emit('settings')">流量设置</button></p>
      <dl class="dt-stats">
        <div v-for="[label, value, sub] in period" :key="label" :data-tone="label === '预计本期' && over ? 'warn' : null"><dt>{{ label }}</dt><dd>{{ value }}</dd><small v-if="sub">{{ sub }}</small></div>
      </dl>
      <p v-if="traffic.calibrated" class="dt-note">{{ dateTime(traffic.calibrated.atMs) }} 按服务商数值校准为 {{ bytes(traffic.calibrated.used) }}，之后的用量由节点累加
        <button type="button" class="ix-text-button" :disabled="busy || clearing" @click="clearCalibration">取消校准</button></p>
    </div>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!data" class="instance-muted dt-wait" role="status">正在读取…</p>
    <template v-else>
      <TrendChart :key="`${unit}-${kind}`" title="下行与上行" label="流量统计" :t="t" :step="step" :series="series" :format="bytes" unit="bytes" :kind="kind" partial :when="when" :height="phone ? 150 : 180" empty="还没有流量记录。" />
      <p class="dt-total"><span><i class="is-rx" aria-hidden="true" />下行合计 {{ bytes(totals.rx) }}</span><span><i class="is-tx" aria-hidden="true" />上行合计 {{ bytes(totals.tx) }}</span>
        <span class="instance-muted">共 {{ bytes(totals.rx + totals.tx) }}</span></p>
    </template>
  </section>
</template>
