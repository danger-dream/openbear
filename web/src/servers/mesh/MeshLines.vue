<script setup>
import { computed, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { flag, regionName } from '../serverHelpers.js';
import { relativeTime, timeLabel } from '../../instances/instanceHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorMessage, errorsUnder, isRelay, isSite, msText, nameOf, rowButton, transportLabel, uid } from './meshHelpers.js';
import { addLink, customRelayLinks, planLink, relayPairs } from './meshGraph.js';
import { blankLimit, blankRule, limitReach, limitSentence, reach, ruleSentence } from './meshAuto.js';
import { nodesFrom, routeRtt, routeState } from './meshRoutes.js';
import { relayEdgeId } from './meshEdit.js';
import MeshDialog from './MeshDialog.vue';
import MeshGainPairs from './MeshGainPairs.vue';
import MeshLimit from './MeshLimit.vue';
import MeshLineRule from './MeshLineRule.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// The lines: the user's line rules first, how relay links come about, the round trips between relays,
// every relay link with what of it differs from the automatic values, direct links and members' entries.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}, create: {type: Object, default: null}});
const emit = defineEmits(['edit', 'open']);
const data = computed(() => mesh.data);
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing));
const byId = computed(() => new Map(members.value.map(m => [m.serverId, m])));
const regionOf = id => String(byId.value.get(id)?.region || props.items.find(i => i.id === id)?.region || '').toUpperCase();
const nm = id => nameOf(members.value, id);
const relays = computed(() => members.value.filter(isRelay).map(m => m.serverId).sort((a, b) => regionOf(a).localeCompare(regionOf(b)) || nm(a).localeCompare(nm(b), 'zh')));
const pk = (a, b) => a < b ? `${a}|${b}` : `${b}|${a}`;
const lines = computed(() => data.value?.lines || []);
const lineOf = (kind, a, b) => lines.value.find(l => l.kind === kind && pk(l.a, l.b) === pk(a, b)) || null;
const mode = computed(() => work.value?.relayLinks?.mode || 'full');
const listed = computed(() => new Map((work.value?.relayLinks?.pairs || []).map(p => [pk(p.a, p.b), p])));
const autoPairs = computed(() => data.value?.auto?.pairs || {});
// The optimizer's view: what each automatic link gives now, since when one gives nothing, the last change and what it watches.
const autoLinks = computed(() => data.value?.autoLinks || {});
const benefits = computed(() => autoLinks.value.benefits || {});
// A link the last change made, until the next round measures it: the pairs it was made for.
const madeWith = computed(() => Object.fromEntries((autoLinks.value.lastChange?.added || []).filter(x => x.pairs?.length).map(x => [pk(x.a, x.b), {kind: x.kind, count: x.count, pairs: x.pairs}])));
const holdRounds = computed(() => Number(autoLinks.value.holdRounds || work.value?.settings?.autoLinks?.holdRounds) || 3);
const dropAfterMs = computed(() => (Number(autoLinks.value.dropAfterS || work.value?.settings?.autoLinks?.dropAfterS) || 3600) * 1000);
const LINK_FIELDS = [['transport', '协议'], ['dialer', '主动连的一端'], ['bias', '偏好']];
const biasText = v => v > 0 ? `少走 +${v} ms` : v < 0 ? `优先 ${v} ms` : '正常';
function fieldText(field, value, p) {
  if (field === 'transport') return transportLabel(value);
  if (field === 'bias') return biasText(Number(value) || 0);
  return value === 'a' || value === 'b' ? nm(p[value]) : '谁都可以';
}
// Every relay link: those applied and those only in the draft, each with its round trip.
const pairs = computed(() => {
  const out = new Map(), ids = new Set(relays.value);
  for (const [a, b] of relayPairs(work.value)) out.set(pk(a, b), {a, b});
  for (const p of listed.value.values()) if (ids.has(p.a) && ids.has(p.b)) out.set(pk(p.a, p.b), {a: p.a, b: p.b});
  if (mode.value !== 'full') for (const k of [...out.keys()]) if (!listed.value.has(k)) out.delete(k);
  return [...out.values()].map(x => {
    const key = pk(x.a, x.b), p = listed.value.get(key), l = lineOf('relay', x.a, x.b);
    const ex = p ? autoPairs.value[`${p.a}|${p.b}`] : autoPairs.value[key];
    const diffs = p && ex ? LINK_FIELDS.filter(([f]) => !(p.auto || []).includes(f) && (f === 'dialer' ? ex.dialer.value && ex.dialer.value !== p.dialer : (p[f] ?? (f === 'bias' ? 0 : '')) !== ex[f].value))
      .map(([f, label]) => ({field: f, label, now: fieldText(f, p[f], p), auto: fieldText(f, ex[f].value, p), reason: ex[f].reason || ''})) : [];
    const pIdx = p ? (work.value?.relayLinks?.pairs || []).indexOf(p) : -1;
    const errors = pIdx >= 0 ? errorsUnder(mesh.errors, `relayLinks.pairs[${pIdx}]`).map(errorMessage) : [];
    const origin = p?.origin === 'auto' ? 'auto' : 'manual', measured = origin === 'auto' && autoLinksEnabled.value;
    return {...x, key, p, ex, line: l, rtt: l?.rttMs, transport: p?.transport || ex?.transport?.value || l?.transport || 'udp', bias: p?.bias ?? ex?.bias?.value ?? 0, ruled: ex?.rules || [],
      diffs, differs: diffs.length, auto: p ? (p.auto || []).length === 3 : true, origin, reason: p?.reason || '', errors,
      benefit: measured ? benefits.value[key] || madeWith.value[key] || null : null, idleSince: measured ? Number(autoLinks.value.idleSince?.[key]) || 0 : 0,
      edge: relayEdgeId(work.value, x.a, x.b)};
  });
});
const FILTERS = [['all', '全部'], ['autoOrigin', '自动建立'], ['changed', '手动设置'], ['ruled', '有规则作用的'], ['slow', '超过 130ms']];
const filter = ref('all'), only = ref(''), opened = ref(new Set()), restShown = ref(false);
function toggleOpen(key) {
  const next = new Set(opened.value);
  if (next.has(key)) next.delete(key); else next.add(key);
  opened.value = next;
}
const matchesFilter = (p, f) => (!only.value || p.a === only.value || p.b === only.value)
  && (f === 'autoOrigin' ? p.origin === 'auto' : f === 'ruled' ? p.ruled.length > 0 : f === 'changed' ? p.differs > 0 : f === 'slow' ? p.rtt > 130 : true);
const shown = computed(() => pairs.value.filter(p => matchesFilter(p, filter.value)).sort((a, b) => (a.rtt ?? 1e9) - (b.rtt ?? 1e9)));
// The lines that want a look first; the rest, when there are many of them, folded under their count.
const LINE_GROUPS = [
  ['problem', '需要处理', '断开、还没应用或有错误'],
  ['auto', '自动建立', '自动调整为让节点之间更快而建；不再让任何节点对变快时会自动拆除'],
  ['ok', '其余线路', '运行正常'],
];
const REST_FOLD = 8;
const groupOf = p => p.errors.length || !p.line || p.line.state === 'down' ? 'problem' : p.origin === 'auto' ? 'auto' : 'ok';
const manualText = p => `手动设置：${p.diffs.map(d => `${d.label} ${d.now}`).join('、')}`;
const lineGroups = computed(() => LINE_GROUPS.map(([id, title, note]) => {
  const rows = shown.value.filter(p => groupOf(p) === id);
  const folded = id === 'ok' && filter.value === 'all' && rows.length > REST_FOLD && !restShown.value;
  return {id, title, note, rows, folded};
}).filter(g => g.rows.length));
const plainLines = computed(() => lineGroups.value.length === 1 && lineGroups.value[0].id === 'ok' && !lineGroups.value[0].folded);
const clockOf = ms => new Date(Number(ms)).toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit'});
const durationText = ms => {
  const min = Math.max(1, Math.round(ms / 60000));
  return min < 60 ? `${min} 分钟` : `${Math.floor(min / 60)} 小时${min % 60 ? ` ${min % 60} 分钟` : ''}`;
};
const idleText = since => {
  const now = Date.now(), left = since + dropAfterMs.value - now;
  return `已 ${durationText(now - since)}没有让任何节点对变快，${left > 0 ? `约 ${durationText(left)}后` : '下一轮'}自动拆除`;
};
const pairAt = computed(() => new Map(pairs.value.map(p => [pk(p.a, p.b), p])));
const band = ms => !Number.isFinite(ms) ? '' : ms < 50 ? 'good' : ms < 130 ? 'mid' : 'far';

// ---- connection limits ----
const limits = computed(() => work.value?.limits || []);
const limitOpen = ref('');
const rulePairs = computed(() => pairs.value.map(p => ({a: p.a, b: p.b})));
function newLimit() {
  const k = {...blankLimit(), id: uid('k')};
  work.value.limits = [...limits.value, k];
  void save();
  limitOpen.value = k.id;
}
function toggleLimit(k, on) { k.on = on; void save(); }
const limitReachOf = k => limitReach(k, rulePairs.value, work.value?.directLinks || [], members.value, regionOf);
const limitErrorsOf = i => errorsUnder(mesh.errors, `limits[${i}]`).map(errorMessage);

// ---- own routes: every pair's, in order; made and changed on a node's page ----
const paths = computed(() => work.value?.paths || []);
const routeViews = computed(() => new Map((data.value?.pathsView || []).map(v => [v.id, v])));
const routePairs = computed(() => {
  const out = new Map();
  for (const p of paths.value) {
    const key = [p.from, p.to].sort().join('|');
    if (!out.has(key)) out.set(key, {a: p.from, b: p.to, routes: []});
    const g = out.get(key), view = routeViews.value.get(p.id) || null;
    g.routes.push({p, nodes: nodesFrom(p, g.a), state: routeState(p, view, g.a, nm), rtt: routeRtt(view)});
  }
  return [...out.values()];
});

// ---- line rules ----
const rules = computed(() => work.value?.lineRules || []);
const regionText = code => regionName(code) || code;
const ruleOpen = ref('');
function newRule() {
  const r = {...blankRule(), id: uid('l')};
  work.value.lineRules = [...rules.value, r];
  void save();
  ruleOpen.value = r.id;
}
function toggleRule(r, on) { r.on = on; void save(); }
const reachOf = r => reach(rules.value, r, rulePairs.value, members.value, regionOf);

// ---- how relay links come about & autoLinks ----
function setMode(next) {
  if (next === mode.value) return;
  if (next === 'custom' && mode.value === 'full') customRelayLinks(work.value);
  else work.value.relayLinks = {...work.value.relayLinks, mode: next};
  void save();
}
const autoLinksEnabled = computed(() => work.value?.settings?.autoLinks?.enabled !== false);
// Members still entering by one relay: the mesh leaves them so until the user moves them.
const singleEntry = computed(() => members.value.filter(m => !isRelay(m) && m.entry && m.entry.mode !== 'upstreams'));
async function allToUpstreams() {
  const list = [...singleEntry.value];
  if (!list.length) return;
  try {
    await ElMessageBox.confirm(`把 ${list.length} 台成员的入口中转都改为「同时连上」：每台同时连着它列出的所有入口，去每个目标自动挑最快的。应用后生效，也可以在节点页逐台改、随时改回。`,
      '全部改为同时连上', {confirmButtonText: '全部改为同时连上', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  for (const m of list) m.entry = {...m.entry, mode: 'upstreams', autoReturn: true};
  void save();
}
function setAutoLinksEnabled(on) {
  if (!work.value.settings) work.value.settings = {};
  work.value.settings.autoLinks = {...(work.value.settings.autoLinks || {}), enabled: on};
  void save();
}
const AUTO_KIND = {relay: '中转线路', entry: '入口中转'};
// An upstream reads member → relay; a relay link as it is keyed.
const endsOf = x => x.kind === 'entry' && isRelay(byId.value.get(x.a)) ? [x.b, x.a] : [x.a, x.b];
// One change of the optimizer, its node pairs as recorded then or, for older records, as the link gives now.
function autoItem(x, op) {
  const [a, b] = endsOf(x), key = pk(x.a, x.b), live = op === 'add' && !x.pairs ? benefits.value[key] : null;
  const src = x.pairs ? x : live;
  return {op, key: `${op}:${key}`, kind: x.kind, a, b, why: x.why || '', reason: x.reason || '', count: src ? Number(src.count) || 0 : null, pairs: src?.pairs || [], liveNow: Boolean(live)};
}
const lastAutoChange = computed(() => {
  const lc = autoLinks.value.lastChange;
  if (!lc) return null;
  if (typeof lc === 'string') return {text: lc, at: 0, items: []};
  const items = [...(lc.added || []).map(x => autoItem(x, 'add')), ...(lc.dropped || []).map(x => autoItem(x, 'drop'))];
  const text = lc.skipped ? lc.reason || '自动版本没有生成' : items.length ? '' : lc.text || lc.summary || lc.reason || '已自动调整线路';
  return {text, skipped: Boolean(lc.skipped), at: Number(lc.at || lc.since || 0), items,
    added: items.filter(i => i.op === 'add').length, dropped: items.filter(i => i.op === 'drop').length};
});
const watching = computed(() => autoLinksEnabled.value ? (autoLinks.value.suggestions || []).map(x => ({...autoItem(x, 'add'), key: `watch:${pk(x.a, x.b)}`, rounds: Number(x.rounds) || 0, ready: Boolean(x.ready)})) : []);

// ---- new links ----
const adding = ref(null);
function startAdd(kind, a = '', b = '') { adding.value = {kind, a, b, error: ''}; }
watch(() => props.create, c => {
  if (!c) return;
  if (c.kind === 'relay' || c.kind === 'direct') startAdd(c.kind);
}, {immediate: true});
const addChoices = computed(() => adding.value?.kind === 'relay' ? members.value.filter(isRelay) : members.value.filter(m => !isRelay(m)));
const addOptions = computed(() => addChoices.value.map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId)})));
function confirmAdd() {
  const x = adding.value, plan = planLink(work.value, x.a, x.b, x.kind);
  if (plan.error) { x.error = plan.error; return; }
  const next = addLink(work.value, plan);
  if (plan.kind === 'relay') { const p = work.value.relayLinks.pairs.at(-1); p.auto = ['transport', 'dialer', 'bias']; }
  void save();
  adding.value = null;
  emit('edit', next);
}
function cellClick(a, b) {
  const p = pairAt.value.get(pk(a, b));
  if (p?.edge) emit('edit', {kind: 'edge', id: p.edge});
  else if (mode.value === 'full') ElMessage.info('所有中转两两相连时，每一对都有线路');
  else startAdd('relay', a, b);
}

const directs = computed(() => (work.value?.directLinks || []).map((d, i) => ({
  ...d, i, line: lineOf('direct', d.a, d.b),
  errors: errorsUnder(mesh.errors, `directLinks[${i}]`).map(errorMessage),
})).filter(d => byId.value.has(d.a) && byId.value.has(d.b)));
const entries = computed(() => (work.value?.members || []).map((m, idx) => ({m, idx})).filter(({m}) => !m.removing && !isRelay(m)).map(({m, idx}) => ({
  m, idx, now: data.value?.nodes?.[m.serverId]?.summary?.entry?.relay || '',
  errors: errorsUnder(mesh.errors, `members[${idx}].entry`).map(errorMessage),
})));
</script>
<template>
  <section class="m3-pane" aria-label="线路">
    <header class="m3-pane-head">
      <h2>线路</h2><span class="ob-grow" />
      <el-button @click="startAdd('relay')">＋ 新建中转线路</el-button>
      <el-button @click="startAdd('direct')">＋ 新建直连</el-button>
    </header>
    <p class="instance-muted">中转之间连成线路，组网按实测延迟自动选最快的路走。成员经中转接入（在各台成员的详情页设置）；两台成员也可以直连。</p>

    <div class="m3-box m3-rulebox">
      <section class="m3-rulebox-sec" aria-label="连接限制">
        <div class="m3-rulebox-h"><b>连接限制</b><small>硬规则：某些范围之间不许直连、只准特定协议或必须经过指定节点；手动改动和自动调整都不能越界</small>
          <span class="m3-faint">{{ limits.length ? `${limits.length} 条` : '还没有' }}</span><el-button size="small" @click="newLimit">＋ 新建限制</el-button></div>
        <ol v-if="limits.length" class="m3-limits-list">
          <li v-for="(k, i) in limits" :key="k.id" :data-off="k.on === false || undefined">
            <span class="m3-no">{{ i + 1 }}</span>
            <button type="button" class="m3-list-main" @click="limitOpen = k.id"><b>{{ k.name }}</b><small>{{ limitSentence(k, nm, regionText) }}</small>
              <small v-for="e in limitErrorsOf(i)" :key="e" class="ob-bad">{{ e }}</small></button>
            <span class="m3-faint">{{ k.on === false ? '已停用' : `涉及 ${limitReachOf(k).total} 条连接` }}</span>
            <el-switch :model-value="k.on !== false" :aria-label="`启用限制 ${k.name}`" @update:model-value="toggleLimit(k, $event)" />
          </li>
        </ol>
      </section>
      <section class="m3-rulebox-sec" aria-label="自建路线">
        <div class="m3-rulebox-h"><b>自建路线</b><small>两台之间按顺序经过指定的中转，每对最多 3 条；在节点页「路线」里新建和调整</small>
          <span class="m3-faint">{{ routePairs.length ? `${routePairs.length} 对 · ${paths.length} 条` : '还没有' }}</span></div>
        <ol v-if="routePairs.length" class="m3-paths-list">
          <li v-for="g in routePairs" :key="g.a + g.b">
            <button type="button" class="m3-list-main" @click="emit('open', g.a)">
              <b><MeshNodeName :id="g.a" plain /> ⇄ <MeshNodeName :id="g.b" plain /></b>
              <ol class="m3-rt-list">
                <li v-for="(r, i) in g.routes" :key="r.p.id" :data-off="r.p.on === false || undefined">
                  <span class="m3-no">{{ i + 1 }}</span>
                  <span class="m3-rt-chain">{{ r.nodes.map(nm).join(' → ') }}</span>
                  <span class="m3-rt-state" :data-tone="r.state.tone">{{ r.state.text }}<template v-if="r.rtt != null && r.state.tone !== 'bad'"> · {{ msText(r.rtt) }}</template></span>
                </li>
              </ol>
            </button>
          </li>
        </ol>
      </section>
      <section class="m3-rulebox-sec" aria-label="线路规则">
        <div class="m3-rulebox-h"><b>线路规则</b><small>对某些节点之间的线路指定协议、谁主动连或偏好；没有规则时按对方开着的最快协议和实测延迟自动定，排在上面的优先</small>
          <span class="m3-faint">{{ rules.length ? `${rules.length} 条` : '还没有' }}</span><el-button size="small" @click="newRule">＋ 新建规则</el-button></div>
        <ol v-if="rules.length" class="m3-list">
          <li v-for="(r, i) in rules" :key="r.id" :data-off="r.on === false || undefined">
            <span class="m3-no">{{ i + 1 }}</span>
            <button type="button" class="m3-list-main" @click="ruleOpen = r.id"><b>{{ r.name }}</b><small>{{ ruleSentence(r, nm, regionText) }}</small></button>
            <span class="m3-faint">{{ r.on === false ? '已停用' : `作用于 ${reachOf(r).lines.length} 条线路${reachOf(r).entries ? `、${reachOf(r).entries} 个接入` : ''}` }}</span>
            <el-switch :model-value="r.on !== false" :aria-label="`启用规则 ${r.name}`" @update:model-value="toggleRule(r, $event)" />
          </li>
        </ol>
      </section>
    </div>

    <div class="m3-box m3-mode">
      <span class="m3-faint">中转之间的线路怎么来</span>
      <div class="m3-radio m3-radio-row" role="radiogroup" aria-label="中转之间的线路怎么来">
        <label :data-on="mode === 'auto' || undefined"><input type="radio" name="m3-link-mode" :checked="mode === 'auto'" @change="setMode('auto')" />
          <span><span class="m3-radio-label">自动生成</span><small class="instance-muted">每台中转连实测最近的几台，再按上面的规则调整；新加的中转自动连上</small></span></label>
        <label :data-on="mode === 'full' || undefined"><input type="radio" name="m3-link-mode" :checked="mode === 'full'" @change="setMode('full')" />
          <span><span class="m3-radio-label">所有中转两两相连</span><small class="instance-muted">{{ relays.length }} 台中转就是 {{ relays.length * (relays.length - 1) / 2 }} 条线路</small></span></label>
        <label :data-on="mode === 'custom' || undefined"><input type="radio" name="m3-link-mode" :checked="mode === 'custom'" @change="setMode('custom')" />
          <span><span class="m3-radio-label">手动</span><small class="instance-muted">一条条自己加，现在有 {{ (work?.relayLinks?.pairs || []).length }} 条</small></span></label>
      </div>
      <div class="m3-autolinks-bar">
        <button type="button" class="m3-sw" role="switch" :aria-checked="autoLinksEnabled" aria-label="自动调整线路" @click="setAutoLinksEnabled(!autoLinksEnabled)"><i></i><span>自动调整线路</span></button>
        <span class="instance-muted">{{ autoLinksEnabled ? `按实测自动补更快的中转线路和成员的入口中转，遵守连接限制：连续 ${holdRounds} 轮都更快才新建，${durationText(dropAfterMs)}没有收益就拆除，两次调整至少隔 10 分钟` : '已关闭：不测速，也不自动改线路' }}</span>
      </div>
      <div v-if="lastAutoChange" class="m3-auto-log">
        <div class="m3-auto-h"><b>最近一次调整</b><span class="m3-faint"><template v-if="lastAutoChange.at"><time :title="timeLabel(lastAutoChange.at)">{{ relativeTime(lastAutoChange.at, Date.now()) }}</time></template><template v-if="lastAutoChange.added"> · 新建 {{ lastAutoChange.added }} 条</template><template v-if="lastAutoChange.dropped"> · 拆除 {{ lastAutoChange.dropped }} 条</template></span></div>
        <p v-if="lastAutoChange.text" :class="lastAutoChange.skipped ? 'ob-warn' : 'instance-muted'">{{ lastAutoChange.text }}</p>
        <ul v-if="lastAutoChange.items.length" class="m3-auto-list">
          <li v-for="x in lastAutoChange.items" :key="x.key">
            <div class="m3-auto-item">
              <span class="pb-tag" :data-tone="x.op === 'add' ? 'ok' : undefined">{{ x.op === 'add' ? '新建' : '拆除' }}</span>
              <span class="m3-auto-who"><MeshNodeName :id="x.a" /><span class="m3-faint">{{ x.kind === 'entry' ? ' → ' : ' ⇄ ' }}</span><MeshNodeName :id="x.b" /><span class="m3-tag">{{ AUTO_KIND[x.kind] || x.kind }}</span></span>
              <span v-if="x.op === 'drop'" class="m3-auto-what">{{ x.why || '不再让节点之间变快' }}</span>
              <span v-else-if="x.pairs.length" class="m3-auto-what">让 {{ x.count }} 对节点变快，最多：<MeshNodeName :id="x.pairs[0].src" /> → <MeshNodeName :id="x.pairs[0].dst" /> {{ Math.round(x.pairs[0].before) }} → {{ Math.round(x.pairs[0].after) }} ms<template v-if="x.liveNow">（按现在的实测）</template></span>
              <span v-else class="m3-auto-what">{{ x.reason }}</span>
              <button v-if="x.pairs.length" type="button" class="ix-text-button" :aria-expanded="opened.has(x.key)" @click="toggleOpen(x.key)">{{ opened.has(x.key) ? '收起' : `查看 ${x.count} 对` }}</button>
            </div>
            <MeshGainPairs v-if="opened.has(x.key)" :pairs="x.pairs" :count="x.count" />
          </li>
        </ul>
      </div>
      <div v-if="autoLinksEnabled" class="m3-auto-log">
        <div class="m3-auto-h"><b>正在观察</b><span class="m3-faint">每分钟测一轮，连续 {{ holdRounds }} 轮都更快才新建<template v-if="autoLinks.measuredAt"> · 上一轮 <time :title="timeLabel(autoLinks.measuredAt)">{{ clockOf(autoLinks.measuredAt) }}</time></template></span></div>
        <p v-if="!watching.length" class="instance-muted">暂时没有更快的候选</p>
        <ul v-else class="m3-auto-list">
          <li v-for="x in watching" :key="x.key">
            <div class="m3-auto-item">
              <span class="pb-tag" :data-tone="x.ready ? 'ok' : 'info'">{{ x.ready ? '下一轮新建' : `${Math.min(x.rounds, holdRounds)}/${holdRounds} 轮` }}</span>
              <span class="m3-auto-who"><MeshNodeName :id="x.a" /><span class="m3-faint">{{ x.kind === 'entry' ? ' → ' : ' ⇄ ' }}</span><MeshNodeName :id="x.b" /><span class="m3-tag">{{ AUTO_KIND[x.kind] || x.kind }}</span></span>
              <span v-if="x.pairs.length" class="m3-auto-what">预计让 {{ x.count }} 对节点变快，最多：<MeshNodeName :id="x.pairs[0].src" /> → <MeshNodeName :id="x.pairs[0].dst" /> {{ Math.round(x.pairs[0].before) }} → {{ Math.round(x.pairs[0].after) }} ms</span>
              <span v-else class="m3-auto-what">{{ x.reason }}</span>
              <button v-if="x.pairs.length" type="button" class="ix-text-button" :aria-expanded="opened.has(x.key)" @click="toggleOpen(x.key)">{{ opened.has(x.key) ? '收起' : `查看 ${x.count} 对` }}</button>
            </div>
            <MeshGainPairs v-if="opened.has(x.key)" :pairs="x.pairs" :count="x.count" />
          </li>
        </ul>
      </div>
      <div v-if="singleEntry.length" class="m3-autolinks-bar">
        <span class="instance-muted">{{ singleEntry.length }} 台成员一次只用一个入口中转（主备或只用第一个），自动调整和自建路线用不到它们</span>
        <button type="button" class="ix-text-button" @click="allToUpstreams">全部改为同时连上</button>
      </div>
    </div>

    <h3 class="m3-sec-h">中转线路<small>{{ pairs.length }} 条</small><span class="ob-grow" />
      <span class="m3-chips">
        <button v-if="only" type="button" class="m3-chip-btn" aria-pressed="true" @click="only = ''">只看 {{ nm(only) }} ✕</button>
        <button v-for="[k, l] in FILTERS" :key="k" type="button" class="m3-chip-btn" :aria-pressed="filter === k" @click="filter = k">{{ l }}<span class="m3-faint"> {{ pairs.filter(p => matchesFilter(p, k)).length }}</span></button>
      </span></h3>
    <ul class="m3-rows m3-rows-lines">
      <template v-for="g in lineGroups" :key="g.id">
        <li v-if="!plainLines" class="m3-rows-group" :data-group="g.id">
          <button v-if="g.id === 'ok' && filter === 'all' && g.rows.length > REST_FOLD" type="button" class="m3-rows-group-btn" :aria-expanded="!g.folded" @click="restShown = !restShown">
            <i class="m3-chev" aria-hidden="true">▸</i><b>{{ g.title }}</b><span class="m3-faint">{{ g.rows.length }} 条 · {{ g.note }}</span><span class="m3-rows-group-act">{{ g.folded ? '展开' : '收起' }}</span></button>
          <template v-else><b>{{ g.title }}</b><span class="m3-faint">{{ g.rows.length }} 条 · {{ g.note }}</span></template></li>
        <template v-if="!g.folded">
          <li v-for="p in g.rows" :key="p.key" :data-group="g.id">
            <div class="m3-row" :class="{'m3-row-click': p.edge}" v-bind="p.edge ? rowButton(() => emit('edit', {kind: 'edge', id: p.edge})) : {}">
              <span class="m3-row-name"><b><MeshNodeName :id="p.a" /></b><span class="m3-faint"> ⇄ </span><b><MeshNodeName :id="p.b" /></b>
                <span class="m3-tag">{{ p.origin === 'auto' ? '自动' : '手动' }}</span>
                <span v-if="p.differs" class="m3-tag" :title="manualText(p)">手动设置 {{ p.differs }} 项</span>
                <span v-if="!p.line" class="pb-tag" data-tone="warn">未应用</span>
                <small v-if="p.origin === 'auto' && p.benefit?.count" class="m3-line-note">
                  <span>让 {{ p.benefit.count }} 对节点变快，最多：<MeshNodeName :id="p.benefit.pairs[0].src" /> → <MeshNodeName :id="p.benefit.pairs[0].dst" /> {{ Math.round(p.benefit.pairs[0].before) }} → {{ Math.round(p.benefit.pairs[0].after) }} ms</span>
                  <button type="button" class="ix-text-button" :aria-expanded="opened.has(p.key)" @click.stop="toggleOpen(p.key)">{{ opened.has(p.key) ? '收起' : `查看 ${p.benefit.count} 对` }}</button>
                </small>
                <small v-else-if="p.origin === 'auto' && p.benefit" class="m3-line-note" data-tone="warn"><span>{{ p.idleSince ? idleText(p.idleSince) : '现在没有让任何节点对变快' }}</span></small>
                <small v-else-if="p.origin === 'auto' && p.reason" class="m3-line-note"><span :title="p.reason">建立时：{{ p.reason }}</span></small>
                <small v-for="e in p.errors" :key="e" class="ob-bad m3-line-err">{{ e }}</small>
              </span>
              <span class="m3-row-role">{{ isSite(byId.get(p.a)) && isSite(byId.get(p.b)) ? 'UDP 打洞' : transportLabel(p.transport) }}</span>
              <span class="m3-row-role">{{ biasText(p.bias) }}</span>
              <span class="m3-row-addr" :class="p.line?.state === 'down' ? 'ob-bad' : ''">{{ p.line?.state === 'down' ? '断开' : Number.isFinite(p.rtt) ? msText(p.rtt) : '—' }}</span>
              <span class="m3-row-tags"><span v-if="p.ruled.length" class="m3-faint">按规则</span><span v-else class="m3-faint">{{ p.auto ? '全自动' : '' }}</span></span>
            </div>
            <div v-if="opened.has(p.key) && p.benefit?.count" class="m3-line-more">
              <MeshGainPairs :pairs="p.benefit.pairs" :count="p.benefit.count" />
              <small v-if="p.reason" class="m3-faint">建立时：{{ p.reason }}</small>
            </div>
          </li>
        </template>
      </template>
      <li v-if="!shown.length" class="instance-muted">没有符合条件的线路。</li>
    </ul>

    <h3 class="m3-sec-h">中转之间的延迟<small>格子里是实测延迟（ms）；点格子看这条线路，点空格子新建一条</small></h3>
    <div class="m3-table-wrap m3-mx-wrap">
      <table class="m3-mx">
        <thead><tr><th class="corner"></th><th v-for="id in relays" :key="id" :title="nm(id)"><MeshNodeName :id="id" stacked /></th></tr></thead>
        <tbody>
          <tr v-for="a in relays" :key="a">
            <th><MeshNodeName :id="a" /></th>
            <template v-for="b in relays" :key="b">
              <td v-if="a === b" class="self"></td>
              <td v-else-if="!pairAt.get(pk(a, b))" class="e" :title="`新建 ${nm(a)} ⇄ ${nm(b)}`" @click="cellClick(a, b)">＋</td>
              <td v-else :class="band(pairAt.get(pk(a, b)).rtt)" :title="`${nm(a)} ⇄ ${nm(b)} · ${transportLabel(pairAt.get(pk(a, b)).transport)}`" @click="cellClick(a, b)">
                {{ Number.isFinite(pairAt.get(pk(a, b)).rtt) ? Math.round(pairAt.get(pk(a, b)).rtt) : '·' }}<span v-if="pairAt.get(pk(a, b)).bias > 0" class="av">少</span></td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>

    <h3 class="m3-sec-h">直连<small>两台成员之间不经中转</small></h3>
    <ul class="m3-rows">
      <li v-for="d in directs" :key="d.i">
        <div class="m3-row m3-row-click" v-bind="rowButton(() => emit('edit', {kind: 'edge', id: `direct:${d.i}`}))">
          <span class="m3-row-name"><b><MeshNodeName :id="d.a" /></b><span class="m3-faint"> ⇄ </span><b><MeshNodeName :id="d.b" /></b>
            <span class="m3-tag">{{ d.origin === 'auto' ? '自动' : '手动' }}</span>
            <small v-if="d.origin === 'auto' && d.reason" class="m3-line-reason">{{ d.reason }}</small>
            <small v-for="e in d.errors" :key="e" class="ob-bad m3-line-err">{{ e }}</small></span>
          <span class="m3-row-role">{{ d.transport === 'udp' ? 'UDP 打洞' : transportLabel(d.transport) }}</span>
          <span class="m3-row-addr">{{ Number.isFinite(d.line?.rttMs) ? msText(d.line.rttMs) : '—' }}</span>
        </div>
      </li>
      <li v-if="!directs.length" class="instance-muted">没有直连。</li>
    </ul>

    <h3 class="m3-sec-h">成员接入<small>每台成员经哪几台中转连进来；点一行到它的详情页改</small></h3>
    <ul class="m3-rows m3-rows-entries">
      <li v-for="e in entries" :key="e.m.serverId">
        <div class="m3-row m3-row-click" v-bind="rowButton(() => emit('open', e.m.serverId))">
          <span class="m3-row-name"><b><MeshNodeName :id="e.m.serverId" /></b>
            <small v-for="err in e.errors" :key="err" class="ob-bad m3-line-err">{{ err }}</small></span>
          <span class="m3-row-conn"><template v-for="(c, i) in e.m.entry?.candidates || []" :key="c.id || c.relay"><span v-if="i" class="m3-faint">{{ e.m.entry?.mode === 'upstreams' ? ' · ' : ' → ' }}</span><span class="m3-hopname" :class="{'m3-cur': c.relay === e.now}"><MeshNodeName :id="c.relay" /> <span class="m3-faint">{{ transportLabel(c.transport) }}</span><span v-if="c.origin === 'auto'" class="m3-tag">自动</span></span></template>
            <span v-if="!(e.m.entry?.candidates || []).length" class="ob-warn">还没有设置</span></span>
          <span class="m3-row-addr"><template v-if="e.now">现在经 <MeshNodeName :id="e.now" /></template><template v-else>—</template></span>
        </div>
      </li>
    </ul>

    <MeshDialog :model-value="Boolean(limitOpen)" title="连接限制" :phone="phone" width="640px" @update:model-value="v => { if (!v) limitOpen = ''; }">
      <MeshLimit v-if="limitOpen" :limit-id="limitOpen" :pairs="rulePairs" :region-of="regionOf" @close="limitOpen = ''" />
    </MeshDialog>
    <MeshDialog :model-value="Boolean(ruleOpen)" title="线路规则" :phone="phone" width="640px" @update:model-value="v => { if (!v) ruleOpen = ''; }">
      <MeshLineRule v-if="ruleOpen" :rule-id="ruleOpen" :pairs="rulePairs" :region-of="regionOf" @close="ruleOpen = ''" />
    </MeshDialog>
    <MeshDialog :model-value="Boolean(adding)" :title="adding?.kind === 'relay' ? '新建中转线路' : '新建直连'" :phone="phone" @update:model-value="v => { if (!v) adding = null; }">
      <div v-if="adding" class="m3-form">
        <div class="m3-field"><span>一端</span><MeshNodeSelect v-model="adding.a" :options="addOptions" placeholder="选择…" aria-label="一端" width="100%" /></div>
        <div class="m3-field"><span>另一端</span><MeshNodeSelect v-model="adding.b" :options="addOptions" placeholder="选择…" aria-label="另一端" width="100%" /></div>
        <p class="instance-muted">{{ adding.kind === 'relay' ? '协议、谁主动连、选路偏好都先交给自动，加好后可以在线路设置里改。' : '默认 UDP 打洞，两端都在 NAT 后也能连。' }}</p>
        <small v-if="adding.error" class="ob-bad">{{ adding.error }}</small>
        <div class="m3-dialog-foot"><span class="ob-grow" /><el-button @click="adding = null">取消</el-button><el-button type="primary" :disabled="!adding.a || !adding.b" @click="confirmAdd">新建</el-button></div>
      </div>
    </MeshDialog>
  </section>
</template>
