<script setup>
import {computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {Close, CollectionTag, Delete, EditPen, Plus, Refresh, RefreshLeft} from "@element-plus/icons-vue";
import {Api, apiError} from "../../api.js";
import {createTaskMemoryRequestGate} from "./taskMemoryRequestGate.js";
import {
	TASK_MEMORY_CHANGED_EVENT_KEY,
	createTaskMemoryBadgeState,
	createTaskMemoryChangedEventGate,
	taskMemoryMutationRecovery,
	taskMemorySourceLabel,
} from "./taskMemoryUiState.js";

const props = defineProps({
	conversationUuid: {type: String, default: ""},
});

const drawerOpen = ref(false);
const activeTab = ref("conversation");
const loading = ref(false);
const tasksLoading = ref(false);
const saving = ref(false);
const items = ref([]);
const tasks = ref([]);
const selectedTaskUuid = ref("");
const includeDeleted = ref(false);
const query = ref("");
const activeTotal = ref(0);
const conversationTotal = ref(0);
const editorOpen = ref(false);
const editorMode = ref("create");
const form = ref(emptyForm());
const injectionPreview = ref("");
const previewRuntimeTokens = ref(0);
const previewMaxTokens = ref(1500);
let refreshTimer = 0;
const requestGate = createTaskMemoryRequestGate();
const changedEvent = inject(TASK_MEMORY_CHANGED_EVENT_KEY, ref(null));
const changedEventGate = createTaskMemoryChangedEventGate();
const badgeState = createTaskMemoryBadgeState();
const stableBadge = ref(badgeState.snapshot());

const usableConversationUuid = computed(() => {
	const value = String(props.conversationUuid || "");
	return value && !value.startsWith("local:") ? value : "";
});
const selectedTask = computed(() => tasks.value.find((task) => task.taskUuid === selectedTaskUuid.value) || null);
const scopeType = computed(() => activeTab.value === "agent" ? "agent_task" : "conversation");
const badgeCount = computed(() => drawerOpen.value
	? (activeTab.value === "agent" ? activeTotal.value : conversationTotal.value)
	: stableBadge.value.count);
const hasContent = computed(() => badgeCount.value > 0);
const bodyBytes = computed(() => new TextEncoder().encode(String(form.value.body || "")).length);
const editorTitle = computed(() => editorMode.value === "create" ? "新增任务记忆" : "编辑任务记忆");

function emptyForm() {
	return {
		memoryUuid: "",
		name: "",
		description: "",
		body: "",
		autoReinjectCatalog: true,
		visibleToAgents: false,
		revision: 0,
	};
}

function currentRequestIdentity() {
	return {
		conversationUuid: usableConversationUuid.value,
		scopeType: scopeType.value,
		taskUuid: scopeType.value === "agent_task" ? selectedTaskUuid.value : "",
	};
}

function requestScopeParams(token, extra = {}) {
	return {
		scopeType: token.scopeType,
		...(token.scopeType === "agent_task" ? {taskUuid: token.taskUuid} : {}),
		...extra,
	};
}

function beginRequest(channel) {
	return requestGate.capture(currentRequestIdentity(), channel);
}

function requestIsCurrent(token) {
	return requestGate.isCurrent(token, currentRequestIdentity());
}

function resetEditorState() {
	editorOpen.value = false;
	editorMode.value = "create";
	form.value = emptyForm();
	saving.value = false;
}

function resetContextState({
	resetTasks = true, resetCounts = true, resetActiveTotal = true, resetFilters = false,
} = {}) {
	resetEditorState();
	items.value = [];
	if (resetActiveTotal) activeTotal.value = 0;
	injectionPreview.value = "";
	previewRuntimeTokens.value = 0;
	loading.value = false;
	tasksLoading.value = false;
	if (resetTasks) {
		tasks.value = [];
		selectedTaskUuid.value = "";
	}
	if (resetCounts) conversationTotal.value = 0;
	if (resetFilters) {
		query.value = "";
		includeDeleted.value = false;
	}
}

function invalidateRequests() {
	requestGate.invalidate();
}

function updateStableBadge(identity, count) {
	stableBadge.value = badgeState.set(identity, count);
}

function statusLabel(status) {
	return ({
		queued: "排队中",
		running: "执行中",
		paused: "已暂停",
		needs_openbear_control: "等待控制",
		completed: "已完成",
		failed: "失败",
		cancelled: "已取消",
		interrupted: "已中断",
	})[String(status || "")] || String(status || "未知");
}

function formatDate(timestamp) {
	const value = Number(timestamp || 0);
	if (!value) return "—";
	return new Intl.DateTimeFormat("zh-CN", {
		month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
	}).format(new Date(value * 1000));
}

function formatBytes(value) {
	const size = Math.max(0, Number(value || 0));
	if (size < 1024) return `${size} B`;
	return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KiB`;
}

function sourceLabel(item) {
	return taskMemorySourceLabel(item);
}

async function loadPreview() {
	const token = beginRequest("preview");
	if (!token.conversationUuid || (token.scopeType === "agent_task" && !token.taskUuid)) {
		injectionPreview.value = "";
		previewRuntimeTokens.value = 0;
		return;
	}
	try {
		const data = await Api.taskMemoryPreview(token.conversationUuid, requestScopeParams(token));
		if (!requestIsCurrent(token)) return;
		injectionPreview.value = String(data?.catalogXml || "");
		previewRuntimeTokens.value = Number(data?.estimatedRuntimeTokens || 0);
		previewMaxTokens.value = Number(data?.maxRuntimeTokens || 1500);
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		injectionPreview.value = "";
		previewRuntimeTokens.value = 0;
		if (drawerOpen.value) ElMessage.error(apiError(error));
	}
}

async function loadConversationCount() {
	const token = beginRequest("count");
	if (!token.conversationUuid) {
		conversationTotal.value = 0;
		return;
	}
	try {
		const data = await Api.taskMemories(token.conversationUuid, {
			scopeType: "conversation", offset: 0, limit: 1,
		});
		if (!requestIsCurrent(token)) return;
		conversationTotal.value = Number(data?.total || 0);
		if (token.scopeType === "conversation") updateStableBadge(token, conversationTotal.value);
	} catch {
		if (requestIsCurrent(token)) conversationTotal.value = 0;
	}
}

async function loadScopedCount() {
	const token = beginRequest("scope-count");
	if (!token.conversationUuid || (token.scopeType === "agent_task" && !token.taskUuid)) {
		activeTotal.value = 0;
		return;
	}
	try {
		const data = await Api.taskMemories(token.conversationUuid, requestScopeParams(token, {
			includeDeleted: 0, offset: 0, limit: 1,
		}));
		if (!requestIsCurrent(token)) return;
		activeTotal.value = Number(data?.activeTotal || 0);
		if (token.scopeType === "conversation") conversationTotal.value = activeTotal.value;
		updateStableBadge(token, activeTotal.value);
	} catch {
		// Keep the last stable badge count; the 5-second refresh remains the fallback.
	}
}

async function loadTasks() {
	const token = beginRequest("tasks");
	if (!token.conversationUuid) return;
	tasksLoading.value = true;
	try {
		const data = await Api.taskMemoryTasks(token.conversationUuid);
		if (!requestIsCurrent(token)) return;
		const nextTasks = Array.isArray(data?.tasks) ? data.tasks : [];
		const nextSelected = nextTasks.some((task) => task.taskUuid === token.taskUuid)
			? token.taskUuid
			: (nextTasks[0]?.taskUuid || "");
		tasks.value = nextTasks;
		tasksLoading.value = false;
		if (selectedTaskUuid.value !== nextSelected) selectedTaskUuid.value = nextSelected;
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		tasks.value = [];
		selectedTaskUuid.value = "";
		tasksLoading.value = false;
		if (drawerOpen.value) ElMessage.error(apiError(error));
	}
}

async function loadCurrent({silent = false} = {}) {
	const token = beginRequest("items");
	const requestQuery = String(query.value || "").trim();
	const requestIncludeDeleted = includeDeleted.value;
	if (!token.conversationUuid || (token.scopeType === "agent_task" && !token.taskUuid)) {
		items.value = [];
		activeTotal.value = 0;
		return;
	}
	if (!silent) loading.value = true;
	try {
		const data = await Api.taskMemories(token.conversationUuid, requestScopeParams(token, {
			query: requestQuery,
			includeDeleted: requestIncludeDeleted ? 1 : 0,
			offset: 0,
			limit: 50,
		}));
		if (!requestIsCurrent(token)) return;
		items.value = Array.isArray(data?.items) ? data.items : [];
		activeTotal.value = Number(data?.activeTotal ?? items.value.filter((item) => !Number(item.deletedAt || 0)).length);
		updateStableBadge(token, activeTotal.value);
		if (token.scopeType === "conversation" && !requestIncludeDeleted && !requestQuery) {
			conversationTotal.value = Number(data?.total || 0);
		}
	} catch (error) {
		if (requestIsCurrent(token) && !silent) ElMessage.error(apiError(error));
	} finally {
		if (requestIsCurrent(token)) loading.value = false;
	}
}

async function openDrawer() {
	drawerOpen.value = true;
	const token = beginRequest("open");
	if (scopeType.value === "agent_task") await loadTasks();
	if (!drawerOpen.value || !requestIsCurrent(token)) return;
	await Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
}

function startRefreshTimer() {
	stopRefreshTimer();
	refreshTimer = window.setInterval(() => {
		if (!drawerOpen.value || document.hidden) return;
		void Promise.all([loadCurrent({silent: true}), loadConversationCount(), loadPreview()]);
	}, 5000);
}

function stopRefreshTimer() {
	if (refreshTimer) window.clearInterval(refreshTimer);
	refreshTimer = 0;
}

function handleWindowFocus() {
	if (!drawerOpen.value) return;
	const loaders = [loadCurrent({silent: true}), loadConversationCount(), loadPreview()];
	if (scopeType.value === "agent_task") loaders.push(loadTasks());
	void Promise.all(loaders);
}

async function handleTaskMemoryChanged(event) {
	if (!changedEventGate.accept(event, currentRequestIdentity())) return;
	invalidateRequests();
	if (drawerOpen.value) {
		await Promise.all([loadCurrent({silent: true}), loadConversationCount(), loadPreview()]);
		return;
	}
	await Promise.all([loadScopedCount(), loadConversationCount()]);
}

async function recoverStaleMutation(error, token) {
	if (!requestIsCurrent(token)) return true;
	const recovery = taskMemoryMutationRecovery(error);
	if (!recovery.refresh) return false;
	invalidateRequests();
	if (recovery.resetEditor) resetEditorState();
	ElMessage.warning(recovery.message);
	await Promise.all([loadCurrent({silent: true}), loadConversationCount(), loadPreview()]);
	return true;
}

function newMemory() {
	if (scopeType.value === "agent_task" && !selectedTaskUuid.value) {
		ElMessage.warning("请先选择 Agent 任务");
		return;
	}
	editorMode.value = "create";
	form.value = emptyForm();
	editorOpen.value = true;
}

async function editMemory(item) {
	const token = beginRequest("detail");
	const memoryUuid = String(item?.memoryUuid || "");
	try {
		const data = await Api.taskMemory(
			token.conversationUuid,
			memoryUuid,
			requestScopeParams(token, {includeDeleted: item.deletedAt ? 1 : 0}),
		);
		if (!requestIsCurrent(token)) return;
		const detail = data?.memory || {};
		editorMode.value = "edit";
		form.value = {
			memoryUuid: detail.memoryUuid || "",
			name: detail.name || "",
			description: detail.description || "",
			body: detail.body || "",
			autoReinjectCatalog: Boolean(detail.autoReinjectCatalog),
			visibleToAgents: Boolean(detail.visibleToAgents),
			revision: Number(detail.revision || 0),
		};
		editorOpen.value = true;
		await nextTick();
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		if (!(await recoverStaleMutation(error, token))) ElMessage.error(apiError(error));
	}
}

async function saveMemory() {
	const name = String(form.value.name || "").trim();
	const description = String(form.value.description || "").trim();
	if (!name) return ElMessage.warning("名称不能为空");
	if (name.length > 80) return ElMessage.warning("名称最多 80 个字符");
	if (description.length > 200) return ElMessage.warning("说明最多 200 个字符");
	if (bodyBytes.value > 16 * 1024) return ElMessage.warning("正文最多 16 KiB（UTF-8）");
	const token = beginRequest("mutation");
	const mode = editorMode.value;
	const memoryUuid = String(form.value.memoryUuid || "");
	const payload = {
		...requestScopeParams(token),
		name,
		description,
		body: String(form.value.body || ""),
		autoReinjectCatalog: Boolean(form.value.autoReinjectCatalog),
		...(token.scopeType === "conversation" ? {visibleToAgents: Boolean(form.value.visibleToAgents)} : {}),
	};
	saving.value = true;
	try {
		if (mode === "create") {
			await Api.createTaskMemory(token.conversationUuid, payload);
		} else {
			await Api.updateTaskMemory(token.conversationUuid, memoryUuid, {
				...payload,
				revision: Number(form.value.revision || 0),
			});
		}
		if (!requestIsCurrent(token)) return;
		editorOpen.value = false;
		ElMessage.success(mode === "create" ? "任务记忆已创建" : "任务记忆已更新");
		await Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		if (mode !== "edit" || !(await recoverStaleMutation(error, token))) ElMessage.error(apiError(error));
	} finally {
		if (requestIsCurrent(token)) saving.value = false;
	}
}

async function deleteMemory(item) {
	const token = beginRequest("mutation");
	try {
		await ElMessageBox.confirm(`删除“${item.name}”？可在“显示已删除”后恢复。`, "删除任务记忆", {
			type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
			customClass: "task-memory-confirm",
		});
		if (!requestIsCurrent(token)) return;
		await Api.deleteTaskMemory(token.conversationUuid, item.memoryUuid, {
			...requestScopeParams(token), revision: Number(item.revision || 0),
		});
		if (!requestIsCurrent(token)) return;
		ElMessage.success("已软删除");
		await Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
	} catch (error) {
		if (error === "cancel" || error === "close" || !requestIsCurrent(token)) return;
		if (!(await recoverStaleMutation(error, token))) ElMessage.error(apiError(error));
	}
}

async function restoreMemory(item) {
	const token = beginRequest("mutation");
	try {
		await Api.restoreTaskMemory(token.conversationUuid, item.memoryUuid, {
			...requestScopeParams(token), revision: Number(item.revision || 0),
		});
		if (!requestIsCurrent(token)) return;
		ElMessage.success("任务记忆已恢复");
		await Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		if (!(await recoverStaleMutation(error, token))) ElMessage.error(apiError(error));
	}
}

watch(() => props.conversationUuid, async () => {
	invalidateRequests();
	stableBadge.value = badgeState.switchConversation(usableConversationUuid.value);
	changedEventGate.reset(currentRequestIdentity());
	drawerOpen.value = false;
	activeTab.value = "conversation";
	resetContextState({resetTasks: true, resetCounts: true, resetFilters: true});
	await nextTick();
	await loadConversationCount();
});
watch(drawerOpen, (open) => {
	if (open) {
		startRefreshTimer();
		return;
	}
	stopRefreshTimer();
	invalidateRequests();
	resetContextState({
		resetTasks: false, resetCounts: false, resetActiveTotal: false, resetFilters: false,
	});
});
watch(activeTab, async () => {
	invalidateRequests();
	changedEventGate.reset(currentRequestIdentity());
	resetContextState({resetTasks: true, resetCounts: true, resetFilters: true});
	if (!drawerOpen.value) return;
	const token = beginRequest("scope-transition");
	if (scopeType.value === "agent_task") await loadTasks();
	if (!drawerOpen.value || !requestIsCurrent(token)) return;
	await Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
});
watch(selectedTaskUuid, () => {
	if (activeTab.value !== "agent") return;
	invalidateRequests();
	changedEventGate.reset(currentRequestIdentity());
	resetContextState({resetTasks: false, resetCounts: false, resetFilters: false});
	if (drawerOpen.value) void Promise.all([loadCurrent(), loadConversationCount(), loadPreview()]);
});
watch(includeDeleted, () => {
	if (drawerOpen.value) void loadCurrent();
});
watch(changedEvent, (event) => {
	if (event) void handleTaskMemoryChanged(event);
});

onMounted(() => {
	stableBadge.value = badgeState.switchConversation(usableConversationUuid.value);
	changedEventGate.reset(currentRequestIdentity());
	window.addEventListener("focus", handleWindowFocus);
	void loadConversationCount();
});
onBeforeUnmount(() => {
	requestGate.dispose();
	resetContextState({resetTasks: true, resetCounts: true, resetFilters: true});
	window.removeEventListener("focus", handleWindowFocus);
	stopRefreshTimer();
});
</script>

<template>
	<div v-if="usableConversationUuid" class="task-memory-entry-wrap">
		<el-tooltip content="任务记忆" placement="left" :show-after="260">
			<button
				type="button"
				class="task-memory-entry"
				:class="{ active: drawerOpen, populated: hasContent }"
				:aria-label="badgeCount ? `打开任务记忆，共 ${badgeCount} 条` : '打开任务记忆'"
				aria-haspopup="dialog"
				:aria-expanded="drawerOpen ? 'true' : 'false'"
				@click="openDrawer"
			>
				<svg class="task-memory-entry-icon" viewBox="0 0 1024 1024" aria-hidden="true" focusable="false">
					<path d="M557.44 57.6c132.736 0 239.36 49.6 316.544 147.712 30.784 39.04 54.336 83.264 70.464 132.352 12.16 36.928 19.328 73.472 21.312 109.568l0.64 21.632h-76.8c0-34.752-6.016-70.4-18.112-107.136a352 352 0 0 0-57.856-108.928c-62.272-79.232-146.56-118.4-256.128-118.4-151.936 0-239.104 59.904-271.36 184.32l-3.2 13.632-3.84 17.664-29.184 15.04-35.456 18.816-37.824 21.056-14.592 8.576-16.192 10.176-6.784 4.8a21.44 21.44 0 0 0-3.456 3.136c-1.088 1.28-1.408 1.408-1.152 2.048l0.64 1.536a11.712 11.712 0 0 0 3.712 4.096l7.36 4.928 8.896 5.12c13.824 7.68 23.744 14.016 31.104 20.288l4.992 4.736 17.216 18.048-11.584 28.032-9.536 20.736-4.224 8.96c-4.096 8.576-7.808 16.576-11.2 24.064l-8.96 20.736-6.656 17.472a192.768 192.768 0 0 0-4.416 14.016c-8.064 30.208-5.696 51.52 5.248 66.816 7.36 10.368 18.56 17.984 34.816 23.168 3.008 0.96 6.848 1.92 11.392 2.944l16 3.008 20.544 3.008c44.608 5.824 75.52 15.04 95.168 30.72 37.888 30.08 65.6 75.648 83.392 133.824l-0.256-0.256 22.08 73.408c-38.912 11.904-80.256-8.832-94.656-48.896-14.08-46.08-33.856-78.464-58.368-97.984-5.12-4.096-21.376-9.216-47.168-13.184l-10.112-1.472c-26.88-3.52-47.104-7.36-61.312-11.968-31.36-9.92-56.448-27.136-73.984-51.584-25.6-35.84-30.592-80.64-16.96-131.456 2.944-10.944 7.424-23.808 13.44-38.72l10.176-23.872 13.568-29.248-5.632-3.136-9.472-5.888a143.68 143.68 0 0 1-4.032-2.752l-5.184-3.84a87.232 87.232 0 0 1-25.856-33.472 77.44 77.44 0 0 1 21.632-92.544l10.88-8.512c4.096-3.008 8.704-6.144 13.824-9.472l16.832-10.56 19.968-11.648 23.296-12.928 41.152-21.952 3.648-13.056C259.072 139.776 369.728 62.848 539.712 57.856L557.44 57.6zM736 512a224 224 0 1 1 0 448 224 224 0 0 1 0-448z m0 76.8a147.2 147.2 0 1 0 0 294.4 147.2 147.2 0 0 0 0-294.4z m38.4 46.976v84.672l64 64-54.4 54.272-86.4-86.4V635.712h76.8zM525.824 218.048c20.736-1.472 39.296 0.512 55.552 6.4l7.424 3.136 8.64 0.256c11.2 0.512 21.76 1.6 31.936 3.2l14.72 2.88c17.984 4.032 33.088 9.856 44.8 18.24l5.568 4.416 4.928 0.32c26.304 2.688 49.024 16.064 66.816 38.336l6.464 8.704c17.472 25.792 24.128 54.528 19.2 84.16-5.888 35.392-30.4 59.84-67.2 70.656a148.48 148.48 0 0 1-36.48 5.632h-12.352l-3.328 2.176c-16.128 9.344-35.84 15.168-58.88 18.048l-14.272 1.344c-42.112 2.944-75.968-9.344-96-39.36a76.8 76.8 0 0 1-8.064-15.68l-0.64-1.984-13.76-3.712a178.88 178.88 0 0 1-33.792-14.016l-9.536-5.76c-40.512-26.88-50.56-71.168-31.36-121.728l0.704-1.6c16.064-36.224 53.12-55.808 106.24-62.72z m32.448 80.128c-5.376-3.456-16.896-5.056-35.136-2.688-29.376 3.84-43.072 11.136-45.632 16.832l-2.112 6.016c-4.288 13.44-2.368 18.432 4.672 23.168 10.752 7.104 28.864 13.12 54.208 17.088l33.92 5.312-1.472 34.304c-0.128 3.84 0.192 5.376 0.448 5.76 2.752 4.032 9.792 6.592 26.88 5.376 23.296-1.6 37.888-6.4 44.352-12.352l12.544-11.392 16.832 1.472c14.72 1.344 26.496 0.512 35.2-2.048 10.176-2.944 12.352-5.12 13.12-9.536a37.12 37.12 0 0 0-7.04-28.48c-7.04-10.432-12.8-13.824-19.712-13.824-6.464 0-12.352-0.256-17.6-0.832l-21.184-2.368-9.216-19.2 0.256 0.896c0 0.448-0.576 0.512-1.6 0.384L627.2 308.8a213.376 213.376 0 0 0-46.72-4.608H567.68z"/>
				</svg>
				<span v-if="badgeCount" class="task-memory-badge" aria-hidden="true">{{ badgeCount > 99 ? "99+" : badgeCount }}</span>
			</button>
		</el-tooltip>
	</div>

	<el-drawer
		v-model="drawerOpen"
		class="task-memory-drawer"
		size="min(31rem, 94vw)"
		append-to-body
		:with-header="false"
		:destroy-on-close="false"
		aria-labelledby="task-memory-drawer-title"
	>
		<header class="memory-drawer-header">
			<div>
				<span class="memory-kicker">TASK MEMORY</span>
				<h2 id="task-memory-drawer-title">任务记忆</h2>
				<p>目录自动进入下一次安全模型边界；正文仅在编辑时读取。</p>
			</div>
			<div class="memory-drawer-actions">
				<span class="memory-quota">{{ activeTotal }}/50</span>
				<button type="button" class="icon-action drawer-close" aria-label="关闭任务记忆" @click="drawerOpen = false"><Close/></button>
			</div>
		</header>

		<el-tabs v-model="activeTab" class="memory-tabs" stretch>
			<el-tab-pane label="会话记忆" name="conversation"/>
			<el-tab-pane label="Agent 任务记忆" name="agent"/>
		</el-tabs>

		<section v-if="activeTab === 'agent'" class="task-picker" aria-labelledby="task-memory-task-label">
			<label id="task-memory-task-label" class="visually-hidden" for="task-memory-task-select">选择 Agent 任务</label>
			<el-select
				id="task-memory-task-select"
				v-model="selectedTaskUuid"
				aria-labelledby="task-memory-task-label"
				:loading="tasksLoading"
				placeholder="当前会话暂无 Agent 任务"
				filterable
				popper-class="task-memory-task-select-popper"
				class="w-full"
			>
				<el-option v-for="task in tasks" :key="task.taskUuid" :value="task.taskUuid">
					<div class="task-option">
						<span>{{ task.name }} · {{ task.taskShortId }}</span>
						<small>{{ statusLabel(task.status) }}</small>
					</div>
				</el-option>
			</el-select>
			<p v-if="selectedTask">{{ selectedTask.title || selectedTask.name }} · {{ statusLabel(selectedTask.status) }}</p>
		</section>

		<div class="memory-toolbar">
			<el-input
				v-model="query"
				clearable
				placeholder="搜索名称、说明或正文"
				aria-label="搜索任务记忆"
				@keyup.enter="loadCurrent()"
				@clear="loadCurrent()"
			/>
			<el-tooltip content="刷新" placement="bottom">
				<button type="button" class="icon-action" aria-label="刷新任务记忆" @click="loadCurrent()"><Refresh/></button>
			</el-tooltip>
			<button type="button" class="primary-action" @click="newMemory"><Plus/>新增</button>
		</div>
		<div class="deleted-toggle">
			<el-switch id="task-memory-show-deleted" v-model="includeDeleted" size="small" aria-labelledby="task-memory-show-deleted-label"/>
			<label id="task-memory-show-deleted-label" for="task-memory-show-deleted">显示已删除</label>
		</div>

		<div v-loading="loading" class="memory-list" aria-live="polite">
			<div v-if="!items.length && !loading" class="memory-empty">
				<CollectionTag/>
				<strong>{{ activeTab === "agent" && !selectedTaskUuid ? "请选择 Agent 任务" : "还没有任务记忆" }}</strong>
				<p>用简短名称和说明维护目录；需要时再读取正文。</p>
			</div>
			<article
				v-for="item in items"
				:key="item.memoryUuid"
				class="memory-row"
				:class="{ deleted: item.deletedAt, reinject: item.autoReinjectCatalog }"
			>
				<button type="button" class="memory-row-main" :aria-label="`编辑 ${item.name}`" @click="editMemory(item)">
					<span class="memory-name-line">
						<strong>{{ item.name }}</strong>
						<em>v{{ item.revision }}</em>
					</span>
					<span class="memory-description">{{ item.description || "无说明" }}</span>
					<span class="memory-meta">{{ formatDate(item.updatedAt) }} · {{ formatBytes(item.sizeBytes) }}</span>
					<span class="memory-source">{{ sourceLabel(item) }}</span>
					<span class="memory-flags">
						<i :class="{ on: item.autoReinjectCatalog }">{{ item.autoReinjectCatalog ? "自动重注入" : "仅工具读取" }}</i>
						<i v-if="activeTab === 'conversation'" :class="{ on: item.visibleToAgents }">{{ item.visibleToAgents ? "Agent 可见" : "Agent 隐藏" }}</i>
						<i v-if="item.deletedAt" class="danger">已删除</i>
					</span>
				</button>
				<div class="memory-row-actions">
					<button v-if="!item.deletedAt" type="button" aria-label="编辑" @click="editMemory(item)"><EditPen/></button>
					<button v-if="!item.deletedAt" type="button" class="danger" aria-label="删除" @click="deleteMemory(item)"><Delete/></button>
					<button v-else type="button" aria-label="恢复" @click="restoreMemory(item)"><RefreshLeft/></button>
				</div>
			</article>
		</div>

		<section class="injection-preview">
			<header><strong>注入预览</strong><span>仅目录 · 无正文 · {{ previewRuntimeTokens }}/{{ previewMaxTokens }} tokens</span></header>
			<pre>{{ injectionPreview || "（当前作用域没有自动重注入条目）" }}</pre>
		</section>
		<p class="refresh-policy">打开时每 5 秒轻量刷新；修改成功、切换 tab/任务、窗口重新聚焦时立即重拉。</p>
	</el-drawer>

	<el-dialog
		v-model="editorOpen"
		class="task-memory-editor"
		:title="editorTitle"
		width="min(34rem, 94vw)"
		append-to-body
		:close-on-click-modal="false"
	>
		<div class="memory-form">
			<label id="task-memory-name-label" for="task-memory-name">名称 <span>{{ form.name.length }}/80</span></label>
			<el-input id="task-memory-name" v-model="form.name" aria-labelledby="task-memory-name-label" maxlength="80" show-word-limit placeholder="例如：部署限制"/>
			<label id="task-memory-description-label" for="task-memory-description">说明 <span>{{ form.description.length }}/200</span></label>
			<el-input id="task-memory-description" v-model="form.description" aria-labelledby="task-memory-description-label" maxlength="200" show-word-limit placeholder="目录中展示的短说明"/>
			<label id="task-memory-body-label" for="task-memory-body">正文 <span :class="{ danger: bodyBytes > 16 * 1024 }">{{ formatBytes(bodyBytes) }}/16 KiB</span></label>
			<el-input id="task-memory-body" v-model="form.body" aria-labelledby="task-memory-body-label" type="textarea" :rows="10" resize="vertical" placeholder="仅 detail/get 会读取正文"/>
			<div class="memory-form-switches">
				<div class="memory-switch-row">
					<el-switch id="task-memory-auto-reinject" v-model="form.autoReinjectCatalog" aria-labelledby="task-memory-auto-reinject-label"/>
					<label id="task-memory-auto-reinject-label" for="task-memory-auto-reinject">自动重注入目录</label>
				</div>
				<div v-if="scopeType === 'conversation'" class="memory-switch-row">
					<el-switch id="task-memory-visible-agents" v-model="form.visibleToAgents" aria-labelledby="task-memory-visible-agents-label"/>
					<label id="task-memory-visible-agents-label" for="task-memory-visible-agents">允许已获 TaskMemory 授权的 Agent 只读</label>
				</div>
			</div>
		</div>
		<template #footer>
			<el-button @click="editorOpen = false">取消</el-button>
			<el-button type="primary" :loading="saving" @click="saveMemory">保存</el-button>
		</template>
	</el-dialog>
</template>

<style scoped>
.task-memory-entry-wrap {
	position: fixed;
	top: calc(48% - 3.25rem);
	right: var(--console-float-rail-right, 1rem);
	z-index: 32;
	pointer-events: auto;
	transition: right .24s cubic-bezier(.22, 1, .36, 1);
}

.task-memory-entry {
	position: relative;
	display: grid;
	width: 2.15rem;
	height: 2.15rem;
	place-items: center;
	border: 1px solid var(--bear-line, rgba(15, 23, 42, .10));
	border-radius: 999px;
	outline: none;
	background: rgba(255, 255, 255, .68);
	color: var(--bear-muted, #71717a);
	box-shadow: 0 8px 24px rgba(15, 23, 42, .07);
	backdrop-filter: blur(14px);
	cursor: pointer;
	transition: border-color .15s ease, background .15s ease, color .15s ease, box-shadow .15s ease, transform .15s ease;
}
.task-memory-entry-icon {
	width: 1.18rem;
	height: 1.18rem;
	fill: currentColor;
	transform: translateX(.02rem);
}
.task-memory-entry:hover { color: var(--bear-ink, #18181b); transform: translateY(-1px); }
.task-memory-entry.populated { border-color: rgba(37, 99, 235, .28); color: var(--bear-accent, #2563eb); }
.task-memory-entry.active { background: var(--bear-ink, #18181b); color: #fff; box-shadow: 0 10px 28px rgba(15, 23, 42, .18); }
.task-memory-entry:focus-visible { box-shadow: 0 0 0 3px rgba(37, 99, 235, .22), 0 8px 24px rgba(15, 23, 42, .10); }
.task-memory-badge {
	position: absolute;
	top: -.38rem;
	right: -.38rem;
	display: grid;
	min-width: 1rem;
	height: 1rem;
	place-items: center;
	border: 2px solid #fff;
	border-radius: 999px;
	background: var(--bear-accent, #2563eb);
	padding: 0 .2rem;
	color: #fff;
	font-size: 10px;
	font-weight: 750;
	line-height: 1;
}

.memory-drawer-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; padding-bottom: .8rem; border-bottom: 1px solid var(--bear-line, rgba(15,23,42,.1)); }
.memory-drawer-actions { display: flex; flex: 0 0 auto; align-items: center; gap: .4rem; }
.drawer-close { width: 2rem; }
.visually-hidden { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; clip-path: inset(50%) !important; white-space: nowrap !important; }
.memory-kicker { color: var(--bear-accent, #2563eb); font-size: 11px; font-weight: 750; letter-spacing: .12em; }
.memory-drawer-header h2 { margin: .15rem 0 0; color: var(--bear-ink, #18181b); font-size: 20px; font-weight: 700; letter-spacing: -.025em; }
.memory-drawer-header p { margin: .28rem 0 0; color: var(--bear-muted, #71717a); font-size: 13px; line-height: 1.5; }
.memory-quota { flex: 0 0 auto; border-radius: 999px; background: #f4f4f5; padding: .3rem .58rem; color: #52525b; font-size: 12px; font-weight: 700; }
.memory-tabs { margin-top: .3rem; }
.task-picker { margin: .1rem 0 .75rem; border: 1px solid var(--bear-line, rgba(15,23,42,.1)); border-radius: .75rem; background: rgba(250,250,250,.82); padding: .6rem; }
.task-picker p { margin: .42rem .15rem 0; overflow: hidden; color: var(--bear-muted, #71717a); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.task-option { display: flex; width: 100%; justify-content: space-between; gap: .75rem; font-size: 13px; }
.task-option small { color: #71717a; font-size: 12px; }
.memory-toolbar { display: grid; grid-template-columns: minmax(0, 1fr) 2.15rem auto; gap: .42rem; align-items: center; }
.icon-action, .primary-action { display: inline-flex; height: 2rem; align-items: center; justify-content: center; gap: .28rem; border: 1px solid var(--bear-line, rgba(15,23,42,.1)); border-radius: .58rem; background: #fff; color: #52525b; cursor: pointer; }
.icon-action { width: 2.15rem; padding: 0; }
.icon-action svg, .primary-action svg { width: .8rem; height: .8rem; }
.primary-action { border-color: var(--bear-ink, #18181b); background: var(--bear-ink, #18181b); padding: 0 .78rem; color: #fff; font-size: 13px; font-weight: 650; }
.icon-action:hover { background: #f4f4f5; color: #18181b; }
.icon-action:focus-visible, .primary-action:focus-visible, .memory-row-main:focus-visible, .memory-row-actions button:focus-visible { outline: 2px solid rgba(37,99,235,.42); outline-offset: 2px; }
.deleted-toggle { display: inline-flex; align-items: center; gap: .46rem; margin: .65rem 0; color: var(--bear-muted, #71717a); font-size: 12px; }
.memory-list { min-height: 8rem; max-height: calc(100vh - 23rem); overflow-y: auto; padding-right: .12rem; scrollbar-width: thin; }
.memory-empty { display: grid; min-height: 10rem; place-items: center; align-content: center; border: 1px dashed rgba(15,23,42,.14); border-radius: .8rem; color: #a1a1aa; text-align: center; }
.memory-empty svg { width: 1.4rem; margin-bottom: .45rem; }
.memory-empty strong { color: #3f3f46; font-size: 14px; }
.memory-empty p { margin: .3rem 0 0; font-size: 12px; line-height: 1.5; }
.memory-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .35rem; margin-bottom: .42rem; border: 1px solid var(--bear-line, rgba(15,23,42,.1)); border-left: 3px solid #d4d4d8; border-radius: 0 .72rem .72rem 0; background: rgba(255,255,255,.88); box-shadow: 0 1px 2px rgba(15,23,42,.025); }
.memory-row.reinject { border-left-color: var(--bear-accent, #2563eb); }
.memory-row.deleted { opacity: .62; }
.memory-row-main { min-width: 0; border: 0; outline: none; background: transparent; padding: .62rem .35rem .62rem .68rem; text-align: left; cursor: pointer; }
.memory-name-line { display: flex; min-width: 0; align-items: center; gap: .42rem; }
.memory-name-line strong { min-width: 0; overflow: hidden; color: var(--bear-ink, #18181b); font-size: 14px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.memory-name-line em { flex: 0 0 auto; border-radius: 999px; background: #f4f4f5; padding: .16rem .4rem; color: #52525b; font-size: 11px; font-style: normal; }
.memory-description { display: block; margin-top: .28rem; overflow: hidden; color: #52525b; font-size: 13px; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.memory-meta, .memory-source { display: block; margin-top: .28rem; overflow: hidden; color: #71717a; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.memory-flags { display: flex; flex-wrap: wrap; gap: .24rem; margin-top: .38rem; }
.memory-flags i { border-radius: 999px; background: #f4f4f5; padding: .2rem .44rem; color: #52525b; font-size: 11px; font-style: normal; }
.memory-flags i.on { background: var(--bear-accent-soft, #eff6ff); color: var(--bear-accent, #2563eb); }
.memory-flags i.danger { background: #fff1f2; color: #e11d48; }
.memory-row-actions { display: flex; flex-direction: column; gap: .2rem; padding: .45rem .42rem .45rem 0; }
.memory-row-actions button { display: grid; width: 1.55rem; height: 1.55rem; place-items: center; border: 0; border-radius: .42rem; background: transparent; color: #a1a1aa; cursor: pointer; }
.memory-row-actions button:hover { background: #f4f4f5; color: #3f3f46; }
.memory-row-actions button.danger:hover { background: #fff1f2; color: #e11d48; }
.memory-row-actions svg { width: .75rem; height: .75rem; }
.injection-preview { margin-top: .75rem; border: 1px solid var(--bear-line, rgba(15,23,42,.1)); border-radius: .75rem; background: #fafafa; overflow: hidden; }
.injection-preview header { display: flex; justify-content: space-between; padding: .5rem .62rem; border-bottom: 1px solid var(--bear-line, rgba(15,23,42,.1)); }
.injection-preview strong { color: #3f3f46; font-size: 12px; }
.injection-preview span { color: #71717a; font-size: 11px; }
.injection-preview pre { max-height: 9.5rem; margin: 0; overflow: auto; padding: .58rem .62rem; color: #52525b; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.refresh-policy { margin: .58rem .1rem 0; color: #71717a; font-size: 11px; line-height: 1.5; }
.memory-form { display: grid; gap: .42rem; }
.memory-form > label { display: flex; justify-content: space-between; margin-top: .38rem; color: #3f3f46; font-size: 13px; font-weight: 620; }
.memory-form > label span { color: #a1a1aa; font-weight: 500; }
.memory-form > label span.danger { color: #e11d48; }
.memory-form-switches { display: grid; gap: .45rem; margin-top: .65rem; border-top: 1px solid rgba(15,23,42,.1); padding-top: .7rem; }
.memory-switch-row { display: flex; align-items: center; gap: .55rem; }
.memory-switch-row label { color: #3f3f46; font-size: 13px; line-height: 1.45; cursor: pointer; }

:global(.task-memory-drawer .el-drawer__body) { padding: 1rem 1rem 1.25rem; }
:global(.task-memory-drawer.el-drawer) { background: rgba(255,255,255,.98); box-shadow: -18px 0 58px rgba(15,23,42,.16); }
:global(.task-memory-drawer .el-tabs__item) { font-size: 13px; font-weight: 620; }
:global(.task-memory-drawer .el-input__inner),
:global(.task-memory-drawer .el-select__placeholder),
:global(.task-memory-drawer .el-select__selected-item) { font-size: 13px; }
:global(.task-memory-editor.el-dialog) { border-radius: 1rem; overflow: hidden; }
:global(.task-memory-editor .el-dialog__title) { color: #18181b; font-size: 18px; font-weight: 700; }
:global(.task-memory-editor .el-input__inner),
:global(.task-memory-editor .el-textarea__inner) { color: #27272a; font-size: 14px; line-height: 1.6; }
:global(.task-memory-editor .el-input__count),
:global(.task-memory-editor .el-input__count-inner) { font-size: 11px; }
:global(.task-memory-editor .el-button) { font-size: 13px; }
:global(.task-memory-task-select-popper .el-select-dropdown__item) { min-height: 38px; font-size: 13px; line-height: 1.4; }
:global(.task-memory-confirm .el-message-box__title) { font-size: 17px; font-weight: 700; }
:global(.task-memory-confirm .el-message-box__message) { color: #3f3f46; font-size: 14px; line-height: 1.6; }
:global(.task-memory-confirm .el-button) { font-size: 13px; }

@media (max-width: 760px) {
	.task-memory-entry-wrap {
		top: auto;
		right: var(--console-float-rail-right);
		bottom: calc(var(--console-float-rail-bottom) + var(--console-float-control-size) + var(--console-float-control-gap));
	}
	.task-memory-entry { width: var(--console-float-control-size); height: var(--console-float-control-size); background: rgba(255,255,255,.9); }
	.memory-list { max-height: calc(100dvh - 25rem); }
	.memory-toolbar { grid-template-columns: minmax(0, 1fr) 2.15rem; }
	.primary-action { grid-column: 1 / -1; }
}

@media (prefers-reduced-motion: reduce) {
	.task-memory-entry { transition: none; }
}
</style>
