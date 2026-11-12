<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, provide, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { MOBILE_VIEWPORT_QUERY } from '../../mobileViewport.js';
import { MeshApi } from './meshApi.js';
import { act, folds, install, mesh, refresh, save, unsaved } from './meshStore.js';
import { isRelay, MESH_ITEMS, MESH_OPEN_NODE, nameOf, uid } from './meshHelpers.js';
import { addLink, planLink } from './meshGraph.js';
import { removeEdge } from './meshEdit.js';
import { MESH_VISIBLE, usePoll } from './usePoll.js';
import MeshBoard from './MeshBoard.vue';
import MeshAccess from './MeshAccess.vue';
import MeshChanges from './MeshChanges.vue';
import MeshDialog from './MeshDialog.vue';
import MeshExits from './MeshExits.vue';
import MeshJoin from './MeshJoin.vue';
import MeshLans from './MeshLans.vue';
import MeshLines from './MeshLines.vue';
import MeshLink from './MeshLink.vue';
import MeshNodePage from './MeshNodePage.vue';
import MeshMachines from './MeshMachines.vue';
import MeshOverview from './MeshOverview.vue';
import MeshRevisions from './MeshRevisions.vue';
import MeshRule from './MeshRule.vue';
import MeshSettings from './MeshSettings.vue';
import './mesh.css';
import './mesh3.css';

// The mesh tab: loading and polling, the pages (overview board, machines, LANs, exits, access rules,
// lines, settings, revisions), the draft bar above them and the editors every page opens.
const props = defineProps({active: Boolean, items: {type: Array, default: () => []}, focus: {type: Object, default: null}});
const emit = defineEmits(['open-server']);
const phone = ref(false), shown = ref(true);
let media;
// The mesh is on screen: its tab is shown and the servers page is not put away. Every reading kept
// fresh under it (the panels, the board's paths and punched ends) stops while it is not, and catches up at once.
const visible = computed(() => props.active && shown.value);
provide(MESH_VISIBLE, visible);
provide('meshActive', computed(() => props.active));
provide(MESH_OPEN_NODE, id => openMachine(id));
provide(MESH_ITEMS, computed(() => props.items));

const data = computed(() => mesh.data);
const work = computed(() => mesh.work);
// A save or an action that failed for something other than the settings (act's "N 处设置需要修改": MeshChanges lists those).
// The classic board shows mesh.error itself.
const actionError = computed(() => mesh.error && !/^\d+ 处设置需要修改$/.test(mesh.error) ? mesh.error : '');
const fast = computed(() => Boolean(data.value?.apply?.running) || mesh.installing || Boolean(data.value?.installing?.length));
// The first reading once the mesh is on screen again (opened, or its browser tab back) asks the nodes
// for their interfaces and routes; the polls in between go by what they reported last.
let fresh = true;
const now = ref(Date.now());
// Ticks only while the page is shown: what it counts down is on screen alone.
let clock = null;
watch(visible, on => {
  clearInterval(clock);
  clock = on ? setInterval(() => { now.value = Date.now(); }, 1000) : null;
  if (on) now.value = Date.now();
}, {immediate: true});
onBeforeUnmount(() => { clearInterval(clock); clock = null; });
const nextIn = computed(() => poll.next.value ? Math.max(0, Math.ceil((poll.next.value - now.value) / 1000)) : 0);
const poll = usePoll(() => { const once = fresh; fresh = false; return refresh(once); }, {interval: () => fast.value ? 1000 : 5000, active: visible});
watch(visible, now => { if (!now) fresh = true; });
const tick = () => poll.run();

async function applyNow() {
  const result = await act(() => MeshApi.apply());
  if (result) poll.schedule();
}
function visibility() { if (document.hidden) fresh = true; else void tick(); }
watch(fast, () => poll.schedule());
watch(() => data.value?.apply?.running, (now, before) => {
  if (!before || now) return;
  const progress = data.value?.apply?.progress || [], failed = progress.filter(p => p.state === 'failed').length;
  const missing = progress.filter(p => p.state === 'missing').length;
  if (failed) ElMessage.warning(`${failed} 台没有应用成功，原因显示在节点列表和详情里`);
  else if (missing) ElMessage.warning(`已应用到 ${progress.filter(p => p.state === 'done').length} 台；${missing} 台的组网服务不见了，已跳过`);
  else ElMessage.success(`已应用到 ${progress.filter(p => p.state === 'done').length} 台`);
});

// ---- pages ----
const TAB_KEY = 'openbear.mesh.tab';
const TABS = [['overview', '概览'], ['machines', '节点'], ['lans', '共享局域网'], ['exits', '出口和代理'], ['rules', '访问规则'], ['lines', '线路'], ['settings', '设置'], ['revisions', '版本记录']];
const tab = ref((() => { try { const t = localStorage.getItem(TAB_KEY); return TABS.some(([k]) => k === t) ? t : 'overview'; } catch { return 'overview'; } })());
watch(tab, t => { try { localStorage.setItem(TAB_KEY, t); } catch { /* private mode */ } });
const machine = ref(''), machineSec = ref(''), exitGroup = ref(''), shownPath = ref('');
// The overview, or the board from before it (dragging links, rule arrows), remembered on this browser.
const CLASSIC_KEY = 'openbear.mesh.classic';
const classic = ref((() => { try { return localStorage.getItem(CLASSIC_KEY) === '1'; } catch { return false; } })());
watch(classic, on => { try { localStorage.setItem(CLASSIC_KEY, on ? '1' : '0'); } catch { /* private mode */ } });
const board = computed(() => tab.value === 'overview' && classic.value);
const members = computed(() => (work.value?.members || []).filter(m => !m.removing));
const counts = computed(() => ({machines: members.value.length, lans: members.value.filter(m => m.gateway?.subnets?.length && !m.gateway.backup).length,
  exits: (work.value?.members || []).filter(m => !m.removing && m.proxy?.exit).length, rules: (work.value?.rules || []).length, lines: lineCount.value}));
// Relay links (every pair of relays when they are all linked) and direct links.
const lineCount = computed(() => {
  const links = work.value?.relayLinks, relays = members.value.filter(m => m.role === 'relay').length;
  return (links?.mode === 'full' ? relays * (relays - 1) / 2 : (links?.pairs || []).length) + (work.value?.directLinks || []).length;
});
const summary = computed(() => [`${members.value.length} 个节点`, `${members.value.filter(m => m.role === 'relay').length} 台中转`, `${counts.value.lans} 个共享局域网`,
  data.value?.applied?.revision ? `当前第 ${data.value.applied.revision} 版` : '还没有应用过'].join(' · '));
// 我想…: each task opens the page and form that does it.
const TASKS = [['join', '加入服务器', '选服务器、角色、接入哪台中转、用什么协议和端口'], ['lan', '共享一个局域网', '选网关、勾网段、决定谁能访问'],
  ['exit', '添加出口', '让程序从某台节点出去，拿到可以直接复制的 SOCKS5 链接'], ['group', '新建出口组', '按地区或用途（家宽、解锁）把出口编在一起'],
  ['rule', '让 A 访问 B', '写一条访问规则'],
  ['relay', '新建中转线路', '把两台中转连起来'], ['direct', '新建直连', '让两台成员不经中转直接通']];
const create = ref(null);
function want(kind) {
  if (kind === 'join') { joinOpen.value = true; return; }
  if (kind === 'rule') {
    const rule = {id: uid('r'), src: {all: false, members: [], tags: [], prefixes: []}, dst: {all: false, members: [], tags: [], subnets: []}, proto: 'any', ports: [], note: ''};
    work.value.rules = [...(work.value.rules || []), rule];
    void save();
    go('rules');
    edit({kind: 'rule', id: rule.id});
    return;
  }
  go(kind === 'lan' ? 'lans' : kind === 'group' || kind === 'exit' ? 'exits' : 'lines');
  create.value = {kind};
  void nextTick(() => nextTick(() => { create.value = null; }));
}
// A route seen somewhere, as an own route: the editor on its source's page, laid out as it goes.
const routeSeed = ref(null);
function pinPath(seed) {
  if (!seed?.src || !seed?.dst || !Array.isArray(seed.via) || !seed.via.length) return;
  openMachine(seed.src, 'overview');
  routeSeed.value = {src: seed.src, dst: seed.dst, via: [...seed.via], nonce: Date.now()};
}
const overview = ref(null);
function go(key) { tab.value = key; if (key !== 'machines') machine.value = ''; if (key === 'rules') folds.rules = true; }
function openMachine(id, sec = '') { if (!id) return; tab.value = 'machines'; machine.value = id; machineSec.value = sec; }
function showOnBoard(id) { tab.value = 'overview'; machine.value = ''; classic.value = false; void nextTick(() => overview.value?.show(id)); }
// A route from a machine's page (its routes, or 查看到…的路径): on the board.
function showPath(p) { shownPath.value = p?.key || ''; tab.value = 'overview'; classic.value = false; void nextTick(() => overview.value?.pathBetween(p.src, p.dst)); }
// From a server's card: its page here, or (on the classic board) the board selects it itself.
watch(() => props.focus?.nonce, () => { if (props.focus?.serverId && !board.value) openMachine(props.focus.serverId); }, {immediate: true});

// ---- editors over the page: a link or an access rule ----
const editor = ref(null);
const editorTitle = computed(() => {
  const e = editor.value;
  if (!e) return '';
  if (e.kind === 'rule') return `访问规则 ${(work.value?.rules || []).findIndex(r => r.id === e.id) + 1}`;
  const kind = String(e.id).split(':')[0];
  return kind === 'entry' ? '接入' : kind === 'direct' ? '直连' : '中转线路';
});
function edit(sel) {
  if (!sel) return;
  if (sel.kind === 'node') openMachine(sel.id);
  else if (sel.kind === 'group') { go('exits'); exitGroup.value = ''; void nextTick(() => { exitGroup.value = sel.id; }); }
  else editor.value = {kind: sel.kind, id: sel.id};
}
function connect(c) {
  if (typeof c === 'string') { tab.value = 'overview'; machine.value = ''; classic.value = false; void nextTick(() => overview.value?.linkFrom(c)); return; }
  const plan = planLink(work.value, c.from, c.to);
  if (plan.error) { ElMessage.warning(plan.error); return; }
  const next = addLink(work.value, plan);
  if (plan.kind === 'relay') work.value.relayLinks.pairs.at(-1).auto = ['transport', 'dialer', 'bias'];
  if (plan.kind === 'entry') { const m = work.value.members.find(x => x.serverId === plan.member); m.entry.candidates.at(-1).auto = ['transport']; }
  void save();
  edit(next);
}
function removeEditing() {
  const e = editor.value;
  if (e?.kind === 'edge') removeEdge(work.value, e.id);
  else if (e?.kind === 'rule') { const i = work.value.rules.findIndex(r => r.id === e.id); if (i >= 0) work.value.rules.splice(i, 1); }
  void save();
  editor.value = null;
}

// ---- joining ----
const joinOpen = ref(false), joining = ref(null), joined = ref('');
async function join(choice) {
  joining.value = {name: choice.name, serverId: choice.serverId};
  const result = await install([choice.serverId]);
  joining.value = null;
  const item = result?.items?.find(i => i.serverId === choice.serverId);
  if (!result || !item?.ok) { ElMessage.error(`没有加入 ${choice.name}：${item?.reason || item?.error || mesh.error || '安装组网服务失败'}`); return; }
  const m = work.value?.members?.find(x => x.serverId === choice.serverId);
  if (m) {
    if (choice.role === 'relay') { m.role = 'relay'; delete m.entry; }
    else if (choice.cands.length) {
      m.entry = {mode: 'upstreams', autoReturn: true,
        candidates: choice.cands.map(c => ({id: uid('c'), relay: c.relay, transport: c.transport, ...(c.auto ? {auto: ['transport']} : {})}))};
    }
    // In auto mode the links picked here stay (bias set, so not every field follows the mesh); the protocol and who dials do follow it.
    const auto = work.value.relayLinks.mode === 'auto' ? ['transport', 'dialer'] : ['transport', 'dialer', 'bias'];
    for (const l of choice.links) work.value.relayLinks.pairs.push({a: m.serverId, b: l.relay, transport: 'udp', cost: 1, dialer: 'a', bias: 0, auto});
    if (choice.address) {
      m.publicEndpoint = choice.address.publicEndpoint;
      if (choice.address.announce === false) m.announce = false;
      if (choice.address.publicPorts) m.publicPorts = choice.address.publicPorts;
    }
    if (choice.tags.length) m.tags = choice.tags;
    await save();
  }
  joined.value = choice.serverId;
  go('machines');
}
const joinedMember = computed(() => members.value.find(m => m.serverId === joined.value) || null);
const joinedText = computed(() => {
  const m = joinedMember.value;
  if (!m) return '';
  const relay = isRelay(m) ? '' : m.entry?.candidates?.[0]?.relay;
  return `${m.name} 已加入草稿，排在下面列表第一行；画板上${relay ? `在 ${nameOf(members.value, relay)} 的成员里` : '在它那个地区的组里'}，标着“待应用”。它还没连进组网，确认设置后点“应用到网格”。`;
});

function syncPhone() { phone.value = media.matches; }
onMounted(() => {
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener?.('change', syncPhone);
  document.addEventListener('visibilitychange', visibility);
  void tick();
});
onActivated(() => { shown.value = true; });
onDeactivated(() => { shown.value = false; });
onBeforeUnmount(() => { media?.removeEventListener?.('change', syncPhone); document.removeEventListener('visibilitychange', visibility); });
</script>
<template>
  <div class="mesh mesh3" :class="{'is-phone': phone}">
    <p v-if="!data && mesh.loadError" role="alert" class="instance-error mesh-state">读取组网失败：{{ mesh.loadError }} <button type="button" class="instance-link" @click="tick">重试</button></p>
    <p v-else-if="mesh.unavailable" class="instance-muted mesh-state">主控还不支持组网，更新 OpenBear 后再试。</p>
    <p v-else-if="!data || !work" class="instance-muted mesh-state" role="status">正在读取…</p>
    <template v-else>
      <div class="m3-top">
        <h2>组网</h2><span class="m3-faint m3-top-sum">{{ summary }}</span><span class="ob-grow" />
        <span class="m3-faint m3-live" :title="`每 ${fast ? 1 : 5} 秒读一次组网状态；延迟和在线状态以节点最近一次上报为准`"><i aria-hidden="true"></i>{{ nextIn > 0 ? `${nextIn} 秒后更新` : '正在更新…' }}</span>
        <span v-if="!data.draft?.dirty" class="m3-faint">没有未应用的改动</span>
        <el-dropdown trigger="click" popper-class="m3-tokens" @command="want">
          <el-button size="small">我想…</el-button>
          <template #dropdown><el-dropdown-menu class="m3-tasks">
            <el-dropdown-item v-for="[k, l, d] in TASKS" :key="k" :command="k"><span class="m3-task"><b>{{ l }}</b><small>{{ d }}</small></span></el-dropdown-item>
          </el-dropdown-menu></template>
        </el-dropdown>
      </div>
      <header class="m3-head">
        <nav class="m3-tabs" role="tablist" aria-label="组网">
          <button v-for="[k, l] in TABS" :key="k" type="button" role="tab" :aria-selected="tab === k" @click="go(k)">{{ l }}<small v-if="counts[k]">{{ counts[k] }}</small></button>
        </nav>
        <div v-if="tab === 'overview' && !phone" class="sv-ranges m3-head-state" role="radiogroup" aria-label="概览的样子">
          <button type="button" role="radio" :aria-checked="!classic" @click="classic = false">概览</button>
          <button type="button" role="radio" :aria-checked="classic" title="原来的画板：拖线连接、访问规则箭头" @click="classic = true">经典画板</button>
        </div>
      </header>
      <MeshChanges v-if="!board" @apply="applyNow" @member="openMachine" />
      <p v-if="!board && actionError" role="alert" class="m3-joined m3-failed"><span>{{ actionError }}</span>
        <el-button v-if="unsaved() && !mesh.saving" size="small" @click="save">重新保存</el-button>
        <button type="button" class="ix-icon-button" aria-label="关闭提示" @click="mesh.error = ''">✕</button></p>
      <p v-if="joining" class="m3-joined" role="status"><span>正在往 <b>{{ joining.name }}</b> 上安装组网服务（不影响它现有的网络，最多约 3 分钟），装好后加入草稿…</span></p>
      <p v-else-if="joinedMember" class="m3-joined" role="status"><span>{{ joinedText }}</span>
        <el-button size="small" @click="openMachine(joined)">查看和修改它的设置</el-button>
        <el-button size="small" @click="showOnBoard(joined)">在画板上看</el-button>
        <el-button size="small" type="primary" :disabled="Boolean(mesh.errors.length)" @click="applyNow(); joined = ''">应用到网格</el-button>
        <button type="button" class="ix-icon-button" aria-label="关闭提示" @click="joined = ''">✕</button></p>

      <main class="m3-page" :class="board ? 'is-classic' : tab === 'overview' ? 'is-board' : tab === 'machines' && machine ? 'is-detail' : 'is-scroll'">
        <MeshBoard v-if="board" :key="phone ? 'phone' : 'desktop'" :phone="phone" :active="active" :items="items" :focus="focus" @apply="applyNow" @open-server="emit('open-server', $event)" @pin-path="pinPath" />
        <MeshOverview v-else-if="tab === 'overview'" ref="overview" :phone="phone" :active="visible" :items="items" @open-machine="openMachine" @join="joinOpen = true" @connect="connect" @pin-path="pinPath" />
        <template v-else-if="tab === 'machines'">
          <MeshNodePage v-if="machine" :id="machine" :section="machineSec" :phone="phone" :items="items" :shown-path="shownPath" :route-seed="routeSeed?.src === machine ? routeSeed : null" @back="machine = ''" @open="openMachine" @board="showOnBoard" @edit="edit"
            @apply="applyNow" @open-server="emit('open-server', $event)" @show-path="showPath" @pin-path="pinPath" @lines="go('lines')" @rules="go('rules')" />
          <MeshMachines v-else :phone="phone" :items="items" :joined="joined" @open="openMachine" @join="joinOpen = true" />
        </template>
        <MeshLans v-else-if="tab === 'lans'" :phone="phone" :items="items" :create="create" @open="openMachine" @rules="go('rules')" />
        <MeshExits v-else-if="tab === 'exits'" :phone="phone" :items="items" :group="exitGroup" :create="create" @open="openMachine" />
        <MeshAccess v-else-if="tab === 'rules'" :phone="phone" :items="items" @edit="id => edit({kind: 'rule', id})" @new="want('rule')" />
        <MeshLines v-else-if="tab === 'lines'" :phone="phone" :items="items" :create="create" @edit="edit" @open="openMachine" />
        <section v-else-if="tab === 'settings'" class="m3-pane" aria-label="设置">
          <header class="m3-pane-head"><h2>设置</h2></header>
          <p class="m3-lead">全网参数。一般只在第一次建网时看一眼；每一项都写着默认值，改完点「保存到草稿」。</p>
          <MeshSettings page :items="items" />
        </section>
        <MeshRevisions v-else-if="tab === 'revisions'" :phone="phone" @applied="poll.schedule()" />
      </main>

      <MeshJoin v-model="joinOpen" :phone="phone" :items="items" @join="join" />
      <MeshDialog :model-value="Boolean(editor)" :title="editorTitle" :phone="phone" width="600px" @update:model-value="v => { if (!v) editor = null; }">
        <MeshLink v-if="editor?.kind === 'edge'" :key="editor.id" :edge-id="editor.id" @remove="removeEditing" @select="edit" />
        <MeshRule v-else-if="editor?.kind === 'rule'" :key="editor.id" :rule-id="editor.id" @remove="removeEditing" @back="editor = null" />
      </MeshDialog>
    </template>
  </div>
</template>
