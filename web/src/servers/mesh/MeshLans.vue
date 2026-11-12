<script setup>
import { computed, ref, watch } from 'vue';
import { flag } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { exportsOf, gatewayNetProblem, nameOf, nodeState, rowButton, ruleWords, sharedBy } from './meshHelpers.js';
import MeshDialog from './MeshDialog.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshSideNames from './MeshSideNames.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// The LANs shared into the mesh, one row each: its networks, gateway, who may reach it and how many nodes do; then the
// LANs nodes report but do not share yet. A row opens its gateway's 共享局域网 section; a new one, or a backup, is set up here.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}, create: {type: Object, default: null}});
const emit = defineEmits(['open', 'rules']);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const regionOf = id => String(members.value.find(m => m.serverId === id)?.region || props.items.find(i => i.id === id)?.region || '').toUpperCase();
const nm = id => nameOf(members.value, id);
const same = (a, b) => a.length === b.length && a.every(x => b.includes(x));
const changed = computed(() => new Set((mesh.data?.draft?.changes || []).map(c => c.serverId).filter(Boolean)));
const lans = computed(() => members.value.filter(m => m.gateway?.subnets?.length && !m.gateway.backup).map(g => {
  const nets = g.gateway.subnets, rules = (mesh.work?.rules || []).filter(r => r.dst?.allSubnets || (r.dst?.subnets || []).some(n => nets.includes(n)));
  const reach = new Set(nets.flatMap(n => exportsOf(mesh.data, g.serverId, n) || []));
  return {g, name: g.gateway.name || `${g.name} 的局域网`, subnets: nets,
    backups: members.value.filter(b => b.gateway?.backup && same(b.gateway.subnets || [], nets)),
    who: rules.map(r => ({src: r.src, what: ruleWords(r, members.value)[4]})), reach: reach.size,
    pending: changed.value.has(g.serverId) || !mesh.data?.nodes?.[g.serverId]?.appliedRevision};
}));
// LAN networks found on nodes that share none yet: the ones another gateway shares already are shown greyed, and a node
// with nothing left to share is left out.
const suggested = id => ((mesh.data?.members || []).find(x => x.serverId === id)?.subnetSuggestions || []).map(net => ({net, by: sharedBy(net, members.value, {serverId: id})}));
const found = computed(() => members.value.filter(m => (m.platform || 'linux') === 'linux' && !m.gateway).map(m => ({m, nets: suggested(m.serverId)}))
  .filter(x => x.nets.some(n => !n.by)));
const orphans = computed(() => members.value.filter(b => b.gateway?.backup && !lans.value.some(l => l.backups.includes(b))));
const linuxFree = computed(() => members.value.filter(m => (m.platform || 'linux') === 'linux' && !m.gateway));
const stateOf = id => nodeState(mesh.data, id);

// ---- a new LAN or a backup gateway ----
const draft = ref(null);
function startNew(gateway = '', subnets = []) { draft.value = {kind: 'new', gateway, name: '', subnets: [...subnets], typed: '', error: ''}; }
watch(() => props.create, c => { if (c?.kind === 'lan') startNew(); }, {immediate: true});
function startBackup(lan) { draft.value = {kind: 'backup', lan: lan.g.serverId, gateway: '', error: ''}; }
const suggestions = computed(() => draft.value?.gateway ? suggested(draft.value.gateway).filter(n => !draft.value.subnets.includes(n.net)) : []);
function addNet(net) {
  const x = draft.value, value = String(net || x.typed).trim();
  if (!value) return;
  const g = members.value.find(m => m.serverId === x.gateway);
  const problem = gatewayNetProblem(value, {settings: mesh.work?.settings, members: mesh.work?.members || [], self: g || null});
  if (problem) { x.error = problem; return; }
  if (!x.subnets.includes(value)) x.subnets.push(value);
  x.typed = ''; x.error = '';
}
function confirm() {
  const x = draft.value, g = members.value.find(m => m.serverId === x.gateway);
  if (!g) return;
  if (x.kind === 'new') {
    if (!x.subnets.length) { x.error = '至少加一个网段'; return; }
    g.gateway = {subnets: [...x.subnets], masquerade: true, ...(x.name.trim() ? {name: x.name.trim()} : {})};
  } else {
    const lan = members.value.find(m => m.serverId === x.lan);
    g.gateway = {subnets: [...lan.gateway.subnets], masquerade: lan.gateway.masquerade !== false, backup: true};
  }
  void save();
  draft.value = null;
}
</script>
<template>
  <section class="m3-pane" aria-label="共享局域网">
    <header class="m3-pane-head"><h2>共享局域网</h2><span class="ob-grow" /><el-button type="primary" @click="startNew()">＋ 共享一个局域网</el-button></header>
    <p class="m3-lead">某个节点打开「共享局域网」后，它所在的局域网就出现在这里。点一行看完整设置：哪个网关、哪些网段、谁能访问。网关掉线时，备用网关接手。</p>
    <ul class="m3-rows m3-rows-lans" :class="{'is-phone': phone}">
      <li v-if="!phone" aria-hidden="true"><div class="m3-row m3-row-head"><span>名称</span><span>网段</span><span>网关</span><span>谁能访问</span><span>状态</span></div></li>
      <li v-for="l in lans" :key="l.g.serverId">
        <div class="m3-row m3-row-click" :aria-label="`打开 ${l.name} 的设置`" v-bind="rowButton(() => emit('open', l.g.serverId, 'lan'))">
          <span class="m3-row-name"><strong>{{ l.name }}</strong></span>
          <span class="mono">{{ l.subnets[0] }}<span v-if="l.subnets.length > 1" class="m3-faint"> 等 {{ l.subnets.length }} 段</span></span>
          <span class="m3-row-name"><span class="ob-dot" :data-tone="stateOf(l.g.serverId)[0]" aria-hidden="true"></span><MeshNodeName :id="l.g.serverId" />
            <span v-for="b in l.backups" :key="b.serverId" class="m3-faint">· 备用 <MeshNodeName :id="b.serverId" /></span></span>
          <span class="m3-row-conn"><template v-for="(w, i) in l.who" :key="i">{{ i ? '；' : '' }}<MeshSideNames :side="w.src" :members="members" /><span class="m3-faint">（{{ w.what }}）</span></template><span v-if="!l.who.length" class="ob-warn">还没放行，谁都访问不了</span></span>
          <span class="m3-row-state"><span v-if="l.pending" class="m3-tag" data-kind="warn">未应用</span><template v-else>{{ l.reach }} 个节点能访问</template></span>
        </div>
      </li>
      <li v-if="!lans.length" class="instance-muted">还没有共享的局域网。</li>
    </ul>
    <h3 class="m3-sec-h">检测到、还没共享的局域网<small>点「共享…」直接开始</small></h3>
    <ul class="m3-rows m3-rows-found">
      <li v-for="x in found" :key="x.m.serverId">
        <div class="m3-row m3-row-click" :aria-label="`共享 ${x.m.name} 所在的局域网`" v-bind="rowButton(() => startNew(x.m.serverId, x.nets.filter(n => !n.by).map(n => n.net)))">
          <span class="m3-row-name"><span class="ob-dot" :data-tone="stateOf(x.m.serverId)[0]" aria-hidden="true"></span><strong><MeshNodeName :id="x.m.serverId" /></strong></span>
          <span class="m3-found-nets"><span v-for="n in x.nets" :key="n.net" class="mono" :class="{'m3-taken': n.by}" :title="n.by ? `已由 ${n.by.name} 共享` : ''">{{ n.net }}<small v-if="n.by">（已由 <MeshNodeName :id="n.by.serverId" /> 共享）</small></span></span>
          <span class="m3-link">共享…</span>
        </div>
      </li>
      <li v-if="!found.length" class="instance-muted">节点网卡上没有检测到其他局域网。</li>
    </ul>
    <p v-if="orphans.length" class="ob-warn">{{ orphans.map(b => b.name).join('、') }} 设成了备用网关，但没有哪台首选网关宣告完全一样的网段。</p>

    <MeshDialog :model-value="Boolean(draft)" :title="draft?.kind === 'backup' ? '加备用网关' : '共享一个局域网'" :phone="phone" @update:model-value="v => { if (!v) draft = null; }">
      <div v-if="draft" class="m3-form">
        <div class="m3-field"><span>{{ draft.kind === 'backup' ? '备用网关' : '网关' }}</span>
          <MeshNodeSelect v-model="draft.gateway" :options="linuxFree.map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId)}))" placeholder="选一台 Linux 节点…" aria-label="网关" width="280px" />
          <small class="instance-muted">{{ draft.kind === 'backup' ? `和 ${nm(draft.lan)} 在同一个局域网里的另一台；${nm(draft.lan)} 连不上时，去这个局域网的流量转给它。` : '在局域网里、能一直开着的 Linux 节点，比如软路由。' }}</small></div>
        <template v-if="draft.kind === 'new'">
          <label class="m3-field"><span>名称</span><input v-model="draft.name" class="m3-input" maxlength="40" placeholder="例如 家里" aria-label="局域网名称" /></label>
          <div class="m3-field"><span>网段</span>
            <span class="m3-chips"><span v-for="n in draft.subnets" :key="n" class="sv-chip mono">{{ n }}<button type="button" :aria-label="`去掉 ${n}`" @click="draft.subnets = draft.subnets.filter(x => x !== n)">✕</button></span>
              <button v-for="n in suggestions" :key="n.net" type="button" class="m3-chip-btn mono" :disabled="Boolean(n.by)" :title="n.by ? `已由 ${n.by.name} 共享` : ''" @click="addNet(n.net)">{{ n.by ? `${n.net} · 已由 ${n.by.name} 共享` : `＋ ${n.net}` }}</button></span>
            <input v-model="draft.typed" class="m3-input mono" placeholder="例如 192.168.1.0/24，回车添加" aria-label="添加网段" @keydown.enter.prevent="addNet()" @blur="addNet()" /></div>
          <p class="instance-muted">共享以后，还要在访问规则里写上谁能访问这些网段。</p>
        </template>
        <small v-if="draft.error" class="ob-bad">{{ draft.error }}</small>
        <div class="m3-dialog-foot"><span class="ob-grow" /><el-button @click="draft = null">取消</el-button><el-button type="primary" :disabled="!draft.gateway" @click="confirm">{{ draft.kind === 'backup' ? '设为备用网关' : '共享' }}</el-button></div>
      </div>
    </MeshDialog>
  </section>
</template>
