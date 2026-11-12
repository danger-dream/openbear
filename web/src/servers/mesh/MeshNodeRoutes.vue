<script setup>
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { MeshApi } from './meshApi.js';
import { mesh, meshError, revert, save } from './meshStore.js';
import { errorMessage, isRelay, msText, nameOf, transportLabel } from './meshHelpers.js';
import { fixedRouteInfo } from './meshAuto.js';
import { addRoute, changeRoute, gapsOf, MAX_PER_PAIR, MAX_VIA, moveInPair, nodesFrom, pairRoutes, pairsOf, routeRtt, routeState, sameRoute } from './meshRoutes.js';
import { usePoll } from './usePoll.js';
import MeshDialog from './MeshDialog.vue';
import MeshFr from './MeshFr.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// A node's routes: its own routes (自建路线) by the other end, each pair's in their order; the route to or
// from another node as the nodes take it now and, on demand, the best ten by the latest reports, any of
// which can become an own route; and an editor that lays one out relay by relay.
// seed: {src, dst, via, nonce} opens the editor with a route from elsewhere (固定这条路线 on the board).
const props = defineProps({id: {type: String, required: true}, phone: Boolean, fresh: Boolean, regionOf: {type: Function, required: true}, seed: {type: Object, default: null}});
const emit = defineEmits(['show-path']);
const members = computed(() => mesh.work?.members || []);
const live = computed(() => members.value.filter(x => !x.removing));
const relays = computed(() => live.value.filter(isRelay));
const nm = sid => nameOf(members.value, sid);
const optionOf = (x, note = '') => ({value: x.serverId, label: x.name, region: props.regionOf(x.serverId), note});
const lines = computed(() => mesh.data?.lines || []);
const lineOf = (a, b) => lines.value.find(l => (l.a === a && l.b === b) || (l.a === b && l.b === a));
const paths = computed(() => mesh.work?.paths || []);
const views = computed(() => new Map((mesh.data?.pathsView || []).map(v => [v.id, v])));

// ---- own routes ----
const pairs = computed(() => pairsOf(paths.value, props.id));
const editing = ref(new Set());
function toggleEdit(other) {
  const next = new Set(editing.value);
  if (next.has(other)) next.delete(other); else next.add(other);
  editing.value = next;
}
const ownRow = p => {
  const view = views.value.get(p.id) || null;
  return {p, nodes: nodesFrom(p, props.id), state: routeState(p, view, props.id, nm), rtt: routeRtt(view)};
};
function move(p, delta) { if (moveInPair(mesh.work.paths, p, delta)) void save(); }
function toggleOn(p) { p.on = p.on === false; void save(); }
async function remove(p) {
  try {
    await ElMessageBox.confirm(`删除自建路线 ${nodesFrom(p, props.id).map(nm).join(' → ')}？删除后不再经过它，加的入口中转和线路留着。`, '删除自建路线',
      {confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  mesh.work.paths = paths.value.filter(x => x !== p);
  void save();
}

// ---- the route between this node and another, now and the best ten ----
const HOME_KEY = 'openbear.mesh.home';
const homeDefault = () => {
  const kept = (() => { try { return localStorage.getItem(HOME_KEY) || ''; } catch { return ''; } })();
  if (kept && live.value.some(x => x.serverId === kept)) return kept;
  return live.value.some(x => x.serverId === 'local') ? 'local' : relays.value[0]?.serverId || '';
};
// The other end: where you usually are (kept in this browser). The route goes from this node there, or back.
const other = ref(homeDefault()), outward = ref(true), livePath = ref(false);
const routeChoices = computed(() => live.value.filter(x => x.serverId !== props.id).sort((a, b) => Number(isRelay(b)) - Number(isRelay(a)) || a.name.localeCompare(b.name, 'zh'))
  .map(x => optionOf(x, isRelay(x) ? '中转' : '')));
function setOther(value) { other.value = value; try { localStorage.setItem(HOME_KEY, value); } catch { /* private mode */ } }
const ends = computed(() => outward.value ? [props.id, other.value] : [other.value, props.id]);
const path = ref(null), pathError = ref('');
async function loadPath(quiet = false) {
  if (!quiet) { path.value = null; pathError.value = ''; }
  if (!other.value || other.value === props.id || props.fresh) return;
  try { path.value = await MeshApi.path(...ends.value); pathError.value = ''; } catch (e) { pathError.value = meshError(e); }
}
const pathPoll = usePoll(() => loadPath(true), {interval: () => livePath.value ? 5000 : 0});
watch(livePath, on => { if (on) void pathPoll.run(); });
const hops = computed(() => (path.value?.hops || []).map((h, i, all) => {
  const l = i ? lineOf(all[i - 1].node, h.node) : null;
  return {node: h.node, transport: l ? transportLabel(l.transport) : h.via === 'lan' ? '局域网' : '', rtt: i ? h.rttMs : null};
}));
const pathFixed = computed(() => fixedRouteInfo(path.value?.fixed));
const nowNodes = computed(() => hops.value.at(-1)?.node === ends.value[1] ? hops.value.map(h => h.node) : []);
function showPathOnBoard() {
  if (!path.value?.hops?.length) return;
  emit('show-path', {src: ends.value[0], dst: ends.value[1], subnet: '', hops: path.value.hops, rttMs: path.value.rttMs, stale: path.value.stale, key: ends.value.join('>')});
}

const best = ref(null), bestError = ref(''), bestBusy = ref(false);
async function loadBest() {
  bestBusy.value = true; bestError.value = '';
  try { best.value = {ends: [...ends.value], ...(await MeshApi.pathOptions(...ends.value))}; } catch (e) { bestError.value = meshError(e); best.value = null; } finally { bestBusy.value = false; }
}
watch(() => [other.value, props.id, outward.value], () => { best.value = null; bestError.value = ''; void loadPath(); }, {immediate: true});
const segText = s => [Number.isFinite(s.rttMs) ? msText(s.rttMs) : s.exists === false ? '应用后实测' : '', s.loss ? `丢包 ${Math.round(s.loss * 1000) / 10}%` : ''].filter(Boolean).join(' · ');
const pairFull = (a, b) => pairRoutes(paths.value, a, b).length >= MAX_PER_PAIR;
// Whether a path can become an own route: relays between, at most MAX_VIA of them, not one already.
function canAdd(nodes) {
  const mids = nodes.slice(1, -1);
  return mids.length >= 1 && mids.length <= MAX_VIA && mids.every(id => isRelay(live.value.find(x => x.serverId === id)))
    && !pairRoutes(paths.value, nodes[0], nodes.at(-1)).some(p => sameRoute(p, nodes));
}
async function addFrom(nodes) {
  const [src, dst] = [nodes[0], nodes.at(-1)];
  if (pairFull(src, dst)) { ElMessage.warning(`${nm(src)} 和 ${nm(dst)} 已经有 ${MAX_PER_PAIR} 条自建路线，先删掉一条`); return; }
  const changes = gapsOf(mesh.work, nodes).filter(Boolean).map(g => g.text);
  if (changes.length) {
    try {
      await ElMessageBox.confirm(`加为自建路线时还会：${changes.join('；')}。`, '加为自建路线', {confirmButtonText: '加为自建路线', cancelButtonText: '取消', type: 'info'});
    } catch { return; }
  }
  const res = addRoute(mesh.work, src, dst, nodes.slice(1, -1));
  if (res.error) { ElMessage.warning(res.error); return; }
  await save();
  if (mesh.rejected) ElMessage.error(`没能存入草稿：${mesh.errors.map(errorMessage).join('；')}`);
  else ElMessage.success(`已加为第 ${pairRoutes(paths.value, src, dst).length} 条，点「应用」后生效`);
}

// ---- the editor: a route laid out relay by relay ----
const builder = ref(null);
function openBuilder(seed = {}) {
  const route = seed.routeId ? paths.value.find(p => p.id === seed.routeId) : null;
  const src = route?.from || seed.src || props.id, dst = route?.to || seed.dst || (other.value !== props.id ? other.value : '');
  builder.value = reactive({routeId: route?.id || '', src, dst, via: (route?.via || seed.via || []).filter(v => v !== src && v !== dst), error: '', rejected: false, saving: false});
}
watch(() => props.seed?.nonce, n => { if (n && props.seed) openBuilder(props.seed); }, {immediate: true});
const bNodes = computed(() => builder.value ? [builder.value.src, ...builder.value.via, builder.value.dst].filter(Boolean) : []);
const bGaps = computed(() => builder.value ? gapsOf(mesh.work, bNodes.value) : []);
const bEndChoices = computed(() => live.value.filter(x => x.serverId !== builder.value?.src && !builder.value?.via.includes(x.serverId)).map(x => optionOf(x, isRelay(x) ? '中转' : '')));
const bRelayChoices = computed(() => relays.value.filter(r => r.serverId !== builder.value?.src && r.serverId !== builder.value?.dst && !builder.value?.via.includes(r.serverId))
  .map(r => optionOf(r)));
// Each link as last measured: up by the latest reports, or measured now from one end. A link already
// measured keeps its figure while the route around it changes and is measured again.
const bMeasure = ref(null), bMeasuring = ref(false), bMeasureError = ref('');
const bMeasured = computed(() => new Map((bMeasure.value?.segments || []).map(s => [`${s.a}>${s.b}`, s])));
const bMeasureNow = computed(() => bMeasure.value?.key === bNodes.value.join('>') ? bMeasure.value : null);
const measurePoll = usePoll(async current => {
  const nodes = bNodes.value;
  if (!builder.value || nodes.length < 2 || bGaps.value.some(g => g?.error)) return;
  bMeasuring.value = true;
  try {
    const got = await MeshApi.measure(nodes);
    if (current()) { bMeasure.value = {key: nodes.join('>'), ...got}; bMeasureError.value = ''; }
  } catch (e) {
    if (current()) bMeasureError.value = meshError(e);
  } finally {
    if (current()) bMeasuring.value = false;
  }
}, {interval: () => builder.value ? 15000 : 0});
let measureSoon = 0;
watch(() => builder.value ? bNodes.value.join('>') : '', key => {
  clearTimeout(measureSoon);
  if (key) measureSoon = setTimeout(() => void measurePoll.run(), 300);
  else { measurePoll.stop(); bMeasure.value = null; bMeasuring.value = false; bMeasureError.value = ''; }
}, {immediate: true});
const lossText = loss => loss ? ` · 丢包 ${Math.round(loss * 1000) / 10}%` : '';
function bSeg(i) {
  const g = bGaps.value[i];
  if (g?.error) return {tone: 'bad', text: g.text};
  const a = bNodes.value[i], b = bNodes.value[i + 1], l = lineOf(a, b), m = bMeasured.value.get(`${a}>${b}`) || bMeasured.value.get(`${b}>${a}`);
  const up = m ? m.how === 'report' : l?.state === 'up';
  const how = g ? `保存后：${g.text}` : up ? ['已连通', l && transportLabel(l.transport)].filter(Boolean).join(' · ') : '已在草稿里，应用后连上';
  const ms = !m ? (bMeasuring.value ? '测量中…' : '') : Number.isFinite(m.rttMs) ? `${up ? '' : '直测 '}${msText(m.rttMs)}${lossText(m.loss)}` : `测不到：${m.why || '没有结果'}`;
  return {tone: g || !up ? 'warn' : m?.loss > 0.02 ? 'warn' : 'ok', text: [how, ms].filter(Boolean).join(' · ')};
}
// The whole route: the sum when every link has a figure, how many were measured from an end, and the route taken now between the same two.
const bTotal = computed(() => {
  const m = bMeasureNow.value, b = builder.value;
  if (!b?.dst || !b.via.length) return null;
  if (bMeasureError.value) return {text: `没测成：${bMeasureError.value}`};
  if (!m) return bMeasuring.value ? {text: '正在测…'} : null;
  const probed = m.segments.filter(s => s.how === 'probe').length, missing = m.segments.filter(s => !Number.isFinite(s.rttMs)).length;
  const sameEnds = [ends.value.join('>'), [...ends.value].reverse().join('>')].includes(`${b.src}>${b.dst}`);
  return {rtt: m.rttMs, missing, probed, now: sameEnds && Number.isFinite(path.value?.rttMs) ? path.value.rttMs : null};
});
function bMove(i, delta) {
  const via = builder.value.via, j = i + delta;
  if (j < 0 || j >= via.length) return;
  [via[i], via[j]] = [via[j], via[i]];
}
const bReady = computed(() => {
  const b = builder.value;
  return Boolean(b?.dst) && b.via.length >= 1 && b.via.length <= MAX_VIA && !bGaps.value.some(g => g?.error);
});
async function saveBuilder() {
  const b = builder.value;
  if (!bReady.value || b.saving) return;
  b.error = '';
  const route = b.routeId ? paths.value.find(p => p.id === b.routeId) : null;
  const res = route ? changeRoute(mesh.work, route, b.via) : addRoute(mesh.work, b.src, b.dst, b.via);
  if (res.error) { b.error = res.error; return; }
  b.saving = true;
  await save();
  b.saving = false;
  if (mesh.rejected) { b.error = mesh.errors.map(errorMessage).join('；'); b.rejected = true; return; }
  builder.value = null;
  ElMessage.success('已存入草稿，点「应用」后生效');
}
function undoBuilder() { revert(); builder.value = null; }
defineExpose({openBuilder});
</script>
<template>
  <MeshFr label="路线" sub="自建的优先，都不通时走自动选路">
    <div class="m3-rt-own">
      <div class="m3-rt-head">
        <b>自建路线</b>
        <span class="ob-grow" />
        <button type="button" class="ix-text-button" @click="openBuilder()">＋ 自己搭一条</button>
      </div>
      <p v-if="!pairs.length" class="m3-hint">还没有。在下面计算路线后点「加为自建」，或自己搭一条。</p>
      <section v-for="g in pairs" :key="g.other" class="m3-rt-pair">
        <div class="m3-rt-pair-h">
          <span><MeshNodeName :id="id" plain /> ⇄ <MeshNodeName :id="g.other" /></span>
          <span class="ob-grow" />
          <button type="button" class="ix-text-button" :aria-expanded="editing.has(g.other)" @click="toggleEdit(g.other)">{{ editing.has(g.other) ? '完成' : '编辑' }}</button>
        </div>
        <ol class="m3-rt-list">
          <li v-for="(r, i) in g.routes.map(ownRow)" :key="r.p.id" :data-off="r.p.on === false || undefined">
            <span class="m3-no">{{ i + 1 }}</span>
            <span class="m3-rt-chain"><template v-for="(n, k) in r.nodes" :key="n"><span v-if="k" class="m3-faint"> → </span><MeshNodeName :id="n" :plain="n === id" /></template></span>
            <span class="m3-rt-state" :data-tone="r.state.tone">{{ r.state.text }}<template v-if="r.rtt != null && r.state.tone !== 'bad'"> · {{ msText(r.rtt) }}</template></span>
            <div v-if="editing.has(g.other)" class="m3-rt-acts">
              <template v-if="g.routes.length > 1">
                <button type="button" class="ix-icon-button" :disabled="!i" title="往前" :aria-label="`第 ${i + 1} 条往前`" @click="move(r.p, -1)">↑</button>
                <button type="button" class="ix-icon-button" :disabled="i >= g.routes.length - 1" title="往后" :aria-label="`第 ${i + 1} 条往后`" @click="move(r.p, 1)">↓</button>
              </template>
              <button type="button" class="ix-text-button" @click="openBuilder({routeId: r.p.id})">修改</button>
              <button type="button" class="ix-text-button" @click="toggleOn(r.p)">{{ r.p.on === false ? '启用' : '停用' }}</button>
              <button type="button" class="ix-text-button m3-danger-text" @click="remove(r.p)">删除</button>
            </div>
          </li>
        </ol>
        <p class="m3-hint">{{ g.routes.length > 1 ? `按顺序用第一条通的；这 ${g.routes.length} 条都不通时` : '不通时' }}走自动选路，回程原路返回。</p>
      </section>
    </div>

    <div class="m3-rt-calc">
      <div class="m3-ctl m3-route-ctl">
        <div class="m3-seg" role="radiogroup" aria-label="路线方向">
          <button type="button" role="radio" :aria-checked="outward" @click="outward = true">从这台出发</button>
          <button type="button" role="radio" :aria-checked="!outward" @click="outward = false">到这台</button>
        </div>
        <span class="m3-faint">{{ outward ? '到' : '从' }}</span>
        <MeshNodeSelect :model-value="other" :options="routeChoices" aria-label="路线另一端" @change="setOther" />
        <button type="button" class="m3-sw" role="switch" :aria-checked="livePath" @click="livePath = !livePath"><i></i><span>实时刷新</span></button>
      </div>
      <p v-if="pathError" class="m3-hint ob-warn">读不到路线：{{ pathError }}</p>
      <div v-else-if="hops.length" class="m3-hops">
        <span class="m3-faint">现在走</span>
        <span v-if="pathFixed" class="pb-tag" :data-tone="pathFixed.tone === 'off' ? undefined : pathFixed.tone">{{ pathFixed.text }}</span>
        <template v-for="(h, i) in hops" :key="h.node + i">
          <span v-if="i" class="m3-hop-ms">{{ [h.transport, msText(h.rtt)].filter(Boolean).join(' ') }} →</span>
          <span class="m3-hop"><MeshNodeName :id="h.node" :plain="h.node === id" /></span>
        </template>
        <span class="ob-grow" /><b>{{ Number.isFinite(path?.rttMs) ? msText(path.rttMs) : '延迟未知' }}</b>
      </div>
      <p v-else-if="path" class="m3-hint ob-warn">现在没有路线：中间缺少接入或线路。</p>
      <div class="m3-rt-calc-acts">
        <el-button size="small" :loading="bestBusy" :disabled="fresh || !other" @click="loadBest">计算前 10 条</el-button>
        <button v-if="hops.length" type="button" class="ix-text-button" @click="showPathOnBoard">在图上显示现在的路线</button>
      </div>
      <p v-if="bestError" class="m3-hint ob-warn">算不出来：{{ bestError }}</p>
      <template v-if="best">
        <p v-if="!best.routes.length && !best.own.length" class="m3-hint ob-warn">按最新上报，两台之间没有连通的路线。</p>
        <ol v-else class="m3-rt-list m3-rt-best">
          <li v-for="(r, i) in best.routes" :key="r.nodes.join('>')">
            <span class="m3-no">{{ i + 1 }}</span>
            <span class="m3-rt-chain"><template v-for="(n, k) in r.nodes" :key="n"><template v-if="k"><span class="m3-hop-ms" :data-loss="r.segments[k - 1].loss ? '' : undefined"> {{ segText(r.segments[k - 1]) }} → </span></template><MeshNodeName :id="n" :plain="n === id" /></template></span>
            <span class="m3-rt-state"><b>{{ Number.isFinite(r.rttMs) ? msText(r.rttMs) : '延迟未知' }}</b>
              <span v-if="r.own" class="pb-tag" data-tone="ok">自建第 {{ r.own }} 条</span>
              <span v-else-if="nowNodes.join('>') === r.nodes.join('>')" class="pb-tag">现在走的</span>
              <button v-if="!r.own && canAdd(r.nodes)" type="button" class="ix-text-button" @click="addFrom(r.nodes)">加为自建</button></span>
          </li>
          <li v-for="o in best.own.filter(x => !x.place)" :key="o.id" data-outside>
            <span class="m3-no">·</span>
            <span class="m3-rt-chain"><template v-for="(n, k) in o.nodes" :key="n"><template v-if="k"><span class="m3-hop-ms"> {{ segText(o.segments[k - 1]) }} → </span></template><MeshNodeName :id="n" :plain="n === id" /></template></span>
            <span class="m3-rt-state"><b>{{ Number.isFinite(o.rttMs) ? msText(o.rttMs) : '' }}</b><span class="pb-tag">自建第 {{ o.rank }} 条{{ o.on ? '' : '（停用）' }} · 不在前 10</span></span>
          </li>
        </ol>
        <p class="m3-hint">按实测排序：延迟加上抖动和丢包，和自动选路的算法一样，所以有丢包的路线会排在后面。</p>
      </template>
    </div>
  </MeshFr>

  <MeshDialog :model-value="Boolean(builder)" :title="builder?.routeId ? '修改自建路线' : '自己搭一条路线'" :phone="phone" width="560px" @update:model-value="v => { if (!v) builder = null; }">
    <div v-if="builder" class="m3-form m3-rt-build">
      <p class="instance-muted">从起点按顺序经过中转到终点，回程原路返回。两台之间还没连着的，保存时自动连上。</p>
      <ol class="m3-rt-steps">
        <li class="m3-rt-step"><span class="m3-no">1</span><MeshNodeName :id="builder.src" /><span class="m3-faint">起点</span></li>
        <template v-for="(v, i) in builder.via" :key="v">
          <li class="m3-rt-link" :data-tone="bSeg(i).tone">↓ {{ bSeg(i).text }}</li>
          <li class="m3-rt-step"><span class="m3-no">{{ i + 2 }}</span><MeshNodeName :id="v" />
            <span class="ob-grow" />
            <button type="button" class="ix-icon-button" :disabled="!i" title="往前" :aria-label="`${nm(v)} 往前`" @click="bMove(i, -1)">↑</button>
            <button type="button" class="ix-icon-button" :disabled="i >= builder.via.length - 1" title="往后" :aria-label="`${nm(v)} 往后`" @click="bMove(i, 1)">↓</button>
            <button type="button" class="ix-icon-button" title="去掉" :aria-label="`去掉 ${nm(v)}`" @click="builder.via.splice(i, 1)">✕</button></li>
        </template>
        <li v-if="builder.via.length < MAX_VIA" class="m3-rt-step m3-rt-insert">
          <MeshNodeSelect :options="bRelayChoices" placeholder="＋ 插入一台中转…" aria-label="插入一台中转" width="220px" @change="id => id && builder.via.push(id)" />
        </li>
        <li v-if="builder.via.length && builder.dst" class="m3-rt-link" :data-tone="bSeg(builder.via.length).tone">↓ {{ bSeg(builder.via.length).text }}</li>
        <li class="m3-rt-step"><span class="m3-no">{{ builder.via.length + 2 }}</span>
          <MeshNodeName v-if="builder.routeId" :id="builder.dst" />
          <MeshNodeSelect v-else :model-value="builder.dst" :options="bEndChoices" placeholder="选终点…" aria-label="终点" width="220px" @change="id => builder.dst = id" />
          <span class="m3-faint">终点</span></li>
      </ol>
      <p v-if="bTotal" class="m3-rt-total">
        <template v-if="bTotal.text">{{ bTotal.text }}</template>
        <template v-else-if="bTotal.missing">有 {{ bTotal.missing }} 段测不到，合计暂缺</template>
        <template v-else>合计 <b>{{ msText(bTotal.rtt) }}</b><span v-if="bTotal.probed" class="m3-faint">（其中 {{ bTotal.probed }} 段是刚刚直测的，应用后以实测为准）</span></template>
        <span v-if="bTotal.now != null" class="m3-faint"> · 现在走 {{ msText(bTotal.now) }}</span>
        <span class="ob-grow" />
        <button type="button" class="ix-text-button" :disabled="bMeasuring" @click="measurePoll.run()">{{ bMeasuring ? '测量中…' : '重新测' }}</button>
      </p>
      <small v-if="!builder.via.length" class="ob-warn">至少经过 1 台中转。</small>
      <small v-if="builder.dst && !builder.routeId && pairFull(builder.src, builder.dst)" class="ob-warn">这一对已经有 {{ MAX_PER_PAIR }} 条自建路线，先删掉一条。</small>
      <small v-if="builder.error" class="ob-bad">{{ builder.error }}</small>
    </div>
    <template #footer>
      <div v-if="builder" class="m3-dialog-foot">
        <el-button v-if="builder.rejected" @click="undoBuilder">撤销这次修改</el-button>
        <span class="ob-grow" />
        <el-button @click="builder = null">取消</el-button>
        <el-button type="primary" :loading="builder.saving" :disabled="!bReady" @click="saveBuilder">存入草稿</el-button>
      </div>
    </template>
  </MeshDialog>
</template>
