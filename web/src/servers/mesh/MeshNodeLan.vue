<script setup>
import { computed, ref, watch } from 'vue';
import { flag, noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorsUnder, exportsOf, gatewayNetProblem, nameOf, sharedBy } from './meshHelpers.js';
import MeshFr from './MeshFr.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// A node page's 共享局域网 section. A node sharing its LAN: the name, its networks, address masquerading both ways,
// the nodes that can reach it and the backups standing by. A node sharing none: share one, or stand by for another
// gateway. Routes the node already has to other networks are taken over or left alone here.
const props = defineProps({member: {type: Object, required: true}, index: {type: Number, required: true}, items: {type: Array, default: () => []}});
const emit = defineEmits(['edit', 'open', 'rules']);
const m = computed(() => props.member);
const id = computed(() => m.value.serverId);
const members = computed(() => (mesh.work?.members || []).filter(x => !x.removing));
const regionOf = sid => String(members.value.find(x => x.serverId === sid)?.region || props.items.find(i => i.id === sid)?.region || '').toUpperCase();
const nm = sid => nameOf(members.value, sid);
const g = computed(() => m.value.gateway || null);
const primary = computed(() => Boolean(g.value) && !g.value.backup);
const linux = computed(() => (m.value.platform || 'linux') === 'linux');
const errors = computed(() => errorsUnder(mesh.errors, `members[${props.index}].gateway`).map(e => e.message || e.text || e.error).filter(Boolean));
// A new gateway masquerades both ways: devices on the LAN and the nodes across need no routes back.
function setOn(on) { m.value.gateway = on ? {subnets: [], masquerade: true, snat: true} : null; void save(); }
const name = ref('');
watch(() => g.value?.name, v => { name.value = v || ''; }, {immediate: true});
function setName() { const v = name.value.trim(); if (v) g.value.name = v; else delete g.value.name; void save(); }
// The LAN networks found on the node's interfaces (shared by another gateway already: greyed), and the ones it shares.
const found = computed(() => (mesh.data?.members || []).find(x => x.serverId === id.value)?.subnetSuggestions || []);
const netProblem = value => gatewayNetProblem(value, {settings: mesh.work?.settings, members: mesh.work?.members || [], self: m.value});
const choices = computed(() => [...new Set([...(g.value?.subnets || []), ...found.value])].map(net => ({net, by: (g.value?.subnets || []).includes(net) ? null : sharedBy(net, members.value, m.value)}))
  .filter(c => c.by || (g.value?.subnets || []).includes(c.net) || !netProblem(c.net)));
function toggleNet(net, on) { g.value.subnets = on ? [...g.value.subnets, net] : g.value.subnets.filter(n => n !== net); void save(); }
const typed = ref(''), typedError = ref('');
function addTyped() {
  const v = typed.value.trim();
  if (!v) { typedError.value = ''; return; }
  typedError.value = netProblem(v);
  if (typedError.value) return;
  if (!g.value.subnets.includes(v)) g.value.subnets = [...g.value.subnets, v];
  typed.value = '';
  void save();
}
function setFlag(key, on) { g.value[key] = on; void save(); }
// Who can reach these networks now, by the draft's access rules.
const reach = computed(() => [...new Set((g.value?.subnets || []).flatMap(n => exportsOf(mesh.data, id.value, n) || []))].filter(sid => sid !== id.value));

// ---- backups ----
const same = (a, b) => a.length === b.length && a.every(x => b.includes(x));
const standby = (x, of) => x.gateway?.backup && same(x.gateway.subnets || [], of.gateway.subnets || []);
const backups = computed(() => primary.value ? members.value.filter(x => x !== m.value && standby(x, m.value)) : []);
const free = computed(() => members.value.filter(x => x !== m.value && (x.platform || 'linux') === 'linux' && !x.gateway));
function addBackup(sid) {
  const x = members.value.find(y => y.serverId === sid);
  if (!x) return;
  x.gateway = {subnets: [...g.value.subnets], masquerade: g.value.masquerade !== false, snat: Boolean(g.value.snat), backup: true};
  void save();
}
function dropBackup(x) { x.gateway = null; void save(); }
const primaries = computed(() => members.value.filter(x => x !== m.value && x.gateway?.subnets?.length && !x.gateway.backup));
const standingFor = computed(() => g.value?.backup ? primaries.value.find(p => standby(m.value, p)) || null : null);
function standFor(sid) {
  const p = primaries.value.find(x => x.serverId === sid);
  if (!p) return;
  m.value.gateway = {subnets: [...p.gateway.subnets], masquerade: p.gateway.masquerade !== false, snat: Boolean(p.gateway.snat), backup: true};
  void save();
}
const option = x => ({value: x.serverId, label: x.name, region: regionOf(x.serverId), note: x.gateway?.name || (x.gateway?.subnets || []).join('、')});

// Routes the node already has to networks another gateway shares (GET /mesh routeClashes), taken over unless kept, and
// kept ones that clash with nothing now. A network the node itself is on is not a conflict: it reaches it on its own LAN.
const clashes = computed(() => {
  const list = (mesh.data?.routeClashes?.[id.value] || []).filter(c => !c.fixed);
  return [...list, ...(m.value.keepRoutes || []).filter(r => !list.some(c => c.route === r)).map(route => ({route, idle: true}))];
});
function setTakeover(route, on) { const kept = (m.value.keepRoutes || []).filter(r => r !== route); m.value.keepRoutes = on ? kept : [...kept, route]; void save(); }
const clashText = c => c.idle ? '现在没有冲突' : `原来经 ${c.dev || '本机路由'}：${c.held}`;
</script>
<template>
  <section id="m3-sec-lan" class="m3-dsec" aria-label="共享局域网">
    <h3>共享局域网</h3>
    <p class="m3-explain">把这个节点所在的局域网共享给组网（就是网关功能），组网里被允许的节点就能直接访问局域网里的电脑、NAS 等，不用在那些设备上装任何东西。</p>
    <template v-if="!g || primary">
      <MeshFr label="共享局域网" :errors="g ? [] : errors">
        <button type="button" class="m3-sw" role="switch" :aria-checked="Boolean(g)" aria-label="把局域网网段接入网格" :disabled="!linux" @click="setOn(!g)"><i></i><span>{{ g ? '已打开' : '关闭' }}</span></button>
        <template v-if="!linux" #hint>只有 Linux 节点能共享局域网。</template>
      </MeshFr>
    </template>
    <MeshFr v-if="!g && linux && primaries.length" label="当备用网关" hint="和另一个网关在同一个局域网里时，可以给它当备用：它连不上时，去这个局域网的流量转给这个节点，恢复后切回。">
      <MeshNodeSelect :options="primaries.map(option)" placeholder="给哪个网关当备用…" aria-label="给哪个网关当备用" width="300px" @change="standFor" />
    </MeshFr>
    <MeshFr v-if="g?.backup" label="备用网关" :errors="errors">
      <span v-if="standingFor">给 <MeshNodeName :id="standingFor.serverId" /> 当备用，接手 <span class="mono">{{ g.subnets.join('、') }}</span></span>
      <span v-else class="ob-warn">没有哪个首选网关共享完全一样的网段：<span class="mono">{{ g.subnets.join('、') }}</span></span>
      <div class="m3-ctl"><el-button size="small" @click="setOn(false)">不当备用了</el-button></div>
    </MeshFr>
    <template v-if="primary">
      <MeshFr label="名称" hint="在访问规则和列表里用这个名字，比如「家里」。">
        <input v-model="name" v-bind="noAutofill" class="m3-input m3-mid" maxlength="40" :placeholder="`${m.name} 的局域网`" aria-label="局域网名称" @change="setName" />
      </MeshFr>
      <MeshFr label="共享哪些网段" :hint="found.length ? '列出来的是这个节点网卡上检测到的局域网网段（Docker 的网段不列）。' : '这个节点还没上报网卡上的局域网网段，可以手填。'" :errors="errors">
        <label v-for="c in choices" :key="c.net" class="m3-check" :class="{'m3-taken': c.by}"><input type="checkbox" :checked="g.subnets.includes(c.net)" :disabled="Boolean(c.by)" @change="toggleNet(c.net, $event.target.checked)" />
          <span class="mono">{{ c.net }}</span><small v-if="c.by" class="m3-faint">已由 <MeshNodeName :id="c.by.serverId" /> 共享</small></label>
        <div class="m3-ctl"><input v-model="typed" v-bind="noAutofill" class="m3-input m3-mid mono" placeholder="手填，例如 192.168.1.0/24" aria-label="添加网关网段" @change="addTyped" @keydown.enter.prevent="addTyped" /></div>
        <small v-if="typedError" class="ob-bad m3-block">{{ typedError }}</small>
      </MeshFr>
      <MeshFr label="能访问的节点" :sub="`${reach.length} 个`">
        <span v-if="reach.length" class="m3-names"><template v-for="(sid, i) in reach" :key="sid">{{ i ? '、' : '' }}<MeshNodeName :id="sid" /></template></span>
        <span v-else class="ob-warn">还没有规则放行，组网里谁都访问不了它</span>
        <template #hint>按现在的访问规则算出来的。<button type="button" class="ix-text-button" @click="emit('rules')">在访问规则里改</button></template>
      </MeshFr>
      <MeshFr label="备用网关" :sub="backups.length ? `${backups.length} 个` : ''" hint="同一个局域网里的另一个 Linux 节点：这个网关连不上时，去这个局域网的流量转给它，恢复后切回。">
        <span v-for="b in backups" :key="b.serverId" class="m3-backup"><MeshNodeName :id="b.serverId" />
          <button type="button" class="ix-text-button" @click="dropBackup(b)">去掉</button></span>
        <span v-if="!backups.length" class="m3-faint">没有</span>
        <div class="m3-ctl"><MeshNodeSelect :options="free.map(option)" placeholder="＋ 加一个备用网关…" aria-label="加一个备用网关" width="300px" @change="addBackup" /></div>
      </MeshFr>
      <MeshFr label="地址伪装：组网 → 局域网" :auto="{value: '打开', why: '局域网路由器不用加回程路由'}" :changed="g.masquerade === false" restorable @restore="setFlag('masquerade', true)"
        hint="组网里的节点访问这个局域网时，局域网设备看到的来源是网关自己（masquerade），路由器上不用加回程路由。关掉的话要在局域网路由器上把组网网段指向这台网关。">
        <button type="button" class="m3-sw" role="switch" :aria-checked="g.masquerade !== false" aria-label="地址伪装：组网访问局域网" @click="setFlag('masquerade', g.masquerade === false)"><i></i><span>{{ g.masquerade !== false ? '打开' : '关闭' }}</span></button>
      </MeshFr>
      <MeshFr label="地址伪装：局域网 → 组网" :auto="{value: '打开', why: '组网里的节点不用认识这个局域网的网段'}" :changed="!g.snat" restorable @restore="setFlag('snat', true)"
        hint="局域网里的设备访问组网时，对面看到的来源是网关的组网地址，对面不用有回到这个局域网的路由；访问规则也把这些流量算作网关发出的。关掉的话对面看到设备的真实地址。">
        <button type="button" class="m3-sw" role="switch" :aria-checked="Boolean(g.snat)" aria-label="地址伪装：局域网访问组网" @click="setFlag('snat', !g.snat)"><i></i><span>{{ g.snat ? '打开' : '关闭' }}</span></button>
      </MeshFr>
    </template>
    <MeshFr v-if="clashes.length" label="路由冲突" sub="别的局域网" :auto="{value: '接管', why: '去这些局域网都走组网'}" :changed="(m.keepRoutes || []).length > 0"
      hint="这个节点上已经有去别的网关所共享网段的路由（比如原来的 WireGuard）。默认接管：改走组网，退出组网或组网服务停止时原来的路由会恢复。取消勾选就保留原来的路由。">
      <label v-for="c in clashes" :key="c.route" class="m3-check"><input type="checkbox" :checked="!(m.keepRoutes || []).includes(c.route)" @change="setTakeover(c.route, $event.target.checked)" />
        <span>接管 <span class="mono">{{ c.route }}</span><span class="m3-faint">（{{ clashText(c) }}）</span></span></label>
    </MeshFr>
  </section>
</template>
