<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, provide, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {
	ChatLineRound,
	Loading,
	Lock,
	Unlock,
} from "@element-plus/icons-vue";
import ConsoleComposer from "./ConsoleComposer.vue";
import ConsoleHeader from "./ConsoleHeader.vue";
import TurnList from "./TurnList.vue";
import TurnMinimap from "./TurnMinimap.vue";
import TaskMemoryDrawer from "./TaskMemoryDrawer.vue";
import TurnWorkDetailPanel from "./TurnWorkDetailPanel.vue";
import WorkDetailIcon from "./WorkDetailIcon.vue";
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
import {Api, apiError, conversationWsUrl, filesToWsPayload} from "../../api.js";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {
	createTerminalStateRefreshScheduler,
	runGuardedConversationStateRefresh,
} from "./terminalStateRefresh.js";
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
	isContextCompactionOperation,
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

const DEFAULT_NEW_CONVERSATION_THINKING = "";
const DRAFT_STORAGE_KEY = "openbear.console.drafts.v1";
const AGENT_PANEL_INTENT_STORAGE_KEY = "openbear.console.agentPanelIntents.v1";
const FRAME_DEBUG_STORAGE_KEY = "openbear.debug.frames";
const STREAM_UI_FRAME_MS = 34;
const ACTIVE_TURN_SCROLL_UPDATE_MS = 140;
const INITIAL_TIMELINE_LIMIT = 200;
const LOAD_EARLIER_SCROLL_THRESHOLD = 180;
const props = defineProps({conversationUuid: {type: String, default: ""}});
const emit = defineEmits(["conversation-created", "conversations-refresh"]);

const loading = ref(false);
const running = ref(false);
const compactPending = ref(false);
const sendPending = ref(false);
const foregroundRunning = ref(false);
const rootTurnRunning = ref(false);
const activeRunTurnUuid = ref("");
const messages = ref([]);
const draft = ref("");
const status = ref("就绪");
const operationsById = ref(new Map());
const orderedOpIds = ref([]);
const revisionByOpId = ref(new Map());
const lastFrameSeq = ref(0);
const lastStats = ref(null);
const chatState = ref(null);
const scroller = ref(null);
const autoScrollLocked = ref(true);
const scrollerOverflow = ref(false);
const activeTurnIndex = ref(0);
const workDetailOpen = ref(false);
const workDetailTooltip = ref(null);
const workDetailTooltipSuppressed = ref(false);
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
const draftByConversation = ref(loadDraftStore());
const restoringDraft = ref(false);
const toolResultTabs = ref({});
const detailOpen = ref(loadAgentPanelIntents());
const pendingConfirmations = ref([]);
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
const retryCancelPending = ref(false);
const deletingTurnUuid = ref("");
let ws = null;
let wsConversationUuid = "";
let reconnectTimer = null;
let operationResyncTimer = null;
let operationResyncInFlight = false;
let timelinePageGeneration = 0;
let timelinePageRequestToken = 0;
let timelinePageConversationUuid = "";
let timelinePageInitialized = false;
let componentMounted = false;
let loadRequestGeneration = 0;
let scrollFrame = 0;
let activeTurnScrollFrame = 0;
let activeTurnScrollTimer = 0;
let workDetailTooltipReleaseTimer = 0;
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
let explicitUnlockAt = 0;
let visibleOutputSignature = "";
let lastVisibleOutputAt = 0;
const stateStatsByOpId = new Map();
const sentAttachmentPreviewUrls = new Set();
const localToServerTransitionUuid = ref("");
let defaultsRequestSeq = 0;
let appliedDefaultsRevision = 0;
let optionsLoadPromise = null;
let agentAutoOpenBoundaryConversation = "";
let agentAutoOpenPendingConversation = "";
let pendingOutboundSend = null;
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

async function cancelActiveRetry(event = {}) {
	const retry = event?.retry && typeof event.retry === "object" ? event.retry : event;
	if (!activeConversationUuid.value || !retry?.active || retryCancelPending.value) return;
	retryCancelPending.value = true;
	try {
		const data = await Api.conversationCancelRetry(activeConversationUuid.value, String(retry.taskUuid || ""));
		if (!data?.accepted) throw new Error("当前重试等待已结束");
		ElMessage.success("已请求取消模型重试；之前完成的进度会保留");
	} catch (error) {
		ElMessage.error(apiError(error));
	} finally {
		retryCancelPending.value = false;
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
const sessionShort = computed(() => {
	const uuid = chatState.value?.sessionUuid || "";
	return uuid ? uuid.slice(0, 8) : "新会话";
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
const serverCompacting = computed(() => Array.from(operationsById.value.values()).some((op) => isContextCompactionOperation(op) && ["active", "paused", "waiting_control"].includes(op?.lifecycle)));
const compacting = computed(() => compactPending.value || serverCompacting.value);
const canCompact = computed(() => !isLocalConversation.value && !running.value && !compacting.value && Boolean(contextUsage.value.authoritative) && Boolean(contextUsage.value.known) && Number(contextUsage.value.percent || 0) >= Number(contextUsage.value.manualMinPercent ?? 50));
const modelCallRows = computed(() => Array.isArray(chatState.value?.modelCalls) ? chatState.value.modelCalls : []);
const toolCallRows = computed(() => Array.isArray(chatState.value?.toolCalls) ? chatState.value.toolCalls : []);
const turns = computed(() => withTransientIdleThinking(attachTurnStats(buildTurns(displayMessages.value), modelCallRows.value, toolCallRows.value)));
const activeTurn = computed(() => turns.value[Math.min(Math.max(0, activeTurnIndex.value), Math.max(0, turns.value.length - 1))] || null);
const activeTurnWorking = computed(() => {
	const events = Array.isArray(activeTurn.value?.events) ? activeTurn.value.events : [];
	// Use the same projected persistent indicator that drives the three dots in the
	// conversation. This keeps both affordances on exactly the same lifecycle.
	return events.some((event) => event?.kind === "live_status" && event?.persistentRunIndicator);
});
const activeTurnHasWork = computed(() => {
	const events = Array.isArray(activeTurn.value?.events) ? activeTurn.value.events : [];
	return events.some((event) => {
		if (event?.kind === "answer") return Boolean(String(event?.message?.reasoning || "").trim());
		return !(event?.kind === "live_status" && event?.persistentRunIndicator && !event?.preview);
	});
});
const activeConversationUuid = computed(() => props.conversationUuid || chatState.value?.conversationUuid || "");
const isLocalConversation = computed(() => String(props.conversationUuid || "").startsWith("local:"));
const conversationTitle = computed(() => {
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
const currentModel = computed(() => (isLocalConversation.value && localModel.value) ? localModel.value : (chatState.value?.model || ""));
const currentThinking = computed(() => (isLocalConversation.value && localThinking.value) ? localThinking.value : (chatState.value?.thinkingLevel || ""));
const effectiveThinking = computed(() => chatState.value?.effectiveThinkingLevel || currentThinking.value || "off");
const currentFast = computed(() => (isLocalConversation.value ? localFast.value : Boolean(chatState.value?.fastMode || chatState.value?.effectiveFastMode)));
const currentModelInfo = computed(() => modelOptions.value.find((m) => m.key === currentModel.value) || null);
const currentThinkLevels = computed(() => {
	const stateLevels = Array.isArray(chatState.value?.thinkingLevels) ? chatState.value.thinkingLevels.filter(Boolean) : [];
	if (!isLocalConversation.value && stateLevels.length) return stateLevels;
	return Array.isArray(currentModelInfo.value?.thinkingLevels) ? currentModelInfo.value.thinkingLevels.filter(Boolean) : [];
});
const supportsThinking = computed(() => currentThinkLevels.value.length > 0);
const fastSupported = computed(() => Boolean(currentModelInfo.value?.supportsFast || chatState.value?.fastSupported));
const agentRunConfig = computed(() => chatState.value?.agentRunConfig || null);
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
const agentEffectiveThinking = computed(() => {
	if (agentThinkLevel.value) return agentThinkLevel.value;
	return String(agentEffective.value?.thinkLevel || agentEffective.value?.defaultThinkingLevel || "off");
});
const agentFastSupported = computed(() => Boolean(agentEffectiveModelInfo.value?.supportsFast || agentEffective.value?.fastSupported));
const agentEffectiveFast = computed(() => {
	if (agentFastMode.value === true) return agentFastSupported.value;
	if (agentFastMode.value === false) return false;
	return Boolean(agentEffective.value?.fastMode ?? (currentFast.value && agentFastSupported.value));
});
const contextWindow = computed(() => Number(currentModelInfo.value?.contextWindow || 0));
const compactTriggerTokens = computed(() => {
	const explicit = Number(currentModelInfo.value?.compactTriggerTokens || chatState.value?.compactTriggerTokens || 0);
	if (explicit > 0) return explicit;
	const ratio = Number(currentModelInfo.value?.compactRatio || chatState.value?.compactRatio || 0.7);
	return contextWindow.value ? Math.round(contextWindow.value * ratio) : 0;
});
const contextPercent = computed(() => compactTriggerTokens.value ? Math.min(999, lastContextTokens.value * 100 / compactTriggerTokens.value) : 0);
const contextUsedDisplay = computed(() => contextUsage.value.known ? fmtTokens(lastContextTokens.value) : "—");
const contextThresholdDisplay = computed(() => compactTriggerTokens.value ? fmtTokens(compactTriggerTokens.value) : "∞");
const contextWindowDisplay = computed(() => contextWindow.value ? fmtTokens(contextWindow.value) : "∞");
const contextPercentDisplay = computed(() => contextUsage.value.known && compactTriggerTokens.value ? `${contextPercent.value.toFixed(1)}%` : "—");
const contextDisplay = computed(() => {
	if (!compactTriggerTokens.value) return `${contextUsedDisplay.value} / ∞`;
	return `${contextUsedDisplay.value} / ${contextThresholdDisplay.value}（${contextPercentDisplay.value}）`;
});
const sessionLedgerUsage = computed(() => normalizeLedgerUsageBaseline(chatState.value?.usage || {}));
const totalTokenParts = computed(() => ledgerTokenParts(sessionLedgerUsage.value));
const totalTokensDisplay = computed(() => tokenLine(totalTokenParts.value));
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
	if (compacting.value || sendPending.value) return false;
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

function restoreDraftForConversation(uuid = props.conversationUuid) {
	restoringDraft.value = true;
	draft.value = String(draftByConversation.value[draftKey(uuid)] || "");
	nextTick(() => {
		restoringDraft.value = false;
		adjustComposerHeight();
	});
}

function clearDraftForConversation(uuid = props.conversationUuid) {
	setDraftForConversation(uuid, "");
}

function clearDraftAndAttachments() {
	clearDraftForConversation();
	clearAttachments();
}

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
		mainModel: localModel.value,
		mainThinkingLevel: localThinking.value || "off",
		mainFastMode: Boolean(localFast.value),
		agentModel: localAgentModel.value || "",
		agentThinkLevel: localAgentThinking.value || "",
		agentFastMode: localAgentFast.value === true || localAgentFast.value === false ? localAgentFast.value : null,
	};
}

async function loadLocalRunDefaults(uuid = props.conversationUuid) {
	const requestSeq = ++defaultsRequestSeq;
	const expectedUuid = String(uuid || "");
	try {
		const data = await Api.conversationDefaults();
		if (requestSeq !== defaultsRequestSeq || props.conversationUuid !== expectedUuid || !isLocalConversation.value) return false;
		const defaults = data?.defaults || {};
		appliedDefaultsRevision = Number(defaults.revision || 0);
		applyLocalRunDefaults(defaults);
		return true;
	} catch {
		// Defaults are an enhancement for unsent local:new only. Keep the existing
		// primary-model fallback and do not surface a blocking error.
		return false;
	}
}

async function patchLocalRunDefaults(patch) {
	defaultsRequestSeq += 1;
	const expectedUuid = String(props.conversationUuid || "");
	const data = await Api.updateConversationDefaults(patch);
	const defaults = data?.defaults || {};
	const revision = Number(defaults.revision || 0);
	if (props.conversationUuid !== expectedUuid || !isLocalConversation.value || revision < appliedDefaultsRevision) {
		return defaults || null;
	}
	appliedDefaultsRevision = revision;
	applyLocalRunDefaults(defaults);
	resetLocalConversationState(expectedUuid || "local:new");
	return defaults || null;
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
			compactTriggerTokens: Number(chatState.value.compactTriggerTokens || 0),
			manualMinPercent: Number(chatState.value.contextUsage?.manualMinPercent ?? 50),
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
	operationsById.value = new Map();
	orderedOpIds.value = [];
	revisionByOpId.value = new Map();
	stateStatsByOpId.clear();
	lastFrameSeq.value = 0;
	resetTimelinePagination(conversationUuid);
}

function clearUiCaches() {
	clearMarkdownCache();
	toolResultTabs.value = {};
	detailOpen.value = loadAgentPanelIntents();
	pendingSteering.value = [];
}

function resetLocalConversationState(uuid = props.conversationUuid || "local:new") {
	pinnedActiveTurnIndex = null;
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
		compactTriggerTokens: Number(model?.compactTriggerTokens || 0),
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

function normalizeConfirmations(list = []) {
	return Array.isArray(list) ? list.filter((item) => item?.confirmationId) : [];
}

function updatePendingConfirmations(list = []) {
	pendingConfirmations.value = normalizeConfirmations(list);
}

async function answerPendingConfirmation(item, answer = {}) {
	const confirmationId = item?.confirmationId;
	const conversationUuid = activeConversationUuid.value;
	if (!confirmationId || !conversationUuid) return;
	const questionnaire = String(item?.action || "") === "questionnaire";
	if (questionnaire && confirmationSubmitting.value[confirmationId]) return;
	if (questionnaire) {
		confirmationSubmitting.value = {...confirmationSubmitting.value, [confirmationId]: true};
		const nextErrors = {...confirmationErrors.value};
		delete nextErrors[confirmationId];
		confirmationErrors.value = nextErrors;
	}
	try {
		await Api.answerConversationConfirmation(conversationUuid, confirmationId, answer);
		pendingConfirmations.value = pendingConfirmations.value.filter((x) => x.confirmationId !== confirmationId);
	} catch (error) {
		const statusCode = Number(error?.response?.status || 0);
		const errorCode = String(error?.response?.data?.error || error?.response?.data?.code || error?.response?.data?.message || "");
		if (questionnaire && statusCode === 404 && errorCode === "confirmation_not_found") {
			pendingConfirmations.value = pendingConfirmations.value.filter((x) => x.confirmationId !== confirmationId);
			await load({conversationUuid, scrollMode: "preserve", manageLoading: false});
			return;
		}
		const message = apiError(error);
		if (questionnaire) {
			confirmationErrors.value = {
				...confirmationErrors.value,
				[confirmationId]: statusCode === 400
					? `回答未能提交：${message || "请检查必填项后重试。"}`
					: `提交失败：${message || "请稍后重试。"}`,
			};
		} else {
			ElMessage.error(message);
		}
	} finally {
		if (questionnaire) {
			const nextSubmitting = {...confirmationSubmitting.value};
			delete nextSubmitting[confirmationId];
			confirmationSubmitting.value = nextSubmitting;
		}
	}
}

function addAttachment(file) {
	const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
	const item = {id, file};
	pendingAttachments.value.push(item);
	if (file.type?.startsWith("image/")) attachmentPreviews.value[id] = URL.createObjectURL(file);
}

async function removeAttachment(id) {
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
	const url = attachmentPreviews.value[id];
	if (url) URL.revokeObjectURL(url);
	const next = {...attachmentPreviews.value};
	delete next[id];
	attachmentPreviews.value = next;
	pendingAttachments.value = pendingAttachments.value.filter((entry) => entry.id !== id);
}

function clearAttachments({revoke = true} = {}) {
	if (revoke) {
		for (const id of Object.keys(attachmentPreviews.value)) URL.revokeObjectURL(attachmentPreviews.value[id]);
	}
	attachmentPreviews.value = {};
	pendingAttachments.value = [];
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

async function toggleWorkDetailPanel() {
	workDetailTooltipSuppressed.value = true;
	workDetailTooltip.value?.hide?.();
	if (workDetailTooltipReleaseTimer) window.clearTimeout(workDetailTooltipReleaseTimer);
	// Let Element Plus remove the teleported Popper before the reference button
	// moves with the 240 ms work-detail layout transition.
	await nextTick();
	workDetailOpen.value = !workDetailOpen.value;
	workDetailTooltipReleaseTimer = window.setTimeout(() => {
		workDetailTooltipReleaseTimer = 0;
		workDetailTooltipSuppressed.value = false;
	}, 300);
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
						compactTriggerTokens: Number(model.compactTriggerTokens || 0),
						compactRatio: Number(model.compactRatio || 0.7),
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

async function selectModel(model) {
	const wasRunning = running.value;
	const switchNow = async () => {
		if (isLocalConversation.value) {
			await patchLocalRunDefaults({mainModel: model.key});
		} else if (activeConversationUuid.value) await Api.conversationSetModel(activeConversationUuid.value, model.key);
		else throw new Error("conversation_required");
		modelQuery.value = "";
		if (!isLocalConversation.value) await load({scrollMode: "preserve"});
		ElMessage.success(wasRunning ? `已保存模型：${model.key}，下一次调用生效` : `已切换模型：${model.key}`);
	};
	try {
		await switchNow();
	} catch (error) {
		const code = apiError(error);
		if (code === "cross_family_requires_new_session") {
			try {
				await ElMessageBox.confirm("这个模型和当前会话历史不是同一 family。要开启新会话后再切换吗？", "需要新会话", {
					type: "warning",
					confirmButtonText: "新会话切换",
					cancelButtonText: "取消",
				});
				const created = await Api.createConversation({title: "新会话", model: model.key});
				const uuid = created.conversation?.conversationUuid || created.state?.conversationUuid || "";
				if (uuid) {
					const nextThinking = modelDefaultThinking(model);
					if (nextThinking) await Api.conversationSetThinking(uuid, nextThinking);
					emit("conversation-created", uuid);
				}
				await load({scrollMode: "bottom"});
				window.dispatchEvent(new CustomEvent("openbear:conversations-refresh"));
			} catch (inner) {
				if (inner === "cancel" || inner === "close") return;
				ElMessage.error(apiError(inner));
			}
			return;
		}
		if (code === "run_is_active") ElMessage.warning("当前有运行中的任务，结束后再切换模型");
		else ElMessage.error(code);
	}
}

async function selectThinking(level) {
	const wasRunning = running.value;
	if (!supportsThinking.value || !currentThinkLevels.value.includes(level)) return;
	try {
		if (isLocalConversation.value) {
			await patchLocalRunDefaults({mainThinkingLevel: level});
		} else if (activeConversationUuid.value) {
			await Api.conversationSetThinking(activeConversationUuid.value, level);
		} else {
			throw new Error("conversation_required");
		}
		if (!isLocalConversation.value) await load({scrollMode: "preserve"});
		if (wasRunning) ElMessage.success("思考强度已保存，下一次调用生效");
	} catch (error) {
		ElMessage.error(apiError(error));
	}
}

async function toggleFastMode() {
	const wasRunning = running.value;
	if (!fastSupported.value) return;
	const next = !currentFast.value;
	try {
		if (isLocalConversation.value) {
			await patchLocalRunDefaults({mainFastMode: next});
		} else if (activeConversationUuid.value) await Api.conversationSetFast(activeConversationUuid.value, next);
		else throw new Error("conversation_required");
		if (!isLocalConversation.value) await load({scrollMode: "preserve"});
		ElMessage.success(wasRunning ? "Fast 模式已保存，下一次调用生效" : (next ? "Fast 模式已开启" : "Fast 模式已关闭"));
	} catch (error) {
		ElMessage.error(apiError(error));
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
	try {
		if (isLocalConversation.value) {
			const defaultsPatch = {};
			if (patch.model !== undefined) defaultsPatch.agentModel = patch.model || "";
			if (patch.thinkLevel !== undefined) defaultsPatch.agentThinkLevel = patch.thinkLevel || "";
			if (patch.fastMode !== undefined) defaultsPatch.agentFastMode = patch.fastMode;
			await patchLocalRunDefaults(defaultsPatch);
		} else if (activeConversationUuid.value) {
			await Api.conversationSetAgentRunConfig(activeConversationUuid.value, patch);
		} else {
			throw new Error("conversation_required");
		}
		if (!isLocalConversation.value) await load({scrollMode: "preserve"});
		ElMessage.success(wasRunning ? `${successText}，下一次新 Agent 生效` : successText);
	} catch (error) {
		ElMessage.error(apiError(error));
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
			stats: reconcileStatsWithAgentCards(turn, stats),
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

function projectOperationMessages(operations = orderedOperationsList()) {
	return projectOperationMessagesFromOperations(operations, operationProjectionOptions());
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
	if (!merge) lastFrameSeq.value = 0;
	return replaceOperationSnapshots(ops, {
		frameSeq: Number(state.frameSeq || state.facts?.latestFrameSeq || 0) || 0,
	});
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
	if (!props.conversationUuid || isLocalConversation.value || operationResyncInFlight) return;
	operationResyncInFlight = true;
	try {
		if (reason?.requiresFullState || reason?.resyncMode === "full_state") {
			debugFrames("frame-resync-full-state", {reason});
			await load({scrollMode: "preserve"});
			return;
		}
		let after = Number(reason?.afterFrameSeq ?? lastFrameSeq.value) || 0;
		for (let i = 0; i < 8; i += 1) {
			const data = await Api.conversationFrames(props.conversationUuid, after, 1000);
			const frames = Array.isArray(data?.frames) ? data.frames : [];
			if (!frames.length) break;
			for (const frame of frames) {
				// During gap recovery, replay even frames whose frameSeq is <= the current
				// high-water mark. The skipped WebSocket frame may have an older frameSeq
				// but the missing op revision needed to make the later frame applicable.
				applyOperationFrameMessage(frame, {resyncing: true});
			}
			const nextAfter = Number(data?.frameSeq || frames.at(-1)?.frameSeq || after) || after;
			if (nextAfter <= after) break;
			after = nextAfter;
			if (frames.length < 1000) break;
		}
	} catch (error) {
		console.warn("operation frame resync failed", reason, error);
		await load({scrollMode: "preserve"});
	} finally {
		operationResyncInFlight = false;
	}
}

function scheduleOperationStateResync(reason = {}) {
	if (!props.conversationUuid || isLocalConversation.value) return;
	if (operationResyncTimer) return;
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
		lastFrameSeq.value = Number(store.lastFrameSeq || 0) || lastFrameSeq.value;
		if (options?.resyncing) {
			void load({scrollMode: "preserve"});
		} else {
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
			appliedOperation?.opType === "context_compaction"
			&& appliedOperation?.lifecycle === "terminal"
			&& appliedOperation?.payload?.did === true
			&& chatState.value
		) {
			chatState.value = {
				...chatState.value,
				contextUsage: invalidateContextUsage(
					chatState.value.contextUsage,
					{compactTriggerTokens: Number(chatState.value.compactTriggerTokens || 0)},
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
	if (!options?.resyncing && isTerminalOperationFrame(frame)) {
		pendingTerminalFrame = {frame: operationDebugRow(appliedOperation), frameSeq: Number(frame.frameSeq || 0) || 0};
	}
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
	explicitUnlockAt = Date.now();
	autoScrollLocked.value = false;
	updateScrollerOverflow();
}

function toggleAutoScrollLock() {
	if (autoScrollLocked.value) unlockAutoScroll();
	else lockAutoScroll();
}

function captureScrollAnchor() {
	const el = scroller.value;
	if (!el) return null;
	const scrollerRect = el.getBoundingClientRect();
	const nodes = Array.from(el.querySelectorAll(".turn-block[data-turn-index]"));
	for (const node of nodes) {
		const rect = node.getBoundingClientRect();
		if (rect.bottom < scrollerRect.top) continue;
		if (rect.top > scrollerRect.bottom) break;
		return {
			index: Number(node.dataset.turnIndex || 0),
			offset: rect.top - scrollerRect.top,
			scrollTop: el.scrollTop,
		};
	}
	return {index: -1, offset: 0, scrollTop: el.scrollTop};
}

async function restoreScrollAnchor(anchor, options = {}) {
	if (!anchor || autoScrollLocked.value || (options?.isCurrent && !options.isCurrent())) return;
	await nextTick();
	if (options?.isCurrent && !options.isCurrent()) return;
	const el = scroller.value;
	if (!el) return;
	runProgrammaticScroll(() => {
		if (Number(anchor.index) >= 0) {
			const node = el.querySelector(`.turn-block[data-turn-index="${Number(anchor.index)}"]`);
			if (node) {
				const scrollerRect = el.getBoundingClientRect();
				const rect = node.getBoundingClientRect();
				el.scrollTop += (rect.top - scrollerRect.top) - Number(anchor.offset || 0);
				return;
			}
		}
		const maxScrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
		el.scrollTop = Math.min(maxScrollTop, Math.max(0, Number(anchor.scrollTop || 0)));
	}, 120);
	updateScrollerOverflow();
	scheduleActiveTurnFromScroll();
}

function updateActiveTurnFromScroll() {
	activeTurnScrollFrame = 0;
	lastActiveTurnScrollUpdateAt = performance.now();
	const el = scroller.value;
	if (!el) return;
	const nodes = Array.from(el.querySelectorAll(".turn-block[data-turn-index]"));
	if (!nodes.length) {
		pinnedActiveTurnIndex = null;
		activeTurnIndex.value = 0;
		return;
	}
	const scrollerRect = el.getBoundingClientRect();
	const rows = nodes.map((node) => {
		const rect = node.getBoundingClientRect();
		return {index: Number(node.dataset.turnIndex || 0), top: rect.top, bottom: rect.bottom};
	});
	activeTurnIndex.value = chooseActiveTurnIndex(rows, scrollerRect.top, scrollerRect.height, {
		atBottom: scrollerAtBottom(el),
		preferredIndex: pinnedActiveTurnIndex,
	});
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
	if (streamFlushFrame) window.cancelAnimationFrame(streamFlushFrame);
	if (streamFlushTimer) window.clearTimeout(streamFlushTimer);
	scrollFrame = 0;
	activeTurnScrollFrame = 0;
	activeTurnScrollTimer = 0;
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
	operationResyncInFlight = false;
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
	if (!pendingOutboundSend || String(requestId || "") !== pendingOutboundSend.requestId) return false;
	queueSentAttachmentPreviewRevokes(pendingOutboundSend.previewUrls || []);
	clearAttachments({revoke: false});
	pendingOutboundSend = null;
	sendPending.value = false;
	return true;
}

function restorePendingOutboundSend(requestId, error = "send_failed") {
	if (!pendingOutboundSend || String(requestId || "") !== pendingOutboundSend.requestId) return false;
	const pending = pendingOutboundSend;
	pendingOutboundSend = null;
	sendPending.value = false;
	if (pending.optimisticId) {
		messages.value = messages.value.filter((message) => String(message?.id || "") !== pending.optimisticId);
	}
	if (!draft.value.trim()) draft.value = pending.draftText;
	setDraftForConversation(activeConversationUuid.value, draft.value);
	adjustComposerHeight();
	if (!pending.wasRunning) {
		running.value = false;
		foregroundRunning.value = false;
		rootTurnRunning.value = false;
		clearActiveRun();
		runStartedAt.value = 0;
	}
	status.value = error === "busy" || error === "conversation_compacting" ? "会话正在压缩" : "发送失败";
	return true;
}

function handleWsMessage(raw, source = {}) {
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
	if (data.type === "bootstrap") {
		updatePendingConfirmations(data.pendingConfirmations || []);
		updatePendingSteering(data.pendingSteering || []);
		return;
	}
	if (data.type === "resync_required") {
		debugFrames("ws-resync-required", data);
		void load({
			scrollMode: "preserve",
			manageLoading: false,
			replaceOperations: Boolean(data.resetOperations),
		});
		return;
	}
	if (data.type === "state") {
		debugFrames("ws-state", {state: data.state});
		const state = data.state || {};
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
		const afterSignature = visibleEventSignatureForMessages(messages.value);
		noteVisibleOutput(messages.value, {force: true});
		if (autoScrollLocked.value && beforeSignature !== afterSignature) scrollBottom();
		else if (preserveAnchor) void restoreScrollAnchor(preserveAnchor);
		return;
	}
	if (data.type === "conversation_reset") {
		lastFrameSeq.value = 0;
		closeWs();
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
		updatePendingConfirmations(data.confirmations || []);
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
			ElMessage.warning("会话正在压缩，消息未发送，草稿已恢复");
		} else {
			ElMessage.error(error);
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
		status.value = running.value ? status.value : "已连接";
	};
	socket.onerror = () => {
		status.value = "连接异常";
	};
	socket.onclose = () => {
		if (ws !== socket || wsConversationUuid !== uuid) return;
		terminalStateRefreshScheduler.invalidate();
		if (!reconnectTimer) reconnectTimer = window.setTimeout(() => {
			reconnectTimer = null;
			void connectWs();
		}, 1200);
	};
	return socket;
}

async function waitWsOpen(sock) {
	if (!sock) throw new Error("ws_not_ready");
	if (sock.readyState === WebSocket.OPEN) return sock;
	await new Promise((resolve, reject) => {
		const t = window.setTimeout(() => reject(new Error("ws_connect_timeout")), 8000);
		const oldOpen = sock.onopen;
		const oldError = sock.onerror;
		sock.onopen = (ev) => {
			window.clearTimeout(t);
			oldOpen?.(ev);
			resolve();
		};
		sock.onerror = (ev) => {
			window.clearTimeout(t);
			oldError?.(ev);
			reject(new Error("ws_connect_failed"));
		};
	});
	return sock;
}

async function ensureServerConversationForSend(firstText) {
	if (!isLocalConversation.value) return activeConversationUuid.value;
	const title = String(firstText || "新会话").replace(/\s+/g, " ").trim().slice(0, 36) || "新会话";
	const created = await Api.createConversation({title, runConfig: completeLocalRunConfig()});
	const uuid = created.conversation?.conversationUuid || created.state?.conversationUuid || "";
	if (!uuid) throw new Error("conversation_create_failed");
	localToServerTransitionUuid.value = uuid;
	chatState.value = {...(chatState.value || {}), conversationUuid: uuid};
	emit("conversation-created", uuid);
	await nextTick();
	return uuid;
}

function applyLoadedConversationState(data, conversationUuid, {replaceOperations = false} = {}) {
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
}

async function load(options = {}) {
	const conversationUuid = String(options?.conversationUuid || props.conversationUuid || "").trim();
	if (!conversationUuid) return;
	const requestGeneration = ++loadRequestGeneration;
	const externalIsCurrent = typeof options?.isCurrent === "function" ? options.isCurrent : null;
	const isCurrent = () => Boolean(
		componentMounted
		&& requestGeneration === loadRequestGeneration
		&& conversationUuid === String(props.conversationUuid || "").trim()
		&& (!externalIsCurrent || externalIsCurrent())
	);
	const scrollMode = String(options?.scrollMode || "preserve");
	const lockOnBottom = options?.lock !== false;
	const preserveAnchor = scrollMode === "preserve" && !autoScrollLocked.value ? captureScrollAnchor() : null;
	if (conversationUuid.startsWith("local:")) {
		if (!modelOptions.value.length) await loadOptions();
		if (!isCurrent()) return;
		if (!localModel.value) applyDefaultLocalModel();
		if (options?.refreshDefaults) await loadLocalRunDefaults(conversationUuid);
		if (!isCurrent()) return;
		resetLocalConversationState(conversationUuid);
		restoreDraftForConversation(conversationUuid);
		resetTransientThinking();
		if (scrollMode === "bottom") {
			if (lockOnBottom) autoScrollLocked.value = true;
			await scrollBottom({force: true, cause: "load-local", isCurrent});
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
	const manageLoading = options?.manageLoading !== false;
	if (manageLoading && isCurrent()) loading.value = true;
	try {
		const outcome = await runGuardedConversationStateRefresh({
			conversationUuid,
			isCurrent,
			requestState: (uuid) => Api.conversationState(uuid, {timelineLimit: INITIAL_TIMELINE_LIMIT}),
			applyState: (data, uuid) => applyLoadedConversationState(data, uuid, {
				replaceOperations: Boolean(options?.replaceOperations),
			}),
			connectState: (uuid) => connectWs(uuid),
		});
		if (outcome.stage !== "complete" || !isCurrent()) return;
		if (scrollMode === "bottom") {
			if (lockOnBottom) autoScrollLocked.value = true;
			await scrollBottom({force: true, cause: "load", isCurrent});
		} else if (preserveAnchor) {
			await restoreScrollAnchor(preserveAnchor, {isCurrent});
		} else {
			await nextTick();
			if (!isCurrent()) return;
			updateScrollerOverflow();
			scheduleActiveTurnFromScroll({force: true});
		}
	} catch (error) {
		if (isCurrent()) ElMessage.error(apiError(error));
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
	const originalUserText = String(turn?.user?.content || "");
	const originalAttachments = Array.isArray(turn?.user?.attachments) ? turn.user.attachments : [];
	const preview = plainText(originalUserText).replace(/\s+/g, " ").trim().slice(0, 72);
	try {
		await ElMessageBox.confirm(
			`将永久删除这轮${affectedTurns > 1 ? `及后续 ${affectedTurns - 1} 轮` : ""}内容。相关回答、工具/Agent 过程、附件引用和模型摘要都会一起移除，之后可从删除点之前继续对话。${preview ? `\n\n起点：${preview}${preview.length >= 72 ? "…" : ""}` : ""}`,
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
	deletingTurnUuid.value = turnUuid;
	try {
		const result = await Api.deleteConversationTurnSuffix(conversationUuid, turnUuid);
		lastFrameSeq.value = 0;
		closeWs();
		toolDetailCache.reset(conversationUuid);
		await load({scrollMode: "bottom", replaceOperations: true});
		emit("conversations-refresh");
		let restoredOriginalText = false;
		if (!preserveExistingDraft && originalUserText && !draft.value.trim()) {
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

async function compactConversation() {
	const uuid = activeConversationUuid.value;
	if (!uuid || !canCompact.value || compactPending.value) return;
	try {
		await ElMessageBox.confirm("将调用压缩模型把现有历史整理为摘要。原始消息仍会保留，但后续模型将主要基于压缩摘要继续对话。", "压缩当前会话？", {type: "warning", confirmButtonText: "开始压缩", cancelButtonText: "取消"});
	} catch { return; }
	compactPending.value = true;
	try {
		const result = await Api.conversationCompact(uuid);
		if (result?.state) chatState.value = result.state;
		await load({scrollMode: "preserve"});
		ElMessage.success(result?.outcome?.did ? "上下文压缩完成" : "当前历史没有可压缩内容");
	} catch (error) { ElMessage.error(apiError(error)); }
	finally { compactPending.value = false; }
}

async function send() {
	if (compacting.value) { ElMessage.warning("上下文正在压缩，请稍候"); return; }
	const text = draft.value.trim();
	if (!text && !pendingAttachments.value.length) return;
	if (running.value && pendingAttachments.value.length) {
		ElMessage.warning("运行中暂不追加附件，可以先停止或等当前轮完成");
		return;
	}
	const wasRunning = running.value;
	const requestId = globalThis.crypto?.randomUUID?.() || `send-${Date.now()}-${Math.random().toString(16).slice(2)}`;
	const files = pendingAttachments.value.map((item) => item.file);
	const sentPreviewUrls = Object.values(attachmentPreviews.value).filter(Boolean);
	const optimisticAttachments = localAttachmentPayload();
	const finalText = text || (files.length ? "请根据我上传的附件回答。" : "");
	let optimisticId = "";
	pendingOutboundSend = {
		requestId,
		draftText: text,
		previewUrls: sentPreviewUrls,
		wasRunning,
		optimisticId,
	};
	sendPending.value = true;
	clearDraftForConversation();
	draft.value = "";
	adjustComposerHeight();
	closeComposerMenus();
	if (!wasRunning) {
		optimisticId = `local-${Date.now()}`;
		pendingOutboundSend.optimisticId = optimisticId;
		messages.value.push({
			id: optimisticId,
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
	await scrollBottom({force: true});
	try {
		const conversationUuid = await ensureServerConversationForSend(finalText);
		const sock = await waitWsOpen(await connectWs(conversationUuid));
		const wsFiles = files.length ? await filesToWsPayload(files) : [];
		sock.send(JSON.stringify({type: "send", requestId, text, files: wsFiles}));
		emit("conversations-refresh");
		window.dispatchEvent(new CustomEvent("openbear:conversations-refresh"));
	} catch (error) {
		localToServerTransitionUuid.value = "";
		restorePendingOutboundSend(requestId, "send_failed");
		ElMessage.error(apiError(error));
		await load({scrollMode: "preserve"});
	}
}

async function stop() {
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

watch(() => props.conversationUuid, async (next, prev) => {
	if (next === prev) return;
	if (prev) setDraftForConversation(prev, draft.value);
	resetAgentAutoOpenBoundary();
	const isLocalToServerSend = String(prev || "").startsWith("local:")
		&& next
		&& next === localToServerTransitionUuid.value
		&& hasOptimisticLocalTurn();
	if (isLocalToServerSend) {
		clearDraftForConversation(prev);
		chatState.value = {...(chatState.value || {}), conversationUuid: next};
		agentAutoOpenBoundaryConversation = String(next || "");
		return;
	}
	pinnedActiveTurnIndex = null;
	activeTurnIndex.value = 0;
	closeWs();
	clearUiCaches();
	resetOperationStore(next);
	messages.value = [];
	runStartedAt.value = 0;
	lastStats.value = null;
	await load({scrollMode: "bottom", refreshDefaults: String(next || "").startsWith("local:")});
	restoreDraftForConversation(next);
	if (isLocalConversation.value) await focusComposer();
}, {immediate: false});

watch(() => draft.value, (value) => {
	if (restoringDraft.value) return;
	setDraftForConversation(props.conversationUuid, value);
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

onMounted(async () => {
	componentMounted = true;
	window.addEventListener("openbear:console-refresh", handleExternalRefresh);
	// Preserve the baseline visible initialization order: model options are
	// applied before remote state, so no temporary model placeholder can render.
	await loadOptions();
	await load({scrollMode: "bottom", refreshDefaults: isLocalConversation.value});
	restoreDraftForConversation(props.conversationUuid);
	adjustComposerHeight();
	await nextTick();
	scheduleActiveTurnFromScroll({force: true});
});
onBeforeUnmount(() => {
	componentMounted = false;
	if (workDetailTooltipReleaseTimer) window.clearTimeout(workDetailTooltipReleaseTimer);
	toolDetailCache.reset("");
	terminalStateRefreshScheduler.dispose();
	window.removeEventListener("openbear:console-refresh", handleExternalRefresh);
	closeWs();
	clearAttachments();
	revokeSentAttachmentPreviewUrls();
});
</script>

<template>
	<section class="console-page h-full min-h-0 flex bg-white text-[#111827]" :class="{'work-detail-open': workDetailOpen}" :style="{'--console-composer-height': `${composerHeight}px`}" @click="onConsoleClick">
		<div class="console-main min-w-0 flex flex-1 flex-col">
			<ConsoleHeader
				:title="conversationTitle"
				:subtitle="`${chatState?.model || '—'} · ${thinkingLabel(effectiveThinking)} · ${sessionShort}`"
				:running="running"
				:run-started-at="runStartedAt"
				:status="status"
				:context-display="contextDisplay"
				:tokens-text="totalTokensDisplay"
				:duration-text="totalDurationDisplay"
				:cost-text="totalCostDisplay"
			/>

			<div class="console-workspace min-h-0 flex-1">
				<div class="conversation-column min-w-0">
					<el-tooltip
						v-if="activeTurnHasWork || workDetailOpen"
						ref="workDetailTooltip"
						:content="activeTurnWorking ? '当前轮次正在工作，点击查看详情' : (workDetailOpen ? '关闭当前轮次工作详情' : '查看当前轮次工作详情')"
						placement="left"
						:show-after="260"
						:disabled="workDetailTooltipSuppressed"
						:popper-style="workDetailTooltipSuppressed ? {display: 'none'} : undefined"
					>
						<button
							type="button"
							class="work-detail-toggle"
							:class="{ active: workDetailOpen, working: activeTurnWorking }"
							:aria-label="activeTurnWorking ? '当前轮次正在工作，点击查看详情' : (workDetailOpen ? '关闭当前轮次工作详情' : '查看当前轮次工作详情')"
							:aria-expanded="workDetailOpen ? 'true' : 'false'"
							@click.stop="toggleWorkDetailPanel"
						><WorkDetailIcon/></button>
					</el-tooltip>

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
				<div v-if="loading && !messages.length" class="grid h-full place-items-center text-sm text-[#6b7280]">
					正在读取会话…
				</div>
				
				<div v-else-if="!turns.length"
				     class="mx-auto flex h-full max-w-3xl flex-col items-center justify-center px-6 text-center">
					<div class="empty-mark">
						<ChatLineRound/>
					</div>
					<h2 class="mt-5 text-2xl font-semibold tracking-tight">今天想让 OpenBear 做什么？</h2>
					<p class="mt-2 max-w-lg text-sm leading-6 text-[#6b7280]">浏览器负责长对话、富文本和过程可视化；Telegram
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
						:retry-cancel-pending="retryCancelPending"
						:detail-key="detailKey"
						:is-detail-open="isDetailOpen"
						:active-tool-result-index="activeToolResultIndex"
						@details-toggle="onDetailsToggle"
						@reasoning-toggle="onReasoningDetailsToggle"
						@select-tool-result="selectToolResult"
						@cancel-retry="cancelActiveRetry"
						@delete-suffix="deleteTurnSuffix"
					/>
				</div>
			</div>
			
			<TurnMinimap
				:turns="turns"
				:active-turn-index="activeTurnIndex"
				:running="running"
				@scroll-to-turn="scrollToTurnIndex"
			/>
			<TaskMemoryDrawer :conversation-uuid="activeConversationUuid"/>
			
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
			
			<ConsoleComposer
				ref="composer"
				v-model:draft="draft"
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
				:can-compact="canCompact"
				:compacting="compacting"
				:context-display="contextDisplay"
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
				@select-thinking="selectThinking"
				@toggle-fast-mode="toggleFastMode"
				@select-agent-model="selectAgentModel"
				@select-agent-thinking="selectAgentThinking"
				@select-agent-fast="selectAgentFast"
				@send="send"
				@compact="compactConversation"
				@stop="stop"
				@answer-confirmation="answerPendingConfirmation"
				@close-menus="closeComposerMenus"
				@height-change="onComposerHeightChange"
			/>
				</div>
				<TurnWorkDetailPanel
					:open="workDetailOpen"
					:turn="activeTurn"
					:turn-index="activeTurnIndex"
					:conversation-uuid="activeConversationUuid"
					:auto-scroll-locked="autoScrollLocked"
					:retry-cancel-pending="retryCancelPending"
					:working="activeTurnWorking"
					:detail-key="detailKey"
					:is-detail-open="isDetailOpen"
					:active-tool-result-index="activeToolResultIndex"
					@close="workDetailOpen = false"
					@details-toggle="onDetailsToggle"
					@reasoning-toggle="onReasoningDetailsToggle"
					@select-tool-result="selectToolResult"
					@cancel-retry="cancelActiveRetry"
				/>
			</div>
		</div>
	</section>
</template>

<style scoped>
.console-page {
	--bear-accent: #2563eb;
	--bear-accent-soft: #eff6ff;
	--bear-ink: #18181b;
	--bear-muted: #71717a;
	--bear-paper: #ffffff;
	--bear-line: rgba(15, 23, 42, .10);
	--console-content-max-width: 56rem;
	--console-content-gutter: 1rem;
	--console-float-rail-right: 1.15rem;
	--console-float-rail-top: calc(48% - 3.25rem);
	--console-float-control-size: 2.15rem;
	--console-float-control-gap: .75rem;
	--console-float-minimap-top: calc(
		var(--console-float-rail-top)
		+ var(--console-float-control-size)
		+ var(--console-float-control-gap)
		+ var(--console-float-control-size)
		+ var(--console-float-control-gap)
	);
	--console-float-rail-bottom: calc(var(--console-composer-height, 135px) + 50px);
	font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
}

.console-main {
	position: relative;
	background: #fff;
}

.console-workspace {
	position: relative;
	display: flex;
	min-width: 0;
	overflow: hidden;
}

.conversation-column {
	position: relative;
	display: flex;
	min-height: 0;
	flex: 1 1 auto;
	flex-direction: column;
	background: #fff;
	transition: width .24s cubic-bezier(.22, 1, .36, 1);
}

.work-detail-toggle {
	position: absolute;
	top: .85rem;
	right: 1rem;
	z-index: 19;
	display: grid;
	width: 2.15rem;
	height: 2.15rem;
	place-items: center;
	border: 1px solid rgba(15, 23, 42, .1);
	border-radius: 999px;
	background: rgba(255, 255, 255, .92);
	color: #64748b;
	box-shadow: 0 12px 30px rgba(15, 23, 42, .12), inset 0 1px 0 rgba(255, 255, 255, .9);
	backdrop-filter: blur(10px);
	cursor: pointer;
	transition: transform .16s ease, border-color .16s ease, background .16s ease, color .16s ease;
}

.work-detail-toggle:hover {
	transform: translateY(-1px);
	border-color: rgba(37, 99, 235, .28);
	color: #2563eb;
}

.work-detail-toggle.active {
	border-color: rgba(37, 99, 235, .25);
	background: #eff6ff;
	color: #1d4ed8;
}

.work-detail-toggle.working {
	border-color: rgba(37, 99, 235, .2);
	background: rgba(239, 246, 255, .96);
	color: #2563eb;
}

.work-detail-toggle.working::before {
	content: "";
	position: absolute;
	inset: 2px;
	border: 2px solid transparent;
	border-top-color: #2563eb;
	border-right-color: rgba(37, 99, 235, .42);
	border-radius: inherit;
	animation: work-detail-button-spin .9s linear infinite;
}

.work-detail-toggle.working svg {
	animation: work-detail-icon-breathe 1.25s ease-in-out infinite;
}

@keyframes work-detail-button-spin {
	to { transform: rotate(360deg); }
}

@keyframes work-detail-icon-breathe {
	0%, 100% { opacity: .62; transform: scale(.94); }
	50% { opacity: 1; transform: scale(1.03); }
}

.work-detail-toggle svg {
	width: 1.08rem;
	height: 1.08rem;
}

.console-scroll {
	background: #fff;
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
	border: 1px solid rgba(37, 99, 235, .16);
	border-radius: 999px;
	background: rgba(255, 255, 255, .94);
	padding: .48rem .78rem;
	color: #475569;
	font-size: 12px;
	line-height: 1;
	white-space: nowrap;
	box-shadow: 0 12px 30px rgba(15, 23, 42, .12), inset 0 1px 0 rgba(255, 255, 255, .9);
	backdrop-filter: blur(10px);
}

.timeline-page-loading-icon {
	color: #2563eb;
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
	border: 1px solid rgba(15, 23, 42, .1);
	border-radius: 999px;
	background: rgba(255, 255, 255, .92);
	color: #64748b;
	box-shadow: 0 14px 34px rgba(15, 23, 42, .14), inset 0 1px 0 rgba(255, 255, 255, .92);
	backdrop-filter: blur(10px);
	transition: bottom .18s ease, transform .16s ease, border-color .16s ease, color .16s ease, background .16s ease;
}

.scroll-lock-toggle:hover {
	transform: translateY(-1px);
	border-color: rgba(37, 99, 235, .26);
	color: #2563eb;
}

.scroll-lock-toggle.locked {
	background: rgba(16, 185, 129, .12);
	border-color: rgba(16, 185, 129, .28);
	color: #047857;
}

.scroll-lock-toggle svg {
	width: 1.02rem;
	height: 1.02rem;
}

.quick-prompt {
	border: 1px solid rgba(15, 23, 42, .08);
	border-radius: 1rem;
	background: rgba(255, 255, 255, .82);
	padding: .72rem .82rem;
	color: #475569;
	font-size: 12px;
	line-height: 1.55;
	text-align: left;
	box-shadow: 0 12px 28px rgba(15, 23, 42, .06);
	transition: transform .16s ease, border-color .16s ease;
}

.quick-prompt:hover {
	transform: translateY(-1px);
	border-color: rgba(37, 99, 235, .24);
	color: #1e40af;
}

.empty-mark {
	display: grid;
	width: 3.3rem;
	height: 3.3rem;
	place-items: center;
	border: 1px solid #e5e7eb;
	border-radius: 1rem;
	background: linear-gradient(145deg, #ffffff, #f4f4f5);
	color: #334155;
	box-shadow: inset 0 1px 0 rgba(255, 255, 255, .9), 0 18px 48px rgba(15, 23, 42, .08);
}

.empty-mark svg {
	width: 1.45rem;
	height: 1.45rem;
}

@media (min-width: 761px) {
	.work-detail-toggle {
		position: fixed;
		top: calc(var(--console-float-rail-top) + var(--console-float-control-size) + var(--console-float-control-gap));
		right: var(--console-float-rail-right);
		z-index: 32;
	}
}

@media (min-width: 1281px) {
	.console-page.work-detail-open {
		--console-content-max-width: 52rem;
		--console-content-gutter: 2rem;
		--console-float-rail-right: calc(clamp(24rem, 32vw, 32rem) + 1rem);
	}
}

@media (max-width: 760px) {
	.console-page {
		--console-float-rail-right: .75rem;
		--console-float-control-size: 2.35rem;
		--console-float-control-gap: .75rem;
		--console-float-rail-bottom: calc(env(safe-area-inset-bottom, 0px) + var(--console-composer-height, 135px) + 50px);
	}
}
</style>
