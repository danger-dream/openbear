<script setup>
import { computed, ref } from 'vue';

// Lines break where samples are missing (node offline) instead of bridging the gap.
const props = defineProps({
  title: {type: String, required: true},
  t: {type: Array, required: true},
  step: {type: Number, required: true},
  series: {type: Array, required: true},
  format: {type: Function, default: v => String(v)},
  max: {type: Number, default: 0},
});
const W = 600, H = 120, PAD = 4;
const hover = ref(-1);
const top = computed(() => {
  if (props.max) return props.max;
  const peak = Math.max(0, ...props.series.flatMap(s => s.values));
  if (!peak) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(peak));
  return Math.ceil(peak / magnitude * 1.1) * magnitude;
});
const span = computed(() => {
  const first = props.t[0] ?? 0, last = props.t[props.t.length - 1] ?? 1;
  return {first, width: Math.max(last - first, props.step)};
});
const x = ts => PAD + (ts - span.value.first) / span.value.width * (W - PAD * 2);
const y = v => H - PAD - Math.min(v, top.value) / top.value * (H - PAD * 2);
const paths = computed(() => props.series.map(s => {
  let d = '';
  props.t.forEach((ts, i) => {
    const gap = i === 0 || ts - props.t[i - 1] > props.step * 1.5;
    d += `${gap ? 'M' : 'L'}${x(ts).toFixed(1)},${y(s.values[i] || 0).toFixed(1)}`;
  });
  return {...s, d};
}));
// A soft fill under the main series, closed per continuous segment.
const area = computed(() => {
  const s = props.series[0];
  if (!s) return '';
  let d = '', start = 0;
  const close = end => { d += `L${x(props.t[end]).toFixed(1)},${H - PAD}L${x(props.t[start]).toFixed(1)},${H - PAD}Z`; };
  props.t.forEach((ts, i) => {
    const gap = i === 0 || ts - props.t[i - 1] > props.step * 1.5;
    if (gap && i) close(i - 1);
    if (gap) start = i;
    d += `${gap ? 'M' : 'L'}${x(ts).toFixed(1)},${y(s.values[i] || 0).toFixed(1)}`;
  });
  if (props.t.length) close(props.t.length - 1);
  return d;
});
const latest = computed(() => props.series.map(s => s.values[s.values.length - 1]));
const shown = computed(() => hover.value >= 0 ? props.series.map(s => s.values[hover.value]) : latest.value);
const when = ts => new Date(ts * 1000).toLocaleString('zh-CN', props.step >= 3600 ? {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}
  : props.step < 60 ? {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false} : {hour: '2-digit', minute: '2-digit', hour12: false});
function pick(event) {
  if (!props.t.length) return;
  const box = event.currentTarget.getBoundingClientRect();
  const ts = span.value.first + (event.clientX - box.left) / box.width * span.value.width;
  let best = 0;
  props.t.forEach((v, i) => { if (Math.abs(v - ts) < Math.abs(props.t[best] - ts)) best = i; });
  hover.value = best;
}
</script>
<template>
  <figure class="sv-chart">
    <figcaption>
      <span>{{ title }}</span>
      <span class="sv-chart-values">
        <span v-for="(s, i) in series" :key="s.label" :style="{'--c': s.color}">{{ series.length > 1 ? s.label + ' ' : '' }}{{ shown[i] === undefined ? '—' : format(shown[i]) }}</span>
        <time v-if="hover >= 0">{{ when(t[hover]) }}</time>
      </span>
    </figcaption>
    <div v-if="!t.length" class="sv-chart-empty">暂无数据</div>
    <svg v-else :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" role="img" :aria-label="title" @pointermove="pick" @pointerdown="pick" @pointerleave="hover = -1">
      <line v-for="f in [0.25, 0.5, 0.75]" :key="f" :x1="0" :x2="W" :y1="H - PAD - f * (H - PAD * 2)" :y2="H - PAD - f * (H - PAD * 2)" class="sv-grid" />
      <path v-if="area" :d="area" :fill="series[0].color" class="sv-area" />
      <path v-for="p in paths" :key="p.label" :d="p.d" :stroke="p.color" class="sv-line" vector-effect="non-scaling-stroke" />
      <line v-if="hover >= 0" :x1="x(t[hover])" :x2="x(t[hover])" y1="0" :y2="H" class="sv-cursor" vector-effect="non-scaling-stroke" />
    </svg>
    <div v-if="t.length" class="sv-chart-axis"><span>{{ when(t[0]) }}</span><span>峰值刻度 {{ format(top) }}</span><span>{{ when(t[t.length - 1]) }}</span></div>
  </figure>
</template>
