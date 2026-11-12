<script setup>
import { computed, ref } from 'vue';
import { mesh } from './meshStore.js';
import { cidr, errorsUnder, ipv4, mutualRule, rowButton, ruleWords } from './meshHelpers.js';
import MeshNodeSelect from './MeshNodeSelect.vue';
import MeshSideNames from './MeshSideNames.vue';

// The access rules page: a preview of what one node or shared LAN may reach and who may reach it, every rule as
// a row (who → what, allowed, note), and what is let through without a rule.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}});
const emit = defineEmits(['edit', 'new']);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const regionOf = id => String(members.value.find(m => m.serverId === id)?.region || props.items.find(i => i.id === id)?.region || '').toUpperCase();
const rules = computed(() => mesh.work?.rules || []);
const lans = computed(() => members.value.filter(m => m.gateway?.subnets?.length && !m.gateway.backup).map(g => ({id: g.serverId, name: g.gateway.name || `${g.name} 的局域网`, nets: g.gateway.subnets})));
const previewOptions = computed(() => [...lans.value.map(l => ({value: `lan:${l.id}`, label: l.name, region: regionOf(l.id), group: '共享局域网'})),
  ...members.value.map(m => ({value: `m:${m.serverId}`, label: m.name, region: regionOf(m.serverId), group: '节点'}))]);
const HOME_KEY = 'openbear.mesh.home';
const home = (() => { try { return localStorage.getItem(HOME_KEY) || ''; } catch { return ''; } })();
const picked = ref(members.value.some(m => m.serverId === home) ? `m:${home}` : members.value.some(m => m.serverId === 'local') ? 'm:local' : `m:${members.value[0]?.serverId || ''}`);
const words = r => ruleWords(r, members.value);
// Networks are matched as the controller matches them (mesh_config): a source network holding a node's mesh address
// names that node, and a rule's network overlapping a LAN covers it, in part when it does not take in a whole LAN network.
const within = (net, ip) => ip !== null && ip >= net.base && ip < net.base + net.size;
const overlap = (a, b) => a.base < b.base + b.size && b.base < a.base + a.size;
const nodeIn = (side, id, dst) => {
  const m = members.value.find(x => x.serverId === id);
  return side?.all || (side?.members || []).includes(id) || (side?.tags || []).some(t => (m?.tags || []).includes(t))
    || (!dst && (side?.prefixes || []).some(n => { const net = cidr(n); return net && within(net, ipv4(m?.address)); }));
};
function lanIn(side, nets, dst) {
  if (side?.allSubnets) return {hit: true, part: []};
  const lan = nets.map(cidr).filter(Boolean);
  const hits = (side?.[dst ? 'subnets' : 'prefixes'] || []).map(n => [n, cidr(n)]).filter(([, c]) => c && lan.some(l => overlap(c, l)));
  return {hit: hits.length > 0, part: hits.filter(([, c]) => !lan.some(l => c.base <= l.base && c.base + c.size >= l.base + l.size)).map(([n]) => n)};
}
const preview = computed(() => {
  const [kind, id] = picked.value.split(':'), lan = kind === 'lan' ? lans.value.find(l => l.id === id) : null;
  const inSide = (side, dst) => lan ? lanIn(side, lan.nets, dst) : {hit: nodeIn(side, id, dst), part: []};
  // The other side of each rule: what it may reach (dst), or who may reach it (src); part: the LAN's share it is about.
  const row = (r, side, part) => ({id: r.id, side: side === 'dst' && mutualRule(r) ? null : r[side], nets: side === 'dst' ? 'subnets' : 'prefixes', what: words(r)[4] || '', note: r.note || '', part});
  const pick = (own, other) => rules.value.map(r => [r, inSide(r[own], own === 'dst')]).filter(([, x]) => x.hit).map(([r, x]) => row(r, other, x.part));
  return {to: pick('src', 'dst'), by: pick('dst', 'src')};
});
const rows = computed(() => rules.value.map((r, i) => {
  const [, src, , dst, what] = words(r);
  return {id: r.id, rule: r, mutual: !dst, src, dst: dst || '它们之间互相访问', what: what || '', note: r.note || '', bad: errorsUnder(mesh.errors, `rules[${i}]`).length > 0};
}));
const proxyPort = computed(() => mesh.work?.settings?.proxy?.port || 51080);
const exitPorts = computed(() => [...new Set(members.value.filter(m => m.proxy?.exit).map(m => m.proxy.exit.port))].join('、'));
const tunnels = computed(() => members.value.some(m => m.proxy?.ports?.length) && members.value.some(m => m.proxy?.exit));
const instances = computed(() => (mesh.data?.autoRules || []).length);
</script>
<template>
  <section class="m3-pane" aria-label="访问规则">
    <header class="m3-pane-head"><h2>访问规则</h2><span class="ob-grow" /><el-button type="primary" @click="emit('new')">＋ 新建规则</el-button></header>
    <p class="m3-lead">组网默认谁都不通，靠规则放行。每条规则是一句话：<b>谁</b>可以访问<b>谁</b>，允许什么。规则只放行、不禁止，所以顺序不影响结果。</p>
    <div class="m3-box m3-preview">
      <h3 class="m3-sec-h">预览
        <MeshNodeSelect v-model="picked" :options="previewOptions" aria-label="预览哪个节点或局域网" width="260px" />
        <small>选一个节点或一个局域网，看它能访问什么、谁能访问它</small></h3>
      <div class="m3-preview-cols">
        <div><div class="m3-faint">它能访问</div>
          <div v-for="r in preview.to" :key="r.id" class="m3-rel" v-bind="rowButton(() => emit('edit', r.id))"><span class="ob-grow"><MeshSideNames v-if="r.side" :side="r.side" :nets="r.nets" :members="members" /><template v-else>它们之间互相访问</template> · {{ r.what }}<span v-if="r.part.length" class="m3-faint"> · 仅其中 <span class="mono">{{ r.part.join('、') }}</span></span><small v-if="r.note" class="m3-faint m3-block">{{ r.note }}</small></span><span class="m3-chev">›</span></div>
          <p v-if="!preview.to.length" class="ob-warn">什么都访问不了</p></div>
        <div><div class="m3-faint">谁能访问它</div>
          <div v-for="r in preview.by" :key="r.id" class="m3-rel" v-bind="rowButton(() => emit('edit', r.id))"><span class="ob-grow"><MeshSideNames :side="r.side" :nets="r.nets" :members="members" /> · {{ r.what }}<span v-if="r.part.length" class="m3-faint"> · 仅其中 <span class="mono">{{ r.part.join('、') }}</span></span><small v-if="r.note" class="m3-faint m3-block">{{ r.note }}</small></span><span class="m3-chev">›</span></div>
          <p v-if="!preview.by.length" class="m3-faint">谁都访问不了它</p></div>
      </div>
    </div>
    <ul class="m3-rows m3-rows-rules" :class="{'is-phone': phone}">
      <li v-if="!phone && rows.length" aria-hidden="true"><div class="m3-row m3-row-head"><span>谁</span><span></span><span>可以访问</span><span>允许</span><span>备注</span></div></li>
      <li v-for="r in rows" :key="r.id">
        <div class="m3-row m3-row-click" :aria-label="`打开规则：${r.src} 访问 ${r.dst}`" v-bind="rowButton(() => emit('edit', r.id))">
          <span class="m3-row-conn"><MeshSideNames :side="r.rule.src" :members="members" /><span v-if="r.bad" class="m3-tag" data-kind="bad">需修改</span></span><span class="m3-faint">→</span>
          <span class="m3-row-conn"><template v-if="r.mutual">它们之间互相访问</template><MeshSideNames v-else :side="r.rule.dst" nets="subnets" :members="members" /></span>
          <span class="m3-faint">{{ r.what }}</span><span class="m3-faint">{{ r.note }}</span>
        </div>
      </li>
      <li v-if="!rows.length" class="instance-muted">还没有规则：组网里的节点互相访问不了。</li>
    </ul>
    <h3 class="m3-sec-h">自动放行<small>不用写规则，系统自动加的</small></h3>
    <ul class="m3-rows m3-rows-auto">
      <li><div class="m3-row"><span>出口</span><span class="m3-faint">{{ exitPorts ? `所有成员 → 每个出口的 ${exitPorts} 端口（TCP 和 UDP）。开了出口就自动放行，用出口不用另外写规则。` : '还没有出口。' }}</span></div></li>
      <li><div class="m3-row"><span>出口隧道</span><span class="m3-faint">{{ tunnels ? `开了代理端口的节点 → 它走的出口的 ${proxyPort} 端口，自动放行。` : '还没有代理端口。' }}</span></div></li>
      <li><div class="m3-row"><span>实例网关</span><span class="m3-faint">{{ instances ? `网关节点 → 它下面实例的端口（${instances} 条）` : '没有托管实例在组网地址上监听。' }}</span></div></li>
    </ul>
  </section>
</template>
