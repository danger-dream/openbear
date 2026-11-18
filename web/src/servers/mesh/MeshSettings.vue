<script setup>
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { noAutofill } from '../serverHelpers.js';
import { mesh, revert, save } from './meshStore.js';
import { COST_FORMULA, errorMessage, errorsAt, errorsUnder, FAST_DEFAULT_PATHS, FAST_DEFAULT_PORT, HEALTH, meshPrefixProblem, ROUTING_DEFAULTS, ROUTING_PARAMS, TRANSPORTS } from './meshHelpers.js';
import MeshChips from './MeshChips.vue';
import MeshField from './MeshField.vue';
import MeshFr from './MeshFr.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// Network-wide settings, in the panel when nothing is selected or as the 设置 page. Written to the
// draft together on save, not field by field, because a new prefix renumbers every member.
const props = defineProps({page: Boolean, items: {type: Array, default: () => []}});
// Where you usually are, kept in this browser: routes and latencies are shown from there.
const HOME_KEY = 'openbear.mesh.home';
const homes = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const home = ref((() => { try { return localStorage.getItem(HOME_KEY) || ''; } catch { return ''; } })());
function setHome(value) { home.value = value; try { localStorage.setItem(HOME_KEY, value); } catch { /* private mode */ } }
const homeShown = computed(() => homes.value.some(m => m.serverId === home.value) ? home.value : homes.value.some(m => m.serverId === 'local') ? 'local' : homes.value.find(m => m.role === 'relay')?.serverId || '');
const current = computed(() => mesh.work?.settings || {});
// settings.proxy.port: the one port every node's tunnel between proxy ports and exits listens on.
const TUNNEL_PORT = 51080, MTU_DEFAULT = 1400;
const PORTS_HINT = 'UDP、TCP、TLS、快速中转是节点打开监听时的默认端口，可以在节点上单独改；隧道端口所有节点统一使用，供代理端口和出口之间通信，只在组网内可达，自动放行。快速中转端口在有快速端口时由中转和快速端口所在的节点监听，自动放行。';
const FAST_PATHS_HINT = '访问快速端口时，按组网的选路（自建路线在前，再是当前路由和次优路由）同时打通并保温这么多条路径；在用的那条断了，新连接立刻走下一条。';
const formOf = s => ({prefix: s.prefix || '', fastPaths: s.fast?.paths ?? FAST_DEFAULT_PATHS, mtu: s.mtu ?? '', tcpJumbo: s.tcpJumbo === true,
  ports: {udp: 51999, tcp: 52000, tls: 52001, ...(s.ports || {})}, tunnel: s.proxy?.port ?? TUNNEL_PORT, fastPort: s.fast?.port ?? FAST_DEFAULT_PORT,
  health: Object.fromEntries(HEALTH.map(([key]) => [key, s.health?.[key] ?? ''])), stun: [...(Array.isArray(s.reachability?.stun) ? s.reachability.stun : [])],
  routing: {...ROUTING_DEFAULTS, ...(s.routing || {})}});
// The routing parameters, a row each: [key, before the box, after it].
const ROUTING_ROWS = [
  {label: '切换门槛', hint: '路线没断时，另一条的代价至少低这么多才换过去；百分比填 0 表示不要求。', fields: [['switchMs', '至少便宜', 'ms'], ['switchPct', '且超过', '%']]},
  {label: '中转更少时', hint: '经过的中转更少的路线，便宜这么多、并持续下面的“持续时间”，就换过去。', fields: [['shorterMs', '至少便宜', 'ms'], ['shorterPct', '且超过', '%']]},
  {label: '通告门槛', hint: '链路实测代价和已通告的差这么多、并持续“持续时间”，才重新通告；各节点按已通告的代价选路。', fields: [['advertMs', '差超过', 'ms'], ['advertPct', '且超过', '%']]},
  {label: '持续时间', hint: '通告门槛和“中转更少时”要持续这么久才算数，短暂的波动不动路线。', fields: [['holdS', '', '秒']]},
  {label: '抖动权重', hint: '每 1 ms 抖动在代价里算几 ms。', fields: [['jitterWeight', '×', '']]},
  {label: '丢包权重', hint: '丢包率乘以这个数加到代价上：1000 时丢 1% 约加 10 ms。', fields: [['lossWeight', '', 'ms（全丢时）']]},
  {label: '来回切后等待', hint: '10 分钟内来回切过的路线，再因为更便宜而切换前要先等这么久，每多切一次翻倍，最多到上限；线路断了照样立即切。', fields: [['flapHoldS', '先等', '秒'], ['flapHoldMaxS', '最长', '秒']]},
];
const rowDefault = row => row.fields.map(([k, , unit]) => `${ROUTING_DEFAULTS[k]}${unit ? ` ${unit.replace(/（.*/, '')}` : ''}`).join(' · ');
const rowChanged = row => row.fields.some(([k]) => Number(form.routing[k]) !== ROUTING_DEFAULTS[k]);
const rowErrors = row => row.fields.flatMap(([k]) => at(`routing.${k}`));
const routingErrors = computed(() => errorsUnder(mesh.errors, 'settings.routing').length + Object.keys(local.value).filter(k => k.startsWith('routing.')).length);
const routingDefault = computed(() => ROUTING_PARAMS.every(([k, v]) => Number(form.routing[k]) === v));
function routingDefaults() { form.routing = {...ROUTING_DEFAULTS}; }
// Extra STUN servers: host:port (a name or an IPv4 address), IPv6 as [v6]:port.
const HOST_PORT = /^(?:(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?|\[[0-9a-f:.]+\]):(\d{1,5})$/i;
const stunProblem = value => { const m = HOST_PORT.exec(value); return m && Number(m[1]) >= 1 && Number(m[1]) <= 65535 ? '' : '写成 主机:端口，例如 stun.example.com:3478；IPv6 写成 [2001:db8::1]:3478'; };
const stunErrors = computed(() => errorsUnder(mesh.errors, 'settings.reachability').map(errorMessage));
const form = reactive(formOf(current.value));
const healthOpen = ref(HEALTH.some(([key]) => errorsAt(mesh.errors, `settings.health.${key}`).length)), busy = ref(false), local = ref({});
let submitted = false;
const changed = computed(() => JSON.stringify(form) !== JSON.stringify(formOf(current.value)));
// Follow the draft (a discard, another tab) while nothing is typed here.
watch(() => JSON.stringify(formOf(current.value)), (now, before) => { if (JSON.stringify(form) === before) Object.assign(form, JSON.parse(now)); });
const renumber = computed(() => current.value.prefix && form.prefix.trim() && form.prefix.trim() !== current.value.prefix);
const at = path => [...(local.value[path] ? [local.value[path]] : []), ...errorsAt(mesh.errors, `settings.${path}`)];
const healthErrors = computed(() => errorsUnder(mesh.errors, 'settings.health').length);
const portErrors = computed(() => [...TRANSPORTS.flatMap(([t]) => at(`ports.${t}`)), ...at('ports'), ...(local.value['proxy.port'] ? [local.value['proxy.port']] : []),
  ...errorsUnder(mesh.errors, 'settings.proxy').map(errorMessage), ...at('fast.port')]);
const portsChanged = computed(() => Number(form.ports.udp) !== 51999 || Number(form.ports.tcp) !== 52000 || Number(form.ports.tls) !== 52001 || Number(form.tunnel) !== TUNNEL_PORT || Number(form.fastPort) !== FAST_DEFAULT_PORT);

function check() {
  const problems = {};
  const prefix = meshPrefixProblem(form.prefix);
  if (prefix) problems.prefix = prefix;
  const mtu = Number(form.mtu);
  if (form.mtu !== '' && (!Number.isInteger(mtu) || mtu < 1280 || mtu > 1420)) problems.mtu = 'MTU 在 1280–1420 之间';
  for (const [t] of TRANSPORTS) {
    const port = Number(form.ports[t]);
    if (!Number.isInteger(port) || port < 1 || port > 65535) problems[`ports.${t}`] = '端口在 1–65535 之间';
  }
  const tunnel = Number(form.tunnel);
  if (!Number.isInteger(tunnel) || tunnel < 1 || tunnel > 65535) problems['proxy.port'] = '隧道端口在 1–65535 之间';
  const fport = Number(form.fastPort);
  if (!Number.isInteger(fport) || fport < 1 || fport > 65535) problems['fast.port'] = '快速中转端口在 1–65535 之间';
  const fpaths = Number(form.fastPaths);
  if (!Number.isInteger(fpaths) || fpaths < 1 || fpaths > 8) problems['fast.paths'] = '路径条数在 1～8 之间';
  for (const [key, , lo, hi] of ROUTING_PARAMS) {
    const v = Number(form.routing[key]);
    if (form.routing[key] === '' || !Number.isFinite(v) || v < lo || v > hi) problems[`routing.${key}`] = `在 ${lo}～${hi} 之间`;
  }
  if (!problems['routing.flapHoldMaxS'] && Number(form.routing.flapHoldMaxS) < Number(form.routing.flapHoldS)) problems['routing.flapHoldMaxS'] = '最长等待不能少于先等的时间';
  local.value = problems;
  return !Object.keys(problems).length;
}
async function submit() {
  if (!check()) return;
  busy.value = true; submitted = true;
  // Left empty is the default, as each field says; the controller keeps a value left out, so it is sent.
  const health = Object.fromEntries(HEALTH.map(([k, , , fallback]) => [k, form.health[k] === '' || form.health[k] == null ? fallback : Number(form.health[k])]));
  // An older controller has no reachability settings: leave the field out until it has one or the list is used.
  const reach = current.value.reachability || form.stun.length ? {reachability: {...(current.value.reachability || {}), stun: [...form.stun]}} : {};
  // An older controller has no routing parameters either.
  const routing = current.value.routing ? {routing: Object.fromEntries(ROUTING_PARAMS.map(([k]) => [k, Number(form.routing[k])]))} : {};
  const proxy = current.value.proxy || Number(form.tunnel) !== TUNNEL_PORT ? {proxy: {...(current.value.proxy || {password: ''}), port: Number(form.tunnel)}} : {};
  const fast = current.value.fast || Number(form.fastPort) !== FAST_DEFAULT_PORT || Number(form.fastPaths) !== FAST_DEFAULT_PATHS
    ? {fast: {...(current.value.fast || {}), port: Number(form.fastPort), paths: Number(form.fastPaths)}} : {};
  mesh.work.settings = {...mesh.work.settings, prefix: form.prefix.trim(), mtu: form.mtu === '' ? MTU_DEFAULT : Number(form.mtu), tcpJumbo: form.tcpJumbo,
    ports: Object.fromEntries(TRANSPORTS.map(([t]) => [t, Number(form.ports[t])])), health, ...reach, ...routing, ...proxy, ...fast};
  await save();
  busy.value = false;
  if (errorsUnder(mesh.errors, 'settings').length) { healthOpen.value ||= healthErrors.value > 0; return; }
  if (!mesh.rejected && !mesh.conflict && !mesh.error) {
    // What was saved, fields left empty included, as the draft now has it.
    Object.assign(form, formOf(current.value));
    ElMessage.success('已保存到草稿，应用后生效');
  }
}
// Back to the draft's settings; settings the draft check rejected are dropped.
function reset() {
  if (submitted && mesh.rejected) revert();
  submitted = false; local.value = {};
  Object.assign(form, formOf(current.value));
}
</script>
<template>
  <form class="instance-form mesh-settings" :class="{'is-page': props.page}" autocomplete="off" novalidate @submit.prevent="submit">
    <div v-if="props.page" class="m3-dsec m3-settings" :class="{busy}">
      <MeshFr label="常用位置" hint="路线、延迟默认从这里算起。只记在这个浏览器里，只影响显示，不影响组网。">
        <MeshNodeSelect :model-value="homeShown" :options="homes.map(m => ({value: m.serverId, label: m.name, region: String(m.region || props.items.find(i => i.id === m.serverId)?.region || '').toUpperCase()}))"
          aria-label="常用位置" width="260px" @change="setHome" />
      </MeshFr>
      <MeshFr label="地址段" hint="组网内部用的私有地址段，不要和任何局域网重叠。改了所有节点会重新分配地址。" :auto="{value: '100.96.0.0/16', why: '默认'}" :changed="form.prefix.trim() !== '100.96.0.0/16'" :errors="at('prefix')">
        <input v-model="form.prefix" v-bind="noAutofill" class="m3-input m3-mid mono" placeholder="100.96.0.0/16" aria-label="地址段" />
        <small v-if="renumber" class="ob-warn m3-block">所有节点地址会重新分配，手工填的地址也会被替换。</small>
      </MeshFr>
      <MeshFr label="MTU" hint="1280–1420。线路上有 PPPoE 之类导致大包丢失时调小；留空用默认值。" :auto="{value: String(MTU_DEFAULT), why: '默认'}" :changed="form.mtu !== '' && Number(form.mtu) !== MTU_DEFAULT" :errors="at('mtu')">
        <input v-model="form.mtu" v-bind="noAutofill" type="number" inputmode="numeric" min="1280" max="1420" class="m3-input m3-short mono" placeholder="1400" aria-label="MTU" />
      </MeshFr>
      <MeshFr label="TCP 大包" hint="带宽更高；有一段是 UDP、旧版或 macOS/Windows 节点时，那条路还按 MTU。" :auto="{value: '关', why: '默认关'}" :changed="form.tcpJumbo" :errors="at('tcpJumbo')">
        <button type="button" class="m3-sw" role="switch" :aria-checked="form.tcpJumbo" aria-label="TCP 直连大包" @click="form.tcpJumbo = !form.tcpJumbo"><i></i><span>每一段都是 TCP/TLS 的 Linux 节点之间用 9000 字节大包</span></button>
      </MeshFr>
      <MeshFr label="默认端口" :hint="PORTS_HINT" :auto="{value: `UDP 51999 · TCP 52000 · TLS 52001 · 隧道 ${TUNNEL_PORT} · 快速中转 ${FAST_DEFAULT_PORT}`, why: '默认'}" :changed="portsChanged" :errors="portErrors">
        <div class="m3-ctl m3-ports"><span v-for="[t, label] in TRANSPORTS" :key="t" class="m3-pair"><span class="m3-faint">{{ label }}</span><input v-model="form.ports[t]" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="m3-input m3-short mono" :aria-label="`${label} 默认端口`" /></span>
          <span class="m3-pair"><span class="m3-faint">隧道</span><input v-model="form.tunnel" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="m3-input m3-short mono" aria-label="隧道端口" /></span>
          <span class="m3-pair"><span class="m3-faint">快速中转</span><input v-model="form.fastPort" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="m3-input m3-short mono" aria-label="快速中转默认端口" /></span></div>
      </MeshFr>
      <MeshFr label="快速通道路径" :hint="FAST_PATHS_HINT" :auto="{value: `${FAST_DEFAULT_PATHS} 条`, why: '默认'}" :changed="Number(form.fastPaths) !== FAST_DEFAULT_PATHS" :errors="at('fast.paths')">
        <div class="m3-ctl"><span class="m3-faint">每个快速端口打通</span><input v-model="form.fastPaths" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="8" class="m3-input m3-short mono" aria-label="快速通道路径条数" /><span class="m3-faint">条</span></div>
      </MeshFr>
      <MeshFr label="STUN 服务器" hint="额外的观测点，用来查节点的真实出口。第三方服务会看到你的出口地址。" :auto="{value: '不用', why: '只用自己的中转'}" :changed="form.stun.length > 0" :errors="stunErrors">
        <MeshChips v-model="form.stun" label="添加 STUN 服务器" placeholder="不填就只用自己的中转，例如 stun.example.com:3478" mono :max="8" :validate="stunProblem" />
      </MeshFr>
      <section class="m3-routing" aria-label="选路">
        <h4 class="m3-routing-title">选路<span v-if="routingErrors" class="m3-tag" data-kind="bad">{{ routingErrors }} 处需修改</span>
          <button type="button" class="ix-text-button" :disabled="routingDefault" @click="routingDefaults">全部恢复默认</button></h4>
        <p class="m3-hint">{{ COST_FORMULA }}</p>
        <MeshFr v-for="row in ROUTING_ROWS" :key="row.label" :label="row.label" :hint="row.hint" :auto="{value: rowDefault(row), why: `默认 ${rowDefault(row)}`}" :changed="rowChanged(row)" :errors="rowErrors(row)">
          <div class="m3-ctl"><template v-for="[key, before, unit] in row.fields" :key="key"><span v-if="before" class="m3-faint">{{ before }}</span>
            <input v-model="form.routing[key]" v-bind="noAutofill" type="number" inputmode="decimal" step="any" class="m3-input m3-short mono" :placeholder="String(ROUTING_DEFAULTS[key])" :aria-label="`${row.label} ${before} ${unit}`.replace(/\s+/g, ' ').trim()" /><span v-if="unit" class="m3-faint">{{ unit }}</span></template></div>
        </MeshFr>
      </section>
      <details class="m3-details m3-health" :open="healthOpen" @toggle="healthOpen = $event.target.open">
        <summary>健康探测参数（{{ HEALTH.length }} 项，只影响自动切换入口）<span v-if="healthErrors" class="m3-tag" data-kind="bad">{{ healthErrors }} 处需修改</span></summary>
        <MeshFr v-for="[key, label, unit, fallback] in HEALTH" :key="key" :label="label" :auto="{value: String(fallback), why: `默认 ${fallback}`}" :changed="form.health[key] !== '' && Number(form.health[key]) !== fallback" :errors="at(`health.${key}`)">
          <div class="m3-ctl"><input v-model="form.health[key]" v-bind="noAutofill" type="number" inputmode="numeric" min="0" class="m3-input m3-short mono" :placeholder="String(fallback)" :aria-label="label" /><span class="m3-faint">{{ unit }}</span></div>
        </MeshFr>
      </details>
    </div>
    <div v-else class="mesh-settings-fields" :class="{busy}">
      <MeshField label="地址段" help="网格内部使用的私有地址段，不要和任何成员所在的局域网重叠。">
        <input v-model="form.prefix" v-bind="noAutofill" class="mono" :class="{invalid: at('prefix').length}" placeholder="100.96.0.0/16" aria-label="地址段" />
        <small v-for="e in at('prefix')" :key="e" class="ob-bad">{{ e }}</small>
        <small v-if="renumber" class="ob-warn">所有成员地址会重新分配，手工填的地址也会被替换。</small>
      </MeshField>
      <MeshField label="MTU" help="网格隧道的最大传输单元，1280–1420；留空用默认值。开了 TCP 直连大包时，大包路径以外仍按这里。">
        <input v-model="form.mtu" v-bind="noAutofill" type="number" inputmode="numeric" min="1280" max="1420" class="mesh-short" :class="{invalid: at('mtu').length}" placeholder="1400" aria-label="MTU" />
        <small v-for="e in at('mtu')" :key="e" class="ob-bad">{{ e }}</small>
      </MeshField>
      <MeshField label="大包" help="两个 Linux 节点之间每一段都是 TCP 或 TLS 时，用 9000 字节的大包，带宽更高。有一段是 UDP、旧版或 macOS、Windows 节点时，那条路径仍按上面的 MTU。">
        <label class="ob-check"><input v-model="form.tcpJumbo" type="checkbox" /><span>TCP 直连大包</span></label>
        <small v-for="e in at('tcpJumbo')" :key="e" class="ob-bad">{{ e }}</small>
      </MeshField>
      <MeshField label="默认端口" :help="PORTS_HINT">
        <div class="mesh-ports">
          <label v-for="[t, label] in TRANSPORTS" :key="t"><span>{{ label }}</span><input v-model="form.ports[t]" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="mono" :class="{invalid: at(`ports.${t}`).length}" :aria-label="`${label} 默认端口`" /></label>
          <label><span>隧道</span><input v-model="form.tunnel" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="mono" :class="{invalid: local['proxy.port'] || errorsUnder(mesh.errors, 'settings.proxy').length}" aria-label="隧道端口" /></label>
          <label><span>快速中转</span><input v-model="form.fastPort" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="65535" class="mono" :class="{invalid: at('fast.port').length}" aria-label="快速中转默认端口" /></label>
        </div>
        <small v-for="e in portErrors" :key="e" class="ob-bad">{{ e }}</small>
      </MeshField>
      <MeshField label="快速通道路径" :help="FAST_PATHS_HINT">
        <div class="ob-inline"><input v-model="form.fastPaths" v-bind="noAutofill" type="number" inputmode="numeric" min="1" max="8" class="mesh-short mono" :class="{invalid: at('fast.paths').length}" aria-label="快速通道路径条数" /><span>条</span></div>
        <small v-for="e in at('fast.paths')" :key="e" class="ob-bad">{{ e }}</small>
      </MeshField>
      <MeshField label="STUN" help="额外观测点：节点用它们和中转一起查自己的真实出口。这些是第三方服务，会看到你的出口地址；留空就只用自己的中转。">
        <MeshChips v-model="form.stun" label="添加 STUN 服务器" placeholder="stun.example.com:3478" mono :max="8" :validate="stunProblem" />
        <small v-for="e in stunErrors" :key="e" class="ob-bad">{{ e }}</small>
      </MeshField>
      <div class="mesh-routing-fields" aria-label="选路">
        <p class="instance-muted">{{ COST_FORMULA }}</p>
        <MeshField v-for="row in ROUTING_ROWS" :key="row.label" :label="row.label" :help="row.hint">
          <div class="ob-inline mesh-routing-row"><template v-for="[key, before, unit] in row.fields" :key="key"><span v-if="before" class="sv-unit-label">{{ before }}</span>
            <input v-model="form.routing[key]" v-bind="noAutofill" type="number" inputmode="decimal" step="any" class="mesh-short mono" :class="{invalid: at(`routing.${key}`).length}" :placeholder="String(ROUTING_DEFAULTS[key])"
              :aria-label="`${row.label} ${before} ${unit}`.replace(/\s+/g, ' ').trim()" /><small v-if="unit" class="sv-unit-label">{{ unit }}</small></template></div>
          <small v-for="e in rowErrors(row)" :key="e" class="ob-bad">{{ e }}</small>
        </MeshField>
        <button type="button" class="ix-text-button" :disabled="routingDefault" @click="routingDefaults">选路参数全部恢复默认</button>
      </div>
      <div class="mesh-fold">
        <button type="button" class="ix-text-button mesh-fold-toggle" :aria-expanded="healthOpen" @click="healthOpen = !healthOpen">
          {{ healthOpen ? '收起' : '展开' }}健康探测参数<span v-if="healthErrors" class="pb-tag" data-tone="bad">{{ healthErrors }} 处需修改</span>
        </button>
        <div v-if="healthOpen" class="mesh-health">
          <p class="instance-muted">只影响自动切换入口；留空用默认值。</p>
          <label v-for="[key, label, unit, fallback] in HEALTH" :key="key" class="mesh-health-item">
            <span>{{ label }}</span>
            <span class="ob-inline">
              <input v-model="form.health[key]" v-bind="noAutofill" type="number" inputmode="numeric" min="0" :placeholder="String(fallback)" :class="{invalid: at(`health.${key}`).length}" :aria-label="label" />
              <small class="sv-unit-label">{{ unit }}</small>
            </span>
            <small v-for="e in at(`health.${key}`)" :key="e" class="ob-bad">{{ e }}</small>
          </label>
        </div>
      </div>
    </div>
    <div class="instance-form-footer">
      <el-button :disabled="!changed && !(submitted && mesh.rejected)" @click="reset">还原</el-button>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!changed">保存到草稿</el-button>
    </div>
  </form>
</template>
