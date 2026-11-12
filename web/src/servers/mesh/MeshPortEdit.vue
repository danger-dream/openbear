<script setup>
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { copyTextToClipboard } from '../../utils/clipboard.js';
import { noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { byAddress, errorMessage, errorsUnder, LINK_SCHEMES, msText, nameOf, newProxyPort, portTarget, proxyRun, proxyUrl, SELECTS, withProxy } from './meshHelpers.js';
import MeshDialog from './MeshDialog.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';

// A proxy port: a port on one node that leads to one exit or one exit group through the tunnel. at: {member, id} edits that
// port as it is (each change saved); {member?, exit?, group?, create: true} makes a new one, added on 添加.
const props = defineProps({at: {type: Object, default: null}, phone: Boolean, regionOf: {type: Function, default: () => ''}});
const emit = defineEmits(['close']);
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing).sort(byAddress));
const exits = computed(() => members.value.filter(m => m.proxy?.exit));
const groups = computed(() => work.value?.exitGroups || []);
const creating = computed(() => Boolean(props.at?.create));
const node = ref('');
const fresh = ref(null);
watch(() => props.at, at => {
  node.value = at?.member || '';
  const m = members.value.find(x => x.serverId === node.value);
  fresh.value = at?.create ? newProxyPort(m || {}, {exit: at.exit || '', group: at.group || ''}) : null;
}, {immediate: true});
const member = computed(() => members.value.find(m => m.serverId === node.value) || null);
const index = computed(() => (work.value?.members || []).indexOf(member.value));
const p = computed(() => creating.value ? fresh.value : (member.value?.proxy?.ports || []).find(x => x.id === props.at?.id) || null);
const pIndex = computed(() => (member.value?.proxy?.ports || []).indexOf(p.value));
const errors = computed(() => creating.value || pIndex.value < 0 ? [] : errorsUnder(mesh.errors, `members[${index.value}].proxy.ports[${pIndex.value}]`).map(errorMessage));
const commit = () => { if (!creating.value) void save(); };
watch(node, id => {
  // A new port on another node: the first port free there.
  if (!creating.value || !fresh.value) return;
  const m = members.value.find(x => x.serverId === id);
  fresh.value = {...fresh.value, port: newProxyPort(m || {}).port};
});
const kind = computed(() => p.value?.group ? 'group' : p.value?.exit || !groups.value.length ? 'exit' : (p.value?.kind || 'exit'));
const toKind = k => { if (k === kind.value) return; p.value.exit = ''; p.value.group = k === 'group' ? groups.value[0]?.id || '' : ''; p.value.pinned = ''; p.value.select = 'order';
  if (k === 'exit') p.value.exit = exits.value[0]?.serverId || ''; commit(); };
const group = computed(() => groups.value.find(g => g.id === p.value?.group) || null);
function setSelect(k) { p.value.select = k; p.value.pinned = k === 'manual' ? p.value.pinned || group.value?.members[0] || '' : ''; commit(); }
function setPort(value) { const n = Number(value); if (Number.isInteger(n)) { p.value.port = n; commit(); } }
function setListen(v) { p.value.listen = v; commit(); }

const nodeOptions = computed(() => members.value.map(m => ({value: m.serverId, label: m.name, region: props.regionOf(m.serverId)})));
const exitOptions = computed(() => exits.value.map(m => ({value: m.serverId, label: m.name, region: props.regionOf(m.serverId), note: m.proxy.exit.user})));
const canAdd = computed(() => Boolean(member.value && p.value && (p.value.exit || p.value.group)));
function add() {
  if (!canAdd.value) return;
  member.value.proxy = withProxy(member.value, 'ports', [...(member.value.proxy?.ports || []), {...fresh.value}]);
  void save();
  emit('close');
}
function remove() {
  member.value.proxy = withProxy(member.value, 'ports', (member.value.proxy?.ports || []).filter(x => x !== p.value));
  void save();
  emit('close');
}

// The link a program puts in: on loopback none of username and password.
const scheme = ref('socks5');
const lan = computed(() => p.value?.listen === 'lan' && p.value.address);
const linkOf = (masked = false) => lan.value ? proxyUrl(scheme.value, p.value.user, p.value.password, p.value.address, p.value.port, {masked})
  : `${scheme.value}://127.0.0.1:${p.value.port}`;
const shown = ref(false);
async function copy(text, what) {
  try { await copyTextToClipboard(text); ElMessage.success(`已复制${what}`); } catch { ElMessage.error('复制失败'); }
}
const run = computed(() => creating.value || !p.value ? null : proxyRun(mesh.data?.nodes?.[node.value]?.summary, p.value.id));
const now = computed(() => {
  const u = run.value?.users?.[0];
  if (!u) return '';
  const at = u.picked || u.exit;
  const x = at && members.value.find(m => m.address === at);
  const rtt = (u.exits || []).find(e => e.address === at)?.rttMs;
  return [x ? `现在走 ${x.name}` : '', Number.isFinite(rtt) ? `到出口 ${msText(rtt)}` : '', `当前 ${u.active} 个连接`].filter(Boolean).join(' · ');
});
</script>
<template>
  <MeshDialog :model-value="Boolean(at && p)" :title="creating ? '新建代理端口' : `代理端口 :${p?.port || ''}`" :phone="phone" width="600px"
    @update:model-value="v => { if (!v) emit('close'); }">
    <div v-if="p" class="m3-form">
      <div v-if="creating" class="m3-field"><span>开在哪台节点</span>
        <MeshNodeSelect v-model="node" :options="nodeOptions" aria-label="开在哪台节点" width="100%" />
        <small class="instance-muted">一般选你的程序所在的机器，比如家里的软路由或这台主控。</small></div>
      <div v-else class="m3-field"><span>节点</span><b>{{ member?.name }}</b></div>

      <div class="m3-field"><span>走哪里</span>
        <div class="m3-seg" role="radiogroup" aria-label="走一个出口还是一个出口组">
          <button type="button" role="radio" :aria-checked="kind === 'exit'" @click="toKind('exit')">一个出口</button>
          <button type="button" role="radio" :aria-checked="kind === 'group'" :disabled="!groups.length" @click="toKind('group')">一个出口组</button>
        </div>
        <MeshNodeSelect v-if="kind === 'exit'" v-model="p.exit" :options="exitOptions" placeholder="选一个出口…" aria-label="走哪个出口" width="100%" @change="commit" />
        <template v-else>
          <select class="m3-sel" :value="p.group" aria-label="走哪个出口组" @change="p.group = $event.target.value; p.pinned = ''; commit()">
            <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}（{{ g.members.length }} 台）</option>
          </select>
          <div class="m3-radio" role="radiogroup" aria-label="组里用哪台">
            <label v-for="[k, l, d] in SELECTS" :key="k" :data-on="p.select === k || undefined"><input type="radio" name="m3-port-select" :checked="p.select === k" @change="setSelect(k)" />
              <span><span class="m3-radio-label">{{ l }}</span><small class="instance-muted">{{ d }}</small></span></label>
          </div>
          <MeshNodeSelect v-if="p.select === 'manual' && group" v-model="p.pinned" :options="[...group.members].sort((x, y) => byAddress(members.find(m => m.serverId === x) || {}, members.find(m => m.serverId === y) || {})).map(id => ({value: id, label: nameOf(members, id), region: regionOf(id)}))"
            aria-label="指定哪台" width="100%" @change="commit" />
        </template>
        <small v-if="!exits.length" class="instance-muted">还没有出口：先在上面「出口」里添加。</small></div>

      <div class="m3-field"><span>端口</span>
        <input v-bind="noAutofill" class="m3-input mono" type="number" min="1" max="65535" :value="p.port" aria-label="端口" style="max-width: 140px" @change="setPort($event.target.value)" />
        <small class="instance-muted">SOCKS5 和 HTTP 共用这一个端口。</small></div>

      <div class="m3-field"><span>谁能连</span>
        <div class="m3-seg" role="radiogroup" aria-label="谁能连">
          <button type="button" role="radio" :aria-checked="p.listen !== 'lan'" @click="setListen('local')">仅本机</button>
          <button type="button" role="radio" :aria-checked="p.listen === 'lan'" @click="setListen('lan')">本机和局域网</button>
        </div>
        <input v-if="p.listen === 'lan'" v-bind="noAutofill" class="m3-input mono" :value="p.address" placeholder="这台的局域网地址，例如 192.168.1.10" aria-label="局域网地址"
          @change="p.address = $event.target.value.trim(); commit()" />
        <small class="instance-muted">{{ p.listen === 'lan' ? '局域网里的设备要填用户名和密码；节点防火墙要放行这个端口。' : '只有这台机器上的程序能用，不用填用户名和密码。' }}</small></div>

      <div v-if="p.listen === 'lan'" class="m3-field"><span>用户名和密码</span>
        <input v-bind="noAutofill" class="m3-input mono" maxlength="32" :value="p.user" aria-label="用户名" style="max-width: 200px" @change="p.user = $event.target.value.trim().toLowerCase(); commit()" />
        <span class="m3-faint mono">{{ p.password ? (shown ? p.password : '••••••••') : (creating ? '添加后自动生成' : '保存后自动生成') }}
          <template v-if="p.password"><button type="button" class="ix-text-button" @click="shown = !shown">{{ shown ? '隐藏' : '显示' }}</button>
            <button type="button" class="ix-text-button" @click="copy(p.password, '密码')">复制</button></template>
          <button v-if="!creating && p.password" type="button" class="ix-text-button" @click="p.password = ''; commit()">重新生成</button></span></div>

      <div class="m3-field"><span>应答时机</span>
        <label class="m3-check"><input type="checkbox" :checked="p.strict" @change="p.strict = $event.target.checked; commit()" />等出口连上目标再应答</label>
        <small class="instance-muted">默认先应答、再连目标，新连接快一个来回；打开后目标连不上时程序能看到具体错误。</small></div>

      <div v-if="!creating" class="m3-field"><span>链接</span>
        <div class="sv-ranges" role="radiogroup" aria-label="链接格式"><button v-for="[k, l] in LINK_SCHEMES" :key="k" type="button" role="radio" :aria-checked="scheme === k" @click="scheme = k">{{ l }}</button></div>
        <span class="mono m3-links-url">{{ linkOf(true) }}<button type="button" class="ix-text-button" @click="copy(linkOf(), '链接')">复制</button></span></div>
      <div v-if="run" class="m3-field"><span>运行情况</span>
        <span class="m3-faint">{{ now || `监听 ${(run.listen || []).join('、') || '无'}` }}</span>
        <small v-for="e in run.errors" :key="e" class="ob-bad">没有监听上：{{ e }}</small></div>
      <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>

      <div class="m3-dialog-foot">
        <template v-if="creating"><span class="ob-grow" /><el-button @click="emit('close')">取消</el-button>
          <el-button type="primary" :disabled="!canAdd" @click="add">添加</el-button></template>
        <template v-else><el-button type="danger" plain @click="remove">删除代理端口</el-button><span class="ob-grow" />
          <span class="m3-faint">{{ portTarget(work, p) }}</span><el-button type="primary" @click="emit('close')">完成</el-button></template>
      </div>
    </div>
  </MeshDialog>
</template>
