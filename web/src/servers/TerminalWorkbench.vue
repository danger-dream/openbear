<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ArrowDown, ArrowUp, Close, Expand, Fold, FolderOpened, FullScreen, Hide, Key, Monitor, MoreFilled, Plus, RefreshRight, Search, Setting, View } from '@element-plus/icons-vue';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import TerminalPane from './TerminalPane.vue';
import FileBrowser from './FileBrowser.vue';
import PlatformMark from './PlatformMark.vue';
import CommandPanel from './CommandPanel.vue';
import { canDesktop, flag, hasDesktop, noAutofill, platformOf } from './serverHelpers.js';

const DesktopPane = defineAsyncComponent(() => import('./DesktopPane.vue'));

const props = defineProps({items: {type: Array, required: true}, request: {type: Object, default: null}});
const emit = defineEmits(['count', 'files', 'batch']);
const FONT = 'openbear.servers.terminalFont';
const sessions = ref([]), active = ref(''), query = ref(''), privacy = ref(false), commands = ref(false), picker = ref(false), phone = ref(false);
const finding = ref(false), needle = ref(''), missed = ref(false), finder = ref(null);
const fontSize = ref(Math.min(20, Math.max(10, Number(localStorage.getItem(FONT)) || 13)));
watch(fontSize, v => { try { localStorage.setItem(FONT, String(v)); } catch { /* storage may be blocked */ } });
const panes = new Map();
let serial = 0, media, observer;
// Files dock beside the terminal: on the right unless moved below, or the area is too narrow
// for both side by side. Each terminal tab keeps its own folder; the place and sizes are remembered.
const DOCK = 'openbear.servers.filesDock', SIDE = 'openbear.servers.terminalSide';
const files = reactive({}), body = ref(null), dock = ref('right'), roomy = ref(true);
const dockSize = reactive((() => { try { return {right: 420, bottom: 300, side: 'right', ...JSON.parse(localStorage.getItem(DOCK) || '{}')}; } catch { return {right: 420, bottom: 300, side: 'right'}; } })());
const filesShown = computed(() => Boolean(current.value && files[current.value.key]));
const saveDock = () => { try { localStorage.setItem(DOCK, JSON.stringify(dockSize)); } catch { /* storage may be blocked */ } };
function layout() {
  const box = body.value?.getBoundingClientRect();
  if (!box?.width) return;
  roomy.value = box.width >= 760;
  dock.value = dockSize.side !== 'bottom' && roomy.value ? 'right' : 'bottom';
}
function moveDock() { dockSize.side = dock.value === 'right' ? 'bottom' : 'right'; saveDock(); layout(); }
// The server list folds away on a wide screen to give the terminal its room.
const folded = ref((() => { try { return localStorage.getItem(SIDE) === 'folded'; } catch { return false; } })());
watch(folded, v => { try { localStorage.setItem(SIDE, v ? 'folded' : 'open'); } catch { /* storage may be blocked */ } });
const sideHidden = computed(() => folded.value && sessions.value.length > 0 && !phone.value);
function clampDock() {
  const box = body.value?.getBoundingClientRect();
  if (!box?.width) return dockSize[dock.value];
  return dock.value === 'right' ? Math.min(Math.max(dockSize.right, 300), box.width - 420) : Math.min(Math.max(dockSize.bottom, 180), box.height - 140);
}
function resize(event) {
  const start = {x: event.clientX, y: event.clientY, size: clampDock()}, side = dock.value;
  event.target.setPointerCapture?.(event.pointerId);
  const move = e => { dockSize[side] = side === 'right' ? start.size - (e.clientX - start.x) : start.size - (e.clientY - start.y); dockSize[side] = clampDock(); };
  const end = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', end); saveDock(); };
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', end);
}
const KEYS = [['Esc', '\x1b'], ['Tab', '\t'], ['Ctrl', 'ctrl'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['←', '\x1b[D'], ['→', '\x1b[C'], ['|', '|'], ['/', '/'], ['-', '-'], ['~', '~']];
const desktopMeta = reactive({});

const usable = item => item.node && item.live && (item.node.caps?.terminal !== false || canDesktop(item));
const servers = computed(() => {
  const q = query.value.trim().toLowerCase();
  return props.items.filter(i => i.node && (!q || [i.name, i.profile.group, i.node.info?.ipv4].some(v => String(v || '').toLowerCase().includes(q))));
});
const current = computed(() => sessions.value.find(s => s.key === active.value) || null);
const serverOf = id => props.items.find(i => i.id === id);
const nameOf = id => serverOf(id)?.name || '已删除的服务器';
const currentServerCanDesktop = computed(() => Boolean(current.value && canDesktop(serverOf(current.value.serverId))));
const scaleModeLabel = mode => ({width: '铺满宽度', fit: '适应窗口', original: '100%', custom: '自定义'}[mode] || '适应窗口');
function address(item) {
  const ip = item.node?.info?.ipv4 || item.node?.info?.ipv6 || '';
  if (!privacy.value || !ip) return ip;
  return ip.includes(':') ? ip.split(':').slice(0, 2).join(':') + ':****' : ip.split('.').slice(0, 2).join('.') + '.*.*';
}
function rowState(item) {
  if (!item.live) return ['bad', '离线'];
  if (item.node.caps?.terminal === false) return ['off', '已关闭终端'];
  return ['ok', ''];
}

function open(serverId, fresh = false, kind = 'terminal') {
  picker.value = false;
  const item = serverOf(serverId);
  const mode = kind === 'desktop' && hasDesktop(item) ? 'desktop' : 'terminal';
  if (mode !== 'desktop' && finding.value) closeSearch();
  const existing = !fresh && sessions.value.find(s => s.serverId === serverId && (s.kind || 'terminal') === mode);
  if (existing) { active.value = existing.key; return; }
  const session = {key: `${serverId}#${++serial}`, serverId, kind: mode, status: 'connecting', reason: ''};
  sessions.value.push(session);
  active.value = session.key;
}
function close(key) {
  const index = sessions.value.findIndex(s => s.key === key);
  if (index < 0) return;
  sessions.value.splice(index, 1);
  panes.delete(key);
  delete files[key];
  delete desktopMeta[key];
  if (active.value === key) active.value = sessions.value[Math.min(index, sessions.value.length - 1)]?.key || '';
}
function tabCommand(command, key) {
  const session = sessions.value.find(s => s.key === key);
  if (command === 'again' && session) open(session.serverId, true, session.kind || 'terminal');
  else if (command === 'desktop' && session) open(session.serverId, false, 'desktop');
  else if (command === 'terminal' && session) open(session.serverId, false, 'terminal');
  else if (command === 'close') close(key);
  else if (command === 'others') sessions.value.filter(s => s.key !== key).forEach(s => close(s.key));
  else if (command === 'all') [...sessions.value].forEach(s => close(s.key));
}
function status(session, value, reason) { session.status = value; session.reason = reason; }
function onDesktopMeta(session, meta) { desktopMeta[session.key] = meta; }
const isDesktop = computed(() => current.value?.kind === 'desktop');
const currentDesktopMeta = computed(() => (current.value && desktopMeta[current.value.key]) || {scaleMode: 'fit', touchMode: 'direct'});
const currentPlatform = computed(() => current.value ? platformOf(serverOf(current.value.serverId)) : 'linux');
function pane(key, instance) { if (instance) panes.set(key, instance); else panes.delete(key); }
function run(text) { panes.get(active.value)?.run(text); }
function key(sequence) { panes.get(active.value)?.key(sequence); }
async function openSearch() { finding.value = true; await nextTick(); finder.value?.focus(); finder.value?.select(); }
function closeSearch() { finding.value = false; panes.get(active.value)?.search(''); panes.get(active.value)?.focus(); }
function find(backwards = false) { missed.value = Boolean(needle.value) && !panes.get(active.value)?.search(needle.value, backwards); }
function font(delta) { fontSize.value = Math.min(20, Math.max(10, fontSize.value + delta)); }
function tool(name) {
  const pane = panes.get(active.value);
  if (name === 'search') openSearch();
  else if (name === 'files' && current.value) {
    // Phones keep the bottom sheet; a docked pane would leave no room to type.
    if (phone.value) emit('files', current.value.serverId);
    else files[current.value.key] = !files[current.value.key];
  }
  else if (name === 'batch') emit('batch', current.value ? [current.value.serverId] : []);
  else if (name === 'clear') pane?.clear();
  else if (name === 'bigger') font(1);
  else if (name === 'smaller') font(-1);
  else if (name === 'dp-scale') pane?.toggleScale?.();
  else if (name?.startsWith('dp-scale:')) pane?.setScaleMode?.(name.slice(9));
  else if (name === 'dp-zoom-in') pane?.zoomBy?.(15);
  else if (name === 'dp-zoom-out') pane?.zoomBy?.(-15);
  else if (name === 'dp-match') pane?.matchResolution?.();
  else if (name === 'dp-fullscreen') pane?.toggleFullscreen?.();
  else if (name === 'dp-clipboard') pane?.openClipboard?.();
  else if (name === 'dp-settings') pane?.openSettings?.();
  else if (name === 'dp-reconnect') pane?.reconnect?.();
  else if (name === 'dp-touch') pane?.toggleTouchMode?.();
  else if (name === 'dp-kbd') pane?.openMobileKeyboard?.();
  else if (name === 'dp-ctrl') pane?.toggleModifier?.('ctrl');
  else if (name === 'dp-alt') pane?.toggleModifier?.('alt');
  else if (name === 'dp-rclick') pane?.triggerRightClick?.();
  else if (name === 'open-desktop' && current.value) open(current.value.serverId, false, 'desktop');
  else if (name === 'open-terminal' && current.value) open(current.value.serverId, false, 'terminal');
  else if (name?.startsWith('combo:')) pane?.sendCombo?.(name.slice(6));
}

watch(() => props.request?.nonce, () => { if (props.request) open(props.request.serverId, false, props.request.kind || 'terminal'); }, {immediate: true});
watch(() => sessions.value.length, n => emit('count', n), {immediate: true});
onMounted(() => {
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  phone.value = media.matches;
  media.addEventListener?.('change', update);
  observer = new ResizeObserver(layout);
});
watch(body, el => { observer?.disconnect(); if (el) { observer?.observe(el); layout(); } }, {flush: 'post'});
function update() { phone.value = media.matches; }
onBeforeUnmount(() => { media?.removeEventListener?.('change', update); observer?.disconnect(); });
</script>
<template>
  <div class="tw" :class="{'has-sessions': sessions.length, 'is-folded': sideHidden}">
    <aside v-if="!phone || !sessions.length" v-show="!sideHidden" class="tw-side" aria-label="可连接的服务器">
      <div class="tw-side-head">
        <label class="tw-search"><el-icon><Search /></el-icon><input v-bind="noAutofill" v-model="query" type="search" class="tw-search-input" placeholder="搜索服务器" aria-label="搜索服务器" /></label>
        <button type="button" class="ix-icon-button" :aria-pressed="privacy" :title="privacy ? '显示完整 IP' : '隐藏 IP'" :aria-label="privacy ? '显示完整 IP' : '隐藏 IP'" @click="privacy = !privacy"><el-icon><View v-if="privacy" /><Hide v-else /></el-icon></button>
      </div>
      <div class="tw-list">
        <p v-if="!servers.length" class="instance-empty">还没有安装节点的服务器</p>
        <template v-for="item in servers" :key="item.id">
          <el-dropdown v-if="hasDesktop(item)" trigger="contextmenu" class="tw-server-drop" @command="cmd => open(item.id, false, cmd)">
            <button type="button" class="tw-server" :class="{'is-active': current?.serverId === item.id}" :disabled="!usable(item)" @click="open(item.id, false, item.node?.caps?.terminal === false && canDesktop(item) ? 'desktop' : 'terminal')">
              <span class="sv-flag" aria-hidden="true">{{ flag(item.region) || '·' }}</span>
              <span class="tw-server-text"><span class="tw-server-name"><strong>{{ item.name }}</strong><PlatformMark :item="item" /></span><small class="mono">{{ address(item) || '—' }}</small></span>
              <span class="ob-dot" :data-tone="rowState(item)[0]">{{ rowState(item)[1] }}</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="desktop" :disabled="!canDesktop(item)">打开远程桌面</el-dropdown-item>
                <el-dropdown-item command="terminal" :disabled="item.node?.caps?.terminal === false">打开终端</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button v-else type="button" class="tw-server" :class="{'is-active': current?.serverId === item.id}" :disabled="!usable(item)" @click="open(item.id)">
            <span class="sv-flag" aria-hidden="true">{{ flag(item.region) || '·' }}</span>
            <span class="tw-server-text"><span class="tw-server-name"><strong>{{ item.name }}</strong><PlatformMark :item="item" /></span><small class="mono">{{ address(item) || '—' }}</small></span>
            <span class="ob-dot" :data-tone="rowState(item)[0]">{{ rowState(item)[1] }}</span>
          </button>
        </template>
      </div>
    </aside>

    <section v-if="sessions.length" class="tw-main">
      <div class="tw-tabs" role="tablist" aria-label="终端会话">
        <button v-if="!phone" type="button" class="tw-tool tw-fold" :title="folded ? '展开服务器列表' : '收起服务器列表'" :aria-label="folded ? '展开服务器列表' : '收起服务器列表'"
          :aria-expanded="!folded" @click="folded = !folded"><el-icon><Expand v-if="folded" /><Fold v-else /></el-icon></button>
        <div class="tw-tab-scroll">
          <el-dropdown v-for="session in sessions" :key="session.key" trigger="contextmenu" @command="tabCommand($event, session.key)">
            <div class="tw-tab" role="tab" :aria-selected="active === session.key" tabindex="0" @click="active = session.key" @keydown.enter="active = session.key" @mousedown.middle.prevent="close(session.key)">
              <span class="tw-status" :data-status="session.status" aria-hidden="true"></span>
              <el-icon v-if="session.kind === 'desktop'" class="tw-tab-kind" title="远程桌面"><Monitor /></el-icon>
              <span class="tw-tab-name">{{ session.kind === 'desktop' ? `桌面 · ${nameOf(session.serverId)}` : nameOf(session.serverId) }}</span>
              <button type="button" class="tw-tab-close" :aria-label="`关闭 ${nameOf(session.serverId)} 的${session.kind === 'desktop' ? '桌面' : '终端'}`" @click.stop="close(session.key)"><el-icon><Close /></el-icon></button>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="again">再开一个</el-dropdown-item>
                <el-dropdown-item v-if="session.kind !== 'desktop' && canDesktop(serverOf(session.serverId))" command="desktop">打开远程桌面</el-dropdown-item>
                <el-dropdown-item v-if="session.kind === 'desktop' && serverOf(session.serverId)?.node?.caps?.terminal !== false" command="terminal">打开终端</el-dropdown-item>
                <el-dropdown-item command="close" divided>关闭</el-dropdown-item>
                <el-dropdown-item command="others">关闭其他</el-dropdown-item>
                <el-dropdown-item command="all">全部关闭</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <button v-if="phone" type="button" class="tw-tool" aria-label="打开新的终端或桌面" @click="picker = true"><el-icon><Plus /></el-icon></button>
        <template v-if="isDesktop">
          <template v-if="!phone">
            <span class="tw-font" role="group" aria-label="画面缩放">
              <button type="button" class="tw-tool" title="缩小画面 (−10%)" aria-label="缩小画面" @click="tool('dp-zoom-out')">−</button>
              <el-dropdown trigger="click" :teleported="false" @command="tool">
                <button type="button" class="tw-tool" title="切换画面缩放方式（Alt+滚轮也可缩放）">{{ scaleModeLabel(currentDesktopMeta.scaleMode) }}{{ currentDesktopMeta.zoomPct ? ` ${currentDesktopMeta.zoomPct}%` : '' }}</button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="dp-scale:width">铺满宽度（鼠标移至上下边缘自动滚屏）</el-dropdown-item>
                    <el-dropdown-item command="dp-scale:fit">适应窗口（完整显示整屏）</el-dropdown-item>
                    <el-dropdown-item command="dp-scale:original">100% 原始尺寸</el-dropdown-item>
                    <el-dropdown-item command="dp-scale:pct:75" divided>固定 75%</el-dropdown-item>
                    <el-dropdown-item command="dp-scale:pct:125">固定 125%</el-dropdown-item>
                    <el-dropdown-item command="dp-scale:pct:150">固定 150%</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <button type="button" class="tw-tool" title="放大画面 (+10%)" aria-label="放大画面" @click="tool('dp-zoom-in')">+</button>
            </span>
            <button v-if="currentPlatform === 'windows'" type="button" class="tw-tool" title="让远程桌面分辨率匹配当前画布尺寸" @click="tool('dp-match')">匹配分辨率</button>
            <el-dropdown trigger="click" :teleported="false" @command="tool">
              <button type="button" class="tw-tool" title="发送组合键"><el-icon><Key /></el-icon><span>组合键</span></button>
              <template #dropdown>
                <el-dropdown-menu v-if="currentPlatform === 'darwin'">
                  <el-dropdown-item command="combo:spotlight">Command + Space（聚焦搜索）</el-dropdown-item>
                  <el-dropdown-item command="combo:forcequit">Command + Option + Esc（强制退出）</el-dropdown-item>
                  <el-dropdown-item command="combo:alttab">Command + Tab（切换应用）</el-dropdown-item>
                  <el-dropdown-item command="combo:win">Command 键</el-dropdown-item>
                  <el-dropdown-item command="combo:lock" divided>Control + Command + Q（锁定屏幕）</el-dropdown-item>
                </el-dropdown-menu>
                <el-dropdown-menu v-else>
                  <el-dropdown-item command="combo:cad">Ctrl + Alt + Del</el-dropdown-item>
                  <el-dropdown-item command="combo:taskmgr">Ctrl + Shift + Esc（任务管理器）</el-dropdown-item>
                  <el-dropdown-item command="combo:win">Windows 键</el-dropdown-item>
                  <el-dropdown-item command="combo:alttab">Alt + Tab（切换窗口）</el-dropdown-item>
                  <el-dropdown-item command="combo:altf4">Alt + F4（关闭窗口）</el-dropdown-item>
                  <el-dropdown-item command="combo:lock" divided>Win + L（锁定计算机）</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <button type="button" class="tw-tool" title="同步剪贴板与发送文本" @click="tool('dp-clipboard')">剪贴板</button>
            <button v-if="serverOf(current?.serverId)?.node?.caps?.terminal !== false" type="button" class="tw-tool" title="打开这台服务器的终端" @click="tool('open-terminal')">终端</button>
            <button type="button" class="tw-tool" title="这台服务器的文件" aria-label="文件" :aria-pressed="filesShown" :disabled="!current" @click="tool('files')"><el-icon><FolderOpened /></el-icon></button>
            <button type="button" class="tw-tool" title="桌面账号、密码与端口设置" aria-label="连接设置" @click="tool('dp-settings')"><el-icon><Setting /></el-icon></button>
            <button type="button" class="tw-tool" :title="currentDesktopMeta.isFullscreen ? '退出全屏 (Esc)' : '全屏显示'" :aria-label="currentDesktopMeta.isFullscreen ? '退出全屏' : '全屏'" :aria-pressed="Boolean(currentDesktopMeta.isFullscreen)" @click="tool('dp-fullscreen')"><el-icon><FullScreen /></el-icon></button>
            <button type="button" class="tw-tool" title="重新连接远程桌面" aria-label="重新连接" @click="tool('dp-reconnect')"><el-icon><RefreshRight /></el-icon></button>
          </template>
          <el-dropdown v-else trigger="click" @command="tool">
            <button type="button" class="tw-tool" aria-label="更多桌面操作"><el-icon><MoreFilled /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="dp-scale:width">缩放：铺满宽度</el-dropdown-item>
                <el-dropdown-item command="dp-scale:fit">缩放：适应窗口</el-dropdown-item>
                <el-dropdown-item command="dp-scale:original">缩放：100% 原始尺寸</el-dropdown-item>
                <el-dropdown-item command="dp-zoom-in">放大画面 (+15%)</el-dropdown-item>
                <el-dropdown-item command="dp-zoom-out">缩小画面 (−15%)</el-dropdown-item>
                <el-dropdown-item command="dp-touch" divided>{{ currentDesktopMeta.touchMode === 'trackpad' ? '触控模式：切换为直点' : '触控模式：切换为触控板' }}</el-dropdown-item>
                <el-dropdown-item command="dp-clipboard">剪贴板与文本输入…</el-dropdown-item>
                <el-dropdown-item v-if="serverOf(current?.serverId)?.node?.caps?.terminal !== false" command="open-terminal">打开终端</el-dropdown-item>
                <el-dropdown-item command="files" :disabled="!current">文件</el-dropdown-item>
                <el-dropdown-item v-if="currentPlatform === 'darwin'" command="combo:spotlight" divided>发送 Command + Space</el-dropdown-item>
                <el-dropdown-item v-if="currentPlatform === 'darwin'" command="combo:forcequit">发送 Command + Option + Esc</el-dropdown-item>
                <el-dropdown-item v-if="currentPlatform !== 'darwin'" command="combo:cad" divided>发送 Ctrl + Alt + Del</el-dropdown-item>
                <el-dropdown-item v-if="currentPlatform !== 'darwin'" command="combo:taskmgr">发送 Ctrl + Shift + Esc</el-dropdown-item>
                <el-dropdown-item v-if="currentPlatform !== 'darwin'" command="combo:altf4">发送 Alt + F4</el-dropdown-item>
                <el-dropdown-item command="dp-settings" divided>桌面连接设置…</el-dropdown-item>
                <el-dropdown-item command="dp-reconnect">重新连接</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
        <template v-else>
          <template v-if="!phone">
            <button v-if="currentServerCanDesktop" type="button" class="tw-tool" title="打开这台服务器的远程桌面" @click="tool('open-desktop')"><el-icon><Monitor /></el-icon><span>查看桌面</span></button>
            <button type="button" class="tw-tool" title="搜索输出（Ctrl+Shift+F）" aria-label="搜索输出" :aria-pressed="finding" @click="finding ? closeSearch() : openSearch()"><el-icon><Search /></el-icon></button>
            <button type="button" class="tw-tool" title="这台服务器的文件" aria-label="文件" :aria-pressed="filesShown" :disabled="!current" @click="tool('files')"><el-icon><FolderOpened /></el-icon></button>
            <span class="tw-font" role="group" aria-label="字号"><button type="button" class="tw-tool" title="缩小字号" aria-label="缩小字号" :disabled="fontSize <= 10" @click="font(-1)">A−</button><button type="button" class="tw-tool" title="放大字号" aria-label="放大字号" :disabled="fontSize >= 20" @click="font(1)">A+</button></span>
            <button type="button" class="tw-tool" @click="tool('clear')">清屏</button>
            <button type="button" class="tw-tool" @click="tool('batch')">批量执行</button>
          </template>
          <button type="button" class="tw-tool" :aria-pressed="commands" @click="commands = !commands">常用命令</button>
          <el-dropdown v-if="phone" trigger="click" @command="tool">
            <button type="button" class="tw-tool" aria-label="更多终端操作"><el-icon><MoreFilled /></el-icon></button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="currentServerCanDesktop" command="open-desktop">查看桌面</el-dropdown-item>
                <el-dropdown-item command="search">搜索输出</el-dropdown-item>
                <el-dropdown-item command="files" :disabled="!current">文件</el-dropdown-item>
                <el-dropdown-item command="batch">批量执行…</el-dropdown-item>
                <el-dropdown-item command="bigger" divided :disabled="fontSize >= 20">放大字号</el-dropdown-item>
                <el-dropdown-item command="smaller" :disabled="fontSize <= 10">缩小字号</el-dropdown-item>
                <el-dropdown-item command="clear">清屏</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </div>
      <div v-if="finding" class="tw-find" role="search">
        <input v-bind="noAutofill" ref="finder" v-model="needle" type="search" class="tw-find-input" :class="{'is-missed': missed}" placeholder="搜索终端输出" aria-label="搜索终端输出"
          @input="find()" @keydown.enter.exact.prevent="find()" @keydown.shift.enter.prevent="find(true)" @keydown.esc.prevent="closeSearch" />
        <span v-if="missed" class="instance-muted">没有找到</span>
        <button type="button" class="tw-tool" title="上一个" aria-label="上一个" @click="find(true)"><el-icon><ArrowUp /></el-icon></button>
        <button type="button" class="tw-tool" title="下一个" aria-label="下一个" @click="find()"><el-icon><ArrowDown /></el-icon></button>
        <button type="button" class="tw-tool" title="关闭搜索" aria-label="关闭搜索" @click="closeSearch"><el-icon><Close /></el-icon></button>
      </div>
      <div ref="body" class="tw-body" :data-dock="dock" :style="filesShown ? {'--tw-dock': clampDock() + 'px'} : null">
        <div class="tw-panes">
          <template v-for="session in sessions" :key="session.key">
            <DesktopPane v-if="session.kind === 'desktop'" v-show="active === session.key" :ref="el => pane(session.key, el)" :server-id="session.serverId"
              :platform="platformOf(serverOf(session.serverId))" :server-name="nameOf(session.serverId)" :active="active === session.key" :phone="phone"
              @status="(v, r) => status(session, v, r)" @meta="m => onDesktopMeta(session, m)" />
            <TerminalPane v-else v-show="active === session.key" :ref="el => pane(session.key, el)" :server-id="session.serverId"
              :active="active === session.key" :font-size="fontSize" @status="(v, r) => status(session, v, r)" @search="openSearch" />
          </template>
        </div>
        <template v-if="filesShown">
          <div class="tw-splitter" role="separator" :aria-orientation="dock === 'right' ? 'vertical' : 'horizontal'" aria-label="调整文件面板大小" @pointerdown.prevent="resize"></div>
        </template>
        <section v-show="filesShown" class="tw-files" aria-label="文件">
          <header class="tw-files-head">
            <span class="tw-files-title">文件 · {{ current ? nameOf(current.serverId) : '' }}</span>
            <button v-if="roomy" type="button" class="tw-tool" :title="dock === 'right' ? '把文件面板放到终端下方' : '把文件面板放到终端右侧'" @click="moveDock">{{ dock === 'right' ? '放到下方' : '放到右侧' }}</button>
            <button type="button" class="tw-tool" title="关闭文件面板" aria-label="关闭文件面板" @click="files[current.key] = false"><el-icon><Close /></el-icon></button>
          </header>
          <template v-for="session in sessions" :key="session.key">
            <FileBrowser v-if="files[session.key] !== undefined" v-show="active === session.key && files[session.key]" :server-id="session.serverId"
              :platform="platformOf(serverOf(session.serverId))" />
          </template>
        </section>
      </div>
      <CommandPanel v-if="commands && !isDesktop" :ready="current?.status === 'open'" @run="run" @close="commands = false" />
      <div v-if="phone && isDesktop" class="tw-keys" aria-label="远程桌面按键">
        <button type="button" :aria-pressed="Boolean(currentDesktopMeta.keyboardOpen)" @pointerdown.prevent @click="tool('dp-kbd')">键盘</button>
        <button type="button" :aria-pressed="currentDesktopMeta.touchMode === 'trackpad'" @pointerdown.prevent @click="tool('dp-touch')">{{ currentDesktopMeta.touchMode === 'trackpad' ? '触控板' : '直点' }}</button>
        <button type="button" @pointerdown.prevent @click="tool('dp-rclick')">右键</button>
        <button type="button" :aria-pressed="Boolean(currentDesktopMeta.ctrlArmed)" @pointerdown.prevent @click="tool('dp-ctrl')">Ctrl</button>
        <button type="button" @pointerdown.prevent @click="tool('combo:win')">{{ currentPlatform === 'darwin' ? '⌘' : 'Win' }}</button>
        <button type="button" @pointerdown.prevent @click="tool('combo:esc')">Esc</button>
        <button type="button" @pointerdown.prevent @click="tool('combo:tab')">Tab</button>
        <button type="button" aria-label="左方向键" @pointerdown.prevent @click="tool('combo:left')">←</button>
        <button type="button" aria-label="下方向键" @pointerdown.prevent @click="tool('combo:down')">↓</button>
        <button type="button" aria-label="上方向键" @pointerdown.prevent @click="tool('combo:up')">↑</button>
        <button type="button" aria-label="右方向键" @pointerdown.prevent @click="tool('combo:right')">→</button>
        <button v-if="currentPlatform === 'windows'" type="button" @pointerdown.prevent @click="tool('combo:cad')">Ctrl+Alt+Del</button>
        <button v-else type="button" @pointerdown.prevent @click="tool('combo:spotlight')">⌘+Space</button>
        <button type="button" @pointerdown.prevent @click="tool('dp-clipboard')">剪贴板</button>
      </div>
      <div v-else-if="phone" class="tw-keys" aria-label="终端按键">
        <button v-for="[label, sequence] in KEYS" :key="label" type="button" @pointerdown.prevent @click="key(sequence)">{{ label }}</button>
      </div>
    </section>
    <div v-else-if="!phone" class="tw-empty">
      <p>在左侧选择一台服务器打开终端或远程桌面</p>
      <small class="instance-muted">macOS 与 Windows 支持直接打开图形桌面（客户端按需加载）；切到别的页面再回来，会话仍然保留，关闭标签才断开。</small>
    </div>

    <el-drawer v-model="picker" direction="btt" size="auto" title="打开终端" class="mobile-asset-sheet mobile-viewport-sheet servers-ui" append-to-body>
      <div class="mobile-asset-grip" aria-hidden="true"></div>
      <div class="tw-list tw-picker">
        <button v-for="item in servers" :key="item.id" type="button" class="tw-server" :disabled="!usable(item)" @click="open(item.id, false, item.node?.caps?.terminal === false && canDesktop(item) ? 'desktop' : 'terminal')">
          <span class="sv-flag" aria-hidden="true">{{ flag(item.region) || '·' }}</span>
          <span class="tw-server-text"><span class="tw-server-name"><strong>{{ item.name }}</strong><PlatformMark :item="item" /></span><small class="mono">{{ address(item) || '—' }}</small></span>
          <span class="ob-dot" :data-tone="rowState(item)[0]">{{ rowState(item)[1] }}</span>
        </button>
      </div>
    </el-drawer>
  </div>
</template>
