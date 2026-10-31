<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { MeshApi } from './meshApi.js';
import { directPeers, linkLive, mesh, save } from './meshStore.js';
import { candLine, codeText, costText, DIRECT_VIA, directErrorText, directLive, directReason, directStateText, durationText, errorMessage, errorsUnder, isPunched, isRelay, isSite,
  LINE_STATES, lineEnd, lineOf, LINK_STATES, listens, lossText, move, msText, nameOf, needsPunch, publicAddress, reachOf, relayMetric, rtt, sameExit, TRANSPORTS, transportLabel, warningsUnder } from './meshHelpers.js';
import { customRelayLinks, entryLook, lineIdOf, punchedRelayLink, standbyDetail } from './meshGraph.js';
import MeshField from './MeshField.vue';
import MeshLineTrend from './MeshLineTrend.vue';
import { usePoll } from './usePoll.js';

// Properties of the selected link: an entry candidate, a relay link (automatic or
// set one by one) or a direct link. Deleting goes through the board, like the Delete key.
const props = defineProps({edgeId: {type: String, required: true}});
const emit = defineEmits(['remove', 'select']);
const work = computed(() => mesh.work);
const members = computed(() => work.value?.members || []);
const find = id => members.value.find(m => m.serverId === id);
const ports = computed(() => ({udp: 51999, tcp: 52000, tls: 52001, ...(work.value?.settings?.ports || {})}));

const link = computed(() => {
  const [kind, ...rest] = props.edgeId.split(':'), key = rest.join(':');
  if (kind === 'entry') {
    const [memberId, candId] = key.split(':'), member = find(memberId), index = member?.entry?.candidates?.findIndex(c => c.id === candId) ?? -1;
    if (index < 0) return null;
    const c = member.entry.candidates[index];
    return {kind, member, index, c, a: memberId, b: c.relay, path: `members[${members.value.indexOf(member)}].entry.candidates[${index}]`};
  }
  if (kind === 'relay') { const [a, b] = key.split('~'); return find(a) && find(b) ? {kind, auto: true, a, b} : null; }
  const list = kind === 'pair' ? work.value?.relayLinks?.pairs : kind === 'direct' ? work.value?.directLinks : null, p = list?.[Number(key)];
  return p ? {kind: kind === 'pair' ? 'relay' : kind, p, a: p.a, b: p.b, path: `${kind === 'pair' ? 'relayLinks.pairs' : 'directLinks'}[${key}]`} : null;
});
// Punched: UDP direct links between members, and links between two site relays.
const relayPunched = computed(() => link.value?.kind === 'relay' && punchedRelayLink(work.value, link.value.a, link.value.b));
const punched = computed(() => (link.value?.kind === 'direct' && isPunched(link.value.p)) || relayPunched.value);
const errors = computed(() => link.value?.path ? errorsUnder(mesh.errors, link.value.path) : []);
// Two site relays always punch; a UDP direct link only when both ends are behind NAT.
const punch = computed(() => relayPunched.value || (punched.value ? needsPunch(mesh.data, link.value.a, link.value.b) : null));
// A punched link's state as the worse of its two ends reports it.
const live = computed(() => punched.value ? directLive(directPeers(link.value.a, props.edgeId), directPeers(link.value.b, props.edgeId), link.value.a, link.value.b) : null);
// Since, attempts and the last error are in the ends' member detail: read it only while this link is selected.
const endsPoll = usePoll(current => {
  const l = link.value;
  return Promise.all([l.a, l.b].map(id => MeshApi.member(id).then(d => {
    if (current() && Array.isArray(d?.status?.peers)) linkLive.peers[id] = d.status.peers;
  }, () => {})));
}, {interval: () => punched.value && link.value ? 5000 : 0});
watch(() => punched.value && `${link.value.a}~${link.value.b}`, () => {
  if (punched.value && link.value && linkLive.id !== props.edgeId) Object.assign(linkLive, {id: props.edgeId, peers: {}});
  void endsPoll.run();
}, {immediate: true});
onBeforeUnmount(() => {
  if (linkLive.id === props.edgeId) Object.assign(linkLive, {id: '', peers: {}});
});
const liveFacts = computed(() => {
  const l = live.value;
  if (!l) return [];
  const state = relayPunched.value && l.state === 'relayed' ? '未打通，经其他中转绕行' : directStateText(l.state, punch.value);
  return [[relayPunched.value ? '打洞' : '状态', state, l.state === 'direct' ? 'ob-ok' : l.state === 'relayed' ? 'ob-warn' : ''],
    ['经由', l.state === 'direct' ? DIRECT_VIA[l.via] || '直接线路' : l.state === 'relayed' ? '中转' : ''],
    ['延迟', l.state === 'direct' && !metric.value ? rtt(l) : ''],
    ['持续', l.since ? durationText(Date.now() - l.since) : ''],
    ['尝试次数', Number.isFinite(l.attempts) ? `${l.attempts} 次` : ''],
    ['最近错误', l.lastError ? directErrorText(l.lastError) : '无', l.lastError ? 'ob-bad' : '']].filter(([, v]) => v);
});
const liveNote = computed(() => {
  const l = live.value;
  if (!l) return '';
  const both = l.differ ? `两端不一致，按较差的一端显示：${l.ends.map(e => `${nameOf(members.value, e.from)} ${directStateText(e.state, punch.value)}`).join('，')}。` : '';
  return l.state === 'direct' && !both ? '' : `${l.state === 'direct' ? '' : `${directReason(l, punch.value)}。`}${both}`;
});
const look = computed(() => link.value?.kind === 'entry' ? entryLook(work.value, mesh.data, mesh.errors, link.value.member, link.value.index) : null);

// The line this link stands for, as the controller reports it (GET /mesh lines): each end's addresses and who dials.
const lineId = computed(() => link.value ? lineIdOf(work.value, props.edgeId) : '');
const line = computed(() => lineOf(mesh.data, lineId.value));
const lineState = computed(() => {
  const l = line.value;
  if (!l) return null;
  const [tone, label] = LINE_STATES[l.state] || LINE_STATES.unknown;
  return {tone, text: [label, l.state === 'up' || l.state === 'standby' ? msText(l.rttMs) : '', Number(l.lossRate) > 0 ? `丢包 ${lossText(l.lossRate)}` : '',
    l.punch === true && !(link.value?.kind === 'direct' && punch.value === false) ? '打洞' : ''].filter(Boolean).join(' · ')};
});
const lineEnds = computed(() => line.value ? ['a', 'b'].map(k => ({key: k, id: line.value[k], name: nameOf(members.value, line.value[k]), info: lineEnd(line.value.ends?.[k], line.value.transport)})) : []);
const cold = computed(() => line.value?.state === 'cold' || line.value?.role === 'cold' || Boolean(look.value?.cold));
const entryRelay = computed(() => link.value?.kind === 'entry' ? find(link.value.c.relay) : null);

// A link between relays as its ends measure it (summary.routing.links).
const metric = computed(() => link.value?.kind === 'relay' ? relayMetric(mesh.data, link.value.a, link.value.b) : null);
const metricFacts = computed(() => {
  const m = metric.value;
  if (!m) return [];
  return [['线路', LINK_STATES[m.state][1], m.state === 'up' ? '' : m.state === 'down' ? 'ob-bad' : 'ob-warn'], ['延迟', m.state === 'up' ? msText(m.rttMs) : ''], ['抖动', m.state === 'up' ? msText(m.jitterMs) : ''],
    ['丢包', lossText(m.loss)], ['代价', costText(m)]].filter(([, v]) => v);
});
const metricNote = computed(() => {
  const m = metric.value;
  if (!m) return '';
  if (m.differ) return `两端测得不同，按较差的一端显示：${m.ends.map(e => `${nameOf(members.value, e.from)} ${LINK_STATES[e.state][1]}`).join('，')}。`;
  return m.ends.length === 1 ? `${nameOf(members.value, m.from)} 测得。` : '';
});
// Who connects: two site relays punch; a site relay dials the public one; else as set.
const dialing = computed(() => {
  const l = link.value;
  if (l?.kind !== 'relay' || relayPunched.value) return null;
  const a = find(l.a), b = find(l.b);
  if (isSite(a) !== isSite(b)) { const site = isSite(a) ? a : b; return {fixed: true, dialer: site, passive: site === a ? b : a}; }
  if (l.auto) return {fixed: true, dialer: null};
  return {fixed: false, dialer: find(l.p.dialer === 'b' ? l.p.b : l.p.a), passive: find(l.p.dialer === 'b' ? l.p.a : l.p.b)};
});
const method = computed(() => {
  if (relayPunched.value) return '打洞：两台都是站点中转';
  const d = dialing.value;
  return d?.dialer && d.passive ? `拨号：${d.dialer.name} 主动连接 ${d.passive.name}` : '拨号';
});
const bias = computed(() => Number(link.value?.p?.bias) || 0);
// What is typed stays while typing: polls re-render this panel every few seconds.
const biasText = ref(String(bias.value));
let biasEditing = false;
watch(bias, v => { if (!biasEditing) biasText.value = String(v); });
const biasFocus = on => { biasEditing = on; if (!on) biasText.value = String(bias.value); };
// Preference, in milliseconds, added to the measured cost at both ends. An automatic link becomes one set by itself.
function setBias(value) {
  const raw = value === '' ? 0 : Math.round(Number(value));
  if (!Number.isFinite(raw)) return;
  const n = Math.max(-10000, Math.min(10000, raw));
  const l = link.value;
  if (l.auto) {
    if (!n) return;
    customRelayLinks(work.value);
    const i = work.value.relayLinks.pairs.findIndex(p => (p.a === l.a && p.b === l.b) || (p.a === l.b && p.b === l.a));
    work.value.relayLinks.pairs[i].bias = n;
    void save();
    ElMessage.info('已改为逐对设置中转连接，并给这一对设了偏好');
    emit('select', {kind: 'edge', id: `pair:${i}`});
    return;
  }
  if (n) l.p.bias = n; else delete l.p.bias;
  void save();
}

function enable(m, transport) {
  m.listen = {udp: 0, tcp: 0, tls: 0, ...(m.listen || {}), [transport]: ports.value[transport]};
  void save();
}
// Only for controllers that send no punch warnings; not once through, nor for two behind one NAT (they meet on the LAN).
const symmetric = computed(() => punched.value && !Array.isArray(mesh.data?.warnings) && live.value?.state !== 'direct' && !sameExit(mesh.data, link.value.a, link.value.b)
  && [link.value.a, link.value.b].every(id => reachOf(mesh.data, id)?.verdict === 'symmetric'));
// Problems the draft check would report anyway, with a one-tap fix where there is one.
const notes = computed(() => {
  const l = link.value;
  if (!l) return [];
  const list = [];
  if (l.kind === 'entry') {
    const relay = entryRelay.value;
    if (relay && !isRelay(relay)) list.push({text: `${relay.name} 已经不是中转`});
    else if (isSite(relay)) {
      // Reached by punching: UDP, and a public relay among the entries to exchange addresses through.
      if (l.c.transport !== 'udp') list.push({text: `${relay.name} 是站点中转，只能打洞（UDP）连接`, fix: '改成 UDP', action: () => setTransport('udp')});
      else if (!listens(relay, 'udp')) list.push({text: `${relay.name} 没有开启 UDP 监听`, fix: `为 ${relay.name} 开启 UDP 监听`, action: () => enable(relay, 'udp')});
      if (!l.member.entry.candidates.some(c => isRelay(find(c.relay)) && !isSite(find(c.relay)))) list.push({text: codeText('mesh_entry_needs_bootstrap')});
    } else if (relay && !listens(relay, l.c.transport)) list.push({text: `${relay.name} 没有开启 ${transportLabel(l.c.transport)} 监听`, fix: `为 ${relay.name} 开启 ${transportLabel(l.c.transport)} 监听`, action: () => enable(relay, l.c.transport)});
    if (relay && isRelay(relay) && !isSite(relay) && !publicAddress(relay)) list.push({text: `${relay.name} 没有可用的对外地址，在它的设置里填写`});
  } else if (punched.value) {
    // Punching: both ends listen on UDP; neither needs an address reachable from outside.
    for (const m of [find(l.a), find(l.b)]) if (m && !listens(m, 'udp')) list.push({text: `${m.name} 没有开启 UDP 监听`, fix: `为 ${m.name} 开启 UDP 监听`, action: () => enable(m, 'udp')});
    if (symmetric.value) list.push({text: codeText('mesh_punch_impossible')});
  } else if (dialing.value?.passive || (!l.auto && l.kind === 'direct')) {
    // The side that waits must listen on the transport and be reachable from outside.
    const passive = dialing.value?.passive || find(l.p.dialer === 'b' ? l.p.a : l.p.b), active = dialing.value?.dialer || find(l.p.dialer === 'b' ? l.p.b : l.p.a);
    const transport = l.p?.transport || 'udp';
    if (passive && !l.auto && !listens(passive, transport)) list.push({text: `${passive.name} 没有开启 ${transportLabel(transport)} 监听，${active?.name || '另一端'} 连不上它`,
      fix: `为 ${passive.name} 开启 ${transportLabel(transport)} 监听`, action: () => enable(passive, transport)});
    if (passive && !publicAddress(passive)) list.push({text: `${passive.name} 没有对外地址，${active?.name || '另一端'} 连不上它`});
  }
  return list;
});
// The server's check; a note above may already say the same with a fix.
const problems = computed(() => errors.value.map(errorMessage).filter(e => !notes.value.some(n => n.text === e)));
// GET /mesh warnings (mesh_punch_impossible): said, never blocking.
const warnings = computed(() => [...new Set(link.value?.path ? warningsUnder(mesh.data, link.value.path) : [])].filter(w => !notes.value.some(n => n.text === w) && !problems.value.includes(w)));
const transports = computed(() => link.value?.kind === 'direct' ? TRANSPORTS.filter(([t]) => t !== 'tls' || link.value.p.transport === 'tls') : TRANSPORTS);
// Punched links and entries to a site relay are UDP: no choice to make.
const fixedTransport = computed(() => relayPunched.value || (link.value?.kind === 'entry' && isSite(entryRelay.value) && link.value.c.transport === 'udp'));
function setTransport(t) {
  if (link.value.kind === 'entry') link.value.c.transport = t; else link.value.p.transport = t;
  void save();
}
function shift(delta) { if (move(link.value.member.entry.candidates, link.value.index, delta)) void save(); }
function setFull(on) {
  const l = link.value;
  if (on) { work.value.relayLinks.mode = 'full'; void save(); emit('select', {kind: 'edge', id: `relay:${[l.a, l.b].sort().join('~')}`}); return; }
  customRelayLinks(work.value);
  void save();
  const i = work.value.relayLinks.pairs.findIndex(p => (p.a === l.a && p.b === l.b) || (p.a === l.b && p.b === l.a));
  emit('select', i >= 0 ? {kind: 'edge', id: `pair:${i}`} : null);
}
</script>
<template>
  <div v-if="link" class="mesh-link">
    <p class="mesh-link-ends">
      <button type="button" class="ix-text-button" @click="emit('select', {kind: 'node', id: link.a})">{{ nameOf(members, link.a) }}</button>
      <span aria-hidden="true">{{ link.kind === 'entry' ? '→' : '⇄' }}</span>
      <button type="button" class="ix-text-button" @click="emit('select', {kind: 'node', id: link.b})">{{ nameOf(members, link.b) }}</button>
      <span v-if="link.kind === 'entry'" class="pb-tag" :data-tone="look.look === 'bad' ? 'bad' : look.look === 'active' ? 'ok' : undefined">{{ look.problem ? '不可用' : look.role }}</span>
    </p>

    <!-- What needs fixing first, each with its one-tap fix. -->
    <div v-if="notes.length || warnings.length || problems.length" class="mesh-link-notes">
      <p v-for="n in notes" :key="n.text" class="mesh-cand-note"><span>{{ n.text }}</span><button v-if="n.fix" type="button" class="ix-text-button" @click="n.action()">{{ n.fix }}</button></p>
      <p v-for="w in warnings" :key="w" class="mesh-cand-note is-info">{{ w }}</p>
      <p v-for="e in problems" :key="e" class="mesh-cand-note is-error">{{ e }}</p>
    </div>

    <section v-if="link.kind === 'entry'" class="mesh-link-group" aria-label="状态">
      <p v-if="look.problem && !notes.some(n => n.text === look.problem)" class="mesh-link-text ob-bad">{{ look.problem }}</p>
      <p v-if="look.health" class="mesh-link-text" :class="look.health.state === 'failing' ? 'ob-bad' : ''">{{ candLine(look.health) }}</p>
      <p v-if="look.punched" class="mesh-link-text instance-muted">{{ look.opened && look.via ? `已打通，经${look.via}` : '打洞：先经其他入口交换地址，在同一局域网时直接走局域网' }}</p>
      <p v-if="look.standby" class="mesh-link-text instance-muted">{{ standbyDetail(look) }}</p>
    </section>
    <section v-else-if="link.kind === 'relay'" class="mesh-link-group mesh-relay-metric" aria-label="实测">
      <dl v-if="metric" class="mesh-stats is-compact">
        <div v-for="[label, value, cls] in metricFacts" :key="label"><dt>{{ label }}</dt><dd :class="cls">{{ value }}</dd></div>
      </dl>
      <p v-else class="instance-muted mesh-link-text">还没有实测数据。中转上报后，这里显示延迟、抖动、丢包和代价。</p>
      <p v-if="metricNote" class="instance-muted mesh-link-text">{{ metricNote }}</p>
      <p v-if="metric?.state === 'suppressed'" class="mesh-link-text mesh-direct-why">这条线路最近反复断开，先不用于选路，稳定后自动恢复。</p>
    </section>
    <section v-if="punched" class="mesh-link-group mesh-direct-live" aria-label="实时状态">
      <h4 class="mesh-group-title">实时状态</h4>
      <dl v-if="live" class="mesh-stats is-compact">
        <div v-for="[label, value, cls] in liveFacts" :key="label" :class="{'is-wide': label === '最近错误' && value !== '无'}"><dt>{{ label }}</dt><dd :class="cls">{{ value }}</dd></div>
      </dl>
      <p v-else-if="link.kind === 'direct'" class="instance-muted mesh-link-text">应用后在这里显示实时状态：直连中、{{ punch === false ? '正在连接' : '正在打洞' }}或走中转。</p>
      <p v-if="liveNote" class="mesh-link-text mesh-direct-why">{{ liveNote }}</p>
    </section>

    <!-- Each end as it reports itself: from which address, over what, to which address; its public mapping when punched; who dials. -->
    <section v-if="lineId && Array.isArray(mesh.data?.lines)" class="mesh-link-group mesh-line-ends" aria-label="两端">
      <h4 class="mesh-group-title">两端<span v-if="lineState" class="ob-dot" :data-tone="lineState.tone">{{ lineState.text }}</span></h4>
      <p v-if="!line" class="instance-muted mesh-link-text">这条线路还没有上报，应用到网格并连上后显示两端的地址。</p>
      <p v-else-if="line.state === 'unused'" class="instance-muted mesh-link-text">入口方式是"固定"，只用第一个入口，这条不会连接。要让它当备用，把入口方式改成"自动切换"。</p>
      <ul v-else class="mesh-line-end-list">
        <li v-for="e in lineEnds" :key="e.key">
          <p class="mesh-line-end-name"><strong>{{ e.name }}</strong><span v-if="e.info?.dialer" class="pb-tag is-plain">{{ e.info.dialer }}</span></p>
          <p v-if="e.info" class="mesh-line-end-route"><template v-if="e.info.local">从 <span class="mono">{{ e.info.local }}</span> 经 </template><template v-else>经 </template>{{ e.info.via }} 连到 <span class="mono">{{ e.info.remote }}</span></p>
          <p v-if="e.info?.public && e.info.public !== e.info.local" class="mesh-line-end-route instance-muted">公网映射 <span class="mono">{{ e.info.public }}</span></p>
          <p v-if="!e.info" class="instance-muted mesh-line-end-route">这端没有上报</p>
        </li>
      </ul>
    </section>
    <MeshLineTrend v-if="lineId && line?.state !== 'unused'" :key="lineId" class="mesh-link-group" :line-id="lineId" :cold="cold" />

    <!-- Its settings: the value first, what it does behind ⓘ. -->
    <section class="mesh-link-group mesh-link-settings" aria-label="设置">
      <template v-if="link.kind === 'entry'">
        <MeshField label="顺序">
          <div class="mesh-link-order">
            <span>第 {{ link.index + 1 }} 个，共 {{ link.member.entry.candidates.length }} 个</span>
            <span class="ob-grow" />
            <button type="button" class="ix-text-button" :disabled="link.index === 0" @click="shift(-1)">提前</button>
            <button type="button" class="ix-text-button" :disabled="link.index === link.member.entry.candidates.length - 1" @click="shift(1)">延后</button>
          </div>
        </MeshField>
        <MeshField label="模式">
          <div class="mesh-link-order">
            <span>{{ link.member.entry.mode === 'failover' ? '自动切换' : '固定（只用第一个）' }}</span>
            <span class="ob-grow" />
            <button type="button" class="ix-text-button" @click="emit('select', {kind: 'node', id: link.a, tab: 'entry'})">修改</button>
          </div>
        </MeshField>
      </template>
      <MeshField v-if="link.kind === 'relay'" class="mesh-relay-facts" label="连接方式">
        <span class="mesh-link-value">{{ method }}</span>
      </MeshField>
      <MeshField v-if="!link.auto && !fixedTransport" label="传输方式">
        <div class="sv-ranges mesh-link-transport" role="radiogroup" aria-label="传输方式">
          <button v-for="[t, label] in transports" :key="t" type="button" role="radio" :aria-checked="(link.c || link.p).transport === t" :aria-selected="(link.c || link.p).transport === t" @click="setTransport(t)">{{ label }}</button>
        </div>
      </MeshField>
      <template v-if="link.kind !== 'entry' && !link.auto">
        <p v-if="relayPunched" class="instance-muted mesh-link-text mesh-punch-note">自动打洞：两台站点中转都在 NAT 后也能直接相连，打不通时经其他中转绕行</p>
        <p v-else-if="punched && punch === false" class="instance-muted mesh-link-text mesh-punch-note">{{ sameExit(mesh.data, link.a, link.b) ? '两边在同一个出口后，经局域网直连' : '至少一端能从外面直接访问，两边同时发起，直接连上，不用打洞' }}；连不上时走中转</p>
        <p v-else-if="punched" class="instance-muted mesh-link-text mesh-punch-note">自动打洞：两边都在防火墙后也能直连，打不通时走中转</p>
        <p v-else-if="dialing?.fixed && dialing.dialer" class="instance-muted mesh-link-text">由 {{ dialing.dialer.name }} 主动拨号：站点中转不对外宣告地址。</p>
        <MeshField v-else label="主动方" help="另一端等待连接，需要开启对应的监听并有对外地址。">
          <select v-model="link.p.dialer" aria-label="由谁主动连接" @change="save">
            <option value="a">{{ nameOf(members, link.p.a) }} 主动连接</option><option value="b">{{ nameOf(members, link.p.b) }} 主动连接</option>
          </select>
        </MeshField>
      </template>
      <MeshField v-if="link.kind === 'relay'" label="偏好" help="选路时把这条线的延迟加上这个数再去比较。例如偏好 -20：把这条线当成比实测快 20 ms，更容易走它；偏好 +50：当成慢 50 ms，有别的路就少走它。两端都按这个数算；默认 0，不影响选路。">
        <div class="ob-inline mesh-bias">
          <input type="number" inputmode="numeric" step="1" min="-10000" max="10000" class="mono mesh-short" v-model="biasText" aria-label="偏好（毫秒）" @focus="biasFocus(true)" @blur="biasFocus(false)" @change="setBias($event.target.value)" />
          <small class="sv-unit-label">毫秒</small>
        </div>
      </MeshField>
      <MeshField v-if="link.kind === 'relay' && !link.auto" label="静态代价" help="主控预先算好的路线用它：还没有实测数据时，或选路设为静态时，走路上代价加起来最小的那条。例如 A 到 C 直连代价 3，经 B 转两段各 1 共 2，就会经 B 走。默认 1，等于按经过几台中转来选；有了实测数据后按实测选路，不再看它。">
        <input v-model.number="link.p.cost" type="number" inputmode="numeric" min="1" max="1000" class="mono mesh-short" aria-label="代价" @change="save" />
      </MeshField>
      <p v-if="link.auto" class="instance-muted mesh-link-text">所有中转两两相连时自动生成{{ relayPunched ? '' : '，传输方式 UDP' }}。</p>
      <label v-if="link.kind === 'relay'" class="ob-check"><input type="checkbox" :checked="link.auto" @change="setFull($event.target.checked)" /><span>所有中转两两相连<small class="instance-muted">打开：每两台中转之间自动各连一条线，以后新加的中转也自动连上（例如 3 台中转就是 3 条线）。关掉：改成一条条自己管，可以删掉不需要的那一对，或单独设传输方式、偏好和代价。</small></span></label>
    </section>

    <div class="mesh-panel-foot">
      <span class="ob-grow" />
      <button type="button" class="ix-text-button ob-danger" @click="emit('remove')">{{ link.auto ? '删除这一对' : '删除连线' }}</button>
    </div>
  </div>
  <p v-else class="instance-muted">这条连线已经不在了。</p>
</template>
