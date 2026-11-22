<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import ReferenceEditor from "../../references/ReferenceEditor.vue";
import ReferencePlainText from "../../references/ReferencePlainText.vue";
import PendingInteractionSheet from "./PendingInteractionSheet.vue";
import WorkItemsDock from "./WorkItemsDock.vue";
import RunStatusLine from "./RunStatusLine.vue";
import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";
import ModelFeatureIcon from "./ModelFeatureIcon.vue";
import ContextUsageMeter from './ContextUsageMeter.vue';
import ExtensionsMenu from './ExtensionsMenu.vue';
import ImageOneTurnMenu from './ImageOneTurnMenu.vue';
import PersonaMenu from './PersonaMenu.vue';
import ComposerToolsMenu from './ComposerToolsMenu.vue';
import {Blocks, Coins, Eraser, ImageIcon, Layers, LayoutGrid, Clock3, Paperclip as PaperclipIcon, Plus as PlusIcon, UserRound} from "@lucide/vue";
import { userStorage } from '../../sessionBoundary.js';
import {
	ArrowDown,
	CircleCheck,
	Close,
	Document,
	Paperclip,
	Plus,
	Promotion,
	Search,
	Timer,
} from "@element-plus/icons-vue";
import {
	fmtBytes,
	fmtTokens,
	modelDefaultThinking,
	modelLabel,
	modelShortLabel,
	thinkingLabel,
} from "./display.js";

const props = defineProps({
	draft: {type: String, default: ""},
	conversationUuid: {type: String, default: ""},
	folderId: {type: String, default: ""},
	// The page assistant has its own system prompt, so a persona does not apply.
	personaAvailable: {type: Boolean, default: true},
	// A narrow panel: the actions keep their room and the run chip shortens.
	compact: {type: Boolean, default: false},
	pendingAttachments: {type: Array, default: () => []},
	attachmentPreviews: {type: Object, default: () => ({})},
	uploadProgress: {type: Object, default: () => ({})},
	pendingConfirmations: {type: Array, default: () => []},
	confirmationSubmitting: {type: Object, default: () => ({})},
	confirmationErrors: {type: Object, default: () => ({})},
	pendingSteering: {type: Array, default: () => []},
	modelMenuOpen: {type: Boolean, default: false},
	modelGroups: {type: Array, default: () => []},
	currentModel: {type: String, default: ""},
	currentModelInfo: {type: Object, default: null},
	currentThinkLevels: {type: Array, default: () => []},
	effectiveThinking: {type: String, default: "off"},
	supportsThinking: {type: Boolean, default: false},
	currentFast: {type: Boolean, default: false},
	fastSupported: {type: Boolean, default: false},
	agentModel: {type: String, default: ""},
	agentThinkLevel: {type: String, default: ""},
	agentFastMode: {default: null},
	agentEffectiveModel: {type: String, default: ""},
	agentEffectiveThinking: {type: String, default: "off"},
	agentEffectiveFast: {type: Boolean, default: false},
	agentThinkLevels: {type: Array, default: () => []},
	agentSupportsThinking: {type: Boolean, default: false},
	agentFastSupported: {type: Boolean, default: false},
	agentDefaultThinkingLabel: {type: String, default: "模型默认"},
	running: {type: Boolean, default: false},
	runStartedAt: {type: Number, default: 0},
	runStats: {type: Object, default: null},
	canSend: {type: Boolean, default: false},
	modelQuery: {type: String, default: ""},
	contextStrategy: {type: String, default: "sliding_window"},
	strategySaving: {type: Boolean, default: false},
	imageOneTurn: {type: Object, default: () => ({})},
	imageOneTurnSaving: {type: Boolean, default: false},
	promptTemplate: {type: String, default: ""},
	promptTemplateLocked: {type: Boolean, default: false},
	promptTemplateMissing: {type: Boolean, default: false},
	promptTemplateSaving: {type: Boolean, default: false},
	canCompact: {type: Boolean, default: false},
	compacting: {type: Boolean, default: false},
	contextDisplay: {type: String, default: "—"},
	contextUsage: {type: Object, default: () => ({known: false})},
	contextWindowTokens: {type: Number, default: 0},
	contextThresholdTokens: {type: Number, default: 0},
	contextUsedDisplay: {type: String, default: "—"},
	contextThresholdDisplay: {type: String, default: "—"},
	contextWindowDisplay: {type: String, default: "—"},
	contextPercentDisplay: {type: String, default: "—"},
	tokensText: {type: String, default: "0"},
	cachePercentText: {type: String, default: "—"},
	durationText: {type: String, default: "—"},
	costText: {type: String, default: "$0.0000"},
});
const emit = defineEmits([
	"update:draft",
	"update:modelQuery",
	"attachment-change",
	"remove-attachment",
	"clear-draft",
	"new-session",
	"toggle-model-menu",
	"select-model",
	"select-context-strategy",
	"update-image-one-turn",
	"update-prompt-template",
	"compact",
	"select-thinking",
	"toggle-fast-mode",
	"select-agent-model",
	"select-agent-thinking",
	"select-agent-fast",
	"send",
	"stop",
	"answer-confirmation",
	"close-menus",
	"height-change",
]);

function onComposerFocusOut() {
	scheduleRunConfigPosition();
}

const fileInput = ref(null);
const composerShell = ref(null);
const composerTextarea = ref(null);
const runConfigPopover = ref(null);
const runConfigPopperOptions = ref({});
const runConfigContent = ref(null);
const runConfigSearchInput = ref(null);
const runConfigCompact = ref(false);
const runConfigSettingsOpen = ref(false);
let runConfigFullChromeHeight = 0;
let composerResizeObserver = null;
let runConfigPositionFrame = 0;
const interactionSheet = ref(null);
const runConfigTab = ref("main"); // main | agent
watch(() => props.modelMenuOpen, (open) => { if (open) runConfigTab.value = "main"; });
const DEFAULT_WINDOW_TRIGGER_RATIO = 0.7;
const runConfigPopoverVisible = computed({
	get: () => props.modelMenuOpen,
	set: (value) => {
		if (value && !props.modelMenuOpen) emit("toggle-model-menu");
		else if (!value && props.modelMenuOpen) emit("close-menus");
	},
});
const runConfigModelText = computed(() => props.currentModelInfo ? modelShortLabel(props.currentModelInfo) : "模型");
const runConfigStrategyText = computed(() => props.contextStrategy === 'model_summary' ? '模型摘要' : '滑动窗口');
const runConfigMetaParts = computed(() => {
	const parts = [];
	if (props.supportsThinking) parts.push({key: 'thinking', text: compactThinkingLabel(props.effectiveThinking)});
	if (props.currentFast) parts.push({key: 'fast', text: 'Fast'});
	if (props.contextDisplay && props.contextDisplay !== "—") parts.push({key: 'context', text: props.contextDisplay});
	return parts;
});
const runConfigMetaText = computed(() => runConfigMetaParts.value.map(part => part.text).join(" · "));
const currentDefaultThinkingLabel = computed(() => {
	const level = modelDefaultThinking(props.currentModelInfo);
	return level ? thinkingLabel(level) : "无";
});
const agentFastTriState = computed(() => {
	if (props.agentFastMode === true) return "on";
	if (props.agentFastMode === false) return "off";
	return "follow";
});
const isAgentTab = computed(() => runConfigTab.value === "agent");
const menuThinkingLevels = computed(() => isAgentTab.value ? props.agentThinkLevels : props.currentThinkLevels);
const menuSupportsThinking = computed(() => isAgentTab.value ? props.agentSupportsThinking : props.supportsThinking);
const menuThinkingLevel = computed(() => isAgentTab.value ? props.agentThinkLevel : props.effectiveThinking);
const menuDefaultThinking = computed(() => compactThinkingLabel(isAgentTab.value ? props.agentDefaultThinkingLabel : currentDefaultThinkingLabel.value));
const menuSelectedModel = computed(() => isAgentTab.value ? props.agentModel : props.currentModel);

const runConfigThinkingBadge = computed(() => props.supportsThinking && props.effectiveThinking && props.effectiveThinking !== 'off'
	? compactThinkingLabel(props.effectiveThinking) : '');
const runConfigStatusLabel = computed(() => [
	runConfigThinkingBadge.value && `思考强度：${runConfigThinkingBadge.value}`,
	props.currentFast && 'Fast 模式已开启',
].filter(Boolean).join('，'));
function compactThinkingLabel(level) {
	return {off: '关闭', minimal: '极简', low: '低', medium: '中', high: '高', xhigh: '极高', max: '最高'}[level] || level || '默认';
}

function selectMenuModel(model) {
	if (isAgentTab.value) emit('select-agent-model', model.key);
	else emit('select-model', model);
}

function selectMenuThinking(level) {
	emit(isAgentTab.value ? 'select-agent-thinking' : 'select-thinking', level);
}

const runConfigSettingsSummary = computed(() => {
	const thinking = menuSupportsThinking.value
		? `思考 ${isAgentTab.value && !props.agentThinkLevel ? '跟随' : compactThinkingLabel(menuThinkingLevel.value)}`
		: '思考未声明';
	const fast = isAgentTab.value
		? `Fast ${{follow: '跟随', on: '开', off: '关'}[agentFastTriState.value]}`
		: `Fast ${props.fastSupported ? (props.currentFast ? '开' : '关') : '不支持'}`;
	return `${thinking} · ${fast} · ${runConfigStrategyText.value}`;
});

function toggleRunConfigSettings() {
	runConfigSettingsOpen.value = !runConfigSettingsOpen.value;
	runConfigSearchInput.value?.blur?.();
	scheduleRunConfigPosition();
}

function finishRunConfigSearch() {
	runConfigSearchInput.value?.blur?.();
	emit('close-menus');
}

function modelTags(model) {
	const tags = [];
	const trigger = rolloverTriggerForModel(model);
	if (trigger > 0) {
		tags.push({id: 'compression', type: 'number', label: `${fmtTokens(trigger)} 压缩`});
	}
	if (model?.maxTokens) {
		tags.push({id: 'output', type: 'number', label: `${fmtTokens(model.maxTokens)} 输出`});
	}
	if (model?.reasoning) {
		tags.push({id: 'reasoning', type: 'feature', icon: 'brain', label: '思考'});
	}
	if (model?.supportsFast) {
		tags.push({id: 'fast', type: 'feature', icon: 'zap', label: 'Fast'});
	}
	return tags;
}

const contextPercentNumber = computed(() => {
	const value = Number(String(props.contextPercentDisplay || "").replace("%", ""));
	return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
});
const runConfigContextRingClass = computed(() => ({
	'is-unknown': !Number.isFinite(Number.parseFloat(props.contextPercentDisplay)),
	'is-warning': contextPercentNumber.value >= 85 && contextPercentNumber.value < 100,
	'is-danger': contextPercentNumber.value >= 100,
}));

function rolloverTriggerForModel(model) {
	const explicit = Number(model?.rolloverTriggerTokens || 0);
	if (explicit > 0) return explicit;
	const ratio = Number(model?.windowTriggerRatio || DEFAULT_WINDOW_TRIGGER_RATIO);
	return Math.round(Number(model?.contextWindow || 0) * ratio);
}

function openFilePicker() {
	fileInput.value?.click?.();
}

function onAttachmentChange(event) {
	const files = Array.from(event.target?.files || []);
	if (files.length) emit("attachment-change", files);
	if (event.target) event.target.value = "";
}

function attachmentPreviewUrl(item) {
	return props.attachmentPreviews?.[item?.id] || "";
}

function pendingImagePreviewList() {
	return props.pendingAttachments
		.map((item) => attachmentPreviewUrl(item))
		.filter(Boolean);
}

function pendingImagePreviewIndex(item) {
	const src = attachmentPreviewUrl(item);
	return Math.max(0, pendingImagePreviewList().indexOf(src));
}

function openPendingImagePreview(item) {
	const urlList = pendingImagePreviewList();
	if (!urlList.length) return;
	window.dispatchEvent?.(new CustomEvent("openbear:preview-images", {
		detail: {urlList, initialIndex: pendingImagePreviewIndex(item)},
	}));
}

function extensionFromMime(type = "") {
	const subtype = String(type || "").split("/")[1] || "";
	if (!subtype) return "bin";
	return subtype.split(";")[0].replace(/^svg\+xml$/, "svg").replace(/[^a-z0-9.+-]/gi, "") || "bin";
}

function normalizePastedFile(file, index) {
	if (!file) return null;
	if (file.name) return file;
	const prefix = String(file.type || "").startsWith("image/") ? "pasted-image" : "pasted-file";
	const name = `${prefix}-${Date.now()}-${index + 1}.${extensionFromMime(file.type)}`;
	return new File([file], name, {type: file.type || "application/octet-stream", lastModified: file.lastModified || Date.now()});
}

function clipboardFiles(event) {
	const clipboard = event.clipboardData;
	if (!clipboard) return [];
	const fromItems = Array.from(clipboard.items || [])
		.filter((item) => item.kind === "file")
		.map((item) => item.getAsFile?.())
		.filter(Boolean)
		.map(normalizePastedFile)
		.filter(Boolean);
	if (fromItems.length) return fromItems;
	return Array.from(clipboard.files || []).map(normalizePastedFile).filter(Boolean);
}

function insertPlainTextFromPaste(event) {
	const text = event.clipboardData?.getData?.("text/plain") || "";
	if (!text) return;
	const el = composerTextarea.value;
	if (typeof el?.insertText === "function") { el.insertText(text); return; }
	const value = String(props.draft || "");
	const start = Number(el?.selectionStart ?? value.length);
	const end = Number(el?.selectionEnd ?? start);
	const next = `${value.slice(0, start)}${text}${value.slice(end)}`;
	emit("update:draft", next);
	nextTick(() => {
		try {
			el?.setSelectionRange?.(start + text.length, start + text.length);
		} catch {
		}
		adjustHeight();
	});
}

function onPaste(event) {
	const files = clipboardFiles(event);
	if (!files.length) return;
	event.preventDefault();
	emit("attachment-change", files);
	insertPlainTextFromPaste(event);
}

function adjustHeight() {
	nextTick(() => {
		const el = composerTextarea.value;
		if (!el) return;
		if (typeof el.adjustHeight === "function") { el.adjustHeight(); return; }
		el.style.height = "auto";
		el.style.height = `${Math.min(el.scrollHeight, 216)}px`;
	});
}

async function focus() {
	await nextTick();
	const el = composerTextarea.value;
	if (!el) return;
	try {
		el.focus({preventScroll: true});
	} catch {
		el.focus?.();
	}
	const end = String(el.value || "").length;
	try {
		el.setSelectionRange(end, end);
	} catch {
	}
}

function onDraftInput(event) {
	emit("update:draft", event.target?.value || "");
	adjustHeight();
}

function clearDraft() {
	emit("update:draft", "");
	emit("clear-draft");
	adjustHeight();
}

function onAnswerConfirmation(item, payload) {
	emit("answer-confirmation", item, payload);
}

// A phone gets one tools button instead of the row of actions, so the model keeps its room.
const phone = ref(false);
let phoneMedia = null;
function updatePhone() { phone.value = Boolean(phoneMedia?.matches); }
const imageMenu = ref(null), extensionsMenu = ref(null), personaMenu = ref(null);
const toolItems = computed(() => {
	const draftEmpty = !props.draft && !props.pendingAttachments.length;
	return [
		{key: "new", label: "新话题", icon: PlusIcon},
		{key: "attach", label: "上传图片或附件", icon: PaperclipIcon},
		{key: "image", label: "图片只看一轮", hint: imageMenu.value?.summary || "", icon: ImageIcon, on: Boolean(props.imageOneTurn?.enabled), more: true},
		{key: "extensions", label: "扩展", hint: extensionsMenu.value?.summary || "", icon: Blocks, more: true},
		...(props.personaAvailable ? [{key: "persona", label: "人设", hint: personaMenu.value?.summary || "", icon: UserRound, on: Boolean(props.promptTemplate), attention: Boolean(props.promptTemplateMissing || personaMenu.value?.attention), more: true}] : []),
		...(props.contextStrategy === "model_summary"
			? [{key: "compact", label: props.compacting ? "正在压缩上下文…" : "手动压缩上下文", icon: ContextCompactionIcon, disabled: !props.canCompact || props.compacting}] : []),
		...(props.running || draftEmpty || props.compact ? [] : [{key: "clear", label: "清空草稿", icon: Eraser}]),
	];
});
const toolsMarked = computed(() => Boolean(props.imageOneTurn?.enabled || props.promptTemplate));
function onToolSelect(key) {
	if (key === "new") emit("new-session");
	else if (key === "attach") openFilePicker();
	else if (key === "compact") emit("compact");
	else if (key === "clear") clearDraft();
	else {
		const menu = {image: imageMenu, extensions: extensionsMenu, persona: personaMenu}[key];
		nextTick(() => menu?.value?.show());
	}
}

function handleKeydown(event) {
	if (event.key !== "Enter") return;
	if (event.shiftKey) return;
	if (event.isComposing) return;
	event.preventDefault();
	if (!props.canSend) return;
	emit("send");
}

function onComposerKeydownCapture(event) {
	// ReferenceEditor normally sends on Enter. Intercept only touch-keyboard Enter
	// before its ProseMirror handler; keep desktop shortcuts and IME untouched.
	if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey ||
		event.isComposing || event.keyCode === 229 ||
		!window.matchMedia?.("(hover: none) and (pointer: coarse)")?.matches) return;
	const editor = composerTextarea.value?.editor;
	if (!editor || editor.view?.composing || !event.target?.closest?.(".reference-editor-content")) return;
	// Let the editor handle Enter when choosing an inline reference suggestion.
	if (document.querySelector('.reference-floating-panel[aria-label="选择引用内容"]')) return;
	event.preventDefault();
	event.stopPropagation();
	editor.commands.insertContent({type: "hardBreak"});
}

function syncRunConfigLayout(mobile) {
	if (!mobile) {
		runConfigCompact.value = false;
		runConfigSettingsOpen.value = false;
		return;
	}
	const content = runConfigContent.value;
	if (!content?.clientHeight) return;
	// Measure the complete, normal chrome once before collapsing it. Its cached
	// footprint must not depend on the compact view, or resize would oscillate.
	if (!runConfigCompact.value) {
		const children = [...content.children].filter(child => window.getComputedStyle(child).display !== 'none');
		const gap = parseFloat(window.getComputedStyle(content).rowGap) || 0;
		runConfigFullChromeHeight = children
			.filter(child => !child.classList.contains('run-config-model-section'))
			.reduce((height, child) => height + child.getBoundingClientRect().height, 0)
			+ Math.max(0, children.length - 1) * gap;
	}
	const capacity = parseFloat(window.getComputedStyle(content).maxHeight)
		|| Math.min(600, (window.visualViewport?.height || window.innerHeight) - 48);
	// Keep room for a readable result, rather than shrinking the list to zero.
	const compact = capacity < runConfigFullChromeHeight + 96;
	runConfigCompact.value = compact;
	if (!compact) runConfigSettingsOpen.value = false;
}

function scheduleRunConfigPosition() {
	if (!props.modelMenuOpen || runConfigPositionFrame) return;
	runConfigPositionFrame = window.requestAnimationFrame(() => {
		runConfigPositionFrame = 0;
		if (!props.modelMenuOpen) return;
		// A body-teleported popover must use the visual viewport, not follow its
		// anchor offscreen while iOS pans/resizes the viewport for the keyboard.
		const mobile = window.matchMedia?.('(max-width: 760px), (hover: none) and (pointer: coarse)')?.matches;
		const strategy = mobile ? 'fixed' : 'absolute';
		if (runConfigPopperOptions.value.strategy !== strategy) {
			runConfigPopperOptions.value = mobile ? {
				strategy,
				modifiers: [
					{name: 'preventOverflow', options: {altAxis: true, tether: false, padding: 12}},
				],
			} : {strategy};
		}
		syncRunConfigLayout(mobile);
		nextTick(() => {
			if (props.modelMenuOpen) runConfigPopover.value?.popperRef?.popperInstanceRef?.update?.();
		});
	});
}

watch(runConfigContent, (content, _, onCleanup) => {
	if (!content || typeof ResizeObserver === 'undefined') return;
	const observer = new ResizeObserver(scheduleRunConfigPosition);
	observer.observe(content);
	onCleanup(() => observer.disconnect());
	nextTick(scheduleRunConfigPosition);
});

watch(() => props.modelMenuOpen, (open, _, onCleanup) => {
	runConfigCompact.value = false;
	runConfigSettingsOpen.value = false;
	runConfigFullChromeHeight = 0;
	if (!open || typeof window === "undefined") return;
	const viewport = window.visualViewport;
	for (const target of [window, viewport]) {
		target?.addEventListener("resize", scheduleRunConfigPosition);
		target?.addEventListener("scroll", scheduleRunConfigPosition);
	}
	nextTick(scheduleRunConfigPosition);
	onCleanup(() => {
		for (const target of [window, viewport]) {
			target?.removeEventListener("resize", scheduleRunConfigPosition);
			target?.removeEventListener("scroll", scheduleRunConfigPosition);
		}
		if (runConfigPositionFrame) window.cancelAnimationFrame(runConfigPositionFrame);
		runConfigPositionFrame = 0;
	});
}, {flush: "post", immediate: true});

onMounted(() => {
	phoneMedia = window.matchMedia?.("(max-width: 760px)") || null;
	updatePhone();
	phoneMedia?.addEventListener?.("change", updatePhone);
	const el = composerShell.value;
	if (!el) return;
	const notifyHeight = () => {
		emit("height-change", Math.ceil(el.getBoundingClientRect().height));
		scheduleRunConfigPosition();
	};
	notifyHeight();
	if (typeof ResizeObserver !== "undefined") {
		composerResizeObserver = new ResizeObserver(notifyHeight);
		composerResizeObserver.observe(el);
	}
});

onBeforeUnmount(() => {
	cancelStopShapePress();
	phoneMedia?.removeEventListener?.("change", updatePhone);
	composerResizeObserver?.disconnect();
	composerResizeObserver = null;
});

const STOP_BUTTON_SHAPE_KEY = 'openbear.console.stopButtonShape.v1';
function readStopButtonShape() {
	try { return userStorage(window.localStorage).getItem(STOP_BUTTON_SHAPE_KEY) === 'circle' ? 'circle' : 'square'; }
	catch { return 'square'; }
}
// Read before the first render; send and stop share the same persisted shape,
// including the idle frame before the conversation's running state is restored.
const stopButtonShape = ref(readStopButtonShape());
let stopShapePress = null;
let stopShapeTimer = null;
let suppressStopClick = false;
function toggleStopButtonShape() {
	stopButtonShape.value = stopButtonShape.value === 'circle' ? 'square' : 'circle';
	try { userStorage(window.localStorage).setItem(STOP_BUTTON_SHAPE_KEY, stopButtonShape.value); } catch { /* Private-mode storage can be unavailable. */ }
}
function cancelStopShapePress() {
	if (stopShapeTimer !== null) window.clearTimeout(stopShapeTimer);
	stopShapeTimer = null;
	stopShapePress = null;
}
function startStopShapePress(event) {
	// A secondary finger must not reset an already-consumed long press. Desktop
	// right-click still starts a fresh gesture before its contextmenu event.
	if (event.isPrimary === false || (event.button > 0 && !(event.pointerType === 'mouse' && event.button === 2))) return;
	cancelStopShapePress();
	suppressStopClick = false;
	if (event.pointerType !== 'touch' || event.button > 0) return;
	stopShapePress = {id: event.pointerId, x: event.clientX, y: event.clientY};
	stopShapeTimer = window.setTimeout(() => {
		stopShapeTimer = null;
		if (!stopShapePress || suppressStopClick) return;
		suppressStopClick = true;
		toggleStopButtonShape();
	}, 550);
}
function moveStopShapePress(event) {
	if (!stopShapePress || stopShapePress.id !== event.pointerId) return;
	if (Math.hypot(event.clientX - stopShapePress.x, event.clientY - stopShapePress.y) > 10) {
		suppressStopClick = true;
		cancelStopShapePress();
	}
}
function endStopShapePress(event) {
	if (stopShapePress && stopShapePress.id !== event.pointerId) return;
	cancelStopShapePress();
}
function stopButtonContextMenu() {
	// Some touch browsers dispatch contextmenu before or after our long-press
	// timer. Whichever arrives first owns the single toggle, never a stop.
	if (!suppressStopClick) toggleStopButtonShape();
	suppressStopClick = true;
	cancelStopShapePress();
}
function consumeShapeClick(event) {
	if (!suppressStopClick) return false;
	// Touch-generated clicks can have detail=0 and omit pointerType. A shape
	// gesture consumes that click too; real keyboard activation resets below.
	suppressStopClick = false;
	event.preventDefault();
	return true;
}
function clickStopButton(event) {
	if (!consumeShapeClick(event)) emit('stop');
}
function clickSendButton(event) {
	if (!consumeShapeClick(event) && props.canSend) emit('send');
}
function buttonShapeKeydown(event) {
	if (event.key !== 'Enter' && event.key !== ' ') return;
	cancelStopShapePress();
	suppressStopClick = false;
}
function cancelButtonShapeInteraction(event) {
	if (event && stopShapePress && event.pointerId !== stopShapePress.id) return;
	if (stopShapePress) suppressStopClick = true;
	cancelStopShapePress();
}
watch(() => [props.conversationUuid, props.running, Boolean(props.draft.trim()), props.canSend], () => cancelButtonShapeInteraction());

function focusInteraction(interactionId) {
	return Boolean(interactionSheet.value?.focusInteraction?.(interactionId));
}

function getReferenceOrder() { return composerTextarea.value?.getReferenceOrder?.() || []; }
defineExpose({focus, adjustHeight, openFilePicker, focusInteraction, getReferenceOrder});
</script>

<template>
	<footer ref="composerShell" class="composer-shell shrink-0 pb-3 pt-5" :class="{'is-compact': props.compact}">
		<div class="composer-content pointer-events-auto relative mx-auto">
			<div v-if="props.pendingSteering.length" class="steering-queue-card">
				<div class="steering-queue-title">
					<Timer/>
					待处理插话队列
				</div>
				<div class="steering-queue-hint">当前运行到轮次边界后，后端会取走这些内容并作为一条 user message 注入模型。</div>
				<div class="steering-queue-items">
					<div v-for="item in props.pendingSteering" :key="item.id" class="steering-queue-item"><ReferencePlainText :text="item.text" :references="item.references || []" :bundle-id="item.referenceBundleId || ''"/><div v-if="item.attachments?.length" class="steering-queue-attachments">附件：{{ item.attachments.join("、") }}</div></div>
				</div>
			</div>

			<PendingInteractionSheet
				ref="interactionSheet"
				:items="props.pendingConfirmations"
				:submitting="props.confirmationSubmitting"
				:errors="props.confirmationErrors"
				:phone="phone"
				:compact="props.compact"
				@answer="onAnswerConfirmation"
			/>

			<RunStatusLine v-if="props.running" :started-at="props.runStartedAt" :stats="props.runStats"/>

			<div class="composer-box">
				<input ref="fileInput" type="file" multiple class="hidden" @change="onAttachmentChange"/>
				<WorkItemsDock/>
				<div v-if="props.pendingAttachments.length" class="attachment-strip">
					<div v-for="item in props.pendingAttachments" :key="item.id" class="attachment-card group">
						<el-image v-if="attachmentPreviewUrl(item)" class="attachment-thumb"
						          :src="attachmentPreviewUrl(item)"
						          :alt="item.file.name"
						          fit="cover"
						          @click="openPendingImagePreview(item)"/>
						<div v-else class="attachment-file-tile">
							<Document class="file-preview-icon"/>
							<span>{{ item.file.name }}</span>
							<small>{{ fmtBytes(item.file.size) }}</small>
						</div>
						<div v-if="props.uploadProgress[item.id]" class="attachment-upload-state"
						     :class="`is-${props.uploadProgress[item.id].phase}`"
						     role="status" :aria-label="`${item.file.name}：${props.uploadProgress[item.id].phase === 'failed' ? '上传失败，附件已保留' : props.uploadProgress[item.id].phase === 'finalizing' ? '保存中' : props.uploadProgress[item.id].phase === 'complete' ? '上传完成，等待发送确认' : `上传 ${props.uploadProgress[item.id].percent}%`}`">
							<span>{{ props.uploadProgress[item.id].phase === 'failed' ? '失败 · 已保留' : props.uploadProgress[item.id].phase === 'finalizing' ? '保存中' : props.uploadProgress[item.id].phase === 'complete' ? '待确认' : `${props.uploadProgress[item.id].percent}%` }}</span>
							<div class="attachment-upload-track"><div :style="{width: `${props.uploadProgress[item.id].percent}%`}"/></div>
						</div>
						<el-tooltip content="移除附件" placement="top" :show-after="260">
							<button type="button" class="attachment-remove" :aria-label="`移除附件：${item.file.name}`"
							        @click.stop="emit('remove-attachment', item.id)">
								<Close/>
							</button>
						</el-tooltip>
					</div>
				</div>
				<ReferenceEditor
					ref="composerTextarea"
					:model-value="props.draft"
					:current-conversation="props.conversationUuid"
					placeholder="在这里输入消息"
					@update:model-value="emit('update:draft', $event)"
					@send="props.canSend && emit('send')"
					@keydown.capture="onComposerKeydownCapture"
					@focusout="onComposerFocusOut"
					@paste="onPaste"
				/>
				<div class="composer-toolbar">
					<div class="composer-actions relative flex min-w-0 flex-1 items-center gap-1.5">
						<ComposerToolsMenu v-if="phone" :items="toolItems" :conversation-uuid="props.conversationUuid" @select="onToolSelect">
							<template #trigger="{open}">
								<button type="button" class="tool-btn composer-tools-trigger" :class="{'tool-btn-active': open}" aria-label="功能" aria-haspopup="dialog" :aria-expanded="open">
									<LayoutGrid :stroke-width="1.5"/>
									<span v-if="toolsMarked" class="persona-menu-mark composer-tools-mark" aria-hidden="true"></span>
								</button>
							</template>
						</ComposerToolsMenu>
						<template v-else>
							<el-tooltip content="新话题（Ctrl+N）" placement="top" :show-after="260">
								<button type="button" class="tool-btn" aria-label="新话题（Ctrl+N）" @click="emit('new-session')">
									<Plus/>
								</button>
							</el-tooltip>
							<el-tooltip content="上传图片或附件" placement="top" :show-after="260">
								<button type="button" class="tool-btn tool-btn-accent" aria-label="上传图片或附件"
								        @click="openFilePicker">
									<Paperclip/>
								</button>
							</el-tooltip>
						</template>
						<ImageOneTurnMenu ref="imageMenu" :value="props.imageOneTurn" :saving="props.imageOneTurnSaving" :draft="String(props.conversationUuid || '').startsWith('local:')"
						                  :conversation-uuid="props.conversationUuid" @change="emit('update-image-one-turn', $event)">
							<template #trigger="{label, open, on}">
								<el-tooltip v-if="!phone" :content="label" placement="top" :show-after="260" :disabled="open">
									<button type="button" class="tool-btn image-one-turn-trigger" :class="{'tool-btn-active': open, 'is-on': on}" :aria-label="label" aria-haspopup="dialog" :aria-expanded="open">
										<ImageIcon :stroke-width="2"/>
										<span v-if="on" class="image-one-turn-mark" aria-hidden="true">1</span>
									</button>
								</el-tooltip>
							</template>
						</ImageOneTurnMenu>
						<ExtensionsMenu ref="extensionsMenu" :conversation-uuid="props.conversationUuid" :folder-id="props.folderId" :running="props.running">
							<template #trigger="{label, open}">
								<el-tooltip v-if="!phone" :content="label" placement="top" :show-after="260" :disabled="open">
									<button type="button" class="tool-btn" :class="{'tool-btn-active': open}" :aria-label="label" aria-haspopup="dialog" :aria-expanded="open"><Blocks :stroke-width="1.5"/></button>
								</el-tooltip>
							</template>
						</ExtensionsMenu>
						<PersonaMenu v-if="props.personaAvailable" ref="personaMenu" :value="props.promptTemplate" :locked="props.promptTemplateLocked" :missing="props.promptTemplateMissing" :saving="props.promptTemplateSaving"
						             :draft="String(props.conversationUuid || '').startsWith('local:')" :conversation-uuid="props.conversationUuid"
						             @change="emit('update-prompt-template', $event)">
							<template #trigger="{label, open, on, attention}">
								<el-tooltip v-if="!phone" :content="label" placement="top" :show-after="260" :disabled="open">
									<button type="button" class="tool-btn persona-menu-trigger" :class="{'tool-btn-active': open, 'is-on': on}" :aria-label="label" aria-haspopup="dialog" :aria-expanded="open">
										<UserRound :stroke-width="1.5"/>
										<span v-if="on || attention" class="persona-menu-mark" :class="{'is-attention': attention}" aria-hidden="true">{{ attention ? "!" : "" }}</span>
									</button>
								</el-tooltip>
							</template>
						</PersonaMenu>

						<el-tooltip v-if="!phone && props.contextStrategy === 'model_summary'" :content="props.compacting ? '正在压缩上下文…' : '手动压缩上下文'" placement="top" :show-after="260">
							<button type="button" class="tool-btn" aria-label="手动压缩上下文" :disabled="!props.canCompact || props.compacting" @click="emit('compact')"><ContextCompactionIcon :class="{'context-compacting-icon': props.compacting}" /></button>
						</el-tooltip>
						<el-tooltip v-if="!phone && !props.compact" content="清空草稿" placement="top" :show-after="260">
							<button type="button" class="tool-btn composer-clear" aria-label="清空草稿"
							        :disabled="props.running || (!props.draft && !props.pendingAttachments.length)"
							        @click="clearDraft">
								<Close/>
							</button>
						</el-tooltip>
					</div>
					<div class="composer-status">
						<el-popover ref="runConfigPopover" v-model:visible="runConfigPopoverVisible"
						            popper-class="composer-menu-popper run-config-menu-popper"
						            placement="top-end"
						            :popper-options="runConfigPopperOptions"
						            trigger="click"
						            :width="'min(26rem, calc(100vw - 2rem))'"
						            :show-arrow="false"
						            :hide-after="0">
							<template #reference>
								<button type="button" class="status-chip run-config-chip" aria-label="运行配置" :aria-description="[`上下文压缩：${runConfigStrategyText}`, runConfigStatusLabel].filter(Boolean).join('，')"
								        :class="props.modelMenuOpen ? 'status-chip-active' : ''">
									<span class="run-config-chip-main">
										<span class="run-config-chip-model">{{ runConfigModelText }}</span>
										<span v-if="runConfigStatusLabel" class="run-config-chip-status" aria-hidden="true">
											<span v-if="runConfigThinkingBadge" class="run-config-status-thinking">{{ runConfigThinkingBadge }}</span>
											<span v-if="props.currentFast" class="run-config-status-fast"><ModelFeatureIcon name="zap"/></span>
										</span>
										<span v-if="runConfigMetaParts.length" class="run-config-chip-meta"><span v-for="part in runConfigMetaParts" :key="part.key" class="run-config-meta-part" :class="part.key === 'fast' ? 'run-config-meta-fast' : ''" :title="part.key === 'fast' ? 'Fast 模式' : undefined"><ModelFeatureIcon v-if="part.key === 'fast'" name="zap"/><template v-else><svg v-if="part.key === 'context'" class="run-config-context-ring" :class="runConfigContextRingClass" viewBox="0 0 24 24" aria-hidden="true"><circle class="run-config-context-ring-track" cx="12" cy="12" r="9"/><circle class="run-config-context-ring-fill" cx="12" cy="12" r="9" pathLength="100" :stroke-dasharray="`${contextPercentNumber} 100`"/></svg>{{ part.text }}</template></span></span>
									</span>
									<span class="run-config-chip-strategy" :aria-label="`上下文压缩：${runConfigStrategyText}${Number(props.contextUsage?.compactionCount) > 0 ? `，已压缩 ${Math.floor(Number(props.contextUsage.compactionCount))} 次` : ''}`">{{ (props.contextStrategy === 'model_summary' ? '摘要压缩' : '滑窗压缩') + (Number(props.contextUsage?.compactionCount) > 0 ? `·${Math.floor(Number(props.contextUsage.compactionCount))}` : '') }}</span>
									<ArrowDown class="chip-caret"/>
								</button>
							</template>
							<div ref="runConfigContent" class="popover-menu-content run-config-popover" :class="{'is-compact': runConfigCompact}">
								<div class="run-config-tabs" role="tablist" aria-label="运行配置切换">
									<button type="button" role="tab" :aria-selected="runConfigTab === 'main'" :class="runConfigTab === 'main' ? 'is-active' : ''" @click="runConfigTab = 'main'">主会话</button>
									<button type="button" role="tab" :aria-selected="runConfigTab === 'agent'" :class="runConfigTab === 'agent' ? 'is-active' : ''" @click="runConfigTab = 'agent'">Agent</button>
								</div>
								<p v-if="props.running" v-show="!runConfigCompact" class="run-config-notice">{{ isAgentTab ? '模型与执行设置用于新 Agent' : '主模型在下一轮运行生效' }}</p>
								<label v-show="!runConfigCompact || !runConfigSettingsOpen" class="model-search run-config-search">
									<Search/>
									<input ref="runConfigSearchInput" :value="props.modelQuery" type="search" :placeholder="isAgentTab ? '搜索 Agent 模型' : '搜索模型'" :aria-label="isAgentTab ? '搜索 Agent 模型' : '搜索模型'" @input="emit('update:modelQuery', $event.target.value)"/>
								</label>
								<div v-show="!runConfigCompact || !runConfigSettingsOpen" class="run-config-model-section">
								<div class="model-list run-config-model-list">
									<div v-if="isAgentTab" class="agent-follow-group">
										<button type="button" class="model-row follow-model-row" :class="!props.agentModel ? 'is-selected' : ''" :aria-pressed="!props.agentModel" @click="emit('select-agent-model', '')">
											<div class="model-row-main">
												<div class="model-row-title-row">
													<span class="model-row-name">跟随主模型</span>
												</div>
												<div class="model-row-tags">
													<span class="model-tag model-follow-detail">{{ runConfigModelText }}</span>
												</div>
											</div>
											<span class="model-selection-mark"><CircleCheck v-if="!props.agentModel" class="model-selected-icon"/></span>
										</button>
									</div>
									<section v-for="group in props.modelGroups" :key="group.provider" class="model-group" :aria-label="group.provider">
										<div class="model-group-title">{{ group.provider }}</div>
										<div v-for="model in group.models" :key="model.key" class="model-row" :class="menuSelectedModel === model.key ? 'is-selected' : ''" @click="selectMenuModel(model)">
											<button type="button" class="model-select" :aria-pressed="menuSelectedModel === model.key" :aria-label="`${modelLabel(model)}（${model.key}）`" @click.stop="selectMenuModel(model)">
												<div class="model-row-main">
													<div class="model-row-title-row">
														<span class="model-row-name">{{ modelLabel(model) }}</span>
													</div>
													<div v-if="modelTags(model).length" class="model-row-tags">
														<span v-for="tag in modelTags(model)" :key="tag.id" class="model-tag" :class="tag.type === 'feature' ? 'model-tag-feature' : ''">
															<ModelFeatureIcon v-if="tag.icon" :name="tag.icon"/>
															<span>{{ tag.label }}</span>
														</span>
													</div>
												</div>
											</button>
											<span class="model-selection-mark"><CircleCheck v-if="menuSelectedModel === model.key" class="model-selected-icon"/></span>
										</div>
									</section>
									<div v-if="!props.modelGroups.length" class="model-empty">没有匹配模型</div>
								</div>
								</div>
								<div v-show="!runConfigCompact || runConfigSettingsOpen" class="run-config-controls">
									<div class="thinking-control">
										<div class="run-config-control-head"><span class="config-label"><ModelFeatureIcon name="brain"/>思考强度</span><span class="config-hint">{{ menuSupportsThinking ? (isAgentTab ? (props.agentThinkLevel ? '未跟随主会话' : `跟随：${compactThinkingLabel(props.agentEffectiveThinking)}`) : `默认 ${menuDefaultThinking}`) : '未声明支持' }}</span></div>
										<div v-if="menuSupportsThinking || isAgentTab" class="thinking-segments" role="group" aria-label="思考强度">
											<el-tooltip v-if="isAgentTab" :content="`跟随主会话思考强度，当前为${compactThinkingLabel(props.agentEffectiveThinking)}`" placement="top" :show-after="400">
												<button type="button" :class="!props.agentThinkLevel ? 'is-active' : ''" :aria-pressed="!props.agentThinkLevel" @click="selectMenuThinking('')">跟随</button>
											</el-tooltip>
											<el-tooltip v-for="level in menuThinkingLevels" :key="level" :content="`思考强度：${level}`" placement="top" :show-after="400">
												<button type="button" :class="menuThinkingLevel === level ? 'is-active' : ''" :aria-pressed="menuThinkingLevel === level" @click="selectMenuThinking(level)">{{ compactThinkingLabel(level) }}</button>
											</el-tooltip>
										</div>
									</div>
									<div v-if="!isAgentTab" class="fast-control" :class="{'is-disabled': !props.fastSupported}">
										<span class="config-label"><ModelFeatureIcon name="zap"/>Fast 模式</span>
										<div class="config-value"><span v-if="!props.fastSupported" class="config-hint">不支持</span>
											<button type="button" class="fast-switch" role="switch" aria-label="Fast 模式" :aria-checked="props.currentFast" :class="props.currentFast ? 'is-on' : ''" :disabled="!props.fastSupported" @click="emit('toggle-fast-mode')"><span></span></button>
										</div>
									</div>
									<div v-else class="fast-control">
										<el-tooltip :content="props.agentFastSupported ? `跟随主会话时当前为${props.currentFast ? '开启' : '关闭'}` : '当前 Agent 模型未声明 Fast 能力'" placement="top" :show-after="400"><span class="config-label"><ModelFeatureIcon name="zap"/>Fast 模式</span></el-tooltip>
										<div class="thinking-segments agent-fast-segments" role="group" aria-label="Agent Fast 模式">
											<button type="button" :class="agentFastTriState === 'follow' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'follow'" aria-label="Fast 跟随主会话" @click="emit('select-agent-fast', null)">跟随</button>
											<button type="button" :class="agentFastTriState === 'on' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'on'" :disabled="!props.agentFastSupported" aria-label="开启 Agent Fast" @click="emit('select-agent-fast', true)">开</button>
											<button type="button" :class="agentFastTriState === 'off' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'off'" aria-label="关闭 Agent Fast" @click="emit('select-agent-fast', false)">关</button>
										</div>
									</div>
									<div class="fast-control context-strategy-control">
										<el-tooltip content="主会话与 Agent 共用；关闭使用滑动窗口，开启使用模型摘要" placement="top" :show-after="260"><span class="config-label"><ModelFeatureIcon name="compression"/>上下文压缩</span></el-tooltip>
										<div class="context-strategy-value config-value">
											<span class="config-hint" aria-live="polite">{{ props.contextStrategy === 'model_summary' ? '模型摘要' : '滑动窗口' }}</span>
											<button type="button" class="fast-switch" role="switch" aria-label="使用模型摘要（关闭为滑动窗口）" :aria-checked="props.contextStrategy === 'model_summary'" :class="props.contextStrategy === 'model_summary' ? 'is-on' : ''" :disabled="props.strategySaving" @click="emit('select-context-strategy', props.contextStrategy === 'model_summary' ? 'sliding_window' : 'model_summary')"><span></span></button>
										</div>
									</div>
								</div>
								<div v-if="runConfigCompact" class="run-config-compact-footer">
									<div class="run-config-compact-summary" aria-live="polite">
										<span>{{ runConfigSettingsSummary }}</span>
										<span v-if="props.running">{{ isAgentTab ? '用于新 Agent' : '下一次调用生效' }}</span>
									</div>
									<div class="run-config-compact-actions">
										<button type="button" :aria-expanded="runConfigSettingsOpen" @click="toggleRunConfigSettings">{{ runConfigSettingsOpen ? '返回模型' : '设置' }}</button>
										<button type="button" @click="finishRunConfigSearch">完成</button>
									</div>
								</div>
							</div>
						</el-popover>
						<el-tooltip v-if="props.running && !props.draft.trim() && !props.pendingAttachments.length" content="停止生成（右键或长按切换按钮形状）" placement="top" :show-after="260">
							<button type="button" class="send-button stop-button" :class="{'is-round': stopButtonShape === 'circle'}" aria-label="停止生成" aria-description="右键单击或长按切换方形与圆形，偏好保存在当前浏览器" @click="clickStopButton" @contextmenu.prevent="stopButtonContextMenu" @pointerdown="startStopShapePress" @pointermove="moveStopShapePress" @pointerup="endStopShapePress" @pointercancel="cancelButtonShapeInteraction" @pointerleave="cancelButtonShapeInteraction" @keydown="buttonShapeKeydown">
								<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
									<rect v-if="stopButtonShape === 'circle'" x="5" y="5" width="14" height="14" rx="1.5"/>
									<rect v-else x="4" y="4" width="16" height="16" rx="3"/>
								</svg>
							</button>
						</el-tooltip>
						<template v-else>
							<button type="button" class="send-button" :class="{'is-round': stopButtonShape === 'circle'}" aria-label="发送消息（桌面 Enter；触屏 Ctrl 或 Command 加 Enter）" aria-description="右键单击或长按切换方形与圆形，空输入也可切换；偏好保存在当前浏览器" :aria-disabled="!props.canSend"
							        @click="clickSendButton" @contextmenu.prevent="stopButtonContextMenu" @pointerdown="startStopShapePress" @pointermove="moveStopShapePress" @pointerup="endStopShapePress" @pointercancel="cancelButtonShapeInteraction" @pointerleave="cancelButtonShapeInteraction" @keydown="buttonShapeKeydown">
								<Promotion v-if="stopButtonShape !== 'circle'"/>
								<svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20V4M5 11l7-7 7 7"/></svg>
							</button>
						</template>
					</div>
				</div>
			</div>
			<div class="composer-usage-summary" aria-label="上下文与会话累计统计">
				<ContextUsageMeter :usage="props.contextUsage" :context-window="props.contextWindowTokens" :threshold="props.contextThresholdTokens" :strategy="props.contextStrategy" :conversation-uuid="props.conversationUuid"/>
				<span class="composer-total" :title="`总 Tokens ${props.tokensText}；缓存命中率 ${props.cachePercentText}`" :aria-label="`总 Tokens ${props.tokensText}；缓存命中率 ${props.cachePercentText}`"><Layers aria-hidden="true"/><span>{{ props.tokensText }}（{{ props.cachePercentText }}）</span></span>
				<span class="composer-total" :title="`总花费 ${props.costText}`" :aria-label="`总花费 ${props.costText}`"><Coins aria-hidden="true"/><span>{{ props.costText }}</span></span>
				<span class="composer-total" :title="`总耗时 ${props.durationText}`" :aria-label="`总耗时 ${props.durationText}`"><Clock3 aria-hidden="true"/><span>{{ props.durationText }}</span></span>
			</div>
			<div class="composer-hints mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-[11px] text-ob-muted">
				<span class="composer-desktop-shortcut">Enter 发送 · Ctrl/⌘+Enter 也可发送</span>
				<span class="composer-touch-shortcut">Enter 换行 · Ctrl/⌘+Enter 发送</span>
				<span class="composer-attachment-hint">图片/文本附件会随本轮发送</span>
			</div>
		</div>
	</footer>
</template>

<style scoped>
.context-compacting-icon { animation: context-compress-spin 1.5s linear infinite; }
@keyframes context-compress-spin { to { transform: rotate(360deg); } }

.composer-shell {
	pointer-events: none;
	background: linear-gradient(to top, var(--ob-chat-bg) 85%, transparent);
	padding-right: var(--console-content-gutter, 1rem);
	padding-left: var(--console-content-gutter, 1rem);
}

.composer-content {
	width: min(100%, var(--console-content-max-width, 56rem));
}

.composer-shell > div {
	pointer-events: auto;
}

.steering-queue-card {
	display: grid;
	gap: 0.55rem;
	margin-bottom: 0.75rem;
	border: 1px solid rgb(var(--ob-blue-rgb) / 0.22);
	border-radius: 1rem;
	background: linear-gradient(180deg, var(--ob-blue-soft), rgb(var(--ob-surface-rgb) / 0.96));
	box-shadow: 0 16px 40px rgb(var(--ob-blue-rgb) / 0.1);
	padding: 0.85rem;
}

.steering-queue-title {
	display: flex;
	align-items: center;
	gap: 0.45rem;
	font-size: 0.82rem;
	font-weight: 800;
	color: var(--ob-blue);
}

.steering-queue-title svg {
	width: 1rem;
	height: 1rem;
}

.steering-queue-hint {
	font-size: 0.73rem;
	line-height: 1.45;
	color: var(--ob-text-subtle);
}

.steering-queue-items {
	display: grid;
	grid-auto-rows: max-content;
	gap: 0.42rem;
	min-width: 0;
	max-height: min(32vh, 16rem);
	max-height: min(32dvh, 16rem);
	overflow-y: auto;
	overscroll-behavior: contain;
}

.steering-queue-item {
	min-width: 0;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	border: 1px solid rgb(var(--ob-blue-rgb) / 0.16);
	border-radius: 0.78rem;
	background: rgb(var(--ob-surface-rgb) / 0.82);
	padding: 0.55rem 0.65rem;
	font-size: 0.8rem;
	line-height: 1.45;
	color: var(--ob-text);
}

.steering-queue-attachments {
	margin-top: 0.25rem;
	color: var(--ob-text-subtle);
	font-size: 0.74rem;
}

.composer-box {
	position: relative;
	z-index: 40;
	border: 1px solid var(--ob-chat-border);
	border-radius: 13px;
	background: var(--ob-chat-panel);
	padding: 0.5rem;
	box-shadow: var(--ob-shadow-panel);
}

.composer-box:focus-within {
	border-color: var(--ob-border-strong);
	box-shadow: var(--ob-shadow-panel);
}

.attachment-strip {
	display: flex;
	gap: 0.62rem;
	overflow-x: auto;
	padding: 0.2rem 0.25rem 0.45rem;
	scrollbar-width: thin;
}

.attachment-card {
	position: relative;
	flex: 0 0 auto;
	width: 82px;
	height: 82px;
	overflow: hidden;
	border: 1px solid var(--ob-border);
	border-radius: 18px;
	background: linear-gradient(180deg, var(--ob-surface), var(--ob-surface));
	box-shadow: var(--ob-shadow-panel);
}

.attachment-upload-state {
	position: absolute;
	inset: auto 0 0;
	padding: 0.18rem 0.3rem 0.24rem;
	background: rgb(var(--ob-text-strong-rgb) / 0.84);
	color: var(--ob-text-inverse);
	font-size: 0.66rem;
	font-weight: 700;
	text-align: center;
	pointer-events: none;
}
.attachment-upload-state.is-failed { background: rgb(var(--ob-danger-rgb) / 0.92); }
.attachment-upload-track { height: 3px; margin-top: 0.15rem; border-radius: 3px; background: rgb(255 255 255 / 0.35); }
.attachment-upload-track div { height: 100%; border-radius: inherit; background: currentColor; }
.attachment-upload-state.is-finalizing .attachment-upload-track div { opacity: 0.65; }

.attachment-thumb {
	display: block;
	width: 100%;
	height: 100%;
	cursor: zoom-in;
}

.attachment-thumb :deep(.el-image__inner) {
	width: 100%;
	height: 100%;
	object-fit: cover;
}

.attachment-file-tile {
	display: grid;
	height: 100%;
	place-items: center;
	padding: 0.45rem;
	text-align: center;
	color: var(--ob-text);
	font-size: 10px;
	line-height: 1.2;
}

.attachment-file-tile span {
	max-width: 100%;
	overflow: hidden;
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	word-break: break-all;
	font-weight: 700;
}

.attachment-file-tile small {
	color: var(--ob-text-muted);
}

.attachment-remove {
	position: absolute;
	top: 0.35rem;
	right: 0.35rem;
	display: grid;
	width: 1.35rem;
	height: 1.35rem;
	place-items: center;
	border: 0;
	border-radius: 999px;
	background: rgb(var(--ob-text-strong-rgb) / 0.76);
	color: var(--ob-text-inverse);
	cursor: pointer;
	opacity: 0;
	transform: scale(0.88);
	transition: opacity .16s ease, transform .16s ease, background .16s ease;
}

.attachment-card:hover .attachment-remove,
.attachment-remove:focus-visible {
	opacity: 1;
	transform: scale(1);
}

.attachment-remove:hover {
	background: rgb(var(--ob-danger-rgb) / 0.92);
}

.attachment-remove svg {
	width: 0.82rem;
	height: 0.82rem;
}

.file-preview-icon {
	width: 1.45rem;
	height: 1.45rem;
	color: var(--ob-text-subtle);
}

.composer-textarea {
	display: block;
	width: 100%;
	min-height: 3.4rem;
	max-height: 13.5rem;
	resize: none;
	overflow-y: auto;
	border: 0;
	outline: 0;
	background: transparent;
	padding: 0.45rem 0.75rem;
	color: var(--ob-text-strong);
	font-size: 14px;
	line-height: 1.65;
}

.composer-textarea::placeholder {
	color: var(--ob-text-muted);
}

.composer-toolbar {
	display: grid;
	grid-template-columns: minmax(0, 1fr) auto;
	align-items: center;
	gap: 0.6rem;
	padding: 0.15rem 0.1rem 0;
}

.tool-btn {
	display: grid;
	width: 2rem;
	height: 2rem;
	place-items: center;
	border: 0;
	border-radius: 999px;
	background: transparent;
	color: var(--ob-text-subtle);
	cursor: pointer;
}

.tool-btn svg {
	width: 1rem;
	height: 1rem;
}

.tool-btn-accent {
	color: var(--ob-chat-subtle);
}

.tool-btn:hover, .tool-btn-active {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}

.tool-btn:disabled {
	opacity: .38;
	cursor: not-allowed;
}

.image-one-turn-trigger {
	position: relative;
}

.image-one-turn-trigger.is-on {
	color: var(--ob-text-strong);
}

.persona-menu-trigger {
	position: relative;
}

.persona-menu-trigger.is-on {
	color: var(--ob-text-strong);
}

/* The dot says this conversation renders a persona, not the default template. */
.persona-menu-mark {
	position: absolute;
	top: 5px;
	right: 5px;
	width: 6px;
	height: 6px;
	border-radius: 50%;
	background: var(--ob-text-strong);
	box-shadow: 0 0 0 1.5px var(--ob-chat-panel);
	pointer-events: none;
}

.persona-menu-mark.is-attention { display: grid; place-items: center; width: 11px; height: 11px; top: 2px; right: 2px; background: var(--ob-warning); color: var(--ob-surface); font-size: 9px; font-weight: 700; line-height: 1; }

/* The "1" says this conversation shows each image for one round only. */
.image-one-turn-mark {
	position: absolute;
	top: 3px;
	right: 2px;
	display: grid;
	min-width: 12px;
	height: 12px;
	place-items: center;
	padding: 0 2px;
	border-radius: 6px;
	background: var(--ob-text-strong);
	color: var(--ob-text-inverse);
	font-size: 8.5px;
	font-weight: 700;
	line-height: 1;
	font-variant-numeric: tabular-nums;
	box-shadow: 0 0 0 1.5px var(--ob-chat-panel);
	pointer-events: none;
}

.composer-status {
	display: flex;
	min-width: 0;
	align-items: center;
	gap: 0.35rem;
}

.status-chip {
	display: inline-flex;
	max-width: 11rem;
	height: 2rem;
	align-items: center;
	gap: 0.28rem;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	border: 0;
	border-radius: 999px;
	background: transparent;
	padding: 0 0.5rem;
	color: var(--ob-text);
	font-size: 12px;
}

.chip-icon {
	width: 0.9rem;
	height: 0.9rem;
	flex: 0 0 auto;
}

.run-config-chip {
	max-width: min(24rem, 38vw);
	min-width: 0;
	flex: 0 1 auto;
	height: 1.78rem;
	gap: 0.34rem;
	background: transparent;
	padding: 0 0.34rem 0 0.48rem;
	color: var(--ob-text-subtle);
	font-size: 11.2px;
}

.run-config-chip:hover,
.run-config-chip.status-chip-active {
	background: var(--ob-hover);
	color: var(--ob-text);
}

.run-config-chip-main {
	display: inline-flex;
	min-width: 0;
	align-items: center;
	gap: 0.28rem;
	overflow: hidden;
}

.run-config-chip-status {
	display: none;
	flex: 0 0 auto;
	align-items: center;
	color: var(--ob-text-muted);
	font-weight: 520;
	white-space: nowrap;
}

.run-config-chip-status > span {
	display: inline-flex;
	align-items: center;
}

/* Separators match the strategy's: "model · 高 · ⚡ · 滑窗压缩". */
.run-config-chip-status::before,
.run-config-chip-status > span + span::before {
	content: "·";
	margin: 0 0.28rem;
	color: var(--ob-text-muted);
}
.run-config-chip-status::before { margin-left: 0; }

.run-config-chip-status .model-feature-icon {
	width: 1.15em;
	height: 1.15em;
}

.run-config-chip-model {
	max-width: 100%;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-weight: 500;
}

.run-config-chip-strategy {
	flex: 0 0 auto;
	white-space: nowrap;
	color: var(--ob-text-muted);
	font-weight: 520;
	font-variant-numeric: tabular-nums;
}

.run-config-chip-meta {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: var(--ob-text-muted);
	font-weight: 520;
}

.run-config-meta-part + .run-config-meta-part::before {
	content: " · ";
	color: var(--ob-text-muted);
}

/* Override Tailwind's block SVG reset so the ring stays beside the usage text. */
.run-config-context-ring { display: inline-block; width: 12px; height: 12px; margin-right: 5px; vertical-align: -2px; transform: rotate(-90deg); }
.run-config-context-ring-track, .run-config-context-ring-fill { fill: none; stroke: currentColor; stroke-width: 3; }
.run-config-context-ring-track { opacity: .22; }
.run-config-context-ring-fill { stroke-linecap: round; opacity: .85; transition: stroke-dasharray 250ms ease; }
.run-config-context-ring.is-unknown .run-config-context-ring-track { stroke-dasharray: 3 3; }
.run-config-context-ring.is-warning { color: var(--ob-warning); }
.run-config-context-ring.is-danger { color: var(--ob-danger); }
@media (prefers-reduced-motion: reduce) { .run-config-context-ring-fill { transition: none; } }

.run-config-meta-fast .model-feature-icon {
	width: 1.15em;
	height: 1.15em;
	vertical-align: -0.2em;
}

.run-config-chip-meta::before, .run-config-chip-strategy::before {
	content: "·";
	margin-right: 0.28rem;
	color: var(--ob-text-muted);
}

.chip-caret {
	width: 0.72rem;
	height: 0.72rem;
	flex: 0 0 auto;
	color: var(--ob-text-muted);
	transition: transform .14s ease, color .14s ease;
}

.run-config-chip.status-chip-active .chip-caret {
	transform: rotate(180deg);
	color: var(--ob-text-subtle);
}

.status-chip strong {
	color: var(--ob-text);
	font-weight: 650;
}

button.status-chip {
	cursor: pointer;
}

button.status-chip:hover, .status-chip-active {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}

/* Model picker: quiet macOS surfaces, one accent, two readable type sizes. */
.popover-menu-content { overflow: hidden; border-radius: 12px; }
.run-config-popover {
	--rc-text: var(--ob-text);
	--rc-muted: var(--ob-text-subtle);
	--rc-line: var(--ob-border);
	--rc-surface: var(--ob-surface-soft);
	--rc-hover: var(--ob-hover);
	--rc-selected: var(--ob-selected);
	--rc-control: var(--ob-surface);
	--rc-accent: var(--ob-blue);
	--rc-track: var(--ob-scrollbar);
	--rc-detail-bg: var(--ob-text-strong);
	--rc-detail-text: var(--ob-text-inverse);
	--rc-detail-line: var(--ob-border);
	position: relative;
	display: flex;
	flex-direction: column;
	gap: 10px;
	width: 100%;
	max-height: min(76vh, 600px);
	max-height: min(76dvh, 600px);
	color: var(--rc-text);
	font-size: 13px;
	line-height: 1.45;
}
.run-config-popover button, .run-config-popover input { font-family: inherit; }
.run-config-tabs {
	display: flex;
	flex: 0 0 auto;
	gap: 3px;
	padding: 3px;
	border-radius: 9px;
	background: var(--rc-surface);
}
.run-config-tabs button {
	flex: 1;
	min-width: 0;
	padding: 5px 12px;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: var(--rc-muted);
	font-size: 13px;
	font-weight: 500;
	line-height: 20px;
	cursor: pointer;
	transition: background-color .15s ease, color .15s ease, box-shadow .15s ease;
}
.run-config-tabs button:hover:not(.is-active) { color: var(--rc-text); }
.run-config-tabs button.is-active {
	background: var(--rc-control);
	color: var(--rc-text);
	box-shadow: var(--ob-shadow-panel);
}
.run-config-control-head, .fast-control { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.config-label { display: inline-flex; align-items: center; flex: 0 0 auto; gap: 7px; font-size: 13px; font-weight: 500; white-space: nowrap; }
.config-label svg { width: 15px; height: 15px; flex: 0 0 auto; color: var(--rc-muted); }
.run-config-notice { margin: 0 4px; color: var(--rc-muted); font-size: 12px; }
.model-search {
	display: flex;
	align-items: center;
	flex: 0 0 auto;
	gap: 8px;
	margin: 0;
	padding: 6px 10px;
	border: 1px solid var(--rc-line);
	border-radius: 8px;
	background: var(--ob-chat-hover);
	color: var(--rc-muted);
	transition: border-color .15s ease, box-shadow .15s ease;
}
.model-search:focus-within {
	border-color: var(--rc-accent);
	box-shadow: 0 0 0 2px rgb(var(--ob-blue-rgb) / 0.15);
}
.model-search svg { width: 14px; height: 14px; flex: 0 0 auto; color: var(--rc-muted); }
.model-search input { min-width: 0; width: 100%; padding: 0; border: 0; outline: 0; background: transparent; color: var(--rc-text); font-size: 13px; line-height: 20px; }
.model-search input::placeholder { color: var(--rc-muted); }
.run-config-model-section { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.run-config-model-list {
	min-height: 0;
	max-height: min(42vh, 320px);
	flex: 1 1 auto;
	overflow-y: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
	scrollbar-color: var(--rc-track) transparent;
	padding: 0 2px;
}
.model-group { margin: 0; }
.model-group + .model-group { margin-top: 10px; }
.model-group-title { padding: 4px 8px 3px; color: var(--rc-muted); font-size: 12px; font-weight: 500; letter-spacing: 0.02em; }
.model-row {
	display: flex;
	align-items: center;
	gap: 6px;
	width: 100%;
	padding: 6px 8px;
	border: 1px solid transparent;
	border-radius: 8px;
	background: transparent;
	color: var(--rc-text);
	text-align: left;
	cursor: pointer;
	transition: background-color .14s ease, border-color .14s ease;
}
.model-row:hover { background: var(--rc-hover); }
.model-row.is-selected {
	background: var(--rc-selected);
	border-color: rgb(var(--ob-blue-rgb) / 0.2);
}
.model-select {
	display: flex;
	align-items: center;
	flex: 1 1 auto;
	min-width: 0;
	align-self: stretch;
	padding: 0;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: inherit;
	text-align: left;
	cursor: pointer;
}
.follow-model-row { padding: 6px 8px; }
.model-row-main {
	display: flex;
	flex-direction: column;
	gap: 4px;
	flex: 1 1 auto;
	min-width: 0;
}
.model-row-title-row {
	display: flex;
	align-items: center;
	gap: 6px;
	min-width: 0;
}
.model-row-name {
	overflow: hidden;
	white-space: nowrap;
	text-overflow: ellipsis;
	font-size: 13px;
	line-height: 18px;
	font-weight: 500;
	transition: color .14s ease;
}
.model-row.is-selected .model-row-name {
	color: var(--rc-accent);
}
.model-row-tags {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 4px;
}
.model-tag {
	display: inline-flex;
	align-items: center;
	gap: 3px;
	font-size: 12px;
	line-height: 16px;
	padding: 1px 6px;
	border-radius: 4px;
	background: var(--rc-surface);
	color: var(--rc-muted);
	font-variant-numeric: tabular-nums;
	white-space: nowrap;
}
.model-tag-feature {
	color: var(--rc-text);
	background: rgb(var(--ob-blue-rgb) / 0.08);
}
.model-tag .model-feature-icon {
	width: 12px;
	height: 12px;
}
.config-label .model-feature-icon { color: var(--rc-muted); }
.model-selection-mark {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 18px;
	height: 18px;
	flex: 0 0 auto;
	margin-left: 4px;
}
.model-selected-icon { width: 15px; height: 15px; flex: 0 0 auto; color: var(--rc-accent); }
.agent-follow-group { margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--rc-line); }
.model-follow-detail { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--rc-muted); font-size: 12px; }
.model-empty { padding: 22px 10px; text-align: center; color: var(--rc-muted); font-size: 13px; }
.run-config-controls {
	display: flex;
	flex: 0 0 auto;
	flex-direction: column;
	border-top: 1px solid var(--rc-line);
	padding: 2px 2px 0;
	margin-top: 2px;
}
.thinking-control { display: grid; gap: 8px; padding: 9px 0 8px; }
.config-hint { color: var(--rc-muted); font-size: 12px; font-weight: 400; white-space: nowrap; }
.thinking-segments {
	display: flex;
	flex-wrap: wrap;
	gap: 3px;
	padding: 3px;
	border-radius: 8px;
	background: var(--rc-surface);
}
.thinking-segments button {
	flex: 1;
	min-width: 34px;
	padding: 4px 7px;
	border: 0;
	border-radius: 6px;
	color: var(--rc-muted);
	background: transparent;
	font-size: 12px;
	font-weight: 500;
	line-height: 20px;
	white-space: nowrap;
	cursor: pointer;
	transition: background-color .15s ease, color .15s ease, box-shadow .15s ease;
}
.thinking-segments button:hover:not(:disabled) { color: var(--rc-text); }
.thinking-segments button.is-active {
	color: var(--rc-text);
	background: var(--rc-control);
	box-shadow: var(--ob-shadow-panel);
}
.thinking-segments button:disabled { opacity: .4; cursor: not-allowed; }
.fast-control {
	min-height: 38px;
	padding: 6px 0;
	border-top: 1px solid var(--rc-line);
}
/* Reserve the same row for the 22px switch and the 30px Agent segments. */
.fast-control:not(.context-strategy-control) { min-height: 44px; }
.config-value { display: flex; align-items: center; gap: 9px; }
.agent-fast-segments { flex: 0 0 auto; }
.agent-fast-segments button { flex: 0 0 auto; min-width: 30px; padding: 2px 10px; font-size: 12px; text-align: center; }
.fast-switch {
	position: relative;
	width: 36px;
	height: 22px;
	flex: 0 0 auto;
	padding: 2px;
	border: 0;
	border-radius: 11px;
	background: var(--rc-track);
	cursor: pointer;
	transition: background-color .18s cubic-bezier(0.4, 0, 0.2, 1);
}
.fast-switch span {
	display: block;
	width: 18px;
	height: 18px;
	border-radius: 50%;
	background: var(--ob-switch-thumb);
	box-shadow: var(--ob-shadow-panel);
	transition: transform .18s cubic-bezier(0.4, 0, 0.2, 1);
}
.fast-switch.is-on { background: var(--rc-accent); }
.fast-switch.is-on span { transform: translateX(14px); }
.fast-switch:disabled { opacity: .45; cursor: not-allowed; }
.run-config-popover button:focus-visible { outline: 2px solid var(--rc-accent); outline-offset: -2px; }
.run-config-popover .fast-switch:focus-visible { outline-offset: 3px; }
@media (max-height: 620px) {
	.run-config-popover { overflow-y: auto; }
	.run-config-model-section { flex: 0 0 auto; }
	.run-config-model-list { flex: 0 0 auto; max-height: 180px; }
}
/* The body-teleported menu follows the visual viewport. Only its active
   middle pane scrolls: results normally, settings when explicitly opened.
   Compact mode is chosen from the normal chrome's measured height. */
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.run-config-popover {
		max-height: min(600px, calc(var(--mobile-viewport-height, 100dvh) - 48px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)));
		overflow: hidden;
		overflow-y: hidden;
		overscroll-behavior: contain;
	}
	.run-config-model-section { flex: 1 1 auto; min-height: 0; overflow: hidden; }
	.run-config-model-list { flex: 1 1 auto; max-height: 320px; }
	.run-config-search { position: static; }
	.run-config-search input { font-size: 16px; }
	.run-config-popover.is-compact { gap: 8px; }
	.run-config-popover.is-compact .run-config-controls { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; margin-top: 0; }
	.run-config-compact-footer { display: flex; flex: 0 0 auto; align-items: center; gap: 8px; padding-top: 6px; border-top: 1px solid var(--rc-line); }
	.run-config-compact-summary { display: flex; flex-direction: column; flex: 1 1 0; min-width: 0; gap: 2px; color: var(--rc-muted); font-size: 12px; line-height: 16px; }
	.run-config-compact-summary > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.run-config-compact-actions { display: flex; flex: 0 0 auto; gap: 3px; }
	.run-config-compact-actions button { min-height: 36px; padding: 4px 8px; border: 0; border-radius: 8px; background: var(--rc-surface); color: var(--rc-text); font-size: 12px; }
}
@media (prefers-reduced-motion: reduce) {
	.run-config-popover *, .run-config-popover *::before { transition: none; }
}

.send-button {
	width: 2rem;
	height: 2rem;
	flex: 0 0 auto;
	display: grid;
	place-items: center;
	border: 0;
	border-radius: 8px;
	background: var(--ob-chat-button);
	color: var(--ob-chat-button-text);
	line-height: 1;
	cursor: pointer;
	box-shadow: var(--ob-shadow-panel);
	-webkit-touch-callout: none;
	user-select: none;
}

.send-button svg {
	width: 1rem;
	height: 1rem;
}

.stop-button {
	background: var(--ob-danger);
	box-shadow: 0 8px 18px rgb(var(--ob-danger-rgb) / 0.18);
	-webkit-touch-callout: none;
	user-select: none;
}
.send-button.is-round { border-radius: 50%; box-shadow: none; }
.send-button.is-round:not(.stop-button) svg { width: 60%; height: 60%; }

.send-button:disabled,
.send-button[aria-disabled="true"] {
	background: var(--ob-text-disabled);
	cursor: not-allowed;
}

/* The page assistant's panel is narrow on any screen. */
.composer-shell.is-compact { padding: .4rem .6rem .5rem; }
.is-compact .composer-toolbar { grid-template-columns: auto minmax(0, 1fr); gap: .25rem; }
.is-compact .composer-actions { gap: 0; }
.is-compact .composer-status { justify-content: flex-end; }
.is-compact .run-config-chip { max-width: 100%; }
.is-compact .run-config-chip-meta { display: none; }
.is-compact .run-config-chip-status { display: inline-flex; }

/* Desktop keeps context in run configuration and totals in the header. */
.composer-usage-summary { display: none; }
.composer-touch-shortcut { display: none; }
@media (hover: none) and (pointer: coarse) {
	.composer-desktop-shortcut { display: none; }
	.composer-touch-shortcut { display: inline; }
}

@media (max-width: 760px) {
	.composer-shell { padding: .5rem .75rem 8px; }
	.composer-box { padding: 7px; border-radius: 15px; }
	/* Normal flow lets long totals wrap without covering the editor or its actions. */
	.composer-usage-summary { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 0 5px; min-height: 32px; padding-top: 2px; color: var(--ob-chat-muted); font-size: 9px; font-variant-numeric: tabular-nums; }
	.composer-total { display: inline-flex; align-items: center; gap: 3px; min-height: 24px; line-height: 24px; white-space: nowrap; }
	.composer-total::before { content: "·"; margin-right: 2px; color: var(--ob-chat-muted); }
	.composer-total > svg { width: 11px; height: 11px; flex: none; stroke-width: 1.7; }
	.composer-total > span { color: var(--ob-chat-subtle); }
	.composer-status .run-config-chip-strategy { display: inline; font-size: 10px; }
	.composer-toolbar {
		display: flex;
		flex-wrap: nowrap;
		gap: 0;
		padding: 0;
	}
	/* Move the complete controls (including their hit areas and feedback)
	   left, and give the reclaimed 12px to the model instead of just painting
	   translated icons. The 44px controls remain separate and reachable. */
	.composer-actions { flex: 0 0 auto; gap: 0; margin-left: -9px; }
	/* Tighten the icon pitch without shrinking the 44px touch height. The 3px
	   group offset difference keeps the first icon at its current position. */
	.composer-actions .tool-btn { width: 38px; }
	.image-one-turn-mark { top: 8px; right: 6px; }
	.persona-menu-mark { top: 10px; right: 10px; }
	.composer-tools-trigger { position: relative; }
	.composer-actions .tool-btn:hover, .composer-actions .tool-btn-active {
		background: radial-gradient(circle at center, var(--ob-hover) 0 16px, transparent 16.5px);
	}
	.composer-status {
		flex: 1 1 0;
		min-width: 0;
		justify-content: flex-end;
		flex-wrap: nowrap;
		gap: 0;
	}
	/* The hit area stays generous; the model is a quiet text link, not a pill.
	   Scope above the global dark .status-chip rule from interaction cards. */
	.composer-toolbar button.run-config-chip {
		flex: 0 1 auto;
		min-width: 0;
		max-width: 100%;
		appearance: none;
		border: 0;
		border-radius: 6px;
		background: transparent;
		box-shadow: none;
		padding-inline: .375rem;
	}
	.run-config-chip-model { font-weight: 500; }
	.run-config-chip-status { display: inline-flex; font-size: 10px; }
	/* Phone uses the text itself as the menu affordance; the caret space is
	   returned to the right-aligned model/status group. */
	.composer-toolbar button.run-config-chip .chip-caret { display: none; }
	.composer-clear:disabled { display: none; }
	.composer-toolbar button.run-config-chip:focus-visible { outline: 2px solid var(--bear-accent); outline-offset: -2px; }
	.run-config-chip-meta { display: none; }
	.composer-attachment-hint { display: none; }
	:deep(.reference-editor-content) { min-height: min(3rem, calc(var(--mobile-viewport-height, 100dvh) * .22)); padding: .55rem .5rem; }
	:deep(.reference-editor-placeholder) { padding: .65rem .5rem; }
}

@media (min-width: 761px) and (max-width: 1120px) {
	.composer-toolbar { grid-template-columns: minmax(0, 1fr); gap: 2px; }
	.composer-status { justify-content: flex-end; }
	.run-config-chip { max-width: min(20rem, 38vw); }
}

@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.composer-hints { display: none; }
	/* In an exceptionally short landscape/keyboard viewport, keep every input
	   action reachable inside the composer rather than overflowing the page. */
	.composer-shell { max-height: 100%; overflow-y: auto; pointer-events: auto; }
	/* Larger hit areas, not larger glyphs or global control typography. */
	.tool-btn, .send-button {
		width: 44px;
		height: 44px;
		flex: 0 0 auto;
	}
	/* Keep the tool controls at 44px height, but make the primary send button a
	   little smaller so more of the row can be used by the model name. */
	.send-button { width: 38px; height: 38px; }
	.run-config-chip { min-height: 44px; }
	.attachment-remove {
		top: 0;
		right: 0;
		width: 44px;
		height: 44px;
		opacity: 1;
		transform: none;
		/* Keep the small macOS remove badge inside its 44px tap target. */
		background: radial-gradient(circle, rgb(var(--ob-text-strong-rgb) / 0.8) 0 12px, transparent 13px);
	}
	:deep(.reference-editor-content) { max-height: min(13.5rem, calc(var(--mobile-viewport-height, 100dvh) * .28)); }
}
</style>

<style>
.run-config-menu-popper.el-popper {
	padding: 10px;
	border: 1px solid var(--ob-border);
	border-radius: 16px;
	background: var(--ob-surface-raised);
	box-shadow: var(--ob-shadow-popover);
	backdrop-filter: blur(24px) saturate(180%);
	-webkit-backdrop-filter: blur(24px) saturate(180%);
}
html.dark .run-config-menu-popper.el-popper {
	border-color: var(--ob-border);
}
/* OpenBear system dark theme */
html.dark .interaction-expired-notice,
html.dark .interaction-submit-error {
		border: 1px solid rgb(var(--ob-danger-rgb) / 0.52);
	}
html.dark .recommendation-badge {
		border: 1px solid var(--ob-border);
	}
html.dark .recommendation-reason {
		border-left: 2px solid rgb(var(--ob-blue-rgb) / 0.52);
	}
</style>
