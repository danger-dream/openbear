<script setup>
import { computed, ref, useId } from 'vue';
import LossLane from './LossLane.vue';

// A chart for the detail page: lines (smoothed without overshooting) or grouped bars, a value scale on the
// left, times underneath, a legend that hides and shows each series, and a crosshair with every value at
// that point. Lines break where a value is missing (null) or samples stop for a while (the node was away).
// Labels live outside the SVG, so stretching the drawing never stretches the text.
// A series is {key, label, color, values, format?, hidden?}: its own format for the tooltip (a line drawn on
// another scale, such as load as a share of the cores), and hidden to start switched off.
const props = defineProps({
  t: {type: Array, required: true},
  step: {type: Number, default: 60},
  series: {type: Array, required: true},
  format: {type: Function, default: v => String(v)},
  // How a point is named under the chart and in the tooltip; by default its time, as fine as the step.
  when: {type: Function, default: null},
  max: {type: Number, default: 0},
  height: {type: Number, default: 180},
  kind: {type: String, default: 'line'},
  // Bars placed by time, under a line chart of the same samples, rather than in equal slots.
  timed: Boolean,
  legend: {type: Boolean, default: true},
  // The heading over the chart, with the legend beside it; label names the drawing for screen readers.
  title: {type: String, default: ''},
  note: {type: String, default: ''},
  label: {type: String, default: ''},
  empty: {type: String, default: '暂无数据'},
  // 'bytes' or 'rate': the scale counts in one binary unit (0 / 0.6 / 1.2 / 1.8 / 2.4 MB/s) rather than each label its own.
  unit: {type: String, default: ''},
  // A soft fill under each line; off where several lines would muddy each other.
  areas: {type: Boolean, default: true},
  // The last bar is the current hour, day or month, still counting.
  partial: Boolean,
  // The time the axis covers, [start, end] in unix seconds, where the range asked for matters more than
  // the samples there are (a record only begun shows at the right, not stretched across).
  domain: {type: Array, default: null},
  // Spans of time marked behind the lines: [{from, to, kind, label?}], kind 'down', 'gap' or 'before'.
  bands: {type: Array, default: () => []},
  // A row under the chart per series, each a strip of its loss along the same axis: [{key, label, color, values}].
  lanes: {type: Array, default: () => []},
  // The tooltip's rows at a point, in place of each series' value: (index) => [{key, label, color, text}].
  rows: {type: Function, default: null},
});
const W = 1000, PAD = 6;
const id = useId();
const off = ref(new Set(props.series.filter(s => s.hidden).map(s => s.key)));
const hover = ref(-1);
const H = computed(() => props.height);
const shown = computed(() => props.series.filter(s => !off.value.has(s.key)));
function toggle(key) {
  const next = new Set(off.value);
  if (next.has(key)) next.delete(key);
  // The last one shown stays: an empty chart says nothing.
  else if (props.series.length - next.size > 1) next.add(key);
  off.value = next;
}
// Bytes count in the largest binary unit the peak reaches; other values as they are.
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
const scale = computed(() => {
  let peak = 0;
  for (const s of shown.value) for (const v of s.values) if (v > peak) peak = v;
  const k = props.unit && peak >= 1024 ? Math.min(Math.floor(Math.log(peak) / Math.log(1024)), UNITS.length - 1) : 0;
  return {peak, base: 1024 ** k, name: props.unit ? UNITS[k] + (props.unit === 'rate' ? '/s' : '') : ''};
});
// The scale's quarters are round numbers in that unit.
const top = computed(() => {
  if (props.max) return props.max;
  const {peak, base} = scale.value;
  if (!peak) return 1;
  const quarter = peak * 1.08 / 4 / base, magnitude = 10 ** Math.floor(Math.log10(quarter));
  return [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(m => m * magnitude >= quarter) * magnitude * 4 * base;
});
// A short chart labels only its ends.
const ticks = computed(() => props.height < 100 ? [0, 1] : [0, 0.25, 0.5, 0.75, 1]);
const grid = computed(() => props.height < 100 ? [0.5, 1] : [0.25, 0.5, 0.75, 1]);
const axis = v => props.unit ? `${+(v / scale.value.base).toFixed(2)} ${scale.value.name}` : props.format(v);
// The widest scale label, laid out unseen so the scale column is as wide as it needs.
const widest = computed(() => ticks.value.map(f => axis(top.value * f)).reduce((a, b) => b.length > a.length ? b : a, ''));
const y = v => H.value - PAD - Math.min(Math.max(v, 0), top.value) / top.value * (H.value - PAD * 2);
const n = computed(() => props.t.length);
// Lines run edge to edge by time; bars sit in equal slots.
const span = computed(() => {
  if (props.domain) return {first: props.domain[0], width: Math.max(props.domain[1] - props.domain[0], props.step)};
  const first = props.t[0] ?? 0, last = props.t[n.value - 1] ?? first;
  return {first, width: Math.max(last - first, props.step)};
});
const at = ts => Math.min(100, Math.max(0, (ts - span.value.first) / span.value.width * 100));
const shownBands = computed(() => props.bands.map((b, i) => {
  const left = at(b.from), width = at(b.to) - left;
  return {...b, key: `${b.kind}-${i}`, left, width, labelled: Boolean(b.label) && width >= 2.5};
}).filter(b => b.width > 0));
const byTime = computed(() => props.kind !== 'bar' || props.timed);
const slot = computed(() => byTime.value ? W / (Math.round(span.value.width / props.step) + 1) : W / Math.max(n.value, 1));
const x = i => byTime.value ? (props.t[i] - span.value.first) / span.value.width * W : slot.value * (i + 0.5);

// Runs of points with a value and no gap between them, as [[index, value], ...].
function runs(values) {
  const out = [];
  let run = [];
  for (let i = 0; i < n.value; i++) {
    const v = values[i];
    const gap = i > 0 && props.t[i] - props.t[i - 1] > props.step * 1.5;
    if (v == null || gap) { if (run.length) out.push(run); run = []; }
    if (v != null) run.push([x(i), y(v)]);
  }
  if (run.length) out.push(run);
  return out;
}
// Monotone cubic (Fritsch–Carlson): smooth, and never above or below the points it joins.
function curve(points) {
  const f = v => v.toFixed(1);
  if (points.length === 1) return `M${f(points[0][0] - 2)},${f(points[0][1])}H${f(points[0][0] + 2)}`;
  const m = points.length, d = [], slopes = [];
  for (let i = 0; i < m - 1; i++) d.push((points[i + 1][1] - points[i][1]) / ((points[i + 1][0] - points[i][0]) || 1));
  for (let i = 0; i < m; i++) {
    if (i === 0) slopes.push(d[0]);
    else if (i === m - 1) slopes.push(d[m - 2]);
    else slopes.push(d[i - 1] * d[i] <= 0 ? 0 : 2 / (1 / d[i - 1] + 1 / d[i]));
  }
  let path = `M${f(points[0][0])},${f(points[0][1])}`;
  for (let i = 0; i < m - 1; i++) {
    const [x0, y0] = points[i], [x1, y1] = points[i + 1], dx = (x1 - x0) / 3;
    path += `C${f(x0 + dx)},${f(y0 + slopes[i] * dx)},${f(x1 - dx)},${f(y1 - slopes[i + 1] * dx)},${f(x1)},${f(y1)}`;
  }
  return path;
}
const lines = computed(() => props.kind !== 'line' ? [] : shown.value.map(s => {
  const parts = runs(s.values);
  const line = parts.map(curve).join('');
  const base = H.value - PAD;
  const area = !props.areas ? '' : parts.filter(p => p.length > 1).map(p => `${curve(p)}L${p[p.length - 1][0].toFixed(1)},${base}L${p[0][0].toFixed(1)},${base}Z`).join('');
  return {key: s.key, color: s.color, line, area};
}));
const bars = computed(() => {
  if (props.kind !== 'bar' || !shown.value.length) return [];
  const group = slot.value * 0.7, width = group / shown.value.length, out = [];
  shown.value.forEach((s, k) => {
    for (let i = 0; i < n.value; i++) {
      const v = s.values[i];
      if (!v) continue;
      const top0 = y(v);
      out.push({key: `${s.key}-${i}`, i, color: s.color, x: x(i) - group / 2 + k * width, y: top0, w: Math.max(width - 1.5, 1), h: H.value - PAD - top0});
    }
  });
  return out;
});

const timeText = ts => new Date(ts * 1000).toLocaleString('zh-CN', props.step >= 86400 ? {month: 'numeric', day: 'numeric'}
  : props.step >= 3600 ? {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}
    : props.step < 60 ? {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false} : {hour: '2-digit', minute: '2-digit', hour12: false});
const name = i => props.when ? props.when(props.t[i], i) : timeText(props.t[i]);
// Five times under a line chart; under bars, at most eight of their own names.
const xLabels = computed(() => {
  if (props.domain) return [0, 0.25, 0.5, 0.75, 1].map((f, i) => ({i, left: f * 100, text: timeText(span.value.first + f * span.value.width)}));
  if (!n.value) return [];
  const count = props.kind === 'bar' ? Math.min(n.value, 8) : Math.min(n.value, 5);
  const picks = count === 1 ? [0] : Array.from({length: count}, (_, k) => Math.round(k * (n.value - 1) / (count - 1)));
  return [...new Set(picks)].map(i => ({i, left: x(i) / W * 100, text: name(i)}));
});

function nearest(event) {
  if (!n.value) return;
  const box = event.currentTarget.getBoundingClientRect();
  const px = (event.clientX - box.left) / box.width * W;
  if (!byTime.value) { hover.value = Math.min(n.value - 1, Math.max(0, Math.floor(px / slot.value))); return; }
  const ts = span.value.first + px / W * span.value.width;
  let lo = 0, hi = n.value - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (props.t[mid] < ts) lo = mid; else hi = mid; }
  hover.value = Math.abs(props.t[lo] - ts) <= Math.abs(props.t[hi] - ts) ? lo : hi;
}
const tip = computed(() => {
  const i = hover.value;
  if (i < 0 || i >= n.value) return null;
  const left = x(i) / W * 100;
  const rows = props.rows ? props.rows(i) : shown.value.map(s => ({key: s.key, label: s.label, color: s.color, text: s.values[i] == null ? '—' : (s.format || props.format)(s.values[i])}));
  return {left, flip: left > 62, title: name(i), rows};
});
</script>
<template>
  <figure class="tc" :style="{'--tc-h': `${height}px`}">
    <div v-if="title || legend && series.length > 1" class="tc-head">
      <h4 v-if="title">{{ title }}<small v-if="note">{{ note }}</small></h4>
      <div v-if="legend && series.length > 1" class="tc-legend" role="group" :aria-label="`${label || title}：显示哪些`">
        <button v-for="s in series" :key="s.key" type="button" :aria-pressed="!off.has(s.key)" :style="{'--c': s.color}" @click="toggle(s.key)"><i aria-hidden="true" />{{ s.label }}</button>
      </div>
    </div>
    <div v-if="!n" class="tc-empty">{{ empty }}</div>
    <div v-else class="tc-body">
      <div class="tc-y" aria-hidden="true"><i class="tc-y-size">{{ widest }}</i><span v-for="f in ticks" :key="f" :style="{bottom: `${(f * (height - PAD * 2) + PAD) / height * 100}%`}">{{ axis(top * f) }}</span></div>
      <div class="tc-plot" @pointermove="nearest" @pointerdown="nearest" @pointerleave="hover = -1">
        <div v-for="b in shownBands" :key="b.key" class="tc-band" :data-band="b.kind" :style="{left: `${b.left}%`, width: `${b.width}%`}" :title="b.label || null">
          <span v-if="b.labelled">{{ b.label }}</span></div>
        <svg :viewBox="`0 0 ${W} ${height}`" preserveAspectRatio="none" role="img" :aria-label="label || title">
          <defs>
            <linearGradient v-for="l in lines" :id="`${id}-${l.key}`" :key="l.key" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" :style="{stopColor: l.color, stopOpacity: 0.2}" /><stop offset="1" :style="{stopColor: l.color, stopOpacity: 0}" />
            </linearGradient>
          </defs>
          <line v-for="f in grid" :key="f" x1="0" :x2="W" :y1="y(top * f)" :y2="y(top * f)" class="tc-grid" vector-effect="non-scaling-stroke" />
          <line x1="0" :x2="W" :y1="height - PAD" :y2="height - PAD" class="tc-base" vector-effect="non-scaling-stroke" />
          <template v-for="l in lines" :key="l.key">
            <path v-if="l.area" :d="l.area" :fill="`url(#${id}-${l.key})`" />
            <path :d="l.line" :style="{stroke: l.color}" class="tc-line" vector-effect="non-scaling-stroke" />
          </template>
          <rect v-for="b in bars" :key="b.key" :x="b.x" :y="b.y" :width="b.w" :height="b.h" :style="{fill: b.color}" rx="1.5" class="tc-bar" :class="{'is-dim': hover >= 0 && b.i !== hover, 'is-partial': partial && b.i === n - 1}" />
          <line v-if="tip && kind === 'line'" :x1="x(hover)" :x2="x(hover)" y1="0" :y2="height" class="tc-cursor" vector-effect="non-scaling-stroke" />
        </svg>
        <div v-if="tip" class="tc-tip" :class="{'is-flip': tip.flip}" :style="{left: `${tip.left}%`}" role="status">
          <time>{{ tip.title }}</time>
          <span v-for="r in tip.rows" :key="r.key" :style="{'--c': r.color}"><i aria-hidden="true" />{{ r.label }}<b>{{ r.text }}</b></span>
        </div>
      </div>
      <template v-for="(lane, k) in lanes" :key="lane.key">
        <div class="tc-lane-label" :style="{gridRow: k + 2, '--c': lane.color}" :title="lane.label"><i aria-hidden="true" />{{ lane.label }}</div>
        <div class="tc-lane" :class="{'is-first': k === 0}" :style="{gridRow: k + 2}" @pointermove="nearest" @pointerdown="nearest" @pointerleave="hover = -1">
          <LossLane :t="t" :step="step" :values="lane.values" :first="span.first" :width="span.width" :label="`${lane.label}的丢包`" />
        </div>
      </template>
      <div class="tc-x" :style="{gridRow: lanes.length + 2}" aria-hidden="true"><span v-for="l in xLabels" :key="l.i" :style="{left: `${l.left}%`}">{{ l.text }}</span></div>
    </div>
  </figure>
</template>
