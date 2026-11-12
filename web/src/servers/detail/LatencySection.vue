<script setup>
import { Odometer } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import TrendChart from './TrendChart.vue';
import LatencyEditor from './LatencyEditor.vue';
import { CARRIER_COLORS, LATENCY_RANGES, LOSS_LEVELS, OTHER_COLORS, latencyTone, lossText, lossTone, ms } from './detailHelpers.js';
import { every, useVisible } from './useVisible.js';
import { safeError } from '../serverHelpers.js';

// 延迟与丢包: one tile per target (its latest latency, the range's average and loss) that also picks the
// lines drawn; the latency of those picked over the whole range asked for, with the spans where they were
// all cut (断连) or not measured marked behind the lines, and under it each one's loss along the same axis.
// Targets no longer measured (已停测) are left out unless the switch shows them, in every server's.
// Read when scrolled to, then each minute; for a while after its targets are saved, every 15 seconds until
// each measured has its first minute.
const props = defineProps({server: {type: Object, required: true}, settings: {type: Object, required: true}, servers: {type: Array, default: () => []},
  active: Boolean, phone: Boolean});
const emit = defineEmits(['settings', 'changed']);
const el = ref(null), range = ref('24h'), data = ref(null), error = ref(''), picked = ref(null), editing = ref(false);
const {visible, seen} = useVisible(el);
const latency = computed(() => props.settings.latency || {});
const front = computed(() => Boolean(props.server.profile?.front?.exits?.length));
const STOPPED_KEY = 'ob.servers.latency.stopped';
const showStopped = ref((() => { try { return localStorage.getItem(STOPPED_KEY) === '1'; } catch { return false; } })());
watch(showStopped, on => { try { if (on) localStorage.setItem(STOPPED_KEY, '1'); else localStorage.removeItem(STOPPED_KEY); } catch { /* storage may be blocked */ } });
let alive = true, mine = 0;

async function load() {
  if (!latency.value.enabled) return;
  const id = ++mine, want = range.value;
  try {
    const result = ok(await Api.serverLatency(props.server.id, want));
    if (alive && id === mine) { data.value = {...result, range: want}; error.value = ''; }
  } catch (e) { if (alive && id === mine) error.value = safeError(e); }
}
watch(() => seen.value && props.active && latency.value.enabled, on => { if (on) void load(); }, {immediate: true});
// The range shown until the next one has come.
watch(range, () => { if (seen.value) void load(); });
// What the server measures changes with its own targets, its country, being a front, and the settings:
// read again at once after any of them is saved.
const measures = computed(() => JSON.stringify([props.server.profile?.latency ?? null, props.server.profile?.region || '',
  props.server.profile?.front?.exits || [], props.server.node?.route?.at || 0, latency.value]));
watch(measures, () => { if (seen.value && props.active) void load(); });
const stop = every(60000, () => props.active && visible.value, load);
let quickUntil = 0;
const stopQuick = every(15000, () => props.active && visible.value && Date.now() < quickUntil && targets.value.some(x => x.waiting), load);
function saved() {
  quickUntil = Date.now() + 5 * 60000;
  emit('changed');
}
onBeforeUnmount(() => { alive = false; stop(); stopQuick(); });

const weighted = (values, weights) => {
  let sum = 0, n = 0;
  values.forEach((v, i) => { if (v != null) { const w = weights ? weights[i] ?? 1 : 1; sum += v * w; n += w; } });
  return n ? sum / n : null;
};
const targets = computed(() => {
  let other = 0;
  return (data.value?.targets || []).map(target => {
    const carrier = !target.id.startsWith('c:') && CARRIER_COLORS[target.id.split('-')[1]];
    // The last sample taken: its latency, or that nothing came back.
    let k = target.avg.length - 1;
    while (k >= 0 && target.avg[k] == null && target.loss[k] == null) k--;
    return {...target, color: carrier || OTHER_COLORS[other++ % OTHER_COLORS.length], latest: k < 0 ? null : target.avg[k], cut: k >= 0 && target.loss[k] === 100,
      mean: weighted(target.avg), lossMean: weighted(target.loss), waiting: target.current && !target.update && k < 0};
  });
});
const stopped = computed(() => targets.value.filter(x => !x.current).length);
const listed = computed(() => showStopped.value ? targets.value : targets.value.filter(x => x.current));
// At first those measured now in the card's group (any, when none is marked) are drawn; one measured from
// then on, as soon as it appears; the others, and one no longer measured, only when picked.
const known = new Set();
watch(listed, list => {
  if (!list.length) return;
  const next = new Set([...(picked.value || [])].filter(id => list.some(x => x.id === id)));
  const marked = list.some(x => x.current && x.primary), first = !known.size;
  for (const x of list) {
    if (x.current && !known.has(x.id) && (!first || !marked || x.primary)) next.add(x.id);
    known.add(x.id);
  }
  if (!next.size) next.add(list[0].id);
  picked.value = next;
});
function toggle(id) {
  const next = new Set(picked.value);
  if (next.has(id)) { if (next.size > 1) next.delete(id); } else next.add(id);
  picked.value = next;
}
const shown = computed(() => listed.value.filter(x => picked.value?.has(x.id)));
const lines = computed(() => shown.value.map(x => ({key: x.id, label: x.label, color: x.color, values: x.avg})));
const lanes = computed(() => shown.value.map(x => ({key: x.id, label: x.label, color: x.color, values: x.loss})));
const domain = computed(() => data.value ? [data.value.start, data.value.end] : null);
// Behind the lines: where every target shown was cut (all it sent lost) or not measured; before the
// record began, a note rather than a gap.
const bands = computed(() => {
  const d = data.value, list = shown.value, out = [];
  if (!d?.t.length || !list.length) return out;
  const {t, step} = d;
  const add = (from, to, kind, label) => {
    const last = out.at(-1);
    if (last && last.kind === kind && from - last.to <= 1) last.to = to;
    else out.push({from, to, kind, label});
  };
  if (t[0] - d.start > step * 1.5) out.push({from: d.start, to: t[0], kind: 'before', label: '还没有记录'});
  t.forEach((ts, i) => {
    if (i > 0 && ts - t[i - 1] > step * 1.5) add(t[i - 1] + step, ts, 'gap', '无数据');
    const states = list.map(x => x.avg[i] != null ? 'up' : x.loss[i] === 100 ? 'down' : 'none');
    if (!states.includes('up')) add(ts, ts + step, states.includes('down') ? 'down' : 'gap', states.includes('down') ? '断连' : '无数据');
  });
  const end = t.at(-1) + step;
  if (d.end - end > step * 1.5) add(end, d.end, 'gap', '无数据');
  return out;
});
const rowsAt = i => shown.value.map(x => ({key: x.id, label: x.label, color: x.color,
  text: x.avg[i] != null ? `${ms(x.avg[i])}${x.loss[i] ? ` · 丢 ${lossText(x.loss[i])}` : ''}` : x.loss[i] === 100 ? '断连（全部丢失）' : '没有数据'}));
const method = computed(() => `${latency.value.method === 'icmp' ? 'ICMP' : 'TCPing'} · 每 ${latency.value.intervalS || 10} 秒`);
</script>
<template>
  <section ref="el" class="ix-card dt-section" :class="{'is-waiting': latency.enabled && !data && !error}" data-jump="latency" aria-label="延迟与丢包">
    <header class="ix-card-head">
      <h3><CardIcon :icon="Odometer" tone="orange" />延迟与丢包</h3>
      <span v-if="latency.enabled" class="instance-muted">{{ method }}{{ front ? ' · 前置机只测到出口' : '' }}</span>
      <span class="ob-grow" />
      <el-switch v-if="latency.enabled && stopped" v-model="showStopped" class="lt-stopped" size="small" :active-text="`已停测 ${stopped}`"
        aria-label="显示已停测的目标" title="显示不再监测、但还有记录的目标" />
      <div v-if="latency.enabled && data?.targets?.length" class="sv-ranges" role="tablist" aria-label="时间范围">
        <button v-for="[key, label] in LATENCY_RANGES" :key="key" type="button" role="tab" :aria-selected="range === key" @click="range = key">{{ label }}</button>
      </div>
      <button v-if="latency.enabled && !front && data" type="button" class="ix-text-button" @click="editing = true">编辑</button>
    </header>
    <p v-if="!latency.enabled" class="dt-empty">延迟监测已关闭。<button type="button" class="ix-text-button" @click="emit('settings')">去设置</button></p>
    <p v-else-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!data" class="instance-muted dt-wait" role="status">正在读取…</p>
    <p v-else-if="data.supported === false" class="dt-empty">这台服务器的节点版本还不支持延迟监测，更新节点后开始记录。</p>
    <p v-else-if="!listed.length" class="dt-empty">{{ stopped ? `这台服务器现在没有在测的目标，${stopped} 个已停测的已隐藏。` : '这台服务器没有要测的目标。' }}
      <button type="button" class="ix-text-button" @click="editing = true">编辑目标</button></p>
    <template v-else>
      <ul class="lt-targets" aria-label="目标">
        <li v-for="x in listed" :key="x.id">
          <button type="button" class="lt-target" :aria-pressed="picked?.has(x.id)" :style="{'--c': x.color}" :title="x.host ? `${x.label} · ${x.host}` : x.label" @click="toggle(x.id)">
            <span class="lt-name"><i aria-hidden="true" />{{ x.label }}<small v-if="!x.current">已停测</small></span>
            <template v-if="x.update">
              <strong>—</strong><small>更新节点后开始测 HTTP</small>
            </template>
            <template v-else-if="x.waiting">
              <strong>—</strong><small>{{ data.range === '1h' || data.range === '24h' ? '等待第一组数据，约 1 分钟' : '还没有数据（这个范围按小时汇总）' }}</small>
            </template>
            <template v-else>
              <strong :data-tone="x.cut ? 'bad' : latencyTone(x.latest)">{{ x.cut ? '断连' : x.latest == null ? '—' : ms(x.latest) }}</strong>
              <small>平均 {{ x.mean == null ? '—' : ms(x.mean) }} · 丢包 <b :data-tone="lossTone(x.lossMean)">{{ lossText(x.lossMean) }}</b></small>
            </template>
          </button>
        </li>
      </ul>
      <div class="dt-chart">
        <TrendChart :key="`lat-${data.range}`" title="延迟" note="下方每条是一个目标的丢包" :t="data.t" :step="data.step" :series="lines" :format="ms" :legend="false" :areas="false"
          :domain="domain" :bands="bands" :lanes="lanes" :rows="rowsAt" :height="phone ? 150 : 180" empty="这段时间还没有探测结果。" />
        <div class="lt-legend" aria-label="图例">
          <p><b>丢包</b><span v-for="[key, name] in LOSS_LEVELS" :key="key" class="lt-key"><i :data-loss="key" aria-hidden="true" />{{ name }}</span></p>
          <p><b>背景</b><span class="lt-key"><i data-band="down" aria-hidden="true" />所选目标全部断连</span><span class="lt-key"><i data-band="gap" aria-hidden="true" />没有数据</span></p>
        </div>
      </div>
    </template>
    <LatencyEditor v-if="!front" v-model="editing" :server="server" :servers="servers" :settings="settings" :phone="phone" @changed="saved" />
  </section>
</template>
