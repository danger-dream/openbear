<script setup>
import { h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { SearchAddon } from '@xterm/addon-search';
import { WebLinksAddon } from '@xterm/addon-web-links';
import '@xterm/xterm/css/xterm.css';
import { terminalReason } from './serverHelpers.js';
import { addressParts, linkInfo } from './terminalLinks.js';

// One shell per pane. The shell lives exactly as long as this socket: a closed
// session is reopened only when the user presses Enter, never automatically.
const props = defineProps({serverId: {type: String, required: true}, active: Boolean, fontSize: {type: Number, default: 13}});
const emit = defineEmits(['status', 'search']);
const host = ref(null);
const encoder = new TextEncoder();
const LIGHT = {background: '#fbfbfb', foreground: '#1f2328', cursor: '#1f2328', cursorAccent: '#fbfbfb', selectionBackground: 'rgba(31, 35, 40, .18)',
  black: '#1f2328', red: '#cf222e', green: '#1a7f37', yellow: '#9a6700', blue: '#0969da', magenta: '#8250df', cyan: '#1b7c83', white: '#6e7781',
  brightBlack: '#57606a', brightRed: '#a40e26', brightGreen: '#116329', brightYellow: '#7d4e00', brightBlue: '#218bff', brightMagenta: '#a475f9', brightCyan: '#3192aa', brightWhite: '#8c959f'};
const DARK = {background: '#161616', foreground: '#e6e6e6', cursor: '#e6e6e6', cursorAccent: '#161616', selectionBackground: 'rgba(230, 230, 230, .22)',
  black: '#2b2b2b', red: '#f47067', green: '#57ab5a', yellow: '#c69026', blue: '#539bf5', magenta: '#b083f0', cyan: '#39c5cf', white: '#cdd9e5',
  brightBlack: '#768390', brightRed: '#ff938a', brightGreen: '#6bc46d', brightYellow: '#daaa3f', brightBlue: '#6cb6ff', brightMagenta: '#dcbdfb', brightCyan: '#56d4dd', brightWhite: '#f0f0f0'};
let term, fit, finder, ws, observer, themeWatch, tip, frame = 0, disposed = false, ctrl = false, state = 'connecting';
const MATCH = {matchBackground: '#f5d06966', activeMatchBackground: '#f59e0b', matchOverviewRuler: '#f59e0b', activeMatchColorOverviewRuler: '#f59e0b'};

const dark = () => document.documentElement.classList.contains('dark');
function setState(value, reason = '') { state = value; emit('status', value, reason); }
function send(data) { if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(data)); }
function sendSize() { if (ws?.readyState === WebSocket.OPEN && term) ws.send(JSON.stringify({type: 'resize', cols: term.cols, rows: term.rows})); }
function refit() {
  if (frame || disposed) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    if (disposed || !host.value?.clientWidth || !host.value.clientHeight) return;
    const before = `${term.cols}x${term.rows}`;
    try { fit.fit(); } catch { return; }
    if (`${term.cols}x${term.rows}` !== before) sendSize();
  });
}
function connect() {
  setState('connecting');
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${proto}//${location.host}/api/servers/${encodeURIComponent(props.serverId)}/terminal?cols=${term.cols}&rows=${term.rows}`);
  socket.binaryType = 'arraybuffer';
  ws = socket;
  let reason = '';
  socket.onmessage = event => {
    if (typeof event.data !== 'string') { term.write(new Uint8Array(event.data)); return; }
    try {
      const message = JSON.parse(event.data);
      if (message.type !== 'status') return;
      if (message.state === 'open') { setState('open'); sendSize(); }
      else reason = message.reason || '';
    } catch { /* ignore malformed control text */ }
  };
  socket.onclose = () => {
    if (disposed || ws !== socket) return;
    setState('closed', reason);
    term.write(`\r\n\x1b[90m[${terminalReason(reason)}，按 Enter 重新连接]\x1b[0m\r\n`);
  };
}
function input(data) {
  if (state === 'closed') { if (data === '\r') connect(); return; }
  if (ctrl && data.length === 1) {
    ctrl = false;
    const code = data.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) { send(String.fromCharCode(code - 64)); return; }
  }
  send(data);
}
// Commands from the panel run as typed; multi-line text keeps its order.
function run(text) { send(text.replace(/\r?\n/g, '\r').replace(/\r?$/, '\r')); term?.focus(); }
function key(sequence) { if (sequence === 'ctrl') ctrl = true; else send(sequence); term?.focus(); }
function focus() { term?.focus(); }
function search(text, backwards = false) {
  if (!finder) return false;
  if (!text) { finder.clearDecorations(); return false; }
  return backwards ? finder.findPrevious(text, {decorations: MATCH}) : finder.findNext(text, {decorations: MATCH});
}
function clear() { term?.clear(); term?.focus(); }

// OSC 8 links show any text for any address: the real one is shown on hover, and again in the
// confirmation that a click (or a tap, where nothing hovers) always goes through.
function linkText(range) {
  return term.buffer.active.getLine(range.start.y - 1)?.translateToString(true, range.start.x - 1, range.end.x) || '';
}
function addressNodes(info) {
  const [before, host, after] = addressParts(info);
  return [before, host ? h('b', host) : null, after];
}
function hideTip() { tip?.remove(); }
function showTip(event, uri, range) {
  const box = term?.element;
  if (!box) return;
  const info = linkInfo(linkText(range), uri);
  const line = (cls, ...parts) => { const el = document.createElement('div'); el.className = cls; el.append(...parts); return el; };
  const [before, host, after] = addressParts(info);
  const strong = document.createElement('b');
  strong.textContent = host;
  tip ||= document.createElement('div');
  tip.className = 'xterm-hover tw-link-tip';
  tip.replaceChildren(...[
    info.mismatch ? line('tw-link-warn', `显示的是 ${info.shownHost}，实际打开 ${info.host || '无效地址'}`) : null,
    line('tw-link-address', before, strong, after),
    line('tw-link-hint', info.url ? '点击后确认再打开' : '不是网页地址，不会打开'),
  ].filter(Boolean));
  box.append(tip);
  const rect = box.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
  tip.style.left = `${Math.max(4, Math.min(x + 12, rect.width - tip.offsetWidth - 4))}px`;
  tip.style.top = `${y + 18 + tip.offsetHeight <= rect.height ? y + 18 : Math.max(4, y - tip.offsetHeight - 8)}px`;
}
function openLink(event, uri, range) {
  hideTip();
  const info = linkInfo(linkText(range), uri);
  if (!info.url) return;
  const message = h('div', {class: 'tw-link-detail'}, [
    info.mismatch ? h('p', {class: 'tw-link-warn'}, `显示的文字指向 ${info.shownHost}，实际会打开 ${info.host}，可能是伪装的链接。`) : null,
    info.shown ? h('p', [h('span', {class: 'tw-link-label'}, '显示文字'), h('span', info.shown)]) : null,
    h('p', [h('span', {class: 'tw-link-label'}, '实际地址'), h('code', {class: 'tw-link-address'}, addressNodes(info))]),
  ]);
  // Opened in the button's own click: a window opened after the box has closed is a blocked popup on phones.
  ElMessageBox.confirm(message, info.mismatch ? '链接文字和实际地址不一致' : '打开链接', {
    type: info.mismatch ? 'warning' : '', confirmButtonText: '打开', cancelButtonText: '取消', customClass: 'tw-link-box',
    beforeClose(action, _, done) { if (action === 'confirm') window.open(info.url, '_blank', 'noopener,noreferrer'); done(); },
  }).catch(() => {}).finally(() => term?.focus());
}
function reconnect() { if (state === 'closed') connect(); }
defineExpose({run, key, focus, search, clear, reconnect});

onMounted(() => {
  term = new Terminal({cursorBlink: true, macOptionIsMeta: true, scrollback: 5000, fontSize: props.fontSize, lineHeight: 1.15, allowProposedApi: true,
    fontFamily: "'Cascadia Mono', 'JetBrains Mono', Menlo, Consolas, 'Noto Sans Mono CJK SC', monospace", theme: dark() ? DARK : LIGHT,
    linkHandler: {activate: openLink, hover: showTip, leave: hideTip}});
  fit = new FitAddon();
  term.loadAddon(fit);
  finder = new SearchAddon();
  term.loadAddon(finder);
  term.attachCustomKeyEventHandler(event => {
    if (event.type === 'keydown' && (event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'f') { emit('search'); return false; }
    return true;
  });
  term.loadAddon(new WebLinksAddon());
  term.open(host.value);
  term.onData(input);
  term.onScroll(hideTip);
  // Like the old panel: selecting copies, right click pastes.
  host.value.addEventListener('mouseup', () => {
    const text = term.getSelection();
    if (!text?.trim()) return;
    navigator.clipboard?.writeText(text).then(() => { term.clearSelection(); ElMessage.success({message: '已复制', duration: 1200}); }).catch(() => {});
  });
  host.value.addEventListener('contextmenu', event => {
    event.preventDefault();
    navigator.clipboard?.readText().then(text => { if (text) send(text.replace(/\r?\n/g, '\r')); }).catch(() => ElMessage.warning('浏览器不允许读取剪贴板'));
  });
  observer = new ResizeObserver(refit);
  observer.observe(host.value);
  themeWatch = new MutationObserver(() => { term.options.theme = dark() ? DARK : LIGHT; });
  themeWatch.observe(document.documentElement, {attributes: true, attributeFilter: ['class']});
  try { fit.fit(); } catch { /* hidden until first shown */ }
  connect();
  if (props.active) term.focus();
});
watch(() => props.active, value => { if (value) { refit(); requestAnimationFrame(() => term?.focus()); } });
watch(() => props.fontSize, value => { if (term) { term.options.fontSize = value; refit(); } });
onBeforeUnmount(() => {
  disposed = true;
  cancelAnimationFrame(frame);
  observer?.disconnect();
  themeWatch?.disconnect();
  hideTip();
  ws?.close();
  term?.dispose();
});
</script>
<template>
  <div ref="host" class="tw-terminal" :class="{'is-active': active}" />
</template>
