<script setup>
import { Box } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import TrendChart from './TrendChart.vue';
import { COLORS, DISK_RANGES, PALETTE, pct } from './detailHelpers.js';
import { every, useVisible } from './useVisible.js';
import { bytes, percent, rate, safeError, tone } from '../serverHelpers.js';

// 硬盘: each partition's use now (sampled hourly), its share used over 7/30/90 days or all of it, and reads
// and writes over the page's range. Read when scrolled to; refreshed every ten minutes while in view.
const props = defineProps({server: {type: Object, required: true}, series: {type: Object, default: null}, rangeLabel: {type: String, default: ''},
  has: {type: Object, required: true}, active: Boolean, phone: Boolean});
const el = ref(null), range = ref('30d'), data = ref(null), error = ref('');
const {visible, seen} = useVisible(el);
let alive = true, mine = 0;

async function load() {
  const id = ++mine, want = range.value;
  try {
    const result = ok(await Api.serverDisks(props.server.id, want));
    if (alive && id === mine) { data.value = result; error.value = ''; }
  } catch (e) { if (alive && id === mine) error.value = safeError(e); }
}
watch(() => seen.value && props.active, on => { if (on) void load(); }, {immediate: true});
watch(range, () => { if (seen.value) void load(); });
const stop = every(600000, () => props.active && visible.value, load);
onBeforeUnmount(() => { alive = false; stop(); });

const parts = computed(() => (data.value?.partitions || []).map((p, i) => ({...p, color: PALETTE[i % PALETTE.length], pct: Math.round(percent(p.used, p.total))})));
const usage = computed(() => parts.value.map(p => ({key: p.path, label: p.path, color: p.color, values: data.value.series[p.path] || []})));
const io = computed(() => {
  const p = props.series?.points;
  return p ? [{key: 'read', label: '读', color: COLORS.read, values: p.ioRead}, {key: 'write', label: '写', color: COLORS.write, values: p.ioWrite}] : [];
});
</script>
<template>
  <section ref="el" class="ix-card dt-section" :class="{'is-waiting': !data && !error}" data-jump="disk" aria-label="硬盘">
    <header class="ix-card-head"><h3><CardIcon :icon="Box" tone="violet" />硬盘</h3>
      <span v-if="data?.sampledAt" class="instance-muted">每小时记录一次</span>
      <span class="ob-grow" />
      <div class="sv-ranges" role="tablist" aria-label="使用率时间范围">
        <button v-for="[key, label] in DISK_RANGES" :key="key" type="button" role="tab" :aria-selected="range === key" @click="range = key">{{ label }}</button>
      </div>
    </header>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!data" class="instance-muted dt-wait" role="status">正在读取…</p>
    <p v-else-if="!parts.length" class="dt-empty">还没有分区记录，节点在线时每小时记录一次。</p>
    <template v-else>
      <ul class="dt-parts" aria-label="分区">
        <li v-for="p in parts" :key="p.path" :style="{'--c': p.color}">
          <span class="dt-part-name"><i aria-hidden="true" /><b class="mono" :title="p.path">{{ p.path }}</b><small v-if="p.fs">{{ p.fs }}</small></span>
          <span class="dt-part-use">{{ bytes(p.used) }} / {{ bytes(p.total) }}<b :data-tone="tone(p.pct)">{{ p.pct }}%</b></span>
          <i class="dt-part-bar" aria-hidden="true"><b :style="{width: `${Math.min(p.pct, 100)}%`}" /></i>
        </li>
      </ul>
      <div class="dt-chart">
        <TrendChart :key="`use-${data.range}`" title="使用率" label="分区使用率" :t="data.t" :step="data.step" :series="usage" :format="pct" :max="100" :height="phone ? 110 : 120" empty="还没有足够的记录。" />
      </div>
    </template>
    <div v-if="has.io && series" class="dt-chart" data-chart="io">
      <TrendChart :key="`io-${series.range}`" title="读写" :note="rangeLabel" label="磁盘读写" :t="series.points.t" :step="series.step" :series="io" :format="rate" unit="rate" :height="phone ? 110 : 120" />
    </div>
  </section>
</template>
