<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { DdnsApi } from '../../ddns/ddnsApi.js';
import { serverDomains } from '../../ddns/ddnsHelpers.js';
import { NetApi } from '../netApi.js';
import { addressList } from '../netHelpers.js';
import { noAutofill } from '../serverHelpers.js';
import { FAST_DEFAULT_PORT, fastListener, listens, TRANSPORTS } from './meshHelpers.js';
import { mesh } from './meshStore.js';

// Where others reach a member, picked from what is known about the machine: the IPv4 its node
// reports (following it), that IPv4 or its IPv6 written down, a dynamic domain bound to it, a LAN
// address, not announced at all, or typed by hand with the ports a mapping forwards.
const props = defineProps({member: {type: Object, required: true}, item: {type: Object, default: null}, online: {type: Boolean, default: true}, errors: {type: Array, default: () => []},
  site: Boolean, name: {type: String, default: 'mesh-address'}});
const emit = defineEmits(['change']);
const m = computed(() => props.member);
const info = computed(() => props.item?.node?.info || {});
const ipv4 = computed(() => m.value.publicEndpointAuto || info.value.ipv4 || '');
const ipv6 = computed(() => info.value.ipv6 || '');
const domains = ref([]), lans = ref([]);
async function load() {
  try {
    const d = await DdnsApi.list();
    domains.value = serverDomains(d?.records, m.value.serverId);
  } catch { domains.value = []; }
  if (!props.online) return;
  try {
    const list = addressList(await NetApi.addresses(m.value.serverId));
    lans.value = [...new Set(list.interfaces.filter(i => i.kind !== 'mesh').flatMap(i => i.addrs).filter(a => !a.control && !a.ip.includes(':') && a.tags.some(([, t]) => t === '私有')).map(a => a.ip))];
  } catch { lans.value = []; }
}
onMounted(load);
watch(() => m.value.serverId, load);

const options = computed(() => {
  const autoOption = m.value.front?.length && !ipv4.value
    ? {key: 'auto', label: '未设置对外地址（前置机不自动探测公网 IP）', note: '要让国内节点连入这台前置机，请选下方“动态域名”或“手填”国内入口地址', value: ''}
    : {key: 'auto', label: `自动：节点上报的公网 IPv4${ipv4.value ? `（现在是 ${ipv4.value}）` : ''}`, note: '推荐，IP 变了会跟着变', value: ''};
  const out = [autoOption];
  if (ipv4.value) out.push({key: 'ipv4', label: `公网 IPv4，固定写死：${ipv4.value}`, note: 'IP 变了不会跟着变', value: ipv4.value});
  if (ipv6.value) out.push({key: 'ipv6', label: `公网 IPv6：${ipv6.value}`, note: '只有有 IPv6 的节点能连到它', value: ipv6.value});
  for (const d of domains.value) out.push({key: `ddns:${d}`, label: `动态域名：${d}`, note: '在“动态域名”里绑定到这台，家宽 IP 会变时用', value: d});
  for (const ip of lans.value) out.push({key: `lan:${ip}`, label: `内网地址：${ip}`, note: '网卡上的局域网地址，只有同一内网的节点能连', value: ip});
  out.push({key: 'none', label: '不对外宣告', note: '别人不主动连它，只能它连别人或打洞'});
  out.push({key: 'custom', label: '手填', note: '其他地址；有端口映射时，在下面填映射出去的端口'});
  return out;
});
const chosen = computed(() => {
  if (m.value.announce === false) return 'none';
  const v = m.value.publicEndpoint || '';
  if (!v) return 'auto';
  if (m.value.publicPorts && Object.keys(m.value.publicPorts).length) return 'custom';
  return options.value.find(o => o.value === v && o.key !== 'auto')?.key || 'custom';
});
const typed = ref(m.value.publicEndpoint || '');
watch(() => m.value.publicEndpoint, v => { typed.value = v || ''; });
const custom = ref(false);
const showCustom = computed(() => custom.value || chosen.value === 'custom');
function pick(key) {
  custom.value = key === 'custom';
  if (key === 'custom') return;
  const o = options.value.find(x => x.key === key);
  // The server keeps what a member leaves out: announcing again and no port mapping are sent as such.
  m.value.announce = key !== 'none';
  if (key !== 'none') m.value.publicEndpoint = o?.value || '';
  m.value.publicPorts = {};
  emit('change');
}
function setTyped() {
  const v = typed.value.trim();
  m.value.announce = true;
  m.value.publicEndpoint = v;
  emit('change');
}
// Kept here while typed: the member's ports are written back on change.
const fastListenPort = computed(() => Number(m.value.listen?.fast) || mesh.data?.effectivePorts?.fast?.[m.value.serverId] || Number(mesh.work?.settings?.fast?.port) || FAST_DEFAULT_PORT);
const ports = ref({});
// By what it maps: no mapping sent as {} and coming back left out is the same, and keeps what is being typed.
watch(() => JSON.stringify(m.value.publicPorts || {}), () => {
  const v = m.value.publicPorts;
  ports.value = Object.fromEntries([...TRANSPORTS, ['fast', '快速中转']].map(([t]) => [t, v?.[t] ? String(v[t]) : '']));
}, {immediate: true});
function setMapping(t, value) {
  const n = Number(String(value).trim() || 0);
  const next = {...(m.value.publicPorts || {})};
  if (n > 0) next[t] = n; else delete next[t];
  // A mapping pins the listener it maps to, including an automatically chosen safe port.
  if (t === 'fast' && n > 0 && !m.value.listen?.fast) m.value.listen = {...m.value.listen, fast: fastListenPort.value};
  m.value.publicPorts = next;
  emit('change');
}
</script>
<template>
  <div class="m3-address">
    <p v-if="site" class="instance-muted">站点中转在 NAT 后面，别人只能和它打洞；这里选什么都不会用。</p>
    <div class="m3-radio" role="radiogroup" aria-label="对外地址">
      <label v-for="o in options" :key="o.key" :data-on="(showCustom ? 'custom' : chosen) === o.key || undefined">
        <input type="radio" :name="name" :checked="(showCustom ? 'custom' : chosen) === o.key" @change="pick(o.key)" />
        <span><span class="m3-radio-label">{{ o.label }}</span><small class="instance-muted">{{ o.note }}</small></span>
      </label>
    </div>
    <div v-if="showCustom" class="m3-address-custom">
      <input v-model.trim="typed" v-bind="noAutofill" class="mono" placeholder="IP 或域名，例如 1.2.3.4" aria-label="手填对外地址" @change="setTyped" />
      <div class="m3-mapping">
        <label v-for="[t, label] in TRANSPORTS.filter(([x]) => listens(m, x))" :key="t">
          <span>{{ label }} 映射到</span>
          <input v-bind="noAutofill" inputmode="numeric" maxlength="5" class="mono" v-model.trim="ports[t]" :placeholder="String(m.listen[t])" :aria-label="`${label} 对外端口`" @change="setMapping(t, ports[t])" />
        </label>
        <label v-if="fastListener(mesh.work, m, mesh.data?.effectivePorts?.fast)">
          <span>快速中转 映射到</span>
          <input v-bind="noAutofill" inputmode="numeric" maxlength="5" class="mono" v-model.trim="ports.fast" :placeholder="String(fastListenPort)" aria-label="快速中转 对外端口" @change="setMapping('fast', ports.fast)" />
        </label>
      </div>
      <small class="instance-muted">端口留空表示和监听端口相同。</small>
    </div>
    <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>
  </div>
</template>
