<script setup>
import { computed, ref, watch } from 'vue';
import { relativeTime } from '../../instances/instanceHelpers.js';
import { bytes, noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { allowLines, byAddress, cidr, errorMessage, errorsUnder, FAST_PROTOS, FORWARD_LINES, FORWARD_PROXY, forwardErrorText, forwardOpen, forwardRun, forwardState,
  ipv4, msText, nameOf, newForward } from './meshHelpers.js';
import MeshDialog from './MeshDialog.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// A port forward: clients of a port on the entry go to an address and port the target reaches, its own loopback by default,
// over the mesh or the fast relay. at: {id} edits it as it is (each change saved); {create: true, entry?, target?} makes a new
// one, added on 添加.
const props = defineProps({at: {type: Object, default: null}, phone: Boolean, regionOf: {type: Function, default: () => ''}});
const emit = defineEmits(['close']);
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing).sort(byAddress));
const creating = computed(() => Boolean(props.at?.create));
const fresh = ref(null), text = ref({listen: null, port: null}), allowText = ref(null);
watch(() => props.at, at => {
  fresh.value = at?.create ? newForward(at.entry || '', at.target || '') : null;
  text.value = {listen: null, port: null};
  allowText.value = null;
}, {immediate: true});
const list = computed(() => work.value?.forwards || []);
const f = computed(() => creating.value ? fresh.value : list.value.find(x => x.id === props.at?.id) || null);
const index = computed(() => list.value.indexOf(f.value));
const errors = computed(() => creating.value || index.value < 0 ? [] : errorsUnder(mesh.errors, `forwards[${index.value}]`).map(errorMessage));
const commit = () => { if (!creating.value) void save(); };
const nodeOptions = computed(() => members.value.map(m => ({value: m.serverId, label: m.name, region: props.regionOf(m.serverId), note: m.address || ''})));
function setNode(key, id) { f.value[key] = id; commit(); }

// Existing records keep edits local until the complete value validates on change.
// New records are local too; typing can enable 添加 without committing shared draft prefixes.
const shown = key => text.value[key] ?? (f.value?.[key] ? String(f.value[key]) : '');
const kinds = proto => proto === 'any' ? ['tcp', 'udp'] : [proto];
function portProblem(key) {
  const v = shown(key).trim();
  if (!v) return text.value[key] === null ? '' : '请输入端口';
  const n = Number(v);
  if (!/^\d+$/.test(v) || !Number.isInteger(n) || n < 1 || n > 65535) return '端口在 1～65535 之间';
  if (key !== 'listen') return '';
  const same = list.value.find(x => x !== f.value && x.enabled && x.entry === f.value.entry && x.listen === n && kinds(x.proto).some(k => kinds(f.value.proto).includes(k)));
  return same ? `${nameOf(members.value, f.value.entry)} 上已经有转发在用 ${n}` : '';
}
function typePort(key, v) {
  text.value = {...text.value, [key]: String(v ?? '').trim()};
  const n = Number(text.value[key]);
  if (creating.value && !portProblem(key)) f.value[key] = n;
}
function setPort(key, v) {
  typePort(key, v);
  if (portProblem(key)) return;
  f.value[key] = Number(text.value[key]);
  text.value = {...text.value, [key]: null};
  commit();
}
function setHost(v) { f.value.host = String(v || '').trim() || '127.0.0.1'; commit(); }
function setProto(k) {
  f.value.proto = k;
  if (k === 'udp') f.value.proxyProtocol = 0;
  commit();
}
function set(key, value) { f.value[key] = value; commit(); }

// ---- who may connect ----
const limiting = ref(false);
const open = computed(() => !limiting.value && forwardOpen(f.value?.allow));
watch(() => props.at, () => { limiting.value = false; });
function setOpen(on) {
  limiting.value = !on;
  if (on) { f.value.allow = {prefixes: [], provinces: []}; allowText.value = null; commit(); }
}
const prefixesShown = computed(() => allowText.value ?? (f.value?.allow?.prefixes || []).join('\n'));
const looksLikeNet = s => ipv4(s) !== null || cidr(s) !== null || /^[0-9a-f:.]*:[0-9a-f:.]*(\/\d{1,3})?$/i.test(s);
const badPrefixes = computed(() => allowText.value === null ? [] : allowLines(allowText.value).filter(s => !looksLikeNet(s)));
function setPrefixes(v) {
  allowText.value = v;
  if (badPrefixes.value.length) return;
  f.value.allow = {...f.value.allow, prefixes: allowLines(v)};
  allowText.value = null;
  commit();
}
const geo = computed(() => mesh.data?.forwardGeo || null);
const provinceOptions = computed(() => (geo.value?.provinces || []).map(p => ({value: p.name, label: p.name, count: p.v4 + p.v6})));
function setProvinces(v) { f.value.allow = {...f.value.allow, provinces: v}; commit(); }
const geoText = computed(() => {
  const g = geo.value;
  if (!g) return '';
  if (g.error && !g.ready) return `省份数据下载失败：${g.error}。主控会每小时重试，应用前需要它下载好。`;
  if (!g.ready) return '省份数据还没下载：保存后主控自动从 ip2region 下载（约 50 MB），应用前需要它下载好。';
  return `省份网段来自 ip2region，主控每天检查更新，有变化自动下发；最近更新 ${relativeTime(g.updatedAt, Date.now())}。`;
});

const line = computed(() => f.value?.line || 'mesh');
const sameNode = computed(() => Boolean(f.value?.entry && f.value.entry === f.value.target));
const canAdd = computed(() => Boolean(f.value?.entry && f.value.target && f.value.listen && f.value.port && !portProblem('listen') && !portProblem('port')
  && !badPrefixes.value.length && !(sameNode.value && line.value === 'fast')));
function add() {
  if (!canAdd.value) return;
  if (allowText.value !== null) f.value.allow = {...f.value.allow, prefixes: allowLines(allowText.value)};
  work.value.forwards = [...list.value, {...fresh.value}];
  void save();
  emit('close');
}
function remove() {
  work.value.forwards = list.value.filter(x => x !== f.value);
  void save();
  emit('close');
}

// ---- how it runs ----
const run = computed(() => creating.value || !f.value ? null : forwardRun(mesh.data, f.value));
const state = computed(() => creating.value || !f.value ? null : forwardState(mesh.data, f.value));
const title = computed(() => creating.value ? '新建端口转发' : `端口转发 ${nameOf(members.value, f.value?.entry)} :${f.value?.listen || ''}`);
</script>
<template>
  <MeshDialog :model-value="Boolean(at && f)" :title="title" :phone="phone" width="660px" @update:model-value="v => { if (!v) emit('close'); }">
    <div v-if="f" class="m3-form m3-fwd" :class="{'is-phone': phone}">
      <p class="instance-muted">入口节点上开一个端口，连进来的客户端被转到目标节点能访问的地址（默认是目标自己的 127.0.0.1），代替在入口上跑 realm。入口防火墙自动放行这个端口，目标只放行这条转发。</p>

      <div class="m3-fwd-pair">
        <div class="m3-field"><span>入口节点</span>
          <MeshNodeSelect :model-value="f.entry" :options="nodeOptions" aria-label="入口节点" width="100%" @update:model-value="v => setNode('entry', v)" />
          <small class="instance-muted">客户端连这台的公网地址，比如专线入口机。</small></div>
        <div class="m3-field"><span>监听端口</span>
          <input v-bind="noAutofill" class="m3-input mono" inputmode="numeric" maxlength="5" :value="shown('listen')" placeholder="例如 443" aria-label="监听端口"
            @input="typePort('listen', $event.target.value)" @change="setPort('listen', $event.target.value)" />
          <small v-if="portProblem('listen')" class="ob-bad">{{ portProblem('listen') }}</small></div>
      </div>

      <div class="m3-field"><span>协议</span>
        <div class="m3-seg" role="radiogroup" aria-label="协议">
          <button v-for="[k, l] in FAST_PROTOS" :key="k" type="button" role="radio" :aria-checked="f.proto === k" @click="setProto(k)">{{ l }}</button>
        </div></div>

      <div class="m3-fwd-pair">
        <div class="m3-field"><span>目标节点</span>
          <MeshNodeSelect :model-value="f.target" :options="nodeOptions" aria-label="目标节点" width="100%" @update:model-value="v => setNode('target', v)" /></div>
        <div class="m3-field"><span>目标地址和端口</span>
          <div class="m3-ctl m3-fwd-dest">
            <input v-bind="noAutofill" class="m3-input mono" maxlength="253" :value="f.host" placeholder="127.0.0.1" aria-label="目标地址" @change="setHost($event.target.value)" />
            <span class="m3-faint">:</span>
            <input v-bind="noAutofill" class="m3-input m3-short mono" inputmode="numeric" maxlength="5" :value="shown('port')" placeholder="端口" aria-label="目标端口"
              @input="typePort('port', $event.target.value)" @change="setPort('port', $event.target.value)" /></div>
          <small v-if="portProblem('port')" class="ob-bad">{{ portProblem('port') }}</small>
          <small v-else class="instance-muted">由目标节点去连：127.0.0.1 是它自己，也可以填它能访问的任意 IP 或域名。</small></div>
      </div>

      <div class="m3-field"><span>线路</span>
        <div class="m3-seg" role="radiogroup" aria-label="线路">
          <button v-for="[k, l] in FORWARD_LINES" :key="k" type="button" role="radio" :aria-checked="line === k" :disabled="k === 'fast' && sameNode" @click="set('line', k)">{{ l }}</button>
        </div>
        <small class="instance-muted">{{ line === 'fast'
          ? '快速通道：沿组网选出的最快路径逐跳直连，载荷不再加密，更快；适合本身已加密的流量（TLS、ss2022、SSH）。首个连接先走组网，后台打通后自动切换。'
          : '组网：经加密隧道、按组网自动选的线路到目标，更安全；入口到目标断了会自动换路。' }}</small></div>

      <div class="m3-field"><span>PROXY protocol</span>
        <div class="m3-seg" role="radiogroup" aria-label="PROXY protocol">
          <button v-for="[k, l] in FORWARD_PROXY" :key="k" type="button" role="radio" :aria-checked="(f.proxyProtocol || 0) === k" :disabled="k && f.proto === 'udp'" @click="set('proxyProtocol', k)">{{ l }}</button>
        </div>
        <small class="instance-muted">让目标拿到客户端的真实 IP（只对 TCP）。目标程序要支持并开启它，比如 nginx 的 proxy_protocol、Xray 的 acceptProxyProtocol，否则连不上。</small></div>

      <div class="m3-field"><span>来源</span>
        <div class="m3-seg" role="radiogroup" aria-label="来源">
          <button type="button" role="radio" :aria-checked="open" @click="setOpen(true)">所有来源</button>
          <button type="button" role="radio" :aria-checked="!open" @click="setOpen(false)">只允许白名单</button>
        </div>
        <template v-if="!open">
          <label class="m3-fwd-sub"><span class="m3-faint">IP 或网段</span>
            <textarea v-bind="noAutofill" class="m3-input m3-fwd-nets mono" rows="3" :value="prefixesShown" placeholder="每行一个，例如&#10;1.2.3.4&#10;112.115.147.0/24" aria-label="允许的 IP 或网段"
              @change="setPrefixes($event.target.value)"></textarea></label>
          <small v-if="badPrefixes.length" class="ob-bad">这些不是 IP 或网段：{{ badPrefixes.join('、') }}</small>
          <div class="m3-fwd-sub"><span class="m3-faint">省份</span>
            <el-select v-if="provinceOptions.length" :model-value="f.allow.provinces" multiple filterable collapse-tags collapse-tags-tooltip :max-collapse-tags="phone ? 2 : 4"
              placeholder="选省份，比如 广东省" aria-label="允许的省份" popper-class="m3-tokens" style="width: 100%" @update:model-value="setProvinces">
              <el-option v-for="o in provinceOptions" :key="o.value" :value="o.value" :label="o.label"><span>{{ o.label }}</span><small v-if="o.count" class="m3-faint"> {{ o.count }} 个网段</small></el-option>
            </el-select>
            <small class="instance-muted">{{ geoText }}</small></div>
          <small v-if="forwardOpen(f.allow)" class="ob-warn">还没填白名单：现在仍允许所有来源。</small>
          <small v-else class="instance-muted">IP、网段和省份任一匹配就放行，其余客户端连上就被断开。</small>
        </template></div>

      <div class="m3-field"><span>启用</span>
        <button type="button" class="m3-sw" role="switch" :aria-checked="f.enabled" aria-label="启用" @click="set('enabled', !f.enabled)"><i></i><span>{{ f.enabled ? '启用' : '草稿停用（应用后生效）' }}</span></button></div>

      <label class="m3-field"><span>备注</span><input v-bind="noAutofill" class="m3-input" maxlength="200" :value="f.note" placeholder="比如 广东专线 → 香港 xray" aria-label="备注"
        @change="set('note', $event.target.value.trim())" /></label>

      <div v-if="state" class="m3-field"><span>运行情况</span>
        <span><span class="ob-dot" :data-tone="state[0]" aria-hidden="true"></span> {{ state[1] }}</span>
        <template v-if="run">
          <small v-if="run.listen?.length" class="m3-faint m3-block">监听：<span class="mono">{{ run.listen.join('、') }}</span></small>
          <small v-for="e in run.errors" :key="e" class="ob-bad m3-block">没有监听上：{{ e }}</small>
          <small class="m3-faint m3-block">累计 {{ run.opened }} 次<template v-if="run.refused"> · 白名单拦下 {{ run.refused }} 次</template><template v-if="run.failed"> · 失败 {{ run.failed }} 次</template>
            · 入 {{ bytes(run.bytesIn) }} · 出 {{ bytes(run.bytesOut) }}</small>
          <small v-if="run.via" class="m3-faint m3-block">到 {{ nameOf(members, f.target) }}：{{ run.via === 'fast' ? '走快速通道' : '快速通道还没通，新连接暂走组网' }}<template v-if="run.rttMs"> · 往返 {{ msText(run.rttMs) }}</template></small>
          <small v-else class="m3-faint m3-block">到 {{ nameOf(members, f.target) }} 的隧道：{{ run.ready ? `预备 ${run.ready} 条` : '没有预备好的' }}<template v-if="run.rttMs"> · 往返 {{ msText(run.rttMs) }}</template>
            <span v-if="run.tunnelError" class="ob-bad"> · {{ run.tunnelError }}</span></small>
          <small v-if="run.lastError" class="ob-warn m3-block">最近一次失败：{{ forwardErrorText(run.lastError) }}<template v-if="run.lastErrorAt">（{{ relativeTime(run.lastErrorAt, Date.now()) }}）</template></small>
        </template></div>
      <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>

      <div class="m3-dialog-foot">
        <template v-if="creating"><span class="ob-grow" /><el-button @click="emit('close')">取消</el-button>
          <el-button type="primary" :disabled="!canAdd" @click="add">添加</el-button></template>
        <template v-else><el-button type="danger" plain @click="remove">删除转发</el-button><span class="ob-grow" />
          <el-button type="primary" @click="emit('close')">完成</el-button></template>
      </div>
    </div>
  </MeshDialog>
</template>
