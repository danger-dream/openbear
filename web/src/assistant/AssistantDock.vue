<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, useZIndex } from "element-plus";
import { ChevronDown, CornerUpLeft, Eye, EyeOff, ListTodo, PanelRight, PictureInPicture2, Plus, Sparkles, SquareArrowOutUpRight, X } from "@lucide/vue";
import ConsoleView from "../views/consoleView/ConsoleView.vue";
import { Api, apiError } from "../api";
import { userStorage } from "../sessionBoundary.js";
import { assistantSettings } from "./assistantSettings.js";
import { ASSISTANT_ROOT, capturePageContext, contextSummary, currentScope, openOverlays, trackSelection } from "./pageContext.js";
import { runPageRequest } from "./pageActions.js";
import { goToScope } from "./openScope.js";
import { referenceCatalog } from "../references/catalog.js";

const FOLDER = "__assistant";
// Each page and main object has its own unsent draft: local:assistant:<scope key>.
const LOCAL = "local:assistant";
const localId = (key) => `${LOCAL}:${key}`;
const isLocal = (uuid) => String(uuid || "").startsWith("local:");
const MAX_SCOPES = 80;
const STATE_KEY = "openbear.assistant.v1";
const BALL = 44;
const EDGE = 12;
// Below this the composer's model name and the message actions no longer fit.
const MIN_W = 400;
const MIN_H = 500;
// Edges and corners of the floating window that resize it.
const HANDLES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
const SUGGESTIONS = {
	servers: ["这台服务器现在状态怎么样？", "内存为什么偏高？帮我查一下原因", "帮我在这台服务器上安装 "],
	memory: ["帮我检查这些记忆，有没有重复或过时的", "把当前这条记忆整理得更清楚"],
	secrets: ["帮我给凭证按用途打上标签", "列出很久没用过的凭证"],
	docs: ["帮我完善这篇文档的结构和内容", "这篇文档有哪些说得不清楚的地方？"],
	notes: ["帮我续写这篇笔记", "优化这篇笔记的表达，不改原意"],
	settings: ["这个设置是做什么的？", "帮我添加一个新模型", "帮我检查提示词模板有什么可以优化的"],
	console: ["总结一下这个会话目前的进展"],
};
const DEFAULT_SUGGESTIONS = ["这个页面是做什么的？", "帮我看看当前页面有没有问题"];

const props = defineProps({page: {type: String, default: ""}});
const {nextZIndex} = useZIndex();

function readState() {
	try { return JSON.parse(userStorage(window.localStorage).getItem(STATE_KEY) || "{}") || {}; } catch { return {}; }
}
function placementOf(state) {
	return {
		layout: state.layout === "dock" ? "dock" : "float",
		dockWidth: Math.min(720, Math.max(340, Number(state.dockWidth) || 420)),
		ball: {side: state.side === "left" ? "left" : "right", y: Number.isFinite(state.y) ? state.y : null},
		panel: Number.isFinite(state.panel?.x) && Number.isFinite(state.panel?.y) ? {x: state.panel.x, y: state.panel.y} : null,
		size: Number.isFinite(state.size?.w) && Number.isFinite(state.size?.h) ? {w: state.size.w, h: state.size.h} : null,
	};
}
const saved = readState();
const placed = placementOf(saved);
const open = ref(false);
const mounted = ref(false);
const layout = ref(placed.layout);
const dockWidth = ref(placed.dockWidth);
const ball = ref(placed.ball);
// The floating window's top-left once the user has moved it; until then it opens beside the ball.
const panelPos = ref(placed.panel);
// Its size once the user has resized it.
const panelSize = ref(placed.size);
const movingPanel = ref(false);
const resizingPanel = ref(false);
const conversationUuid = ref(String(saved.conversationUuid || LOCAL));
const includeContext = ref(saved.includeContext !== false);
const detailsOpen = ref(false);
// Where the user is now, and per scope the conversation shown there last ({scope key: uuid}).
const scope = ref(currentScope());
const byScope = ref(saved.byScope && typeof saved.byScope === "object" ? saved.byScope : {});
// What is known of each conversation's and draft's scope ({key, label, path, open?}).
const scopeOfConversation = reactive(new Map());
const scopeMeta = reactive(new Map());
const tasks = ref([]);
const tasksLoading = ref(false);
const tasksComplete = ref(false);
let followGeneration = 0;
// While navigating to a task's scope (e.g. servers -> ServerDetail), ignore intermediate parent scopes until it mounts.
let pendingScope = null;
const summary = ref(contextSummary());
const panelZ = ref(0);
const phone = ref(false);
const viewport = ref({w: window.innerWidth, h: window.innerHeight});
const consoleRef = ref(null);
const panelRef = ref(null);
const dragging = ref(false);
// Where the ball is while it is dragged; the style follows it on every pointer move.
const ballDrag = ref(null);
const sheetOffset = ref(0);
let media = null, summaryTimer = 0, stopSelection = null, ballGesture = null, sheetGesture = null, resizeGesture = null, panelGesture = null;

// Every tab keeps the whole state in memory from when it loaded. Each save merges only what
// this tab just changed, so an older tab never writes back a position or size moved elsewhere.
function save(patch) {
	try {
		userStorage(window.localStorage).setItem(STATE_KEY, JSON.stringify({...readState(), ...patch}));
	} catch { /* Storage denial keeps the in-memory state. */ }
}
watch([conversationUuid, includeContext, byScope], () => save({
	conversationUuid: conversationUuid.value, includeContext: includeContext.value, byScope: byScope.value,
}), {deep: true});
// Another tab moved or resized the assistant: show it in the same place here.
function onStorage(event) {
	if (event.storageArea !== window.localStorage || !String(event.key || "").endsWith(STATE_KEY)) return;
	if (dragging.value || movingPanel.value || resizingPanel.value || resizeGesture) return;
	const next = placementOf(readState());
	layout.value = next.layout;
	dockWidth.value = next.dockWidth;
	ball.value = next.ball;
	panelPos.value = next.panel;
	panelSize.value = next.size;
}

const enabled = computed(() => assistantSettings.enabled);
const docked = computed(() => open.value && layout.value === "dock" && !phone.value);
const showBall = computed(() => enabled.value && !open.value);
const ballY = computed(() => {
	const max = viewport.value.h - BALL - EDGE;
	return Math.min(max, Math.max(EDGE, ball.value.y ?? viewport.value.h - BALL - 96));
});
const ballStyle = computed(() => ({
	top: `${ballY.value}px`,
	[ball.value.side]: `${EDGE}px`,
	...(dragging.value && ballDrag.value ? {left: `${ballDrag.value.x}px`, top: `${ballDrag.value.y}px`, right: "auto"} : {}),
}));
const floatSize = computed(() => {
	const maxW = viewport.value.w - 2 * EDGE, maxH = viewport.value.h - 2 * EDGE;
	const {w, h} = panelSize.value || {w: 440, h: 680};
	return {w: Math.min(maxW, Math.max(Math.min(MIN_W, maxW), w)), h: Math.min(maxH, Math.max(Math.min(MIN_H, maxH), h))};
});
const panelStyle = computed(() => {
	if (phone.value) return {zIndex: panelZ.value, ...(sheetOffset.value ? {transform: `translateY(${sheetOffset.value}px)`} : {})};
	if (layout.value === "dock") return {zIndex: panelZ.value, width: `${dockWidth.value}px`};
	const {w, h} = floatSize.value;
	if (panelPos.value) {
		const {x, y} = clampPanel(panelPos.value);
		return {zIndex: panelZ.value, width: `${w}px`, height: `${h}px`, top: `${y}px`, left: `${x}px`};
	}
	const top = Math.min(viewport.value.h - h - EDGE, Math.max(EDGE, ballY.value + BALL - h));
	return {zIndex: panelZ.value, width: `${w}px`, height: `${h}px`, top: `${top}px`, [ball.value.side]: `${EDGE}px`};
});
const currentTitle = computed(() => {
	if (isLocal(conversationUuid.value)) return "新对话";
	return tasks.value.find(item => item.conversationUuid === conversationUuid.value)?.title || "助手对话";
});
// The scope of the conversation on screen: a draft's from its id, a conversation's as created.
const shownScope = computed(() => {
	const uuid = conversationUuid.value;
	if (uuid.startsWith(`${LOCAL}:`)) {
		const key = uuid.slice(LOCAL.length + 1);
		return scopeMeta.get(key) || (key === scope.value.key ? scope.value : {key, label: key});
	}
	return scopeOfConversation.get(uuid) || tasks.value.find(item => item.conversationUuid === uuid)?.assistantScope || null;
});
const elsewhere = computed(() => Boolean(shownScope.value?.key && shownScope.value.key !== scope.value.key));
// Background progress from the global status: assistant tasks running, and results not seen yet.
const assistantActivity = computed(() => (referenceCatalog.treeStatus?.activityItems || []).filter(item => item.assistant));
const runningCount = computed(() => assistantActivity.value.filter(item => item.running).length);
const unseenCount = computed(() => assistantActivity.value.filter(item => !item.running && item.activityUnread).length);
const ballLabel = computed(() => ["打开页面助手", runningCount.value ? `${runningCount.value} 个任务进行中` : "",
	unseenCount.value ? `${unseenCount.value} 个结果未查看` : ""].filter(Boolean).join("，"));
const suggestions = computed(() => SUGGESTIONS[props.page] || DEFAULT_SUGGESTIONS);
const contextLabel = computed(() => {
	const s = summary.value;
	return [s.page, s.overlay, s.subject].filter(Boolean).filter((v, i, all) => all.indexOf(v) === i).join(" › ") || "当前页面";
});

function syncDockClass() {
	const root = document.documentElement;
	root.classList.toggle("assistant-docked", docked.value);
	root.style.setProperty("--assistant-dock-width", docked.value ? `${dockWidth.value}px` : "0px");
}
watch([docked, dockWidth], syncDockClass, {immediate: true});

function bringToFront() {
	const top = Math.max(0, ...openOverlays().map(item => item.z));
	if (!panelZ.value || panelZ.value <= top) panelZ.value = nextZIndex();
}

async function loadTasks() {
	tasksLoading.value = true;
	try {
		const data = await Api.assistantConversations();
		tasks.value = data?.items || [];
		tasksComplete.value = tasks.value.length < 40;
		for (const item of tasks.value) if (item.assistantScope) scopeOfConversation.set(item.conversationUuid, item.assistantScope);
		// A conversation deleted meanwhile: show the draft of where the user is.
		if (tasksComplete.value && !isLocal(conversationUuid.value) && !tasks.value.some(item => item.conversationUuid === conversationUuid.value)) {
			conversationUuid.value = localId(scope.value.key);
		}
	} catch { /* The list is a convenience; the panel keeps working without it. */ }
	finally { tasksLoading.value = false; }
}

function remember(key, uuid) {
	const next = {...byScope.value};
	delete next[key];
	next[key] = uuid;
	const keys = Object.keys(next);
	for (const old of keys.slice(0, Math.max(0, keys.length - MAX_SCOPES))) delete next[old];
	byScope.value = next;
}

// The user moved: show the conversation of that page and object, the latest one there, else its draft.
async function followScope(next = currentScope()) {
	scope.value = next;
	scopeMeta.set(next.key, next);
	if (shownScope.value?.key === next.key) return;
	const generation = ++followGeneration;
	let target = byScope.value[next.key];
	if (!target) {
		try { target = (await Api.assistantConversations({scope: next.key}))?.latest || ""; } catch { target = ""; }
		if (generation !== followGeneration) return;
	}
	conversationUuid.value = target || localId(next.key);
}

async function show() {
	if (!enabled.value) return;
	mounted.value = true;
	open.value = true;
	bringToFront();
	refreshSummary();
	void loadTasks();
	await followScope();
	await nextTick();
	consoleRef.value?.focusComposer?.();
}
function hide() {
	open.value = false;
	detailsOpen.value = false;
	sheetOffset.value = 0;
}
function toggleLayout() {
	layout.value = layout.value === "dock" ? "float" : "dock";
	save({layout: layout.value});
	bringToFront();
}
function newConversation() {
	pendingScope = null;
	scope.value = currentScope();
	scopeMeta.set(scope.value.key, scope.value);
	conversationUuid.value = localId(scope.value.key);
	remember(scope.value.key, conversationUuid.value);
	void nextTick(() => consoleRef.value?.focusComposer?.());
}
// A task from the list: its conversation, and back to the page and object it belongs to.
function pickTask(uuid) {
	const item = tasks.value.find(task => task.conversationUuid === uuid);
	if (!item) return;
	conversationUuid.value = uuid;
	if (item.assistantScope?.key) remember(item.assistantScope.key, uuid);
	if (item.assistantScope && item.assistantScope.key !== scope.value.key) {
		pendingScope = {key: item.assistantScope.key, until: Date.now() + 3000};
		goToScope(item.assistantScope);
	} else {
		pendingScope = null;
	}
}
function backToShownScope() {
	if (shownScope.value?.key && shownScope.value.key !== scope.value.key) {
		pendingScope = {key: shownScope.value.key, until: Date.now() + 3000};
	}
	goToScope(shownScope.value);
}
function taskState(item) {
	if (item.running) return item.activityState === "waiting" ? "等你处理" : "进行中";
	if (item.activityUnread) return "已完成 · 未查看";
	return "";
}
// The scope a draft becomes a conversation with.
function createExtras(localUuid) {
	const uuid = String(localUuid || "");
	const key = uuid.startsWith(`${LOCAL}:`) ? uuid.slice(LOCAL.length + 1) : scope.value.key;
	const meta = scopeMeta.get(key) || (key === scope.value.key ? scope.value : {key, label: key, path: ""});
	return {assistantScope: meta};
}
function openInConsole() {
	if (conversationUuid.value.startsWith("local:")) {
		ElMessage.info("还没有发送过消息，发送后可以在主对话里打开");
		return;
	}
	window.dispatchEvent(new CustomEvent("openbear:open-conversation", {detail: {conversationUuid: conversationUuid.value}}));
	hide();
}
function onConversationCreated(uuid) {
	const from = shownScope.value || scope.value;
	const next = String(uuid || "");
	if (!next || isLocal(next)) {
		conversationUuid.value = localId(scope.value.key);
		return;
	}
	if (from?.key) {
		scopeOfConversation.set(next, from);
		remember(from.key, next);
	}
	conversationUuid.value = next;
	void loadTasks();
}

function refreshSummary() {
	summary.value = contextSummary();
	const here = currentScope();
	if (pendingScope) {
		if (here.key === pendingScope.key) {
			pendingScope = null;
			scope.value = here;
			scopeMeta.set(here.key, here);
		} else if (Date.now() < pendingScope.until) {
			if (open.value && (layout.value === "float" || phone.value)) bringToFront();
			return;
		} else {
			pendingScope = null;
		}
	}
	if (open.value && here.key !== scope.value.key) void followScope(here);
	// A dialog opened meanwhile would cover the floating panel; the user may want to ask about that very dialog.
	if (open.value && (layout.value === "float" || phone.value)) bringToFront();
}
function collectContext() {
	if (!includeContext.value) return null;
	return capturePageContext({visibleChars: assistantSettings.contextChars});
}
function handlePageRequest(request) {
	return runPageRequest(request, {visibleChars: assistantSettings.contextChars});
}

// Ball: a tap opens the panel, a drag moves it and it settles on the nearer side.
function ballDown(event) {
	if (event.button > 0) return;
	const box = event.currentTarget.getBoundingClientRect();
	ballGesture = {id: event.pointerId, startX: event.clientX, startY: event.clientY, dx: event.clientX - box.left, dy: event.clientY - box.top};
	event.currentTarget.setPointerCapture?.(event.pointerId);
}
function ballMove(event) {
	if (!ballGesture || event.pointerId !== ballGesture.id) return;
	if (!dragging.value && Math.hypot(event.clientX - ballGesture.startX, event.clientY - ballGesture.startY) < 5) return;
	dragging.value = true;
	const {w, h} = viewport.value;
	ballDrag.value = {
		x: Math.min(w - BALL, Math.max(0, event.clientX - ballGesture.dx)),
		y: Math.min(h - BALL, Math.max(0, event.clientY - ballGesture.dy)),
	};
}
function ballUp(event) {
	if (!ballGesture || event.pointerId !== ballGesture.id) return;
	if (dragging.value) {
		const at = ballDrag.value || {x: event.clientX - BALL / 2, y: event.clientY - BALL / 2};
		ball.value = {side: at.x + BALL / 2 < viewport.value.w / 2 ? "left" : "right", y: at.y};
		save({side: ball.value.side, y: ball.value.y});
		dragging.value = false;
		ballDrag.value = null;
		ballGesture = null;
		return;
	}
	ballGesture = null;
	void show();
}
function ballCancel() {
	ballGesture = null;
	ballDrag.value = null;
	dragging.value = false;
}

// The floating window keeps whole inside the viewport, also after the viewport shrinks.
function clampPanel({x, y}) {
	const {w, h} = floatSize.value;
	return {
		x: Math.min(viewport.value.w - w - EDGE, Math.max(EDGE, x)),
		y: Math.min(viewport.value.h - h - EDGE, Math.max(EDGE, y)),
	};
}

// Floating window: a drag on the title bar (not on its buttons) moves it with the pointer.
function panelStart(event) {
	if (phone.value || layout.value !== "float" || event.button > 0 || event.target.closest("button, input, textarea, a, .el-dropdown")) return;
	const box = panelRef.value?.getBoundingClientRect();
	if (!box) return;
	panelGesture = {id: event.pointerId, dx: event.clientX - box.left, dy: event.clientY - box.top};
	event.currentTarget.setPointerCapture?.(event.pointerId);
	event.preventDefault();
}
function panelMove(event) {
	if (!panelGesture || event.pointerId !== panelGesture.id) return;
	movingPanel.value = true;
	panelPos.value = clampPanel({x: event.clientX - panelGesture.dx, y: event.clientY - panelGesture.dy});
}
function panelEnd(event) {
	if (!panelGesture || (event && event.pointerId !== panelGesture.id)) return;
	panelGesture = null;
	if (movingPanel.value && panelPos.value) save({panel: panelPos.value});
	movingPanel.value = false;
}

// Floating window: an edge or corner resizes it. The opposite edges stay where they are.
let sizeGesture = null;
function sizeStart(event, dir) {
	if (phone.value || layout.value !== "float" || event.button > 0) return;
	const box = panelRef.value?.getBoundingClientRect();
	if (!box) return;
	sizeGesture = {id: event.pointerId, dir, x: event.clientX, y: event.clientY, rect: {x: box.left, y: box.top, w: box.width, h: box.height}};
	panelPos.value = {x: box.left, y: box.top};
	resizingPanel.value = true;
	event.currentTarget.setPointerCapture?.(event.pointerId);
	event.preventDefault();
}
function sizeMove(event) {
	if (!sizeGesture || event.pointerId !== sizeGesture.id) return;
	const {dir, rect} = sizeGesture, dx = event.clientX - sizeGesture.x, dy = event.clientY - sizeGesture.y;
	const {w: vw, h: vh} = viewport.value;
	let {x, y, w, h} = rect;
	if (dir.includes("e")) w = Math.min(vw - EDGE - rect.x, Math.max(MIN_W, rect.w + dx));
	if (dir.includes("s")) h = Math.min(vh - EDGE - rect.y, Math.max(MIN_H, rect.h + dy));
	if (dir.includes("w")) { x = Math.min(rect.x + rect.w - MIN_W, Math.max(EDGE, rect.x + dx)); w = rect.x + rect.w - x; }
	if (dir.includes("n")) { y = Math.min(rect.y + rect.h - MIN_H, Math.max(EDGE, rect.y + dy)); h = rect.y + rect.h - y; }
	panelSize.value = {w: Math.round(w), h: Math.round(h)};
	panelPos.value = {x: Math.round(x), y: Math.round(y)};
}
function sizeEnd(event) {
	if (!sizeGesture || (event && event.pointerId !== sizeGesture.id)) return;
	sizeGesture = null;
	if (panelPos.value && panelSize.value) save({panel: panelPos.value, size: panelSize.value});
	resizingPanel.value = false;
}

function headDown(event) { if (phone.value) sheetStart(event); else panelStart(event); }
function headMove(event) { if (phone.value) sheetMove(event); else panelMove(event); }
function headUp(event) { if (phone.value) sheetEnd(event); else panelEnd(event); }

// Dock width follows a drag on its left edge.
function resizeStart(event) {
	resizeGesture = {id: event.pointerId, x: event.clientX, width: dockWidth.value};
	event.currentTarget.setPointerCapture?.(event.pointerId);
}
function resizeMove(event) {
	if (!resizeGesture || event.pointerId !== resizeGesture.id) return;
	dockWidth.value = Math.min(Math.min(720, viewport.value.w - 360), Math.max(340, resizeGesture.width + resizeGesture.x - event.clientX));
}
function resizeEnd() {
	if (resizeGesture && dockWidth.value !== resizeGesture.width) save({dockWidth: dockWidth.value});
	resizeGesture = null;
}

// Phone sheet: a downward drag on the header follows the finger and closes past a threshold.
function sheetStart(event) {
	if (!phone.value || event.button > 0 || event.target.closest("button, input, textarea, a, .assistant-context")) return;
	sheetGesture = {id: event.pointerId, y: event.clientY, x: event.clientX, lastY: event.clientY, lastT: event.timeStamp, speed: 0, moving: false};
	event.currentTarget.setPointerCapture?.(event.pointerId);
}
function sheetMove(event) {
	if (!sheetGesture || event.pointerId !== sheetGesture.id) return;
	const dx = event.clientX - sheetGesture.x, dy = event.clientY - sheetGesture.y;
	if (!sheetGesture.moving) {
		if (Math.hypot(dx, dy) < 4) return;
		if (dy < 0 || Math.abs(dx) > Math.abs(dy)) { sheetGesture = null; return; }
		sheetGesture.moving = true;
	}
	sheetGesture.speed = (event.clientY - sheetGesture.lastY) / Math.max(1, event.timeStamp - sheetGesture.lastT);
	sheetGesture.lastY = event.clientY;
	sheetGesture.lastT = event.timeStamp;
	sheetOffset.value = Math.max(0, dy);
}
function sheetEnd() {
	if (!sheetGesture) return;
	const close = sheetOffset.value >= 88 || (sheetOffset.value > 24 && sheetGesture.speed > 0.5);
	sheetGesture = null;
	if (close) hide();
	else sheetOffset.value = 0;
}

function onResize() {
	viewport.value = {w: window.innerWidth, h: window.innerHeight};
}
function updateMedia() {
	phone.value = Boolean(media?.matches);
}
// A modal dialog's focus trap would pull focus back out of the panel; focus moving into it is the user's choice.
function keepFocus(event) {
	if (open.value && event.relatedTarget?.closest?.(ASSISTANT_ROOT)) event.stopPropagation();
}
function onOpenRequest() { void show(); }

watch(enabled, (on) => { if (!on) hide(); });
// A page switch is followed at once rather than at the next refresh.
watch(() => props.page, () => { if (open.value) void nextTick(() => refreshSummary()); });
// The list keeps up with runs starting and finishing while it can be seen.
let tasksTimer = 0;
watch(() => referenceCatalog.treeStatus, () => {
	if (!open.value) return;
	window.clearTimeout(tasksTimer);
	tasksTimer = window.setTimeout(() => void loadTasks(), 600);
});
watch(open, (value) => {
	window.clearInterval(summaryTimer);
	if (value) summaryTimer = window.setInterval(refreshSummary, 1200);
});

onMounted(() => {
	media = window.matchMedia?.("(max-width: 760px)") || null;
	updateMedia();
	media?.addEventListener?.("change", updateMedia);
	window.addEventListener("resize", onResize);
	window.addEventListener("storage", onStorage);
	window.addEventListener("focusout", keepFocus, true);
	window.addEventListener("openbear:open-assistant", onOpenRequest);
	stopSelection = trackSelection();
});
onBeforeUnmount(() => {
	window.clearInterval(summaryTimer);
	window.clearTimeout(tasksTimer);
	media?.removeEventListener?.("change", updateMedia);
	window.removeEventListener("resize", onResize);
	window.removeEventListener("storage", onStorage);
	window.removeEventListener("focusout", keepFocus, true);
	window.removeEventListener("openbear:open-assistant", onOpenRequest);
	stopSelection?.();
	document.documentElement.classList.remove("assistant-docked");
});
</script>

<template>
	<div v-if="enabled" class="assistant-root" data-assistant-root>
		<button v-if="showBall" type="button" class="assistant-ball" :class="{'is-dragging': dragging, 'is-busy': runningCount > 0}" :style="ballStyle"
		        :aria-label="ballLabel" :title="`${ballLabel}（可拖动）`"
		        @pointerdown="ballDown" @pointermove="ballMove" @pointerup="ballUp" @pointercancel="ballCancel">
			<Sparkles :stroke-width="1.8" aria-hidden="true"/>
			<span v-if="runningCount" class="assistant-ball-count" aria-hidden="true">{{ runningCount }}</span>
			<span v-if="unseenCount" class="assistant-ball-dot" aria-hidden="true"></span>
		</button>
		<div v-if="phone && open" class="assistant-backdrop" :style="{zIndex: panelZ}" aria-hidden="true" @click="hide"></div>
		<section v-if="mounted" v-show="open" ref="panelRef" class="assistant-panel"
		         :class="[phone ? 'is-sheet' : `is-${layout}`, {'is-sheet-dragging': sheetOffset > 0, 'is-resizing': resizingPanel}]" :style="panelStyle"
		         role="complementary" aria-label="页面助手" @pointerdown.capture="bringToFront" @keydown.esc.stop>
			<div v-if="!phone && layout === 'dock'" class="assistant-resize" aria-hidden="true"
			     @pointerdown="resizeStart" @pointermove="resizeMove" @pointerup="resizeEnd" @pointercancel="resizeEnd"></div>
			<template v-if="!phone && layout === 'float'">
				<div v-for="dir in HANDLES" :key="dir" class="assistant-size-handle" :class="`is-${dir}`" aria-hidden="true"
				     @pointerdown="sizeStart($event, dir)" @pointermove="sizeMove" @pointerup="sizeEnd" @pointercancel="sizeEnd"></div>
			</template>
			<header class="assistant-head" :class="{'is-moving': movingPanel}" @pointerdown="headDown" @pointermove="headMove" @pointerup="headUp" @pointercancel="headUp">
				<i v-if="phone" class="assistant-grip" aria-hidden="true"></i>
				<div class="assistant-title">
					<strong>页面助手</strong>
					<span :title="currentTitle">{{ currentTitle }}</span>
				</div>
				<div class="assistant-tools">
					<el-dropdown trigger="click" placement="bottom-end" popper-class="assistant-history-popper" @command="pickTask" @visible-change="$event && loadTasks()">
						<button type="button" class="assistant-icon assistant-tasks-button" :aria-label="runningCount ? `任务，${runningCount} 个进行中` : '任务'" title="任务">
							<ListTodo :stroke-width="1.7"/>
							<span v-if="runningCount" class="assistant-tasks-count" aria-hidden="true">{{ runningCount }}</span>
							<span v-else-if="unseenCount" class="assistant-tasks-dot" aria-hidden="true"></span>
						</button>
						<template #dropdown>
							<el-dropdown-menu>
								<el-dropdown-item v-if="tasksLoading && !tasks.length" disabled>正在读取…</el-dropdown-item>
								<el-dropdown-item v-else-if="!tasks.length" disabled>还没有助手对话</el-dropdown-item>
								<el-dropdown-item v-for="item in tasks" :key="item.conversationUuid" :command="item.conversationUuid"
								                  :class="{'is-current': item.conversationUuid === conversationUuid}">
									<span class="assistant-task">
										<span class="assistant-task-title">{{ item.title }}</span>
										<span class="assistant-task-meta">
											<span class="assistant-task-scope">{{ item.assistantScope?.label || '未记录页面' }}</span>
											<span v-if="taskState(item)" class="assistant-task-state" :class="{'is-running': item.running, 'is-unseen': !item.running && item.activityUnread}">{{ taskState(item) }}</span>
										</span>
									</span>
								</el-dropdown-item>
							</el-dropdown-menu>
						</template>
					</el-dropdown>
					<button type="button" class="assistant-icon" aria-label="新对话" title="新对话" @click="newConversation"><Plus :stroke-width="1.7"/></button>
					<button type="button" class="assistant-icon" aria-label="在主对话中打开" title="在主对话中打开" @click="openInConsole"><SquareArrowOutUpRight :stroke-width="1.7"/></button>
					<button v-if="!phone" type="button" class="assistant-icon" :aria-label="layout === 'dock' ? '改为浮动窗口' : '停靠到右侧'" :title="layout === 'dock' ? '改为浮动窗口' : '停靠到右侧'" @click="toggleLayout">
						<PictureInPicture2 v-if="layout === 'dock'" :stroke-width="1.7"/><PanelRight v-else :stroke-width="1.7"/>
					</button>
					<button type="button" class="assistant-icon" aria-label="收起页面助手" title="收起" @click="hide"><X :stroke-width="1.8"/></button>
				</div>
			</header>
			<div v-if="elsewhere" class="assistant-elsewhere" role="status">
				<span>这段对话属于「{{ shownScope.label }}」，页面上的操作要回到那里才会执行</span>
				<button v-if="shownScope.path || shownScope.open" type="button" @click="backToShownScope"><CornerUpLeft :stroke-width="1.7" aria-hidden="true"/>回到该页面</button>
			</div>
			<div class="assistant-context" :class="{'is-off': !includeContext}">
				<button type="button" class="assistant-context-toggle" :aria-pressed="includeContext"
				        :title="includeContext ? '发送时附带当前页面，点击关闭' : '发送时不附带页面，点击开启'" @click="includeContext = !includeContext">
					<Eye v-if="includeContext" :stroke-width="1.7"/><EyeOff v-else :stroke-width="1.7"/>
				</button>
				<button type="button" class="assistant-context-label" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen; refreshSummary()">
					<span class="assistant-context-text">{{ includeContext ? contextLabel : '不附带页面内容' }}</span>
					<span v-if="includeContext && summary.selection" class="assistant-context-badge">{{ summary.selectionSource === 'terminal' ? '终端选区' : '已选文字' }}</span>
					<ChevronDown class="assistant-context-caret" :class="{'is-open': detailsOpen}" :stroke-width="1.7" aria-hidden="true"/>
				</button>
			</div>
			<dl v-if="detailsOpen" class="assistant-context-details">
				<div><dt>页面</dt><dd>{{ summary.page || '—' }}</dd></div>
				<div v-if="summary.overlay"><dt>弹窗</dt><dd>{{ summary.overlay }}</dd></div>
				<div><dt>对象</dt><dd>{{ summary.subject ? `${summary.subject}${summary.subjectCount > 1 ? ` 等 ${summary.subjectCount} 个` : ''}` : '页面没有声明，助手读可见内容' }}</dd></div>
				<div v-if="summary.selection"><dt>选中</dt><dd class="assistant-context-selection">{{ summary.selection }}</dd></div>
				<div v-if="summary.actions.length"><dt>可用动作</dt><dd>{{ summary.actions.join('、') }}</dd></div>
				<p>{{ includeContext ? '发送时附带这些内容和页面可见文字。' : '已关闭：发送时不附带页面内容。' }}</p>
			</dl>
			<div class="assistant-body">
				<ConsoleView ref="consoleRef" variant="assistant" :conversation-uuid="conversationUuid" :folder-id="FOLDER"
				             :page-context="collectContext" :page-host="handlePageRequest" :create-extras="createExtras"
				             @conversation-created="onConversationCreated" @conversations-refresh="loadTasks">
					<template #assistant-empty>
						<div class="assistant-empty-inner">
							<p>我能看到你正在看的{{ summary.page ? `「${summary.page}」` : '页面' }}{{ summary.overlay ? `和打开的「${summary.overlay}」` : '' }}，直接说要做什么就行。</p>
							<div class="assistant-chips">
								<button v-for="text in suggestions" :key="text" type="button" @click="consoleRef?.applyQuickPrompt(text)">{{ text }}</button>
							</div>
						</div>
					</template>
				</ConsoleView>
			</div>
		</section>
	</div>
</template>

<style scoped>
/* Above every dialog and popup: a modal must not hide the way to ask about it. */
.assistant-ball {
	position: fixed;
	z-index: 2147483000;
	display: grid;
	place-items: center;
	width: 44px;
	height: 44px;
	border: 1px solid var(--ob-border);
	border-radius: 50%;
	background: var(--ob-surface-raised);
	color: var(--ob-text);
	box-shadow: var(--ob-shadow-popover);
	cursor: grab;
	touch-action: none;
	user-select: none;
	transition: box-shadow .15s ease, transform .15s ease;
}
.assistant-ball:hover { transform: scale(1.04); }
.assistant-ball.is-dragging { cursor: grabbing; transition: none; transform: scale(1.06); }
.assistant-ball svg { width: 20px; height: 20px; }
.assistant-ball.is-busy { border-color: var(--ob-blue); }
.assistant-ball-count, .assistant-tasks-count {
	position: absolute;
	top: -4px;
	right: -4px;
	min-width: 17px;
	height: 17px;
	padding: 0 4px;
	border-radius: 9px;
	background: var(--ob-blue);
	color: #fff;
	font-size: 10px;
	font-weight: 600;
	line-height: 17px;
	text-align: center;
	pointer-events: none;
}
.assistant-ball-dot, .assistant-tasks-dot {
	position: absolute;
	top: 1px;
	right: 1px;
	width: 9px;
	height: 9px;
	border: 2px solid var(--ob-surface-raised);
	border-radius: 50%;
	background: var(--ob-success);
	pointer-events: none;
}
.assistant-ball-count + .assistant-ball-dot { top: auto; bottom: 0; }
.assistant-tasks-button { position: relative; }
.assistant-tasks-count { top: 1px; right: 0; min-width: 15px; height: 15px; line-height: 15px; font-size: 9px; }
.assistant-tasks-dot { top: 5px; right: 5px; width: 8px; height: 8px; }
.assistant-elsewhere {
	display: flex;
	flex: none;
	align-items: center;
	gap: 8px;
	margin: 8px 10px 0;
	padding: 6px 8px 6px 10px;
	border: 1px solid var(--ob-border);
	border-radius: 8px;
	background: var(--ob-surface-soft, var(--ob-hover));
	color: var(--ob-text-subtle);
	font-size: 12px;
	line-height: 1.5;
}
.assistant-elsewhere span { flex: 1; min-width: 0; }
.assistant-elsewhere button {
	display: inline-flex;
	flex: none;
	align-items: center;
	gap: 4px;
	padding: 3px 8px;
	border: 1px solid var(--ob-border);
	border-radius: 6px;
	background: var(--ob-surface);
	color: var(--ob-text);
	font-size: 12px;
	cursor: pointer;
}
.assistant-elsewhere button svg { width: 13px; height: 13px; }
.assistant-backdrop { position: fixed; inset: 0; background: var(--ob-mask); }
.assistant-panel {
	position: fixed;
	display: flex;
	flex-direction: column;
	min-height: 0;
	overflow: hidden;
	background: var(--ob-bg);
	color: var(--ob-text);
}
.assistant-panel.is-float {
	border: 1px solid var(--ob-border);
	border-radius: 14px;
	box-shadow: var(--ob-shadow-dialog);
}
.assistant-panel.is-dock {
	top: 0;
	right: 0;
	bottom: 0;
	border-left: 1px solid var(--ob-border);
}
.assistant-panel.is-sheet {
	left: 0;
	right: 0;
	bottom: max(0px, calc(100% - var(--mobile-viewport-top, 0px) - var(--mobile-viewport-height, 100dvh)));
	height: calc(var(--mobile-viewport-height, 100dvh) - max(56px, env(safe-area-inset-top, 0px) + 40px));
	border-radius: 16px 16px 0 0;
	box-shadow: var(--ob-shadow-dialog);
	transition: transform 280ms cubic-bezier(.22, .68, 0, 1);
}
.assistant-panel.is-sheet-dragging { transition: none; user-select: none; }
.assistant-resize {
	position: absolute;
	top: 0;
	bottom: 0;
	left: -3px;
	z-index: 2;
	width: 6px;
	cursor: col-resize;
	touch-action: none;
}
.assistant-size-handle { position: absolute; z-index: 3; touch-action: none; }
.assistant-size-handle.is-n, .assistant-size-handle.is-s { left: 12px; right: 12px; height: 6px; cursor: ns-resize; }
.assistant-size-handle.is-e, .assistant-size-handle.is-w { top: 12px; bottom: 12px; width: 6px; cursor: ew-resize; }
.assistant-size-handle.is-n { top: 0; }
.assistant-size-handle.is-s { bottom: 0; }
.assistant-size-handle.is-e { right: 0; }
.assistant-size-handle.is-w { left: 0; }
.assistant-size-handle.is-ne, .assistant-size-handle.is-nw, .assistant-size-handle.is-se, .assistant-size-handle.is-sw { width: 14px; height: 14px; }
.assistant-size-handle.is-ne { top: 0; right: 0; cursor: nesw-resize; }
.assistant-size-handle.is-sw { bottom: 0; left: 0; cursor: nesw-resize; }
.assistant-size-handle.is-nw { top: 0; left: 0; cursor: nwse-resize; }
.assistant-size-handle.is-se { bottom: 0; right: 0; cursor: nwse-resize; }
.assistant-panel.is-resizing { user-select: none; }
.assistant-head {
	position: relative;
	display: flex;
	flex: none;
	align-items: center;
	gap: 8px;
	min-height: 44px;
	padding: 6px 6px 6px 14px;
	border-bottom: 1px solid var(--ob-border-soft);
}
.is-sheet .assistant-head { padding-top: 14px; touch-action: none; }
.is-float .assistant-head { cursor: move; touch-action: none; user-select: none; }
.is-float .assistant-head.is-moving { cursor: grabbing; }
.assistant-grip {
	position: absolute;
	top: 6px;
	left: 50%;
	width: 36px;
	height: 4px;
	margin-left: -18px;
	border-radius: 2px;
	background: var(--ob-border-strong);
}
.assistant-title { display: flex; flex: 1; align-items: baseline; gap: 8px; min-width: 0; }
.assistant-title strong { flex: none; font-size: 14px; font-weight: 600; }
.assistant-title span { overflow: hidden; color: var(--ob-text-subtle); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.assistant-tools { display: flex; flex: none; align-items: center; gap: 2px; }
.assistant-icon {
	display: grid;
	place-items: center;
	width: 30px;
	height: 30px;
	border: 0;
	border-radius: 8px;
	background: transparent;
	color: var(--ob-text-subtle);
	cursor: pointer;
}
.assistant-icon:hover { background: var(--ob-hover); color: var(--ob-text); }
.assistant-icon svg { width: 16px; height: 16px; }
.assistant-context {
	display: flex;
	flex: none;
	align-items: center;
	gap: 4px;
	margin: 8px 10px 0;
	padding: 2px;
	border: 1px solid var(--ob-border-soft);
	border-radius: 9px;
	background: var(--ob-surface-soft);
}
.assistant-context.is-off { opacity: .72; }
.assistant-context-toggle,
.assistant-context-label {
	display: flex;
	align-items: center;
	border: 0;
	background: transparent;
	color: var(--ob-text-subtle);
	cursor: pointer;
}
.assistant-context-toggle { flex: none; justify-content: center; width: 26px; height: 26px; border-radius: 7px; }
.assistant-context-toggle:hover, .assistant-context-label:hover { background: var(--ob-hover); }
.assistant-context-toggle svg, .assistant-context-caret { width: 14px; height: 14px; flex: none; }
.assistant-context-label { flex: 1; gap: 6px; min-width: 0; height: 26px; padding: 0 6px; border-radius: 7px; font-size: 12px; }
.assistant-context-text { flex: 1; overflow: hidden; color: var(--ob-text); text-align: left; text-overflow: ellipsis; white-space: nowrap; }
.assistant-context-badge { flex: none; padding: 0 6px; border-radius: 6px; background: var(--ob-blue-soft); color: var(--ob-blue); font-size: 11px; line-height: 18px; }
.assistant-context-caret { transition: transform .15s ease; }
.assistant-context-caret.is-open { transform: rotate(180deg); }
.assistant-context-details {
	flex: none;
	max-height: 30%;
	margin: 6px 10px 0;
	padding: 8px 10px;
	overflow-y: auto;
	border-radius: 9px;
	background: var(--ob-surface-soft);
	font-size: 12px;
}
.assistant-context-details div { display: flex; gap: 8px; padding: 2px 0; }
.assistant-context-details dt { flex: none; width: 52px; color: var(--ob-text-subtle); }
.assistant-context-details dd { min-width: 0; margin: 0; color: var(--ob-text); overflow-wrap: anywhere; }
.assistant-context-selection { display: -webkit-box; overflow: hidden; -webkit-line-clamp: 3; -webkit-box-orient: vertical; white-space: pre-wrap; }
.assistant-context-details p { margin: 4px 0 0; color: var(--ob-text-subtle); }
.assistant-body { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.assistant-body :deep(.console-page) { flex: 1; min-height: 0; }
.assistant-body :deep(.assistant-empty) { display: flex; align-items: flex-end; height: 100%; padding: 16px 14px; }
.assistant-empty-inner { width: 100%; }
.assistant-empty-inner p { margin: 0 0 10px; color: var(--ob-text-subtle); font-size: 13px; line-height: 1.6; }
.assistant-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.assistant-chips button {
	max-width: 100%;
	padding: 6px 10px;
	border: 1px solid var(--ob-border);
	border-radius: 999px;
	background: var(--ob-surface-raised);
	color: var(--ob-text);
	font-size: 12px;
	text-align: left;
	cursor: pointer;
}
.assistant-chips button:hover { background: var(--ob-hover); }
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.assistant-icon { width: 40px; height: 40px; }
	.assistant-context-toggle { width: 34px; height: 34px; }
	.assistant-context-label { height: 34px; font-size: 13px; }
	.assistant-chips button { padding: 8px 12px; font-size: 13px; }
}
</style>

<style>
html.assistant-docked .app-shell { padding-right: var(--assistant-dock-width, 0px); }
/* Dialogs and drawers share the narrowed page, so the docked panel never covers them. */
html.assistant-docked .el-overlay { right: var(--assistant-dock-width, 0px); }
.ob-assistant-flash {
	outline: 2px solid var(--ob-blue) !important;
	outline-offset: 2px;
	animation: ob-assistant-flash 2.6s ease-out;
}
@keyframes ob-assistant-flash {
	0%, 30% { box-shadow: 0 0 0 4px rgba(var(--ob-blue-rgb), .28); }
	100% { box-shadow: 0 0 0 0 rgba(var(--ob-blue-rgb), 0); }
}
.assistant-history-popper .el-dropdown-menu { width: 300px; max-width: calc(100vw - 24px); max-height: 60vh; overflow-y: auto; }
.assistant-history-popper .el-dropdown-menu__item { line-height: 1.4; padding-block: 7px; }
.assistant-task { display: flex; flex-direction: column; gap: 2px; min-width: 0; width: 100%; }
.assistant-task-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-text); }
.assistant-task-meta { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 11px; color: var(--ob-text-subtle); }
.assistant-task-scope { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.assistant-task-state { flex: none; }
.assistant-task-state.is-running { color: var(--ob-blue); }
.assistant-task-state.is-unseen { color: var(--ob-success, #16a34a); }
.assistant-history-popper .is-current .assistant-task-title { color: var(--ob-blue); }
</style>
