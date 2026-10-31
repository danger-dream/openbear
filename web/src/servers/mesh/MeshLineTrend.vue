<script setup>
import { computed, ref, watch } from 'vue';
import { MeshApi } from './meshApi.js';
import { meshError } from './meshStore.js';
import { availabilityText, LINE_RANGES, lineFacts, lossText, msText } from './meshHelpers.js';
import { usePoll } from './usePoll.js';

// One line's long-term record (GET /mesh/lines/:id/trend): availability, latency and loss over a
// range, with how often it went down. A cold standby is only tried now and then: points without
// a sample are gaps, not time down.
const props = defineProps({lineId: {type: String, required: true}, cold: Boolean});
const range = ref('24h'), data = ref(null), error = ref(''), missing = ref(false);
const LINE_ERRORS = {mesh_line_not_found: '这条线路还没有记录，应用到网格并连上后开始记录', invalid_mesh_line: '线路编号不正确', invalid_range: '时间范围不正确'};

// Every minute; an older controller without line records is not asked again.
const poll = usePoll(async current => {
  try {
    const r = await MeshApi.lineTrend(props.lineId, range.value);
    if (!current()) return;
    data.value = {line: r?.line || null, summary: r?.summary || null, series: Array.isArray(r?.series) ? r.series : []}; error.value = '';
  } catch (e) {
    if (!current()) return;
    // An older controller has no such endpoint; a line it does not know yet answers mesh_line_not_found.
    const code = e?.response?.data?.error;
    if (e?.response?.status === 404 && code !== 'mesh_line_not_found') missing.value = true;
    else error.value = LINE_ERRORS[code] || meshError(e);
  }
}, {interval: () => missing.value ? 0 : 60000});
watch(() => [props.lineId, range.value], () => { data.value = null; error.value = ''; void poll.run(); }, {immediate: true});

const num = v => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
const W = 120, H = 28;
const points = computed(() => (data.value?.series || []).map(p => ({at: Number(p.at), rtt: num(p.rttMs), loss: num(p.lossRate), up: num(p.upRatio), downs: num(p.downs), role: p.role || ''}))
  .filter(p => Number.isFinite(p.at)).sort((a, b) => a.at - b.at));
const sampled = computed(() => points.value.some(p => p.up !== null || p.rtt !== null));
// Availability as a strip of cells: up, partly down, down, or no sample (left empty).
const cells = computed(() => {
  const list = points.value, w = list.length ? W / list.length : W;
  return list.map((p, i) => ({x: (i * w).toFixed(2), w: Math.max(w - 0.6, 0.4).toFixed(2), tone: p.up === null ? 'none' : p.up >= 0.999 ? 'ok' : p.up <= 0.001 ? 'bad' : 'warn'}));
});
// A line through the points that have a value; a missing sample breaks it instead of dropping to zero.
function line(key) {
  const all = points.value, list = all.filter(p => p[key] !== null);
  if (!list.length) return null;
  const t0 = all[0].at, t1 = all.at(-1).at, top = Math.max(...list.map(p => p[key])) || 1;
  const x = at => ((t1 > t0 ? (at - t0) / (t1 - t0) : 1) * W).toFixed(1), y = v => (H - 2 - (v / top) * (H - 4)).toFixed(1);
  const runs = [[]];
  for (const p of all) { if (p[key] === null) { if (runs.at(-1).length) runs.push([]); } else runs.at(-1).push(p); }
  const kept = runs.filter(r => r.length);
  // Runs of two or more are lines; a lone sample (a cold standby's occasional try) is a dot.
  return {line: kept.filter(r => r.length > 1).map(r => r.map((p, i) => `${i ? 'L' : 'M'} ${x(p.at)} ${y(p[key])}`).join(' ')).join(' '),
    dots: kept.filter(r => r.length === 1).map(([p]) => `M ${x(p.at)} ${y(p[key])} l 0.01 0`).join(' ')};
}
const rttLine = computed(() => line('rtt')), lossLine = computed(() => line('loss'));
const summary = computed(() => data.value?.summary || null);
// Cold: as the board knows it now, or every sample over the range was a cold standby's try.
const cold = computed(() => props.cold || (points.value.some(p => p.role) && points.value.every(p => !p.role || p.role === 'cold')));
const avail = computed(() => (num(summary.value?.knownSeconds) === 0 ? '' : availabilityText(summary.value?.availability, summary.value?.downSeconds)) || '—');
const avgRtt = computed(() => msText(num(summary.value?.rttMs?.avg)) || '—'), avgLoss = computed(() => lossText(num(summary.value?.lossRate)) || '—');
const facts = computed(() => lineFacts(summary.value, cold.value, range.value).filter(([label]) => !['可用率', '试连成功率', '平均延迟', '丢包'].includes(label)));
// A line no longer in the applied mesh still has its record.
const gone = computed(() => data.value?.line?.current === false);
</script>
<template>
  <section class="mesh-trend mesh-line-trend" aria-label="线路稳定性">
    <header class="mesh-trend-head">
      <h4>稳定性</h4>
      <span v-if="cold" class="pb-tag is-plain">冷备，抽测</span>
      <span class="ob-grow" />
      <div v-if="!missing" class="sv-ranges mesh-line-ranges" role="tablist" aria-label="时间范围">
        <button v-for="[value, label] in LINE_RANGES" :key="value" type="button" role="tab" :aria-selected="range === value" @click="range = value">{{ label }}</button>
      </div>
    </header>
    <p v-if="missing" class="instance-muted">主控还不提供线路稳定性数据，更新主控后可用。</p>
    <p v-else-if="error" class="instance-muted">读不到稳定性：{{ error }}</p>
    <p v-else-if="!data" class="instance-muted" role="status">正在读取…</p>
    <p v-else-if="!sampled" class="instance-muted">{{ cold ? '这段时间还没有抽测过。' : '这段时间还没有数据。' }}</p>
    <template v-else>
      <p v-if="gone" class="instance-muted mesh-line-note">这条线路已不在应用的网格里，下面是以前的记录。</p>
      <ul class="mesh-sparks">
        <li class="mesh-line-up">
          <span class="mesh-spark-label">{{ cold ? '试连' : '可用' }}</span>
          <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
            <line class="mesh-line-base" x1="0" :y1="H / 2" :x2="W" :y2="H / 2" />
            <rect v-for="(c, i) in cells" :key="i" :x="c.x" :y="c.tone === 'none' ? H / 2 - 1 : 6" :width="c.w" :height="c.tone === 'none' ? 2 : H - 12" :data-tone="c.tone" />
          </svg>
          <span class="mesh-spark-value">{{ avail }}</span>
        </li>
        <li>
          <span class="mesh-spark-label">平均延迟</span>
          <svg v-if="rttLine" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true"><path v-if="rttLine.line" class="mesh-spark-line" :d="rttLine.line" /><path v-if="rttLine.dots" class="mesh-spark-line mesh-spark-dots" :d="rttLine.dots" /></svg>
          <span v-else class="mesh-spark-empty">—</span>
          <span class="mesh-spark-value" title="平均">{{ avgRtt }}</span>
        </li>
        <li data-tone="bad">
          <span class="mesh-spark-label">丢包</span>
          <svg v-if="lossLine" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true"><path v-if="lossLine.line" class="mesh-spark-line" :d="lossLine.line" /><path v-if="lossLine.dots" class="mesh-spark-line mesh-spark-dots" :d="lossLine.dots" /></svg>
          <span v-else class="mesh-spark-empty">—</span>
          <span class="mesh-spark-value" title="平均">{{ avgLoss }}</span>
        </li>
      </ul>
      <p class="mesh-line-legend instance-muted" aria-hidden="true">
        <span data-tone="ok">{{ cold ? '试连成功' : '连通' }}</span><span v-if="!cold" data-tone="warn">部分时间断开</span><span data-tone="bad">{{ cold ? '试连失败' : '断开' }}</span><span data-tone="none">没有样本</span>
      </p>
      <dl v-if="facts.length" class="mesh-stats is-compact mesh-line-facts">
        <div v-for="[label, value, tone] in facts" :key="label"><dt>{{ label }}</dt><dd :class="tone ? `ob-${tone}` : ''">{{ value }}</dd></div>
      </dl>
    </template>
    <p v-if="cold && !missing" class="instance-muted mesh-line-note">冷备平时不连，只定时抽测；图上的空白是那段时间没有抽测，不是断开。</p>
  </section>
</template>
