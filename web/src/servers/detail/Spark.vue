<script setup>
import { computed, useId } from 'vue';

// The last ten minutes under a summary card: a line or two and a soft fill under the first, nothing to point at.
// lines: [{values, color}], all on one scale (max, or the highest value).
const props = defineProps({lines: {type: Array, default: () => []}, max: {type: Number, default: 0}});
const W = 200, H = 34, id = useId();
const shapes = computed(() => {
  const lines = props.lines.map(l => ({...l, values: l.values.filter(v => v != null)})).filter(l => l.values.length > 1);
  if (!lines.length) return [];
  let peak = 0;
  for (const l of lines) for (const v of l.values) if (v > peak) peak = v;
  const top = props.max || peak * 1.15 || 1;
  return lines.map((l, k) => {
    const pts = l.values.map((value, i) => [i / (l.values.length - 1) * W, H - 1.5 - Math.min(value, top) / top * (H - 4)]);
    const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
    return {key: k, color: l.color, line, area: k === 0 ? `${line}L${W},${H}L0,${H}Z` : ''};
  });
});
</script>
<template>
  <svg v-if="shapes.length" class="tc-spark" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient :id="id" x1="0" y1="0" x2="0" y2="1"><stop offset="0" :style="{stopColor: shapes[0].color, stopOpacity: 0.22}" /><stop offset="1" :style="{stopColor: shapes[0].color, stopOpacity: 0}" /></linearGradient></defs>
    <path v-if="shapes[0].area" :d="shapes[0].area" :fill="`url(#${id})`" />
    <path v-for="s in shapes" :key="s.key" :d="s.line" :style="{stroke: s.color}" fill="none" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round" />
  </svg>
  <span v-else class="tc-spark is-empty" aria-hidden="true" />
</template>
