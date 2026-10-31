<script setup>
import { computed } from 'vue';
import { relativeTime } from '../instances/instanceHelpers.js';
import PlatformMark from './PlatformMark.vue';
import { nodeStateTag } from './nodeVersions.js';
import { bytes, daysLeft, flag, percent, platformOf, rate, regionName, sparkPath, tone, uptime } from './serverHelpers.js';

// sortable: the flag is the handle the board drags the card by.
const props = defineProps({item: {type: Object, required: true}, now: {type: Number, required: true}, spark: {type: Object, default: null}, sortable: Boolean});
const emit = defineEmits(['open', 'terminal']);
const m = computed(() => props.item.live?.m || null);
const info = computed(() => props.item.node?.info || {});
// Windows has no load average; the card shows its process count instead.
const windows = computed(() => platformOf(props.item) === 'windows');
const billing = computed(() => props.item.profile.billing || {});
const state = computed(() => !props.item.node ? 'none' : m.value ? 'online' : 'offline');
const bars = computed(() => [
  ['CPU', m.value ? m.value.cpu : 0],
  ['内存', m.value ? percent(m.value.mem.used, m.value.mem.total) : 0],
  ['硬盘', m.value ? percent(m.value.disk.used, m.value.disk.total) : 0],
].map(([label, value]) => [label, Math.round(value)]));
const price = computed(() => {
  const b = billing.value;
  if (b.cycle === -1) return b.price ? `${b.currency}${b.price} 一次性` : '';
  if (!b.price) return '';
  const unit = {30: '月', 31: '月', 90: '季', 184: '半年', 365: '年', 730: '两年', 1095: '三年'}[b.cycle] || (b.cycle ? `${b.cycle}天` : '');
  return `${b.currency || ''}${b.price}${unit ? '/' + unit : ''}`;
});
const days = computed(() => billing.value.cycle === -1 ? null : daysLeft(billing.value.expiresAt, new Date(props.now)));
// Past a hundred years an expiry date only means the plan does not expire.
const daysTag = computed(() => days.value === null ? '' : days.value < 0 ? `过期 ${-days.value} 天` : days.value > 36500 ? '长期' : `余 ${days.value} 天`);
const traffic = computed(() => props.item.traffic);
const used = computed(() => traffic.value?.limit ? Math.round(percent(traffic.value.used, traffic.value.limit)) : null);
const ring = computed(() => `${Math.min(100, used.value || 0) * 0.942} 94.2`);
const typeLabel = computed(() => ({sum: '双向', up: '上行', down: '下行', max: '取大', min: '取小'})[traffic.value?.type] || '');
const trafficNote = computed(() => {
  const t = traffic.value, cur = billing.value.currency || '';
  if (!t) return '';
  if (t.mode === 'metered') return `本期约 ${cur}${(t.cost || 0).toFixed(2)} · ${cur}${t.price}/GB`;
  if (t.mode === 'unlimited') return '不限流量';
  return t.limit ? `共 ${bytes(t.limit)}（${typeLabel.value}）` : '';
});
const statusText = computed(() => {
  if (state.value === 'online') return `在线 ${uptime(m.value.uptime)}`;
  if (state.value === 'offline') return props.item.node.lastSeenAtMs ? `${relativeTime(props.item.node.lastSeenAtMs, props.now)}离线` : '尚未连接';
  return '未安装节点';
});
const versionTag = computed(() => nodeStateTag(props.item.node));
const hasTags = computed(() => Boolean(price.value || daysTag.value || props.item.profile.group || props.item.profile.tags?.length || steal.value || versionTag.value));
const expiryLabel = computed(() => billing.value.cycle === -1 ? '一次性付费' : billing.value.expiresAt && days.value <= 36500 ? `到期 ${billing.value.expiresAt}` : days.value > 36500 ? '长期' : '');
const canTerminal = computed(() => state.value === 'online' && props.item.node.caps?.terminal !== false);
const steal = computed(() => m.value && m.value.steal >= 5 ? Math.round(m.value.steal) : 0);
// Both directions share one scale so their heights compare.
const lines = computed(() => {
  const s = props.spark;
  if (!s || s.rx.length < 2) return null;
  const top = Math.max(1, ...s.rx, ...s.tx);
  return {rx: sparkPath(s.rx, 100, 22, top), tx: sparkPath(s.tx, 100, 22, top)};
});
</script>
<template>
  <article class="pb-card" :data-state="state" tabindex="0" role="button" :aria-label="`${item.name} 详情`" @click="emit('open', item)" @keydown.enter.self="emit('open', item)">
    <header class="pb-head">
      <span class="sv-flag pb-flag" :class="{'pb-grip': sortable}" :title="sortable ? `${regionName(item.region) ? `${regionName(item.region)} · ` : ''}拖动调整顺序` : regionName(item.region)" aria-hidden="true">{{ flag(item.region) || '🖥' }}</span>
      <span class="pb-title"><strong class="pb-name" :title="item.name">{{ item.name }}</strong><PlatformMark :item="item" /></span>
      <button v-if="canTerminal" type="button" class="pb-icon" title="打开终端" aria-label="打开终端" @click.stop="emit('terminal', item)">&gt;_</button>
      <span class="pb-dot" :data-tone="{online: 'ok', offline: 'bad', none: 'off'}[state]" :title="statusText" aria-hidden="true"></span>
    </header>
    <div v-if="hasTags || item.node" class="pb-tags">
      <span v-if="item.node?.joinPending" class="pb-tag" data-tone="warn" data-join-pending :title="item.node.joinPending.name ? `和「${item.node.joinPending.name}」像是同一台，打开详情确认` : '打开详情确认'">待确认接入</span>
      <span v-if="price" class="pb-tag">{{ price }}</span>
      <span v-if="daysTag" class="pb-tag" :data-tone="days <= 3 ? 'bad' : days <= 10 ? 'warn' : 'ok'">{{ daysTag }}</span>
      <span v-if="item.profile.group" class="pb-tag is-plain">{{ item.profile.group }}</span>
      <span v-for="tag in item.profile.tags || []" :key="tag" class="pb-tag is-plain">{{ tag }}</span>
      <span v-if="steal" class="pb-tag" :data-tone="steal >= 20 ? 'bad' : 'warn'" title="CPU 被宿主机其他虚拟机占用的比例，持续偏高说明母鸡超售">steal {{ steal }}%</span>
      <span v-if="versionTag" class="pb-tag" :class="{'is-plain': !versionTag.tone}" :data-tone="versionTag.tone || null" :data-node-state="versionTag.state" :title="versionTag.title">{{ versionTag.text }}</span>
    </div>
    <template v-if="item.node">
      <p class="pb-spec">
        <span title="CPU 核心"><svg viewBox="0 0 24 24" aria-hidden="true"><rect width="16" height="16" x="4" y="4" rx="2" /><rect width="6" height="6" x="9" y="9" rx="1" /><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2" /></svg>{{ info.cores ? `${info.cores} 核` : '—' }}</span>
        <span title="内存"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 19v-3M10 19v-3M14 19v-3M18 19v-3M8 11V9M16 11V9M12 11V9M2 15h20M2 7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v1.1a2 2 0 0 0 0 3.837V17a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-5.1a2 2 0 0 0 0-3.837Z" /></svg>{{ info.memTotal ? bytes(info.memTotal) : '—' }}</span>
        <span title="硬盘"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 12H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11zM6 16h.01M10 16h.01" /></svg>{{ info.diskTotal ? bytes(info.diskTotal) : '—' }}</span>
      </p>
      <div class="pb-bars">
        <div v-for="[label, value] in bars" :key="label" class="pb-bar" :data-tone="m ? tone(value) : 'off'">
          <span>{{ label }}</span><i class="sv-bar" :style="{'--v': value + '%'}" aria-hidden="true"></i><b>{{ m ? value + '%' : '—' }}</b>
        </div>
      </div>
      <dl class="pb-facts">
        <div class="pb-net"><dt>网络</dt><dd>
          <svg v-if="lines && m" class="pb-spark" viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true"><path :d="lines.rx" class="is-rx" vector-effect="non-scaling-stroke" /><path :d="lines.tx" class="is-tx" vector-effect="non-scaling-stroke" /></svg>
          <span class="pb-rates">↑ {{ m ? rate(m.net.txRate) : '—' }}<span>↓ {{ m ? rate(m.net.rxRate) : '—' }}</span></span>
        </dd></div>
        <div v-if="traffic" class="pb-traffic">
          <dt>流量</dt>
          <dd>
            <svg v-if="used !== null" class="pb-ring" viewBox="0 0 36 36" :data-tone="tone(used)" aria-hidden="true"><circle cx="18" cy="18" r="15" /><circle cx="18" cy="18" r="15" :stroke-dasharray="ring" /><text x="18" y="21">{{ used }}%</text></svg>
            <span class="pb-traffic-text">↑ {{ bytes(traffic.tx) }} ↓ {{ bytes(traffic.rx) }}<small v-if="trafficNote">{{ trafficNote }}</small></span>
          </dd>
        </div>
        <div v-if="windows" class="pb-load"><dt>进程</dt><dd>{{ m ? m.processes : '—' }}</dd></div>
        <div v-else class="pb-load"><dt>负载</dt><dd>{{ m ? m.load.join(' | ') : '—' }}</dd></div>
      </dl>
    </template>
    <p v-else class="pb-empty">安装节点后上报运行状态 · 点击安装</p>
    <footer class="pb-foot">
      <span>{{ expiryLabel }}</span>
      <span :class="{'ob-bad': state === 'offline'}">{{ statusText }}</span>
    </footer>
  </article>
</template>
