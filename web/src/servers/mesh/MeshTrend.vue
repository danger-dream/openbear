<script setup>
import { computed, ref, watch } from 'vue';
import { bytes } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { mesh, meshError } from './meshStore.js';
import { trendRttText } from './meshHelpers.js';
import { usePoll } from './usePoll.js';

// Small trend charts for one member: latency, timeouts, memory and CPU.
const props = defineProps({serverId: {type: String, required: true}});
const RANGES = [['1h', '1 小时'], ['24h', '24 小时'], ['7d', '7 天']];
const range = ref('1h'), points = ref(null), error = ref(''), missing = ref(false), rtt = ref(null);

// Every minute; an older controller without trends is not asked again.
const poll = usePoll(async current => {
  try {
    const data = await MeshApi.trend(props.serverId, range.value);
    if (!current()) return;
    points.value = Array.isArray(data?.points) ? data.points : []; error.value = '';
    // Who the latency is measured to: {kind: 'entry' | 'links', peers}; older controllers leave it out.
    rtt.value = data?.rtt && typeof data.rtt === 'object' ? data.rtt : null;
  } catch (e) {
    if (!current()) return;
    if (e?.response?.status === 404) missing.value = true; else error.value = meshError(e);
  }
}, {interval: () => missing.value ? 0 : 60000});
watch(() => [props.serverId, range.value], () => { points.value = null; void poll.run(); }, {immediate: true});

const num = v => (Number.isFinite(Number(v)) && v !== null && v !== '' ? Number(v) : null);
const pct = v => `${v >= 10 ? Math.round(v) : Math.round(v * 10) / 10}%`;
const SERIES = [
  ['rttMs', '延迟', v => `${Math.round(v)} ms`],
  ['timeoutRate', '探测超时', v => pct(v * 100), 'bad'],
  ['rssBytes', '内存', v => bytes(v)],
  ['cpuPercent', 'CPU', v => pct(v)],
];
const W = 120, H = 28;
// Points without a value (latency while every probe timed out) break the line.
const charts = computed(() => (points.value || []).length ? SERIES.map(([key, label, fmt, tone]) => {
  const all = points.value.map(p => ({at: Number(p.at), v: num(p[key])})).filter(p => Number.isFinite(p.at)).sort((a, b) => a.at - b.at);
  const list = all.filter(p => p.v !== null);
  if (!list.length) return {key, label, empty: true};
  const t0 = all[0].at, t1 = all[all.length - 1].at, max = Math.max(...list.map(p => p.v)), top = max || 1;
  const x = at => ((t1 > t0 ? (at - t0) / (t1 - t0) : 1) * W).toFixed(1), y = v => (H - 2 - (v / top) * (H - 4)).toFixed(1);
  const runs = [[]];
  for (const p of all) { if (p.v === null) { if (runs.at(-1).length) runs.push([]); } else runs.at(-1).push(p); }
  const segments = runs.filter(r => r.length);
  const line = segments.map(r => r.map((p, i) => `${i ? 'L' : 'M'} ${x(p.at)} ${y(p.v)}`).join(' ') + (r.length === 1 ? ` l 0.1 0` : '')).join(' ');
  const area = segments.map(r => `M ${x(r[0].at)} ${H} ${r.map(p => `L ${x(p.at)} ${y(p.v)}`).join(' ')} L ${x(r.at(-1).at)} ${H} Z`).join(' ');
  const last = all[all.length - 1];
  return {key, label, tone, line, area, now: last.v === null ? '—' : fmt(last.v), max: fmt(max)};
}) : []);
const rttNote = computed(() => trendRttText(rtt.value, mesh.work?.members || []));
const counts = computed(() => {
  const sum = key => (points.value || []).reduce((n, p) => n + (num(p[key]) || 0), 0);
  return (points.value || []).length ? `切换 ${sum('switches')} 次 · 重连 ${sum('reconnects')} 次` : '';
});
</script>
<template>
  <section class="mesh-trend" aria-label="趋势">
    <header class="mesh-trend-head">
      <h4>趋势</h4>
      <span v-if="counts" class="instance-muted">{{ counts }}</span>
      <span class="ob-grow" />
      <div v-if="!missing" class="sv-ranges" role="tablist" aria-label="时间范围">
        <button v-for="[value, label] in RANGES" :key="value" type="button" role="tab" :aria-selected="range === value" @click="range = value">{{ label }}</button>
      </div>
    </header>
    <p v-if="missing" class="instance-muted">主控还不提供趋势数据。</p>
    <p v-else-if="error" class="instance-muted">读不到趋势：{{ error }}</p>
    <p v-else-if="!points" class="instance-muted" role="status">正在读取…</p>
    <p v-else-if="!points.length" class="instance-muted">这段时间还没有数据。</p>
    <ul v-else class="mesh-sparks">
      <li v-for="c in charts" :key="c.key" :data-tone="c.tone">
        <span class="mesh-spark-label">{{ c.label }}</span>
        <svg v-if="!c.empty" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true"><path class="mesh-spark-area" :d="c.area" /><path class="mesh-spark-line" :d="c.line" /></svg>
        <span v-else class="mesh-spark-empty">—</span>
        <span class="mesh-spark-value" :title="c.empty ? '' : `最高 ${c.max}`">{{ c.empty ? '' : c.now }}</span>
      </li>
    </ul>
    <p v-if="points?.length && rttNote" class="instance-muted mesh-trend-note">上面是{{ rttNote }}；单条线路请点连线查看稳定性。</p>
  </section>
</template>
