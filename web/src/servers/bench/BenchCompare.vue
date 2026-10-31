<script setup>
import { ref, watch } from 'vue';
import { flag } from '../serverHelpers.js';
import BenchReport from './BenchReport.vue';
import { runTime } from './benchHelpers.js';

// Reports side by side, scrolling together so the same part of each stays level. A phone has no
// room for columns: one report at a time, picked by the tabs above it.
const props = defineProps({runs: {type: Array, required: true}, serverOf: {type: Function, required: true}, now: {type: Number, default: () => Date.now()}, phone: Boolean,
  active: {type: Boolean, default: true}});
const boxes = ref([]), shown = ref(0);
let syncing = false;

watch(() => props.runs.length, n => { if (shown.value >= n) shown.value = 0; });
function sync(index) {
  if (syncing || props.phone) return;
  const source = boxes.value[index];
  if (!source) return;
  syncing = true;
  for (const [i, box] of boxes.value.entries()) {
    if (i !== index && box) { box.scrollTop = source.scrollTop; box.scrollLeft = source.scrollLeft; }
  }
  requestAnimationFrame(() => { syncing = false; });
}
</script>
<template>
  <div class="bn-compare" :class="{'is-phone': phone}">
    <div v-if="phone" class="sv-ranges bn-compare-tabs" role="tablist" aria-label="对比的报告">
      <button v-for="(r, i) in runs" :key="r.id" type="button" role="tab" :aria-selected="shown === i" @click="shown = i">
        <span class="sv-flag" aria-hidden="true">{{ flag(serverOf(r)?.region) }}</span>{{ serverOf(r)?.name || '已删除' }} · {{ runTime(r.createdMs).slice(5) }}
      </button>
    </div>
    <div class="bn-compare-cols" :style="{'--bn-cols': phone ? 1 : runs.length}">
      <div v-for="(r, i) in runs" v-show="!phone || shown === i" :key="r.id" :ref="el => { boxes[i] = el; }" class="bn-compare-col" @scroll="sync(i)">
        <BenchReport :run="r" :server="serverOf(r)" :now="now" :active="active" compact />
      </div>
    </div>
  </div>
</template>
