<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import draggable from 'vuedraggable';
import { Grid, List } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import PlatformMark from './PlatformMark.vue';
import ServerCard from './ServerCard.vue';
import { dragEdgeScroll } from './dragEdgeScroll.js';
import { nodeStateTag, updateAllText, updateSummary } from './nodeVersions.js';
import { bytes, daysLeft, expiryTone, flag, monthly, percent, rate, regionName, safeError, spendInCny, tone, noAutofill } from './serverHelpers.js';

const props = defineProps({items: {type: Array, required: true}, now: {type: Number, required: true}, loading: Boolean,
  spark: {type: Object, default: () => ({})}, build: {type: Object, default: () => ({})}, fx: {type: Object, default: null}});
const emit = defineEmits(['open', 'terminal', 'add', 'changed', 'reordered']);
const VIEW = 'openbear.servers.view';
const group = ref(''), region = ref(''), sort = ref('default'), query = ref(''), quick = ref(''), updating = ref(false);
const view = ref(localStorage.getItem(VIEW) === 'list' ? 'list' : 'cards');
watch(view, value => { try { localStorage.setItem(VIEW, value); } catch { /* storage may be blocked */ } });
const SORTS = [['default', '默认排序'], ['name', '名称'], ['cpu', 'CPU 占用'], ['mem', '内存占用'], ['traffic', '流量使用'], ['expiry', '到期时间'], ['price', '月均价格']];
const m = item => item.live?.m;
const expiry = item => item.profile.billing?.cycle === -1 ? null : daysLeft(item.profile.billing?.expiresAt, new Date(props.now));
const expiring = item => { const d = expiry(item); return d !== null && d <= 30; };

const groups = computed(() => [...new Set(props.items.map(i => i.profile.group).filter(Boolean))]);
const regions = computed(() => [...new Set(props.items.map(i => i.region).filter(Boolean))]);
// Online nodes that can be brought to the controller's node build; those without command
// execution can only be updated on the machine itself.
const summary = computed(() => updateSummary(props.items));
const target = computed(() => `${props.build.source === 'local' ? '本地构建' : ''} ${props.build.version}`);
// The listing says what it found; before it arrives nothing is claimed missing.
const noRelease = computed(() => 'checkedAtMs' in props.build && !props.build.available && props.items.some(i => i.node));
const checking = ref(false);
async function retryRelease() {
  if (checking.value) return;
  checking.value = true;
  try {
    const release = ok(await Api.checkNodeRelease()).release;
    if (!release.available) ElMessage.warning('暂时还是没有可用的节点版本，稍后再试');
    emit('changed');
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { checking.value = false; }
}
// Reordering on the board: drag a card (or a row) by its flag, in the default order only, since any
// other sort is not the saved order. With a group, region, quick filter or search, the servers shown
// change places among themselves and the hidden ones keep theirs. Installed and uninstalled servers are
// ordered apart (the uninstalled always come last), so a card does not cross between them.
// A drop shows at once and is saved with PUT /servers/order; if that fails it goes back.
const order = ref(null), savingOrder = ref(false), frozen = ref(null);
const sortable = computed(() => sort.value === 'default' && props.items.length > 1);
const ordered = computed(() => {
  if (!order.value) return props.items;
  const at = new Map(order.value.map((id, i) => [id, i]));
  return [...props.items].sort((a, b) => (at.get(a.id) ?? Infinity) - (at.get(b.id) ?? Infinity));
});
const visible = computed(() => {
  const q = query.value.trim().toLowerCase();
  const list = ordered.value.filter(i => (!group.value || i.profile.group === group.value) && (!region.value || i.region === region.value)
    && (quick.value !== 'offline' || i.node && !i.live) && (quick.value !== 'expiring' || expiring(i))
    && (!q || [i.name, i.profile.provider, i.profile.group, ...(i.profile.tags || []), i.node?.info?.ipv4, i.ssh?.host].some(v => String(v || '').toLowerCase().includes(q))));
  const key = {
    name: i => i.name.toLowerCase(),
    cpu: i => -(m(i)?.cpu ?? -1),
    mem: i => -(m(i) ? percent(m(i).mem.used, m(i).mem.total) : -1),
    traffic: i => -(i.traffic?.limit ? i.traffic.used / i.traffic.limit : -1),
    expiry: i => expiry(i) ?? Infinity,
    price: i => -monthly(i.profile.billing),
  }[sort.value];
  return key ? [...list].sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) : list;
});
const stats = computed(() => {
  const nodes = props.items.filter(i => i.node), online = nodes.filter(m);
  const sum = (list, fn) => list.reduce((total, i) => total + (fn(i) || 0), 0);
  const spend = {};
  for (const i of props.items) {
    const value = monthly(i.profile.billing);
    if (value) spend[i.profile.billing.currency || ''] = (spend[i.profile.billing.currency || ''] || 0) + value;
  }
  return {
    online: online.length, nodes: nodes.length, offline: nodes.length - online.length, regions: regions.value.length,
    up: sum(online, i => m(i).net.txRate), down: sum(online, i => m(i).net.rxRate),
    periodUp: sum(nodes, i => i.traffic?.tx), periodDown: sum(nodes, i => i.traffic?.rx),
    spend: Object.entries(spend).sort((a, b) => b[1] - a[1]), expiring: props.items.filter(expiring).length,
  };
});
const money = (currency, value) => `${currency}${value >= 1000 ? Math.round(value).toLocaleString() : value.toFixed(2)}`;
// Bills in other currencies add up in CNY once the controller has rates;
// without them each currency keeps its own total.
const cny = computed(() => props.fx?.rates ? spendInCny(stats.value.spend, props.fx) : null);
const spendTitle = computed(() => !cny.value ? '' : [
  ...cny.value.parts.map(p => p.code === 'CNY' ? money('¥', p.value) : `${money(p.currency, p.value)} ≈ ${money('¥', p.cny)}（1 ${p.code} = ${p.rate.toFixed(4)} CNY）`),
  ...cny.value.rest.map(([currency, value]) => `${money(currency, value)}（没有汇率，未计入）`),
  props.fx.date ? `汇率日期 ${props.fx.date}` : '',
].filter(Boolean).join('\n'));
function toggleQuick(value) { quick.value = quick.value === value ? '' : value; }
async function updateAll() {
  if (updating.value) return;
  updating.value = true;
  try {
    const {tone, text, failed} = updateAllText(ok(await Api.updateServerNodes()).items);
    ElMessage[tone](failed?.length ? `${text}：${safeError({response: {data: {error: failed[0].error}}})}` : text);
    emit('changed');
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { updating.value = false; }
}
// While a card is dragged the list holds still: a poll replacing the items must not move it underneath.
const shown = computed(() => frozen.value || visible.value);
const sameSection = e => !e.relatedContext?.element || Boolean(e.draggedContext.element.node) === Boolean(e.relatedContext.element.node);
let stopEdgeScroll = () => {};
function dragStart(event) { frozen.value = visible.value.slice(); stopEdgeScroll(); stopEdgeScroll = dragEdgeScroll(event.from); }
function dragEnd() { stopEdgeScroll(); stopEdgeScroll = () => {}; frozen.value = null; }
function dropped(list) {
  const ids = list.map(i => i.id);
  if (ids.join('\n') === shown.value.map(i => i.id).join('\n')) return;
  // The shown servers take the shown servers' places in the whole order, in their new order.
  const now = new Set(ids);
  let k = 0;
  void saveOrder(ordered.value.map(i => now.has(i.id) ? ids[k++] : i.id));
}
async function saveOrder(ids) {
  order.value = ids; savingOrder.value = true;
  try { emit('reordered', ok(await Api.orderServers(ids)).items); }
  catch (e) { ElMessage.error(`顺序没有保存：${safeError(e)}`); }
  finally { order.value = null; savingOrder.value = false; }
}
onBeforeUnmount(() => stopEdgeScroll());
const pct = (u) => u?.total ? Math.round(percent(u.used, u.total)) : 0;
const trafficPct = i => i.traffic?.limit ? Math.round(percent(i.traffic.used, i.traffic.limit)) : null;
</script>
<template>
  <div class="pb">
    <section class="pb-stats" aria-label="服务器汇总">
      <button type="button" class="pb-stat" :aria-pressed="quick === 'offline'" :disabled="!stats.offline && quick !== 'offline'" :title="stats.offline ? '只看离线的服务器' : ''" @click="toggleQuick('offline')">
        <span>在线</span><b>{{ stats.online }} <small>/ {{ stats.nodes }}</small><em v-if="stats.offline" class="pb-stat-alert">{{ stats.offline }} 离线</em></b>
      </button>
      <div class="pb-stat"><span>地区</span><b>{{ stats.regions }}</b></div>
      <div class="pb-stat"><span>实时速率</span><b class="pb-pair"><em>↑ {{ rate(stats.up) }}</em><em>↓ {{ rate(stats.down) }}</em></b></div>
      <div class="pb-stat"><span>本期流量</span><b class="pb-pair"><em>↑ {{ bytes(stats.periodUp) }}</em><em>↓ {{ bytes(stats.periodDown) }}</em></b></div>
      <div v-if="cny && stats.spend.length" class="pb-stat" :title="spendTitle"><span>每月费用</span><b>{{ money('¥', cny.total) }}<small v-if="cny.rest.length"> + {{ cny.rest.length }} 种外币</small></b></div>
      <div v-else class="pb-stat"><span>每月费用</span><b class="pb-pair is-wrap"><em v-for="[currency, value] in stats.spend" :key="currency">{{ money(currency, value) }}</em><em v-if="!stats.spend.length">—</em></b></div>
      <button type="button" class="pb-stat" :aria-pressed="quick === 'expiring'" :disabled="!stats.expiring && quick !== 'expiring'" :title="stats.expiring ? '只看 30 天内到期的服务器' : ''" @click="toggleQuick('expiring')">
        <span>30 天内到期</span><b :class="{'ob-warn': stats.expiring}">{{ stats.expiring }} <small>台</small></b>
      </button>
    </section>

    <div v-if="noRelease" class="pb-notice" role="status" data-node-release="none">
      <span>暂时没有可用的节点版本，稍后重试</span>
      <span class="ob-grow" />
      <el-button size="small" :loading="checking" @click="retryRelease">重试</el-button>
    </div>
    <div v-else-if="summary.due" class="pb-notice" role="status" data-node-update="summary">
      <span v-if="summary.direct">{{ summary.direct }} 台服务器的节点可以更新到{{ target }}<template v-if="summary.required">，其中 {{ summary.required }} 台{{ summary.features ? `需要更新才能使用${summary.features}` : '必须更新' }}</template><template v-if="summary.blocked">；另有 {{ summary.blocked }} 台关闭了命令执行，需要在服务器上重新运行安装命令</template></span>
      <span v-else>{{ summary.due }} 台服务器的节点可以更新到{{ target }}，它们关闭了命令执行，需要在服务器上重新运行安装命令来更新</span>
      <span class="ob-grow" />
      <el-button v-if="summary.direct" size="small" :loading="updating" @click="updateAll">全部更新</el-button>
    </div>

    <div class="pb-filters" :class="{'has-search': items.length > 8}">
      <div class="pb-chips" role="group" aria-label="按分组筛选">
        <button type="button" :aria-pressed="!group" @click="group = ''">全部</button>
        <button v-for="g in groups" :key="g" type="button" :aria-pressed="group === g" @click="group = group === g ? '' : g">{{ g }}</button>
      </div>
      <div v-if="regions.length > 1" class="pb-chips pb-regions" role="group" aria-label="按地区筛选">
        <button type="button" :aria-pressed="!region" @click="region = ''">全部地区</button>
        <button v-for="r in regions" :key="r" type="button" class="sv-flag" :aria-pressed="region === r" :title="regionName(r)" :aria-label="regionName(r)" @click="region = region === r ? '' : r">{{ flag(r) }}</button>
      </div>
      <div class="pb-tools">
        <input v-bind="noAutofill" v-if="items.length > 8" v-model="query" type="search" class="pb-search" placeholder="搜索名称、IP、标签" aria-label="搜索服务器" />
        <select v-model="sort" class="pb-sort" aria-label="排序方式"><option v-for="[key, label] in SORTS" :key="key" :value="key">{{ label }}</option></select>
        <div class="pb-view" role="group" aria-label="显示方式">
          <button type="button" :aria-pressed="view === 'cards'" title="卡片" aria-label="卡片视图" @click="view = 'cards'"><el-icon><Grid /></el-icon></button>
          <button type="button" :aria-pressed="view === 'list'" title="列表" aria-label="列表视图" @click="view = 'list'"><el-icon><List /></el-icon></button>
        </div>
      </div>
    </div>
    <p v-if="quick" class="pb-quick">{{ quick === 'offline' ? '只显示离线的服务器' : '只显示 30 天内到期的服务器' }}<button type="button" class="ix-text-button" @click="quick = ''">显示全部</button></p>

    <!-- While a card is dragged the frozen list stays up whatever a poll brings: taking it away would end the drag without its end. -->
    <p v-if="!frozen && loading && !items.length" class="instance-empty" role="status">正在加载…</p>
    <div v-else-if="!frozen && !items.length" class="ob-empty">
      <h2>还没有服务器</h2>
      <p>添加服务器并安装节点后，这里显示每台服务器的运行状态、流量和账单。</p>
      <el-button type="primary" @click="emit('add')">添加服务器</el-button>
    </div>
    <p v-else-if="!shown.length" class="instance-empty">没有符合条件的服务器</p>
    <draggable v-else-if="view === 'cards'" :model-value="shown" item-key="id" class="pb-grid" handle=".pb-grip" :animation="160" ghost-class="so-ghost" :scroll="false"
      :disabled="!sortable || savingOrder" :move="sameSection" @update:model-value="dropped" @start="dragStart" @end="dragEnd">
      <template #item="{element: item}">
        <ServerCard :item="item" :now="now" :spark="spark[item.id]" :sortable="sortable" @open="emit('open', $event)" @terminal="emit('terminal', $event)" />
      </template>
    </draggable>
    <draggable v-else :model-value="shown" item-key="id" class="pl" role="table" aria-label="服务器列表" handle=".pb-grip" :animation="160" ghost-class="so-ghost" :scroll="false"
      :disabled="!sortable || savingOrder" :move="sameSection" @update:model-value="dropped" @start="dragStart" @end="dragEnd">
      <template #header>
        <div class="pl-head" role="row">
          <span role="columnheader">服务器</span><span role="columnheader">CPU</span><span role="columnheader">内存</span><span role="columnheader">硬盘</span>
          <span role="columnheader">网速</span><span role="columnheader">本期流量</span><span role="columnheader">到期</span><span role="columnheader">价格</span>
        </div>
      </template>
      <template #item="{element: item}">
      <div class="pl-row" role="row" tabindex="0" :data-state="!item.node ? 'none' : m(item) ? 'online' : 'offline'"
        @click="emit('open', item)" @keydown.enter.self="emit('open', item)">
        <span class="pl-name" role="cell">
          <span class="pb-dot" :data-tone="!item.node ? 'off' : m(item) ? 'ok' : 'bad'" aria-hidden="true"></span>
          <span class="sv-flag" :class="{'pb-grip': sortable}" :title="sortable ? '拖动调整顺序' : undefined" aria-hidden="true">{{ flag(item.region) || '🖥' }}</span>
          <strong :title="item.name">{{ item.name }}</strong>
          <PlatformMark :item="item" />
          <small v-if="item.profile.group || item.profile.tags?.length">{{ [item.profile.group, ...(item.profile.tags || [])].filter(Boolean).join(' · ') }}</small>
          <span v-if="nodeStateTag(item.node)" class="pb-tag pl-state" :class="{'is-plain': !nodeStateTag(item.node).tone}" :data-tone="nodeStateTag(item.node).tone || null"
            :data-node-state="nodeStateTag(item.node).state" :title="nodeStateTag(item.node).title">{{ nodeStateTag(item.node).text }}</span>
        </span>
        <template v-if="m(item)">
          <span v-for="[label, value] in [['CPU', Math.round(m(item).cpu)], ['内存', pct(m(item).mem)], ['硬盘', pct(m(item).disk)]]" :key="label" class="pl-meter" role="cell" :data-tone="tone(value)" :aria-label="`${label} ${value}%`">
            <i class="sv-bar" :style="{'--v': value + '%'}" aria-hidden="true"></i><b>{{ value }}%</b>
          </span>
          <span class="pl-net" role="cell"><span>↑ {{ rate(m(item).net.txRate) }}</span><span>↓ {{ rate(m(item).net.rxRate) }}</span></span>
        </template>
        <span v-else class="pl-off" role="cell">{{ item.node ? '离线' : '未安装节点' }}</span>
        <span class="pl-traffic" :class="{'has-limit': trafficPct(item) !== null}" role="cell" :data-tone="trafficPct(item) === null ? '' : tone(trafficPct(item))"><span class="pl-used">{{ item.traffic ? bytes(item.traffic.used) : '—' }}</span><small v-if="trafficPct(item) !== null">{{ trafficPct(item) }}%</small></span>
        <span class="pl-expiry" role="cell" :data-tone="expiryTone(item.profile.billing, new Date(now))">{{ expiry(item) === null ? (item.profile.billing?.cycle === -1 ? '一次性' : '—') : expiry(item) < 0 ? `过期 ${-expiry(item)} 天` : expiry(item) > 36500 ? '长期' : `${expiry(item)} 天` }}</span>
        <span class="pl-price" role="cell">{{ monthly(item.profile.billing) ? money(item.profile.billing.currency || '', monthly(item.profile.billing)) + '/月' : '—' }}</span>
      </div>
      </template>
    </draggable>
  </div>
</template>
