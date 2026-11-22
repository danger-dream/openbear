<script setup>
import { computed, ref, watch } from 'vue';
import { bytes, noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { byAddress, errorsAt, FAST_DEFAULT_PORT, FAST_PATH_STATES, fastChannelText, fastListener, fastPathText, fastProtoText, fastSources, listenWarnings, msText, nameOf, portProblem } from './meshHelpers.js';
import MeshFastPortEdit from './MeshFastPortEdit.vue';
import MeshFr from './MeshFr.vue';
import MeshNodeName from './MeshNodeName.vue';

// A node's part in the fast relay: the fast ports on it, the relay port it listens on (relays and fast ports' nodes),
// how it takes its connections to fast ports, and its channels: the paths to each fast port's node it keeps warm.
const props = defineProps({
  member: {type: Object, required: true},
  index: {type: Number, required: true},
  detail: {type: Object, default: null},
  phone: Boolean,
});
const emit = defineEmits(['open', 'net']);

const m = computed(() => props.member);
const id = computed(() => m.value.serverId);
const members = computed(() => (mesh.work?.members || []).filter(x => !x.removing).sort(byAddress));
const linux = computed(() => (m.value.platform || 'linux') === 'linux');
const allPorts = computed(() => mesh.work?.fastPorts || []);
const ports = computed(() => allPorts.value.filter(p => p.serverId === id.value));
const listening = computed(() => fastListener(mesh.work, m.value, mesh.data?.effectivePorts?.fast));
const defaultPort = computed(() => Number(mesh.work?.settings?.fast?.port) || FAST_DEFAULT_PORT);
const effectivePort = computed(() => mesh.data?.effectivePorts?.fast?.[id.value] || customPort.value || defaultPort.value);
const customPort = computed(() => Number(m.value.listen?.fast) || 0);
const mappedPort = computed(() => Number(m.value.publicPorts?.fast) || 0);
const editing = ref(null);

const at = f => errorsAt(mesh.errors, `members[${props.index}].${f}`);
const portText = ref(''), portLocal = ref('');
watch(() => `${id.value}|${m.value.listen?.fast || ''}`, () => {
  portText.value = m.value.listen?.fast ? String(m.value.listen.fast) : '';
  portLocal.value = '';
}, {immediate: true});
function setCustomPort(raw) {
  const v = String(raw ?? '').trim();
  portText.value = v;
  if (!v || Number(v) === defaultPort.value) {
    portLocal.value = '';
    portText.value = '';
    // The controller keeps a port left out of listen: back to the default is said as 0.
    if (m.value.listen?.fast) {
      m.value.listen = {...m.value.listen, fast: 0};
      void save();
    }
    return;
  }
  const problem = portProblem(v, m.value.listen, 'fast');
  portLocal.value = problem;
  if (problem) return;
  m.value.listen = {...(m.value.listen || {}), fast: Number(v)};
  void save();
}
const portErrors = computed(() => [...(portLocal.value ? [portLocal.value] : []), ...at('listen.fast')]);
const portWarnings = computed(() => listenWarnings(mesh.data, props.index).fast || []);

const fastStatus = computed(() => props.detail?.status?.fast || mesh.data?.nodes?.[id.value]?.summary?.fast || null);
const capture = computed(() => fastStatus.value?.capture || null);
// How this machine takes its connections to the fast ports, in words: [tone, text].
const captureText = computed(() => {
  const c = capture.value;
  if (!c) return null;
  if (c.missing) return ['warn', `本机没有 ${c.missing} 命令，访问快速端口的连接走加密组网；装上 nftables 后自动接管`];
  if (!c.active) return ['bad', `没有生效${c.error ? `：${c.error}` : ''}，访问快速端口的连接走加密组网`];
  if (c.mode === 'tun') return ['ok', '由组网数据面接管（TCP 和 UDP）'];
  return c.udp ? ['ok', '由独立 nftables 表接管（TCP 和 UDP）'] : ['warn', `只接管 TCP${c.error ? `（${c.error}）` : ''}，UDP 走加密组网`];
});
const channels = computed(() => (fastStatus.value?.channels || []).map(ch => ({...ch, id: id.value})));
const unfolded = ref('');
const tcpMem = computed(() => fastStatus.value?.tcpMem || null);
const memTriplet = arr => Array.isArray(arr) && arr.length === 3 ? arr.map(v => bytes(v)).join(' / ') : '';
const roles = computed(() => {
  const fs = fastStatus.value;
  if (!fs) return [];
  return [['起点', fs.entry], ['中转', fs.transit], ['终点', fs.exit]].filter(([, r]) => r);
});
const refused = computed(() => {
  const fs = fastStatus.value;
  return fs ? [fs.headerErrors && `头部无效 ${fs.headerErrors}`, fs.refusedEntry && `起点规则拒绝 ${fs.refusedEntry}`, fs.refusedExit && `终点规则拒绝 ${fs.refusedExit}`].filter(Boolean) : [];
});
</script>
<template>
  <section id="m3-sec-fast" class="m3-dsec" aria-label="快速中转">
    <h3>快速中转</h3>
    <p class="m3-explain">在<b>出口和代理 → 快速端口</b>里声明一台节点的端口后，访问它的组网地址加这个端口就自动沿组网选出的最快路径直连接力；路径、放行和回落都由组网自动完成。数据在节点间不再加密，只适合 ss2022、HTTPS、SSH 这类本身已加密的流量。</p>

    <MeshFr label="这台的快速端口">
      <button v-for="p in ports" :key="p.id" type="button" class="m3-rel" @click="editing = {id: p.id}">
        <span class="ob-grow"><b class="mono">{{ m.address ? `${m.address}:${p.port}` : `:${p.port}` }}</b> · {{ fastProtoText(p.proto) }} · {{ fastSources(p.src, members) }}
          <span v-if="p.note" class="m3-faint"> · {{ p.note }}</span></span><span class="m3-chev">›</span></button>
      <span v-if="!ports.length" class="m3-faint">没有。这台跑着 ss2022 之类的服务时，可以把它的端口声明成快速端口。</span>
      <div class="m3-ctl"><el-button size="small" @click="editing = {create: true, member: id}">＋ 在这台声明快速端口</el-button></div>
    </MeshFr>

    <MeshFr v-if="listening" label="快速中转监听端口" :auto="{value: String(customPort ? defaultPort : effectivePort), why: customPort || effectivePort === defaultPort ? `优先全局设置 ${defaultPort}` : '自动避开已用端口'}" :changed="Boolean(customPort)" restorable :errors="portErrors"
      @restore="setCustomPort('')">
      <div class="m3-ctl">
        <input v-bind="noAutofill" class="m3-input m3-short mono" inputmode="numeric" maxlength="5" :value="portText" :placeholder="String(effectivePort)" aria-label="快速中转监听端口"
          @input="portText = $event.target.value" @change="setCustomPort($event.target.value)" />
        <span v-if="mappedPort" class="m3-faint">对外端口映射 <span class="mono">:{{ mappedPort }}</span></span>
        <button type="button" class="ix-text-button" @click="emit('net')">{{ mappedPort ? '修改端口映射' : '设置对外端口映射' }}</button>
      </div>
      <small v-for="w in portWarnings" :key="w.text" class="ob-warn m3-block">{{ w.text }}</small>
      <template #hint>{{ m.role === 'relay' ? '中转' : '快速端口所在的节点' }}在这个 TCP 端口上接别的节点的快速通道，自动放行。留空自动分配（当前 {{ effectivePort }}，优先 {{ defaultPort }}）；在 NAT 后面做了端口转发的，到「网络参数 → 对外地址」里填映射出去的端口。</template>
    </MeshFr>

    <template v-if="fastStatus">
      <h4 class="m3-subh">实时运行与诊断</h4>
      <p v-if="fastStatus.listenError" class="m3-callout" data-tone="bad">快速中转端口监听失败：{{ fastStatus.listenError }}</p>
      <p v-if="tcpMem?.limited" class="m3-callout" data-tone="warn">
        <span><b>TCP 内存偏小，速度和并发受限</b><small v-if="tcpMem.container">（容器环境无法改写内核 tcp_mem，当前 {{ memTriplet(tcpMem.currentBytes) || '未知' }}）</small>
          <small v-else-if="tcpMem.error">（{{ tcpMem.error }}）</small></span>
      </p>
      <dl class="m3-kvs m3-fast-kvs">
        <div><dt>监听</dt><dd>{{ fastStatus.port ? `TCP ${fastStatus.port}` : '不监听（不是中转，也没有快速端口）' }}</dd></div>
        <div v-if="captureText"><dt>接管本机{{ linux && m.gateway?.subnets?.length ? '和局域网' : '' }}的连接</dt><dd :class="captureText[0] === 'ok' ? '' : `ob-${captureText[0]}`">{{ captureText[1] }}</dd></div>
        <div v-if="tcpMem"><dt>TCP 内存水位</dt>
          <dd :class="tcpMem.limited ? 'ob-warn' : ''">
            <template v-if="tcpMem.changed">{{ memTriplet(tcpMem.originalBytes) }} → {{ memTriplet(tcpMem.currentBytes) }}</template>
            <template v-else>{{ memTriplet(tcpMem.currentBytes) || '—' }}<small class="m3-faint">{{ tcpMem.readOnly ? '（只读）' : '（原值已满足）' }}</small></template>
          </dd></div>
        <div v-if="Number.isFinite(tcpMem?.pressures24h)"><dt>TCP 内存告急</dt>
          <dd :class="tcpMem.pressures24h ? 'ob-warn' : ''">最近 24 小时 {{ tcpMem.pressures24h }} 次<small class="m3-faint">（开机以来 {{ tcpMem.pressuresSinceBoot }} 次）</small></dd></div>
        <div v-if="refused.length"><dt>拦截统计</dt><dd>{{ refused.join(' · ') }}</dd></div>
      </dl>

      <h4 v-if="channels.length" class="m3-subh">这台的快速通道</h4>
      <ul v-if="channels.length" class="m3-fast-users" aria-label="这台的快速通道">
        <li v-for="ch in channels" :key="ch.dest">
          <button type="button" class="m3-fast-user" :aria-expanded="unfolded === ch.dest" @click="unfolded = unfolded === ch.dest ? '' : ch.dest">
            <span class="ob-dot" :data-tone="ch.ready ? 'ok' : ch.paths?.some(x => x.state === 'untested') ? 'warn' : 'bad'" aria-hidden="true"></span>
            <b>到 {{ nameOf(members, ch.dest) }}</b>
            <span class="m3-fast-user-now">{{ fastChannelText(members, ch) }}</span>
            <span class="m3-chev">{{ unfolded === ch.dest ? '⌃' : '⌄' }}</span>
          </button>
          <div v-if="unfolded === ch.dest" class="m3-fast-paths">
            <div v-for="(x, i) in ch.paths" :key="i" class="m3-fast-path">
              <span class="m3-no">{{ i + 1 }}</span>
              <span class="m3-fast-path-hops">{{ fastPathText(members, id, x) }}<span v-if="x.fixed" class="m3-tag">自建路线</span><span v-if="i === ch.using" class="m3-tag" data-kind="ok">在用</span></span>
              <span class="m3-faint"><span class="ob-dot" :data-tone="(FAST_PATH_STATES[x.state] || FAST_PATH_STATES.untested)[0]" aria-hidden="true"></span>
                {{ (FAST_PATH_STATES[x.state] || FAST_PATH_STATES.untested)[1] }}<template v-if="x.rttMs"> · {{ msText(x.rttMs) }}</template></span>
              <small v-if="x.error" class="ob-bad m3-block">{{ x.error }}</small>
            </div>
            <small class="m3-faint m3-block">走快速通道 {{ ch.streams }} 次 · 回落加密组网 {{ ch.fallbacks }} 次</small>
          </div>
        </li>
      </ul>

      <div class="m3-fast-roles" aria-label="各角色流量">
        <div v-for="[label, r] in roles" :key="label" class="m3-fast-role">
          <b>{{ label }}</b>
          <span>活动 {{ r.active }} · 累计 {{ r.opened }}<template v-if="r.failed">（失败 <span class="ob-bad">{{ r.failed }}</span>）</template></span>
          <span class="mono m3-faint">↓ {{ bytes(r.bytesIn) }} · ↑ {{ bytes(r.bytesOut) }}</span>
        </div>
      </div>

      <div v-if="fastStatus.peers?.length" class="m3-tw m3-fast-peers">
        <table class="m3-table" aria-label="下一跳连接池">
          <thead><tr><th>下一跳</th><th>目标地址</th><th>空闲连接</th><th>建连延迟</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="p in fastStatus.peers" :key="p.peer">
              <td><button type="button" class="ix-text-button" @click="emit('open', p.peer)"><MeshNodeName :id="p.peer" plain /></button></td>
              <td class="mono">{{ p.target || '—' }} <span v-if="p.viaMesh" class="m3-tag" title="不能直连，这一段经加密组网">走组网</span></td>
              <td>{{ p.ready }}</td>
              <td>{{ msText(p.rttMs) || '—' }}</td>
              <td>
                <span v-if="p.error" class="ob-bad">{{ p.error }}</span>
                <span v-else-if="p.down" class="ob-warn">暂不可达</span>
                <span v-else class="m3-faint">正常</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <MeshFastPortEdit :at="editing" :phone="phone" @close="editing = null" @open="x => { editing = null; emit('open', x); }" />
  </section>
</template>
