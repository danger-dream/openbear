<script setup>
import { computed } from 'vue';
import { COUNTRIES, SIZE } from '../worldMap.js';
import { homeBox, pinOf } from './mapPlace.js';

// Where an address is, on the built-in world map (nothing is fetched): its country highlighted, its pin
// (mapPlace.js), and the view brought in to the country. Shown where the online map cannot load (IpMap.vue).
const props = defineProps({lat: {type: Number, default: null}, lon: {type: Number, default: null}, country: {type: String, default: ''}});
const ASPECT = 2;
const pin = computed(() => pinOf(props.lat, props.lon, props.country));
// The country itself last, so its outline is drawn over its neighbours'.
const paths = computed(() => Object.entries(COUNTRIES).sort(([a], [b]) => (a === props.country) - (b === props.country)));
const view = computed(() => {
  const p = pin.value;
  let box = homeBox(props.country, p);
  if (!box && p) box = {x0: p[0], y0: p[1], x1: p[0], y1: p[1]};
  if (!box) return {x: 0, y: 150, w: SIZE, h: SIZE / ASPECT};
  if (p) box = {x0: Math.min(box.x0, p[0]), y0: Math.min(box.y0, p[1]), x1: Math.max(box.x1, p[0]), y1: Math.max(box.y1, p[1])};
  const cx = (box.x0 + box.x1) / 2, cy = (box.y0 + box.y1) / 2;
  let w = Math.max((box.x1 - box.x0) * 1.7, (box.y1 - box.y0) * 1.7 * ASPECT, 70);
  w = Math.min(w, SIZE);
  const h = w / ASPECT;
  return {x: Math.min(Math.max(cx - w / 2, 0), SIZE - w), y: Math.min(Math.max(cy - h / 2, 0), SIZE - h), w, h};
});
const r = computed(() => view.value.w * 0.011);
</script>
<template>
  <svg class="wm" :viewBox="`${view.x.toFixed(1)} ${view.y.toFixed(1)} ${view.w.toFixed(1)} ${view.h.toFixed(1)}`" preserveAspectRatio="xMidYMid slice" role="img" aria-label="位置地图">
    <path v-for="[code, d] in paths" :key="code" :d="d" class="wm-land" :class="{'is-home': code === country}" vector-effect="non-scaling-stroke" />
    <g v-if="pin" class="wm-pin">
      <circle :cx="pin[0]" :cy="pin[1]" :r="r * 2.6" class="wm-pulse" />
      <circle :cx="pin[0]" :cy="pin[1]" :r="r" class="wm-dot" vector-effect="non-scaling-stroke" />
    </g>
  </svg>
</template>
