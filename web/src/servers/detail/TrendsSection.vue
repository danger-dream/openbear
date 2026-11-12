<script setup>
import { TrendCharts } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed } from 'vue';
import TrendChart from './TrendChart.vue';
import { COLORS, METRIC_RANGES, pct } from './detailHelpers.js';
import { rate } from '../serverHelpers.js';

// 负载 and 带宽 over the chosen range, side by side. The range is the page's: 网络设备 and 硬盘 below
// draw their history from the same samples.
const props = defineProps({series: {type: Object, default: null}, range: {type: String, required: true}, has: {type: Object, required: true},
  info: {type: Object, default: () => ({})}, phone: Boolean});
const emit = defineEmits(['update:range']);
const height = computed(() => props.phone ? 150 : 190);
const points = computed(() => props.series?.points || null);
const load = computed(() => {
  const p = points.value;
  if (!p) return [];
  const cores = props.info.cores || 1, swapTotal = props.info.swapTotal || 0;
  return [
    {key: 'cpu', label: 'CPU', color: COLORS.cpu, values: p.cpu},
    {key: 'mem', label: '内存', color: COLORS.mem, values: p.mem},
    swapTotal && p.swap && {key: 'swap', label: '交换', color: COLORS.swap, values: p.swap.map(v => v * 100 / swapTotal)},
    props.has.wait && {key: 'steal', label: 'steal', color: COLORS.steal, values: p.steal},
    props.has.wait && p.iowait && {key: 'iowait', label: 'iowait', color: COLORS.iowait, values: p.iowait, hidden: true},
    // Load drawn as a share of the cores, so it sits on the same 0-100 scale; the tooltip shows it as it is.
    props.has.load && {key: 'load', label: '负载', color: COLORS.load, values: p.load.map(v => v * 100 / cores), format: v => (v * cores / 100).toFixed(2), hidden: true},
  ].filter(Boolean);
});
const bandwidth = computed(() => {
  const p = points.value;
  return p ? [{key: 'rx', label: '下行', color: COLORS.rx, values: p.rxRate}, {key: 'tx', label: '上行', color: COLORS.tx, values: p.txRate}] : [];
});
const empty = computed(() => props.range === 'live' ? '节点在线后，这里显示最近 10 分钟每 3 秒一个点的数据。' : '这段时间还没有数据。');
</script>
<template>
  <section class="ix-card dt-section" data-jump="trends" aria-label="走势">
    <header class="ix-card-head">
      <h3><CardIcon :icon="TrendCharts" />走势</h3>
      <span class="ob-grow" />
      <div class="sv-ranges dt-ranges" role="tablist" aria-label="时间范围">
        <button v-for="[key, label] in METRIC_RANGES" :key="key" type="button" role="tab" :aria-selected="range === key" @click="emit('update:range', key)">{{ label }}</button>
      </div>
    </header>
    <p v-if="!series" class="instance-muted" role="status">正在读取…</p>
    <div v-else class="dt-pair">
      <div class="dt-chart" data-chart="load">
        <TrendChart :key="`load-${range}`" title="负载" :t="points.t" :step="series.step" :series="load" :format="pct" :max="100" :height="height" :empty="empty" />
      </div>
      <div class="dt-chart" data-chart="bandwidth">
        <TrendChart :key="`bw-${range}`" title="带宽" :t="points.t" :step="series.step" :series="bandwidth" :format="rate" unit="rate" :height="height" :empty="empty" />
      </div>
    </div>
  </section>
</template>
