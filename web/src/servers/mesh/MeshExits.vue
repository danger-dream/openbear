<script setup>
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { copyTextToClipboard } from '../../utils/clipboard.js';
import { flag } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { byAddress, errorMessage, errorsUnder, LINK_SCHEMES, nodeState, portTarget, proxyPorts, proxyRun, proxyUrl, rowButton, setExit, uid } from './meshHelpers.js';
import MeshDialog from './MeshDialog.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshPortEdit from './MeshPortEdit.vue';

// Exits and proxy ports: the machines that are exits, each a SOCKS5 / HTTP proxy on its mesh address to copy; exit groups,
// sets of exits by region or kind; and proxy ports, a port on one machine leading to an exit or a group through the tunnel.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}, group: {type: String, default: ''}, create: {type: Object, default: null}});
const emit = defineEmits(['open']);
const work = computed(() => mesh.work);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing).sort(byAddress));
const regionOf = id => String(members.value.find(m => m.serverId === id)?.region || props.items.find(i => i.id === id)?.region || '').toUpperCase();
const groups = computed(() => work.value?.exitGroups || []);
const exits = computed(() => members.value.filter(m => m.proxy?.exit));
const stateOf = id => nodeState(mesh.data, id);
const errorsOf = m => errorsUnder(mesh.errors, `members[${(work.value?.members || []).indexOf(m)}].proxy.exit`).map(errorMessage);
const groupsOf = id => groups.value.filter(g => g.members.includes(id));
async function copy(text, what) {
  try { await copyTextToClipboard(text); ElMessage.success(`已复制${what}`); } catch { ElMessage.error('复制失败'); }
}

// ---- exits ----
const scheme = ref('socks5');
const ready = m => Boolean(m.address && m.proxy.exit.password);
const exitLink = (m, masked = false) => proxyUrl(scheme.value, m.proxy.exit.user, m.proxy.exit.password, m.address, m.proxy.exit.port, {masked});
const copyExit = m => ready(m) ? copy(exitLink(m), ` ${m.name} 的出口`) : ElMessage.info('保存后生成密码，再复制');
const copyExits = () => copy(exits.value.filter(ready).map(m => exitLink(m)).join('\n'), ` ${exits.value.length} 个出口`);
function exitRun(m) {
  const x = mesh.data?.nodes?.[m.serverId]?.summary?.proxy?.exit;
  const e = proxyRun(mesh.data?.nodes?.[m.serverId]?.summary, 'exit');
  if (x?.error) return ['bad', '没有启动'];
  if (e?.errors?.length) return ['bad', '没有监听上'];
  if (!x) return ['off', '未应用'];
  return ['ok', x.active ? `${x.active} 个连接` : '运行中'];
}
// Adding exits: several at once, a region at a time.
const adding = ref(false);
const picks = ref([]);
const candidates = computed(() => members.value.filter(m => !m.proxy?.exit));
const regions = list => [...new Set(list.map(m => regionOf(m.serverId)).filter(Boolean))].sort();
function pickRegion(r) {
  const ids = candidates.value.filter(m => regionOf(m.serverId) === r).map(m => m.serverId);
  const all = ids.every(id => picks.value.includes(id));
  picks.value = all ? picks.value.filter(id => !ids.includes(id)) : [...new Set([...picks.value, ...ids])];
}
function togglePick(id) { picks.value = picks.value.includes(id) ? picks.value.filter(x => x !== id) : [...picks.value, id]; }
function openAdd() { picks.value = []; adding.value = true; }
function addExits() {
  for (const id of picks.value) { const m = work.value.members.find(x => x.serverId === id); if (m) setExit(work.value, m, true); }
  void save();
  adding.value = false;
}

// ---- exit groups ----
const editing = ref('');
watch(() => props.group, id => { if (id) editing.value = id; }, {immediate: true});
const g = computed(() => groups.value.find(x => x.id === editing.value) || null);
const gIndex = computed(() => groups.value.indexOf(g.value));
const groupErrors = i => errorsUnder(mesh.errors, `exitGroups[${i}]`).map(errorMessage);
const commit = () => void save();
function newGroup(region = '') {
  if (!exits.value.length) { ElMessage.info('先添加出口，再把它们编进组'); return; }
  const ids = region ? exits.value.filter(m => regionOf(m.serverId) === region).map(m => m.serverId) : [];
  const group = {id: uid('g'), name: region || '新出口组', members: ids};
  work.value.exitGroups = [...groups.value, group];
  commit();
  editing.value = group.id;
}
// A region whose exits no group holds exactly yet.
const freeRegions = computed(() => regions(exits.value).filter(r => {
  const ids = exits.value.filter(m => regionOf(m.serverId) === r).map(m => m.serverId).sort().join();
  return !groups.value.some(x => [...x.members].sort().join() === ids);
}));
function toggleMember(id) {
  const l = g.value.members;
  g.value.members = l.includes(id) ? l.filter(x => x !== id) : [...l, id];
  commit();
}
function moveMember(i) { const l = g.value.members; l.splice(i - 1, 0, l.splice(i, 1)[0]); commit(); }
const usedBy = gid => members.value.flatMap(m => proxyPorts(m).filter(p => p.group === gid).map(p => `${m.name} :${p.port}`));
function removeGroup() {
  const gid = g.value.id;
  work.value.exitGroups = groups.value.filter(x => x !== g.value);
  for (const m of members.value) for (const p of proxyPorts(m)) if (p.group === gid) { p.group = ''; p.pinned = ''; }
  commit();
  editing.value = '';
}

// ---- proxy ports ----
const ports = computed(() => members.value.flatMap(m => proxyPorts(m).map((p, i) => ({m, p, i}))));
const port = ref(null);
const portErrors = ({m, i}) => errorsUnder(mesh.errors, `members[${(work.value?.members || []).indexOf(m)}].proxy.ports[${i}]`).length;
const portWhere = p => p.listen === 'lan' && p.address ? `本机和局域网 ${p.address}` : '仅本机';
function portRun({m, p}) {
  const e = proxyRun(mesh.data?.nodes?.[m.serverId]?.summary, p.id);
  if (!e) return '未应用';
  if (e.errors?.length) return '没有监听上';
  const u = e.users?.[0], at = u?.picked || u?.exit;
  return at ? `在用 ${members.value.find(x => x.address === at)?.name || at}` : '';
}
const portLink = p => p.listen === 'lan' && p.address ? proxyUrl(scheme.value, p.user, p.password, p.address, p.port) : `${scheme.value}://127.0.0.1:${p.port}`;
const copyPort = ({m, p}) => p.listen === 'lan' && !p.password ? ElMessage.info('保存后生成密码，再复制') : copy(portLink(p), ` ${m.name} :${p.port} 的链接`);

const tunnelPort = computed(() => work.value?.settings?.proxy?.port || 51080);
function setTunnelPort(value) {
  const n = Number(value);
  if (!Number.isInteger(n)) return;
  work.value.settings.proxy = {...(work.value.settings.proxy || {password: ''}), port: n};
  commit();
}
const tunnelErrors = computed(() => errorsUnder(mesh.errors, 'settings.proxy').map(errorMessage));
watch(() => props.create, c => { if (c?.kind === 'exit') openAdd(); else if (c?.kind === 'group') newGroup(); else if (c?.kind === 'port') port.value = {create: true}; }, {immediate: true});
</script>
<template>
  <section class="m3-pane" aria-label="出口和代理">
    <header class="m3-pane-head"><h2>出口和代理</h2><span class="ob-grow" />
      <div class="sv-ranges" role="radiogroup" aria-label="链接格式"><button v-for="[k, l] in LINK_SCHEMES" :key="k" type="button" role="radio" :aria-checked="scheme === k" @click="scheme = k">{{ l }}</button></div>
    </header>
    <p class="m3-lead"><b>出口</b>是组网里一台对外的代理：复制它的链接，组网里的程序直接用，流量从那台机器出去。<b>出口组</b>按地区或用途（家宽、解锁）把出口编在一起。
      <b>代理端口</b>开在你程序所在的那台机器上，经提前建好的隧道走某个出口或出口组，新连接不用跨组网握手，延迟更低。</p>

    <h3 class="m3-sec-h">出口<small>每个出口在自己的组网地址上提供 SOCKS5 / HTTP，组网成员凭用户名和密码直接用</small><span class="ob-grow" />
      <el-button v-if="exits.length > 1" size="small" @click="copyExits">复制全部</el-button>
      <el-button size="small" type="primary" :disabled="!candidates.length" @click="openAdd">＋ 添加出口</el-button></h3>
    <ul class="m3-rows m3-rows-exits" :class="{'is-phone': phone}">
      <li v-if="!phone && exits.length" aria-hidden="true"><div class="m3-row m3-row-head"><span>出口</span><span>地址</span><span>用户名</span><span>出口组</span><span>状态</span><span></span></div></li>
      <li v-for="m in exits" :key="m.serverId">
        <div class="m3-row m3-row-click" :aria-label="`打开 ${m.name} 的出口设置`" v-bind="rowButton(() => emit('open', m.serverId, 'proxy'))">
          <span class="m3-row-name"><span class="ob-dot" :data-tone="stateOf(m.serverId)[0]" aria-hidden="true"></span><strong><MeshNodeName :id="m.serverId" plain /></strong>
            <span v-if="errorsOf(m).length" class="m3-tag" data-kind="bad">需修改</span></span>
          <span class="mono m3-row-addr">{{ m.address ? `${m.address}:${m.proxy.exit.port}` : '待分配' }}</span>
          <span class="mono m3-faint">{{ m.proxy.exit.user }}</span>
          <span class="m3-row-tags"><span v-for="x in groupsOf(m.serverId)" :key="x.id" class="m3-tag">{{ x.name }}</span></span>
          <span class="m3-faint"><span class="ob-dot" :data-tone="exitRun(m)[0]" aria-hidden="true"></span> {{ exitRun(m)[1] }}</span>
          <span><el-button size="small" :aria-label="`复制 ${m.name} 的出口链接`" @click.stop="copyExit(m)">复制</el-button></span>
        </div>
      </li>
      <li v-if="!exits.length" class="instance-muted">还没有出口：点「添加出口」选一台或几台节点。</li>
    </ul>

    <h3 class="m3-sec-h">出口组<small>按地区或用途编组，代理端口可以走一个组，组里几台互为备用或按延迟挑</small><span class="ob-grow" />
      <el-dropdown v-if="freeRegions.length" trigger="click" popper-class="m3-tokens" @command="r => newGroup(r)">
        <el-button size="small">按地区建组</el-button>
        <template #dropdown><el-dropdown-menu><el-dropdown-item v-for="r in freeRegions" :key="r" :command="r">
          <span v-if="flag(r)" class="sv-flag" aria-hidden="true">{{ flag(r) }}</span>{{ r }} · {{ exits.filter(m => regionOf(m.serverId) === r).length }} 台</el-dropdown-item></el-dropdown-menu></template>
      </el-dropdown>
      <el-button size="small" @click="newGroup()">＋ 新建出口组</el-button></h3>
    <ul class="m3-rows m3-rows-groups" :class="{'is-phone': phone}">
      <li v-for="(x, i) in groups" :key="x.id">
        <div class="m3-row m3-row-click" :aria-label="`打开出口组 ${x.name}`" v-bind="rowButton(() => { editing = x.id; })">
          <span class="m3-row-name"><b>{{ x.name }}</b><span v-if="groupErrors(i).length" class="m3-tag" data-kind="bad">需修改</span></span>
          <span class="m3-row-conn"><template v-for="(id, k) in x.members" :key="id"><span v-if="k" class="m3-faint">、</span><MeshNodeName :id="id" /></template>
            <span v-if="!x.members.length" class="m3-faint">还没有出口</span></span>
          <span class="m3-faint">{{ x.members.length }} 台</span>
        </div>
      </li>
      <li v-if="!groups.length" class="instance-muted">还没有出口组：可以按地区一键建，或新建后勾选出口。</li>
    </ul>

    <h3 class="m3-sec-h">代理端口<small>开在程序所在的机器上，经预先建好的隧道走出口，比直连出口少 2～3 个跨组网往返</small><span class="ob-grow" />
      <el-button size="small" :disabled="!exits.length" @click="port = {create: true}">＋ 新建代理端口</el-button></h3>
    <ul class="m3-rows m3-rows-ports" :class="{'is-phone': phone}">
      <li v-if="!phone && ports.length" aria-hidden="true"><div class="m3-row m3-row-head"><span>节点</span><span>端口</span><span>走</span><span>状态</span><span></span></div></li>
      <li v-for="x in ports" :key="`${x.m.serverId}/${x.p.id}`">
        <div class="m3-row m3-row-click" :aria-label="`打开 ${x.m.name} :${x.p.port} 的代理端口`" v-bind="rowButton(() => { port = {member: x.m.serverId, id: x.p.id}; })">
          <span class="m3-row-name"><span class="ob-dot" :data-tone="stateOf(x.m.serverId)[0]" aria-hidden="true"></span><strong><MeshNodeName :id="x.m.serverId" plain /></strong>
            <span v-if="portErrors(x)" class="m3-tag" data-kind="bad">需修改</span></span>
          <span class="m3-faint"><span class="mono">:{{ x.p.port }}</span> · {{ portWhere(x.p) }}</span>
          <span class="m3-row-conn">{{ portTarget(work, x.p) || '还没选' }}</span>
          <span class="m3-faint">{{ portRun(x) }}</span>
          <span><el-button size="small" :aria-label="`复制 ${x.m.name} :${x.p.port} 的链接`" @click.stop="copyPort(x)">复制</el-button></span>
        </div>
      </li>
      <li v-if="!ports.length" class="instance-muted">还没有代理端口。不开也能直接用出口；在意延迟时，在程序所在的机器上开一个。</li>
    </ul>

    <details class="m3-more">
      <summary>隧道端口 <span class="mono m3-faint">{{ tunnelPort }}</span></summary>
      <p class="instance-muted">代理端口和出口之间的隧道用这个端口，只在组网内可达，自动放行，一般不用改。</p>
      <input class="m3-input mono" type="number" min="1" max="65535" :value="tunnelPort" aria-label="隧道端口" style="max-width: 140px" @change="setTunnelPort($event.target.value)" />
      <small v-for="e in tunnelErrors" :key="e" class="ob-bad">{{ e }}</small>
    </details>

    <MeshDialog v-model="adding" title="添加出口" :phone="phone" width="560px">
      <div class="m3-form">
        <p class="instance-muted">选中的节点会在自己的组网地址上开 SOCKS5 / HTTP 出口（端口 1081），用户名取节点名，密码自动生成。</p>
        <div v-if="regions(candidates).length > 1" class="m3-chips" aria-label="按地区选">
          <button v-for="r in regions(candidates)" :key="r" type="button" class="m3-chip-btn" @click="pickRegion(r)"><span v-if="flag(r)" class="sv-flag" aria-hidden="true">{{ flag(r) }}</span>{{ r }}</button>
        </div>
        <div class="m3-picks">
          <label v-for="m in candidates" :key="m.serverId" class="m3-check"><input type="checkbox" :checked="picks.includes(m.serverId)" @change="togglePick(m.serverId)" />
            <span v-if="flag(regionOf(m.serverId))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(m.serverId)) }}</span>{{ m.name }}</label>
        </div>
        <div class="m3-dialog-foot"><span class="ob-grow" /><el-button @click="adding = false">取消</el-button>
          <el-button type="primary" :disabled="!picks.length" @click="addExits">添加 {{ picks.length || '' }} 个出口</el-button></div>
      </div>
    </MeshDialog>

    <MeshDialog :model-value="Boolean(g)" title="出口组" :phone="phone" width="560px" @update:model-value="v => { if (!v) editing = ''; }">
      <div v-if="g" class="m3-form">
        <label class="m3-field"><span>名称</span><input v-model.trim="g.name" class="m3-input" maxlength="40" aria-label="出口组名称" @change="commit" />
          <small class="instance-muted">比如 美国、家宽、解锁奈飞。</small></label>
        <div class="m3-field"><span>出口</span>
          <ol v-if="g.members.length" class="m3-items">
            <li v-for="(id, i) in g.members" :key="id">
              <span class="m3-no">{{ i + 1 }}</span>
              <span class="m3-items-main"><b><MeshNodeName :id="id" /></b></span>
              <button type="button" class="ix-icon-button" :disabled="!i" title="上移" aria-label="上移" @click="moveMember(i)">↑</button>
              <button type="button" class="ix-icon-button" title="移出这个组" aria-label="移出这个组" @click="toggleMember(id)">✕</button>
            </li>
          </ol>
          <small class="instance-muted">顺序是「按顺序」选用时的先后。勾选下面的出口加进来：</small>
          <div class="m3-picks">
            <label v-for="m in exits.filter(x => !g.members.includes(x.serverId))" :key="m.serverId" class="m3-check"><input type="checkbox" @change="toggleMember(m.serverId)" />
              <span v-if="flag(regionOf(m.serverId))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(m.serverId)) }}</span>{{ m.name }}</label>
          </div>
          <small v-if="exits.length === g.members.length" class="instance-muted">所有出口都在这个组里了。</small></div>
        <div v-if="usedBy(g.id).length" class="m3-field"><span>用到它的代理端口</span><span class="m3-faint">{{ usedBy(g.id).join('、') }}</span></div>
        <small v-for="e in groupErrors(gIndex)" :key="e" class="ob-bad">{{ e }}</small>
        <div class="m3-dialog-foot"><el-button type="danger" plain @click="removeGroup">删除出口组</el-button><span class="ob-grow" /><el-button type="primary" @click="editing = ''">完成</el-button></div>
      </div>
    </MeshDialog>

    <MeshPortEdit :at="port" :phone="phone" :region-of="regionOf" @close="port = null" />
  </section>
</template>
