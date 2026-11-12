<script setup>
import { computed, nextTick, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { ArrowDown, ArrowLeft, Close, Search, Star, StarFilled } from '@element-plus/icons-vue';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import { flag, safeError } from '../serverHelpers.js';
import DdnsSheet from '../../ddns/DdnsSheet.vue';
import LatencyAddressForm from '../LatencyAddressForm.vue';
import { GROUP_NAMES, MAX_TARGETS, PROVINCE_ID, VIA_NAMES, addressWay, defaultTargets, newAddressId, serverAddress, serverAddresses, serverTarget, serverTargetId,
  targetLabel } from '../latencyTargets.js';

// 编辑延迟目标. On the left 正在测: what it measures, each with how and where, a star for the card
// (those starred are what its card's latency is worked out over) and, when its own, a way to take it off;
// under them which list that is, the default one or its own (only the one it is, so it is not read as
// measuring both), and the way to the other. On the right 添加目标, for a list of its own: the provinces'
// three carriers, the other servers by group (each at the address and the way picked for it; a group
// added at once), the addresses built in and the user's own, by tab or found by name. A new address goes
// into the settings' library, saved with the rest. On a phone the two are pages of the sheet, 添加目标 a
// tap away.
const props = defineProps({modelValue: Boolean, server: {type: Object, required: true}, servers: {type: Array, default: () => []},
  settings: {type: Object, required: true}, phone: Boolean});
const emit = defineEmits(['update:modelValue', 'changed']);
const latency = computed(() => props.settings.latency || {});
const catalog = computed(() => props.settings.latencyCatalog || {});
const defaults = computed(() => defaultTargets(latency.value, catalog.value, props.server.region === 'CN', props.server.node?.route || null));
const others = computed(() => props.servers.filter(x => x.id !== props.server.id && x.node));
const names = computed(() => Object.fromEntries(props.servers.map(x => [x.id, x.name])));
const method = computed(() => latency.value.method === 'icmp' ? 'ICMP' : 'TCPing');
const home = computed(() => (catalog.value.provinces || []).find(p => p.name === latency.value.province)?.code || '');
const onMesh = computed(() => Boolean(props.server.meshAddress));

const following = ref(true), list = ref([]), pins = ref([]), added = ref([]), ways = ref({});
const tab = ref('three'), province = ref(''), search = ref(''), view = ref('list'), adding = ref(false), busy = ref(false), error = ref(''), chosen = ref(null);
let start = '';
const snapshot = () => JSON.stringify([following.value, list.value, pins.value, added.value]);
watch(() => props.modelValue, open => {
  if (!open) return;
  const own = props.server.profile?.latency || {};
  following.value = !own.targets;
  list.value = [...(own.targets || defaults.value.ids)];
  pins.value = [...(own.card || [])];
  added.value = [];
  ways.value = {};
  search.value = '';
  tab.value = 'three';
  province.value = home.value;
  view.value = 'list';
  adding.value = false;
  error.value = '';
  start = snapshot();
}, {immediate: true});
const dirty = computed(() => props.modelValue && snapshot() !== start);
const full = computed(() => list.value.length >= MAX_TARGETS);
const has = id => list.value.includes(id);
const mine = computed(() => [...(latency.value.custom || []), ...added.value]);

function customize() {
  following.value = false;
}
function follow() {
  following.value = true;
  list.value = [...defaults.value.ids];
  pins.value = pins.value.filter(has);
}
function set(id, on) {
  if (following.value || on === has(id)) return;
  if (on) {
    if (full.value) return;
    list.value = [...list.value, id];
    // Added at the end of 正在测: brought into its view.
    void nextTick(() => [...chosen.value?.children || []].find(li => li.dataset.target === id)?.scrollIntoView?.({block: 'nearest'}));
  } else {
    list.value = list.value.filter(t => t !== id);
    pins.value = pins.value.filter(t => t !== id);
  }
}
function pin(id) {
  pins.value = pins.value.includes(id) ? pins.value.filter(t => t !== id) : [...pins.value, id];
}

// What each is called on the left, and how and where it is measured.
const WAYS = {icmp: 'ICMP', tcp: 'TCP', http: 'HTTP'};
const regionOf = id => props.servers.find(s => s.id === serverTarget(id)?.server)?.region;
function nameOf(id) {
  const server = serverTarget(id);
  return server ? names.value[server.server] || '已删除的服务器' : targetLabel(id, {catalog: catalog.value, latency: {custom: mine.value}, names: names.value});
}
function detailOf(id) {
  const server = serverTarget(id);
  if (server) {
    const x = props.servers.find(s => s.id === server.server), ip = serverAddress(x, server.via), way = WAYS[server.mode];
    if (!x) return '这台服务器已删除';
    if (server.via === 'mesh' && !onMesh.value) return `${way} · 组网 · 这台不在组网里，测不了`;
    if (!ip) return `${way} · ${server.via === 'mesh' ? '它不在组网里' : server.via === 'v6' ? '没有公网 IPv6' : '没有公网地址'}`;
    const host = server.mode !== 'icmp' && ip.includes(':') ? `[${ip}]` : ip;
    return `${way} · ${server.via ? `${VIA_NAMES[server.via]} ` : ''}${host}${server.mode === 'icmp' ? '' : `:${server.port}`}`;
  }
  const own = mine.value.find(e => e.id === id);
  if (own) { const w = addressWay(own); return `${w.way || method.value} · ${w.where}`; }
  const extra = (catalog.value.extras || []).find(e => e.id === id);
  if (extra) return `${method.value} · ${extra.host}`;
  if (id.startsWith('c:')) return `${method.value} · ${id.slice(2)}`;
  if (PROVINCE_ID.test(id)) {
    const [code, carrier] = id.split('-');
    return `${method.value} · ${(catalog.value.directory?.[code]?.[carrier] || '').replace(/:\d+$/, '') || '三网节点'}`;
  }
  return '这个地址已从地址库删除';
}

// The right: options by tab, or found by name or address across them all.
const TABS = [['three', '三网'], ['servers', '服务器'], ['common', '常用'], ['mine', '我的地址']];
const KINDS = {dns: 'DNS', ai: 'AI 平台', site: '常用网站', service: '常用服务'};
const carriers = computed(() => catalog.value.carriers || []);
const provinceName = code => (catalog.value.provinces || []).find(p => p.code === code)?.name || code;
const threeOf = code => carriers.value.map(c => ({id: `${code}-${c.id}`, name: `${provinceName(code)}${c.name}`, sub: (catalog.value.directory?.[code]?.[c.id] || '').replace(/:\d+$/, '')}));
const commonGroups = computed(() => ['cn', 'intl'].flatMap(group => Object.entries(KINDS).map(([kind, name]) => ({
  title: `${group === 'cn' ? '国内' : '国外'} · ${name}`, items: (catalog.value.extras || []).filter(e => e.group === group && e.kind === kind).map(e => ({id: e.id, name: e.label, sub: e.host}))})))
  .filter(g => g.items.length));
const mineItems = computed(() => mine.value.map(e => { const w = addressWay(e); return {id: e.id, name: e.label, sub: `${w.way || method.value} · ${w.where}`}; }));
const serverGroups = computed(() => {
  const by = new Map();
  for (const x of others.value) {
    const g = x.profile?.group || '';
    if (!by.has(g)) by.set(g, []);
    by.get(g).push(x);
  }
  return [...by].sort(([a], [b]) => (a === '') - (b === '') || a.localeCompare(b, 'zh-CN')).map(([g, items]) => ({title: g || '未分组', items}));
});
const query = computed(() => search.value.trim().toLowerCase());
const found = computed(() => {
  const q = query.value;
  if (!q) return null;
  const match = o => o.name.toLowerCase().includes(q) || o.sub.toLowerCase().includes(q);
  const provinces = (catalog.value.provinces || []).flatMap(p => threeOf(p.code));
  return {options: [...provinces, ...commonGroups.value.flatMap(g => g.items), ...mineItems.value].filter(match),
    servers: others.value.filter(x => match({name: x.name, sub: serverAddresses(x, true).map(a => a.ip).join(' ')}))};
});

// A server: one entry on its list at most, measured at the address and the way picked for it here.
const serverIdOf = x => list.value.find(id => serverTarget(id)?.server === x.id) || '';
const reachable = x => serverAddresses(x, onMesh.value).length > 0;
const wayOf = x => {
  const on = serverTarget(serverIdOf(x));
  return on ? {via: on.via, mode: on.mode, port: on.port || (on.mode === 'http' ? 80 : x.ssh?.port || 22)}
    : ways.value[x.id] || {via: serverAddresses(x, onMesh.value)[0]?.via || '', mode: 'icmp', port: x.ssh?.port || 22};
};
// Those offered: the one its entry is at kept, named so, when it no longer has it.
function addressesOf(x) {
  const options = serverAddresses(x, onMesh.value), via = wayOf(x).via;
  return options.some(a => a.via === via) ? options : [...options, {via, name: via ? VIA_NAMES[via] : '公网', ip: ''}];
}
function addressName(x) {
  const a = addressesOf(x).find(o => o.via === wayOf(x).via);
  return `${a.name} · ${a.ip || '没有这个地址'}`;
}
function setWay(x, patch) {
  const w = {...wayOf(x), ...patch};
  w.port = Number(w.port) || (w.mode === 'http' ? 80 : x.ssh?.port || 22);
  if (w.port < 1 || w.port > 65535) return;
  ways.value = {...ways.value, [x.id]: w};
  const id = serverIdOf(x);
  if (!id) return;
  const next = serverTargetId(x.id, w.mode, w.port, w.via);
  list.value = list.value.map(t => t === id ? next : t);
  pins.value = pins.value.map(t => t === id ? next : t);
}
function setServer(x, on) {
  const id = serverIdOf(x);
  if (!on) { if (id) set(id, false); return; }
  const w = wayOf(x);
  if (id || !serverAddress(x, w.via)) return;
  set(serverTargetId(x.id, w.mode, w.port, w.via), true);
}
function addGroup(g) {
  const wanted = g.items.filter(x => reachable(x) && !serverIdOf(x));
  for (const x of wanted) setServer(x, true);
  if (wanted.some(x => !serverIdOf(x))) error.value = `最多测 ${MAX_TARGETS} 个，「${g.title}」没能全部加上`;
}
const groupLeft = g => g.items.filter(x => reachable(x) && !serverIdOf(x)).length;

function addAddress(value) {
  if (mine.value.length >= (catalog.value.maxCustom || 32)) { error.value = '自己的地址最多 32 个，已经加满'; return; }
  const entry = {id: newAddressId(), label: value.label, host: value.host, port: value.port, group: value.group, ...(value.mode ? {mode: value.mode} : {}), ...(value.url ? {url: value.url} : {})};
  added.value = [...added.value, entry];
  adding.value = false;
  set(entry.id, true);
}

async function save() {
  busy.value = true;
  error.value = '';
  try {
    if (added.value.length) ok(await Api.saveServerSettings({latency: {custom: [...(latency.value.custom || []), ...added.value]}}));
    const card = pins.value.filter(has);
    const value = following.value ? (card.length ? {card} : null) : {targets: list.value, ...(card.length ? {card} : {})};
    ok(await Api.updateServer(props.server.id, {profile: {latency: value}}));
    start = snapshot();
    ElMessage.success('已保存，节点马上改测新的目标');
    emit('changed');
    emit('update:modelValue', false);
  } catch (e) { error.value = safeError(e); }
  finally { busy.value = false; }
}
</script>
<template>
  <DdnsSheet :model-value="modelValue" :phone="phone" width="880px" title="编辑延迟目标" :busy="busy" form :dirty="dirty"
    @update:model-value="v => emit('update:modelValue', v)">
    <form class="lte" :data-view="phone ? view : null" autocomplete="off" @submit.prevent="save">
      <div class="lte-body">
        <section v-if="!phone || view === 'list'" class="lte-chosen" aria-label="正在测">
          <header class="lte-head"><h4>正在测</h4><b :data-tone="full ? 'warn' : null">{{ list.length }} / {{ MAX_TARGETS }}</b><span class="ob-grow" />
            <button v-if="phone && !following" type="button" class="ix-text-button" @click="view = 'add'">添加目标</button></header>
          <ul v-if="list.length" ref="chosen" class="lte-list">
            <li v-for="id in list" :key="id" :data-target="id">
              <span class="lte-name"><span><span v-if="flag(regionOf(id))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(id)) }}</span>{{ nameOf(id) }}</span>
                <small>{{ detailOf(id) }}</small></span>
              <button type="button" class="lte-icon lte-star" :aria-pressed="pins.includes(id)" :aria-label="`${nameOf(id)}：在服务器卡片上显示`"
                :title="pins.includes(id) ? '服务器卡片上的延迟用它算（点一下取消）' : '用它算服务器卡片上的延迟'" @click="pin(id)">
                <el-icon><StarFilled v-if="pins.includes(id)" /><Star v-else /></el-icon></button>
              <button v-if="!following" type="button" class="lte-icon" :aria-label="`去掉 ${nameOf(id)}`" title="不再测" @click="set(id, false)"><el-icon><Close /></el-icon></button>
            </li>
          </ul>
          <p v-else class="dt-empty lte-empty">一个都不测。{{ following ? '默认列表是空的。' : '从右边添加。' }}</p>
          <div class="lte-mode">
            <span class="lte-mode-text"><b>{{ following ? '跟随默认列表' : '这台自己选的目标' }}</b>
              <small class="instance-muted">{{ following
                ? `「${GROUP_NAMES[defaults.group]}」，在「设置 → 延迟」里改${defaults.leftOut.length ? `；${defaults.leftOut.join('、')}回程是普通线路，不测` : ''}`
                : `默认列表是「${GROUP_NAMES[defaults.group]}」的 ${defaults.ids.length} 个` }}</small></span>
            <el-button v-if="following" size="small" @click="customize">自己选目标</el-button>
            <el-button v-else size="small" @click="follow">改为跟随默认列表</el-button>
          </div>
          <p class="lte-note"><el-icon><StarFilled /></el-icon>标了星的目标用来算服务器卡片上的延迟；都不标，按设置里的规则（测了三网就算三网）。</p>
        </section>

        <section v-if="!phone || view === 'add'" class="lte-pick" aria-label="添加目标">
          <header v-if="phone" class="lte-head">
            <button type="button" class="ix-text-button lte-back" @click="view = 'list'"><el-icon><ArrowLeft /></el-icon>完成</button>
            <span class="ob-grow" /><b :data-tone="full ? 'warn' : null">{{ list.length }} / {{ MAX_TARGETS }}</b></header>
          <header v-else class="lte-head"><h4>添加目标</h4></header>
          <div v-if="following" class="dt-empty lte-locked">
            <span>现在跟随默认列表：按服务器在哪、三网回程是哪种线路自动决定测什么。要自己增减目标，点「自己选目标」。</span>
            <button type="button" class="ix-text-button" @click="customize">自己选目标</button>
          </div>
          <template v-else>
            <label class="lte-search"><el-icon><Search /></el-icon>
              <input v-model="search" type="search" aria-label="搜索目标" placeholder="搜索名称或地址，如 北京、1.1.1.1" /></label>
            <div v-if="!query" class="sv-ranges lte-tabs" role="tablist" aria-label="目标分类">
              <button v-for="[key, name] in TABS" :key="key" type="button" role="tab" :aria-selected="tab === key" @click="tab = key">{{ name }}</button>
            </div>
            <p v-if="full" class="lte-full" role="status">已经选满 {{ MAX_TARGETS }} 个，去掉一些才能再加。</p>

            <div class="lte-options">
              <!-- One option: name, where, and its button. -->
              <template v-if="found">
                <p v-if="!found.options.length && !found.servers.length" class="instance-muted lte-none">没有找到“{{ search.trim() }}”。</p>
                <ul v-if="found.options.length" class="lte-rows">
                  <li v-for="o in found.options" :key="o.id">
                    <span class="lte-name"><span>{{ o.name }}</span><small>{{ o.sub }}</small></span>
                    <button type="button" class="lte-add" :aria-pressed="has(o.id)" :disabled="!has(o.id) && full" :aria-label="`添加 ${o.name}`"
                      @click="set(o.id, !has(o.id))">{{ has(o.id) ? '已添加' : '添加' }}</button>
                  </li>
                </ul>
              </template>

              <template v-else-if="tab === 'three'">
                <div class="lte-row-head"><span>省份</span>
                  <select v-model="province" class="lte-province" aria-label="省份">
                    <option v-for="p in catalog.provinces || []" :key="p.code" :value="p.code">{{ p.name }}{{ p.code === home ? '（观测地）' : '' }}</option>
                  </select></div>
                <ul class="lte-rows">
                  <li v-for="o in threeOf(province)" :key="o.id">
                    <span class="lte-name"><span>{{ o.name }}</span><small>{{ o.sub }}</small></span>
                    <button type="button" class="lte-add" :aria-pressed="has(o.id)" :disabled="!has(o.id) && full" :aria-label="`添加 ${o.name}`"
                      @click="set(o.id, !has(o.id))">{{ has(o.id) ? '已添加' : '添加' }}</button>
                  </li>
                </ul>
              </template>

              <template v-else-if="tab === 'common'">
                <div v-for="g in commonGroups" :key="g.title" class="lte-group">
                  <h5>{{ g.title }}</h5>
                  <ul class="lte-rows">
                    <li v-for="o in g.items" :key="o.id">
                      <span class="lte-name"><span>{{ o.name }}</span><small>{{ o.sub }}</small></span>
                      <button type="button" class="lte-add" :aria-pressed="has(o.id)" :disabled="!has(o.id) && full" :aria-label="`添加 ${o.name}`"
                        @click="set(o.id, !has(o.id))">{{ has(o.id) ? '已添加' : '添加' }}</button>
                    </li>
                  </ul>
                </div>
              </template>

              <template v-else-if="tab === 'mine'">
                <LatencyAddressForm v-if="adding" ask-group @save="addAddress" @cancel="adding = false" />
                <button v-else type="button" class="ix-text-button lte-new" @click="adding = true">新增地址</button>
                <ul v-if="mineItems.length" class="lte-rows">
                  <li v-for="o in mineItems" :key="o.id">
                    <span class="lte-name"><span>{{ o.name }}</span><small>{{ o.sub }}</small></span>
                    <button type="button" class="lte-add" :aria-pressed="has(o.id)" :disabled="!has(o.id) && full" :aria-label="`添加 ${o.name}`"
                      @click="set(o.id, !has(o.id))">{{ has(o.id) ? '已添加' : '添加' }}</button>
                  </li>
                </ul>
                <p v-else-if="!adding" class="instance-muted lte-none">还没有自己的地址。新增的地址存进地址库，别的服务器也能选。</p>
              </template>

              <template v-if="found ? found.servers.length : tab === 'servers'">
                <p v-if="!found && !serverGroups.length" class="instance-muted lte-none">还没有别的服务器接入。</p>
                <p v-else-if="!found && !onMesh && others.some(x => x.meshAddress)" class="instance-muted lte-none">这台不在组网里，只能测别的服务器的公网地址。</p>
                <div v-for="g in found ? [{title: '服务器', items: found.servers}] : serverGroups" :key="g.title" class="lte-group">
                  <h5>{{ g.title }}<span class="ob-grow" />
                    <button v-if="!found && groupLeft(g) > 1" type="button" class="ix-text-button" :disabled="full" @click="addGroup(g)">全部添加（{{ groupLeft(g) }}）</button></h5>
                  <ul class="lte-rows">
                    <li v-for="x in g.items" :key="x.id" :data-server="x.id" :data-pick="addressesOf(x).length > 1 || null">
                      <span class="lte-name"><span><span v-if="flag(x.region)" class="sv-flag" aria-hidden="true">{{ flag(x.region) }}</span>{{ x.name }}</span>
                        <small v-if="addressesOf(x).length < 2" class="mono">{{ addressesOf(x)[0].ip || '没有公网地址' }}</small></span>
                      <el-dropdown v-if="addressesOf(x).length > 1" class="lte-at" trigger="click" @command="a => setWay(x, {via: a.via})">
                        <button type="button" class="lte-at-button" :aria-label="`${x.name} 的地址：${addressName(x)}`"><span>{{ addressName(x) }}</span>
                          <el-icon><ArrowDown /></el-icon></button>
                        <template #dropdown>
                          <el-dropdown-menu>
                            <el-dropdown-item v-for="a in addressesOf(x)" :key="a.via" :command="a" :class="{'lte-at-on': a.via === wayOf(x).via}">
                              {{ a.name }} · {{ a.ip || '没有这个地址' }}</el-dropdown-item>
                          </el-dropdown-menu>
                        </template>
                      </el-dropdown>
                      <span v-if="reachable(x)" class="lte-way">
                        <select :value="wayOf(x).mode" :aria-label="`${x.name} 的协议`" @change="setWay(x, {mode: $event.target.value})">
                          <option value="icmp">ICMP</option><option value="tcp">TCP</option><option value="http">HTTP</option>
                        </select>
                        <input v-if="wayOf(x).mode !== 'icmp'" :value="wayOf(x).port" type="number" inputmode="numeric" min="1" max="65535" :aria-label="`${x.name} 的端口`"
                          @change="setWay(x, {port: $event.target.value})" />
                      </span>
                      <button type="button" class="lte-add" :aria-pressed="Boolean(serverIdOf(x))" :disabled="!serverIdOf(x) && (!serverAddress(x, wayOf(x).via) || full)"
                        :aria-label="`添加 ${x.name}`" @click="setServer(x, !serverIdOf(x))">{{ serverIdOf(x) ? '已添加' : '添加' }}</button>
                    </li>
                  </ul>
                </div>
              </template>
            </div>
          </template>
        </section>
      </div>

      <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
      <div class="lte-actions">
        <el-button :disabled="busy" @click="emit('update:modelValue', false)">取消</el-button>
        <el-button type="primary" native-type="submit" :loading="busy">保存</el-button>
      </div>
    </form>
  </DdnsSheet>
</template>
