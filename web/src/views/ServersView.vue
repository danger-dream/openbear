<script setup>
import { computed, defineAsyncComponent, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Close } from '@element-plus/icons-vue';
import { Api } from '../api.js';
import AdminPageHeader from '../components/AdminPageHeader.vue';
import ServerForm from '../instances/ServerForm.vue';
import { ok } from '../instances/instanceHelpers.js';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import AddServer from '../servers/AddServer.vue';
import BatchExec from '../servers/BatchExec.vue';
import FileBrowser from '../servers/FileBrowser.vue';
import ProbeBoard from '../servers/ProbeBoard.vue';
import ServerDetail from '../servers/ServerDetail.vue';
import ServerEditor from '../servers/ServerEditor.vue';
import ServerGuide from '../servers/ServerGuide.vue';
import ServerOrder from '../servers/ServerOrder.vue';
import ServerSettings from '../servers/ServerSettings.vue';
import DdnsSheet from '../ddns/DdnsSheet.vue';
import BgpSection from '../servers/detail/BgpSection.vue';
import { loadRates, storedRates } from '../servers/fxRates.js';
import { ensureFlagFont, flag, platformOf, safeError, setServerZone } from '../servers/serverHelpers.js';
import { ddnsRequest } from '../ddns/ddnsLink.js';
import '../instances/instances.css';
import '../servers/servers.css';

// xterm is only loaded once the terminal tab is used.
const TerminalWorkbench = defineAsyncComponent(() => import('../servers/TerminalWorkbench.vue'));
const MeshView = defineAsyncComponent(() => import('../servers/mesh/MeshView.vue'));
const DdnsView = defineAsyncComponent(() => import('../ddns/DdnsView.vue'));
const BenchView = defineAsyncComponent(() => import('../servers/bench/BenchView.vue'));
const STORAGE = 'openbear.servers.tabs';
// Live samples kept per server for the card sparklines (about two minutes).
const SPARK = 40;
const emit = defineEmits(['mobile-header-ready']);
const items = ref([]), settings = ref({accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}), build = ref({available: false, version: ''}), fx = ref(storedRates());
const loading = ref(false), error = ref(''), now = ref(Date.now());
const saved = restore();
const tabs = ref(saved.tabs), active = ref(saved.active), terminalMounted = ref(saved.active === 'terminal');
const terminalRequest = ref(null), terminalCount = ref(0);
const meshMounted = ref(saved.active === 'mesh'), meshFocus = ref(null);
const ddnsMounted = ref(saved.active === 'ddns'), ddnsFocus = ref(null);
const benchMounted = ref(saved.active === 'bench');
const dialog = ref(''), dialogRef = ref(null), editing = ref(null), editPart = ref(''), sshTarget = ref(null);
const spark = ref({}), batchTargets = ref([]), filesFor = ref(''), phone = ref(false), guide = ref(false);
// Each server's last half hour of latency for the board, read each minute apart from the live samples.
const pings = ref({});
// The page is on screen (KeepAlive puts it away while another one is shown).
const visible = ref(true);
let alive = true, livePoll, listPoll, pingPoll, clock, media;

function restore() {
  try {
    const value = JSON.parse(sessionStorage.getItem(STORAGE) || '{}');
    return {tabs: Array.isArray(value.tabs) ? value.tabs.filter(t => typeof t === 'string') : [], active: typeof value.active === 'string' ? value.active : 'probe'};
  } catch { return {tabs: [], active: 'probe'}; }
}
watch([tabs, active], () => {
  try { sessionStorage.setItem(STORAGE, JSON.stringify({tabs: tabs.value, active: active.value})); } catch { /* storage may be blocked */ }
}, {deep: true});

const itemOf = id => items.value.find(i => i.id === id);
const groups = computed(() => [...new Set(items.value.map(i => i.profile.group).filter(Boolean))]);
const tags = computed(() => [...new Set(items.value.flatMap(i => i.profile.tags || []))]);
const regions = computed(() => [...new Set(items.value.map(i => i.region).filter(Boolean))]);
const tabTone = item => !item?.node ? 'off' : item.live ? 'ok' : 'bad';
const sshServer = computed(() => {
  const s = sshTarget.value;
  if (!s) return null;
  return s.ssh ? {id: s.id, name: s.name, ...s.ssh} : {id: s.id, name: s.name, host: s.node?.info?.ipv4 || '', port: 22, username: 'root', auth: 'key'};
});

async function load(silent = false) {
  if (!silent) loading.value = true;
  try {
    const data = ok(await Api.servers());
    if (!alive) return;
    const lives = new Map(items.value.map(i => [i.id, i.live]));
    items.value = (data.items || []).map(i => ({...i, live: i.live || lives.get(i.id) || null}));
    settings.value = data.settings;
    setServerZone(data.settings?.timezone);
    build.value = data.node;
    error.value = '';
    tabs.value = tabs.value.filter(itemOf);
    if (!['probe', 'terminal', 'mesh', 'ddns', 'bench'].includes(active.value) && !tabs.value.includes(active.value)) active.value = 'probe';
  } catch (e) { if (alive) error.value = safeError(e); }
  finally { if (alive) loading.value = false; }
}
async function refreshLive() {
  try {
    const data = ok(await Api.serversLive());
    for (const item of items.value) {
      const value = data.items[item.id] || null;
      if (item.node) {
        if (!value && item.live) item.node.lastSeenAtMs = Date.now();
        item.node.online = Boolean(value);
      }
      // No new sample since the last round: the cards keep what they have and are not redrawn.
      if (value && item.live && value.at === item.live.at && value.connectedAtMs === item.live.connectedAtMs) continue;
      item.live = value;
      if (value?.m) {
        const line = spark.value[item.id] || (spark.value[item.id] = {cpu: [], rx: [], tx: []});
        line.cpu.push(value.m.cpu); line.rx.push(value.m.net.rxRate); line.tx.push(value.m.net.txRate);
        for (const key of ['cpu', 'rx', 'tx']) if (line[key].length > SPARK) line[key].shift();
      }
    }
  } catch { /* the next round retries; cards keep their last values */ }
}

async function refreshPings() {
  if (!settings.value.latency?.enabled) { pings.value = {}; return; }
  try { pings.value = ok(await Api.serversLatency()).items || {}; } catch { /* the next minute retries */ }
}
watch(() => settings.value.latency?.enabled, () => { void refreshPings(); });

function openServer(item) {
  if (!tabs.value.includes(item.id)) tabs.value.push(item.id);
  active.value = item.id;
}
function closeTab(id) {
  const index = tabs.value.indexOf(id);
  if (index < 0) return;
  tabs.value.splice(index, 1);
  if (active.value === id) active.value = tabs.value[Math.min(index, tabs.value.length - 1)] || 'probe';
}
function showTerminal() { terminalMounted.value = true; active.value = 'terminal'; }
function openTerminal(item) { showTerminal(); terminalRequest.value = {serverId: item.id, kind: 'terminal', nonce: Date.now()}; }
function openDesktop(item) { showTerminal(); terminalRequest.value = {serverId: item.id, kind: 'desktop', nonce: Date.now()}; }
function showMesh(serverId = '') {
  meshMounted.value = true; active.value = 'mesh';
  if (serverId) meshFocus.value = {serverId, nonce: Date.now()};
}
function showDdns(focus = null) {
  ddnsMounted.value = true; active.value = 'ddns';
  if (focus) ddnsFocus.value = focus;
}
function showBench() { benchMounted.value = true; active.value = 'bench'; }
// A link to 动态域名 (a server's card, a reminder, /ddns) asked for before or while the page is up.
watch(ddnsRequest, request => { if (request) showDdns(request); }, {immediate: true});
// part: the editor's part to show first (traffic).
function open(name, value = null, part = '') { editing.value = value; editPart.value = part; dialog.value = name; }
function editSsh(item) { sshTarget.value = item; dialog.value = 'ssh'; }
function dismiss(name) { if (dialog.value === name) dialog.value = ''; }
function closeDialog(done) { if (!dialogRef.value?.busy) done(); }
function editorSaved(item, push, pending) {
  dialog.value = '';
  if (push && !push.ok) ElMessage.warning('和节点的连接中断了，不确定是否已切换；节点连回后会自动按新的接入点核对');
  else if (pending) ElMessage.info('已保存，节点下次连上 OpenBear 时切换到新的接入点');
  else if (push?.ok && push.backupReachable === false) ElMessage.warning('已改用新的接入点，但节点连不上备用接入点');
  const index = items.value.findIndex(i => i.id === item.id);
  if (index >= 0) items.value[index] = {...item, live: items.value[index].live}; else items.value.push(item);
  openServer(item);
  void load(true);
}
function reordered(list) { items.value = list.map(item => ({...item, live: itemOf(item.id)?.live ?? item.live})); }
function orderSaved(list) { reordered(list); dialog.value = ''; ElMessage.success('顺序已保存'); }
function settingsSaved(value) {
  // A check switched on or off shows or hides its results, which come with the list.
  const switched = JSON.stringify(value?.checks) !== JSON.stringify(settings.value?.checks);
  const latencyMoved = JSON.stringify(value?.latency) !== JSON.stringify(settings.value?.latency);
  settings.value = value; setServerZone(value?.timezone); dialog.value = ''; ElMessage.success('已保存');
  if (switched) void load(true);
  if (latencyMoved) void refreshPings();
}
function added(item) { dialog.value = ''; void load(true).then(() => openServer(items.value.find(i => i.id === item.id) || item)); }
function sshSaved() { dialog.value = ''; void load(true); ElMessage.success('SSH 连接已保存'); }
function removed(id) { closeTab(id); void load(true); }
function openBatch(ids = []) { batchTargets.value = ids; dialog.value = 'batch'; }
function openFiles(id) { filesFor.value = id; }
function syncPhone() { phone.value = media.matches; }

onMounted(() => {
  ensureFlagFont();
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener?.('change', syncPhone);
  void load().then(refreshLive);
  void loadRates().then(value => { if (value) fx.value = value; });
  startPolls();
});
function startPolls() {
  stopPolls();
  livePoll = setInterval(() => { if (!document.hidden) void refreshLive(); }, 3000);
  listPoll = setInterval(() => { if (!document.hidden && !dialog.value) void load(true); }, 30000);
  pingPoll = setInterval(() => { if (!document.hidden && active.value === 'probe') void refreshPings(); }, 60000);
  clock = setInterval(() => { now.value = Date.now(); }, 30000);
}
function stopPolls() { clearInterval(livePoll); clearInterval(listPoll); clearInterval(pingPoll); clearInterval(clock); }
// The page stays alive while another one is shown; terminals keep their
// connections, but nothing polls until it comes back.
let shownBefore = false;
onActivated(() => {
  visible.value = true;
  startPolls();
  if (shownBefore) { now.value = Date.now(); void load(true).then(refreshLive); }
  shownBefore = true;
});
onDeactivated(() => { visible.value = false; stopPolls(); });
onBeforeUnmount(() => { alive = false; stopPolls(); media?.removeEventListener?.('change', syncPhone); emit('mobile-header-ready', false); document.removeEventListener('keydown', escBgp); document.removeEventListener('pointerdown', pressBgp, true); });

// BGP 上游 from a card's network line: one popover for the whole board, by the line tapped; a bottom sheet on a phone.
// The page alone opens and closes it (Element Plus would also toggle it from the line once it had been opened
// there): a second tap on the same line, Escape, or on a desktop a press anywhere but the popover or a network line.
const bgpFor = ref(null);
const bgpWidth = ref(960);
function openBgp(item, anchor) {
  if (bgpFor.value?.item.id === item.id) { bgpFor.value = null; return; }
  bgpWidth.value = Math.min(1000, window.innerWidth - 48);
  bgpFor.value = {item, anchor};
}
const escBgp = event => { if (event.key === 'Escape') bgpFor.value = null; };
const pressBgp = event => {
  if (!phone.value && !event.target.closest?.('.sv-bgp-popper, [data-network]')) bgpFor.value = null;
};
watch(bgpFor, open => {
  const how = open ? 'addEventListener' : 'removeEventListener';
  document[how]('keydown', escBgp);
  document[how]('pointerdown', pressBgp, true);
});
</script>
<template>
  <section class="instances-page instance-ui servers-ui h-full min-h-0">
    <AdminPageHeader title="服务器" subtitle="探针、终端与账单" @mobile-header-ready="emit('mobile-header-ready', $event)">
      <template #mobile-navigation><slot name="mobile-navigation" /></template>
      <template #actions><el-button @click="openBatch()">批量执行</el-button><el-button :disabled="items.length < 2" @click="open('order')">调整顺序</el-button><el-button @click="open('settings')">设置</el-button></template>
      <template #primary><el-button type="primary" @click="open('add')">添加服务器</el-button></template>
    </AdminPageHeader>

    <nav class="sv-tabs" role="tablist" aria-label="服务器页面">
      <button type="button" role="tab" class="sv-tab" :aria-selected="active === 'probe'" @click="active = 'probe'">探针</button>
      <button type="button" role="tab" class="sv-tab" :aria-selected="active === 'terminal'" @click="showTerminal">终端<span v-if="terminalCount" class="ix-count">{{ terminalCount }}</span></button>
      <button type="button" role="tab" class="sv-tab" :aria-selected="active === 'mesh'" @click="showMesh()">组网</button>
      <button type="button" role="tab" class="sv-tab" :aria-selected="active === 'ddns'" @click="showDdns()">动态域名</button>
      <button type="button" role="tab" class="sv-tab" :aria-selected="active === 'bench'" @click="showBench">评测</button>
      <div v-for="id in tabs" :key="id" role="tab" class="sv-tab is-server" :aria-selected="active === id" tabindex="0" @click="active = id" @keydown.enter="active = id" @mousedown.middle.prevent="closeTab(id)">
        <span class="sv-flag" aria-hidden="true">{{ flag(itemOf(id)?.region) }}</span>
        <span class="sv-tab-name">{{ itemOf(id)?.name }}</span>
        <span class="pb-dot" :data-tone="tabTone(itemOf(id))" aria-hidden="true"></span>
        <button type="button" class="sv-tab-close" :aria-label="`关闭 ${itemOf(id)?.name}`" @click.stop="closeTab(id)"><el-icon><Close /></el-icon></button>
      </div>
    </nav>
    <p v-if="error" role="alert" class="instance-error instance-banner">{{ error }} <button type="button" class="instance-link" @click="load()">重试</button></p>

    <div class="sv-body">
      <div v-show="active === 'probe'" class="sv-scroll">
        <ProbeBoard :items="items" :now="now" :loading="loading" :spark="spark" :pings="pings" :build="build" :fx="fx" @open="openServer" @terminal="openTerminal" @desktop="openDesktop" @bgp="openBgp" @add="open('add')" @changed="load(true)" @reordered="reordered" />
      </div>
      <TerminalWorkbench v-if="terminalMounted" v-show="active === 'terminal'" :items="items" :request="terminalRequest" @count="terminalCount = $event" @files="openFiles" @batch="openBatch" />
      <div v-if="meshMounted" v-show="active === 'mesh'" class="sv-scroll">
        <MeshView :active="active === 'mesh'" :items="items" :focus="meshFocus" @open-server="id => itemOf(id) && openServer(itemOf(id))" />
      </div>
      <DdnsView v-if="ddnsMounted" v-show="active === 'ddns'" :active="active === 'ddns' && visible" :focus="ddnsFocus" />
      <BenchView v-if="benchMounted" v-show="active === 'bench'" :active="active === 'bench' && visible" :servers="items" />
      <div v-for="id in tabs" v-show="active === id" :key="id" class="sv-scroll">
        <ServerDetail v-if="itemOf(id)" :item="itemOf(id)" :settings="settings" :build="build" :now="now" :active="active === id && visible" :phone="phone" :servers="items"
          @edit="(item, part) => open('edit', item, part)" @ssh="editSsh" @settings="open('settings')" @terminal="openTerminal" @desktop="openDesktop" @changed="load(true)" @removed="removed"
          @files="openFiles($event.id)" @batch="openBatch([$event.id])" @mesh="showMesh" @open="other => itemOf(other) && openServer(itemOf(other))" />
      </div>
    </div>

    <DdnsSheet :model-value="dialog === 'edit'" kind="drawer" size="580px" form :phone="phone" :title="editing ? `编辑「${editing.name}」` : '添加服务器'" :busy="Boolean(dialogRef?.busy)" :dirty="Boolean(dialogRef?.dirty)" @update:model-value="dismiss('edit')">
      <ServerEditor v-if="dialog === 'edit'" ref="dialogRef" :server="editing" :part="editPart" :servers="items" :groups="groups" :tags="tags" :regions="regions" :settings="settings" @saved="editorSaved" @cancel="dialog = ''" />
    </DdnsSheet>
    <el-dialog :model-value="dialog === 'order'" @update:model-value="dismiss('order')" title="调整顺序" width="520px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body destroy-on-close :close-on-click-modal="false" :before-close="closeDialog">
      <ServerOrder v-if="dialog === 'order'" ref="dialogRef" :items="items" @saved="orderSaved" @cancel="dialog = ''" />
    </el-dialog>
    <el-dialog :model-value="dialog === 'add'" @update:model-value="dismiss('add')" title="添加服务器" width="620px" class="admin-dialog instance-dialog instance-ui servers-ui sv-add-dialog" append-to-body destroy-on-close :close-on-click-modal="false">
      <AddServer v-if="dialog === 'add'" :settings="settings" :groups="groups" :servers="items" @open="added" @changed="load(true)" @settings="open('settings')" @guide="guide = true" @close="dialog = ''" />
    </el-dialog>
    <el-drawer v-model="guide" :direction="phone ? 'btt' : 'rtl'" :size="phone ? '86%' : '560px'" title="服务器接入说明"
      :class="['instance-ui', 'servers-ui', 'sv-guide-drawer', phone ? 'mobile-asset-sheet mobile-viewport-sheet' : '']" append-to-body>
      <div v-if="phone" class="mobile-asset-grip" aria-hidden="true"></div>
      <ServerGuide />
    </el-drawer>
    <el-dialog :model-value="dialog === 'ssh'" @update:model-value="dismiss('ssh')" :title="sshTarget?.ssh ? 'SSH 连接' : '登记 SSH 连接'" width="500px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body destroy-on-close :close-on-click-modal="false" :before-close="closeDialog">
      <ServerForm v-if="dialog === 'ssh' && sshServer" ref="dialogRef" :server="sshServer" @saved="sshSaved" @cancel="dialog = ''" />
    </el-dialog>
    <el-popover v-if="!phone" :visible="Boolean(bgpFor)" :virtual-ref="bgpFor?.anchor" virtual-triggering placement="bottom-start" :width="bgpWidth"
      popper-class="instance-ui servers-ui sv-bgp-popper">
      <BgpSection v-if="bgpFor" :key="bgpFor.item.id" bare closable :server="bgpFor.item" @close="bgpFor = null" />
    </el-popover>
    <DdnsSheet :model-value="phone && Boolean(bgpFor)" phone :title="bgpFor ? `BGP 上游 · ${bgpFor.item.name}` : ''" @update:model-value="v => { if (!v) bgpFor = null; }">
      <BgpSection v-if="phone && bgpFor" :key="bgpFor.item.id" bare phone heading="" :server="bgpFor.item" />
    </DdnsSheet>
    <DdnsSheet :model-value="dialog === 'settings'" kind="drawer" size="520px" :phone="phone" title="服务器设置" :busy="Boolean(dialogRef?.busy)" @update:model-value="dismiss('settings')">
      <ServerSettings v-if="dialog === 'settings'" ref="dialogRef" :settings="settings" :items="items" :build="build" @saved="settingsSaved" @cancel="dialog = ''" @guide="guide = true" @checked="load(true)" />
    </DdnsSheet>
    <el-dialog :model-value="dialog === 'batch'" @update:model-value="dismiss('batch')" title="批量执行" width="720px" class="admin-dialog instance-dialog instance-ui servers-ui sv-batch-dialog" append-to-body destroy-on-close :close-on-click-modal="false">
      <BatchExec v-if="dialog === 'batch'" :items="items" :preset="batchTargets" @close="dialog = ''" @done="load(true)" />
    </el-dialog>
    <el-drawer :model-value="Boolean(filesFor)" @update:model-value="v => { if (!v) filesFor = '' }" :direction="phone ? 'btt' : 'rtl'" :size="phone ? '86%' : '460px'"
      :title="`文件 · ${itemOf(filesFor)?.name || ''}`" :class="['instance-ui', 'servers-ui', 'sv-files-drawer', phone ? 'mobile-asset-sheet mobile-viewport-sheet' : '']" append-to-body destroy-on-close>
      <div v-if="phone" class="mobile-asset-grip" aria-hidden="true"></div>
      <FileBrowser v-if="filesFor" :server-id="filesFor" :platform="platformOf(itemOf(filesFor))" :phone="phone" />
    </el-drawer>
  </section>
</template>
