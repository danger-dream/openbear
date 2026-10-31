<script setup>
import { computed, onBeforeUnmount } from 'vue';
import draggable from 'vuedraggable';
import { dragEdgeScroll } from '../dragEdgeScroll.js';
import { mesh, save } from './meshStore.js';
import { candParts, CAND_STATES, codeText, errorsAt, errorsUnder, isRelay, isSite, listens, move, publicAddress, TRANSPORTS, transportLabel, uid } from './meshHelpers.js';

// A member's ordered entry candidates, one card each: the relay on the first line with the
// order and delete buttons, how it connects and how it is doing on the second. On phones the
// relay keeps the first line to itself (with delete), the order buttons move to the second. Desktop
// reorders by dragging the handle or with the arrows; phones only get the arrows.
// health: {candidateId: {state, since, lastOkAt, lastError}} as the member reports it.
const props = defineProps({member: {type: Object, required: true}, index: {type: Number, required: true}, phone: Boolean, health: {type: Object, default: () => ({})}});
const relays = computed(() => (mesh.work?.members || []).filter(m => isRelay(m) && m.serverId !== props.member.serverId));
const relayOf = id => (mesh.work?.members || []).find(m => m.serverId === id);
const ports = computed(() => ({udp: 51999, tcp: 52000, tls: 52001, ...(mesh.work?.settings?.ports || {})}));
if (!props.member.entry) props.member.entry = {mode: 'fixed', candidates: [], autoReturn: true};
const entry = computed(() => props.member.entry);
const base = computed(() => `members[${props.index}].entry`);
const errorsOf = i => errorsUnder(mesh.errors, `${base.value}.candidates[${i}]`).map(e => e.message || e.text || e.error || '这个入口的设置不正确');
const MODES = [['fixed', '固定', '只用第一个入口；它连不上时不会自动换。'], ['failover', '自动切换', '第一个连不上时，按顺序换到下一个。']];
// What a candidate is to the member, before it reports: the first one is the one it starts with.
const role = (i) => i === 0 ? '首选' : entry.value.mode === 'failover' ? '备用' : '不使用';

// Problems the draft check would report anyway, shown with a one-tap fix.
function notes(c) {
  const relay = relayOf(c.relay);
  if (!relay) return [];
  if (!isRelay(relay)) return [{text: `${relay.name} 已经不是中转`}];
  const list = [];
  // A site relay is reached by punching (UDP) and needs no address from outside.
  if (isSite(relay) && c.transport !== 'udp') return [{text: `${relay.name} 是站点中转，只能打洞（UDP）连接`, fix: '改成 UDP', action: () => { c.transport = 'udp'; void save(); }}];
  if (!listens(relay, c.transport)) list.push({text: `${relay.name} 没有开启 ${transportLabel(c.transport)} 监听`, fix: `为 ${relay.name} 开启 ${transportLabel(c.transport)} 监听`, action: () => enable(relay, c.transport)});
  if (!isSite(relay) && !publicAddress(relay)) list.push({text: `${relay.name} 没有可用的对外地址，在它的设置里填写`});
  return list;
}
function enable(relay, transport) {
  relay.listen = {udp: 0, tcp: 0, tls: 0, ...(relay.listen || {}), [transport]: ports.value[transport]};
  void save();
}
function setTransport(c, t) { if (c.transport !== t) { c.transport = t; void save(); } }
function setMode(mode) { if (entry.value.mode !== mode) { entry.value.mode = mode; void save(); } }
// With a site relay among the entries there must be a public one to reach it through first.
const needsBootstrap = computed(() => entry.value.candidates.some(c => isSite(relayOf(c.relay)))
  && !entry.value.candidates.some(c => isRelay(relayOf(c.relay)) && !isSite(relayOf(c.relay))));
function add() {
  const used = new Set(entry.value.candidates.map(c => c.relay));
  const relay = relays.value.find(r => !used.has(r.serverId)) || relays.value[0];
  entry.value.candidates.push({id: uid('c'), relay: relay?.serverId || '', transport: 'udp'});
  void save();
}
function shift(i, delta) { if (move(entry.value.candidates, i, delta)) void save(); }
function remove(i) { entry.value.candidates.splice(i, 1); void save(); }
let stopEdgeScroll = () => {};
function startDrag(event) { stopEdgeScroll(); stopEdgeScroll = dragEdgeScroll(event.from); }
function endDrag() { stopEdgeScroll(); stopEdgeScroll = () => {}; void save(); }
onBeforeUnmount(() => stopEdgeScroll());
</script>
<template>
  <section class="mesh-entries" aria-label="入口">
    <div class="mesh-entry-mode">
      <div class="mesh-choice" role="radiogroup" aria-label="入口模式">
        <label v-for="[value, label] in MODES" :key="value"><input type="radio" name="mesh-entry-mode" :value="value" :checked="entry.mode === value" @change="setMode(value)" />{{ label }}</label>
      </div>
      <label v-if="entry.mode === 'failover'" class="ob-check"><input v-model="entry.autoReturn" type="checkbox" @change="save" /><span>恢复后自动切回</span></label>
    </div>
    <p class="instance-muted mesh-hint">{{ MODES.find(([v]) => v === entry.mode)?.[2] }}{{ entry.mode === 'failover' && entry.autoReturn ? '排在前面的入口持续健康 60 秒后切回去。' : '' }}</p>

    <p v-if="!relays.length" class="ix-notice" data-tone="warn"><span>还没有中转。先在一台 Linux 成员的“配置”里打开“作为中转”。</span></p>
    <draggable v-model="entry.candidates" item-key="id" handle=".so-grip" :animation="160" ghost-class="so-ghost" tag="ol" class="mesh-cands" :disabled="phone" :scroll="false" @start="startDrag" @end="endDrag">
      <template #item="{element: c, index: i}">
        <li class="mesh-cand" :data-error="errorsOf(i).length > 0">
          <div class="mesh-cand-row">
            <span v-if="!phone" class="so-grip" aria-hidden="true" title="拖动调整顺序"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg></span>
            <span class="so-number">{{ i + 1 }}</span>
            <select v-model="c.relay" class="mesh-cand-relay" :aria-label="`第 ${i + 1} 个入口的中转`" @change="save">
              <option v-if="!relays.some(r => r.serverId === c.relay)" :value="c.relay" disabled>{{ relayOf(c.relay)?.name || '选择中转' }}</option>
              <option v-for="r in relays" :key="r.serverId" :value="r.serverId">{{ r.name }}{{ isSite(r) ? '（站点中转）' : '' }}</option>
            </select>
            <div v-if="!phone" class="so-actions mesh-cand-actions">
              <button type="button" :disabled="i === 0" :aria-label="`上移第 ${i + 1} 个入口`" title="上移" @click="shift(i, -1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 6-6 6 6"/></svg></button>
              <button type="button" :disabled="i === entry.candidates.length - 1" :aria-label="`下移第 ${i + 1} 个入口`" title="下移" @click="shift(i, 1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 10 6 6 6-6"/></svg></button>
              <button type="button" :aria-label="`删除第 ${i + 1} 个入口`" title="删除" @click="remove(i)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
            </div>
            <div v-else class="so-actions mesh-cand-actions">
              <button type="button" :aria-label="`删除第 ${i + 1} 个入口`" title="删除" @click="remove(i)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
            </div>
          </div>
          <div class="mesh-cand-sub">
            <span v-if="isSite(relayOf(c.relay)) && c.transport === 'udp'" class="mesh-cand-transport mesh-cand-punch" title="站点中转：先经其他入口交换地址，再打洞连接；同一局域网时走局域网">打洞</span>
            <div v-else class="sv-ranges mesh-cand-transport" role="radiogroup" :aria-label="`第 ${i + 1} 个入口的传输方式`">
              <button v-for="[value, label] in TRANSPORTS" :key="value" type="button" role="radio" :aria-checked="c.transport === value" :aria-selected="c.transport === value" @click="setTransport(c, value)">{{ label }}</button>
            </div>
            <div v-if="phone" class="so-actions mesh-cand-actions mesh-cand-move">
              <button type="button" :disabled="i === 0" :aria-label="`上移第 ${i + 1} 个入口`" title="上移" @click="shift(i, -1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 6-6 6 6"/></svg></button>
              <button type="button" :disabled="i === entry.candidates.length - 1" :aria-label="`下移第 ${i + 1} 个入口`" title="下移" @click="shift(i, 1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 10 6 6 6-6"/></svg></button>
            </div>
            <span v-if="health[c.id]" class="mesh-cand-state" :data-tone="CAND_STATES[health[c.id].state][0]" :title="candParts(health[c.id]).detail">
              <b>{{ candParts(health[c.id]).label }}</b><small v-if="candParts(health[c.id]).detail">{{ candParts(health[c.id]).detail }}</small>
            </span>
            <span v-else class="mesh-cand-state" data-tone="off"><b>{{ role(i) }}</b></span>
          </div>
          <p v-for="n in notes(c)" :key="n.text" class="mesh-cand-note"><span>{{ n.text }}</span><button v-if="n.fix" type="button" class="ix-text-button" @click="n.action()">{{ n.fix }}</button></p>
          <p v-for="e in errorsOf(i).filter(e => !notes(c).some(n => n.text === e))" :key="e" class="mesh-cand-note is-error">{{ e }}</p>
        </li>
      </template>
    </draggable>
    <p v-if="needsBootstrap" class="mesh-cand-note">{{ codeText('mesh_entry_needs_bootstrap') }}</p>
    <p v-for="e in errorsAt(mesh.errors, `${base}.candidates`).concat(errorsAt(mesh.errors, base)).filter(e => !needsBootstrap || e !== codeText('mesh_entry_needs_bootstrap'))" :key="e" class="instance-error">{{ e }}</p>
    <div class="mesh-entries-foot">
      <button type="button" class="ix-text-button mesh-add-line" :disabled="!relays.length || entry.candidates.length >= 8" @click="add">＋ 添加入口</button>
      <span class="instance-muted mesh-hint">{{ phone ? '用箭头调整顺序' : '拖动把手或用箭头调整顺序' }}，排第一的是首选</span>
    </div>
  </section>
</template>
