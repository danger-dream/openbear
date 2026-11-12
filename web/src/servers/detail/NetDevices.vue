<script setup>
import { Connection } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Api } from '../../api.js';
import TrendChart from './TrendChart.vue';
import TopPopover from './TopPopover.vue';
import { COLORS } from './detailHelpers.js';
import { every, useVisible } from './useVisible.js';
import { bytes } from '../serverHelpers.js';

// 网络设备: connections now (TCP split into established and listening where the node tells), their history
// over the page's range, and each interface's traffic since boot (read from the node when scrolled to, then each minute).
const props = defineProps({server: {type: Object, required: true}, live: {type: Object, default: null}, series: {type: Object, default: null},
  rangeLabel: {type: String, default: ''}, online: Boolean, active: Boolean, phone: Boolean, platform: {type: String, default: ''}});
const el = ref(null), nics = ref(null), all = ref(false);
const {visible, seen} = useVisible(el);
let alive = true;

async function load() {
  if (!props.online) return;
  try {
    const result = await Api.serverDetails(props.server.id);
    if (alive) nics.value = result.details?.interfaces || [];
  } catch { if (alive && !nics.value) nics.value = []; }
}
watch(() => seen.value && props.active && props.online, on => { if (on) void load(); }, {immediate: true});
const stop = every(60000, () => props.active && visible.value && props.online, load);
onBeforeUnmount(() => { alive = false; stop(); });

const counts = computed(() => {
  const m = props.live;
  if (!m) return [];
  return [['TCP', m.tcp, 'tcp'], m.tcpEstab != null && ['已建立', m.tcpEstab], m.tcpListen != null && ['监听', m.tcpListen], ['UDP', m.udp], ['进程', m.processes]].filter(Boolean);
});
const connSort = computed(() => props.platform === 'linux' ? 'conn' : '');
const points = computed(() => props.series?.points || null);
const lines = computed(() => points.value ? [{key: 'tcp', label: 'TCP', color: COLORS.tcp, values: points.value.tcp}, {key: 'udp', label: 'UDP', color: COLORS.udp, values: points.value.udp}] : []);
// Counters left out for every interface were not read (a macOS node): unknown, not zero.
const counted = computed(() => (nics.value || []).some(n => n.rx || n.tx));
// Busiest first; interfaces that never moved a byte (down, or idle bridges) at the end.
const sorted = computed(() => [...(nics.value || [])].sort((a, b) => (b.rx || 0) + (b.tx || 0) - (a.rx || 0) - (a.tx || 0)));
const FEW = 4;
const listed = computed(() => all.value ? sorted.value : sorted.value.slice(0, FEW));
const most = computed(() => Math.max(1, ...sorted.value.map(n => (n.rx || 0) + (n.tx || 0))));
const speed = mbps => !mbps ? '' : mbps >= 1000 ? `${mbps / 1000} Gbps` : `${mbps} Mbps`;
</script>
<template>
  <section ref="el" class="ix-card dt-section" :class="{'is-waiting': online && !nics}" data-jump="net" aria-label="网络设备">
    <header class="ix-card-head"><h3><CardIcon :icon="Connection" />网络设备</h3><span class="instance-muted">{{ live ? '连接数每 3 秒更新' : '节点离线' }}</span></header>
    <div v-if="counts.length" class="dt-counts" data-counts>
      <template v-for="[label, value, kind] in counts" :key="label">
        <TopPopover v-if="kind === 'tcp' && connSort" v-slot="{open, tap}" :server="server" :sort="connSort" :phone="phone">
          <button type="button" class="dt-count is-action" :class="{'is-open': open}" aria-label="TCP 连接，查看连接最多的进程" @click="tap"><small>{{ label }}</small><strong>{{ value }}</strong></button>
        </TopPopover>
        <span v-else class="dt-count"><small>{{ label }}</small><strong>{{ value }}</strong></span>
      </template>
    </div>
    <div v-if="series" class="dt-chart">
      <TrendChart :key="`conn-${series.range}`" title="连接数" :note="rangeLabel" :t="points.t" :step="series.step" :series="lines" :format="v => String(Math.round(v))" :height="phone ? 110 : 120" />
    </div>
    <div class="dt-nics">
      <h4>网卡<small>{{ !nics?.length || counted ? '开机以来' : '节点没有提供各网卡的流量' }}</small></h4>
      <p v-if="!online && !nics" class="instance-muted">节点在线时读取。</p>
      <p v-else-if="!nics" class="instance-muted" role="status">正在读取…</p>
      <p v-else-if="!nics.length" class="instance-muted">没有读到网卡。</p>
      <ul v-else class="dt-nic-list">
        <li v-for="n in listed" :key="n.name" :data-up="n.up ? 'up' : 'down'">
          <span class="dt-nic-name"><i aria-hidden="true" /><b class="mono">{{ n.name }}</b><small v-if="speed(n.speed)">{{ speed(n.speed) }}</small></span>
          <span v-if="counted" class="dt-nic-bytes"><span><i class="is-rx" aria-label="下行">↓</i>{{ bytes(n.rx || 0) }}</span><span><i class="is-tx" aria-label="上行">↑</i>{{ bytes(n.tx || 0) }}</span></span>
          <i v-if="n.rx || n.tx" class="dt-nic-bar" aria-hidden="true"><b :style="{width: `${(n.rx || 0) / most * 100}%`}" /><b :style="{width: `${(n.tx || 0) / most * 100}%`}" /></i>
        </li>
      </ul>
      <button v-if="sorted.length > FEW" type="button" class="ix-text-button dt-more" @click="all = !all">{{ all ? '收起' : `显示全部 ${sorted.length} 个` }}</button>
    </div>
  </section>
</template>
