<script setup>
import {computed, markRaw, nextTick, onBeforeUnmount, onMounted, provide, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {
	ChatLineRound,
	Hide,
	Loading,
	Lock,
	Unlock,
} from "@element-plus/icons-vue";
import ConsoleComposer from "./ConsoleComposer.vue";
import {referenceErrorText, referenceDisplayText, referencesInText} from "../../references/codec.js";
import {referenceCatalog, acceptActivityReadReceipt} from "../../references/catalog.js";
import {createActivityReadTracker, withActivityReadVersion} from "../../conversationActivity.js";
import {initialConversationTitle} from "../../conversationTitle.js";
import ConsoleHeader from "./ConsoleHeader.vue";
import MobileConversationTools from "./MobileConversationTools.vue";
import TurnList from "./TurnList.vue";
import {WORK_MOTION} from './conversationWork.js';
import HiddenMessagesDrawer from "./HiddenMessagesDrawer.vue";
import MessageVisibilityBar from "./MessageVisibilityBar.vue";
import MessageVisibilityMobileMenu from "./MessageVisibilityMobileMenu.vue";
import {createMessageVisibility, MESSAGE_VISIBILITY} from "./messageVisibility.js";
import TurnMinimap from "./TurnMinimap.vue";
import TaskMemoryDrawer from "./TaskMemoryDrawer.vue";
import {chooseActiveTurnIndex} from "./activeTurn.js";
import {
	decideAgentAutoOpen,
	isAgentPanelDetailKey,
	normalizeAgentPanelIntents,
} from "./agentPanelState.js";
import {
	answerContent,
	clearMarkdownCache,
	plainText,
} from "./markdown.js";
import {
	agentSummary,
	agentTasks,
	callName,
	fmtCost,
	fmtMs,
	fmtTokens,
	isAgentEvent,
	tokenLine,
	modelDefaultThinking,
	modelThinkingLevels,
	thinkingLabel,
	toolBatchSize,
	toolCallIdOf,
	toolResultForIndex,
	toolResultItems,
	toolResultKey,
} from "./display.js";
import {Api, apiError, conversationWsUrl} from "../../api.js";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {
	createTerminalStateRefreshScheduler,
	runGuardedConversationStateRefresh,
} from "./terminalStateRefresh.js";
import {createConversationStateRequests} from "./conversationStateRequests.js";
import {createOperationFrameBuffer} from "./operationFrameBuffer.js";
import {
	applyLedgerUsageSnapshot,
	ledgerTokenParts,
	normalizeLedgerUsageBaseline,
	totalSessionDurationMs,
} from "./ledgerUsage.js";
import {
	invalidateContextUsage,
	mergeStatsContextUsage,
	resolveContextUsage,
} from "./contextUsage.js";
import {reconcileAgentTaskUsage} from "./turnStats.js";
import {
	applyOperationFrame,
	convergeStoppedAcknowledgement,
	deriveOperationRunState,
	isRootRunTerminalFrame,
	isTerminalOperationFrame,
	normalizeOperations,
	projectOperationMessages as projectOperationMessagesFromOperations,
	shouldApplyOperationFrame,
	withTransientIdleThinking as projectTransientIdleThinking,
} from "../../timelineProjection.js";
import {
	TASK_MEMORY_CHANGED_EVENT_KEY,
	taskMemoryChangedTransportEvent,
} from "./taskMemoryUiState.js";
import {
	interactionErrorCode,
	interactionRevision,
	isTerminalInteractionError,
} from "./userInteractionState.js";
import {
	capturePrependAnchor,
	findTurnIndexByIdentity,
	mergeOperationSnapshots,
	prependAnchoredScrollTop,
	sameTimelinePageRequest,
	settleTimelinePageRequest,
	shouldRequestEarlierPage,
	stableTurnIdentity,
	touchMovesTimelineUp,
} from "./timelinePagination.js";
import {
	createToolDetailCache,
	TOOL_DETAIL_CACHE_KEY,
} from "./toolDetailCache.js";
import {
	createOutboundSendTracker,
	probeSocket,
	restoreOutboundDraft,
	waitForSocketOpen,
} from "./outboundSend.js";
import {
	createRunConfigSaveQueue,
	mayRetireRunConfigOverride,
	runConfigForDisplay,
} from "./runConfigState.js";
import {createAttachmentDraftStorage} from "./attachmentDraftStorage.js";
import {captureTranscriptContentAnchor, transcriptContentAnchorDelta} from "./transcriptContentAnchor.js";

const DEFAULT_NEW_CONVERSATION_THINKING = "";
const DRAFT_STORAGE_KEY = "openbear.console.drafts.v1";
const AGENT_PANEL_INTENT_STORAGE_KEY = "openbear.console.agentPanelIntents.v1";
const FRAME_DEBUG_STORAGE_KEY = "openbear.debug.frames";
const STREAM_UI_FRAME_MS = 34;
const ACTIVE_TURN_SCROLL_UPDATE_MS = 140;
const INITIAL_TIMELINE_LIMIT = 200;
const LOAD_EARLIER_SCROLL_THRESHOLD = 180;
const props = defineProps({
	conversationUuid: {type: String, default: ""},
	canonicalTitle: {type: String, default: ""},
	conversationPath: {type: String, default: ""},
	navigationObscured: {type: Boolean, default: false},
	// Immutable ownership for the current local draft. App changes it only after
	// an explicit reassignment confirmation; persisted conversations use it as the
	// target for cross-family "new conversation" creation.
	folderId: {type: String, default: ""},
});
const emit = defineEmits(["conversation-created", "conversations-refresh"]);

const loading = ref(false);
// A replacement state request must finish an outstanding entry scroll instead
// of replacing its bottom intent with the background refresh's preserve mode.
let pendingLoadBottomScroll = null;
let pendingLoadReplaceOperations = false;
const operationFrameBuffer = createOperationFrameBuffer();
const conversationStateRequests = createConversationStateRequests(async (uuid, signal) => {
	const runConfigVersionAtRequest = runConfigSaves.appliedVersion;
	const data = await Api.conversationState(uuid, {timelineLimit: INITIAL_TIMELINE_LIMIT}, {signal});
	return {data, runConfigVersionAtRequest};
});
const running = ref(false);
const sendPending = ref(false);
const foregroundRunning = ref(false);
const rootTurnRunning = ref(false);
const activeRunTurnUuid = ref("");
const messages = ref([]);
const draftByConversation = ref(loadDraftStore());
// Bind persisted text before the first editable render, not after options/state
// HTTP completes. Subsequent edits are authoritative while initialization waits.
const draft = ref(String(draftByConversation.value[draftKey(props.conversationUuid)] || ""));
let draftEditRevision = 0;
watch(draft, () => { draftEditRevision += 1; }, {flush: 'sync'});
const status = ref("就绪");
const operationsById = ref(new Map());
const orderedOpIds = ref([]);
const revisionByOpId = ref(new Map());
const lastFrameSeq = ref(0);
const lastStats = ref(null);
const chatState = ref(null);
// Successful config saves update this display-only overlay. Keeping chatState,
// messages and operation refs untouched prevents an unrelated timeline rebuild.
const runConfigOverride = ref(null);
const scroller = ref(null);
const autoScrollLocked = ref(true);
const scrollerOverflow = ref(false);
const activeTurnIndex = ref(0);
const taskMemoryDrawer = ref(null);
const composer = ref(null);
const composerHeight = ref(135);
const pendingAttachments = ref([]);
const attachmentPreviews = ref({});
const modelMenuOpen = ref(false);
const optionsLoading = ref(false);
const modelOptions = ref([]);
const primaryModelKey = ref("");
const currentPrimaryModelKey = ref("");
const thinkLevels = ref([]);
const modelQuery = ref("");
const runStartedAt = ref(0);
// Attachments belong to their conversation just like the text draft. File objects
// cannot be written to localStorage next to it, so they are kept per conversation
// in this map plus an IndexedDB record. The map holds the conversations the user
// switched away from; the conversation on screen is always represented by
// pendingAttachments/attachmentPreviews, never by both at once.
const attachmentsByConversation = new Map();
const attachmentDrafts = createAttachmentDraftStorage();
let attachmentsLoadedKey = "";
const attachmentHydrations = new Map();
const attachmentRestoring = ref(false);
let conversationSwitchGeneration = 0;
const restoringDraft = ref(false);
const toolResultTabs = ref({});
const detailOpen = ref(loadAgentPanelIntents());
const pendingConfirmations = ref([]);
const pendingInteractionFocus = ref(null);
const confirmationSubmitting = ref({});
const confirmationErrors = ref({});
const pendingSteering = ref([]);
const taskMemoryChangedEvent = ref(null);
provide(TASK_MEMORY_CHANGED_EVENT_KEY, taskMemoryChangedEvent);
const toolDetailCache = createToolDetailCache();
provide(TOOL_DETAIL_CACHE_KEY, toolDetailCache);
const hasMoreBefore = ref(false);
const nextBeforeDisplaySeq = ref(null);
const timelinePageInFlight = ref(null);
const retryActionPending = ref({});
const deletingTurnUuid = ref("");
let ws = null;
let wsConversationUuid = "";
let reconnectTimer = null;
let operationResyncTimer = null;
let operationResyncInFlight = null;
let timelinePageGeneration = 0;
let timelinePageRequestToken = 0;
let timelinePageConversationUuid = "";
let timelinePageInitialized = false;
let componentMounted = false;
let loadRequestGeneration = 0;
let scrollFrame = 0;
let activeTurnScrollFrame = 0;
let activeTurnScrollTimer = 0;
let lastActiveTurnScrollUpdateAt = 0;
let streamFlushTimer = 0;
let streamFlushFrame = 0;
let streamFlushPending = false;
let pendingProjectionOps = null;
let pendingScrollImpact = "none";
let pendingPreserveAnchor = null;
let pendingTerminalFrame = null;
let programmaticScrollDepth = 0;
let userScrollIntentAt = 0;
let lastScrollerScrollTop = 0;
let touchScrollClientY = null;
let pinnedActiveTurnIndex = null;
let scrollerResizeObserver = null;
let scrollerReflowFrame = 0;
let observedScrollerWidth = 0;
// Reading position of the last settled scroll, in pre-reflow coordinates.
let readingAnchor = null;
let explicitUnlockAt = 0;
let visibleOutputSignature = "";
let lastVisibleOutputAt = 0;
const stateStatsByOpId = new Map();
const sentAttachmentPreviewUrls = new Set();
const localToServerTransitionUuid = ref("");
let defaultsRequestSeq = 0;
let appliedDefaultsRevision = 0;
let localDefaultsFolderId = null;
let localFolderDefaults = {};
let localDefaultsOverrides = {};
let localDefaultsLoading = null;
let optionsLoadPromise = null;
let agentAutoOpenBoundaryConversation = "";
let agentAutoOpenPendingConversation = "";
let sendAttemptGeneration = 0;
let connectionResumePromise = null;
const outboundSends = createOutboundSendTracker({
	onTimeout: (pending) => recoverUnconfirmedSend(pending),
});
const handleExternalRefresh = async () => {
	const shouldFocus = isLocalConversation.value;
	await load({scrollMode: "preserve"});
	if (shouldFocus) await focusComposer();
};

const quickPrompts = [
	"帮我梳理一下当前项目的下一步优先级",
	"检查一下最近一轮工具调用和模型统计是否异常",
	"把这个问题拆成可执行的 TODO，并直接开始处理",
];

const displayMessages = computed(() => [...messages.value]);

async function controlActiveRetry(event = {}, action = "cancel") {
	const retry = event?.retry && typeof event.retry === "object" ? event.retry : event;
	const waitId = String(retry?.waitId || "");
	if (!activeConversationUuid.value || !retry?.active || !waitId || retryActionPending.value.waitId === waitId) return;
	retryActionPending.value = {waitId, action};
	try {
		const taskUuid = String(retry.taskUuid || "");
		const data = action === "retry"
			? await Api.conversationRetryNow(activeConversationUuid.value, waitId, taskUuid)
			: await Api.conversationCancelRetry(activeConversationUuid.value, waitId, taskUuid);
		if (!data?.accepted) throw new Error("当前重试等待已结束或已收到操作");
		ElMessage.success(action === "retry"
			? "已请求立即重试"
			: taskUuid ? "已请求取消 Agent 重试；子任务会结束，主会话可能继续" : "已请求取消重试；本轮进度和统计会保留");
	} catch (error) {
		if (retryActionPending.value.waitId === waitId) retryActionPending.value = {};
		ElMessage.error(apiError(error));
	}
}

function hasOptimisticLocalTurn() {
	return foregroundRunning.value
		&& messages.value.some((msg) => String(msg?.id || "").startsWith("local-"));
}

function shouldPreserveOptimisticMessages(state) {
	const serverMessages = Array.isArray(state?.messages) ? state.messages : [];
	return hasOptimisticLocalTurn() && !serverMessages.length;
}

const operationStatsPayloads = computed(() => orderedOpIds.value
.map((id) => operationsById.value.get(id))
.filter((op) => op?.opType === "stats" && op.payload)
.map((op) => op.payload));
const latestStatsPayload = computed(() => operationStatsPayloads.value.at(-1) || null);
const usage = computed(() => {
	const base = chatState.value?.usage || {};
	const stats = latestStatsPayload.value || lastStats.value || null;
	if (!stats) return base;
	const last = stats.lastUsage || {};
	const aggregate = stats.usage || {};
	const statsLastInput = Number(last.inputTokens || stats.contextTokens || 0);
	const statsLastCacheRead = Number(last.cacheReadTokens || 0);
	const statsLastCacheWrite = Number(last.cacheWriteTokens || 0);
	return {
		...base,
		input_tokens: Number(base.input_tokens || aggregate.inputTokens || 0),
		output_tokens: Number(base.output_tokens || aggregate.outputTokens || 0),
		cache_read_tokens: Number(base.cache_read_tokens || aggregate.cacheReadTokens || 0),
		cache_write_tokens: Number(base.cache_write_tokens || aggregate.cacheWriteTokens || 0),
		// “上下文”是最近一次模型 API 调用的 prompt 体积。
		// 有 live/operation stats 时它比持久化 session aggregate 更新，必须优先用 stats；
		// 否则刷新前会沿用上一轮 base.last_*，刷新后才跳回 DB 里的真实 last_*。
		last_input_tokens: Number(statsLastInput || base.last_input_tokens || 0),
		last_cache_read_tokens: Number(statsLastCacheRead || base.last_cache_read_tokens || 0),
		last_cache_write_tokens: Number(statsLastCacheWrite || base.last_cache_write_tokens || 0),
		cost_usd: Number(base.cost_usd || 0),
	};
});
const serverContextUsage = computed(() => {
	const value = chatState.value?.contextUsage;
	return value && typeof value.known === "boolean" ? value : null;
});
const legacyContextTokens = computed(() => Math.max(0,
	Number(usage.value.last_input_tokens || 0)
	+ Number(usage.value.last_cache_read_tokens || 0)
	+ Number(usage.value.last_cache_write_tokens || 0),
));
const contextUsage = computed(() => resolveContextUsage(
	serverContextUsage.value,
	legacyContextTokens.value,
));
const lastContextTokens = computed(() => contextUsage.value.known ? Number(contextUsage.value.tokens || 0) : 0);
const modelCallRows = computed(() => Array.isArray(chatState.value?.modelCalls) ? chatState.value.modelCalls : []);
const toolCallRows = computed(() => Array.isArray(chatState.value?.toolCalls) ? chatState.value.toolCalls : []);
const turns = computed(() => withTransientIdleThinking(attachTurnStats(buildTurns(displayMessages.value), modelCallRows.value, toolCallRows.value)));
const activeTurn = computed(() => turns.value[Math.min(Math.max(0, activeTurnIndex.value), Math.max(0, turns.value.length - 1))] || null);
const activeConversationUuid = computed(() => props.conversationUuid || chatState.value?.conversationUuid || "");
const messageVisibility = createMessageVisibility({
	conversationUuid: activeConversationUuid, operations: operationsById, api: Api,
	preserveSelectionPosition: () => window.matchMedia('(max-width: 760px)').matches,
	beforeChange: () => ({uuid: activeConversationUuid.value, anchor: captureScrollAnchor()}),
	afterChange: async saved => { await nextTick(); if (saved?.uuid === activeConversationUuid.value) applyReadingAnchor(saved.anchor); },
	onError: error => ElMessage.error(apiError(error)),
});
const hiddenMessageCount = computed(() => messageVisibility.items.value.length);
provide(MESSAGE_VISIBILITY, messageVisibility);
provide(WORK_MOTION, {capture: captureWorkMotion, restore: restoreWorkMotion, finish: finishWorkMotion});
const isLocalConversation = computed(() => String(props.conversationUuid || "").startsWith("local:"));
const modelMutationCounts = ref(new Map());
function modelMutationKey(conversationUuid = activeConversationUuid.value, folderId = props.folderId) {
	return `${String(conversationUuid || "")}\u0000${String(folderId || "")}`;
}
const modelMutating = computed(() => Number(modelMutationCounts.value.get(modelMutationKey()) || 0) > 0);
let runConfigInteractionGeneration = 0;
const runConfigSaves = createRunConfigSaveQueue({
	captureScope: () => runConfigInteractionGeneration,
	isCurrent: (uuid, generation) => Boolean(componentMounted && generation === runConfigInteractionGeneration && !isLocalConversation.value && String(props.conversationUuid || "") === uuid),
	apply: (runConfig) => {
		runConfigOverride.value = runConfig;
	},
});
const displayedRunConfig = computed(() => runConfigForDisplay(
	chatState.value,
	runConfigOverride.value,
	activeConversationUuid.value,
));
const conversationTitle = computed(() => {
	const canonical = String(props.canonicalTitle || "").trim();
	if (canonical) return canonical;
	const title = String(chatState.value?.conversation?.title || "").trim();
	if (title) return title;
	return isLocalConversation.value ? "新会话" : "会话";
});
const localModel = ref("");
const localThinking = ref(DEFAULT_NEW_CONVERSATION_THINKING);
const localFast = ref(false);
const localAgentModel = ref("");
const localAgentThinking = ref("");
const localAgentFast = ref(null);
const localContextStrategy = ref("sliding_window");
const contextStrategy = computed(() => isLocalConversation.value ? localContextStrategy.value : (displayedRunConfig.value?.contextStrategy || "sliding_window"));
const strategySaving = ref(false);
const compactingUuid = ref("");
const compacting = computed(() => compactingUuid.value === activeConversationUuid.value);
const currentModel = computed(() => (isLocalConversation.value && localModel.value) ? localModel.value : (displayedRunConfig.value?.model || ""));
const currentThinking = computed(() => (isLocalConversation.value && localThinking.value) ? localThinking.value : (displayedRunConfig.value?.thinkingLevel || ""));
const effectiveThinking = computed(() => displayedRunConfig.value?.effectiveThinkingLevel || currentThinking.value || "off");
const currentFast = computed(() => (isLocalConversation.value ? localFast.value : Boolean(displayedRunConfig.value?.fastMode || displayedRunConfig.value?.effectiveFastMode)));
const currentModelInfo = computed(() => modelOptions.value.find((m) => m.key === currentModel.value) || null);
const currentThinkLevels = computed(() => {
	const stateLevels = Array.isArray(displayedRunConfig.value?.thinkingLevels) ? displayedRunConfig.value.thinkingLevels.filter(Boolean) : [];
	if (!isLocalConversation.value && stateLevels.length) return stateLevels;
	return Array.isArray(currentModelInfo.value?.thinkingLevels) ? currentModelInfo.value.thinkingLevels.filter(Boolean) : [];
});
const supportsThinking = computed(() => currentThinkLevels.value.length > 0);
const fastSupported = computed(() => Boolean(currentModelInfo.value?.supportsFast || displayedRunConfig.value?.fastSupported));
const agentRunConfig = computed(() => displayedRunConfig.value?.agentRunConfig || null);
const agentModel = computed(() => {
	if (isLocalConversation.value) return localAgentModel.value || "";
	return String(agentRunConfig.value?.model || "");
});
const agentThinkLevel = computed(() => {
	if (isLocalConversation.value) return localAgentThinking.value || "";
	return String(agentRunConfig.value?.thinkLevel || "");
});
const agentFastMode = computed(() => {
	if (isLocalConversation.value) return localAgentFast.value;
	const raw = agentRunConfig.value?.fastMode;
	return raw === true || raw === false ? raw : null;
});
const agentEffective = computed(() => agentRunConfig.value?.effective || null);
const agentEffectiveModel = computed(() => {
	if (agentModel.value) return agentModel.value;
	return String(agentEffective.value?.model || currentModel.value || "");
});
const agentEffectiveModelInfo = computed(() => modelOptions.value.find((m) => m.key === agentEffectiveModel.value) || null);
const agentThinkLevels = computed(() => {
	const fromState = Array.isArray(agentEffective.value?.thinkingLevels) ? agentEffective.value.thinkingLevels.filter(Boolean) : [];
	if (fromState.length) return fromState;
	return Array.isArray(agentEffectiveModelInfo.value?.thinkingLevels) ? agentEffectiveModelInfo.value.thinkingLevels.filter(Boolean) : [];
});
const agentSupportsThinking = computed(() => agentThinkLevels.value.length > 0 || Boolean(agentEffective.value?.supportsThinking));
const agentDefaultThinkingLabel = computed(() => {
	const level = agentEffective.value?.defaultThinkingLevel || modelDefaultThinking(agentEffectiveModelInfo.value);
	return level ? thinkingLabel(level) : "模型默认";
});
const agentEffectiveThinking = computed(() => String(
	agentEffective.value?.thinkLevel || agentThinkLevel.value || agentEffective.value?.defaultThinkingLevel || "off",
));
const agentFastSupported = computed(() => Boolean(agentEffectiveModelInfo.value?.supportsFast || agentEffective.value?.fastSupported));
const agentEffectiveFast = computed(() => {
	if (agentFastMode.value === true) return agentFastSupported.value;
	if (agentFastMode.value === false) return false;
	return Boolean(agentEffective.value?.fastMode ?? (currentFast.value && agentFastSupported.value));
});
const contextWindow = computed(() => Number(currentModelInfo.value?.contextWindow || displayedRunConfig.value?.contextWindow || 0));
const rolloverTriggerTokens = computed(() => {
	const explicit = Number(currentModelInfo.value?.rolloverTriggerTokens || displayedRunConfig.value?.rolloverTriggerTokens || 0);
	if (explicit > 0) return explicit;
	const ratio = Number(currentModelInfo.value?.windowTriggerRatio || displayedRunConfig.value?.windowTriggerRatio || 0.7);
	return contextWindow.value ? Math.round(contextWindow.value * ratio) : 0;
});
const contextPercent = computed(() => rolloverTriggerTokens.value ? Math.min(999, lastContextTokens.value * 100 / rolloverTriggerTokens.value) : 0);
const canCompact = computed(() => !isLocalConversation.value && !running.value && !compacting.value && contextStrategy.value === "model_summary" && contextUsage.value.known && contextPercent.value >= Number(displayedRunConfig.value?.manualCompactMinPercent ?? 50));
const contextUsedDisplay = computed(() => contextUsage.value.known ? fmtTokens(lastContextTokens.value) : "待实测");
const contextThresholdDisplay = computed(() => rolloverTriggerTokens.value ? fmtTokens(rolloverTriggerTokens.value) : "∞");
const contextWindowDisplay = computed(() => contextWindow.value ? fmtTokens(contextWindow.value) : "∞");
const contextPercentDisplay = computed(() => contextUsage.value.known && rolloverTriggerTokens.value ? `${contextPercent.value.toFixed(1)}%` : "—");
const contextDisplay = computed(() => {
	if (!rolloverTriggerTokens.value) return `${contextUsedDisplay.value} / ∞`;
	return `${contextUsedDisplay.value} / ${contextThresholdDisplay.value}（${contextPercentDisplay.value}）`;
});
const sessionLedgerUsage = computed(() => normalizeLedgerUsageBaseline(chatState.value?.usage || {}));
const totalTokenParts = computed(() => ledgerTokenParts(sessionLedgerUsage.value));
const totalTokens = computed(() => totalTokenParts.value.input + totalTokenParts.value.output);
const totalTokensDisplay = computed(() => fmtTokens(totalTokens.value));
const totalTokensDetail = computed(() => `${totalTokens.value.toLocaleString("en-US")} Tokens（输入 + 输出，缓存已包含在输入中）\n${tokenLine(totalTokenParts.value)}`);
const totalDurationMs = computed(() => {
	const liveMs = latestStatsPayload.value?.durationMs || lastStats.value?.durationMs || 0;
	return totalSessionDurationMs({
		turns: turns.value,
		modelCalls: modelCallRows.value,
		liveMs,
		timelineTotalDurationMs: chatState.value?.timelineTotalDurationMs,
		ledgerUsage: sessionLedgerUsage.value,
	});
});
const totalDurationDisplay = computed(() => fmtMs(totalDurationMs.value));
// Session/model_calls is the durable per-request billing ledger used by the
// sidebar and backend statistics. Timeline stats are per-turn presentation
// snapshots and may be partial, compacted, or replayed; never sum them as the
// conversation total.
const totalCostUsd = computed(() => Number(sessionLedgerUsage.value.cost_usd || 0));
const totalCostDisplay = computed(() => fmtCost(totalCostUsd.value));
const canSend = computed(() => {
	if (sendPending.value || modelMutating.value || compacting.value || attachmentRestoring.value) return false;
	if (running.value) return Boolean(draft.value.trim()) && pendingAttachments.value.length === 0;
	return Boolean(draft.value.trim() || pendingAttachments.value.length);
});
const modelGroups = computed(() => {
	const q = modelQuery.value.trim().toLowerCase();
	const groups = new Map();
	for (const model of modelOptions.value) {
		const hay = `${model.key || ""} ${model.label || ""} ${model.provider || ""} ${model.protocol || ""}`.toLowerCase();
		if (q && !hay.includes(q)) continue;
		const key = model.provider || "default";
		if (!groups.has(key)) groups.set(key, []);
		groups.get(key).push(model);
	}
	return Array.from(groups.entries()).map(([provider, models]) => ({provider, models}));
});

function loadDraftStore() {
	if (typeof window === "undefined") return {};
	try {
		const parsed = JSON.parse(window.localStorage.getItem(DRAFT_STORAGE_KEY) || "{}");
		return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

function loadAgentPanelIntents() {
	if (typeof window === "undefined") return {};
	try {
		return normalizeAgentPanelIntents(JSON.parse(window.sessionStorage.getItem(AGENT_PANEL_INTENT_STORAGE_KEY) || "{}"));
	} catch {
		return {};
	}
}

function saveAgentPanelIntents() {
	if (typeof window === "undefined") return;
	try {
		window.sessionStorage.setItem(AGENT_PANEL_INTENT_STORAGE_KEY, JSON.stringify(normalizeAgentPanelIntents(detailOpen.value)));
	} catch {
		// sessionStorage may be unavailable; in-memory intent remains authoritative for this mount.
	}
}

function saveDraftStore(next) {
	draftByConversation.value = next;
	if (typeof window === "undefined") return;
	try {
		window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(next));
	} catch {
		// localStorage may be unavailable or full; in-memory drafts still work for this tab.
	}
}

function draftKey(uuid = props.conversationUuid || chatState.value?.conversationUuid || "local:new") {
	return String(uuid || "local:new");
}

function setDraftForConversation(uuid, text) {
	const key = draftKey(uuid);
	const value = String(text || "");
	const next = {...draftByConversation.value};
	if (value.trim()) next[key] = value;
	else delete next[key];
	saveDraftStore(next);
}

function persistComposerDraft(value) {
	const pending = outboundSends.current;
	const preparing = pending && !pending.storageReleased && draftKey(pending.conversationUuid) === draftKey(props.conversationUuid);
	setDraftForConversation(props.conversationUuid, preparing ? restoreOutboundDraft(pending.draftText, value) : value);
}

function restoreDraftForConversation(uuid = props.conversationUuid) {
	if (!componentMounted || draftKey(uuid) !== draftKey(props.conversationUuid)) return;
	restoringDraft.value = true;
	draft.value = String(draftByConversation.value[draftKey(uuid)] || "");
	void restoreAttachmentsForConversation(uuid);
	nextTick(() => {
		restoringDraft.value = false;
		adjustComposerHeight();
	});
}

function clearDraftForConversation(uuid = props.conversationUuid) {
	setDraftForConversation(uuid, "");
}

function clearDraftAndAttachments() {
	const key = activeAttachmentKey();
	clearDraftForConversation();
	const hydration = attachmentHydrations.get(key);
	if (hydration) { hydration.cleared = true; hydration.dirty = true; }
	clearAttachments();
	void attachmentDrafts.remove(key);
}

function discardConversationDraft(conversationUuid) {
	const key = String(conversationUuid || "");
	if (!key) return;
	clearDraftForConversation(key);
	// Deleting a conversation invalidates the read even if it is still off screen.
	attachmentHydrations.delete(draftKey(key));
	if (activeAttachmentKey() === draftKey(key)) attachmentRestoring.value = false;
	releaseStashedAttachments(key);
	void attachmentDrafts.remove(draftKey(key));
	if (draftKey(props.conversationUuid) === key) {
		draft.value = "";
		clearAttachments();
		adjustComposerHeight();
	}
}
defineExpose({discardConversationDraft, focusPendingInteraction, captureMobileViewportAnchor, restoreMobileViewportAnchor});

function primaryModelInfo() {
	const preferred = currentPrimaryModelKey.value || primaryModelKey.value;
	return modelOptions.value.find((m) => m.key === preferred)
		|| modelOptions.value.find((m) => m.primary)
		|| modelOptions.value[0]
		|| null;
}

function applyDefaultLocalModel() {
	const model = primaryModelInfo();
	if (!model) {
		localModel.value = "";
		localThinking.value = DEFAULT_NEW_CONVERSATION_THINKING;
		localFast.value = false;
		localAgentModel.value = "";
		localAgentThinking.value = "";
		localAgentFast.value = null;
		return null;
	}
	localModel.value = model.key;
	localThinking.value = modelDefaultThinking(model);
	localFast.value = false;
	localAgentModel.value = "";
	localAgentThinking.value = "";
	localAgentFast.value = null;
	return model;
}

function applyLocalRunDefaults(defaults = {}) {
	localContextStrategy.value = defaults.contextStrategy === "model_summary" ? "model_summary" : "sliding_window";
	const main = modelOptions.value.find((item) => item.key === String(defaults.mainModel || "")) || primaryModelInfo();
	if (!main) return applyDefaultLocalModel();
	const mainLevels = modelThinkingLevels(main);
	const requestedThinking = String(defaults.mainThinkingLevel || "");
	localModel.value = main.key;
	localThinking.value = mainLevels.includes(requestedThinking) ? requestedThinking : (modelDefaultThinking(main) || "off");
	localFast.value = defaults.mainFastMode === true && Boolean(main.supportsFast);

	const requestedAgentModel = String(defaults.agentModel || "");
	const agentInfo = modelOptions.value.find((item) => item.key === requestedAgentModel) || null;
	localAgentModel.value = agentInfo ? requestedAgentModel : "";
	const effectiveAgentInfo = agentInfo || main;
	const agentLevels = modelThinkingLevels(effectiveAgentInfo);
	const requestedAgentThinking = String(defaults.agentThinkLevel || "");
	localAgentThinking.value = agentLevels.includes(requestedAgentThinking) ? requestedAgentThinking : "";
	if (defaults.agentFastMode === false) localAgentFast.value = false;
	else if (defaults.agentFastMode === true && effectiveAgentInfo?.supportsFast) localAgentFast.value = true;
	else localAgentFast.value = null;
	return main;
}

function completeLocalRunConfig() {
	return {
		contextStrategy: localContextStrategy.value,
		mainModel: localModel.value,
		mainThinkingLevel: localThinking.value || "off",
		mainFastMode: Boolean(localFast.value),
		agentModel: localAgentModel.value || "",
		agentThinkLevel: localAgentThinking.value || "",
		agentFastMode: localAgentFast.value === true || localAgentFast.value === false ? localAgentFast.value : null,
	};
}

async function loadLocalRunDefaults(uuid = props.conversationUuid, {preserveManual = false} = {}) {
	const requestSeq = ++defaultsRequestSeq;
	const expectedUuid = String(uuid || "");
	const folderId = String(props.folderId || "");
	if (!preserveManual) localDefaultsOverrides = {};
	localDefaultsFolderId = null;
	const pending = {uuid: expectedUuid, folderId, promise: null};
	localDefaultsLoading = pending;
	pending.promise = (async () => {
		try {
			const data = await Api.conversationDefaults(folderId ? {folderId} : {});
			if (requestSeq !== defaultsRequestSeq || props.conversationUuid !== expectedUuid || String(props.folderId || "") !== folderId || !isLocalConversation.value) return false;
			const defaults = data?.defaults || {};
			appliedDefaultsRevision = Number(defaults.revision || 0);
			localFolderDefaults = data?.folderDefaults || {};
			localDefaultsFolderId = folderId;
			applyLocalRunDefaults({...defaults, ...localDefaultsOverrides});
			return true;
		} catch {
			// Both temporary and project drafts must load their scoped defaults
			// before sending, never silently use a previous group's cost tier.
			return false;
		} finally {
			if (localDefaultsLoading === pending) localDefaultsLoading = null;
		}
	})();
	return pending.promise;
}

async function ensureLocalRunDefaults() {
	const folderId = String(props.folderId || "");
	if (!isLocalConversation.value || localDefaultsFolderId === folderId) return;
	const pending = localDefaultsLoading;
	const loaded = await (pending?.folderId === folderId && pending.uuid === String(props.conversationUuid || "")
		? pending.promise : loadLocalRunDefaults());
	if (!loaded) throw new Error("无法读取会话默认配置，请检查连接后重试");
}

async function patchLocalRunDefaults(patch) {
	const expectedUuid = String(props.conversationUuid || "");
	const folderId = String(props.folderId || "");
	await ensureLocalRunDefaults();
	if (props.conversationUuid !== expectedUuid || String(props.folderId || "") !== folderId || !isLocalConversation.value) return null;
	const requestSeq = ++defaultsRequestSeq;
	if (Object.keys(localFolderDefaults).length || Object.hasOwn(patch, "contextStrategy")) {
		localDefaultsOverrides = {...localDefaultsOverrides, ...patch};
		// A one-off choice in a project draft is not a global preference update
		// and must not reset the other fields inherited from that project.
		applyLocalRunDefaults({...completeLocalRunConfig(), ...patch});
		resetLocalConversationState(expectedUuid || "local:new");
		return completeLocalRunConfig();
	}
	const data = await Api.updateConversationDefaults(patch);
	const defaults = data?.defaults || {};
	const revision = Number(defaults.revision || 0);
	if (requestSeq !== defaultsRequestSeq || props.conversationUuid !== expectedUuid || String(props.folderId || "") !== folderId || !isLocalConversation.value || revision < appliedDefaultsRevision) {
		return defaults || null;
	}
	appliedDefaultsRevision = revision;
	localDefaultsOverrides = {...localDefaultsOverrides, ...patch};
	applyLocalRunDefaults({...defaults, ...(Object.hasOwn(localDefaultsOverrides, "contextStrategy") ? {contextStrategy: localDefaultsOverrides.contextStrategy} : {})});
	resetLocalConversationState(expectedUuid || "local:new");
	return defaults || null;
}

async function refreshFolderRunDefaults({preserveManual = false} = {}) {
	if (!isLocalConversation.value || outboundSends.current) return;
	const uuid = String(props.conversationUuid || "");
	const folderId = String(props.folderId || "");
	await loadOptions();
	const loaded = await loadLocalRunDefaults(uuid, {preserveManual});
	if (loaded && isLocalConversation.value && props.conversationUuid === uuid && String(props.folderId || "") === folderId && !outboundSends.current) resetLocalConversationState(uuid);
}

function handleFolderPropertiesChanged(event) {
	const temporary = event?.detail?.folderId === "__temporary";
	if (isLocalConversation.value && Boolean(props.folderId) !== temporary) void refreshFolderRunDefaults({preserveManual: true});
}

function statsUsageSnapshot(stats = {}) {
	const own = stats?.usage || {};
	const expert = stats?.expertUsage || {};
	const last = stats?.lastUsage || {};
	return {
		input_tokens: Number(own.inputTokens || 0) + Number(expert.inputTokens || 0),
		output_tokens: Number(own.outputTokens || 0) + Number(expert.outputTokens || 0),
		cache_read_tokens: Number(own.cacheReadTokens || 0) + Number(expert.cacheReadTokens || 0),
		cache_write_tokens: Number(own.cacheWriteTokens || 0) + Number(expert.cacheWriteTokens || 0),
		last_input_tokens: Number(last.inputTokens || stats?.contextTokens || 0),
		last_cache_read_tokens: Number(last.cacheReadTokens || 0),
		last_cache_write_tokens: Number(last.cacheWriteTokens || 0),
	};
}

function mergeLedgerUsageIntoState(ledgerUsage = null) {
	if (!chatState.value) return false;
	const result = applyLedgerUsageSnapshot(chatState.value.usage || {}, ledgerUsage);
	if (!result.applied) return false;
	chatState.value = {...chatState.value, usage: result.usage};
	return true;
}

function mergeStatsUsageIntoState(opId, stats = {}) {
	const key = String(opId || "");
	if (!key || !chatState.value || !stats || typeof stats !== "object") return;
	// Per-turn usage remains presentation/context data. Header totals are replaced
	// only from the revisioned absolute sessions ledger, never added to task/stats.
	const next = statsUsageSnapshot(stats);
	stateStatsByOpId.set(key, next);
	const base = chatState.value.usage || {};
	const hasLedgerUsage = Boolean(stats.ledgerUsage && typeof stats.ledgerUsage === "object");
	const ledgerApplied = mergeLedgerUsageIntoState(stats.ledgerUsage);
	const ledgerCost = Number(stats?.ledgerCostUsd);
	const usageBase = chatState.value.usage || base;
	const canApplyLegacyLedgerCost = Number(usageBase.ledger_revision || 0) <= 0;
	const nextContextUsage = mergeStatsContextUsage(
		chatState.value.contextUsage,
		stats.contextUsage,
		{
			rolloverTriggerTokens: Number(chatState.value.rolloverTriggerTokens || 0),
		},
	);
	chatState.value = {
		...chatState.value,
		contextUsage: nextContextUsage,
		usage: {
			...usageBase,
			last_input_tokens: next.last_input_tokens || Number(usageBase.last_input_tokens || 0),
			last_cache_read_tokens: next.last_cache_read_tokens || Number(usageBase.last_cache_read_tokens || 0),
			last_cache_write_tokens: next.last_cache_write_tokens || Number(usageBase.last_cache_write_tokens || 0),
			cost_usd: canApplyLegacyLedgerCost && !hasLedgerUsage && !ledgerApplied && Number.isFinite(ledgerCost) && ledgerCost >= 0
				? ledgerCost
				: Number(usageBase.cost_usd || 0),
		},
	};
}

function resetTimelinePagination(conversationUuid = "") {
	timelinePageGeneration += 1;
	timelinePageInFlight.value = null;
	timelinePageConversationUuid = String(conversationUuid || "");
	timelinePageInitialized = false;
	hasMoreBefore.value = false;
	nextBeforeDisplaySeq.value = null;
	userScrollIntentAt = 0;
	touchScrollClientY = null;
	lastScrollerScrollTop = Number(scroller.value?.scrollTop || 0);
	toolDetailCache.reset(conversationUuid);
}

function resetOperationStore(conversationUuid = "") {
	conversationStateRequests.invalidate();
	operationFrameBuffer.reset();
	pendingLoadReplaceOperations = false;
	operationsById.value = new Map();
	orderedOpIds.value = [];
	revisionByOpId.value = new Map();
	stateStatsByOpId.clear();
	lastFrameSeq.value = 0;
	resetTimelinePagination(conversationUuid);
}

function applyCommittedTurnDeletion(conversationUuid, deletedRootTurns = []) {
	const deleted = new Set((Array.isArray(deletedRootTurns) ? deletedRootTurns : []).map(String));
	const survivors = deleted.size ? orderedOperationsList().filter((op) => (
		!deleted.has(String(op.turnId || op.turnUuid || ""))
		&& !deleted.has(String(op.runRootTurnId || op.runRootTurnUuid || ""))
	)) : [];
	closeWs();
	loadRequestGeneration += 1;
	resetOperationStore(conversationUuid);
	replaceOperationSnapshots(survivors);
	messages.value = projectOperationMessages(survivors);
	chatState.value = {...(chatState.value || {}), operations: survivors, running: false, backgroundRunning: false};
	running.value = false;
	foregroundRunning.value = false;
	rootTurnRunning.value = false;
	clearActiveRun();
	lastStats.value = null;
	runStartedAt.value = 0;
	status.value = "就绪";
}

function clearUiCaches() {
	clearMarkdownCache();
	toolResultTabs.value = {};
	detailOpen.value = loadAgentPanelIntents();
	pendingSteering.value = [];
}

function resetLocalConversationState(uuid = props.conversationUuid || "local:new") {
	pinnedActiveTurnIndex = null;
	readingAnchor = null;
	activeTurnIndex.value = 0;
	closeWs();
	clearUiCaches();
	resetOperationStore(uuid);
	messages.value = [];
	lastStats.value = null;
	running.value = false;
	foregroundRunning.value = false;
	rootTurnRunning.value = false;
	clearActiveRun();
	runStartedAt.value = 0;
	status.value = "未发送";
	const model = currentModelInfo.value || primaryModelInfo();
	chatState.value = {
		conversationUuid: uuid,
		sessionUuid: "",
		model: localModel.value || model?.key || "",
		thinkingLevel: localThinking.value || modelDefaultThinking(model) || "",
		effectiveThinkingLevel: localThinking.value || modelDefaultThinking(model) || "off",
		thinkingLevels: modelThinkingLevels(model),
		defaultThinkingLevel: modelDefaultThinking(model),
		fastMode: localFast.value && Boolean(model?.supportsFast),
		effectiveFastMode: localFast.value && Boolean(model?.supportsFast),
		fastSupported: Boolean(model?.supportsFast),
		agentRunConfig: buildLocalAgentRunConfig(),
		rolloverTriggerTokens: Number(model?.rolloverTriggerTokens || 0),
		usage: normalizeLedgerUsageBaseline({}),
		modelCalls: [],
		toolCalls: [],
		operations: [],
		frameSeq: 0,
		facts: {latestFrameSeq: 0, activeForegroundTurnIds: [], activeBackgroundTurnIds: []},
		conversation: {conversationUuid: uuid, title: "新会话", currentStatus: "未发送"},
	};
}

function detailKey(...parts) {
	return parts.map((part) => String(part ?? "").replace(/[:\s]+/g, "_")).join(":");
}

function isDetailOpen(key) {
	const value = detailOpen.value[key];
	return value === true || value === "open" || value === "auto";
}

function setAgentPanelIntent(key, intent) {
	if (!key || !isAgentPanelDetailKey(key) || !["auto", "open", "closed"].includes(intent)) return;
	if (detailOpen.value[key] === intent) return;
	detailOpen.value = {...detailOpen.value, [key]: intent};
	saveAgentPanelIntents();
}

const DETAILS_REVEAL_SELECTOR = "details.tool-event:not(.reasoning-card), details.agent-tool-event";
const DETAILS_REVEAL_MARGIN_PX = 24;

function revealExpandedDetails(details) {
	if (!details?.matches?.(DETAILS_REVEAL_SELECTOR)) return;
	void nextTick().then(() => {
		window.requestAnimationFrame(() => {
			const scrollContainer = scroller.value;
			if (!scrollContainer || !details.isConnected || !details.open || !scrollContainer.contains(details)) return;
			const scrollerRect = scrollContainer.getBoundingClientRect();
			const detailsRect = details.getBoundingClientRect();
			const availableHeight = Math.max(0, scrollerRect.height - DETAILS_REVEAL_MARGIN_PX);
			const reducedMotion = Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
			runProgrammaticScroll(() => {
				details.scrollIntoView({
					behavior: reducedMotion ? "auto" : "smooth",
					block: detailsRect.height <= availableHeight ? "center" : "start",
					inline: "nearest",
				});
			}, reducedMotion ? 120 : 700);
		});
	});
}

function onDetailsToggle(event, key) {
	const details = event?.currentTarget;
	const open = Boolean(details?.open ?? event?.target?.open);
	const wasOpen = isDetailOpen(key);
	if (isAgentPanelDetailKey(key)) {
		const currentIntent = detailOpen.value[key];
		// Setting :open from an automatic hydration can emit an untrusted toggle;
		// keep its `auto` provenance until the user explicitly clicks the summary.
		if (event?.isTrusted === false && currentIntent === "auto") return;
		setAgentPanelIntent(key, open ? "open" : "closed");
		if (open && !wasOpen) revealExpandedDetails(details);
		return;
	}
	if (open) {
		if (detailOpen.value[key]) return;
		detailOpen.value = {...detailOpen.value, [key]: true};
		revealExpandedDetails(details);
		return;
	}
	if (!detailOpen.value[key]) return;
	const next = {...detailOpen.value};
	delete next[key];
	detailOpen.value = next;
}

function applyAgentAutoOpenDecision(decision, conversationUuid) {
	if (decision.action === "pending") {
		agentAutoOpenPendingConversation = conversationUuid;
		return;
	}
	agentAutoOpenPendingConversation = "";
	if (decision.action === "open") setAgentPanelIntent(decision.key, "auto");
}

function hydrateAgentAutoOpenBoundary(conversationUuid, operations = [], runState = null) {
	const uuid = String(conversationUuid || "").trim();
	if (!uuid || uuid.startsWith("local:") || agentAutoOpenBoundaryConversation === uuid) return;
	agentAutoOpenBoundaryConversation = uuid;
	applyAgentAutoOpenDecision(decideAgentAutoOpen({
		conversationUuid: uuid,
		operations,
		runState: runState || deriveOperationRunState(operations),
		intents: detailOpen.value,
	}), uuid);
}

function consumePendingAgentAutoOpen(operations = [], runState = null) {
	const uuid = String(activeConversationUuid.value || "").trim();
	if (!uuid || agentAutoOpenPendingConversation !== uuid) return;
	const decision = decideAgentAutoOpen({
		conversationUuid: uuid,
		operations,
		runState: runState || deriveOperationRunState(operations),
		intents: detailOpen.value,
	});
	if (decision.action !== "pending") applyAgentAutoOpenDecision(decision, uuid);
}

function resetAgentAutoOpenBoundary() {
	agentAutoOpenBoundaryConversation = "";
	agentAutoOpenPendingConversation = "";
}

function onReasoningDetailsToggle(event, key, active) {
	if (!key) return;
	if (active) {
		if (event?.target && !event.target.open) event.target.open = true;
		return;
	}
	onDetailsToggle(event, key);
}

function focusPendingInteraction(target) {
	if (!target?.interactionId || target.conversationUuid !== props.conversationUuid) return;
	pendingInteractionFocus.value = {...target};
	void nextTick(tryFocusPendingInteraction);
}
function tryFocusPendingInteraction() {
	const target = pendingInteractionFocus.value;
	if (!target) return;
	if (target.conversationUuid !== props.conversationUuid) {
		pendingInteractionFocus.value = null;
		return;
	}
	if (loading.value || chatState.value?.conversationUuid !== target.conversationUuid) return;
	if (!pendingConfirmations.value.some(item => item.confirmationId === target.interactionId)) return;
	if (composer.value?.focusInteraction(target.interactionId)) pendingInteractionFocus.value = null;
}
watch(() => [props.conversationUuid, loading.value, chatState.value, pendingConfirmations.value], tryFocusPendingInteraction, {flush: "post"});

function normalizeConfirmations(list = []) {
	return Array.isArray(list) ? list.filter((item) => item?.confirmationId) : [];
}

function updatePendingConfirmations(list = []) {
	pendingConfirmations.value = normalizeConfirmations(list);
}

async function answerPendingConfirmation(item, answer = {}) {
	const confirmationId = item?.confirmationId;
	const conversationUuid = activeConversationUuid.value;
	if (!confirmationId || !conversationUuid || confirmationSubmitting.value[confirmationId]) return;
	confirmationSubmitting.value = {...confirmationSubmitting.value, [confirmationId]: true};
	const nextErrors = {...confirmationErrors.value};
	delete nextErrors[confirmationId];
	confirmationErrors.value = nextErrors;
	try {
		await Api.answerConversationConfirmation(conversationUuid, confirmationId, {
			...answer,
			revision: interactionRevision(item),
		});
		pendingConfirmations.value = pendingConfirmations.value.filter((x) => x.confirmationId !== confirmationId);
	} catch (error) {
		const statusCode = Number(error?.response?.status || 0);
		const errorCode = interactionErrorCode(error);
		if (isTerminalInteractionError(error)) {
			pendingConfirmations.value = pendingConfirmations.value.filter((x) => x.confirmationId !== confirmationId);
			await load({conversationUuid, scrollMode: "preserve", manageLoading: false, fresh: true});
			return;
		}
		const message = apiError(error);
		confirmationErrors.value = {
			...confirmationErrors.value,
			[confirmationId]: statusCode === 400
				? `回答未能提交：${message || "请检查输入后重试。"}`
				: (statusCode === 409 && errorCode
					? `提交冲突：${message || errorCode}`
					: `提交失败：${message || "请稍后重试。"}`),
		};
	} finally {
		const nextSubmitting = {...confirmationSubmitting.value};
		delete nextSubmitting[confirmationId];
		confirmationSubmitting.value = nextSubmitting;
	}
}

function addAttachment(file) {
	const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
	const item = {id, file};
	pendingAttachments.value.push(item);
	if (file.type?.startsWith("image/")) attachmentPreviews.value[id] = URL.createObjectURL(file);
	persistActiveAttachments();
}

async function removeAttachment(id) {
	const key = activeAttachmentKey();
	const item = pendingAttachments.value.find((entry) => entry.id === id);
	try {
		await ElMessageBox.confirm(`确定移除附件「${item?.file?.name || '未命名附件'}」吗？`, "移除附件", {
			confirmButtonText: "移除",
			cancelButtonText: "取消",
			type: "warning",
		});
	} catch {
		return;
	}
	// The confirmation may outlive a conversation switch. Edit its original owner.
	const active = key === attachmentsLoadedKey;
	const stash = attachmentsByConversation.get(key);
	const files = active ? pendingAttachments.value : (stash?.attachments || []);
	const previews = active ? attachmentPreviews.value : (stash?.previews || {});
	if (previews[id]) URL.revokeObjectURL(previews[id]);
	const next = {...previews};
	delete next[id];
	const attachments = files.filter((entry) => entry.id !== id);
	const hydration = attachmentHydrations.get(key);
	if (hydration) hydration.removed.add(id);
	if (active) {
		attachmentPreviews.value = next;
		pendingAttachments.value = attachments;
	} else if (stash) attachmentsByConversation.set(key, {...stash, attachments, previews: next});
	persistAttachmentList(key, attachments);
}

function clearAttachments({revoke = true} = {}) {
	if (revoke) {
		for (const id of Object.keys(attachmentPreviews.value)) URL.revokeObjectURL(attachmentPreviews.value[id]);
	}
	attachmentPreviews.value = {};
	pendingAttachments.value = [];
}

function activeAttachmentKey() {
	return attachmentsLoadedKey || draftKey();
}

function persistAttachmentList(key, attachments) {
	const hydration = attachmentHydrations.get(key);
	if (hydration) {
		// Do not overwrite the durable files with a not-yet-hydrated partial list.
		hydration.dirty = true;
		return;
	}
	const pending = outboundSends.current;
	const sent = pending?.storageReleased && draftKey(pending.conversationUuid) === key
		? new Set(pending.attachments.map(item => item.id)) : new Set();
	void attachmentDrafts.save(key, attachments.filter(item => !sent.has(item.id)));
}

function persistActiveAttachments() {
	persistAttachmentList(activeAttachmentKey(), pendingAttachments.value);
}

function ensureAttachmentPreviews() {
	let previews = attachmentPreviews.value;
	for (const item of pendingAttachments.value) {
		if (previews[item.id] || !item.file?.type?.startsWith("image/")) continue;
		if (previews === attachmentPreviews.value) previews = {...previews};
		previews[item.id] = URL.createObjectURL(item.file);
	}
	if (previews !== attachmentPreviews.value) attachmentPreviews.value = previews;
}

// Switching away parks the composer's files under the conversation they were
// chosen for. Their preview URLs travel with them: revoking here would leave
// empty thumbnails when the user comes back.
function stashAttachmentsForConversation(uuid) {
	const key = draftKey(uuid);
	if (pendingAttachments.value.length) attachmentsByConversation.set(key, {attachments: pendingAttachments.value, previews: attachmentPreviews.value});
	else attachmentsByConversation.delete(key);
	pendingAttachments.value = [];
	attachmentPreviews.value = {};
	attachmentsLoadedKey = "";
}

function warnSkippedAttachments(skipped = []) {
	if (!skipped.length) return;
	ElMessage.warning({
		message: `${skipped.length} 个较大的附件无法在刷新后保留，请重新选择：${skipped.map((item) => item.fileName).join("、")}`,
		duration: 8000,
	});
}

async function restoreAttachmentsForConversation(uuid = props.conversationUuid) {
	const key = draftKey(uuid);
	if (!componentMounted || key !== draftKey(props.conversationUuid)) return;
	if (attachmentsLoadedKey === key) return attachmentHydrations.get(key)?.promise;
	if (attachmentsLoadedKey) stashAttachmentsForConversation(attachmentsLoadedKey);
	attachmentsLoadedKey = key;
	const stash = attachmentsByConversation.get(key);
	if (stash) {
		attachmentsByConversation.delete(key);
		pendingAttachments.value = stash.attachments;
		attachmentPreviews.value = stash.previews;
		ensureAttachmentPreviews();
		warnSkippedAttachments(stash.skipped);
	}
	const existing = attachmentHydrations.get(key);
	attachmentRestoring.value = Boolean(existing || !stash);
	if (existing) return existing.promise;
	if (stash) return;
	// One hydration per owner, even across A -> B -> A. Edits made meanwhile
	// are an overlay, not a replacement for the unread durable files.
	const hydration = {cleared: false, dirty: false, removed: new Set()};
	attachmentHydrations.set(key, hydration);
	hydration.promise = (async () => {
		const stored = await attachmentDrafts.load(key);
		if (attachmentHydrations.get(key) !== hydration) return;
		const active = componentMounted && attachmentsLoadedKey === key;
		const parked = attachmentsByConversation.get(key);
		const current = hydration.detached || (active ? pendingAttachments.value : (parked?.attachments || []));
		const known = new Set(current.map(item => item.id));
		const restored = hydration.cleared ? [] : stored.items.filter(item => !known.has(item.id) && !hydration.removed.has(item.id));
		const merged = [...current, ...restored];
		const skipped = hydration.cleared ? [] : stored.skipped;
		attachmentHydrations.delete(key);
		if (active) {
			pendingAttachments.value = merged;
			ensureAttachmentPreviews();
			attachmentRestoring.value = false;
			adjustComposerHeight();
			warnSkippedAttachments(skipped);
		} else if (componentMounted) {
			attachmentsByConversation.set(key, {attachments: merged, previews: parked?.previews || {}, skipped});
		}
		// Include both restored files and edits. A clear/delete never revives an
		// old snapshot; skipped-file warnings are consumed once on this page.
		if (hydration.dirty || skipped.length) persistAttachmentList(key, merged);
	})();
	return hydration.promise;
}

function detachAttachmentHydrations() {
	// SPA unmount may precede an IDB read. Finish its persistence without touching
	// the detached composer or creating preview URLs after teardown.
	for (const [key, hydration] of attachmentHydrations) {
		hydration.detached = [...(key === attachmentsLoadedKey
			? pendingAttachments.value : (attachmentsByConversation.get(key)?.attachments || []))];
	}
}

// The local draft just became a real conversation: the same files are still in
// the composer, since only a send receipt releases them. Move their storage key
// instead of parking them under the retired local id.
function migrateAttachmentDraft(from, to) {
	const fromKey = draftKey(from);
	const toKey = draftKey(to);
	if (!fromKey || !toKey || fromKey === toKey) return;
	const stash = attachmentsByConversation.get(fromKey);
	if (stash) {
		attachmentsByConversation.delete(fromKey);
		attachmentsByConversation.set(toKey, stash);
	}
	if (attachmentsLoadedKey === fromKey) attachmentsLoadedKey = toKey;
	void attachmentDrafts.move(fromKey, toKey);
}

// A send receipt belongs to the conversation that was sent to, which may no
// longer be the one on screen. The persisted copy must lose the sent files too,
// or a reload would offer them again.
function releaseSentAttachments(uuid, sentIds) {
	if (!sentIds.size) return;
	const key = draftKey(uuid);
	const keepPreviews = (previews) => Object.fromEntries(Object.entries(previews).filter(([id]) => !sentIds.has(id)));
	if (key === attachmentsLoadedKey) {
		pendingAttachments.value = pendingAttachments.value.filter((item) => !sentIds.has(item.id));
		attachmentPreviews.value = keepPreviews(attachmentPreviews.value);
	} else {
		const stash = attachmentsByConversation.get(key);
		if (stash) {
			const attachments = stash.attachments.filter((item) => !sentIds.has(item.id));
			if (attachments.length) attachmentsByConversation.set(key, {attachments, previews: keepPreviews(stash.previews)});
			else attachmentsByConversation.delete(key);
		}
	}
	void attachmentDrafts.removeItems(key, sentIds);
}

// Files whose send was released return to their own conversation's draft, even
// when the user has already moved on to another one.
function restoreReleasedAttachments(uuid, attachments = []) {
	const key = draftKey(uuid);
	if (key === attachmentsLoadedKey) {
		const known = new Set(pendingAttachments.value.map((item) => item.id));
		const added = attachments.filter((item) => !known.has(item.id));
		if (added.length) {
			pendingAttachments.value = [...pendingAttachments.value, ...added];
			ensureAttachmentPreviews();
		}
		// These files are draft again, so they are stored again even when they never
		// left the composer: sending had cleared their stored copy.
		persistActiveAttachments();
		return;
	}
	const stash = attachmentsByConversation.get(key);
	const parked = stash?.attachments || [];
	const known = new Set(parked.map((item) => item.id));
	const merged = [...parked, ...attachments.filter((item) => !known.has(item.id))];
	if (!merged.length) return;
	// Previews are recreated when this conversation is opened again; a conversation
	// off screen has no thumbnail to keep alive.
	attachmentsByConversation.set(key, {attachments: merged, previews: stash?.previews || {}});
	persistAttachmentList(key, merged);
}

function releaseStashedAttachments(uuid) {
	const key = draftKey(uuid);
	const stash = attachmentsByConversation.get(key);
	if (!stash) return;
	for (const url of Object.values(stash.previews)) if (url) URL.revokeObjectURL(url);
	attachmentsByConversation.delete(key);
}

function releaseAllStashedAttachments() {
	for (const key of [...attachmentsByConversation.keys()]) releaseStashedAttachments(key);
}

function queueSentAttachmentPreviewRevokes(urls = []) {
	for (const url of urls) {
		if (url) sentAttachmentPreviewUrls.add(url);
	}
}

function revokeSentAttachmentPreviewUrls() {
	for (const url of sentAttachmentPreviewUrls) URL.revokeObjectURL(url);
	sentAttachmentPreviewUrls.clear();
}

function maybeRevokeSentAttachmentPreviews() {
	const stillShowingLocalAttachment = messages.value.some((msg) => String(msg?.id || "").startsWith("local-"));
	if (!stillShowingLocalAttachment) revokeSentAttachmentPreviewUrls();
}

function localAttachmentPayload(items = pendingAttachments.value) {
	return items.map((item) => ({
		id: item.id,
		kind: String(item.file?.type || "").startsWith("image/") ? "image" : "file",
		fileName: item.file?.name || "attachment",
		mimeType: item.file?.type || "application/octet-stream",
		sizeBytes: item.file?.size || 0,
		previewUrl: attachmentPreviews.value[item.id] || "",
		contentUrl: attachmentPreviews.value[item.id] || "",
		inlinePreview: Boolean(attachmentPreviews.value[item.id]),
		local: true,
	}));
}

function adjustComposerHeight() {
	composer.value?.adjustHeight?.();
}

function onComposerHeightChange(height) {
	const next = Math.ceil(Number(height || 0));
	if (next > 0) composerHeight.value = next;
}

async function focusComposer() {
	await nextTick();
	await composer.value?.focus?.();
}

function closeComposerMenus() {
	modelMenuOpen.value = false;
}

async function onConsoleClick(event) {
	const target = event?.target;
	const button = target?.closest?.(".md-code-copy");
	if (!button) return;
	event.preventDefault();
	event.stopPropagation();
	const block = button.closest?.(".md-code-block");
	const code = block?.querySelector?.("pre code")?.innerText || "";
	if (!code) return;
	try {
		await copyTextToClipboard(code);
		const oldText = button.textContent || "复制";
		button.textContent = "已复制";
		button.classList.add("copied");
		window.setTimeout(() => {
			button.textContent = oldText;
			button.classList.remove("copied");
		}, 1200);
	} catch {
		ElMessage.error("复制失败");
	}
}

async function loadOptions() {
	if (modelOptions.value.length) return;
	if (optionsLoadPromise) return optionsLoadPromise;
	optionsLoading.value = true;
	optionsLoadPromise = (async () => {
		try {
			const data = await Api.rathOptions();
			modelOptions.value = Array.isArray(data.models) ? data.models : [];
			primaryModelKey.value = String(data.primaryModel || "");
			currentPrimaryModelKey.value = String(data.currentModel || data.primaryModel || "");
			if (Array.isArray(data.thinkLevels)) thinkLevels.value = data.thinkLevels.filter(Boolean);
			if (isLocalConversation.value && !localModel.value) {
				const model = applyDefaultLocalModel();
				if (model) {
					chatState.value = {
						...(chatState.value || {}),
						model: model.key,
						thinkingLevel: localThinking.value,
						effectiveThinkingLevel: localThinking.value || "off",
						thinkingLevels: modelThinkingLevels(model),
						defaultThinkingLevel: modelDefaultThinking(model),
						fastMode: false,
						effectiveFastMode: false,
						fastSupported: Boolean(model.supportsFast),
						rolloverTriggerTokens: Number(model.rolloverTriggerTokens || 0),
						windowTriggerRatio: Number(model.windowTriggerRatio || 0.7),
					};
				}
			}
		} catch (error) {
			ElMessage.error(apiError(error));
		} finally {
			optionsLoading.value = false;
			optionsLoadPromise = null;
		}
	})();
	return optionsLoadPromise;
}

async function toggleModelMenu() {
	modelMenuOpen.value = !modelMenuOpen.value;
	if (modelMenuOpen.value) await loadOptions();
}

function isRunConfigInteractionCurrent(conversationUuid, localAtRequest) {
	return Boolean(
		componentMounted
		&& String(props.conversationUuid || "") === String(conversationUuid || "")
		&& isLocalConversation.value === Boolean(localAtRequest)
	);
}

function beginModelMutation() {
	const key = modelMutationKey();
	const pending = modelMutationCounts.value;
	let finished = false;
	pending.set(key, Number(pending.get(key) || 0) + 1);
	return () => {
		if (finished) return;
		finished = true;
		const remaining = Math.max(0, Number(pending.get(key) || 0) - 1);
		if (remaining) pending.set(key, remaining);
		else pending.delete(key);
	};
}

async function selectContextStrategy(strategy) {
	if (strategySaving.value || !["sliding_window", "model_summary"].includes(strategy)) return;
	const uuid = String(activeConversationUuid.value || "");
	const local = isLocalConversation.value;
	strategySaving.value = true;
	try {
		if (local) await patchLocalRunDefaults({contextStrategy: strategy});
		else await runConfigSaves.enqueue(uuid, () => Api.conversationSetContextStrategy(uuid, strategy));
		if (isRunConfigInteractionCurrent(uuid, local)) ElMessage.success(`已切换${strategy === "model_summary" ? "模型摘要" : "滑动窗口"}${running.value ? "，下个安全边界生效" : ""}`);
	} catch (error) {
		if (isRunConfigInteractionCurrent(uuid, local)) ElMessage.error(apiError(error));
	} finally { strategySaving.value = false; }
}

async function compactContext() {
	if (!canCompact.value) return;
	const uuid = String(activeConversationUuid.value || "");
	compactingUuid.value = uuid;
	try {
		const data = await Api.conversationCompact(uuid);
		if (data?.ok === false) throw new Error(data.message || data.error || "压缩失败");
		if (!isRunConfigInteractionCurrent(uuid, false)) return;
		await load({fresh: true});
		ElMessage.success("上下文压缩完成");
	} catch (error) {
		if (isRunConfigInteractionCurrent(uuid, false)) ElMessage.error(apiError(error));
	} finally { if (compactingUuid.value === uuid) compactingUuid.value = ""; }
}

async function selectModel(model) {
	const wasRunning = running.value;
	const conversationUuid = String(activeConversationUuid.value || "");
	const localAtRequest = isLocalConversation.value;
	const finishMutation = beginModelMutation();
	const switchNow = async () => {
		let applied = true;
		if (localAtRequest) {
			await patchLocalRunDefaults({mainModel: model.key});
		} else if (conversationUuid) {
			const outcome = await runConfigSaves.enqueue(
				conversationUuid,
				() => Api.conversationSetModel(conversationUuid, model.key),
			);
			applied = outcome.applied;
		} else throw new Error("conversation_required");
		if (!applied || !isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
		modelQuery.value = "";
		ElMessage.success(wasRunning ? `已保存模型：${model.key}，下一次调用生效` : `已切换模型：${model.key}`);
	};
	try {
		await switchNow();
	} catch (error) {
		if (!isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
		const code = apiError(error);
		if (code === "cross_family_requires_new_session") {
			try {
				await ElMessageBox.confirm("这个模型和当前会话历史不是同一 family。要开启新会话后再切换吗？", "需要新会话", {
					type: "warning",
					confirmButtonText: "新会话切换",
					cancelButtonText: "取消",
				});
				if (!isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
				const created = await Api.createConversation({title: "新会话", model: model.key, folderId: props.folderId || ""});
				if (!isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
				const uuid = created.conversation?.conversationUuid || created.state?.conversationUuid || "";
				if (uuid) {
					const nextThinking = modelDefaultThinking(model);
					if (nextThinking) await Api.conversationSetThinking(uuid, nextThinking);
					if (!isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
					emit("conversation-created", uuid);
				}
				await load({scrollMode: "bottom"});
				window.dispatchEvent(new CustomEvent("openbear:conversations-refresh"));
			} catch (inner) {
				if (inner === "cancel" || inner === "close" || !isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
				ElMessage.error(apiError(inner));
			}
			return;
		}
		if (code === "run_is_active") ElMessage.warning("当前有运行中的任务，结束后再切换模型");
		else ElMessage.error(code);
	} finally {
		finishMutation();
	}
}

async function selectThinking(level) {
	const wasRunning = running.value;
	if (!supportsThinking.value || !currentThinkLevels.value.includes(level)) return;
	const conversationUuid = String(activeConversationUuid.value || "");
	const localAtRequest = isLocalConversation.value;
	try {
		let applied = true;
		if (localAtRequest) {
			await patchLocalRunDefaults({mainThinkingLevel: level});
		} else if (conversationUuid) {
			const outcome = await runConfigSaves.enqueue(
				conversationUuid,
				() => Api.conversationSetThinking(conversationUuid, level),
			);
			applied = outcome.applied;
		} else {
			throw new Error("conversation_required");
		}
		if (!applied || !isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
		if (wasRunning) ElMessage.success("思考强度已保存，下一次调用生效");
	} catch (error) {
		if (isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) ElMessage.error(apiError(error));
	}
}

async function toggleFastMode() {
	const wasRunning = running.value;
	if (!fastSupported.value) return;
	const next = !currentFast.value;
	const conversationUuid = String(activeConversationUuid.value || "");
	const localAtRequest = isLocalConversation.value;
	try {
		let applied = true;
		if (localAtRequest) {
			await patchLocalRunDefaults({mainFastMode: next});
		} else if (conversationUuid) {
			const outcome = await runConfigSaves.enqueue(
				conversationUuid,
				() => Api.conversationSetFast(conversationUuid, next),
			);
			applied = outcome.applied;
		} else throw new Error("conversation_required");
		if (!applied || !isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
		ElMessage.success(wasRunning ? "Fast 模式已保存，下一次调用生效" : (next ? "Fast 模式已开启" : "Fast 模式已关闭"));
	} catch (error) {
		if (isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) ElMessage.error(apiError(error));
	}
}

function buildLocalAgentRunConfig(overrides = {}) {
	const model = overrides.model !== undefined ? overrides.model : localAgentModel.value;
	const thinkLevel = overrides.thinkLevel !== undefined ? overrides.thinkLevel : localAgentThinking.value;
	const fastMode = overrides.fastMode !== undefined ? overrides.fastMode : localAgentFast.value;
	const effectiveModel = model || currentModel.value || "";
	const info = modelOptions.value.find((m) => m.key === effectiveModel) || null;
	const levels = Array.isArray(info?.thinkingLevels) ? info.thinkingLevels.filter(Boolean) : [];
	const defaultThinking = modelDefaultThinking(info) || "";
	const resolvedThink = thinkLevel && levels.includes(thinkLevel) ? thinkLevel : (defaultThinking || "off");
	const supportsFast = Boolean(info?.supportsFast);
	const resolvedFast = fastMode === true ? supportsFast : (fastMode === false ? false : Boolean(currentFast.value && supportsFast));
	return {
		model: model || "",
		thinkLevel: thinkLevel || "",
		fastMode: fastMode === true || fastMode === false ? fastMode : null,
		effective: {
			model: effectiveModel,
			thinkLevel: resolvedThink,
			fastMode: resolvedFast,
			fastSupported: supportsFast,
			thinkingLevels: levels,
			defaultThinkingLevel: defaultThinking,
			supportsThinking: levels.length > 0,
			source: {
				model: model ? "conversation" : "main",
				thinkLevel: thinkLevel ? "conversation" : "model_default",
				fastMode: fastMode === true || fastMode === false ? "conversation" : "main",
			},
		},
	};
}

async function saveAgentRunConfig(patch, successText) {
	const wasRunning = running.value;
	const conversationUuid = String(activeConversationUuid.value || "");
	const localAtRequest = isLocalConversation.value;
	try {
		let applied = true;
		if (localAtRequest) {
			const defaultsPatch = {};
			if (patch.model !== undefined) defaultsPatch.agentModel = patch.model || "";
			if (patch.thinkLevel !== undefined) defaultsPatch.agentThinkLevel = patch.thinkLevel || "";
			if (patch.fastMode !== undefined) defaultsPatch.agentFastMode = patch.fastMode;
			await patchLocalRunDefaults(defaultsPatch);
		} else if (conversationUuid) {
			const outcome = await runConfigSaves.enqueue(
				conversationUuid,
				() => Api.conversationSetAgentRunConfig(conversationUuid, patch),
			);
			applied = outcome.applied;
		} else {
			throw new Error("conversation_required");
		}
		if (!applied || !isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) return;
		ElMessage.success(wasRunning ? `${successText}，下一次新 Agent 生效` : successText);
	} catch (error) {
		if (isRunConfigInteractionCurrent(conversationUuid, localAtRequest)) ElMessage.error(apiError(error));
	}
}

async function selectAgentModel(modelKey) {
	await saveAgentRunConfig({model: modelKey || ""}, modelKey ? `Agent 模型已设为 ${modelKey}` : "Agent 模型已跟随主模型");
}

async function selectAgentThinking(level) {
	await saveAgentRunConfig({thinkLevel: level || ""}, level ? `Agent 思考已设为 ${level}` : "Agent 思考已跟随模型默认");
}

async function selectAgentFast(mode) {
	if (mode === true && !agentFastSupported.value) return;
	const text = mode === true ? "Agent Fast 已开启" : (mode === false ? "Agent Fast 已关闭" : "Agent Fast 已跟随主会话");
	await saveAgentRunConfig({fastMode: mode}, text);
}

function applyQuickPrompt(text) {
	draft.value = draft.value ? `${draft.value}\n${text}` : text;
	adjustComposerHeight();
	void focusComposer();
}

function activeToolResultIndex(event) {
	const key = toolResultKey(event);
	const total = toolBatchSize(event);
	const idx = Number(toolResultTabs.value[key] || 0);
	return Math.min(Math.max(0, idx), Math.max(0, total - 1));
}

function activeToolResult(event) {
	return toolResultForIndex(event, activeToolResultIndex(event));
}

function selectToolResult(event, idx) {
	const key = toolResultKey(event);
	toolResultTabs.value = {...toolResultTabs.value, [key]: idx};
}

function msgTime(msg) {
	return Number(msg?.createdAt || msg?.created_at || 0);
}

function eventTime(event) {
	return msgTime(event.message) || msgTime(event.result) || 0;
}

function buildTurns(list) {
	const out = [];
	let current = null;
	
	function ensureTurn() {
		if (!current) current = {id: `orphan-${out.length}`, user: null, events: [], startAt: 0, endAt: 0};
		return current;
	}
	
	function flush() {
		if (current) {
			if (!current.startAt) current.startAt = current.events.map(eventTime).find(Boolean) || 0;
			out.push(current);
		}
		current = null;
	}
	
	for (const msg of list) {
		if (msg.role === "user") {
			flush();
			current = {
				id: msg.id || `turn-${out.length}`,
				turnUuid: String(msg.turnUuid || msg.turn_uuid || "").trim(),
				user: msg,
				events: [],
				startAt: msgTime(msg),
				endAt: 0,
				queuedSteering: Boolean(msg.queuedSteering)
			};
			continue;
		}
		const turn = ensureTurn();
		appendAssistantEvent(turn, msg);
	}
	flush();
	return out.map((turn, idx) => ({
		...turn,
		endAt: out[idx + 1]?.startAt || 0,
		isLatest: idx === out.length - 1,
	}));
}

function mergeTurnLocalStats(turn, stats) {
	if (!stats) return;
	turn.localStats = turn.localStats ? combineControllerAndAgentStats(turn.localStats, stats) : stats;
}

function appendAssistantEvent(turn, msg) {
	if (msg.continuedBySteering) turn.continuedBySteering = true;
	if (msg.queuedSteering) turn.queuedSteering = true;
	if (Array.isArray(msg.localTimeline) && msg.localTimeline.length) {
		for (const event of msg.localTimeline) turn.events.push(event);
		mergeTurnLocalStats(turn, msg.localStats);
		return;
	}
	mergeTurnLocalStats(turn, msg.localStats);
	const calls = Array.isArray(msg.toolCalls) ? msg.toolCalls : [];
	const content = answerContent(msg.content || "");
	const cleanMsg = content === String(msg.content || "") ? msg : {...msg, content};
	const text = String(content || "").trim();
	if (msg.role === "assistant" && calls.length) {
		// Assistant messages may legitimately contain visible text before the
		// model starts tool calls.  Keep that pre-tool text in the timeline so the
		// UI does not appear to "swallow" content when the following tool/reasoning
		// block arrives.  The tool_calls themselves are rendered as separate cards.
		if (text || msg.reasoning) turn.events.push({
			kind: "answer",
			message: {...cleanMsg, content: text, toolCalls: []}
		});
		for (const call of calls) {
			turn.events.push({
				kind: "tool",
				calls: [call],
				toolName: callName(call),
				message: msg,
				result: null,
				results: []
			});
		}
		return;
	}
	if (msg.role === "assistant" && /^调用工具[:：]/.test(plainText(text))) {
		const name = plainText(text).replace(/^调用工具[:：]\s*/, "").split(/[\s,，、]/)[0] || "Tool";
		turn.events.push({
			kind: "tool",
			calls: [{name, arguments: ""}],
			toolName: name,
			message: msg,
			result: null,
			results: []
		});
		return;
	}
	if (msg.role === "tool") {
		const name = msg.name || "Tool";
		const toolCallId = msg.toolCallId || msg.tool_call_id || "";
		const pending = [...turn.events].reverse().find((event) => {
			if (event.kind !== "tool") return false;
			const results = toolResultItems(event);
			if (toolCallId) return event.calls.some((call) => toolCallIdOf(call) === String(toolCallId)) && !results.some((item) => toolCallIdOf(item) === String(toolCallId));
			return (!event.toolName || event.toolName === name || event.calls.some((call) => callName(call) === name)) && results.length < Math.max(1, event.calls.length);
		});
		if (pending) {
			pending.results = [...(pending.results || []), msg];
			pending.result = pending.results[0] || msg;
		} else {
			turn.events.push({
				kind: "tool",
				calls: [{id: toolCallId, name, arguments: ""}],
				toolName: name,
				message: null,
				result: msg,
				results: [msg]
			});
		}
		return;
	}
	if (text || msg.reasoning || msg.live) turn.events.push({kind: "answer", message: cleanMsg});
}

function usageSum(a = {}, b = {}) {
	return {
		inputTokens: Number(a.inputTokens || 0) + Number(b.inputTokens || 0),
		outputTokens: Number(a.outputTokens || 0) + Number(b.outputTokens || 0),
		cacheReadTokens: Number(a.cacheReadTokens || 0) + Number(b.cacheReadTokens || 0),
		cacheWriteTokens: Number(a.cacheWriteTokens || 0) + Number(b.cacheWriteTokens || 0),
		totalTokens: Number(a.totalTokens || 0) + Number(b.totalTokens || 0),
	};
}

function weightedAvg(aValue, aWeight, bValue, bWeight) {
	const total = Number(aWeight || 0) + Number(bWeight || 0);
	if (!total) return Number(bValue || aValue || 0);
	return (Number(aValue || 0) * Number(aWeight || 0) + Number(bValue || 0) * Number(bWeight || 0)) / total;
}

function taskWallDurationMs(task = {}) {
	const direct = Number(task?.durationMs || task?.duration_ms || 0);
	if (direct > 0) return direct;
	const started = Number(task?.startedAtMs || task?.started_at_ms || 0);
	const finished = Number(task?.finishedAtMs || task?.finished_at_ms || 0);
	return started > 0 && finished > started ? finished - started : 0;
}

function maxAgentDurationForTurn(turn) {
	let maxMs = 0;
	for (const event of turn?.events || []) {
		for (const task of agentTasks(event)) {
			maxMs = Math.max(maxMs, taskWallDurationMs(task));
		}
	}
	return maxMs;
}

function reconcileStatsWithAgentCards(turn, stats) {
	if (!stats) return stats;
	const tasks = [];
	for (const event of turn?.events || []) tasks.push(...agentTasks(event));
	const reconciled = reconcileAgentTaskUsage(stats, tasks);
	const agentDurationMs = maxAgentDurationForTurn(turn);
	if (!agentDurationMs || agentDurationMs <= Number(reconciled.durationMs || 0)) return reconciled;
	return {
		...reconciled,
		durationMs: agentDurationMs,
		avgTps: agentDurationMs > 0
			? (Number(reconciled?.usage?.outputTokens || 0) + Number(reconciled?.expertUsage?.outputTokens || 0)) * 1000 / agentDurationMs
			: Number(reconciled.avgTps || 0),
		agentDurationReconciled: true,
	};
}

function combineControllerAndAgentStats(controller, current) {
	if (!controller || !current) return current || controller || null;
	const leftOk = Number(controller.modelOk || controller.modelCalls || 0);
	const rightOk = Number(current.modelOk || current.modelCalls || 0);
	const totalDuration = Number(controller.durationMs || 0) + Number(current.durationMs || 0);
	const usage = usageSum(controller.usage, current.usage);
	const expertUsage = usageSum(controller.expertUsage, current.expertUsage);
	const outputForTps = Number(usage.outputTokens || 0) + Number(expertUsage.outputTokens || 0);
	const positiveMin = [controller.minTps, current.minTps].map((n) => Number(n || 0)).filter((n) => n > 0);
	return {
		...controller,
		...current,
		durationMs: totalDuration,
		reasoningMs: Number(controller.reasoningMs || 0) + Number(current.reasoningMs || 0),
		modelCalls: Number(controller.modelCalls || 0) + Number(current.modelCalls || 0),
		modelOk: Number(controller.modelOk || 0) + Number(current.modelOk || 0),
		modelRetry: Number(controller.modelRetry || 0) + Number(current.modelRetry || 0),
		modelFail: Number(controller.modelFail || 0) + Number(current.modelFail || 0),
		toolCalls: Number(controller.toolCalls || 0) + Number(current.toolCalls || 0),
		expertModelCalls: Number(controller.expertModelCalls || 0) + Number(current.expertModelCalls || 0),
		expertToolCalls: Number(controller.expertToolCalls || 0) + Number(current.expertToolCalls || 0),
		expertTasks: Number(controller.expertTasks || 0) + Number(current.expertTasks || 0),
		contextTokens: Number(current.contextTokens || 0) || Number(controller.contextTokens || 0),
		contextWindow: Number(current.contextWindow || 0) || Number(controller.contextWindow || 0),
		usage,
		expertUsage,
		costUsd: Number(controller.costUsd || 0) + Number(current.costUsd || 0),
		avgConnectMs: weightedAvg(controller.avgConnectMs, leftOk, current.avgConnectMs, rightOk),
		avgFirstTokenMs: weightedAvg(controller.avgFirstTokenMs, leftOk, current.avgFirstTokenMs, rightOk),
		avgTotalMs: weightedAvg(controller.avgTotalMs, leftOk, current.avgTotalMs, rightOk),
		avgTps: totalDuration > 0 ? outputForTps * 1000 / totalDuration : Number(current.avgTps || controller.avgTps || 0),
		peakTps: Math.max(Number(controller.peakTps || 0), Number(current.peakTps || 0)),
		minTps: positiveMin.length ? Math.min(...positiveMin) : 0,
		combinedControllerStats: true,
	};
}

function shouldCombineControllerStats(turn, prevTurn) {
	if (!turn?.stats || !prevTurn?.stats) return false;
	if (plainText(turn.user?.content || "")) return false;
	return Number(turn.stats.expertTasks || turn.stats.expertToolCalls || 0) > 0;
}

function attachTurnStats(turnList, modelRows, toolRows) {
	const modelBuckets = bucketRowsByTurn(turnList, modelRows);
	const toolBuckets = bucketRowsByTurn(turnList, toolRows);
	const withStats = turnList.map((turn, idx) => {
		const stats = turn.localStats || statsForTurn(turn, modelBuckets[idx] || [], toolBuckets[idx] || []);
		return {
			...turn,
			// No assistant content was produced: retain the user turn and stop state,
			// without projecting a duration-only assistant reply.
			stats: turn.events.length ? reconcileStatsWithAgentCards(turn, stats) : null,
		};
	});
	return withStats.map((turn, idx) => shouldCombineControllerStats(turn, withStats[idx - 1])
		? {...turn, stats: combineControllerAndAgentStats(withStats[idx - 1].stats, turn.stats)}
		: turn);
}

function inTurn(row, turn) {
	const ts = Number(row?.created_at || row?.createdAt || 0);
	if (!ts || !turn.startAt) return false;
	if (ts < turn.startAt) return false;
	return !turn.endAt || ts < turn.endAt;
}

function bucketRowsByTurn(turnList = [], rows = []) {
	const buckets = turnList.map(() => []);
	if (!turnList.length || !Array.isArray(rows) || !rows.length) return buckets;
	let idx = 0;
	for (const row of rows) {
		const ts = Number(row?.created_at || row?.createdAt || 0);
		if (!ts) continue;
		while (idx < turnList.length - 1 && turnList[idx]?.endAt && ts >= turnList[idx].endAt) idx += 1;
		for (let probe = idx; probe < turnList.length; probe += 1) {
			const turn = turnList[probe];
			if (inTurn(row, turn)) {
				buckets[probe].push(row);
				idx = probe;
				break;
			}
			if (turn?.startAt && ts < turn.startAt) break;
		}
	}
	return buckets;
}

function rowMetric(row, key, fallback = 0) {
	const value = row?.[key];
	if (value === undefined || value === null || value === "") return fallback;
	return Number(value || 0);
}

function statsForTurn(turn, modelRows, toolRows) {
	const models = modelRows.filter((row) => inTurn(row, turn));
	const tools = toolRows.filter((row) => inTurn(row, turn));
	if (!models.length && !tools.length) return null;
	const last = models.at(-1) || {};
	const usageRows = models.reduce((acc, row) => {
		acc.inputTokens += Number(row.input_tokens || 0);
		acc.outputTokens += Number(row.output_tokens || 0);
		acc.cacheReadTokens += Number(row.cache_read_tokens || 0);
		acc.cacheWriteTokens += Number(row.cache_write_tokens || 0);
		acc.totalTokens += Number(row.input_tokens || 0) + Number(row.output_tokens || 0) + Number(row.cache_read_tokens || 0) + Number(row.cache_write_tokens || 0);
		return acc;
	}, {inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, totalTokens: 0});
	const expertUsageRows = models.reduce((acc, row) => {
		acc.inputTokens += Number(row.expert_input_tokens || 0);
		acc.outputTokens += Number(row.expert_output_tokens || 0);
		acc.cacheReadTokens += Number(row.expert_cache_read_tokens || 0);
		acc.cacheWriteTokens += Number(row.expert_cache_write_tokens || 0);
		return acc;
	}, {inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0});
	const modelCalls = models.reduce((sum, row) => sum + rowMetric(row, "model_call_count", 1), 0);
	const modelOk = models.reduce((sum, row) => sum + rowMetric(row, "model_ok_count", row.status === "ok" ? 1 : 0), 0);
	const totalMs = models.reduce((sum, row) => sum + Number(row.total_time_ms || 0), 0);
	const positiveMinTps = models.map((row) => Number(row.min_tps || 0)).filter((n) => n > 0);
	const expertToolCalls = models.reduce((sum, row) => sum + Number(row.expert_tool_calls || 0), 0);
	const okDen = modelOk || modelCalls;
	const lastContext = Number(last.last_input_tokens || 0) + Number(last.last_cache_read_tokens || 0) + Number(last.last_cache_write_tokens || 0)
		|| (Number(last.model_call_count || 0) <= 1 ? Number(last.input_tokens || 0) + Number(last.cache_read_tokens || 0) + Number(last.cache_write_tokens || 0) : 0);
	const model = last.model || chatState.value?.model || "";
	const meta = modelOptions.value.find((m) => m.key === model) || null;
	return {
		model,
		protocol: last.protocol || meta?.protocol || "",
		thinkLevel: last.think_level || "",
		durationMs: totalMs,
		modelCalls,
		modelOk,
		modelRetry: models.reduce((sum, row) => sum + rowMetric(row, "model_retry_count", 0), 0),
		modelFail: models.reduce((sum, row) => sum + rowMetric(row, "model_fail_count", row.status !== "ok" ? 1 : 0), 0),
		toolCalls: tools.length + expertToolCalls,
		contextTokens: lastContext,
		contextWindow: Number(meta?.contextWindow || 0),
		avgConnectMs: okDen ? models.reduce((sum, row) => sum + Number(row.connect_ms || 0), 0) / okDen : 0,
		avgFirstTokenMs: okDen ? models.reduce((sum, row) => sum + Number(row.first_token_ms || 0), 0) / okDen : 0,
		avgTotalMs: okDen ? totalMs / okDen : 0,
		avgTps: totalMs > 0 ? usageRows.outputTokens * 1000 / totalMs : 0,
		peakTps: Math.max(0, ...models.map((row) => Number(row.peak_tps || 0))),
		minTps: positiveMinTps.length ? Math.min(...positiveMinTps) : 0,
		usage: usageRows,
		expertUsage: expertUsageRows,
		costUsd: models.reduce((sum, row) => sum + Number(row.cost_usd || 0), 0),
		haltedReason: last.error_type || (last.status === "cancelled" ? "cancelled" : ""),
		historical: true,
	};
}

function operationProjectionOptions() {
	return {
		answerContent,
		agentSummary,
	};
}

function orderedOperationsList() {
	return orderedOpIds.value.map((id) => operationsById.value.get(id)).filter(Boolean);
}

// Rendering reads one event at a time. Letting Vue deep-proxy every projected
// event turns each read into a dependency-tracked reactive traversal, which is
// what makes a long conversation freeze for a second per repaint. The timeline
// contract is already immutable — every projection and every operation frame
// produces new arrays/objects — so the event payload can be marked raw while the
// message list itself stays reactive for optimistic `push` and whole-list
// replacement.
function withRawTimeline(list) {
	return list.map((message) => (
		Array.isArray(message?.localTimeline)
			? {...message, localTimeline: markRaw(message.localTimeline.map((event) => markRaw(event)))}
			: message
	));
}

function projectOperationMessages(operations = orderedOperationsList()) {
	return withRawTimeline(projectOperationMessagesFromOperations(operations, operationProjectionOptions()));
}

function clearActiveRun() {
	activeRunTurnUuid.value = "";
}

function replaceOperationSnapshots(operations = [], {frameSeq = null} = {}) {
	const ops = normalizeOperations(operations);
	const byId = new Map();
	const revisions = new Map();
	stateStatsByOpId.clear();
	for (const op of ops) {
		byId.set(op.opId, op);
		revisions.set(op.opId, Number(op.revision || 0) || 0);
		if (op?.opType === "stats" && op.payload) stateStatsByOpId.set(op.opId, statsUsageSnapshot(op.payload));
	}
	operationsById.value = byId;
	orderedOpIds.value = ops.map((op) => op.opId);
	revisionByOpId.value = revisions;
	if (frameSeq !== null) lastFrameSeq.value = Math.max(lastFrameSeq.value, Number(frameSeq || 0) || 0);
	return ops;
}

function loadOperationsFromState(state = {}, {merge = false} = {}) {
	const incoming = normalizeOperations(Array.isArray(state.operations) ? state.operations : []);
	const ops = merge
		? mergeOperationSnapshots(orderedOperationsList(), incoming)
		: incoming;
	// HTTP owns this baseline. Frames crossing the snapshot boundary are replayed
	// from the buffer, never acknowledged via an unrelated newer high-water mark.
	lastFrameSeq.value = Number(state.frameSeq || state.facts?.latestFrameSeq || 0) || 0;
	return replaceOperationSnapshots(ops, {frameSeq: lastFrameSeq.value});
}

function applyTimelinePageMetadata(data = {}, conversationUuid = "", {preserve = false} = {}) {
	const uuid = String(conversationUuid || "");
	if (timelinePageConversationUuid !== uuid) resetTimelinePagination(uuid);
	if (preserve && timelinePageInitialized) return;
	hasMoreBefore.value = Boolean(data.hasMoreBefore);
	const cursor = Number(data.nextBeforeDisplaySeq || 0) || 0;
	nextBeforeDisplaySeq.value = hasMoreBefore.value && cursor > 0 ? cursor : null;
	timelinePageInitialized = true;
}

function frameDebugEnabled() {
	if (typeof window === "undefined") return false;
	try {
		const params = new URLSearchParams(window.location.search || "");
		if (params.get("debugFrames") === "1") return true;
		return window.localStorage.getItem(FRAME_DEBUG_STORAGE_KEY) === "1";
	} catch {
		return false;
	}
}

function operationDebugRow(op) {
	if (!op) return null;
	return {
		opId: op.opId,
		opType: op.opType,
		status: op.status || op.payload?.status || "",
		lifecycle: op.lifecycle || "",
		revision: Number(op.revision || 0) || 0,
		displaySeq: Number(op.displaySeq || 0) || 0,
		turnId: op.turnId || op.turnUuid || "",
		taskUuid: op.taskUuid || op.payload?.taskUuid || op.payload?.task?.taskUuid || "",
	};
}

function activeOperationDebugRows(operations = orderedOperationsList()) {
	return operations
		.filter((op) => {
			const lifecycle = String(op?.lifecycle || "");
			if (["terminal", "informational", "paused", "waiting_control"].includes(lifecycle)) return false;
			return ["active"].includes(lifecycle) || ["queued", "running", "pausing", "resuming", "stopping"].includes(String(op?.status || op?.payload?.status || ""));
		})
		.map(operationDebugRow)
		.filter(Boolean);
}

function debugFrames(label, detail = {}) {
	if (!frameDebugEnabled()) return;
	try {
		console.debug(`[OpenBear frames] ${label}`, {
			conversationUuid: activeConversationUuid.value,
			lastFrameSeq: lastFrameSeq.value,
			running: running.value,
			foregroundRunning: foregroundRunning.value,
			status: status.value,
			active: activeOperationDebugRows(),
			...detail,
		});
	} catch {
	}
}

function syncRunStateFromOperations(operations = orderedOperationsList(), stateFacts = null) {
	const derived = deriveOperationRunState(operations);
	const stateBackgroundRunning = Boolean(stateFacts?.backgroundRunning);
	const stateForegroundRunning = Boolean(stateFacts?.running && !stateBackgroundRunning);
	const stateBackgroundStatus = String(stateFacts?.backgroundStatus || "").trim();
	const stateConversationStatus = String((stateFacts?.live?.running ? stateFacts?.live?.currentStatus : "") || stateFacts?.conversation?.currentStatus || stateFacts?.live?.currentStatus || "").trim();
	const hasForeground = Boolean(derived.foregroundRunning || stateForegroundRunning);
	running.value = Boolean(derived.running || stateBackgroundRunning || stateForegroundRunning);
	foregroundRunning.value = hasForeground;
	rootTurnRunning.value = Boolean(derived.rootTurnRunning);
	activeRunTurnUuid.value = derived.activeRootTurnId || "";
	runStartedAt.value = Number(derived.activeStartedAtMs || 0)
		|| (stateForegroundRunning ? Number(stateFacts?.live?.startedAtMs || 0) : 0)
		|| (stateBackgroundRunning ? Number(stateFacts?.backgroundStartedAtMs || 0) : 0)
		|| (running.value ? runStartedAt.value || Date.now() : 0);
	status.value = hasForeground
		? (derived.statusLabel || stateConversationStatus || "运行中")
		: stateBackgroundRunning
			? (stateBackgroundStatus || "Agent 后台执行中")
			: (derived.statusLabel || (running.value ? (stateConversationStatus || "运行中") : "就绪"));
	consumePendingAgentAutoOpen(operations, derived);
	debugFrames("sync-run-state", {
		derived,
		stateFacts: stateFacts ? {
			running: Boolean(stateFacts.running),
			backgroundRunning: Boolean(stateFacts.backgroundRunning),
			backgroundStatus: stateFacts.backgroundStatus || "",
			backgroundStartedAtMs: Number(stateFacts.backgroundStartedAtMs || 0) || 0,
		} : null,
	});
	return {
		...derived,
		running: running.value,
		foregroundRunning: foregroundRunning.value,
		backgroundRunning: Boolean(derived.backgroundRunning || stateBackgroundRunning),
		statusLabel: status.value
	};
}

async function resyncOperationFrames(reason = {}) {
	if (!componentMounted || !props.conversationUuid || isLocalConversation.value) return;
	if (reason?.resetOperations) pendingLoadReplaceOperations = true;
	if (reason?.frameSeq && (reason?.requiresFullState || reason?.resyncMode === "full_state")) {
		operationFrameBuffer.requireSnapshotThrough(reason.frameSeq);
	}
	if (operationResyncInFlight) return;
	const recovery = {conversationUuid: String(props.conversationUuid), socket: ws};
	operationResyncInFlight = recovery;
	const isCurrent = () => componentMounted
		&& operationResyncInFlight === recovery
		&& recovery.conversationUuid === String(props.conversationUuid)
		&& recovery.socket === ws;
	let snapshotApplied = false;
	const refreshState = async () => {
		const result = await load({
			conversationUuid: recovery.conversationUuid,
			scrollMode: "preserve",
			manageLoading: false,
			replaceOperations: Boolean(reason?.resetOperations),
			fresh: true,
			isCurrent,
		});
		snapshotApplied ||= Boolean(result?.applied);
	};
	const finishReplay = async () => {
		if (!replayBufferedOperationFrames(lastFrameSeq.value)) await refreshState();
	};
	try {
		if (reason?.requiresFullState || reason?.resyncMode === "full_state") {
			debugFrames("frame-resync-full-state", {reason});
			await refreshState();
			return;
		}
		let after = Number(reason?.afterFrameSeq ?? lastFrameSeq.value) || 0;
		for (let i = 0; i < 8; i += 1) {
			const data = await Api.conversationFrames(recovery.conversationUuid, after, 1000);
			if (!isCurrent()) return;
			const frames = Array.isArray(data?.frames) ? data.frames : [];
			if (!frames.length) { await finishReplay(); return; }
			for (const frame of frames) {
				// Replay older revisions to repair a gap, but stop at the first frame
				// whose base was pruned. One snapshot replaces the unusable replay;
				// a page of 1000 missing bases must never launch 1000 HTTP requests.
				const result = applyOperationFrameMessage(frame, {resyncing: true});
				if (result?.needsResync) {
					await refreshState();
					return;
				}
			}
			const nextAfter = Number(data?.frameSeq || frames.at(-1)?.frameSeq || after) || after;
			if (nextAfter <= after || frames.length < 1000) { await finishReplay(); return; }
			after = nextAfter;
		}
		// A bounded replay must converge instead of leaving a long backlog half applied.
		if (isCurrent()) await refreshState();
	} catch (error) {
		if (!isCurrent()) return;
		console.warn("operation frame resync failed", reason, error);
		await refreshState();
	} finally {
		if (operationResyncInFlight === recovery) {
			operationResyncInFlight = null;
			if (snapshotApplied && operationFrameBuffer.blocked) scheduleOperationStateResync({requiresFullState: true});
		}
	}
}

function scheduleOperationStateResync(reason = {}) {
	if (!props.conversationUuid || isLocalConversation.value) return;
	if (operationResyncTimer || operationResyncInFlight) return;
	operationResyncTimer = window.setTimeout(async () => {
		operationResyncTimer = null;
		await resyncOperationFrames(reason);
	}, 0);
}

const terminalStateRefreshScheduler = createTerminalStateRefreshScheduler({
	delayMs: 650,
	getConversationUuid: () => activeConversationUuid.value,
	getSocket: () => ws,
	getSocketConversationUuid: () => wsConversationUuid,
	isComponentActive: () => componentMounted,
	isSocketActive: (socket) => socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING,
	scheduleTimeout: (callback, delay) => window.setTimeout(callback, delay),
	clearScheduledTimeout: (handle) => window.clearTimeout(handle),
	refresh: async ({conversationUuid, reason, isCurrent}) => {
		if (!isCurrent()) return;
		debugFrames("terminal-state-refresh", {reason, conversationUuid});
		await load({
			scrollMode: "preserve",
			conversationUuid,
			fresh: true,
			isCurrent,
			manageLoading: false,
		});
	},
	onError: (error, reason) => console.warn("terminal state refresh failed", reason, error),
});

function scheduleTerminalStateRefresh(reason = {}) {
	if (!props.conversationUuid || isLocalConversation.value) return false;
	return terminalStateRefreshScheduler.schedule(reason);
}

const AGENT_TOOL_NAMES = new Set(["Agent", "AgentMessage", "AgentStop"]);

function isAgentToolName(name) {
	return AGENT_TOOL_NAMES.has(String(name || ""));
}

function isAgentOperationLike(opType, payload = {}) {
	if (String(opType || "") === "agent" || String(payload?.opType || "") === "agent") return true;
	return [payload.toolName, payload.rootToolName, payload.name, payload.rootName]
		.some((name) => isAgentToolName(name));
}

function operationScrollImpact(frame = {}, op = null, {visibleChanged = false} = {}) {
	if (!visibleChanged) return "none";
	const opType = String(frame?.opType || op?.opType || "");
	const payload = (op?.payload && typeof op.payload === "object")
		? op.payload
		: (frame?.payload && typeof frame.payload === "object" ? frame.payload : {});
	if (["stats", "status", "run", "run_control"].includes(opType)) return "metadata";
	if (opType === "agent" || opType === "agent_control" || isAgentOperationLike(opType, payload)) return "panel";
	if (opType === "notice") {
		const taskUuid = String(payload.taskUuid || payload.task_uuid || "").trim();
		if (taskUuid || payload.agentNotice || payload.source === "task_notification" || payload.internal) return "panel";
		return "metadata";
	}
	if (opType === "user_message") {
		if (payload.internal || payload.hidden || payload.queued) return "metadata";
		return "tail";
	}
	if (["assistant_message", "reasoning", "tool"].includes(opType)) return "tail";
	return "tail";
}

function shouldScrollForImpact(impact) {
	return impact === "tail";
}

function mergeScrollImpact(left = "none", right = "none") {
	const rank = {none: 0, metadata: 1, panel: 2, tail: 3};
	return (rank[right] || 0) > (rank[left] || 0) ? right : left;
}

function flushProjectedMessages() {
	streamFlushPending = false;
	streamFlushFrame = 0;
	const ops = pendingProjectionOps || orderedOperationsList();
	pendingProjectionOps = null;
	operationsById.value = new Map(operationsById.value);
	orderedOpIds.value = [...orderedOpIds.value];
	revisionByOpId.value = new Map(revisionByOpId.value);
	const beforeSignature = visibleEventSignatureForMessages(messages.value);
	const projected = projectOperationMessages(ops);
	const shouldReplaceMessages = projected.length || !hasOptimisticLocalTurn();
	const nextMessages = shouldReplaceMessages ? projected : messages.value;
	const afterSignature = visibleEventSignatureForMessages(nextMessages);
	const visibleChanged = beforeSignature !== afterSignature;
	const scrollImpact = visibleChanged ? pendingScrollImpact : "none";
	pendingScrollImpact = "none";
	const preserveAnchor = pendingPreserveAnchor || (!autoScrollLocked.value && !shouldScrollForImpact(scrollImpact) ? captureScrollAnchor() : null);
	pendingPreserveAnchor = null;
	if (shouldReplaceMessages) messages.value = projected;
	syncRunStateFromOperations(ops);
	if (pendingTerminalFrame) {
		scheduleTerminalStateRefresh(pendingTerminalFrame);
		pendingTerminalFrame = null;
	}
	noteVisibleOutput(messages.value);
	if (shouldScrollForImpact(scrollImpact)) scheduleScrollBottom({cause: scrollImpact});
	else if (preserveAnchor) void restoreScrollAnchor(preserveAnchor);
}

function scheduleProjectedMessagesFlush(options = {}) {
	streamFlushPending = true;
	if (options?.force) {
		if (streamFlushTimer) window.clearTimeout(streamFlushTimer);
		if (streamFlushFrame) window.cancelAnimationFrame(streamFlushFrame);
		streamFlushTimer = 0;
		streamFlushFrame = 0;
		flushProjectedMessages();
		return;
	}
	if (streamFlushTimer || streamFlushFrame) return;
	streamFlushTimer = window.setTimeout(() => {
		streamFlushTimer = 0;
		streamFlushFrame = window.requestAnimationFrame(flushProjectedMessages);
	}, STREAM_UI_FRAME_MS);
}

function applyOperationFrameMessage(frame, options = {}) {
	debugFrames("frame-before-apply", {
		frame: {
			frameSeq: Number(frame?.frameSeq || 0) || 0,
			opId: frame?.opId || "",
			opType: frame?.opType || "",
			action: frame?.action || "",
			revision: Number(frame?.revision || 0) || 0,
			displaySeq: Number(frame?.displaySeq || 0) || 0,
			status: frame?.payload?.status || "",
			taskUuid: frame?.taskUuid || frame?.payload?.taskUuid || frame?.payload?.task?.taskUuid || "",
		},
		options,
	});
	const store = {
		operationsById: operationsById.value,
		orderedOpIds: orderedOpIds.value,
		revisionByOpId: revisionByOpId.value,
		lastFrameSeq: lastFrameSeq.value,
		needsResync: false,
		revisionGap: null,
	};
	const changed = applyOperationFrame(store, frame);
	if (store.needsResync) {
		debugFrames("frame-revision-gap", {revisionGap: store.revisionGap, frame});
		const previousFrameSeq = lastFrameSeq.value;
		operationFrameBuffer.block(frame);
		if (!options?.resyncing) {
			scheduleOperationStateResync({...(store.revisionGap || frame), afterFrameSeq: previousFrameSeq});
		}
		return {applied: false, scrollImpact: "none", needsResync: true};
	}
	operationsById.value = store.operationsById;
	orderedOpIds.value = store.orderedOpIds;
	revisionByOpId.value = store.revisionByOpId;
	lastFrameSeq.value = Number(store.lastFrameSeq || 0) || lastFrameSeq.value;
	if (changed && frame.opType === "stats" && frame.payload) {
		mergeStatsUsageIntoState(frame.opId, operationsById.value.get(frame.opId)?.payload || frame.payload);
	}
	else if (changed) {
		const appliedOperation = operationsById.value.get(frame.opId);
		if (
			["context_window", "context_compaction"].includes(appliedOperation?.opType)
			&& appliedOperation?.lifecycle === "terminal"
			&& appliedOperation?.status === "completed"
			&& appliedOperation?.payload?.windowVersion
			&& chatState.value
		) {
			chatState.value = {
				...chatState.value,
				contextUsage: invalidateContextUsage(
					chatState.value.contextUsage,
					{...appliedOperation.payload, rolloverTriggerTokens: Number(chatState.value.rolloverTriggerTokens || 0)},
				),
			};
		}
		mergeLedgerUsageIntoState(appliedOperation?.payload?.ledgerUsage || frame.payload?.ledgerUsage);
	}
	if (!changed && orderedOpIds.value.length) {
		debugFrames("frame-no-change", {frame});
		return {applied: false, scrollImpact: "none"};
	}
	const appliedOperation = operationsById.value.get(frame.opId);
	const scrollImpact = operationScrollImpact(frame, appliedOperation, {visibleChanged: changed});
	pendingProjectionOps = orderedOperationsList();
	pendingScrollImpact = mergeScrollImpact(pendingScrollImpact, scrollImpact);
	if (!options?.resyncing && isRootRunTerminalFrame(frame)) {
		pendingTerminalFrame = {frame: operationDebugRow(appliedOperation), frameSeq: Number(frame.frameSeq || 0) || 0};
	}
	// Every terminal operation remains an immediate projection boundary, but only
	// the root controller run is an authoritative full-state calibration boundary.
	scheduleProjectedMessagesFlush({force: isTerminalOperationFrame(frame)});
	debugFrames("frame-after-apply", {changed, scrollImpact, applied: operationDebugRow(appliedOperation)});
	return {applied: true, scrollImpact, visibleChanged: changed};
}

function textSignal(text) {
	const source = String(text || "");
	if (!source) return "";
	return `${source.length}:${source.slice(0, 16)}:${source.slice(-16)}`;
}

function resultTextLength(event, activeResult = null) {
	const item = activeResult || event?.result || null;
	return String(item?.content || event?.message?.content || "").length;
}

function visibleEventSignatureForMessages(list = []) {
	const parts = [];
	for (const msg of list || []) {
		if (msg?.role === "user") {
			parts.push(["user", msg.id || "", textSignal(msg.content || "")].join(":"));
			continue;
		}
		if (Array.isArray(msg?.localTimeline)) {
			for (const event of msg.localTimeline) {
				if (!event || event.kind === "live_status") continue;
				if (event.kind === "answer") {
					parts.push(["answer", event.id || event.eventKey || "", textSignal(event.message?.content || ""), textSignal(event.message?.reasoning || ""), event.reasoningActive ? "live" : "done"].join(":"));
				} else if (event.kind === "tool" || event.kind === "tool_group") {
					if (isAgentEvent(event)) continue;
					parts.push(["tool", event.id || toolResultKey(event) || "", event.live ? "live" : "done", resultTextLength(event, activeToolResult(event)), Number(event.operation?.revision || 0) || ""].join(":"));
				} else {
					parts.push([event.kind || "event", event.id || "", Number(event.operation?.revision || 0) || textSignal(event.status || event.preview || "")].join(":"));
				}
			}
			continue;
		}
		const text = [textSignal(msg?.content || ""), textSignal(msg?.reasoning || "")].filter(Boolean).join("\n");
		if (text) parts.push([msg?.role || "msg", msg?.id || "", text].join(":"));
	}
	return parts.join("|");
}

function noteVisibleOutput(list = messages.value, {force = false} = {}) {
	const sig = visibleEventSignatureForMessages(list);
	if (force || sig !== visibleOutputSignature) {
		visibleOutputSignature = sig;
		lastVisibleOutputAt = Date.now();
	}
}

function resetTransientThinking() {
	visibleOutputSignature = "";
	lastVisibleOutputAt = 0;
}

function withTransientIdleThinking(turnList = []) {
	return projectTransientIdleThinking(turnList, {
		rootTurnRunning: rootTurnRunning.value,
		activeRootTurnId: activeRunTurnUuid.value,
	}, {
		startedAtMs: runStartedAt.value,
		lastVisibleOutputAtMs: lastVisibleOutputAt,
	});
}

const SCROLL_BOTTOM_THRESHOLD = 80;
const USER_SCROLL_INTENT_MS = 1200;
const EXPLICIT_UNLOCK_GRACE_MS = 450;

function scrollerDistanceFromBottom(el = scroller.value) {
	if (!el) return 0;
	return Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight);
}

function scrollerAtBottom(el = scroller.value) {
	return scrollerDistanceFromBottom(el) <= SCROLL_BOTTOM_THRESHOLD;
}

function updateScrollerOverflow() {
	const el = scroller.value;
	scrollerOverflow.value = Boolean(el && el.scrollHeight > el.clientHeight + SCROLL_BOTTOM_THRESHOLD);
}

function markUserScrollIntent() {
	userScrollIntentAt = Date.now();
}

function hasRecentUserScrollIntent(now = Date.now()) {
	return userScrollIntentAt > 0 && now - userScrollIntentAt <= USER_SCROLL_INTENT_MS;
}

function markProgrammaticScroll(durationMs = 120) {
	programmaticScrollDepth += 1;
	window.setTimeout(() => {
		programmaticScrollDepth = Math.max(0, programmaticScrollDepth - 1);
	}, Math.max(0, Number(durationMs || 0)));
}

function runProgrammaticScroll(fn, durationMs = 120) {
	markProgrammaticScroll(durationMs);
	try {
		fn?.();
	} catch {
	} finally {
		// Keep direction detection aligned even if the browser coalesces the
		// resulting scroll event until after the timed programmatic guard.
		lastScrollerScrollTop = Number(scroller.value?.scrollTop || 0);
	}
}

function lockAutoScroll() {
	pinnedActiveTurnIndex = null;
	autoScrollLocked.value = true;
	void scrollBottom({force: true, cause: "lock"});
}

function unlockAutoScroll() {
	pendingLoadBottomScroll = null;
	explicitUnlockAt = Date.now();
	autoScrollLocked.value = false;
	updateScrollerOverflow();
}

function toggleAutoScrollLock() {
	if (autoScrollLocked.value) unlockAutoScroll();
	else lockAutoScroll();
}

// App calls these only around mobile shell geometry changes. Reuse the existing
// reading anchor; never turn a viewport/keyboard change into a scroll-to-bottom.
function captureMobileViewportAnchor() {
	return {uuid: props.conversationUuid, generation: loadRequestGeneration, anchor: autoScrollLocked.value ? null : captureScrollAnchor()};
}
function restoreMobileViewportAnchor(snapshot) {
	if (!snapshot?.anchor) return;
	return restoreScrollAnchor(snapshot.anchor, {
		isCurrent: () => componentMounted && props.conversationUuid === snapshot.uuid && loadRequestGeneration === snapshot.generation && !autoScrollLocked.value,
	});
}

// Read every turn's viewport box once; the anchor policy and the active-turn
// policy both need the same geometry.
function scrollerTurnBoxes(el) {
	const scrollerRect = el.getBoundingClientRect();
	const nodes = Array.from(el.querySelectorAll(".turn-block[data-turn-index]"));
	const rows = nodes.map((node) => {
		const rect = node.getBoundingClientRect();
		return {node, index: Number(node.dataset.turnIndex || 0), top: rect.top, bottom: rect.bottom};
	});
	return {scrollerRect, rows};
}

// Height animation preserves either the reading character or, when that
// character is being folded, the disclosure's stationary header.
function captureWorkMotion(element) {
	const scroll = scroller.value;
	if (!scroll) return null;
	const anchor = captureScrollAnchor();
	const marker = anchor?.content?.node && element.contains(anchor.content.node)
		? element.previousElementSibling : null;
	const saved = {uuid: activeConversationUuid.value, generation: loadRequestGeneration,
		anchor, marker, markerTop: marker?.getBoundingClientRect().top,
		follow: autoScrollLocked.value, scroll, cancelled: false};
	saved.cancel = () => { saved.cancelled = true; };
	scroll.addEventListener('wheel', saved.cancel, {passive: true});
	scroll.addEventListener('touchstart', saved.cancel, {passive: true});
	return saved;
}
function restoreWorkMotion(saved) {
	if (!saved || saved.cancelled || saved.uuid !== activeConversationUuid.value || saved.generation !== loadRequestGeneration) return;
	if (saved.follow && autoScrollLocked.value) {
		runProgrammaticScroll(() => { saved.scroll.scrollTop = saved.scroll.scrollHeight; }, 120);
	} else if (!saved.follow && !autoScrollLocked.value) {
		if (saved.marker?.isConnected) runProgrammaticScroll(() => {
			saved.scroll.scrollTop += saved.marker.getBoundingClientRect().top - saved.markerTop;
		}, 120);
		else applyReadingAnchor(saved.anchor);
	}
}
function finishWorkMotion(saved) {
	if (!saved) return;
	saved.scroll.removeEventListener('wheel', saved.cancel);
	saved.scroll.removeEventListener('touchstart', saved.cancel);
}

function captureScrollAnchor() {
	const el = scroller.value;
	if (!el) return null;
	const {scrollerRect, rows} = scrollerTurnBoxes(el);
	for (const row of rows) {
		if (row.bottom < scrollerRect.top) continue;
		if (row.top > scrollerRect.bottom) break;
		return {index: row.index, offset: row.top - scrollerRect.top, scrollTop: el.scrollTop,
			content: captureTranscriptContentAnchor(row.node, scrollerRect)};
	}
	return {index: -1, offset: 0, scrollTop: el.scrollTop};
}

// The single place that moves scrollTop back onto a captured reading anchor.
// Both the async restore paths and width-reflow compensation go through here so
// they share the programmatic-scroll guard and active-turn bookkeeping.
function applyReadingAnchor(anchor) {
	const el = scroller.value;
	if (!el || !anchor) return;
	runProgrammaticScroll(() => {
		if (Number(anchor.index) >= 0) {
			const node = el.querySelector(`.turn-block[data-turn-index="${Number(anchor.index)}"]`);
			if (node) {
				const scrollerRect = el.getBoundingClientRect();
				const rect = node.getBoundingClientRect();
				const contentDelta = transcriptContentAnchorDelta(anchor.content, node, scrollerRect);
				el.scrollTop += contentDelta ?? ((rect.top - scrollerRect.top) - Number(anchor.offset || 0));
				return;
			}
		}
		const maxScrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
		el.scrollTop = Math.min(maxScrollTop, Math.max(0, Number(anchor.scrollTop || 0)));
	}, 120);
	updateScrollerOverflow();
	scheduleActiveTurnFromScroll();
}

async function restoreScrollAnchor(anchor, options = {}) {
	if (!anchor || autoScrollLocked.value || (options?.isCurrent && !options.isCurrent())) return;
	await nextTick();
	if (options?.isCurrent && !options.isCurrent()) return;
	applyReadingAnchor(anchor);
}

// A width change re-wraps every turn without emitting a scroll event, so the
// transcript silently slides under the reader: the same scrollTop now sits
// above newer lines, and the work-detail panel keeps showing the turn the
// reader has already moved past. Re-apply the intent of each scroll state
// instead of leaving the stale pixel offset in place.
function compensateTranscriptReflow() {
	const el = scroller.value;
	if (!el) return;
	if (autoScrollLocked.value) {
		runProgrammaticScroll(() => {
			el.scrollTop = el.scrollHeight;
		}, 120);
	} else if (readingAnchor) {
		// ResizeObserver reports after the new layout exists, so the DOM cannot say
		// where the reader was. Restore the position this component observed while
		// the old layout was still on screen.
		applyReadingAnchor(readingAnchor);
	} else {
		// Without evidence of a reading position, guessing one would move the
		// transcript for no reason.
		return;
	}
	// Re-derive both the reading position and the active turn from the corrected
	// layout. The panel width animates, so the next frame of the same transition
	// starts from what the reader can actually see, and the panel never describes
	// a turn the correction has just scrolled past.
	updateActiveTurnFromScroll();
	updateScrollerOverflow();
}

function observeScrollerReflow() {
	if (typeof ResizeObserver === "undefined") return;
	const el = scroller.value;
	if (!el) return;
	scrollerResizeObserver = new ResizeObserver(() => {
		const target = scroller.value;
		if (!target) return;
		const width = Math.round(target.clientWidth);
		// The first callback only establishes the baseline. Height-only changes
		// (composer growth, keyboard) already own their own restore paths.
		if (!observedScrollerWidth) {
			observedScrollerWidth = width;
			return;
		}
		if (width === observedScrollerWidth) return;
		observedScrollerWidth = width;
		// The panel width animates for 240 ms, so one width change arrives as a
		// burst of callbacks. Coalesce them into one compensation per frame that
		// reads the layout as it stands when it runs.
		if (scrollerReflowFrame) return;
		scrollerReflowFrame = window.requestAnimationFrame(() => {
			scrollerReflowFrame = 0;
			compensateTranscriptReflow();
		});
	});
	scrollerResizeObserver.observe(el);
}

function updateActiveTurnFromScroll() {
	activeTurnScrollFrame = 0;
	lastActiveTurnScrollUpdateAt = performance.now();
	const el = scroller.value;
	if (!el) return;
	const {scrollerRect, rows} = scrollerTurnBoxes(el);
	if (!rows.length) {
		pinnedActiveTurnIndex = null;
		readingAnchor = null;
		activeTurnIndex.value = 0;
		return;
	}
	const nextActiveTurnIndex = chooseActiveTurnIndex(rows, scrollerRect.top, scrollerRect.height, {
		atBottom: scrollerAtBottom(el),
		preferredIndex: pinnedActiveTurnIndex,
	});
	activeTurnIndex.value = nextActiveTurnIndex;
	// Record the reading position while the current layout is still observable.
	// It has to be the same turn this function just decided the panel describes:
	// a width change rewraps the transcript without emitting a scroll event, so
	// this anchor is the only evidence of what the reader was looking at.
	const activeRow = rows.find((row) => row.index === nextActiveTurnIndex);
	readingAnchor = activeRow
		? {index: activeRow.index, offset: activeRow.top - scrollerRect.top, scrollTop: Number(el.scrollTop || 0),
			content: autoScrollLocked.value ? null : captureTranscriptContentAnchor(activeRow.node, scrollerRect,
				readingAnchor?.index === activeRow.index ? readingAnchor.content : null)}
		: null;
}

function scheduleActiveTurnFromScroll({force = false} = {}) {
	if (force) {
		if (activeTurnScrollTimer) window.clearTimeout(activeTurnScrollTimer);
		if (activeTurnScrollFrame) window.cancelAnimationFrame(activeTurnScrollFrame);
		activeTurnScrollTimer = 0;
		activeTurnScrollFrame = window.requestAnimationFrame(updateActiveTurnFromScroll);
		return;
	}
	if (activeTurnScrollTimer || activeTurnScrollFrame) return;
	const wait = Math.max(0, ACTIVE_TURN_SCROLL_UPDATE_MS - (performance.now() - lastActiveTurnScrollUpdateAt));
	activeTurnScrollTimer = window.setTimeout(() => {
		activeTurnScrollTimer = 0;
		activeTurnScrollFrame = window.requestAnimationFrame(updateActiveTurnFromScroll);
	}, wait);
}

function scrollToTurnIndex(index) {
	const el = scroller.value;
	if (!el) return;
	const targetIndex = Number(index);
	const node = el.querySelector(`.turn-block[data-turn-index="${targetIndex}"]`);
	if (!node) return;
	// Short adjacent turns near the bottom cannot all reach the fixed reading
	// anchor. Treat an explicit minimap choice as authoritative until the user
	// genuinely scrolls again, rather than letting scroll clamping overwrite it.
	pinnedActiveTurnIndex = targetIndex;
	userScrollIntentAt = 0;
	activeTurnIndex.value = targetIndex;
	unlockAutoScroll();
	runProgrammaticScroll(() => {
		node.scrollIntoView({behavior: "smooth", block: "start"});
	}, 700);
}

async function loadEarlierOperations() {
	const conversationUuid = String(activeConversationUuid.value || "");
	const requestedCursor = Number(nextBeforeDisplaySeq.value || 0) || 0;
	if (
		!conversationUuid
		|| conversationUuid.startsWith("local:")
		|| !hasMoreBefore.value
		|| requestedCursor <= 0
		|| timelinePageInFlight.value
	) return false;
	const request = {
		generation: timelinePageGeneration,
		token: ++timelinePageRequestToken,
		conversationUuid,
	};
	timelinePageInFlight.value = request;
	try {
		const data = await Api.conversationOperations(conversationUuid, {
			timelineLimit: INITIAL_TIMELINE_LIMIT,
			beforeDisplaySeq: requestedCursor,
		});
		if (
			!sameTimelinePageRequest(timelinePageInFlight.value, request)
			|| request.generation !== timelinePageGeneration
			|| conversationUuid !== String(activeConversationUuid.value || "")
			|| timelinePageConversationUuid !== conversationUuid
		) return false;
		const preservedActiveTurnIdentity = stableTurnIdentity(activeTurn.value);
		const activeTurnWasPinned = pinnedActiveTurnIndex !== null;
		const anchor = capturePrependAnchor(scroller.value);
		const incoming = normalizeOperations(Array.isArray(data?.operations) ? data.operations : []);
		const merged = mergeOperationSnapshots(orderedOperationsList(), incoming);
		replaceOperationSnapshots(merged);
		messages.value = projectOperationMessages(merged);
		if (chatState.value) chatState.value = {...chatState.value, operations: merged};
		syncRunStateFromOperations(merged, chatState.value);
		const nextCursor = Number(data?.nextBeforeDisplaySeq || 0) || 0;
		const cursorProgressed = nextCursor > 0 && nextCursor < requestedCursor;
		hasMoreBefore.value = Boolean(data?.hasMoreBefore && cursorProgressed);
		nextBeforeDisplaySeq.value = hasMoreBefore.value ? nextCursor : null;
		timelinePageInitialized = true;
		const remappedActiveTurnIndex = findTurnIndexByIdentity(turns.value, preservedActiveTurnIdentity);
		if (remappedActiveTurnIndex >= 0) {
			activeTurnIndex.value = remappedActiveTurnIndex;
			if (activeTurnWasPinned) pinnedActiveTurnIndex = remappedActiveTurnIndex;
		}
		await nextTick();
		if (
			!sameTimelinePageRequest(timelinePageInFlight.value, request)
			|| request.generation !== timelinePageGeneration
			|| conversationUuid !== String(activeConversationUuid.value || "")
		) return false;
		const el = scroller.value;
		if (el && anchor) {
			runProgrammaticScroll(() => {
				el.scrollTop = prependAnchoredScrollTop(anchor, el.scrollHeight);
			}, 120);
			updateScrollerOverflow();
			scheduleActiveTurnFromScroll({force: true});
		}
		return true;
	} catch (error) {
		console.warn("earlier operation page load failed", error);
		return false;
	} finally {
		timelinePageInFlight.value = settleTimelinePageRequest(timelinePageInFlight.value, request);
	}
}

function requestEarlierPageFromUser({explicitUpward = false, previousScrollTop = null} = {}) {
	const currentScrollTop = Number(scroller.value?.scrollTop || 0);
	const shouldRequest = shouldRequestEarlierPage({
		programmaticScroll: programmaticScrollDepth > 0,
		userIntent: hasRecentUserScrollIntent(),
		explicitUpward,
		previousScrollTop,
		scrollTop: currentScrollTop,
		threshold: LOAD_EARLIER_SCROLL_THRESHOLD,
	});
	if (!shouldRequest) return false;
	// Consume this input direction before the async prepend. Its height
	// compensation and any delayed layout scroll therefore cannot request a
	// second page without a new wheel/touch/pointer gesture.
	userScrollIntentAt = 0;
	void loadEarlierOperations();
	return true;
}

function handleScrollerScroll() {
	scheduleActivityRead();
	const el = scroller.value;
	if (!el) return;
	const previousScrollTop = lastScrollerScrollTop;
	lastScrollerScrollTop = Number(el.scrollTop || 0);
	updateScrollerOverflow();
	const atBottom = scrollerAtBottom(el);
	const now = Date.now();
	const userIntent = hasRecentUserScrollIntent(now);
	// Pagination must be behind the programmatic guard. In particular, the
	// scrollTop adjustment after prepend is never interpreted as user movement.
	if (programmaticScrollDepth > 0) {
		scheduleActiveTurnFromScroll();
		return;
	}
	if (userIntent) {
		requestEarlierPageFromUser({previousScrollTop});
		pinnedActiveTurnIndex = null;
	}
	// Only user-driven movement changes the lock state. Programmatic scrolls and
	// layout reflows must not silently unlock/re-lock while the user is reading.
	if (autoScrollLocked.value && !atBottom && userIntent) {
		unlockAutoScroll();
	}
	if (!autoScrollLocked.value && atBottom && userIntent && now - explicitUnlockAt > EXPLICIT_UNLOCK_GRACE_MS) {
		autoScrollLocked.value = true;
	}
	scheduleActiveTurnFromScroll({force: userIntent});
}

function handleScrollerWheel(event) {
	pinnedActiveTurnIndex = null;
	markUserScrollIntent();
	const scrollingUp = Number(event?.deltaY || 0) < 0;
	lastScrollerScrollTop = Number(scroller.value?.scrollTop || 0);
	// When locked, an upward scroll means the user wants to read history:
	// unlock and let the browser scroll normally. At the loaded top, the same
	// gesture transparently prepends the previous SQL page.
	if (autoScrollLocked.value && scrollingUp) unlockAutoScroll();
	if (programmaticScrollDepth > 0) return;
	requestEarlierPageFromUser({explicitUpward: scrollingUp});
}

function handleScrollerTouchStart(event) {
	const touch = event?.touches?.[0];
	touchScrollClientY = touch ? Number(touch.clientY) : null;
	lastScrollerScrollTop = Number(scroller.value?.scrollTop || 0);
	markUserScrollIntent();
}

function handleScrollerTouchMove(event) {
	const touch = event?.touches?.[0];
	const currentClientY = touch ? Number(touch.clientY) : null;
	const scrollingUp = touchMovesTimelineUp(touchScrollClientY, currentClientY);
	touchScrollClientY = currentClientY;
	pinnedActiveTurnIndex = null;
	markUserScrollIntent();
	if (autoScrollLocked.value && scrollingUp) unlockAutoScroll();
	if (programmaticScrollDepth > 0) return;
	requestEarlierPageFromUser({explicitUpward: scrollingUp});
}

function handleScrollerTouchEnd() {
	touchScrollClientY = null;
}

function handleScrollerPointerDown() {
	lastScrollerScrollTop = Number(scroller.value?.scrollTop || 0);
	markUserScrollIntent();
}

async function scrollBottom(options = {}) {
	const force = Boolean(options?.force);
	if ((!force && !autoScrollLocked.value) || (options?.isCurrent && !options.isCurrent())) return;
	await nextTick();
	if (options?.isCurrent && !options.isCurrent()) return;
	if (scroller.value) {
		runProgrammaticScroll(() => {
			scroller.value.scrollTop = scroller.value.scrollHeight;
		}, Number(options?.durationMs || 120));
		updateScrollerOverflow();
		scheduleActiveTurnFromScroll({force: true});
	}
}

function scheduleScrollBottom(options = {}) {
	const force = Boolean(options?.force);
	if (!force && !autoScrollLocked.value) return;
	if (scrollFrame) return;
	scrollFrame = window.requestAnimationFrame(async () => {
		scrollFrame = 0;
		await nextTick();
		if ((force || autoScrollLocked.value) && scroller.value) {
			runProgrammaticScroll(() => {
				scroller.value.scrollTop = scroller.value.scrollHeight;
			}, Number(options?.durationMs || 120));
			updateScrollerOverflow();
			scheduleActiveTurnFromScroll({force: true});
		}
	});
}

function cancelScheduledUiWork() {
	if (scrollFrame) window.cancelAnimationFrame(scrollFrame);
	if (activeTurnScrollFrame) window.cancelAnimationFrame(activeTurnScrollFrame);
	if (activeTurnScrollTimer) window.clearTimeout(activeTurnScrollTimer);
	if (scrollerReflowFrame) window.cancelAnimationFrame(scrollerReflowFrame);
	if (streamFlushFrame) window.cancelAnimationFrame(streamFlushFrame);
	if (streamFlushTimer) window.clearTimeout(streamFlushTimer);
	scrollFrame = 0;
	activeTurnScrollFrame = 0;
	activeTurnScrollTimer = 0;
	scrollerReflowFrame = 0;
	streamFlushFrame = 0;
	streamFlushTimer = 0;
	streamFlushPending = false;
	pendingProjectionOps = null;
	pendingScrollImpact = "none";
	pendingPreserveAnchor = null;
	pendingTerminalFrame = null;
	if (operationResyncTimer) {
		window.clearTimeout(operationResyncTimer);
		operationResyncTimer = null;
	}
	operationResyncInFlight = null;
}

function closeWs() {
	terminalStateRefreshScheduler.invalidate();
	cancelScheduledUiWork();
	if (reconnectTimer) {
		window.clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
	if (ws) {
		try {
			ws.onclose = null;
			ws.onerror = null;
			ws.onmessage = null;
			ws.close();
		} catch { /* ignore */
		}
	}
	ws = null;
	wsConversationUuid = "";
}

function normalizePendingSteering(items = []) {
	return (Array.isArray(items) ? items : [])
		.map((item) => ({
			id: String(item?.id || item?.messageUuid || item?.turnUuid || `${Date.now()}-${Math.random()}`),
			text: String(item?.visibleText || item?.text || item?.content || "").trim(),
			...(item?.referenceBundleId ? {referenceBundleId: item.referenceBundleId, references: item.references || []} : {}),
			submittedAtMs: Number(item?.submittedAtMs || 0) || Date.now(),
		}))
		.filter((item) => item.text);
}

function updatePendingSteering(items = []) {
	pendingSteering.value = normalizePendingSteering(items);
}

function applyPendingSteeringEvent(data = {}) {
	const action = String(data.action || "snapshot");
	if (action === "drain" || action === "clear") {
		const ids = new Set((Array.isArray(data.itemIds) ? data.itemIds : []).map((x) => String(x)).filter(Boolean));
		pendingSteering.value = ids.size ? pendingSteering.value.filter((item) => !ids.has(String(item.id))) : [];
		return;
	}
	updatePendingSteering(data.items || []);
}

function finishPendingOutboundSend(requestId) {
	const pending = outboundSends.take(String(requestId || ""));
	if (!pending) return false;
	sendPending.value = false;
	// Only release attachments belonging to this request. The user may already
	// have added files for their next message while this ACK was in flight.
	const sentIds = new Set(pending.attachments.map((item) => item.id));
	queueSentAttachmentPreviewRevokes(pending.previewUrls || []);
	releaseSentAttachments(pending.conversationUuid, sentIds);
	return true;
}

function restoreReleasedOutboundSend(pending, error = "send_failed", {uncertain = false} = {}) {
	if (!pending) return false;
	pending.uploadController?.abort();
	sendPending.value = Boolean(outboundSends.current);
	const uuid = pending.conversationUuid;
	const isActive = uuid === activeConversationUuid.value;
	const currentDraft = isActive ? draft.value : (draftByConversation.value[draftKey(uuid)] || "");
	const restoredDraft = restoreOutboundDraft(pending.draftText, currentDraft);
	setDraftForConversation(uuid, restoredDraft);
	// Retain the original files as well as any new draft attachments. Recreate
	// previews if a file was removed while awaiting its acceptance receipt.
	restoreReleasedAttachments(uuid, pending.attachments);
	if (!isActive) return true;
	if (pending.optimisticId) {
		messages.value = messages.value.filter((message) => String(message?.id || "") !== pending.optimisticId);
	}
	draft.value = restoredDraft;
	adjustComposerHeight();
	// Do not turn off a real run whose frames arrived before a lost ACK.
	const operations = orderedOperationsList();
	syncRunStateFromOperations(operations, operations.length ? null : chatState.value);
	status.value = uncertain ? "发送结果未确认" : (["busy", "conversation_compacting"].includes(error) ? "会话正在处理其他操作" : "发送失败");
	return true;
}

function restorePendingOutboundSend(requestId, error = "send_failed") {
	return restoreReleasedOutboundSend(outboundSends.take(String(requestId || "")), error);
}

function recoverUnconfirmedSend(pending) {
	if (!pending) return;
	const uncertain = pending.phase === "sent";
	restoreReleasedOutboundSend(pending, "send_timeout", {uncertain});
	ElMessage.warning({
		message: uncertain
			? "发送结果未确认，草稿和附件已保留。请先核对会话再重试；不会自动重发。"
			: "消息未能发送，草稿和附件已恢复，请检查连接后重试。",
		duration: 8000,
	});
	const uuid = pending.conversationUuid;
	const generation = sendAttemptGeneration;
	const isCurrent = () => componentMounted && uuid === activeConversationUuid.value && generation === sendAttemptGeneration;
	if (!isCurrent() || String(uuid).startsWith("local:")) return;
	// The UI is already unlocked. A failed HTTP refresh must not lock it again.
	closeWs();
	void connectWs(uuid);
	void load({conversationUuid: uuid, scrollMode: "preserve", manageLoading: false, isCurrent}).then(() => {
		if (isCurrent() && uncertain && !running.value) status.value = "发送结果未确认，请核对会话";
	});
}

function recoverDisconnectedSend() {
	const pending = outboundSends.current;
	if (!pending || pending.phase !== "sent") return false;
	recoverUnconfirmedSend(outboundSends.take(pending.requestId));
	return true;
}

function leavePendingSend() {
	sendAttemptGeneration += 1;
	const pending = outboundSends.current;
	if (pending) restoreReleasedOutboundSend(outboundSends.take(pending.requestId));
}

function handleWsMessage(raw, source = {}) {
	if (source.socket && (source.socket !== ws || source.conversationUuid !== wsConversationUuid)) return;
	let data = null;
	try {
		data = JSON.parse(raw?.data || raw || "{}");
	} catch {
		return;
	}
	if (data.type === "task_memory.changed") {
		const event = taskMemoryChangedTransportEvent(data, {
			activeConversationUuid: activeConversationUuid.value,
			sourceConversationUuid: source.conversationUuid,
			socketConversationUuid: wsConversationUuid,
			sourceIsActive: !source.socket || source.socket === ws,
		});
		if (event) taskMemoryChangedEvent.value = event;
		return;
	}
	if (data.type === "message_visibility.changed") {
		messageVisibility.apply(data.visibility);
		return;
	}
	if (data.type === "bootstrap") {
		messageVisibility.apply(data.messageVisibility);
		updatePendingConfirmations(data.pendingConfirmations || []);
		updatePendingSteering(data.pendingSteering || []);
		return;
	}
	if (data.type === "resync_required") {
		debugFrames("ws-resync-required", data);
		void resyncOperationFrames({...data, requiresFullState: true});
		return;
	}
	if (data.type === "state") {
		debugFrames("ws-state", {state: data.state});
		const state = data.state || {};
		messageVisibility.apply(state.messageVisibility, false);
		const beforeSignature = visibleEventSignatureForMessages(messages.value);
		const preserveAnchor = !autoScrollLocked.value ? captureScrollAnchor() : null;
		const ops = loadOperationsFromState(state);
		const preserveOptimisticMessages = shouldPreserveOptimisticMessages(state) && !ops.length;
		if (!preserveOptimisticMessages)
			messages.value = ops.length ? projectOperationMessages(ops) : (Array.isArray(state.messages) ? state.messages : []);
		chatState.value = {
			...state,
			usage: normalizeLedgerUsageBaseline(state.usage || {}),
		};
		updatePendingConfirmations(state.pendingConfirmations || []);
		updatePendingSteering(state.pendingSteering || []);
		lastStats.value = null;
		if (ops.length) syncRunStateFromOperations(ops, state);
		else {
			running.value = Boolean(state.running);
			foregroundRunning.value = Boolean(state.running && !state.backgroundRunning);
			rootTurnRunning.value = false;
			clearActiveRun();
			status.value = state.backgroundRunning ? (state.backgroundStatus || "Agent 后台执行中") : (state.conversation?.currentStatus || (running.value ? "运行中" : "就绪"));
			if (!running.value) runStartedAt.value = 0;
		}
		replayBufferedOperationFrames(Number(state.frameSeq || state.facts?.latestFrameSeq || 0));
		if (operationFrameBuffer.blocked) scheduleOperationStateResync({requiresFullState: true});
		const afterSignature = visibleEventSignatureForMessages(messages.value);
		noteVisibleOutput(messages.value, {force: true});
		if (autoScrollLocked.value && beforeSignature !== afterSignature) scrollBottom();
		else if (preserveAnchor) void restoreScrollAnchor(preserveAnchor);
		return;
	}
	if (data.type === "conversation_reset") {
		applyCommittedTurnDeletion(activeConversationUuid.value, data.deletedRootTurns);
		void load({scrollMode: "preserve", replaceOperations: true});
		return;
	}
	if (data.type === "stopped") {
		convergeStoppedAcknowledgement({
			message: data,
			sourceSocket: source.socket,
			activeSocket: ws,
			sourceConversationUuid: source.conversationUuid,
			socketConversationUuid: wsConversationUuid,
			activeConversationUuid: activeConversationUuid.value,
			setStatus: (nextStatus) => {
				status.value = nextStatus;
			},
			refreshCurrentState: (reason) => scheduleTerminalStateRefresh(reason),
			refreshConversationList: () => emit("conversations-refresh"),
		});
		return;
	}
	if (data.type === "frame") {
		const frame = data.frame || {};
		if (conversationStateRequests.pending || operationResyncInFlight || operationFrameBuffer.blocked) operationFrameBuffer.add(frame);
		if (operationFrameBuffer.blocked) return;
		const shouldApply = shouldApplyOperationFrame(frame, {
			operationsById: operationsById.value,
			revisionByOpId: revisionByOpId.value,
			lastFrameSeq: lastFrameSeq.value,
		});
		debugFrames("ws-frame", {shouldApply, frame});
		if (!shouldApply) return;
		applyOperationFrameMessage(frame);
		return;
	}
	if (data.type === "event") return;
	if (data.type === "web_confirmation") {
		updatePendingConfirmations(data.confirmations || data.confirmation || []);
		return;
	}
	if (data.type === "pending_steering") {
		const anchor = !autoScrollLocked.value ? captureScrollAnchor() : null;
		applyPendingSteeringEvent(data);
		if (anchor) void restoreScrollAnchor(anchor);
		return;
	}
	if (data.type === "ack") {
		finishPendingOutboundSend(data.requestId);
		return;
	}
	if (data.type === "error") {
		const error = String(data.error || "WebSocket 错误");
		const restored = restorePendingOutboundSend(data.requestId, error);
		if (restored && ["busy", "conversation_compacting"].includes(error)) {
			ElMessage.warning("会话正在处理其他操作，消息未发送，草稿已恢复");
		} else {
			ElMessage.error(data.referenceError ? `${referenceErrorText(error)}${data.referenceError.label ? '：' + data.referenceError.label : ''}` : error);
		}
	}
}

async function connectWs(conversationUuid = props.conversationUuid) {
	const uuid = conversationUuid;
	if (!uuid || String(uuid).startsWith("local:")) return null;
	if (ws && wsConversationUuid === uuid && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return ws;
	closeWs();
	wsConversationUuid = uuid;
	const socket = new WebSocket(conversationWsUrl(uuid, lastFrameSeq.value, {bootstrap: "incremental"}));
	ws = socket;
	socket.onmessage = (event) => handleWsMessage(event, {socket, conversationUuid: uuid});
	// Keep the baseline user-visible connection acknowledgement even though the
	// transport now bootstraps incrementally instead of sending a second state.
	socket.onopen = () => {
		if (ws !== socket || wsConversationUuid !== uuid || !componentMounted) return;
		status.value = running.value ? status.value : "已连接";
		if (operationFrameBuffer.blocked) scheduleOperationStateResync({requiresFullState: true});
	};
	socket.onerror = () => {
		if (ws !== socket || wsConversationUuid !== uuid) return;
		status.value = "连接异常";
		recoverDisconnectedSend();
	};
	socket.onclose = () => {
		if (ws !== socket || wsConversationUuid !== uuid) return;
		terminalStateRefreshScheduler.invalidate();
		if (recoverDisconnectedSend()) return;
		if (!reconnectTimer) reconnectTimer = window.setTimeout(() => {
			reconnectTimer = null;
			void connectWs();
		}, 1200);
	};
	return socket;
}

async function ensureResponsiveWs(conversationUuid, isCurrent) {
	for (let attempt = 0; attempt < 2; attempt += 1) {
		if (!isCurrent()) throw new Error("send_cancelled");
		const socket = await connectWs(conversationUuid);
		try {
			await waitForSocketOpen(socket);
			if (!isCurrent() || socket !== ws) throw new Error("send_cancelled");
			await probeSocket(socket);
			if (!isCurrent() || socket !== ws) throw new Error("send_cancelled");
			return socket;
		} catch (error) {
			if (!isCurrent()) throw error;
			if (ws === socket) closeWs();
			if (attempt === 1) throw error;
		}
	}
}

function checkConnectionOnResume() {
	if (!componentMounted || document.visibilityState !== "visible") return;
	outboundSends.checkDeadline();
	if (outboundSends.current || connectionResumePromise || isLocalConversation.value) return;
	const uuid = activeConversationUuid.value;
	// pageshow/focus may arrive before the first HTTP snapshot (or mid-switch).
	// Connecting here with the reset cursor would replay all retained history.
	if (!uuid || !timelinePageInitialized || timelinePageConversationUuid !== uuid) return;
	const generation = sendAttemptGeneration;
	const isCurrent = () => componentMounted && uuid === activeConversationUuid.value && generation === sendAttemptGeneration;
	const promise = (async () => {
		let socket = null;
		try {
			socket = await connectWs(uuid);
			await waitForSocketOpen(socket);
			if (!isCurrent() || socket !== ws) return;
			await probeSocket(socket);
		} catch {
			if (!isCurrent() || !socket || socket !== ws) return;
			closeWs();
			void connectWs(uuid);
			await load({conversationUuid: uuid, scrollMode: "preserve", manageLoading: false, isCurrent});
		}
	})().finally(() => {
		if (connectionResumePromise === promise) connectionResumePromise = null;
	});
	connectionResumePromise = promise;
}

async function ensureServerConversationForSend(firstText, pending) {
	if (!isLocalConversation.value) return activeConversationUuid.value;
	await ensureLocalRunDefaults();
	if (!outboundSends.isCurrent(pending)) return "";
	const title = initialConversationTitle(referenceDisplayText(firstText || "新会话"));
	const created = await Api.createConversation({title, runConfig: completeLocalRunConfig(), folderId: props.folderId || ""});
	if (!outboundSends.isCurrent(pending)) return "";
	const uuid = created.conversation?.conversationUuid || created.state?.conversationUuid || "";
	if (!uuid) throw new Error("conversation_create_failed");
	pending.conversationUuid = uuid;
	localToServerTransitionUuid.value = uuid;
	// The composer keeps these files until a receipt releases them, so they must
	// follow the conversation to its server id before any receipt can arrive.
	migrateAttachmentDraft(props.conversationUuid, uuid);
	chatState.value = {...(chatState.value || {}), conversationUuid: uuid};
	emit("conversation-created", uuid);
	await nextTick();
	return uuid;
}

function applyLoadedConversationState(data, conversationUuid, {replaceOperations = false, runConfigVersionAtRequest = null} = {}) {
	messageVisibility.apply(data.messageVisibility, false);
	const mergeExisting = !replaceOperations
		&& timelinePageConversationUuid === String(conversationUuid || "")
		&& operationsById.value.size > 0;
	const ops = loadOperationsFromState(data, {merge: mergeExisting});
	applyTimelinePageMetadata(data, conversationUuid, {preserve: mergeExisting});
	const preserveOptimisticMessages = shouldPreserveOptimisticMessages(data) && !ops.length;
	if (!preserveOptimisticMessages) messages.value = ops.length ? projectOperationMessages(ops) : (Array.isArray(data.messages) ? data.messages : []);
	chatState.value = {
		...data,
		operations: ops,
		usage: normalizeLedgerUsageBaseline(data.usage || {}),
	};
	if (
		runConfigVersionAtRequest !== null
		&& runConfigOverride.value?.conversationUuid === String(conversationUuid || "")
		&& mayRetireRunConfigOverride(runConfigVersionAtRequest, runConfigSaves.appliedVersion)
	) runConfigOverride.value = null;
	updatePendingSteering(data.pendingSteering || []);
	lastStats.value = null;
	let operationRunState = null;
	if (ops.length) operationRunState = syncRunStateFromOperations(ops, data);
	else {
		running.value = Boolean(data.running);
		foregroundRunning.value = Boolean(data.running && !data.backgroundRunning);
		rootTurnRunning.value = false;
		clearActiveRun();
		runStartedAt.value = running.value
			? Number(data.live?.startedAtMs || data.backgroundStartedAtMs || 0) || runStartedAt.value || Date.now()
			: 0;
		status.value = data.backgroundRunning ? (data.backgroundStatus || "Agent 后台执行中") : (data.conversation?.currentStatus || (running.value ? "运行中" : "就绪"));
	}
	hydrateAgentAutoOpenBoundary(conversationUuid, ops, operationRunState);
	noteVisibleOutput(messages.value, {force: true});
	replayBufferedOperationFrames(Number(data.frameSeq || data.facts?.latestFrameSeq || 0));
}

function replayBufferedOperationFrames(cursor) {
	return operationFrameBuffer.replayAfter(cursor, frame => applyOperationFrameMessage(frame, {resyncing: true}));
}

async function load(options = {}) {
	const conversationUuid = String(options?.conversationUuid || props.conversationUuid || "").trim();
	const externalIsCurrent = typeof options?.isCurrent === "function" ? options.isCurrent : null;
	// A stale caller must not invalidate the selected conversation's valid request.
	if (!componentMounted || !conversationUuid || conversationUuid !== String(props.conversationUuid || "").trim()
		|| (externalIsCurrent && !externalIsCurrent())) return;
	const requestGeneration = ++loadRequestGeneration;
	let runConfigVersionAtRequest = runConfigSaves.appliedVersion;
	if (options?.replaceOperations) pendingLoadReplaceOperations = true;
	const isCurrent = () => Boolean(
		componentMounted
		&& requestGeneration === loadRequestGeneration
		&& conversationUuid === String(props.conversationUuid || "").trim()
		&& (!externalIsCurrent || externalIsCurrent())
	);
	const requestedScrollMode = String(options?.scrollMode || "preserve");
	if (requestedScrollMode === "bottom") {
		pendingLoadBottomScroll = {conversationUuid, lock: options?.lock !== false};
	}
	const bottomScroll = pendingLoadBottomScroll?.conversationUuid === conversationUuid ? pendingLoadBottomScroll : null;
	const scrollMode = bottomScroll ? "bottom" : requestedScrollMode;
	const lockOnBottom = bottomScroll ? bottomScroll.lock : options?.lock !== false;
	const preserveAnchor = scrollMode === "preserve" && !autoScrollLocked.value ? captureScrollAnchor() : null;
	if (conversationUuid.startsWith("local:")) {
		if (!modelOptions.value.length) await loadOptions();
		if (!isCurrent()) return;
		if (!localModel.value) applyDefaultLocalModel();
		if (options?.refreshDefaults) await loadLocalRunDefaults(conversationUuid);
		if (!isCurrent()) return;
		resetLocalConversationState(conversationUuid);
		// Initial setup and navigation already own the composer draft. A late
		// defaults/state load must not restore text over edits made meanwhile.
		resetTransientThinking();
		if (scrollMode === "bottom" && bottomScroll === pendingLoadBottomScroll) {
			if (lockOnBottom) autoScrollLocked.value = true;
			await scrollBottom({force: true, cause: "load-local", isCurrent});
			if (isCurrent() && bottomScroll === pendingLoadBottomScroll) pendingLoadBottomScroll = null;
		} else if (preserveAnchor) {
			await restoreScrollAnchor(preserveAnchor, {isCurrent});
		} else {
			await nextTick();
			if (!isCurrent()) return;
			updateScrollerOverflow();
			scheduleActiveTurnFromScroll({force: true});
		}
		return;
	}
	const manageLoading = options?.manageLoading !== false || loading.value;
	if (manageLoading && isCurrent()) loading.value = true;
	try {
		const outcome = await runGuardedConversationStateRefresh({
			conversationUuid,
			isCurrent,
			requestState: async (uuid) => {
				const result = await conversationStateRequests.request(uuid, {fresh: Boolean(options?.fresh || options?.replaceOperations)});
				runConfigVersionAtRequest = result.runConfigVersionAtRequest;
				return result.data;
			},
			applyState: (data, uuid) => {
				applyLoadedConversationState(data, uuid, {
					replaceOperations: pendingLoadReplaceOperations,
					runConfigVersionAtRequest,
				});
				pendingLoadReplaceOperations = false;
			},
			connectState: (uuid) => connectWs(uuid),
		});
		if (outcome.stage !== "complete" || !isCurrent()) return;
		if (operationFrameBuffer.blocked && !operationResyncInFlight) scheduleOperationStateResync({requiresFullState: true});
		if (scrollMode === "bottom" && bottomScroll === pendingLoadBottomScroll) {
			if (lockOnBottom) autoScrollLocked.value = true;
			await scrollBottom({force: true, cause: "load", isCurrent});
			if (isCurrent() && bottomScroll === pendingLoadBottomScroll) pendingLoadBottomScroll = null;
		} else if (preserveAnchor) {
			await restoreScrollAnchor(preserveAnchor, {isCurrent});
		} else {
			await nextTick();
			if (!isCurrent()) return;
			updateScrollerOverflow();
			scheduleActiveTurnFromScroll({force: true});
		}
		return {applied: true};
	} catch (error) {
		if (isCurrent()) ElMessage.error(apiError(error));
		return {applied: false};
	} finally {
		if (manageLoading && requestGeneration === loadRequestGeneration && componentMounted && conversationUuid === String(props.conversationUuid || "").trim()) {
			loading.value = false;
		}
	}
}

async function deleteTurnSuffix(turn) {
	const conversationUuid = activeConversationUuid.value;
	const turnUuid = String(turn?.user?.turnUuid || "").trim();
	if (!conversationUuid || isLocalConversation.value || !turnUuid || deletingTurnUuid.value) return;
	if (running.value) {
		ElMessage.warning("请先停止当前运行并等待收尾，再删除历史轮次");
		return;
	}
	const allTurns = turns.value;
	const index = allTurns.findIndex((item) => String(item?.user?.turnUuid || "") === turnUuid);
	const affectedTurns = index >= 0 ? allTurns.length - index : 1;
	// Canonical reference tokens retain exact IDs/scopes through this same
	// durable draft path; the rich editor restores them as inline nodes.
	const originalUserText = String(turn?.user?.content || "");
	const stillHere = () => componentMounted && activeConversationUuid.value === conversationUuid;
	const originalAttachments = Array.isArray(turn?.user?.attachments) ? turn.user.attachments : [];
	const preview = plainText(originalUserText).replace(/\s+/g, " ").trim().slice(0, 72);
	try {
		await ElMessageBox.confirm(
			`将永久删除这轮${affectedTurns > 1 ? `及后续 ${affectedTurns - 1} 轮` : ""}内容。相关回答、工具/Agent 过程、附件引用和覆盖删除点的模型摘要会一起移除，Agent 仅能从可靠的存活检查点续接，之后可从删除点之前继续对话。会话/实例便笺仍保留当前版本；这不会撤销已发生的文件修改、配置变更、发送或其他现实副作用。${preview ? `\n\n起点：${preview}${preview.length >= 72 ? "…" : ""}` : ""}`,
			"从此处重新开始？",
			{
				type: "warning",
				confirmButtonText: "删除本轮及之后",
				cancelButtonText: "取消",
				distinguishCancelAndClose: true,
			},
		);
	} catch {
		return;
	}
	const preserveExistingDraft = Boolean(draft.value.trim());
	if (preserveExistingDraft) {
		try {
			await ElMessageBox.confirm(
				"当前输入框已有内容。继续删除会保留现有输入内容，不会把被删除的原消息覆盖到输入框。是否继续？",
				"输入框已有内容",
				{
					type: "warning",
					confirmButtonText: "保留现有内容并继续",
					cancelButtonText: "取消",
					distinguishCancelAndClose: true,
				},
			);
		} catch {
			return;
		}
	}
	if (!stillHere()) return;
	const draftRevisionAtDelete = draftEditRevision;
	deletingTurnUuid.value = turnUuid;
	try {
		const result = await Api.deleteConversationTurnSuffix(conversationUuid, turnUuid);
		if (!stillHere()) { emit("conversations-refresh"); return; }
		// DELETE committed: remove only server-confirmed roots immediately. Keep
		// surviving turns visible even when the fresh HTTP request is still pending.
		applyCommittedTurnDeletion(conversationUuid, result?.deletedRootTurns);
		await load({scrollMode: "bottom", replaceOperations: true});
		emit("conversations-refresh");
		if (!stillHere()) return;
		let restoredOriginalText = false;
		if (!preserveExistingDraft && originalUserText && !draft.value.trim() && draftRevisionAtDelete === draftEditRevision) {
			draft.value = originalUserText;
			setDraftForConversation(conversationUuid, originalUserText);
			adjustComposerHeight();
			await focusComposer();
			restoredOriginalText = true;
		}
		const deletedTurns = Array.isArray(result?.deletedRootTurns) ? result.deletedRootTurns.length : affectedTurns;
		ElMessage.success(restoredOriginalText
			? `已删除 ${deletedTurns} 轮内容，原消息已放回输入框`
			: `已删除 ${deletedTurns} 轮内容，可以从这里继续`);
		if (originalAttachments.length) ElMessage.warning("原消息包含附件，附件需要重新上传");
	} catch (error) {
		ElMessage.error(apiError(error));
	} finally {
		deletingTurnUuid.value = "";
	}
}



async function send() {
	// Enter and click must share the same single-flight guard. A model-source save
	// must commit before WebSocket send refreshes and freezes the next run model.
	if (sendPending.value || outboundSends.current || modelMutating.value || attachmentRestoring.value) return;
	const text = draft.value.trim();
	const referenceOrder = composer.value?.getReferenceOrder?.() || [];
	if (!text && !pendingAttachments.value.length) return;
	if (referencesInText(text).length && (!referenceCatalog.ready || !referenceCatalog.connected)) {
		ElMessage.warning("引用目录尚未连接，请稍后发送；草稿已保留");
		return;
	}
	if (running.value && pendingAttachments.value.length) {
		ElMessage.warning("运行中暂不追加附件，可以先停止或等当前轮完成");
		return;
	}
	const wasRunning = running.value;
	const requestId = globalThis.crypto?.randomUUID?.() || `send-${Date.now()}-${Math.random().toString(16).slice(2)}`;
	const attachments = [...pendingAttachments.value];
	const files = attachments.map((item) => item.file);
	const sentPreviewUrls = Object.values(attachmentPreviews.value).filter(Boolean);
	const optimisticAttachments = localAttachmentPayload(attachments);
	const finalText = text || (files.length ? "请根据我发送的附件内容回答。" : "");
	const pending = outboundSends.begin({
		requestId,
		conversationUuid: activeConversationUuid.value,
		draftText: draft.value,
		attachments,
		previewUrls: sentPreviewUrls,
		wasRunning,
		optimisticId: "",
	});
	if (!pending) return;
	const generation = ++sendAttemptGeneration;
	const isCurrent = () => componentMounted && outboundSends.isCurrent(pending) && generation === sendAttemptGeneration;
	sendPending.value = true;
	// The editor can clear immediately, but preparation is still safely unsent.
	setDraftForConversation(pending.conversationUuid, pending.draftText);
	draft.value = "";
	adjustComposerHeight();
	closeComposerMenus();
	if (!wasRunning) {
		pending.optimisticId = `local-${Date.now()}`;
		messages.value.push({
			id: pending.optimisticId,
			role: "user",
			content: finalText,
			attachments: optimisticAttachments,
			createdAt: Math.floor(Date.now() / 1000)
		});
		noteVisibleOutput(messages.value, {force: true});
		lastStats.value = null;
		running.value = true;
		foregroundRunning.value = true;
		rootTurnRunning.value = false;
		clearActiveRun();
		runStartedAt.value = Date.now();
	}
	lockAutoScroll();
	status.value = wasRunning ? "插话排队中" : "提交中";
	try {
		await scrollBottom({force: true, isCurrent});
		if (!isCurrent()) return;
		const conversationUuid = await ensureServerConversationForSend(finalText, pending);
		if (!isCurrent()) return;
		setDraftForConversation(conversationUuid, restoreOutboundDraft(pending.draftText, draft.value));
		let uploadedFiles = [];
		if (files.length) {
			pending.uploadController = new AbortController();
			outboundSends.markUploading(pending);
			status.value = "上传附件中";
			uploadedFiles = await Api.uploadConversationFiles(conversationUuid, files, {
				signal: pending.uploadController.signal,
				onProgress: ({loaded, total, fileIndex, fileCount}) => {
					if (!isCurrent()) return;
					const percent = total ? Math.min(100, Math.floor(loaded * 100 / total)) : 100;
					status.value = `上传附件 ${fileIndex + 1}/${fileCount} · ${percent}%${loaded >= total ? " · 保存中" : ""}`;
				},
			});
			if (!isCurrent()) return;
			outboundSends.markPrepared(pending);
			status.value = "提交中";
		}
		if (!isCurrent()) return;
		const sock = await ensureResponsiveWs(conversationUuid, isCurrent);
		if (!isCurrent()) return;
		pending.storageReleased = true;
		if (attachments.length) {
			// Upload/preparation is definitely unsent: retain its reloadable files.
			// Fence storage just before send, not at ACK (which may never arrive).
			const cleared = await attachmentDrafts.removeItems(conversationUuid, new Set(attachments.map(item => item.id)));
			if (!isCurrent()) return;
			if (!cleared) throw new Error("attachment_draft_clear_failed");
		}
		setDraftForConversation(conversationUuid, draft.value);
		sock.send(JSON.stringify({type: "send", requestId, text, files: uploadedFiles, referenceOrder}));
		outboundSends.markSent(pending);
		emit("conversations-refresh");
	} catch (error) {
		if (!isCurrent()) return;
		localToServerTransitionUuid.value = "";
		restorePendingOutboundSend(requestId, "send_failed");
		ElMessage.error(apiError(error));
		await load({
			conversationUuid: pending.conversationUuid,
			scrollMode: "preserve",
			manageLoading: false,
			isCurrent: () => componentMounted && generation === sendAttemptGeneration && pending.conversationUuid === activeConversationUuid.value,
		});
	}
}

async function stop() {
	if (outboundSends.current?.phase === "uploading") {
		leavePendingSend();
		status.value = "上传已取消，草稿和附件已保留";
		return;
	}
	try {
		await ElMessageBox.confirm(
			"停止后会中断当前正在执行的模型请求和工具任务，已产生的对话与统计会尽量保留。确定要停止吗？",
			"确认停止当前任务",
			{
				confirmButtonText: "停止",
				cancelButtonText: "取消",
				type: "warning",
				confirmButtonClass: "el-button--danger",
			},
		);
		if (ws && ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({type: "stop"}));
			status.value = "已请求停止";
			ElMessage.success(status.value);
			return;
		}
		const data = activeConversationUuid.value ? await Api.conversationStop(activeConversationUuid.value) : {stoppedRun: false, stoppedTasks: false};
		status.value = data.stoppedRun || data.stoppedTasks ? "已请求停止" : "没有运行中的任务";
		ElMessage.success(status.value);
	} catch (error) {
		if (error === "cancel" || error === "close") return;
		ElMessage.error(apiError(error));
	}
}

async function newSession() {
	await loadOptions();
	applyDefaultLocalModel();
	emit("conversation-created", "local:new");
	await nextTick();
	await loadLocalRunDefaults("local:new");
	resetLocalConversationState("local:new");
	restoreDraftForConversation("local:new");
	resetTransientThinking();
	ElMessage.success(isLocalConversation.value ? "已聚焦未发送的新会话" : "已开启新会话");
	await focusComposer();
}

async function switchConversation(next, prev) {
	const generation = ++conversationSwitchGeneration;
	// The width baseline tracks the scroller element itself, not the
	// conversation, so it stays valid across a switch. Only the reading position
	// belongs to the conversation being left.
	readingAnchor = null;
	pendingLoadBottomScroll = null;
	runConfigInteractionGeneration += 1;
	if (prev) setDraftForConversation(prev, draft.value);
	resetAgentAutoOpenBoundary();
	const isLocalToServerSend = String(prev || "").startsWith("local:")
		&& next
		&& next === localToServerTransitionUuid.value
		&& hasOptimisticLocalTurn();
	if (isLocalToServerSend) {
		// Same conversation under a new id: the draft attachments already moved with
		// it in ensureServerConversationForSend and stay in the composer.
		clearDraftForConversation(prev);
		runConfigOverride.value = null;
		chatState.value = {...(chatState.value || {}), conversationUuid: next};
		agentAutoOpenBoundaryConversation = String(next || "");
		return;
	}
	// Park this conversation's files before the released send hands its own files
	// back, so both end up in the same place.
	if (attachmentsLoadedKey) stashAttachmentsForConversation(attachmentsLoadedKey);
	leavePendingSend();
	runConfigOverride.value = null;
	pinnedActiveTurnIndex = null;
	readingAnchor = null;
	activeTurnIndex.value = 0;
	closeWs();
	clearUiCaches();
	resetOperationStore(next);
	messages.value = [];
	runStartedAt.value = 0;
	lastStats.value = null;
	// Bind the draft immediately, before any HTTP await. A stale load must never
	// resurrect its files in the next conversation's composer.
	restoreDraftForConversation(next);
	await load({scrollMode: "bottom", refreshDefaults: String(next || "").startsWith("local:")});
	if (!componentMounted || generation !== conversationSwitchGeneration || props.conversationUuid !== next) return;
	if (isLocalConversation.value) await focusComposer();
}

watch(() => props.conversationUuid, async (next, prev) => {
	if (next === prev) return;
	await switchConversation(next, prev);
}, {immediate: false});

watch(() => props.folderId, (next, prev) => {
	if (next === prev || !isLocalConversation.value) return;
	leavePendingSend();
	localDefaultsFolderId = null;
	localFolderDefaults = {};
	localDefaultsOverrides = {};
	applyDefaultLocalModel();
	void refreshFolderRunDefaults();
});

watch(() => draft.value, (value) => {
	if (restoringDraft.value) return;
	persistComposerDraft(value);
});

watch(() => turns.value.length, async (length) => {
	if (pinnedActiveTurnIndex !== null && pinnedActiveTurnIndex >= Number(length || 0)) {
		pinnedActiveTurnIndex = null;
	}
	await nextTick();
	updateScrollerOverflow();
	scheduleActiveTurnFromScroll({force: true});
});

watch(messages, () => {
	maybeRevokeSentAttachmentPreviews();
});

let activityReadTracker = null;
function activityReadSnapshot() {
	const uuid = String(activeConversationUuid.value || "");
	const catalogItems = referenceCatalog.treeStatus?.activityItems;
	const raw = Array.isArray(catalogItems)
		? catalogItems.find(item => item.conversationUuid === uuid)
		: chatState.value?.conversation;
	const item = raw ? withActivityReadVersion(raw, referenceCatalog.activityReadVersions?.get(uuid)) : null;
	const el = scroller.value;
	return {
		item, conversationUuid: uuid, loadedConversationUuid: String(chatState.value?.conversationUuid || ""),
		visible: document.visibilityState === "visible"
			&& !(props.navigationObscured && window.matchMedia("(max-width: 760px)").matches),
		focused: document.hasFocus(),
		ready: componentMounted && !loading.value && !streamFlushPending && turns.value.length > 0 && !turns.value.at(-1)?.stats?.live,
		atLatest: Boolean(el && el.clientHeight > 0 && scrollerDistanceFromBottom(el) <= 12),
		running: running.value || sendPending.value,
		operation: operationsById.value.get(item?.activityResult?.opId),
	};
}
function scheduleActivityRead() { activityReadTracker?.schedule(); }
watch(() => [props.conversationUuid, props.navigationObscured, chatState.value, referenceCatalog.treeStatus, referenceCatalog.activityReadVersions,
	running.value, loading.value, messages.value, operationsById.value], scheduleActivityRead, {flush: "post"});

onMounted(async () => {
	componentMounted = true;
	void attachmentDrafts.prune();
	void restoreAttachmentsForConversation(props.conversationUuid);
	activityReadTracker = createActivityReadTracker({snapshot: activityReadSnapshot, send: items => Api.readConversationActivity(items), accepted: acceptActivityReadReceipt});
	window.addEventListener("focus", scheduleActivityRead);
	window.addEventListener("blur", scheduleActivityRead);
	window.addEventListener("resize", scheduleActivityRead);
	document.addEventListener("visibilitychange", scheduleActivityRead);
	window.addEventListener("openbear:console-refresh", handleExternalRefresh);
	window.addEventListener("openbear:folder-properties-changed", handleFolderPropertiesChanged);
	window.addEventListener("focus", checkConnectionOnResume);
	window.addEventListener("pageshow", checkConnectionOnResume);
	window.addEventListener("online", checkConnectionOnResume);
	document.addEventListener("visibilitychange", checkConnectionOnResume);
	// Preserve the baseline visible initialization order: model options are
	// applied before remote state, so no temporary model placeholder can render.
	await loadOptions();
	await load({scrollMode: "bottom", refreshDefaults: isLocalConversation.value});
	adjustComposerHeight();
	observeScrollerReflow();
	await nextTick();
	scheduleActiveTurnFromScroll({force: true});
});
onBeforeUnmount(() => {
	detachAttachmentHydrations();
	attachmentRestoring.value = false;
	conversationSwitchGeneration += 1;
	scrollerResizeObserver?.disconnect();
	scrollerResizeObserver = null;
	scrollerReflowFrame = 0;
	observedScrollerWidth = 0;
	readingAnchor = null;
	activityReadTracker?.dispose();
	window.removeEventListener("focus", scheduleActivityRead);
	window.removeEventListener("blur", scheduleActivityRead);
	window.removeEventListener("resize", scheduleActivityRead);
	document.removeEventListener("visibilitychange", scheduleActivityRead);
	leavePendingSend();
	pendingLoadBottomScroll = null;
	componentMounted = false;
	conversationStateRequests.invalidate();
	operationFrameBuffer.reset();
	toolDetailCache.reset("");
	terminalStateRefreshScheduler.dispose();
	window.removeEventListener("openbear:console-refresh", handleExternalRefresh);
	window.removeEventListener("openbear:folder-properties-changed", handleFolderPropertiesChanged);
	defaultsRequestSeq += 1;
	window.removeEventListener("focus", checkConnectionOnResume);
	window.removeEventListener("pageshow", checkConnectionOnResume);
	window.removeEventListener("online", checkConnectionOnResume);
	document.removeEventListener("visibilitychange", checkConnectionOnResume);
	closeWs();
	clearAttachments();
	releaseAllStashedAttachments();
	revokeSentAttachmentPreviewUrls();
});
</script>

<template>
	<section class="console-page h-full min-h-0 flex bg-ob-bg text-ob-text" :class="{'visibility-overlay-active': messageVisibility.selecting.value || messageVisibility.undoIds.value.length}" :style="{'--console-composer-height': `${composerHeight}px`}" @click="onConsoleClick">
		<div class="console-main min-w-0 flex flex-1 flex-col">
			<ConsoleHeader
				:title="conversationTitle"
				:title-identity="activeConversationUuid"
				:conversation-path="props.conversationPath"
				:running="running"
				:run-started-at="runStartedAt"
				:status="status"
				:context-display="contextDisplay"
				:tokens-text="totalTokensDisplay"
				:tokens-detail="totalTokensDetail"
				:duration-ms="totalDurationMs"
				:cost-text="totalCostDisplay"
			>
				<template #mobile-navigation><slot name="mobile-navigation"/></template>
				<template #mobile-actions>
					<MobileConversationTools
						:conversation-uuid="activeConversationUuid"
						:token-parts="totalTokenParts"
						:duration-text="totalDurationDisplay"
						:cost-text="totalCostDisplay"
						:turns="turns"
						:active-turn-index="activeTurnIndex"
						:auto-scroll-locked="autoScrollLocked"
						:hidden-count="hiddenMessageCount"
						@open-hidden="messageVisibility.managing.value = true"
						@open-memory="taskMemoryDrawer?.open()"
						@toggle-scroll-lock="toggleAutoScrollLock"
						@scroll-to-turn="scrollToTurnIndex"
					/>
				</template>
			</ConsoleHeader>

			<div class="console-workspace min-h-0 flex-1">
				<div class="conversation-column min-w-0">
					<div
						v-if="timelinePageInFlight"
						class="timeline-page-loading"
						role="status"
						aria-live="polite"
					>
						<el-icon class="timeline-page-loading-icon"><Loading/></el-icon>
						<span>正在加载更早内容…</span>
					</div>

			<div ref="scroller" class="min-h-0 flex-1 overflow-y-auto console-scroll"
			     @scroll.passive="handleScrollerScroll"
			     @wheel="handleScrollerWheel"
			     @touchstart.passive="handleScrollerTouchStart"
			     @touchmove.passive="handleScrollerTouchMove"
			     @touchend.passive="handleScrollerTouchEnd"
			     @touchcancel.passive="handleScrollerTouchEnd"
			     @pointerdown="handleScrollerPointerDown">
				<div v-if="loading && !messages.length" class="grid h-full place-items-center text-sm text-ob-subtle">
					正在读取会话…
				</div>
				
				<div v-else-if="!turns.length"
				     class="mx-auto flex h-full max-w-3xl flex-col items-center justify-center px-6 text-center">
					<div class="empty-mark">
						<ChatLineRound/>
					</div>
					<h2 class="mt-5 text-2xl font-semibold tracking-tight">今天想让 OpenBear 做什么？</h2>
					<p class="mt-2 max-w-lg text-sm leading-6 text-ob-subtle">浏览器负责长对话、富文本和过程可视化；Telegram
						继续保留熟悉的模型、思考、工具和本轮统计交互语义。</p>
					<div class="mt-5 grid w-full max-w-2xl gap-2 sm:grid-cols-3">
						<button v-for="prompt in quickPrompts" :key="prompt" type="button" class="quick-prompt"
						        @click="applyQuickPrompt(prompt)">{{ prompt }}
						</button>
					</div>
				</div>
				
				<div v-else class="conversation-timeline-shell mx-auto py-5">
					<TurnList
						:turns="turns"
						:conversation-uuid="activeConversationUuid"
						:running="running"
						:deleting-turn-uuid="deletingTurnUuid"
						:auto-scroll-locked="autoScrollLocked"
						:retry-action-pending="retryActionPending"
						:detail-key="detailKey"
						:is-detail-open="isDetailOpen"
						:active-tool-result-index="activeToolResultIndex"
						@details-toggle="onDetailsToggle"
						@reasoning-toggle="onReasoningDetailsToggle"
						@select-tool-result="selectToolResult"
						@cancel-retry="controlActiveRetry($event, 'cancel')"
						@retry-now="controlActiveRetry($event, 'retry')"
						@delete-suffix="deleteTurnSuffix"
					/>
				</div>
			</div>
			
			<MessageVisibilityBar/>
			<aside class="console-controls" aria-label="会话工具与导航">
				<el-tooltip v-if="!isLocalConversation" :content="hiddenMessageCount ? `隐藏内容 · ${hiddenMessageCount} 条` : '隐藏内容'" placement="left" :show-after="260">
					<button type="button" class="hidden-content-toggle" :class="{populated: hiddenMessageCount > 0}" :aria-label="`管理隐藏内容，${hiddenMessageCount} 条`" @click="messageVisibility.managing.value = true">
						<Hide/><span v-if="hiddenMessageCount" class="hidden-content-count">{{ hiddenMessageCount > 99 ? '99+' : hiddenMessageCount }}</span>
					</button>
				</el-tooltip>
				<TaskMemoryDrawer ref="taskMemoryDrawer" :conversation-uuid="activeConversationUuid"/>
			<TurnMinimap
				:turns="turns"
				:active-turn-index="activeTurnIndex"
				:running="running"
				@scroll-to-turn="scrollToTurnIndex"
			/>
			<el-tooltip
				v-if="scrollerOverflow"
				:content="autoScrollLocked ? '滚动已锁定到底部，点击解锁' : '滚动未锁定，点击锁定到底部'"
				placement="left"
				:show-after="260"
			>
				<button
					type="button"
					class="scroll-lock-toggle"
					:class="{ locked: autoScrollLocked }"
					:aria-label="autoScrollLocked ? '滚动已锁定到底部，点击解锁' : '滚动未锁定，点击锁定到底部'"
					@click.stop="toggleAutoScrollLock"
				>
					<Lock v-if="autoScrollLocked"/>
					<Unlock v-else/>
				</button>
			</el-tooltip>
			</aside>
			
			<ConsoleComposer
				ref="composer"
				v-model:draft="draft"
				:conversation-uuid="activeConversationUuid"
				v-model:model-query="modelQuery"
				:pending-attachments="pendingAttachments"
				:attachment-previews="attachmentPreviews"
				:pending-confirmations="pendingConfirmations"
				:confirmation-submitting="confirmationSubmitting"
				:confirmation-errors="confirmationErrors"
				:pending-steering="pendingSteering"
				:model-menu-open="modelMenuOpen"
				:model-groups="modelGroups"
				:current-model="currentModel"
				:current-model-info="currentModelInfo"
				:current-think-levels="currentThinkLevels"
				:effective-thinking="effectiveThinking"
				:supports-thinking="supportsThinking"
				:current-fast="currentFast"
				:fast-supported="fastSupported"
				:agent-model="agentModel"
				:agent-think-level="agentThinkLevel"
				:agent-fast-mode="agentFastMode"
				:agent-effective-model="agentEffectiveModel"
				:agent-effective-thinking="agentEffectiveThinking"
				:agent-effective-fast="agentEffectiveFast"
				:agent-think-levels="agentThinkLevels"
				:agent-supports-thinking="agentSupportsThinking"
				:agent-fast-supported="agentFastSupported"
				:agent-default-thinking-label="agentDefaultThinkingLabel"
				:running="running"
				:can-send="canSend"
				:context-strategy="contextStrategy"
				:strategy-saving="strategySaving"
				:can-compact="canCompact"
				:compacting="compacting"
				:context-display="contextDisplay"
				:context-usage="contextUsage"
				:context-window-tokens="contextWindow"
				:context-threshold-tokens="rolloverTriggerTokens"
				:context-used-display="contextUsedDisplay"
				:context-threshold-display="contextThresholdDisplay"
				:context-window-display="contextWindowDisplay"
				:context-percent-display="contextPercentDisplay"
				:cost-text="totalCostDisplay"
				@attachment-change="$event.forEach(addAttachment)"
				@remove-attachment="removeAttachment"
				@clear-draft="clearDraftAndAttachments"
				@new-session="newSession"
				@toggle-model-menu="toggleModelMenu"
				@select-model="selectModel"
				@select-context-strategy="selectContextStrategy"
				@compact="compactContext"
				@select-thinking="selectThinking"
				@toggle-fast-mode="toggleFastMode"
				@select-agent-model="selectAgentModel"
				@select-agent-thinking="selectAgentThinking"
				@select-agent-fast="selectAgentFast"
				@send="send"
				@stop="stop"
				@answer-confirmation="answerPendingConfirmation"
				@close-menus="closeComposerMenus"
				@height-change="onComposerHeightChange"
			/>
				</div>
			</div>
		</div>
		<MessageVisibilityMobileMenu/>
		<HiddenMessagesDrawer :conversation-uuid="activeConversationUuid"/>
	</section>
</template>

<style scoped>
.hidden-content-toggle {
	position: fixed; right: var(--console-float-rail-right); top: calc(var(--console-float-rail-top) - var(--console-float-control-size) - var(--console-float-control-gap));
	z-index: 32; display: grid; place-items: center; width: var(--console-float-control-size); height: var(--console-float-control-size);
	border: 1px solid transparent; border-radius: 7px; background: var(--ob-chat-bg); color: var(--ob-chat-subtle);
	box-shadow: none; cursor: pointer; transition: color .15s ease, border-color .15s ease, right .24s ease;
}
.hidden-content-toggle:hover, .hidden-content-toggle.populated { color: var(--ob-chat-text); border-color: var(--ob-chat-line); background: var(--ob-chat-selected); }
.hidden-content-toggle:focus-visible { outline: 2px solid var(--ob-chat-subtle); outline-offset: 3px; }
.hidden-content-toggle > svg { width: 17px; height: 17px; }
.hidden-content-count { position: absolute; right: -5px; top: -5px; display: grid; place-items: center; min-width: 16px; height: 16px; padding: 0 3px; border: 2px solid var(--el-bg-color); border-radius: 8px; background: var(--el-fill-color); color: var(--el-text-color-secondary); font-size: 9px; font-weight: 600; font-variant-numeric: tabular-nums; }
.console-page {
	--bear-accent: var(--ob-blue);
	--bear-accent-soft: var(--ob-blue-soft);
	--bear-ink: var(--ob-text);
	--bear-muted: var(--ob-text-subtle);
	--bear-paper: var(--ob-surface);
	--bear-line: var(--ob-border);
	--console-content-max-width: 56rem;
	--console-content-gutter: 1rem;
	--console-float-rail-right: 1.15rem;
	--console-float-rail-top: calc(48% - 3.25rem);
	--console-float-control-size: 30px;
	--console-float-control-gap: 8px;
	--console-float-minimap-top: calc(
		var(--console-float-rail-top)
		+ var(--console-float-control-size)
		+ var(--console-float-control-gap)
	);
	--console-float-rail-bottom: calc(var(--console-composer-height, 135px) + 50px);
	font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
}

.console-main {
	position: relative;
	background: var(--ob-chat-bg);
}

.console-workspace {
	position: relative;
	display: flex;
	min-width: 0;
	overflow: hidden;
}

.console-controls { display: contents; }

.conversation-column {
	position: relative;
	display: flex;
	min-height: 0;
	flex: 1 1 auto;
	flex-direction: column;
	background: var(--ob-chat-bg);
	transition: width .24s cubic-bezier(.22, 1, .36, 1);
}

.console-scroll {
	background: var(--ob-chat-bg);
	overflow-x: hidden;
}

.timeline-page-loading {
	position: absolute;
	top: .75rem;
	left: 50%;
	z-index: 20;
	display: flex;
	align-items: center;
	gap: .45rem;
	transform: translateX(-50%);
	pointer-events: none;
	border: 1px solid rgb(var(--ob-blue-rgb) / 0.16);
	border-radius: 999px;
	background: var(--ob-surface-raised);
	padding: .48rem .78rem;
	color: var(--ob-text);
	font-size: 12px;
	line-height: 1;
	white-space: nowrap;
	box-shadow: var(--ob-shadow-panel);
	backdrop-filter: blur(10px);
}

.timeline-page-loading-icon {
	color: var(--ob-blue);
	animation: timeline-page-loading-spin .9s linear infinite;
}

@keyframes timeline-page-loading-spin {
	to { transform: rotate(360deg); }
}

.conversation-timeline-shell {
	width: min(var(--console-content-max-width), calc(100% - var(--console-content-gutter) - var(--console-content-gutter)));
	max-width: var(--console-content-max-width);
	overflow: visible;
}

.scroll-lock-toggle {
	position: fixed;
	right: var(--console-float-rail-right);
	bottom: var(--console-float-rail-bottom);
	z-index: 18;
	display: grid;
	width: var(--console-float-control-size);
	height: var(--console-float-control-size);
	place-items: center;
	border: 1px solid transparent;
	border-radius: 7px;
	background: var(--ob-chat-bg);
	color: var(--ob-chat-subtle);
	box-shadow: none;
	transition: bottom .18s ease, transform .16s ease, border-color .16s ease, color .16s ease, background .16s ease;
}

.scroll-lock-toggle:hover {
	transform: translateY(-1px);
	border-color: var(--ob-chat-line);
	background: var(--ob-chat-hover);
	color: var(--ob-chat-text);
}

.scroll-lock-toggle.locked {
	background: var(--ob-chat-selected);
	border-color: var(--ob-chat-line);
	color: var(--ob-chat-text);
}

.scroll-lock-toggle svg {
	width: 1.02rem;
	height: 1.02rem;
}

.quick-prompt {
	border: 1px solid var(--ob-border);
	border-radius: 1rem;
	background: rgb(var(--ob-surface-rgb) / 0.82);
	padding: .72rem .82rem;
	color: var(--ob-text);
	font-size: 12px;
	line-height: 1.55;
	text-align: left;
	box-shadow: var(--ob-shadow-panel);
	transition: transform .16s ease, border-color .16s ease;
}

.quick-prompt:hover {
	transform: translateY(-1px);
	border-color: rgb(var(--ob-blue-rgb) / 0.24);
	color: var(--ob-blue);
}

.empty-mark {
	display: grid;
	width: 3.3rem;
	height: 3.3rem;
	place-items: center;
	border: 1px solid var(--ob-border);
	border-radius: 1rem;
	background: linear-gradient(145deg, var(--ob-surface), var(--ob-surface-soft));
	color: var(--ob-text);
	box-shadow: var(--ob-shadow-panel);
}

.empty-mark svg {
	width: 1.45rem;
	height: 1.45rem;
}

@media (min-width: 761px) {
	/* A fixed narrow gutter keeps hover controls and selection off the text,
	   without rewrapping messages when selection mode starts. */
	.conversation-timeline-shell { width: min(var(--console-content-max-width), calc(100% - max(2rem, var(--console-content-gutter)) - max(2rem, var(--console-content-gutter)))); }
}

@media (max-width: 760px) {
	.console-page { --console-content-gutter: 1rem; }
	/* Selection adds scroll clearance; a scrollbar must not rewrap the text. */
	.console-scroll { scrollbar-gutter: stable; }
	.console-page.visibility-overlay-active .conversation-timeline-shell { padding-bottom: 96px; }
	/* Phone tools are available on demand in the header, never beside or over
	   the transcript. Keep the existing drawer mounted for its scoped state. */
	.console-controls { display: none; }
}
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.console-page { --console-float-control-size: 44px; }
}
</style>
