<script setup>
import { computed } from 'vue';
import { lossLevel } from './detailHelpers.js';

// One target's loss along the chart's time axis: a cell per sample, from its time to the next step, coloured
// by how much was lost (all of it: 断连); nothing where no sample was taken. Neighbouring cells alike are
// drawn as one, so a day of five-minute samples is a handful of shapes.
const props = defineProps({t: {type: Array, required: true}, step: {type: Number, required: true}, values: {type: Array, required: true},
  first: {type: Number, required: true}, width: {type: Number, required: true}, label: {type: String, default: ''}});
const W = 1000;
const cells = computed(() => {
  const out = [];
  let run = null;
  props.t.forEach((ts, i) => {
    const kind = lossLevel(props.values[i]);
    const x = (ts - props.first) / props.width * W, w = props.step / props.width * W;
    if (run && run.kind === kind && ts - props.t[i - 1] <= props.step * 1.5) run.w = x + w - run.x;
    else if (kind !== 'none') out.push(run = {x, w, kind});
    else run = null;
  });
  return out.map((c, i) => ({...c, key: i}));
});
</script>
<template>
  <svg class="ll" :viewBox="`0 0 ${W} 10`" preserveAspectRatio="none" role="img" :aria-label="label">
    <rect v-for="c in cells" :key="c.key" :x="c.x.toFixed(1)" y="0" :width="Math.max(c.w, 0.8).toFixed(1)" height="10" :data-loss="c.kind" />
  </svg>
</template>
