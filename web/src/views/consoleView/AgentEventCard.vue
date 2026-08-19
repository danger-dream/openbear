<script setup>
import {computed, nextTick, ref, watch} from "vue";
import {ElMessage} from "element-plus";
import {ArrowRight, Cpu, Document, Loading, Operation} from "@element-plus/icons-vue";
import {Api, apiError} from "../../api.js";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import AgentPlanWorkspace from "./AgentPlanWorkspace.vue";
import AgentProcessActivity from "./AgentProcessActivity.vue";
import {agentEventIdentity, agentPanelDetailKey} from "./agentPanelState.js";
import {
	agentOutputDisplay,
	agentDisplayState,
	agentTasks,
	agentRowMetricChips,
	agentRowArgumentsDisplay,
	agentRowOutputSection,
	agentStatusMeta,
	fmtNum,
	recentEventMessage,
	toolPreview,
} from "./display.js";
import {
	agentCompactionActivityView,
	agentTaskPushSnapshot,
	buildAgentMonitorTimeline,
	buildAgentPlanView,
	initialAgentLaunch,
	isActiveAgentEvent,
	isActivityExcludedEventKind,
	isAgentMonitorEventKind,
	mergeAgentEventLines,
} from "./agentPlanPresentation.js";
import {toolArgumentsSummary} from "./toolArgumentsPresentation.js";

const props = defineProps({
	event: {type: Object, required: true},
	conversationUuid: {type: String, default: ""},
	turnId: {type: [String, Number], required: true},
	index: {type: Number, required: true},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	onDetailsToggle: {type: Function, required: true},
	previewOnly: {type: Boolean, default: false},
});

const workspaceLoading = ref(false);
const workspaceError = ref("");
const workspaceData = ref(null);
const workspaceTaskUuid = ref("");
const workspaceDirty = ref(false);
const panelTab = ref("plan");
const activityScroller = ref(null);
const activityLines = ref([]);
const activityLoading = ref(false);
const activityLoaded = ref(false);
const activityError = ref("");
const activityHasMore = ref(false);
const activityBeforeSeq = ref(0);
const activityTotal = ref(0);
const monitorTotal = ref(0);
const eventCountsTaskUuid = ref("");
const eventScrollState = {
	activity: {top: 0, interacted: false},
	monitor: {top: 0, interacted: false},
};
let activityLoadPromise = null;
let eventCountsPromise = null;
let planRecentActivityPromise = null;
let workspaceReloadQueued = false;
let workspaceLoadingTaskUuid = "";

const legacyDetailId = computed(() => props.detailKey(props.turnId, "agent", props.index));
const detailId = computed(() => agentPanelDetailKey(props.conversationUuid, agentEventIdentity(props.event)) || legacyDetailId.value);
const isOpen = computed(() => !props.previewOnly && props.isDetailOpen(detailId.value));

function liveLines() {
	return Array.isArray(props.event?.lines) ? props.event.lines : [];
}

const agentState = computed(() => {
	const state = agentDisplayState(props.event);
	const summary = state.summary;
	const rows = state.rows.map((row) => ({
		...row,
		statusMeta: agentStatusMeta(row.status, summary.toolName),
		metricChips: agentRowMetricChips(row),
	}));
	return {...state, rows, subtitle: summary.preview || toolPreview(props.event) || summary.countText};
});

// Persisted Agent operations are projected as `kind: "tool"`; live_agent is
// only the transient fallback.  Activity/monitor polling must follow task
// state, not the projection kind, or a visible running card never refreshes.
const isActiveLiveAgent = computed(() => isActiveAgentEvent(props.event, agentState.value));
const isSingleAgentSummary = computed(() => (
	agentState.value.rows.length === 1
	&& agentState.value.summary.toolName === "Agent"
));
const panelTitle = computed(() => isActiveLiveAgent.value ? "Agent 运行中" : agentState.value.summary.title);
const panelSubtitle = computed(() => isActiveLiveAgent.value
	? "正在等待子 Agent 返回结果"
	: `${agentState.value.subtitle} · ${agentState.value.summary.label}`);
const summaryTitle = computed(() => isSingleAgentSummary.value ? "Agent" : panelTitle.value);
const summarySubtitle = computed(() => isSingleAgentSummary.value
	? `· ${agentState.value.subtitle} · ${isActiveLiveAgent.value ? "运行中" : agentState.value.summary.label}`
	: panelSubtitle.value);
const panelStatusLabel = computed(() => isActiveLiveAgent.value ? "执行中" : agentState.value.summary.label);
const panelStatusClass = computed(() => isActiveLiveAgent.value ? "running" : agentState.value.summary.cls);

function rowOutputAvailable(row) {
	return Boolean(row?.hasOutput && !["running", "resuming", "queued", "pausing", "stopping"].includes(String(row?.status || "")));
}

const primaryRowIndex = computed(() => agentState.value.rows.findIndex((row) => row?.hasArguments || rowOutputAvailable(row)));
const primaryActionRow = computed(() => primaryRowIndex.value >= 0 ? agentState.value.rows[primaryRowIndex.value] : null);
const primaryOutputSection = computed(() => primaryActionRow.value ? agentRowOutputSection(primaryActionRow.value) : null);
const fallbackArguments = computed(() => primaryActionRow.value
	? agentRowArgumentsDisplay(props.event, primaryActionRow.value, primaryRowIndex.value)
	: "");

const taskUuid = computed(() => {
	const row = agentState.value.rows.find((item) => item?.taskUuid || item?.task_uuid || item?.task?.taskUuid || item?.task?.task_uuid);
	return String(
		row?.taskUuid || row?.task_uuid || row?.task?.taskUuid || row?.task?.task_uuid
		|| props.event?.taskUuid || props.event?.task_uuid
		|| props.event?.result?.taskUuid || props.event?.result?.task_uuid
		|| ""
	).trim();
});
const canLoadPlan = computed(() => Boolean(taskUuid.value && props.conversationUuid && !props.conversationUuid.startsWith("local:")));
const taskPlanMode = computed(() => {
	const tasks = agentTasks(props.event);
	const task = tasks.find((item) => String(item?.taskUuid || item?.task_uuid || "").trim() === taskUuid.value)
		|| (tasks.length === 1 ? tasks[0] : null);
	return String(task?.planMode || task?.plan_mode || task?.input?.planMode || task?.input?.plan_mode || "").trim().toLowerCase();
});
// direct tasks are definitively non-Plan. Managed and legacy tasks retain the
// historical Plan tab until this task's workspace snapshot resolves.
const planCapability = computed(() => taskPlanMode.value !== "direct");
const planResolved = computed(() => Boolean(
	taskUuid.value
	&& workspaceTaskUuid.value === taskUuid.value
	&& workspaceData.value
	&& typeof workspaceData.value === "object"
	&& !Array.isArray(workspaceData.value)
));
const hasPlan = computed(() => Boolean(planResolved.value && buildAgentPlanView(workspaceData.value).hasPlan));
const showPlanTab = computed(() => Boolean(
	canLoadPlan.value
	&& planCapability.value
	&& (!planResolved.value || hasPlan.value)
));
const eventCountsReady = computed(() => Boolean(taskUuid.value && eventCountsTaskUuid.value === taskUuid.value));
const launchInfo = computed(() => initialAgentLaunch(workspaceData.value || {}, fallbackArguments.value));
const activityModelLabel = computed(() => {
	const launchModel = String(launchInfo.value.model || "");
	return launchModel && launchModel !== "—"
		? launchModel
		: (workspaceData.value?.task?.modelLabel || workspaceData.value?.task?.model || "");
});
const activityThinkLevel = computed(() => {
	const level = String(launchInfo.value.thinkLevel || "");
	return level === "—" ? "" : level;
});
const activityFastMode = computed(() => Boolean(launchInfo.value.fastMode));
const displayLines = computed(() => {
	if (!isActiveLiveAgent.value) return agentState.value.recentLines;
	return liveLines().map((message, index) => ({key: `live-${index}`, timeLabel: "运行中", message, kind: "event"}));
});
const activityDisplayLines = computed(() => {
	const lines = activityLoaded.value ? activityLines.value : displayLines.value;
	return lines
		.filter((item) => !isActivityExcludedEventKind(item.kind))
		.map((item) => {
			const compaction = agentCompactionActivityView(item);
			if (compaction.isCompaction) {
				return {
					...item,
					kind: "context_compaction_compact",
					compaction,
					compactedOutput: compaction.output,
					emptyOutputText: compaction.emptyOutputText,
					message: compaction.message,
					tone: compaction.failed ? "danger" : "success",
				};
			}
			if (item.kind !== "tool_call_started") return item;
			const toolName = String(item.toolName || item.detail?.name || "Tool");
			const rawArguments = item.rawArguments ?? item.detail?.arguments ?? "";
			const summary = toolArgumentsSummary(toolName, rawArguments);
			return {
				...item,
				toolName,
				rawArguments,
				toolDescription: summary,
				message: `调用工具 ${toolName}${summary ? ` · ${summary}` : ""}`,
			};
		});
});
const pushedMonitorLines = computed(() => {
	const candidates = [
		props.event?.livePayload?.recentEvents,
		props.event?.result?.recentEvents,
		...(agentState.value.rows || []).flatMap((row) => [row?.recentEvents, row?.task?.recentEvents, row?.payload?.recentEvents]),
	];
	const byKey = new Map();
	for (const events of candidates) {
		if (!Array.isArray(events)) continue;
		for (const item of events) {
			if (!isAgentMonitorEventKind(item?.kind || item?.type)) continue;
			const line = activityEventLine(item);
			byKey.set(line.key, line);
		}
	}
	return [...byKey.values()];
});
const monitorDisplayLines = computed(() => {
	const base = activityLoaded.value ? activityLines.value : displayLines.value;
	const byKey = new Map([...base, ...pushedMonitorLines.value]
		.filter((item) => isAgentMonitorEventKind(item.kind))
		.map((item) => [item.key, item]));
	return [...byKey.values()].sort((left, right) => {
		const leftTs = Number(left.ts || 0) * (Number(left.ts || 0) < 10_000_000_000 ? 1000 : 1);
		const rightTs = Number(right.ts || 0) * (Number(right.ts || 0) < 10_000_000_000 ? 1000 : 1);
		return leftTs - rightTs || Number(left.seq || 0) - Number(right.seq || 0);
	});
});
const monitorCards = computed(() => buildAgentMonitorTimeline(monitorDisplayLines.value, workspaceData.value || {}));
const activityCount = computed(() => activityDisplayLines.value.length);
const monitorCount = computed(() => monitorDisplayLines.value.length);
const monitorDisplayTotal = computed(() => Math.max(monitorTotal.value, monitorCount.value));
const loadedEventCount = computed(() => activityLoaded.value ? activityLines.value.length : activityCount.value);
const activityLatestSeq = computed(() => Math.max(0, ...activityLines.value.map((item) => Number(item.seq || 0))));
const panelTabs = computed(() => [
	{id: "plan", label: "执行进度", show: showPlanTab.value},
	{id: "activity", label: "过程记录", show: true, count: canLoadPlan.value ? (eventCountsReady.value ? activityTotal.value : 0) : activityCount.value},
	{id: "monitor", label: "监控事件", show: true, count: canLoadPlan.value ? (eventCountsReady.value ? monitorTotal.value : 0) : monitorCount.value},
	{id: "launch", label: "启动信息", show: Boolean(canLoadPlan.value || fallbackArguments.value)},
	{id: "output", label: "结果", show: Boolean(primaryOutputSection.value)},
].filter((item) => item.show));

// The Agent operation is the WS-native source for Rath event progress.  Its
// revision and task-local event seq deliberately exclude projected supervision
// operations, whose displaySeq belongs to a different numbering domain.
const agentPushSnapshot = computed(() => agentTaskPushSnapshot(props.event, taskUuid.value));
const agentPushKey = computed(() => [
	taskUuid.value,
	agentPushSnapshot.value.operationRevision,
	agentPushSnapshot.value.latestSeq,
].join("|"));

const workspaceRefreshKey = computed(() => {
	const row = agentState.value.rows.find((item) => String(item?.taskUuid || "") === taskUuid.value) || agentState.value.rows[0] || {};
	const runtime = props.event?.livePayload?.planRuntime || props.event?.livePayload?.plan_runtime || {};
	return [
		taskUuid.value, row.status,
		runtime.phase, runtime.activePlanVersion, runtime.pendingPlanVersion,
		runtime.currentStepId, runtime.rowRevision, runtime.latestEventSeq,
	].join("|");
});

function activityTimeLabel(value) {
	const number = Number(value || 0);
	if (!number) return "—";
	return new Date(number * (number < 10_000_000_000 ? 1000 : 1)).toLocaleTimeString("zh-CN", {
		hour: "2-digit",
		minute: "2-digit",
		second: "2-digit",
		hour12: false,
	});
}

function activityEventLine(item, index = 0) {
	const source = {
		...(item && typeof item === "object" ? item : {}),
		taskUuid: taskUuid.value,
		detail: item?.detail && typeof item.detail === "object" ? item.detail : {},
	};
	const kind = String(source.kind || "event");
	const toolName = String(source.detail.name || source.name || "Tool");
	const rawArguments = source.detail.arguments ?? source.arguments ?? "";
	return {
		key: `${taskUuid.value}|${source.seq || index}|${kind}`,
		seq: Number(source.seq || 0),
		ts: Number(source.ts || 0),
		kind,
		summary: String(source.summary || ""),
		detail: source.detail,
		toolName,
		rawArguments,
		timeLabel: activityTimeLabel(source.ts),
		message: recentEventMessage(source) || String(source.summary || source.kind || "Agent 动态"),
	};
}

function responseMonitorTotal(data) {
	if (!data || !Object.prototype.hasOwnProperty.call(data, "monitorTotal")) return null;
	const value = Number(data.monitorTotal);
	return Number.isFinite(value) ? Math.max(0, value) : null;
}

function countMonitorEvents(events) {
	return (Array.isArray(events) ? events : []).filter((item) => isAgentMonitorEventKind(item?.kind)).length;
}

async function loadEventCounts(force = false) {
	const requestedTaskUuid = taskUuid.value;
	if (!canLoadPlan.value || !requestedTaskUuid) return;
	if (!force && eventCountsTaskUuid.value === requestedTaskUuid) return;
	if (eventCountsPromise) {
		await eventCountsPromise;
		if (taskUuid.value === requestedTaskUuid && eventCountsTaskUuid.value !== requestedTaskUuid) {
			await loadEventCounts(force);
		}
		return;
	}
	eventCountsPromise = (async () => {
		let data = await Api.rathTaskEvents(props.conversationUuid, requestedTaskUuid, {limit: 100});
		if (data?.ok === false) throw new Error(data.error || "事件数量读取失败");
		let total = Math.max(0, Number(data?.total || 0));
		const authoritativeMonitorTotal = responseMonitorTotal(data);
		const legacyApi = authoritativeMonitorTotal === null;
		let nextMonitorTotal = authoritativeMonitorTotal ?? countMonitorEvents(data?.events);
		let hasMore = Boolean(data?.hasMore);
		let beforeSeq = Math.max(0, Number(data?.nextBeforeSeq || 0));
		let previousBeforeSeq = -1;
		while (legacyApi && hasMore && beforeSeq && beforeSeq !== previousBeforeSeq) {
			if (taskUuid.value !== requestedTaskUuid) return;
			previousBeforeSeq = beforeSeq;
			data = await Api.rathTaskEvents(props.conversationUuid, requestedTaskUuid, {beforeSeq, limit: 100});
			if (data?.ok === false) throw new Error(data.error || "事件数量读取失败");
			total = Math.max(total, Number(data?.total || 0));
			nextMonitorTotal += countMonitorEvents(data?.events);
			hasMore = Boolean(data?.hasMore);
			beforeSeq = Math.max(0, Number(data?.nextBeforeSeq || 0));
		}
		if (taskUuid.value !== requestedTaskUuid) return;
		activityTotal.value = total;
		monitorTotal.value = Math.max(0, nextMonitorTotal);
		eventCountsTaskUuid.value = requestedTaskUuid;
	})().catch(() => {
		// 内容页仍可独立加载；数量请求失败时保持角标为空，避免展示局部缓存数量。
	}).finally(() => {
		eventCountsPromise = null;
	});
	await eventCountsPromise;
}

function eventTab(value = panelTab.value) {
	return value === "activity" || value === "monitor" ? value : "";
}

function rememberEventScroll(tab = eventTab(), scroller = activityScroller.value) {
	if (!tab || !scroller) return;
	eventScrollState[tab].top = Number(scroller.scrollTop || 0);
}

async function restoreEventScroll(tab) {
	await nextTick();
	if (panelTab.value !== tab) return;
	const scroller = activityScroller.value;
	const state = eventScrollState[tab];
	if (!scroller || !state) return;
	const maxTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
	scroller.scrollTop = state.interacted ? Math.min(state.top, maxTop) : maxTop;
	state.top = scroller.scrollTop;
}

async function loadActivity({older = false, newer = false, force = false, limit = 20} = {}) {
	if (!canLoadPlan.value) return;
	if (activityLoading.value) {
		if (activityLoadPromise) await activityLoadPromise;
		return;
	}
	if (older && (!activityLoaded.value || !activityHasMore.value)) return;
	if (newer && !activityLoaded.value) newer = false;
	if (!older && !newer && !force && activityLoaded.value) return;
	const visibleTab = eventTab();
	const visibleState = visibleTab ? eventScrollState[visibleTab] : null;
	const previousScroller = activityScroller.value;
	const previousHeight = Number(previousScroller?.scrollHeight || 0);
	const previousTop = Number(previousScroller?.scrollTop || 0);
	const stickToLatest = !visibleState?.interacted
		|| !previousScroller
		|| previousScroller.scrollHeight - previousScroller.clientHeight - previousTop <= 36;
	let finishLoad;
	activityLoadPromise = new Promise((resolve) => { finishLoad = resolve; });
	activityLoading.value = true;
	activityError.value = "";
	try {
		const data = await Api.rathTaskEvents(props.conversationUuid, taskUuid.value, {
			beforeSeq: older ? activityBeforeSeq.value : 0,
			afterSeq: newer ? activityLatestSeq.value : 0,
			limit: newer ? 100 : limit,
		});
		if (data?.ok === false) throw new Error(data.error || "过程记录读取失败");
		const incoming = (Array.isArray(data?.events) ? data.events : []).map(activityEventLine);
		const existing = new Set(activityLines.value.map((item) => item.key));
		const novel = incoming.filter((item) => !existing.has(item.key));
		if (older) {
			activityLines.value = [...novel, ...activityLines.value];
		} else if (newer) {
			activityLines.value = [...activityLines.value, ...novel].sort((a, b) => a.seq - b.seq);
		} else {
			activityLines.value = incoming;
		}
		activityLoaded.value = true;
		const nextActivityTotal = Math.max(0, Number(data?.total || activityLines.value.length));
		const authoritativeMonitorTotal = responseMonitorTotal(data);
		if (authoritativeMonitorTotal !== null) {
			monitorTotal.value = authoritativeMonitorTotal;
			eventCountsTaskUuid.value = taskUuid.value;
		} else if (newer && eventCountsReady.value && nextActivityTotal > activityTotal.value) {
			monitorTotal.value += novel.filter((item) => isAgentMonitorEventKind(item.kind)).length;
		}
		activityTotal.value = nextActivityTotal;
		if (!newer) {
			activityHasMore.value = Boolean(data?.hasMore);
			activityBeforeSeq.value = Math.max(0, Number(data?.nextBeforeSeq || 0));
		}
		await nextTick();
		const scroller = eventTab() === visibleTab ? activityScroller.value : null;
		if (scroller) {
			if (older) scroller.scrollTop = Math.max(0, previousTop + scroller.scrollHeight - previousHeight);
			else if (!newer || stickToLatest) scroller.scrollTop = scroller.scrollHeight;
			if (visibleState) visibleState.top = scroller.scrollTop;
		}
	} catch (error) {
		activityError.value = apiError(error);
	} finally {
		activityLoading.value = false;
		finishLoad?.();
		activityLoadPromise = null;
	}
}

async function ensurePlanRecentActivity() {
	const requestedTaskUuid = taskUuid.value;
	if (!canLoadPlan.value || !requestedTaskUuid || activityLoaded.value) return;
	if (planRecentActivityPromise) {
		await planRecentActivityPromise;
		return;
	}
	planRecentActivityPromise = loadActivity({limit: 100}).finally(() => {
		planRecentActivityPromise = null;
	});
	await planRecentActivityPromise;
}

async function applyAgentPushEvents(events) {
	if (!activityLoaded.value || !Array.isArray(events) || !events.length) return;
	const incoming = events.map(activityEventLine);
	const merged = mergeAgentEventLines(activityLines.value, incoming);
	if (merged.gap) {
		// A reconnect or dropped WS frame left a real task-seq hole.  HTTP is a
		// recovery path only; normal contiguous WS events are merged directly.
		void loadActivity({newer: true});
		return;
	}
	if (!merged.novel.length) return;
	const visibleTab = eventTab();
	const state = visibleTab ? eventScrollState[visibleTab] : null;
	const scroller = activityScroller.value;
	const stickToLatest = !state?.interacted
		|| !scroller
		|| scroller.scrollHeight - scroller.clientHeight - Number(scroller.scrollTop || 0) <= 36;
	activityLines.value = merged.lines;
	activityTotal.value = Math.max(activityTotal.value, merged.latestSeq, activityLines.value.length);
	if (eventCountsReady.value) {
		monitorTotal.value += merged.novel.filter((item) => isAgentMonitorEventKind(item.kind)).length;
	}
	await nextTick();
	const nextScroller = eventTab() === visibleTab ? activityScroller.value : null;
	if (nextScroller && stickToLatest) {
		nextScroller.scrollTop = nextScroller.scrollHeight;
		if (state) state.top = nextScroller.scrollTop;
	}
}

function pushedEventsAffectWorkspace(events) {
	return (Array.isArray(events) ? events : []).some((item) => {
		const kind = String(item?.kind || item?.type || "");
		return kind.startsWith("plan_")
			|| ["task_completed", "task_failed", "task_cancelled", "task_interrupted", "needs_openbear_control"].includes(kind);
	});
}

function markActivityInteraction(event) {
	const tab = eventTab();
	if (!tab) return;
	const scroller = event?.currentTarget || activityScroller.value;
	eventScrollState[tab].interacted = true;
	rememberEventScroll(tab, scroller);
}

function onActivityScroll(event) {
	const tab = eventTab();
	if (!tab) return;
	const top = Number(event?.currentTarget?.scrollTop || 0);
	eventScrollState[tab].top = top;
	if (!eventScrollState[tab].interacted || activityLoading.value || !activityHasMore.value) return;
	if (top <= 24) void loadActivity({older: true});
}

async function ensureMonitorHistory() {
	let previousCursor = -1;
	while (
		panelTab.value === "monitor"
		&& activityLoaded.value
		&& monitorCount.value < monitorTotal.value
		&& activityHasMore.value
	) {
		const cursor = Number(activityBeforeSeq.value || 0);
		if (!cursor || cursor === previousCursor) break;
		previousCursor = cursor;
		await loadActivity({older: true, limit: 100});
	}
}

async function prepareEventTab(tab) {
	await nextTick();
	if (panelTab.value !== tab) return;
	if (!eventCountsReady.value) await loadEventCounts();
	if (!activityLoaded.value) await loadActivity();
	if (tab === "monitor") await ensureMonitorHistory();
	await restoreEventScroll(tab);
}

async function loadWorkspace(force = false) {
	if (!canLoadPlan.value) return;
	const mustReload = force || workspaceDirty.value;
	if (workspaceLoading.value) {
		if (mustReload || workspaceLoadingTaskUuid !== taskUuid.value) workspaceReloadQueued = true;
		return;
	}
	if (!mustReload && workspaceData.value && workspaceTaskUuid.value === taskUuid.value) return;
	const requestedTaskUuid = taskUuid.value;
	const requestedRefreshKey = workspaceRefreshKey.value;
	workspaceLoadingTaskUuid = requestedTaskUuid;
	workspaceLoading.value = true;
	workspaceError.value = "";
	try {
		const data = await Api.rathTaskPlan(props.conversationUuid, requestedTaskUuid);
		if (data?.ok === false) throw new Error(data.error || "执行计划读取失败");
		if (taskUuid.value !== requestedTaskUuid) return;
		workspaceData.value = data;
		workspaceTaskUuid.value = requestedTaskUuid;
		if (workspaceRefreshKey.value === requestedRefreshKey && !workspaceReloadQueued) workspaceDirty.value = false;
		if (planResolved.value && !hasPlan.value && panelTab.value === "plan") {
			ensureDefaultTab();
			void prepareEventTab("activity");
		}
	} catch (error) {
		if (taskUuid.value === requestedTaskUuid) workspaceError.value = apiError(error);
	} finally {
		const reloadQueued = workspaceReloadQueued;
		workspaceReloadQueued = false;
		workspaceLoadingTaskUuid = "";
		workspaceLoading.value = false;
		if (reloadQueued && canLoadPlan.value) void loadWorkspace(true);
	}
}

function ensureDefaultTab() {
	const available = panelTabs.value.map((item) => item.id);
	if (!available.includes(panelTab.value)) panelTab.value = showPlanTab.value && available.includes("plan") ? "plan" : "activity";
}

function prepareOpenPanel({resetTab = false} = {}) {
	if (props.previewOnly) return;
	if (resetTab) panelTab.value = showPlanTab.value ? "plan" : "activity";
	if (canLoadPlan.value) {
		void loadWorkspace();
		void loadEventCounts();
		if (showPlanTab.value) void ensurePlanRecentActivity();
		else void prepareEventTab("activity");
	} else {
		void prepareEventTab("activity");
	}
}

function onToggle(event) {
	if (props.previewOnly) return;
	props.onDetailsToggle(event, detailId.value);
	if (!event?.currentTarget?.open) {
		rememberEventScroll();
		return;
	}
	prepareOpenPanel({resetTab: true});
}

function selectTab(tab) {
	rememberEventScroll();
	panelTab.value = tab;
	if (["plan", "launch"].includes(tab)) void loadWorkspace();
	if (tab === "plan") void ensurePlanRecentActivity();
	if (["activity", "monitor"].includes(tab)) void prepareEventTab(tab);
}

async function copyText(value, label) {
	const content = String(value || "");
	if (!content) return;
	try {
		await copyTextToClipboard(content);
		ElMessage.success(`${label}已复制`);
	} catch {
		ElMessage.error("复制失败，请手动选择文本");
	}
}

watch(isOpen, (open) => {
	if (!props.previewOnly && open) prepareOpenPanel({resetTab: true});
}, {immediate: true});

watch(taskUuid, () => {
	workspaceData.value = null;
	workspaceTaskUuid.value = "";
	workspaceError.value = "";
	workspaceDirty.value = false;
	activityLines.value = [];
	activityLoaded.value = false;
	activityLoading.value = false;
	activityError.value = "";
	activityHasMore.value = false;
	activityBeforeSeq.value = 0;
	activityTotal.value = 0;
	monitorTotal.value = 0;
	eventCountsTaskUuid.value = "";
	workspaceReloadQueued = false;
	for (const state of Object.values(eventScrollState)) {
		state.top = 0;
		state.interacted = false;
	}
	ensureDefaultTab();
	if (isOpen.value && canLoadPlan.value) {
		void loadWorkspace();
		void loadEventCounts();
		if (showPlanTab.value) void ensurePlanRecentActivity();
		else void prepareEventTab("activity");
	}
});
watch(workspaceRefreshKey, (value, oldValue) => {
	if (value === oldValue) return;
	workspaceDirty.value = true;
	if (isOpen.value) void loadWorkspace(true);
});
watch(agentPushKey, (value, oldValue) => {
	if (value === oldValue) return;
	const pushed = agentPushSnapshot.value;
	const oldParts = String(oldValue || "").split("|");
	const previousTaskUuid = oldParts[0] || "";
	const previousSeq = previousTaskUuid === taskUuid.value ? Number(oldParts[oldParts.length - 1] || 0) : 0;
	const novelPushedEvents = pushed.events.filter((item) => Number(item?.seq || 0) > previousSeq);
	const affectsWorkspace = pushedEventsAffectWorkspace(novelPushedEvents);
	activityTotal.value = Math.max(activityTotal.value, pushed.latestSeq);
	if (affectsWorkspace) workspaceDirty.value = true;
	if (!isOpen.value) return;
	void applyAgentPushEvents(pushed.events);
	if (affectsWorkspace) void loadWorkspace(true);
});
</script>

<template>
	<div
		v-if="props.previewOnly"
		class="agent-tool-event"
		:class="[`agent-${panelStatusClass}`, {'process-live': isActiveLiveAgent || (props.event.live && panelStatusClass === 'running')}]"
	>
		<div class="agent-summary-row agent-preview-row">
			<span class="agent-icon"><Cpu/></span>
			<span class="agent-summary-title">{{ summaryTitle }}</span>
			<span class="agent-summary-preview">{{ summarySubtitle }}</span>
			<span class="agent-summary-status-icon" :class="panelStatusClass">
				<Loading v-if="panelStatusClass === 'running'"/>
				<component v-else :is="agentState.summary.statusIcon"/>
			</span>
		</div>
	</div>

	<details
		v-else
		class="agent-tool-event"
		:class="[`agent-${panelStatusClass}`, {'process-live': isActiveLiveAgent || (props.event.live && panelStatusClass === 'running')}]"
		:open="isOpen"
		@toggle="onToggle"
	>
		<summary class="agent-summary-row">
			<span class="agent-icon"><Cpu/></span>
			<span class="agent-summary-title">{{ summaryTitle }}</span>
			<span class="agent-summary-preview">{{ summarySubtitle }}</span>
			<span class="agent-summary-status-icon" :class="panelStatusClass">
				<Loading v-if="panelStatusClass === 'running'"/>
				<component v-else :is="agentState.summary.statusIcon"/>
			</span>
			<span class="disclosure-icon"><ArrowRight/></span>
		</summary>

		<div v-if="isOpen" class="agent-tool-detail">
			<div class="agent-event-card agent-panel-card">
				<header class="agent-panel-head">
					<div class="agent-identity">
						<div class="agent-orb" :class="panelStatusClass"><Cpu/></div>
						<div class="min-w-0">
							<div class="agent-title-row"><span class="agent-title">{{ panelTitle }}</span><span class="agent-state" :class="panelStatusClass">{{ panelStatusLabel }}</span></div>
							<div class="agent-subtitle">{{ agentState.subtitle || '后台 Agent 任务' }}</div>
						</div>
					</div>
					<div v-if="agentState.metricChips.length" class="agent-metrics-row" aria-label="Agent 运行指标">
						<span v-for="chip in agentState.metricChips" :key="chip.key"><component :is="chip.icon" class="tiny-icon"/>{{ chip.label }}</span>
					</div>
				</header>

				<nav class="agent-tabs" role="tablist" aria-label="Agent 详情">
					<button v-for="tab in panelTabs" :key="tab.id" type="button" role="tab" :aria-selected="panelTab === tab.id" :class="{active: panelTab === tab.id}" @click="selectTab(tab.id)">
						{{ tab.label }}<span v-if="Number(tab.count || 0)">{{ tab.count }}</span>
					</button>
				</nav>

				<section class="agent-tab-panel">
					<AgentPlanWorkspace
						v-if="panelTab === 'plan'"
						:data="workspaceData"
						:loading="workspaceLoading"
						:error="workspaceError"
						:activity-lines="activityDisplayLines"
						:activity-loading="activityLoading"
						:activity-error="activityError"
						@refresh="loadWorkspace(true); ensurePlanRecentActivity()"
					/>

					<div v-else-if="panelTab === 'activity'" class="activity-panel">
						<div class="tab-intro"><div><strong>过程记录</strong><span>实时追加业务执行事件；向上滚动加载更早记录</span></div><em>已载入 {{ loadedEventCount }} / {{ activityTotal }} 条事件</em></div>
						<div
							ref="activityScroller"
							class="activity-scroll"
							@scroll.passive="onActivityScroll"
							@wheel.passive="markActivityInteraction"
							@touchmove.passive="markActivityInteraction"
							@pointerdown="markActivityInteraction"
						>
							<div class="activity-history-state">
								<Loading v-if="activityLoading" class="activity-loading"/>
								<button v-else-if="activityHasMore" type="button" @click="markActivityInteraction(); loadActivity({older: true})">上拉或点击加载更早 20 条</button>
								<span v-else-if="activityLoaded && activityDisplayLines.length">已到最早一条记录</span>
							</div>
							<div v-if="activityError" class="activity-error"><span>{{ activityError }}</span><button type="button" @click="loadActivity({force: true})">重试</button></div>
							<AgentProcessActivity
								v-if="activityDisplayLines.length || !activityLoading"
								:source-lines="activityDisplayLines"
								:model-label="activityModelLabel"
								:think-level="activityThinkLevel"
								:fast-mode="activityFastMode"
							/>
						</div>
					</div>

					<div v-else-if="panelTab === 'monitor'" class="activity-panel monitor-panel">
						<div class="tab-intro"><div><strong>监控事件</strong><span>任务启动、计划审查、步骤进展、主控干预与最终交付</span></div><em>已载入 {{ monitorCount }} / {{ monitorDisplayTotal }} 条事件</em></div>
						<div
							ref="activityScroller"
							class="activity-scroll"
							@scroll.passive="onActivityScroll"
							@wheel.passive="markActivityInteraction"
							@touchmove.passive="markActivityInteraction"
							@pointerdown="markActivityInteraction"
						>
							<div class="activity-history-state">
								<Loading v-if="activityLoading" class="activity-loading"/>
								<button v-else-if="activityHasMore" type="button" @click="markActivityInteraction(); loadActivity({older: true})">上拉或点击加载更早 20 条</button>
								<span v-else-if="activityLoaded && monitorDisplayLines.length">已到最早一条记录</span>
							</div>
							<div v-if="activityError" class="activity-error"><span>{{ activityError }}</span><button type="button" @click="loadActivity({force: true})">重试</button></div>
							<div v-if="monitorCards.length" class="monitor-timeline">
								<article v-for="card in monitorCards" :key="card.key" class="monitor-card" :class="`tone-${card.tone}`">
									<div class="monitor-rail"><time>{{ card.timeLabel }}</time><span class="monitor-dot"></span></div>
									<div class="monitor-card-body">
										<div class="monitor-card-heading"><span>{{ card.category }}</span><strong>{{ card.title }}</strong></div>
										<p v-if="card.description">{{ card.description }}</p>
										<ul v-if="card.bullets.length"><li v-for="(bullet, bulletIndex) in card.bullets" :key="`${card.key}-bullet-${bulletIndex}`">{{ bullet }}</li></ul>
										<div v-if="card.badges.length" class="monitor-badges"><span v-for="badge in card.badges" :key="badge">{{ badge }}</span></div>
									</div>
								</article>
							</div>
							<div v-else-if="!activityLoading" class="tab-empty">暂无监控事件。</div>
						</div>
					</div>

					<div v-else-if="panelTab === 'launch'" class="launch-panel">
						<div class="tab-intro">
							<div class="tab-intro-copy">
								<div class="tab-title-line"><strong>启动信息</strong><span>这里始终显示创建 task 时的原始任务，不会被后续 AgentWait 覆盖</span></div>
								<div class="launch-meta-tags" aria-label="Agent 启动参数">
									<span class="launch-meta-tag"><b>Agent</b>{{ launchInfo.agentName }}</span>
									<span class="launch-meta-tag"><b>模型</b>{{ launchInfo.model }}</span>
									<span class="launch-meta-tag"><b>思考</b>{{ launchInfo.thinkLevel }}</span>
									<span class="launch-meta-tag"><b>Fast</b>{{ launchInfo.fastMode ? '开启' : '关闭' }}</span>
									<span class="launch-meta-tag launch-tools-tag"><b>工具</b>{{ launchInfo.tools.length ? launchInfo.tools.join(' · ') : '未记录' }}</span>
								</div>
							</div>
						</div>
						<div class="content-frame launch-content-frame">
							<div class="content-frame-head">
								<div class="content-frame-head-main"><span>原始任务 Prompt</span></div>
								<button class="frame-copy-button" type="button" @click="copyText(launchInfo.prompt, '原始任务')"><Operation/>复制任务</button>
							</div>
							<pre class="content-frame-scroll">{{ launchInfo.prompt || '未找到原始任务参数' }}</pre>
						</div>
					</div>

					<div v-else-if="panelTab === 'output'" class="output-panel">
						<div class="tab-intro"><div class="tab-title-line"><strong>Agent 结果</strong><span>任务完成后交给主控汇总的最终输出</span></div></div>
						<div v-if="primaryOutputSection?.text" class="content-frame output-content-frame">
							<div class="content-frame-head">
								<div class="content-frame-head-main"><span>结论内容</span><em v-if="primaryOutputSection.segmented">{{ fmtNum(primaryOutputSection.originalChars) }} 字符 · 当前展示摘要和首段</em></div>
								<button class="frame-copy-button" type="button" @click="copyText(primaryOutputSection.text, 'Agent 结果')"><Document/>复制结果</button>
							</div>
							<div class="content-frame-scroll markdown-scroll"><ConsoleMarkdown class="agent-output" :text="agentOutputDisplay(primaryOutputSection.text)"/></div>
						</div>
						<div v-else class="tab-empty">Agent 尚未生成最终结果。</div>
					</div>
				</section>
			</div>
		</div>
	</details>
</template>

<style scoped>
.agent-tool-event { width: 100%; max-width: 100%; margin: .16rem 0; overflow: visible; border: 0; background: transparent; color: #52525b; font-size: 12px; }
.agent-summary-row { display: flex; gap: .38rem; min-width: 0; min-height: 1.55rem; align-items: center; justify-content: flex-start; padding: .06rem 0; cursor: pointer; list-style: none; user-select: none; }
.agent-preview-row {
	display: grid;
	grid-template-columns: .92rem max-content minmax(0, 1fr) .9rem;
	width: 100%;
	min-height: 1.45rem;
	align-items: center;
	gap: .38rem;
	padding: 0;
	cursor: default;
	line-height: 1.45rem;
}
.agent-preview-row .agent-summary-title { max-width: none; }
.agent-preview-row .agent-summary-preview { width: 100%; max-width: none; }
.agent-preview-row .agent-summary-status-icon { justify-self: end; }
.agent-summary-row::-webkit-details-marker { display: none; }
.agent-icon, .agent-summary-status-icon, .disclosure-icon { display: grid; place-items: center; }
.agent-icon { width: .92rem; height: .92rem; color: #a1a1aa; }
.agent-icon svg { width: .78rem; }
.agent-summary-title, .agent-summary-preview { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-summary-title { max-width: 18rem; flex: 0 1 auto; color: #52525b; font-size: 12px; font-weight: 580; }
.agent-summary-preview { max-width: 24rem; flex: 0 1 auto; color: #a1a1aa; }
.agent-summary-status-icon, .disclosure-icon { flex: 0 0 auto; }
.agent-summary-status-icon { width: .9rem; height: .9rem; color: #94a3b8; }
.agent-summary-status-icon svg { width: .72rem; }
.agent-summary-status-icon.ok { color: #0f766e; }
.agent-summary-status-icon.error { color: #dc2626; }
.agent-summary-status-icon.pending, .agent-summary-status-icon.partial { color: #d97706; }
.agent-summary-status-icon.running { position: relative; border-radius: 999px; background: rgba(37,99,235,.1); color: #2563eb; }
.agent-summary-status-icon.running::before { content: ""; position: absolute; inset: -.18rem; border-radius: inherit; background: conic-gradient(from 0deg, transparent 0 34%, rgba(37,99,235,.95) 45%, transparent 62% 100%); -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1px)); mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1px)); animation: agentOrbit 1.05s linear infinite; }
.disclosure-icon { width: 1rem; height: 1rem; color: #64748b; opacity: 0; transition: opacity .14s ease; }
.disclosure-icon svg { width: .72rem; transition: transform .14s ease; }
.agent-summary-row:hover .disclosure-icon, details[open] .disclosure-icon { opacity: 1; }
details[open] .disclosure-icon svg { transform: rotate(90deg); }
.agent-tool-detail { box-sizing: border-box; width: 100%; max-width: 100%; margin: .3rem 0 .45rem; border: 1px solid rgba(212,212,216,.82); border-radius: 18px; background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(247,247,248,.96)); box-shadow: 0 12px 34px rgba(24,24,27,.08), inset 0 1px 0 rgba(255,255,255,.92); padding: 14px; }
.agent-panel-head { display: flex; gap: 12px; align-items: center; justify-content: space-between; border-bottom: 1px solid rgba(228,228,231,.88); padding: 1px 2px 12px; }
.agent-identity { display: flex; min-width: 0; align-items: center; gap: 9px; }
.agent-orb { display: grid; width: 34px; height: 34px; flex: 0 0 auto; place-items: center; border: 1px solid rgba(212,212,216,.9); border-radius: 11px; background: linear-gradient(180deg, #fff, #f4f4f5); color: #52525b; box-shadow: 0 2px 7px rgba(24,24,27,.06); }
.agent-orb svg { width: 16px; }
.agent-orb.running { border-color: #bfdbfe; background: #eff6ff; color: #2563eb; }
.agent-title-row { display: flex; min-width: 0; align-items: center; gap: 6px; }
.agent-title { min-width: 0; overflow: hidden; color: #18181b; font-size: 14px; font-weight: 680; text-overflow: ellipsis; white-space: nowrap; }
.agent-state { border: 1px solid #e4e4e7; border-radius: 999px; background: #f4f4f5; padding: 2px 7px; color: #71717a; font-size: 11px; font-weight: 650; }
.agent-state.running { border-color: #dbeafe; background: #eff6ff; color: #1d4ed8; }
.agent-state.ok { border-color: #cce8df; background: #f0fdf8; color: #0f766e; }
.agent-state.error { border-color: #fecaca; background: #fff1f2; color: #b91c1c; }
.agent-subtitle { margin-top: 3px; overflow: hidden; color: #a1a1aa; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.agent-metrics-row { display: flex; min-width: 0; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
.agent-metrics-row span { display: inline-flex; align-items: center; gap: 4px; border: 1px solid #e4e4e7; border-radius: 8px; background: rgba(255,255,255,.82); padding: 5px 8px; color: #71717a; font-size: 11px; font-weight: 600; }
.tiny-icon { width: 11px; height: 11px; color: #98a2b3; }
.agent-tabs { display: flex; gap: 3px; overflow-x: auto; margin: 11px 0 12px; border: 1px solid rgba(228,228,231,.72); border-radius: 11px; background: rgba(228,228,231,.5); padding: 3px; scrollbar-width: none; }
.agent-tabs::-webkit-scrollbar { display: none; }
.agent-tabs button { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 5px; border: 0; border-radius: 8px; background: transparent; padding: 7px 11px; color: #71717a; font-size: 12px; font-weight: 620; cursor: pointer; transition: background .15s ease, color .15s ease, box-shadow .15s ease; }
.agent-tabs button:hover { color: #27272a; }
.agent-tabs button.active { background: rgba(255,255,255,.96); color: #18181b; box-shadow: 0 1px 4px rgba(24,24,27,.12), inset 0 1px 0 #fff; }
.agent-tabs button span { border-radius: 999px; background: #e4e4e7; padding: 1px 5px; color: #71717a; font-size: 10px; }
.agent-tab-panel { height: 450px; min-width: 0; min-height: 0; overflow: hidden; }
.agent-tab-panel :deep(.plan-workspace) { width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0; }
.activity-panel, .launch-panel, .output-panel { box-sizing: border-box; display: flex; width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; }
.activity-panel > .tab-intro, .launch-panel > .tab-intro, .output-panel > .tab-intro { flex: 0 0 auto; }
.activity-scroll { min-height: 0; flex: 1 1 auto; overflow-x: hidden; overflow-y: auto; padding-right: 4px; scrollbar-color: #c7c7cc transparent; scrollbar-width: thin; }
.tab-intro { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.tab-intro > div { display: grid; min-width: 0; gap: 3px; }
.tab-intro strong { color: #27272a; font-size: 13px; }
.tab-intro span { color: #a1a1aa; font-size: 12px; }
.tab-intro em { border-radius: 999px; background: #f4f4f5; padding: 3px 7px; color: #71717a; font-size: 11px; font-style: normal; }
.tab-empty { display: grid; min-height: 150px; flex: 1 1 auto; place-items: center; border: 1px dashed #d4d4d8; border-radius: 12px; color: #a1a1aa; font-size: 12px; }
.monitor-timeline { position: relative; padding: 2px 0 4px 4px; }
.monitor-timeline::before { content: ""; position: absolute; left: 62px; top: 15px; bottom: 18px; width: 1px; background: linear-gradient(#bfdbfe, #d4d4d8 30%, #bbf7d0); }
.monitor-card { display: grid; grid-template-columns: 48px 12px minmax(0, 1fr); gap: 7px; align-items: start; position: relative; padding: 5px 0 8px; }
.monitor-rail { display: contents; }
.monitor-rail time { padding-top: 9px; color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; }
.monitor-dot { z-index: 1; width: 8px; height: 8px; margin-top: 11px; border: 2px solid #fff; border-radius: 50%; background: #71717a; box-shadow: 0 0 0 1px #d4d4d8; }
.monitor-card-body { --monitor-accent: #a1a1aa; min-width: 0; overflow: hidden; border: 1px solid #e4e4e7; border-left: 3px solid var(--monitor-accent); border-radius: 0 11px 11px 0; background: rgba(255,255,255,.82); padding: 9px 11px 10px; box-shadow: 0 1px 2px rgba(24,24,27,.025); }
.monitor-card-heading { display: flex; min-width: 0; align-items: center; gap: 7px; }
.monitor-card-heading > span { flex: 0 0 auto; border-radius: 999px; background: #f4f4f5; padding: 2px 6px; color: #71717a; font-size: 9px; font-weight: 650; }
.monitor-card-heading strong { min-width: 0; color: #27272a; font-size: 11.5px; font-weight: 650; line-height: 1.4; overflow-wrap: anywhere; word-break: break-word; }
.monitor-card-body > p { margin: 6px 0 0; color: #52525b; font-size: 11px; line-height: 1.55; overflow-wrap: anywhere; word-break: break-word; }
.monitor-card-body ul { display: grid; gap: 5px; margin: 7px 0 0; padding: 0; list-style: none; }
.monitor-card-body li { position: relative; border-radius: 7px; background: rgba(244,244,245,.75); padding: 5px 7px 5px 17px; color: #52525b; font-size: 10.5px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.monitor-card-body li::before { content: ""; position: absolute; left: 7px; top: 11px; width: 4px; height: 4px; border-radius: 50%; background: #a1a1aa; }
.monitor-badges { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.monitor-badges span { border: 1px solid #e4e4e7; border-radius: 999px; background: #fafafa; padding: 2px 6px; color: #71717a; font-size: 9px; }
.monitor-card.tone-active .monitor-dot { background: #2563eb; box-shadow: 0 0 0 1px #93c5fd; }
.monitor-card.tone-active .monitor-card-body { --monitor-accent: #3b82f6; border-color: #dbeafe; border-left-color: var(--monitor-accent); }
.monitor-card.tone-waiting .monitor-dot, .monitor-card.tone-warning .monitor-dot { background: #d97706; box-shadow: 0 0 0 1px #fcd34d; }
.monitor-card.tone-waiting .monitor-card-body, .monitor-card.tone-warning .monitor-card-body { --monitor-accent: #f59e0b; border-color: #fde7c2; border-left-color: var(--monitor-accent); }
.monitor-card.tone-success .monitor-dot { background: #16a34a; box-shadow: 0 0 0 1px #86efac; }
.monitor-card.tone-success .monitor-card-body { --monitor-accent: #22c55e; border-color: #dcfce7; border-left-color: var(--monitor-accent); }
.monitor-card.tone-danger .monitor-dot { background: #dc2626; box-shadow: 0 0 0 1px #fca5a5; }
.monitor-card.tone-danger .monitor-card-body { --monitor-accent: #ef4444; border-color: #fee2e2; border-left-color: var(--monitor-accent); }
.activity-history-state { display: flex; min-height: 28px; align-items: center; justify-content: center; color: #a1a1aa; font-size: 10px; }
.activity-history-state button { border: 0; background: transparent; padding: 4px 8px; color: #71717a; font-size: 10px; cursor: pointer; }
.activity-history-state button:hover { color: #18181b; }
.activity-loading { width: 13px; color: #71717a; animation: agentOrbit 1s linear infinite; }
.activity-error { display: flex; align-items: center; justify-content: center; gap: 8px; margin: 4px 0 7px; border-radius: 8px; background: #fff4f4; padding: 7px 9px; color: #b42318; font-size: 10.5px; }
.activity-error button { border: 0; background: transparent; color: #b42318; font-size: 10.5px; font-weight: 650; cursor: pointer; }
.tab-intro-copy { min-width: 0; }
.tab-intro .tab-title-line { display: flex; min-width: 0; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.tab-title-line > strong { flex: 0 0 auto; }
.tab-title-line > span { font-size: 11px; }
.launch-meta-tags { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 1px; }
.launch-meta-tag { display: inline-flex; min-width: 0; max-width: 280px; align-items: center; gap: 4px; overflow: hidden; border: 1px solid #e4e4e7; border-radius: 999px; background: #f7f7f8; padding: 1px 7px; color: #52525b !important; font-size: 10px !important; line-height: 1.3; text-overflow: ellipsis; white-space: nowrap; }
.launch-meta-tag b { color: #a1a1aa; font-size: 9px; font-weight: 650; letter-spacing: .02em; }
.launch-tools-tag { max-width: 420px; }
.content-frame { display: flex; min-width: 0; min-height: 0; flex: 1 1 auto; flex-direction: column; overflow: hidden; border: 1px solid #e4e4e7; border-radius: 11px; background: rgba(255,255,255,.9); box-shadow: inset 0 1px 0 rgba(255,255,255,.95); }
.content-frame-head { box-sizing: border-box; display: flex; height: 35px; min-height: 35px; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 10px; border-bottom: 1px solid #e4e4e7; background: rgba(244,244,245,.72); padding: 4px 8px 4px 10px; }
.content-frame-head-main { display: flex; min-width: 0; align-items: center; gap: 8px; overflow: hidden; }
.content-frame-head-main > span { flex: 0 0 auto; color: #71717a; font-size: 11px; font-weight: 650; }
.content-frame-head-main em { overflow: hidden; color: #a1a1aa; font-size: 10px; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.frame-copy-button { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 4px; border: 1px solid #d4d4d8; border-radius: 7px; background: rgba(255,255,255,.92); padding: 3px 7px; color: #52525b; font-size: 10px; cursor: pointer; box-shadow: 0 1px 2px rgba(24,24,27,.04); }
.frame-copy-button:hover { border-color: #a1a1aa; color: #27272a; }
.frame-copy-button svg { width: 10px; }
.content-frame-scroll { box-sizing: border-box; min-width: 0; min-height: 0; flex: 1 1 auto; overflow: auto; margin: 0; padding: 11px 12px; scrollbar-color: #c7c7cc transparent; scrollbar-width: thin; }
.launch-content-frame pre { max-width: 100%; border: 0; background: transparent; color: #3f3f46; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; white-space: pre-wrap; word-break: break-word; }
.markdown-scroll { padding: 12px 14px 16px; }
.output-panel .agent-output { max-width: 100%; max-height: none; overflow: visible; color: #1f2937; font-size: 12px; line-height: 1.65; }
@keyframes agentOrbit { to { transform: rotate(360deg); } }
@media (max-width: 760px) {
	.agent-tool-detail { padding: 9px; }
	.agent-tab-panel { height: min(450px, 68vh); }
	.agent-panel-head { align-items: flex-start; flex-direction: column; }
	.agent-metrics-row { justify-content: flex-start; }
	.launch-meta-tag { max-width: 220px; }
	.launch-tools-tag { max-width: 100%; }
	.agent-tabs button { padding: 7px 9px; }
}
</style>
