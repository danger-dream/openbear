<script setup>
import { computed } from 'vue';
import Spark from './Spark.vue';
import TopPopover from './TopPopover.vue';
import { COLORS, ICONS } from './detailHelpers.js';
import { bytes, percent, rate, tone } from '../serverHelpers.js';

// 处理器, 内存, 硬盘 and 网络 at a glance: the figure, a ring, what stands behind it, and the last ten
// minutes underneath. A card whose figure the node can rank processes by opens them.
const props = defineProps({server: {type: Object, required: true}, live: {type: Object, default: null}, recent: {type: Object, default: null},
  has: {type: Object, required: true}, platform: {type: String, default: ''}, phone: Boolean, busy: Boolean});
const emit = defineEmits(['calibrate']);
const RING = 2 * Math.PI * 22;
const info = computed(() => props.server.node?.info || {});
// Linux ranks by all of them; macOS and Windows by CPU and memory.
const sortable = sort => sort && (props.platform === 'linux' || sort === 'cpu' || sort === 'mem') ? sort : '';
const p = computed(() => props.recent || {});
const sumOf = (a = [], b = []) => a.map((v, i) => v + (b[i] || 0));
const traffic = computed(() => props.server.traffic || null);
const trafficPct = computed(() => traffic.value?.limit ? Math.round(percent(traffic.value.used, traffic.value.limit)) : null);

const cards = computed(() => {
  const m = props.live;
  if (!m) return [];
  const steal = props.has.wait ? m.steal : 0;
  const swapPct = m.swap.total ? Math.round(percent(m.swap.used, m.swap.total)) : null;
  return [
    {key: 'cpu', label: '处理器', value: Math.round(m.cpu), sort: sortable('cpu'),
      lines: [[info.value.cores ? `${info.value.cores} 核` : '', props.has.load ? `负载 ${m.load.map(v => v.toFixed(2)).join(' / ')}` : `${m.processes} 个进程`].filter(Boolean).join(' · '),
        props.has.wait ? `steal ${m.steal}% · iowait ${m.iowait}%` : props.has.load ? `${m.processes} 个进程` : ''],
      alarm: steal >= 20 ? 'bad' : steal >= 5 ? 'warn' : '', spark: [{values: p.value.cpu || [], color: COLORS.cpu}], max: 100},
    {key: 'mem', label: '内存', value: Math.round(percent(m.mem.used, m.mem.total)), sort: sortable('mem'),
      lines: [`${bytes(m.mem.used)} / ${bytes(m.mem.total)}`, m.swap.total ? `交换 ${bytes(m.swap.used)} / ${bytes(m.swap.total)} · ${swapPct}%` : '交换未启用'],
      spark: [{values: p.value.mem || [], color: COLORS.mem}], max: 100},
    {key: 'disk', label: '硬盘', value: Math.round(percent(m.disk.used, m.disk.total)), sort: sortable(props.has.io ? 'io' : ''),
      lines: [`${bytes(m.disk.used)} / ${bytes(m.disk.total)}`, props.has.io ? `读 ${rate(m.io.readRate)} · 写 ${rate(m.io.writeRate)}` : '根分区'],
      spark: props.has.io ? [{values: sumOf(p.value.ioRead, p.value.ioWrite), color: COLORS.write}] : [{values: p.value.disk || [], color: COLORS.write}], max: props.has.io ? 0 : 100},
  ].map(c => ({...c, tone: tone(c.value)}));
});
const net = computed(() => {
  const m = props.live;
  return m && {sort: sortable('net'), rx: rate(m.net.rxRate), tx: rate(m.net.txRate),
    spark: [{values: p.value.rxRate || [], color: COLORS.rx}, {values: p.value.txRate || [], color: COLORS.tx}]};
});
</script>
<template>
  <div v-if="live" class="ov-grid">
    <TopPopover v-for="c in cards" :key="c.key" v-slot="{open, tap}" :server="server" :sort="c.sort" :phone="phone">
      <component :is="c.sort ? 'button' : 'div'" :type="c.sort ? 'button' : undefined" class="ov-card" :class="{'is-action': c.sort, 'is-open': open}"
        :data-kind="c.key" :data-tone="c.tone" :aria-label="c.sort ? `${c.label} ${c.value}%，查看占用最高的进程` : undefined" @click="tap">
        <span class="ov-head"><span class="ov-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path v-for="d in ICONS[c.key]" :key="d" :d="d" /></svg></span>{{ c.label }}
          <b v-if="c.alarm" class="ov-alarm" :data-tone="c.alarm">steal 高</b></span>
        <span class="ov-main">
          <span class="ov-figure"><strong>{{ c.value }}<small>%</small></strong>
            <small v-for="(line, i) in c.lines.filter(Boolean)" :key="i" :title="line">{{ line }}</small></span>
          <svg class="ov-ring" viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="22" /><circle v-if="c.value > 0" cx="26" cy="26" r="22" :stroke-dasharray="`${Math.min(c.value, 100) / 100 * RING} ${RING}`" /></svg>
        </span>
        <Spark :lines="c.spark" :max="c.max" />
      </component>
    </TopPopover>
    <TopPopover v-slot="{open, tap}" :server="server" :sort="net.sort" :phone="phone">
      <div class="ov-card" :class="{'is-action': net.sort, 'is-open': open}" data-kind="net" :data-tone="trafficPct === null ? null : tone(trafficPct)" @click="tap">
        <button v-if="net.sort" type="button" class="ov-hit" aria-label="网络，查看连接最多的进程" />
        <span class="ov-head"><span class="ov-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path v-for="d in ICONS.net" :key="d" :d="d" /></svg></span>网络
          <button v-if="traffic" type="button" class="ov-link" :disabled="busy" title="按服务商面板上的数值校准本期流量" @click.stop="emit('calibrate')">校准</button></span>
        <span class="ov-main">
          <span class="ov-figure">
            <strong class="ov-rates"><span><i class="is-rx" aria-label="下行">↓</i>{{ net.rx }}</span><span><i class="is-tx" aria-label="上行">↑</i>{{ net.tx }}</span></strong>
            <template v-if="traffic">
              <small data-period>本期 <b>{{ bytes(traffic.used) }}</b>{{ traffic.limit ? ` / ${bytes(traffic.limit)}` : traffic.mode === 'unlimited' ? ' · 不限流量' : '' }}<b v-if="trafficPct !== null" class="ov-pct"> · {{ trafficPct }}%</b></small>
              <small>下行 {{ bytes(traffic.rx) }} · 上行 {{ bytes(traffic.tx) }}</small>
            </template>
            <small v-else>本期流量还没有统计</small>
          </span>
          <svg v-if="trafficPct !== null" class="ov-ring is-traffic" viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="22" /><circle v-if="trafficPct > 0" cx="26" cy="26" r="22" :stroke-dasharray="`${Math.min(trafficPct, 100) / 100 * RING} ${RING}`" />
            <text x="26" y="30" transform="rotate(90 26 26)">{{ trafficPct }}%</text></svg>
        </span>
        <Spark :lines="net.spark" />
      </div>
    </TopPopover>
  </div>
</template>
