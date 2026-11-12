<script setup>
import { computed, inject, ref, watch } from 'vue';
import { relativeTime, timeLabel } from '../../instances/instanceHelpers.js';
import { bytes, flag } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { mesh, meshError } from './meshStore.js';
import { directLine, entryHealth, eventRows, isRelay, msText, nameOf, peerKeys, routeRows, routineEvent, transportLabel } from './meshHelpers.js';
import { canPinRoute, fixedRouteInfo } from './meshAuto.js';
import { usePoll } from './usePoll.js';
import MeshNodeName from './MeshNodeName.vue';
import MeshTrend from './MeshTrend.vue';
import NetProbe from '../NetProbe.vue';

// A node page's 诊断 section: the trend always on top, then its peers (a table), events, routes and the network tools.
const props = defineProps({member: {type: Object, required: true}, items: {type: Array, default: () => []}, detail: {type: Object, default: null},
  detailError: {type: String, default: ''}, shownPath: {type: String, default: ''}});
const emit = defineEmits(['show-path', 'pin-path']);
const m = computed(() => props.member);
const id = computed(() => m.value.serverId);
const members = computed(() => mesh.work?.members || []);
const nm = sid => nameOf(members.value, sid);
const flagOf = sid => flag(String(members.value.find(x => x.serverId === sid)?.region || props.items.find(i => i.id === sid)?.region || '').toUpperCase());
const meshActive = inject('meshActive', computed(() => true));
const TABS = [['peers', '对端'], ['events', '事件'], ['routes', '路由'], ['tools', '工具']];
const tab = ref('peers'), toolsSeen = ref(false);
watch(tab, value => { if (value === 'tools') toolsSeen.value = true; if (value === 'routes') void routePoll.run(); });

// ---- peers ----
const status = computed(() => props.detail?.status || {});
const health = computed(() => entryHealth(mesh.data, id.value, props.detail?.status));
const handshakeMs = p => p?.lastHandshakeUnixNano ? Number(p.lastHandshakeUnixNano) / 1e6 : 0;
const fresh = p => handshakeMs(p) > 0 && Date.now() - handshakeMs(p) < 180000;
// WireGuard handshakes only while traffic flows: an old handshake on a line the node still measures is an idle line, not a dead one.
const idle = p => !fresh(p) && handshakeMs(p) > 0 && p.active !== false && p.rttMs != null && Number.isFinite(Number(p.rttMs)) && Number(p.timeoutRate || 0) < 1;
const handshakeText = p => !handshakeMs(p) ? '未握手' : idle(p) ? `空闲 · ${relativeTime(handshakeMs(p), Date.now())}握手` : relativeTime(handshakeMs(p), Date.now());
const handshakeTitle = p => !handshakeMs(p) ? '' : idle(p) ? `${timeLabel(handshakeMs(p))}。线路是通的（节点实测 ${msText(p.rttMs)}），只是没有流量经过：WireGuard 只在有流量时握手，有流量时会马上重新握手。` : timeLabel(handshakeMs(p));
const KINDS = {entry: '接入', relay: '中转', direct: '直连', member: '成员'};
const ORDER = ['entry', 'relay', 'direct', 'member'];
function coldPeer(p) {
  if (p.kind === 'entry') return (m.value.entry?.candidates || []).some(c => c.relay === p.id && health.value[c.id]?.state === 'untested');
  if (!isRelay(m.value)) return false;
  const other = members.value.find(x => x.serverId === p.id), h = entryHealth(mesh.data, p.id);
  return Boolean(other?.entry?.candidates?.some(c => c.relay === id.value && h[c.id]?.state === 'untested'));
}
const lineRtt = p => (mesh.data?.lines || []).find(l => (l.a === id.value && l.b === p.id) || (l.b === id.value && l.a === p.id))?.rttMs;
const peers = computed(() => (status.value.peers || []).map((p, i) => ({p, i})).sort((a, b) => ORDER.indexOf(a.p.kind) - ORDER.indexOf(b.p.kind)
  || Number(b.p.active !== false) - Number(a.p.active !== false) || a.i - b.i).map(({p}) => ({...p, key: `${p.kind}:${p.id}`,
  note: [coldPeer(p) ? `冷备，定时试连${p.reconnects ? ` ${p.reconnects} 次` : ''}` : p.reconnects ? `重连 ${p.reconnects} 次` : '', directLine(p.direct)].filter(Boolean).join(' · ')})));

// ---- events ----
const allEvents = computed(() => (props.detail?.events || status.value.events || []).map((e, i) => ({...e, key: `${e.seq ?? ''}-${e.kind || ''}-${e.at ?? ''}-${i}`})));
const showRoutine = ref(false);
const routine = computed(() => allEvents.value.filter(e => routineEvent(e, m.value, health.value)));
const routineCount = computed(() => routine.value.reduce((n, e) => n + (Number(e.repeat) > 1 ? Number(e.repeat) : 1), 0));
const events = computed(() => showRoutine.value ? allEvents.value : allEvents.value.filter(e => !routine.value.includes(e)));
const eventList = computed(() => eventRows(events.value, m.value, members.value, peerKeys(props.detail?.config)));
const openRuns = ref(new Set());
function toggleRun(key) { const next = new Set(openRuns.value); if (!next.delete(key)) next.add(key); openRuns.value = next; }
const PAGE = 20;
const eventPage = ref(1), routePage = ref(1);
const paged = (list, page) => list.slice((page - 1) * PAGE, page * PAGE);
watch(() => eventList.value.length, n => { eventPage.value = Math.min(eventPage.value, Math.max(1, Math.ceil(n / PAGE))); });
const since = r => Number.isFinite(r.firstAt) && r.firstAt < r.at ? `最早 ${relativeTime(r.firstAt, Date.now())}` : '';
const repeatText = r => r.items ? since(r) : [`重复 ${r.repeat} 次`, since(r)].filter(Boolean).join('，');

// ---- routes, every 10 seconds while shown ----
const routes = ref(null), routesError = ref(''), routesState = ref('loading');
const routeList = computed(() => routes.value ? routeRows(routes.value, id.value, members.value, mesh.data) : null);
const allRoutes = computed(() => routeList.value ? [...routeList.value.routes, ...routeList.value.subnets] : []);
watch(() => allRoutes.value.length, n => { routePage.value = Math.min(routePage.value, Math.max(1, Math.ceil(n / PAGE))); });
const routeKey = r => `${id.value}>${r.dst || r.gateway}${r.subnet ? `>${r.subnet}` : ''}`;
function showRoute(r) { emit('show-path', {src: id.value, dst: r.dst || r.gateway, subnet: r.subnet || '', hops: r.hops, rttMs: r.rttMs, stale: r.stale, fixed: r.fixed || null, key: routeKey(r)}); }
const fixedOf = r => fixedRouteInfo(r?.fixed);
const canPinOf = r => !r.broken && !r.subnet && canPinRoute(r, sid => isRelay(members.value.find(x => x.serverId === sid)));
function pinRoute(r) {
  if (!canPinOf(r)) return;
  emit('pin-path', {src: id.value, dst: r.dst, via: r.hops.slice(1, -1).map(h => h.node)});
}
const routePoll = usePoll(async () => {
  if (tab.value !== 'routes') return;
  try { routes.value = await MeshApi.routes(id.value); routesState.value = 'ok'; routesError.value = ''; }
  catch (e) {
    const code = e?.response?.data?.error;
    if (e?.response?.status === 404 && code !== 'mesh_member_not_found') { routesState.value = 'unsupported'; return; }
    if (code === 'mesh_member_not_found') { routes.value = null; routesState.value = 'draft'; }
    else { routesError.value = meshError(e); if (!routes.value) routesState.value = 'error'; }
  }
}, {interval: () => routesState.value === 'unsupported' ? 0 : 10000});
</script>
<template>
  <section id="m3-sec-diag" class="m3-dsec" aria-label="诊断">
    <h3>诊断</h3>
    <p class="m3-explain">排查连不上、慢的时候用：趋势一直显示在这里；对端、事件、路由是实时状态；工具里可以从这个节点对任意域名或 IP 发 Ping、TCPing 和路由追踪。</p>
    <div class="m3-trendbox"><MeshTrend :server-id="id" /></div>
    <div class="m3-subtabs" role="tablist" aria-label="诊断">
      <button v-for="[k, l] in TABS" :key="k" type="button" role="tab" :aria-selected="tab === k" @click="tab = k">{{ l }}</button>
    </div>

    <template v-if="tab === 'peers'">
      <p v-if="!detail && !detailError" class="m3-faint" role="status">正在读取…</p>
      <p v-else-if="!peers.length" class="m3-faint">{{ detailError ? `读不到：${detailError}` : '还没有对端。应用后，这里实时显示它和每个对端的连接。' }}</p>
      <div v-else class="m3-tw">
        <p v-if="detailError" class="m3-hint">暂时读不到最新状态：{{ detailError }}</p>
        <table class="m3-table" aria-label="对端">
          <thead><tr><th>对端</th><th>类型</th><th>协议</th><th>地址</th><th>延迟</th><th>最近握手</th><th>收 / 发</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="p in peers" :key="p.key" :data-idle="p.active === false || undefined">
              <td><MeshNodeName :id="p.id" /></td>
              <td>{{ KINDS[p.kind] || p.kind }}</td>
              <td>{{ transportLabel(p.transport) || '—' }}</td>
              <td class="mono" :title="p.localAddr ? `本机 ${p.localAddr}` : ''">{{ p.endpoint || '—' }}</td>
              <td>{{ msText(lineRtt(p)) || '—' }}</td>
              <td><span class="ob-dot" :data-tone="fresh(p) || idle(p) ? 'ok' : 'off'" :title="handshakeTitle(p)">{{ handshakeText(p) }}</span></td>
              <td>{{ bytes(p.rxBytes || 0) }} / {{ bytes(p.txBytes || 0) }}</td>
              <td class="m3-faint">{{ p.note }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <template v-else-if="tab === 'events'">
      <p v-if="!detail && !detailError" class="m3-faint" role="status">正在读取…</p>
      <p v-else-if="!allEvents.length" class="m3-faint">{{ detailError ? `读不到：${detailError}` : '还没有切换、重连或错误。' }}</p>
      <template v-else>
        <p v-if="routine.length" class="m3-hint">{{ showRoutine ? '含 ' : '已隐藏 ' }}{{ routineCount }} 条例行记录：备用入口定时试连、中转之间确认接入等
          <button type="button" class="ix-text-button" :aria-expanded="showRoutine" @click="showRoutine = !showRoutine">{{ showRoutine ? '隐藏' : '显示' }}</button></p>
        <ul class="m3-nitems">
          <li v-for="r in paged(eventList, eventPage)" :key="r.key" :data-tone="r.tone">
            <span class="m3-tag" :data-kind="r.tone === 'bad' ? 'bad' : r.tone === 'warn' ? 'warn' : undefined">{{ r.label }}</span>
            <span class="m3-nitems-main">{{ r.text }}
              <small v-if="r.repeat > 1 && repeatText(r)">{{ repeatText(r) }}</small>
              <button v-if="r.items" type="button" class="ix-text-button" :aria-expanded="openRuns.has(r.key)" @click="toggleRun(r.key)">{{ openRuns.has(r.key) ? '收起' : `展开 ${r.items.length} 条` }}</button>
              <details v-if="r.raw" class="m3-details"><summary>原文</summary><code>{{ r.raw }}</code></details>
              <ol v-if="r.items && openRuns.has(r.key)" class="m3-plain">
                <li v-for="x in r.items" :key="x.key">{{ x.text }}<small v-if="x.repeat > 1"> · {{ x.repeat }} 次</small> <time class="m3-faint" :title="timeLabel(x.at)">{{ relativeTime(x.at, Date.now()) }}</time></li>
              </ol></span>
            <time class="m3-faint" :title="timeLabel(r.at)">{{ relativeTime(r.at, Date.now()) }}</time>
          </li>
        </ul>
        <el-pagination v-model:current-page="eventPage" class="m3-pager" size="small" layout="total, prev, pager, next" :pager-count="5" :page-size="PAGE" :total="eventList.length" hide-on-single-page />
      </template>
    </template>

    <template v-else-if="tab === 'routes'">
      <p v-if="routesState === 'unsupported'" class="m3-faint">主控还不支持查看路由。</p>
      <p v-else-if="routesState === 'loading'" class="m3-faint" role="status">正在读取…</p>
      <p v-else-if="routesState === 'draft'" class="m3-faint">应用到网格后，这里显示它去各处实际走的路线。</p>
      <p v-else-if="routesState === 'error'" class="m3-faint">读不到路由：{{ routesError }}</p>
      <template v-else-if="routeList">
        <p v-if="!routeList.routes.length && !routeList.subnets.length" class="m3-faint">还没有路由。</p>
        <ul class="m3-nitems">
          <li v-for="r in paged(allRoutes, routePage)" :key="r.key" :data-broken="r.broken || undefined">
            <span class="m3-nitems-main"><b><span v-if="r.subnet" class="mono">{{ r.title }}</span><MeshNodeName v-else :id="r.dst" :name="r.title" />
              <span v-if="fixedOf(r)" class="pb-tag" :data-tone="fixedOf(r).tone === 'off' ? undefined : fixedOf(r).tone">{{ fixedOf(r).text }}</span></b>
              <small>{{ r.through }}<span v-if="r.stale && !r.broken" class="ob-warn"> · 数据不完整</span></small></span>
            <span class="m3-faint">{{ r.rtt }}</span>
            <el-button v-if="canPinOf(r)" size="small" @click="pinRoute(r)">加为自建路线</el-button>
            <el-button size="small" :type="shownPath === routeKey(r) ? 'primary' : undefined" @click="showRoute(r)">图上看</el-button>
          </li>
        </ul>
        <el-pagination v-model:current-page="routePage" class="m3-pager" size="small" layout="total, prev, pager, next" :pager-count="5" :page-size="PAGE" :total="allRoutes.length" hide-on-single-page />
        <p v-if="routesError" class="m3-hint">暂时读不到最新路由（{{ routesError }}），稍后重试。</p>
      </template>
    </template>

    <NetProbe v-if="toolsSeen" v-show="tab === 'tools'" :server-id="id" :servers="items" :online="items.find(i => i.id === id)?.live !== false" :active="meshActive" board :shown-path="shownPath" @show-path="emit('show-path', $event)" />
  </section>
</template>
