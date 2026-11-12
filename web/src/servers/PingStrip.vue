<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { stripBackground, stripCells, stripPaths } from './detail/detailHelpers.js';
import { unwatchSize, watchSize } from './stripSize.js';

// Thirty minutes of latency or loss. Once its width is known the minutes are drawn on whole device
// pixels without smoothing, every one as wide as the next; before that, a background by percentages.
const props = defineProps({cells: {type: Array, required: true}, kind: {type: String, required: true}});
const el = ref(null), size = ref(null);
const places = computed(() => size.value ? stripCells(size.value.px, size.value.dpr, props.cells.length) : null);
const paths = computed(() => places.value ? stripPaths(props.cells, props.kind, places.value) : null);
const background = computed(() => paths.value ? null : {backgroundImage: stripBackground(props.cells, props.kind)});
onMounted(() => watchSize(el.value, s => { if (['px', 'dpr', 'offset'].some(k => s[k] !== size.value?.[k])) size.value = s; }));
onBeforeUnmount(() => unwatchSize(el.value));
</script>
<template>
  <i ref="el" class="pb-strip" :class="{'is-exact': paths}" :style="background" aria-hidden="true">
    <svg v-if="paths" :width="(size.px + 2) / size.dpr" height="100%" :viewBox="`0 0 ${size.px + 2} 1`" preserveAspectRatio="none" shape-rendering="crispEdges"
      :style="{marginLeft: `${size.offset}px`}"><path v-for="p in paths" :key="p.fill" :d="p.d" :style="{fill: p.fill}" /></svg>
  </i>
</template>
