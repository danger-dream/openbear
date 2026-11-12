<script setup>
import { computed, reactive, ref, watch } from 'vue';
import { flag, regionName } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { mesh, meshError } from './meshStore.js';
import { isRelay, isSite, msText, nameOf, TRANSPORTS, transportLabel } from './meshHelpers.js';
import MeshNodeName from './MeshNodeName.vue';
import MeshAddress from './MeshAddress.vue';
import MeshDialog from './MeshDialog.vue';

// Adding a server: pick it, let its node measure every relay, then its role, how it connects (recommended
// from the measurements, each with its reason), where others reach it, and what will happen. The draft
// gets it once the mesh service is installed (run by the page: `join` carries the choices).
const props = defineProps({modelValue: Boolean, phone: Boolean, items: {type: Array, default: () => []}});
const emit = defineEmits(['update:modelValue', 'join']);
const STEPS = ['选服务器', '角色', '怎么连进组网', '对外地址', '确认'];
const ORDER = ['udp', 'tcp', 'tls'];
const RES = {ok: ['ok', '✓', '通'], timeout: ['bad', '✕', '超时'], refused: ['bad', '✕', '拒绝连接'], closed: ['off', '—', '没开'], punch: ['warn', '洞', '要打洞'], unknown: ['off', '?', '没测到']};
const step = ref(0);
const d = reactive({serverId: '', probe: null, probing: false, error: '', role: 'member', cands: [], links: [], member: null, tags: ''});

const data = computed(() => mesh.data);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const relays = computed(() => members.value.filter(isRelay));
const itemOf = id => props.items.find(i => i.id === id) || null;
const regionOf = id => String(members.value.find(m => m.serverId === id)?.region || itemOf(id)?.region || '').toUpperCase();
const nm = id => nameOf(members.value, id) || itemOf(id)?.name || id;
const eligible = computed(() => (data.value?.eligible || []).filter(e => !members.value.some(m => m.serverId === e.serverId)));
const ready = computed(() => eligible.value.filter(e => e.ok));
const blocked = computed(() => eligible.value.filter(e => !e.ok));
const ELIGIBLE_REASONS = {node_offline: '节点离线，连上后才能加入', capability_disabled: '节点关了命令执行，装不了组网服务', node_update_required: '节点版本太旧，先更新节点',
  platform_unsupported: '这个系统不能加入组网', node_not_enrolled: '还没装节点'};
const reasonText = e => ELIGIBLE_REASONS[e.reason] || e.reason || '暂时不能加入';
const chosen = computed(() => eligible.value.find(e => e.serverId === d.serverId) || null);
const item = computed(() => itemOf(d.serverId));
const platform = computed(() => item.value?.node?.info?.platform || 'linux');
const linux = computed(() => platform.value === 'linux');
const ipv4 = computed(() => item.value?.node?.info?.ipv4 || '');
const region = computed(() => regionOf(d.serverId));

// ---- measuring ----
async function pickServer(id) {
  if (d.serverId === id && (d.probe || d.probing)) return;
  Object.assign(d, {serverId: id, probe: null, probing: true, error: '', cands: [], links: [], member: null});
  try {
    const r = await MeshApi.probe(id);
    if (d.serverId !== id) return;
    d.probe = (r?.results || []).filter(p => relays.value.some(x => x.serverId === p.relay)).map(p => ({...p, usable: ORDER.some(t => p[t] === 'ok')}));
    d.role = advice.value[0];
  } catch (e) {
    if (d.serverId !== id) return;
    const code = e?.response?.data?.error;
    d.error = code === 'node_update_required' ? '这台的节点版本太旧，测不了速：先在服务器页更新节点。也可以不测速直接加入，再手动选中转。' : `测速失败：${meshError(e)}。也可以不测速直接加入，再手动选中转。`;
    d.probe = [];
    d.role = 'member';
  } finally { if (d.serverId === id) d.probing = false; }
}
const score = p => p.rttMs * (1 + (p.loss || 0) * 30);
const usable = computed(() => (d.probe || []).filter(p => p.usable && Number.isFinite(p.rttMs)).sort((a, b) => score(a) - score(b)));
const best = computed(() => usable.value[0] || null);
// Each relay's recommendation and its reason, from the measurements only.
const rank = computed(() => {
  const out = new Map(), b = best.value;
  usable.value.forEach((p, i) => {
    const ok = ORDER.filter(t => p[t] === 'ok').map(transportLabel).join('、');
    if (i === 0) out.set(p.relay, {rec: 1, why: `延迟最低（${msText(p.rttMs)}），${ok} 通`});
    else if (i === 1 && p.rttMs <= Math.max(b.rttMs * 4, b.rttMs + 60) && (p.loss || 0) < 0.01) out.set(p.relay, {rec: 2, why: `第二近（${msText(p.rttMs)}），第一台连不上时自动切过去`});
    else out.set(p.relay, {rec: 0, why: `实测 ${msText(p.rttMs)}${p.loss >= 0.01 ? `、丢包 ${Math.round(p.loss * 100)}%` : ''}，比 ${nm(b.relay)} 多 ${msText(p.rttMs - b.rttMs)}`});
  });
  for (const p of d.probe || []) if (!out.has(p.relay)) {
    const states = ORDER.map(t => p[t]);
    out.set(p.relay, {rec: 0, why: states.includes('punch') ? '在 NAT 后，要打洞才能连，不适合当接入' : states.some(s => s === 'timeout' || s === 'refused') ? '连不上：开着的协议都不通，它可能不接受这里连入' : states.includes('unknown') ? '没测到' : '没开任何监听'});
  }
  return out;
});
const summary = computed(() => {
  if (!d.probe) return '';
  if (!d.probe.length) return d.error || '组网里还没有中转可测。';
  return `测完了：到 ${d.probe.length} 台中转，${usable.value.length} 台能连上${best.value ? `；最近的是 ${nm(best.value.relay)}（${msText(best.value.rttMs)}）` : ''}。`;
});

// ---- role ----
const sameRegionRelays = computed(() => relays.value.filter(r => regionOf(r.serverId) === region.value && region.value));
const advice = computed(() => {
  const b = best.value;
  if (!linux.value) return ['member', `它是 ${platform.value === 'windows' ? 'Windows' : platform.value === 'darwin' ? 'macOS' : platform.value}，只能当成员。`];
  if (b && b.rttMs < 25) return ['member', `附近已经有中转 ${nm(b.relay)}，离它只有 ${msText(b.rttMs)}。当成员经它接入就行；再加一台中转，路线也不会更短。`];
  if (!ipv4.value) return ['member', '它没有公网 IP，别人连不到它，当成员更合适。'];
  return ['relay', `附近 25ms 内没有中转（${b ? `最近的是 ${nm(b.relay)}，${msText(b.rttMs)}` : '没测到能连上的中转'}）${sameRegionRelays.value.length ? '' : `，${regionName(region.value) || '这个地区'}还没有中转`}。它有公网 IP，当中转可以让附近的节点就近接入、经它转发。`];
});

// ---- connecting ----
const probeOf = r => (d.probe || []).find(p => p.relay === r) || null;
// The first protocol the relay answers on; auto when that is what its listen ports give anyway.
function transportFor(relay) {
  const p = probeOf(relay), m = members.value.find(x => x.serverId === relay);
  const open = ORDER.filter(t => Number(m?.listen?.[t]) > 0);
  if (isSite(m)) return {transport: 'udp', auto: true, why: '站点中转只能 UDP 打洞'};
  if (!p) return {transport: open[0] || 'udp', auto: true, why: '没有测速，交给自动'};
  const ok = ORDER.find(t => p[t] === 'ok');
  if (!ok) return {transport: open[0] || 'udp', auto: true, why: '测速都没通'};
  if (ok === open[0]) return {transport: ok, auto: true, why: ok === 'udp' ? 'UDP 通，延迟最低' : `${ORDER.slice(0, ORDER.indexOf(ok)).map(transportLabel).join('、')} 没开，用 ${transportLabel(ok)}`};
  return {transport: ok, auto: false, why: `${ORDER.slice(0, ORDER.indexOf(ok)).filter(t => p[t] !== 'ok').map(t => `${transportLabel(t)}${RES[p[t]]?.[2] || ''}`).join('、')}，用 ${transportLabel(ok)}`};
}
const nearest = computed(() => usable.value.filter(p => !isSite(members.value.find(x => x.serverId === p.relay))).map(p => p.relay));
function defaults() {
  if (d.role === 'member' && !d.cands.length) d.cands = [...rank.value].filter(([, v]) => v.rec).sort((a, b) => a[1].rec - b[1].rec).map(([r]) => ({relay: r, ...transportFor(r)}));
  if (d.role === 'relay' && !d.links.length) d.links = nearest.value.slice(0, 3).map(r => ({relay: r}));
}
watch(() => d.role, () => { d.cands = []; d.links = []; });
function addCand(r) { if (!d.cands.some(c => c.relay === r)) d.cands.push({relay: r, ...transportFor(r)}); }
function setCandTransport(i, t) { const c = d.cands[i]; c.transport = t; c.auto = false; }
function moveCand(i) { if (i > 0) d.cands.splice(i - 1, 0, d.cands.splice(i, 1)[0]); }
function addLink(r) { if (!d.links.some(l => l.relay === r)) d.links.push({relay: r}); }
const fullMesh = computed(() => (mesh.work?.relayLinks?.mode || 'full') === 'full');
const linkMode = computed(() => mesh.work?.relayLinks?.mode || 'full');
const rows = computed(() => [...(d.probe || [])].sort((a, b) => Number(!a.usable) - Number(!b.usable) || (a.rttMs ?? 1e9) - (b.rttMs ?? 1e9)));
const picked = r => d.role === 'member' ? d.cands.some(c => c.relay === r) : d.links.some(l => l.relay === r);
const rowWhy = p => {
  if (d.role === 'member') return rank.value.get(p.relay) || {};
  const i = nearest.value.indexOf(p.relay);
  return i >= 0 && i < 3 ? {rec: 1, why: `实测最近的第 ${i + 1} 台（${msText(p.rttMs)}）；别处的流量可以经这条线路转发`} : rank.value.get(p.relay) || {};
};

// ---- address ----
function ensureMember() {
  if (!d.member || d.member.serverId !== d.serverId) {
    d.member = {serverId: d.serverId, publicEndpoint: '', publicEndpointAuto: ipv4.value, listen: d.role === 'relay' ? {udp: 51999, tcp: 52000, tls: 0} : {udp: 51999, tcp: 0, tls: 0}};
  }
}
const addressText = computed(() => {
  const m = d.member;
  if (!m || m.announce === false) return m ? '不对外宣告' : '';
  return m.publicEndpoint ? `${m.publicEndpoint}${m.publicPorts ? `（端口映射 ${Object.entries(m.publicPorts).map(([t, p]) => `${transportLabel(t)} ${p}`).join('、')}）` : ''}` : `自动（公网 IPv4 ${ipv4.value || '还没上报'}）`;
});
const listenWhy = computed(() => d.role === 'relay' ? '公网中转开 UDP 和 TCP，新节点接入时用；别的中转要用 TLS 连它时自动加上' : '成员只开 UDP：别人一般不主动连成员，UDP 用来打洞直连');

const canNext = computed(() => [Boolean(d.serverId && d.probe && !d.probing), true, d.role === 'member' ? d.cands.length > 0 || !relays.value.length : fullMesh.value || d.links.length > 0, true, true][step.value]);
function next() {
  if (step.value === 1) defaults();
  if (step.value === 2) ensureMember();
  if (step.value < 4) step.value++;
}
function finish() {
  emit('join', {serverId: d.serverId, name: chosen.value?.name || nm(d.serverId), role: d.role, cands: d.role === 'member' ? d.cands.map(c => ({...c})) : [],
    links: d.role === 'relay' && !fullMesh.value ? d.links.map(l => ({...l})) : [], address: d.member ? {publicEndpoint: d.member.publicEndpoint || '', announce: d.member.announce,
      publicPorts: d.member.publicPorts} : null, tags: d.tags.split(/[,，\s]+/).filter(Boolean)});
  close();
}
function close() { emit('update:modelValue', false); }
function reset() { step.value = 0; Object.assign(d, {serverId: '', probe: null, probing: false, error: '', role: 'member', cands: [], links: [], member: null, tags: ''}); }
</script>
<template>
  <MeshDialog :model-value="modelValue" title="加入一台服务器" :phone="phone" width="760px" @update:model-value="emit('update:modelValue', $event)" @closed="reset">
    <ol class="m3-steps" aria-label="步骤">
      <li v-for="(s, i) in STEPS" :key="s" :data-state="i < step ? 'done' : i === step ? 'now' : undefined"><span>{{ i + 1 }}</span>{{ s }}</li>
    </ol>

    <template v-if="step === 0">
      <p class="instance-muted">这些服务器已经装好 OpenBear 节点，可以加入组网。选一台后，它会先测一下到每台中转的延迟、丢包，以及 UDP、TCP、TLS 各自通不通，后面的推荐都按测速结果来。</p>
      <p v-if="!ready.length" class="instance-muted">现在没有可以加入的服务器。还没装节点的，先到“服务器”页添加。</p>
      <div v-else class="m3-pick" role="listbox" aria-label="可以加入的服务器">
        <button v-for="e in ready" :key="e.serverId" type="button" role="option" :aria-selected="d.serverId === e.serverId" @click="pickServer(e.serverId)">
          <span v-if="flag(regionOf(e.serverId))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(e.serverId)) }}</span>
          <span class="ob-grow"><b>{{ e.name }}</b> <span class="m3-faint">{{ regionName(regionOf(e.serverId)) }}</span></span>
          <span class="mono m3-faint">{{ itemOf(e.serverId)?.node?.info?.ipv4 || '' }}</span>
        </button>
      </div>
      <p v-if="d.serverId" class="m3-callout" :data-tone="d.probe ? 'info' : undefined" role="status">{{ d.probing ? `正在从 ${chosen?.name} 测到 ${relays.length} 台中转的延迟，以及 UDP、TCP、TLS 能不能连上…` : summary }}</p>
      <details v-if="blocked.length" class="m3-blocked"><summary class="instance-muted">不能加入的（{{ blocked.length }} 台）</summary>
        <ul class="m3-plain"><li v-for="e in blocked" :key="e.serverId"><b><MeshNodeName :id="e.serverId" :name="e.name" plain /></b> <span class="instance-muted">{{ reasonText(e) }}</span></li></ul>
      </details>
    </template>

    <template v-else-if="step === 1">
      <div class="m3-roles">
        <label class="m3-rolecard" :data-on="d.role === 'member' || undefined"><input v-model="d.role" type="radio" value="member" name="m3-join-role" />
          <div><b>成员</b> <span v-if="advice[0] === 'member'" class="pb-tag" data-tone="ok">建议</span>
            <p>只自己用组网：经一台附近的中转连进来，能访问组网里别的节点和共享的局域网，也可以当出口。<b>不替别人转发，不占它的带宽。</b></p>
            <p class="instance-muted">适合：公司的 Windows 电脑要访问家里的 NAS；一台 VPS 只用来当出口；Windows、macOS 只能当成员。</p></div></label>
        <label class="m3-rolecard" :data-on="d.role === 'relay' || undefined" :data-disabled="!linux || undefined"><input v-model="d.role" type="radio" value="relay" name="m3-join-role" :disabled="!linux" />
          <div><b>中转</b> <span v-if="advice[0] === 'relay'" class="pb-tag" data-tone="ok">建议</span>
            <p>替别人转发：附近的成员可以经它接入；它和其他中转连成线路，组网按实测延迟选路，别处的流量可能经过它。<b>会占用它的带宽和流量。</b></p>
            <p class="instance-muted">适合：线路好、带宽和流量充足的 Linux VPS，比如某个地区的第一个节点、到其他地区线路特别好的节点；家里或公司的软路由当“站点中转”。</p></div></label>
      </div>
      <p class="m3-callout" data-tone="info"><b>{{ chosen?.name }} 的情况：</b>{{ linux ? 'Linux' : platform }} · {{ ipv4 ? `公网 IP ${ipv4}` : '没有公网 IP' }} · 最近的中转是 {{ best ? `${nm(best.relay)}（${msText(best.rttMs)}）` : '没测到' }}
        · {{ regionName(region) || '这个地区' }}现在有 {{ sameRegionRelays.length }} 台中转。<br><b>建议当{{ advice[0] === 'member' ? '成员' : '中转' }}：</b>{{ advice[1] }}之后在它的详情页“角色”里随时能改。</p>
    </template>

    <template v-else-if="step === 2">
      <template v-if="d.role === 'member'">
        <p class="instance-muted">它经哪几台中转连进组网。排第一的优先用，后面的作为备用。按刚才的测速推荐，理由写在每一行。</p>
        <ol class="m3-items">
          <li v-for="(c, i) in d.cands" :key="c.relay">
            <span class="m3-no">{{ i + 1 }}</span>
            <span class="m3-items-main"><b><span v-if="flag(regionOf(c.relay))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(c.relay)) }}</span>{{ nm(c.relay) }}</b><span v-if="i" class="m3-faint"> 备用</span>
              <small>{{ rank.get(c.relay)?.rec ? '推荐：' : '你加的：' }}{{ rank.get(c.relay)?.why || '' }}<br>协议{{ c.auto ? '自动' : '' }}用 {{ transportLabel(c.transport) }}：{{ c.why }}</small></span>
            <span class="sv-ranges" role="radiogroup" :aria-label="`${nm(c.relay)} 的协议`">
              <button v-for="t in ORDER" :key="t" type="button" role="radio" :aria-checked="c.transport === t" :disabled="probeOf(c.relay) && probeOf(c.relay)[t] !== 'ok'"
                :title="probeOf(c.relay) && probeOf(c.relay)[t] !== 'ok' ? `测速：${RES[probeOf(c.relay)[t]]?.[2]}` : ''" @click="setCandTransport(i, t)">{{ transportLabel(t) }}</button>
            </span>
            <button type="button" class="ix-icon-button" :disabled="!i" title="上移" aria-label="上移" @click="moveCand(i)">↑</button>
            <button type="button" class="ix-icon-button" title="移除" aria-label="移除" @click="d.cands.splice(i, 1)">✕</button>
          </li>
          <li v-if="!d.cands.length" class="ob-warn">{{ relays.length ? '至少选一台中转，不然它连不进组网' : '组网里还没有中转：加入后它先自己待着，有了中转再设置' }}</li>
        </ol>
      </template>
      <template v-else>
        <p v-if="fullMesh" class="instance-muted">中转现在是“两两相连”：它加入后自动和每台中转连上线路。下面是测速结果，供参考。</p>
        <template v-else>
          <p class="instance-muted">它和哪些中转连成线路。按测速推荐了最近的几台{{ linkMode === 'auto' ? '（线路是自动生成的：这几条会保留，以后也按实测再调整）' : '' }}；以后组网按实测延迟自动选路。协议和谁主动连交给自动。</p>
          <ol class="m3-items">
            <li v-for="(l, i) in d.links" :key="l.relay">
              <span class="m3-items-main"><b><span v-if="flag(regionOf(l.relay))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(l.relay)) }}</span>{{ nm(l.relay) }}</b>
                <small>{{ nearest.indexOf(l.relay) >= 0 && nearest.indexOf(l.relay) < 3 ? `推荐：实测最近的第 ${nearest.indexOf(l.relay) + 1} 台` : '你加的' }} · 实测 {{ msText(probeOf(l.relay)?.rttMs) || '—' }}</small></span>
              <button type="button" class="ix-icon-button" title="移除" aria-label="移除" @click="d.links.splice(i, 1)">✕</button>
            </li>
            <li v-if="!d.links.length" class="ob-warn">至少连一台中转</li>
          </ol>
        </template>
      </template>
      <h4 class="m3-sub">所有中转的测速结果</h4>
      <div class="m3-table-wrap">
        <table class="m3-table m3-probe">
          <thead><tr><th>中转</th><th>延迟</th><th>丢包</th><th>UDP</th><th>TCP</th><th>TLS</th><th>说明</th><th></th></tr></thead>
          <tbody>
            <tr v-for="p in rows" :key="p.relay" :data-rec="rowWhy(p).rec || undefined">
              <td><span v-if="flag(regionOf(p.relay))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(p.relay)) }}</span>{{ nm(p.relay) }}</td>
              <td>{{ Number.isFinite(p.rttMs) ? msText(p.rttMs) : '—' }}</td>
              <td>{{ Number.isFinite(p.loss) ? `${Math.round(p.loss * 1000) / 10}%` : '—' }}</td>
              <td v-for="t in ORDER" :key="t"><span class="m3-res" :data-tone="RES[p[t]]?.[0]" :title="RES[p[t]]?.[2]">{{ RES[p[t]]?.[1] || '?' }}</span></td>
              <td class="m3-probe-why"><b v-if="rowWhy(p).rec" class="ob-ok">推荐</b>{{ rowWhy(p).rec ? ' · ' : '' }}{{ rowWhy(p).why || '' }}</td>
              <td><span v-if="picked(p.relay)" class="m3-faint">已选</span>
                <el-button v-else-if="p.usable && (d.role === 'member' || !fullMesh)" size="small" @click="d.role === 'member' ? addCand(p.relay) : addLink(p.relay)">加上</el-button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="instance-muted m3-note">✓ 通　✕ 不通　— 没开这个协议　洞 在 NAT 后、要打洞。测速是加入前由这台服务器上的节点做的。</p>
    </template>

    <template v-else-if="step === 3">
      <h4 class="m3-sub">对外地址</h4>
      <p class="instance-muted">别的节点主动连它时用的地址。</p>
      <MeshAddress v-if="d.member" :member="d.member" :item="item" :online="true" name="m3-join-address" />
      <h4 class="m3-sub">监听端口</h4>
      <p class="instance-muted">自动：{{ listenWhy }}。加入后可以在它的详情页“网络参数”里改。</p>
      <h4 class="m3-sub">标签</h4>
      <input v-model="d.tags" class="m3-input" placeholder="可选，例如 sg；多个用空格分开" aria-label="标签" />
    </template>

    <template v-else>
      <p class="instance-muted">确认后会这样：</p>
      <ul class="m3-plain m3-changes">
        <li><MeshNodeName :id="d.serverId" :name="chosen?.name" plain /> 加入组网，作为{{ d.role === 'relay' ? '中转' : '成员' }}</li>
        <template v-if="d.role === 'member'"><li v-for="(c, i) in d.cands" :key="c.relay">经 <MeshNodeName :id="c.relay" plain /> 接入（{{ transportLabel(c.transport) }}{{ c.auto ? '，自动' : '' }}{{ i ? '，备用' : '' }}）</li></template>
        <li v-else-if="fullMesh">和每台中转连上线路（两两相连）</li>
        <li v-for="l in d.role === 'relay' && !fullMesh ? d.links : []" :key="l.relay">新建线路：<MeshNodeName :id="d.serverId" :name="chosen?.name" plain /> ⇄ <MeshNodeName :id="l.relay" plain />（协议和谁主动连按自动值）</li>
        <li>对外地址：{{ addressText }}；监听端口：自动</li>
        <li v-if="d.tags.trim()">标签：{{ d.tags.trim() }}</li>
      </ul>
      <p class="m3-callout" data-tone="info"><b>接下来会发生什么：</b>点“加入草稿”后，组网服务就开始往 {{ chosen?.name }} 上装（不影响它现有的网络，最多约 3 分钟）。装好后它排在节点列表第一行，画板上标着“待应用”；
        这时它还没连进组网，设置都可以再改。确认无误后点顶部的“应用到网格”，它才按这些设置连上。</p>
    </template>

    <template #footer>
      <div class="m3-dialog-foot">
        <el-button v-if="step" @click="step--">上一步</el-button>
        <span class="ob-grow" />
        <el-button @click="close">取消</el-button>
        <el-button v-if="step < 4" type="primary" :disabled="!canNext" @click="next">下一步</el-button>
        <el-button v-else type="primary" @click="finish">加入草稿</el-button>
      </div>
    </template>
  </MeshDialog>
</template>
