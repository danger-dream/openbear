<script setup>
import { computed, ref, watch } from 'vue';
import { relativeTime } from '../../instances/instanceHelpers.js';
import { noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { byAddress, errorMessage, errorsUnder, FAST_ALL_SOURCES, FAST_PATH_STATES, FAST_PROTOS, fastChannelText, fastOpen, fastPathText, fastUsers, msText, nameOf } from './meshHelpers.js';
import MeshDialog from './MeshDialog.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';
import MeshSide from './MeshSide.vue';

// A fast port: connections to a node's mesh address on this port go hop by hop along the mesh's own paths, warmed up
// on first use. at: {id} edits it as it is (each change saved); {create: true, member?} makes a new one, added on 添加.
const props = defineProps({at: {type: Object, default: null}, phone: Boolean, regionOf: {type: Function, default: () => ''}});
const emit = defineEmits(['close', 'open']);
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing).sort(byAddress));
const tags = computed(() => [...new Set(members.value.flatMap(m => m.tags || []))].sort());
const creating = computed(() => Boolean(props.at?.create));
const fresh = ref(null), portText = ref(null);
watch(() => props.at, at => {
  fresh.value = at?.create ? {serverId: at.member || '', port: null, proto: 'any', src: FAST_ALL_SOURCES(), note: ''} : null;
  portText.value = null;
}, {immediate: true});
const list = computed(() => work.value?.fastPorts || []);
const p = computed(() => creating.value ? fresh.value : list.value.find(x => x.id === props.at?.id) || null);
const index = computed(() => list.value.indexOf(p.value));
const member = computed(() => members.value.find(m => m.serverId === p.value?.serverId) || null);
const errors = computed(() => creating.value || index.value < 0 ? [] : errorsUnder(mesh.errors, `fastPorts[${index.value}]`).map(errorMessage));
const commit = () => { if (!creating.value) void save(); };

const nodeOptions = computed(() => members.value.map(m => ({value: m.serverId, label: m.name, region: props.regionOf(m.serverId), note: m.address || ''})));
const portShown = computed(() => portText.value ?? (p.value?.port ? String(p.value.port) : ''));
const portProblem = computed(() => {
  const v = portShown.value.trim();
  if (!v) return portText.value === null ? '' : '请输入端口';
  const n = Number(v);
  if (!/^\d+$/.test(v) || !Number.isInteger(n) || n < 1 || n > 65535) return '端口在 1～65535 之间';
  const same = list.value.find(x => x !== p.value && x.serverId === p.value?.serverId && x.port === n && (x.proto === 'any' || p.value.proto === 'any' || x.proto === p.value.proto));
  return same ? `${member.value?.name || ''} 上已经有 ${n} 这个快速端口` : '';
});
// Taken as typed, so 添加 is ready before the box loses focus; saved when it does.
function typePort(v) {
  portText.value = String(v ?? '').trim();
  const n = Number(portText.value);
  if (creating.value && !portProblem.value) p.value.port = n;
}
function setPort(v) {
  typePort(v);
  if (portProblem.value) return;
  p.value.port = Number(portText.value);
  portText.value = null;
  commit();
}
function setProto(k) { p.value.proto = k; commit(); }
const open = computed(() => fastOpen(p.value?.src));
function setOpen(on) {
  p.value.src = on ? FAST_ALL_SOURCES() : {all: false, allSubnets: false, members: [], tags: [], prefixes: []};
  commit();
}
const address = computed(() => member.value?.address ? `${member.value.address}:${p.value?.port || '端口'}` : '');
const canAdd = computed(() => Boolean(p.value?.serverId && p.value.port && !portProblem.value));
function add() {
  if (!canAdd.value) return;
  work.value.fastPorts = [...list.value, {...fresh.value}];
  void save();
  emit('close');
}
function remove() {
  work.value.fastPorts = list.value.filter(x => x !== p.value);
  void save();
  emit('close');
}

// Who uses it: each node's channel to the port's node, the path new connections take and the ones held ready.
const users = computed(() => creating.value || !p.value ? [] : fastUsers(mesh.data, p.value.serverId));
const unfolded = ref('');
</script>
<template>
  <MeshDialog :model-value="Boolean(at && p)" :title="creating ? '新建快速端口' : `快速端口 ${member?.name || ''} :${p?.port || ''}`" :phone="phone" width="640px"
    @update:model-value="v => { if (!v) emit('close'); }">
    <div v-if="p" class="m3-form">
      <p class="instance-muted m3-fast-lead">访问这台节点的<b>组网地址加这个端口</b>时，自动走快速通道：沿组网选出的最快几条路径直连接力，首次访问先走加密组网并在后台打通，之后的新连接直接走快速通道；访问规则和中转放行都自动完成，客户端照常填组网地址。数据在节点间不再加密，只适合 ss2022、HTTPS、SSH 这类本身已加密的流量。</p>
      <div v-if="creating" class="m3-field"><span>哪台节点</span>
        <MeshNodeSelect v-model="p.serverId" :options="nodeOptions" aria-label="快速端口所在的节点" width="100%" />
        <small class="instance-muted">跑服务的那台，比如装了 ss2022 的服务器。</small></div>
      <div v-else class="m3-field"><span>节点</span><b><MeshNodeName :id="p.serverId" plain /></b></div>

      <div class="m3-field"><span>端口</span>
        <input v-bind="noAutofill" class="m3-input mono" inputmode="numeric" maxlength="5" :value="portShown" placeholder="例如 48888" aria-label="快速端口" style="max-width: 140px"
          @input="typePort($event.target.value)" @change="setPort($event.target.value)" />
        <small v-if="portProblem" class="ob-bad">{{ portProblem }}</small>
        <small v-else-if="address" class="instance-muted">客户端连 <span class="mono">{{ address }}</span>，和平时访问组网地址一样。</small></div>

      <div class="m3-field"><span>协议</span>
        <div class="m3-seg" role="radiogroup" aria-label="协议">
          <button v-for="[k, l] in FAST_PROTOS" :key="k" type="button" role="radio" :aria-checked="(p.proto || 'any') === k" @click="setProto(k)">{{ l }}</button>
        </div></div>

      <div class="m3-field"><span>谁能用</span>
        <div class="m3-seg" role="radiogroup" aria-label="谁能用">
          <button type="button" role="radio" :aria-checked="open" @click="setOpen(true)">所有成员和共享局域网</button>
          <button type="button" role="radio" :aria-checked="!open" @click="setOpen(false)">只允许指定的</button>
        </div>
        <MeshSide v-if="!open" :side="p.src" nets="prefixes" label="谁能用" :members="members" :tags="tags" @change="commit" />
        <small class="instance-muted">这台节点上自动放行这个端口给它们，不用另加访问规则。</small></div>

      <label class="m3-field"><span>备注</span><input v-bind="noAutofill" class="m3-input" maxlength="200" :value="p.note" placeholder="比如 ss2022" aria-label="备注"
        @change="p.note = $event.target.value.trim(); commit()" /></label>

      <div v-if="!creating" class="m3-field"><span>谁在用</span>
        <span v-if="!users.length" class="m3-faint">还没有节点访问过它。第一次访问会先走加密组网，同时在后台打通快速通道。</span>
        <ul v-else class="m3-fast-users">
          <li v-for="u in users" :key="u.id">
            <button type="button" class="m3-fast-user" :aria-expanded="unfolded === u.id" @click="unfolded = unfolded === u.id ? '' : u.id">
              <span class="ob-dot" :data-tone="u.ready ? 'ok' : u.paths?.some(x => x.state === 'untested') ? 'warn' : 'bad'" aria-hidden="true"></span>
              <b>{{ nameOf(members, u.id) }}</b>
              <span class="m3-fast-user-now">{{ fastChannelText(members, u) }}</span>
              <span class="m3-chev">{{ unfolded === u.id ? '⌃' : '⌄' }}</span>
            </button>
            <div v-if="unfolded === u.id" class="m3-fast-paths">
              <div v-for="(x, i) in u.paths" :key="i" class="m3-fast-path">
                <span class="m3-no">{{ i + 1 }}</span>
                <span class="m3-fast-path-hops">{{ fastPathText(members, u.id, x) }}<span v-if="x.fixed" class="m3-tag">自建路线</span><span v-if="i === u.using" class="m3-tag" data-kind="ok">在用</span></span>
                <span class="m3-faint"><span class="ob-dot" :data-tone="(FAST_PATH_STATES[x.state] || FAST_PATH_STATES.untested)[0]" aria-hidden="true"></span>
                  {{ (FAST_PATH_STATES[x.state] || FAST_PATH_STATES.untested)[1] }}<template v-if="x.rttMs"> · {{ msText(x.rttMs) }}</template></span>
                <small v-if="x.error" class="ob-bad m3-block">{{ x.error }}</small>
              </div>
              <small class="m3-faint m3-block">走快速通道 {{ u.streams }} 次 · 回落加密组网 {{ u.fallbacks }} 次<template v-if="u.lastUse"> · 最近使用 {{ relativeTime(u.lastUse, Date.now()) }}</template></small>
              <button type="button" class="ix-text-button" @click="emit('open', u.id)">打开 {{ nameOf(members, u.id) }} 的快速中转诊断</button>
            </div>
          </li>
        </ul></div>
      <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>

      <div class="m3-dialog-foot">
        <template v-if="creating"><span class="ob-grow" /><el-button @click="emit('close')">取消</el-button>
          <el-button type="primary" :disabled="!canAdd" @click="add">添加</el-button></template>
        <template v-else><el-button type="danger" plain @click="remove">删除快速端口</el-button><span class="ob-grow" />
          <el-button type="primary" @click="emit('close')">完成</el-button></template>
      </div>
    </div>
  </MeshDialog>
</template>
