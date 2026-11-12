<script setup>
import { computed, ref, watch } from 'vue';
import { relativeTime } from '../instances/instanceHelpers.js';
import PlatformMark from './PlatformMark.vue';
import { nodeStateTag } from './nodeVersions.js';
import { CELL_ICONS, latencyTone, lossText, lossTone, ms, pingTitle } from './detail/detailHelpers.js';
import PingStrip from './PingStrip.vue';
import { useVisible } from './detail/useVisible.js';
import { addressNetworks, asnText, bytes, canDesktop as checkDesktop, daysLeft, flag, percent, platformOf, rate, regionName, routeView, sparkPath, tone, unlockServices } from './serverHelpers.js';

// A server on the board: its name with what needs attention beside it, then its figures as pairs in two
// aligned columns (name on the left, value on the right, a bar under those that fill up), and what it is
// filed under at the bottom. sortable: the flag is the handle the board drags the card by.
// ping: its last half hour of latency (one cell a minute), when that is measured.
const props = defineProps({item: {type: Object, required: true}, now: {type: Number, required: true}, spark: {type: Object, default: null},
  ping: {type: Object, default: null}, sortable: Boolean});
const emit = defineEmits(['open', 'terminal', 'desktop', 'bgp']);
// Off screen a card keeps what it last showed and is not redrawn with every live sample; it reads them
// again as it comes back near the screen.
const root = ref(null);
const {visible} = useVisible(root, {margin: 200});
const held = ref({m: props.item.live?.m || null, spark: props.spark});
watch(visible, on => { if (!on) held.value = {m: props.item.live?.m || null, spark: props.spark && {rx: [...props.spark.rx], tx: [...props.spark.tx]}}; });
const m = computed(() => (visible.value ? props.item.live?.m : held.value.m) || null);
const sparkNow = computed(() => visible.value ? props.spark : held.value.spark);
const info = computed(() => props.item.node?.info || {});
const billing = computed(() => props.item.profile.billing || {});
const state = computed(() => !props.item.node ? 'none' : m.value ? 'online' : 'offline');
const price = computed(() => {
  const b = billing.value;
  if (b.cycle === -1) return b.price ? `${b.currency}${b.price} 一次性` : '';
  if (!b.price) return '';
  const unit = {30: '月', 31: '月', 90: '季', 184: '半年', 365: '年', 730: '两年', 1095: '三年'}[b.cycle] || (b.cycle ? `${b.cycle}天` : '');
  return `${b.currency || ''}${b.price}${unit ? '/' + unit : ''}`;
});
const days = computed(() => billing.value.cycle === -1 ? null : daysLeft(billing.value.expiresAt, new Date(props.now)));
// Past a hundred years an expiry date only means the plan does not expire.
const expiry = computed(() => {
  const d = days.value;
  if (billing.value.cycle === -1) return {text: '一次性', tone: null};
  if (d === null) return {text: '—', tone: null};
  if (d > 36500) return {text: '长期', tone: null};
  return {text: d < 0 ? `过期 ${-d} 天` : `余 ${d} 天`, tone: d <= 3 ? 'bad' : d <= 10 ? 'warn' : null};
});
const traffic = computed(() => props.item.traffic);
const typeLabel = computed(() => ({sum: '双向', up: '上行', down: '下行', max: '取大', min: '取小'})[traffic.value?.type] || '');
// "4.0 / 750 GB": the unit once when both are in the same one.
function pair(used, limit) {
  const a = bytes(used), b = bytes(limit), [av, au] = a.split(' '), [bv, bu] = b.split(' ');
  return au === bu ? `${av} / ${bv} ${bu}` : `${a} / ${b}`;
}
const steal = computed(() => m.value && m.value.steal >= 5 ? Math.round(m.value.steal) : 0);
const versionTag = computed(() => nodeStateTag(props.item.node));
const routes = computed(() => (routeView(props.item.node)?.carriers || []).filter(c => c.line));
const canTerminal = computed(() => state.value === 'online' && props.item.node.caps?.terminal !== false);
const canDesktop = computed(() => state.value === 'online' && checkDesktop(props.item));
// The first address's ASN and operator; the title has each address's.
const network = computed(() => {
  const list = addressNetworks(props.item.node, props.item.profile?.region);
  const asn = asnText(list.find(e => e.asn || e.org));
  return asn ? {asn, title: list.map(e => `IPv${e.family} ${e.ip} · ${asnText(e) || '运营商未知'}`).join('\n')} : null;
});
const unlock = computed(() => {
  const found = unlockServices(props.item.node, props.item.profile?.region);
  if (!found) return null;
  const at = `检测于 ${relativeTime(found.at, props.now)}`;
  return {...found, items: found.items.map(s => ({...s, title: `${s.label}\n${at}`}))};
});
const span = seconds => { const d = Math.floor(seconds / 86400), h = Math.floor(seconds / 3600); return d ? `${d} 天` : h ? `${h} 小时` : `${Math.max(1, Math.floor(seconds / 60))} 分钟`; };
// Each direction on its own scale, with room above its peak: they sit in cells of their own, beside their figures.
const spark = values => values?.length > 1 ? sparkPath(values, 100, 14, Math.max(1, ...values) * 1.25) : '';

// The pairs, row by row: each cell {key, label, value} with what it has of note, bar, tone, title, spark and strip.
const cells = computed(() => {
  const live = m.value, i = info.value, t = traffic.value, rows = [];
  const pct = u => live ? Math.round(percent(u.used, u.total)) : null;
  const cpu = live ? Math.round(live.cpu) : null, mem = live ? pct(live.mem) : null, disk = live ? pct(live.disk) : null;
  rows.push([
    {key: 'cpu', label: 'CPU', note: i.cores ? `${i.cores} 核` : '', value: cpu === null ? '—' : `${cpu}%`, bar: cpu, tone: cpu === null ? null : tone(cpu),
      title: live ? (platformOf(props.item) === 'windows' ? `${live.processes} 个进程` : `负载 ${live.load.join(' / ')} · ${live.processes} 个进程`) : ''},
    {key: 'mem', label: '内存', note: i.memTotal ? bytes(i.memTotal) : '', value: mem === null ? '—' : `${mem}%`, bar: mem, tone: mem === null ? null : tone(mem),
      title: live ? `${bytes(live.mem.used)} / ${bytes(live.mem.total)}` : ''},
  ]);
  const used = t?.limit ? Math.round(percent(t.used, t.limit)) : null;
  const trafficCell = !t ? {key: 'traffic', label: '流量', value: '—'}
    : t.mode === 'metered' ? {key: 'traffic', label: '流量费', value: `≈ ${billing.value.currency || ''}${(t.cost || 0).toFixed(2)}`, title: `本期约 ${billing.value.currency || ''}${(t.cost || 0).toFixed(2)} · ${billing.value.currency || ''}${t.price}/GB`}
      : t.mode === 'unlimited' ? {key: 'traffic', label: '流量', value: `${bytes(t.used)} / ∞`, title: '不限流量'}
        : t.limit ? {key: 'traffic', label: '流量', value: pair(t.used, t.limit), bar: used, tone: tone(used), title: `本期已用 ${used}%（${typeLabel.value}）`}
          : {key: 'traffic', label: '流量', value: bytes(t.used), title: '未设额度'};
  rows.push([
    {key: 'disk', label: '硬盘', note: i.diskTotal ? bytes(i.diskTotal) : '', value: disk === null ? '—' : `${disk}%`, bar: disk, tone: disk === null ? null : tone(disk),
      title: live ? `${bytes(live.disk.used)} / ${bytes(live.disk.total)}` : ''},
    trafficCell,
  ]);
  const s = sparkNow.value;
  rows.push([
    {key: 'rx', label: '下行', value: live ? rate(live.net.rxRate) : '—', spark: live ? spark(s?.rx) : ''},
    {key: 'tx', label: '上行', value: live ? rate(live.net.txRate) : '—', spark: live ? spark(s?.tx) : ''},
  ]);
  rows.push([
    {key: 'rxSum', label: '本期下行', value: t ? bytes(t.rx) : '—'},
    {key: 'txSum', label: '本期上行', value: t ? bytes(t.tx) : '—'},
  ]);
  const p = props.ping;
  if (p) {
    rows.push([
      {key: 'latency', label: '延迟', note: p.group || '', value: p.avg == null ? '—' : ms(p.avg), tone: latencyTone(p.avg), strip: {cells: p.cells, kind: 'latency'}, title: pingTitle(p, 'latency')},
      {key: 'loss', label: '丢包', note: p.group || '', value: lossText(p.loss), tone: lossTone(p.loss), strip: {cells: p.cells, kind: 'loss'}, title: pingTitle(p, 'loss')},
    ]);
  }
  const node = props.item.node;
  rows.push([
    {key: 'expiry', label: '到期', value: expiry.value.text, tone: expiry.value.tone, title: billing.value.expiresAt && days.value <= 36500 ? `${billing.value.expiresAt} 到期` : ''},
    state.value === 'online' ? {key: 'uptime', label: '在线', value: span(live.uptime)}
      : {key: 'uptime', label: '离线', value: node?.lastSeenAtMs ? relativeTime(node.lastSeenAtMs, props.now) : '尚未连接', tone: 'bad'},
  ]);
  return rows;
});
// What needs a look, as marks beside the name.
const marks = computed(() => [
  versionTag.value && {key: 'update', icon: 'update', tone: versionTag.value.tone || 'info', text: versionTag.value.text, title: versionTag.value.title || versionTag.value.text, state: versionTag.value.state},
  steal.value && {key: 'steal', icon: 'steal', tone: steal.value >= 20 ? 'bad' : 'warn', text: `steal ${steal.value}%`, title: `steal ${steal.value}%：CPU 被宿主机其他虚拟机占用的比例，持续偏高说明母鸡超售`},
].filter(Boolean));
const statusText = computed(() => state.value === 'online' ? '在线' : state.value === 'offline' ? '离线' : '未安装节点');
const hasFoot = computed(() => Boolean(price.value || props.item.profile.group || props.item.profile.tags?.length || routes.value.length || unlock.value));
</script>
<template>
  <article ref="root" class="pb-card" :data-state="state" tabindex="0" role="button" :aria-label="`${item.name} 详情`" @click="emit('open', item)" @keydown.enter.self="emit('open', item)">
    <header class="pb-head">
      <span class="sv-flag pb-flag" :class="{'pb-grip': sortable}" :title="sortable ? `${regionName(item.region) ? `${regionName(item.region)} · ` : ''}拖动调整顺序` : regionName(item.region)" aria-hidden="true">{{ flag(item.region) || '🖥' }}</span>
      <span class="pb-title"><strong class="pb-name" :title="item.name">{{ item.name }}</strong><PlatformMark :item="item" />
        <span v-for="mk in marks" :key="mk.key" class="pb-mark" :data-tone="mk.tone" :data-mark="mk.key" :data-node-state="mk.state || null" :title="mk.title" role="img" :aria-label="mk.text">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path v-for="d in CELL_ICONS[mk.icon]" :key="d" :d="d" /></svg></span>
        <span v-if="item.node?.joinPending" class="pb-tag" data-tone="warn" data-join-pending :title="item.node.joinPending.name ? `和「${item.node.joinPending.name}」像是同一台，打开详情确认` : '打开详情确认'">待确认接入</span>
      </span>
      <button v-if="canDesktop" type="button" class="pb-icon pb-desktop" title="打开远程桌面" aria-label="打开远程桌面" @click.stop="emit('desktop', item)">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="2" /><path d="M8 21h8M12 17v4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" /></svg>
      </button>
      <button v-if="canTerminal" type="button" class="pb-icon" title="打开终端" aria-label="打开终端" @click.stop="emit('terminal', item)">&gt;_</button>
      <span class="pb-dot" :data-tone="{online: 'ok', offline: 'bad', none: 'off'}[state]" :title="statusText" aria-hidden="true"></span>
    </header>
    <button v-if="network" type="button" class="pb-asn" :title="`${network.title}\n点开看 BGP 上游`" data-network @click.stop="emit('bgp', item, $event.currentTarget)"
      ><svg viewBox="0 0 24 24" aria-hidden="true"><path v-for="d in CELL_ICONS.bgp" :key="d" :d="d" /></svg><span class="pb-asn-org">{{ network.asn }}</span></button>
    <div v-if="item.node" class="pb-cells">
      <template v-for="(row, r) in cells" :key="r">
        <div v-for="c in row" :key="c.key" class="pb-cell" :data-cell="c.key" :data-tone="c.tone || null" :title="c.title || null">
          <span class="pb-cell-name"><svg viewBox="0 0 24 24" aria-hidden="true"><path v-for="d in CELL_ICONS[c.key]" :key="d" :d="d" /></svg>{{ c.label }}<small v-if="c.note">{{ c.note }}</small></span>
          <b class="pb-cell-value">{{ c.value }}</b>
          <i v-if="c.bar != null" class="pb-cell-bar" aria-hidden="true"><b :style="{width: `${Math.min(c.bar, 100)}%`}" /></i>
          <PingStrip v-else-if="c.strip" :cells="c.strip.cells" :kind="c.strip.kind" />
          <svg v-else-if="c.spark" class="pb-spark" viewBox="0 0 100 14" preserveAspectRatio="none" aria-hidden="true"><path :d="c.spark" :class="c.key === 'rx' ? 'is-rx' : 'is-tx'" vector-effect="non-scaling-stroke" /></svg>
        </div>
      </template>
    </div>
    <template v-else>
      <p class="pb-empty">安装节点后上报运行状态 · 点击安装</p>
      <div class="pb-cells"><div class="pb-cell" data-cell="expiry" :data-tone="expiry.tone || null"><span class="pb-cell-name"><svg viewBox="0 0 24 24" aria-hidden="true"><path v-for="d in CELL_ICONS.expiry" :key="d" :d="d" /></svg>到期</span><b class="pb-cell-value">{{ expiry.text }}</b></div></div>
    </template>
    <footer v-if="hasFoot" class="pb-foot">
      <div v-if="price || item.profile.group || item.profile.tags?.length || routes.length" class="pb-tags">
        <span v-if="price" class="pb-tag" data-price>{{ price }}</span>
        <span v-if="item.profile.group" class="pb-tag is-plain">{{ item.profile.group }}</span>
        <span v-for="tag in item.profile.tags || []" :key="tag" class="pb-tag is-plain">{{ tag }}</span>
        <span v-for="c in routes" :key="c.id" class="pb-tag sv-route-tag" :data-tier="c.tier" :data-route="c.id" :title="c.title">{{ c.name }} {{ c.line }}</span>
      </div>
      <p v-if="unlock" class="pb-unlock" data-unlock>
        <span v-for="svc in unlock.items" :key="svc.id" class="sv-svc" :data-service="svc.id" :data-state="svc.state" :title="svc.title" :aria-label="svc.title"
          :style="{'--svc': svc.color, '--svc-dark': svc.dark || svc.color}">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path :d="svc.path" :fill-rule="svc.evenodd ? 'evenodd' : null" /></svg>
          <b v-if="svc.mark" :data-tone="svc.mark.tone">{{ svc.mark.text }}</b>
        </span>
      </p>
    </footer>
  </article>
</template>
