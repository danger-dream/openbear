<script setup>
import {computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {ArrowLeft, ArrowRight, Close, CollectionTag, Delete, EditPen, Plus, Refresh, RefreshLeft, Search} from "@element-plus/icons-vue";
import InteractionMarkdown from "./InteractionMarkdown.vue";
import {Api, apiError} from "../../api.js";
import {createTaskMemoryRequestGate} from "./taskMemoryRequestGate.js";
import {taskMemoryInjectionPreview, taskMemoryInjectionUsage} from "./taskMemoryInjection.js";
import {
	TASK_MEMORY_CHANGED_EVENT_KEY,
	createTaskMemoryBadgeState,
	createTaskMemoryChangedEventGate,
	taskMemoryMutationRecovery,
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
const previewShortBodyMaxChars = ref(0);
const previewIncludedCount = ref(null);
const previewOmittedCount = ref(0);
const detail = ref(null);
const detailLoading = ref(false);
const detailError = ref("");
const detailChanged = ref(false);
const previewOpen = ref(false);
const page = ref(1);
const total = ref(0);
const pageSize = 50;
const editorBaseline = ref("");
const detailHeading = ref(null);
let lastReadButton = null;
let searchTimer = 0;
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
const editorTitle = computed(() => editorMode.value === "create" ? "新增记忆" : "编辑记忆");
const canCreate = computed(() => Boolean(usableConversationUuid.value) && (scopeType.value === "conversation" || Boolean(selectedTaskUuid.value)));
const emptyTitle = computed(() => activeTab.value === "agent" && !selectedTaskUuid.value
	? "暂无 Agent 任务"
	: query.value.trim() ? "没有找到匹配的记忆" : "还没有记忆");
const emptyDescription = computed(() => activeTab.value === "agent" && !selectedTaskUuid.value
	? "此会话中的 Agent 开始工作后，可在这里选择任务并查看记忆。"
	: query.value.trim() ? "试试其他关键词，或清空搜索查看全部。" : "保存本会话的资料和执行偏好，不重复记录任务进度。");

function taskLabel(task) {
	return String(task?.title || task?.name || "未命名任务");
}

function taskSearchLabel(task) {
	return `${taskLabel(task)} · ${task.name || "Agent"} · ${statusLabel(task.status)} · ${formatDate(task.updatedAt)}`;
}

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
	detail.value = null;
	detailLoading.value = false;
	detailError.value = "";
	detailChanged.value = false;
	previewOpen.value = false;
	page.value = 1;
	total.value = 0;
	window.clearTimeout(searchTimer);
	if (resetActiveTotal) activeTotal.value = 0;
	injectionPreview.value = "";
	previewRuntimeTokens.value = 0;
	previewMaxTokens.value = 1500;
	previewShortBodyMaxChars.value = 0;
	previewIncludedCount.value = null;
	previewOmittedCount.value = 0;
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
	const actor = String(item?.createdBy || "");
	if (actor.startsWith("web:")) return "手动创建";
	if (actor === "main-controller") return "助手记录";
	if (actor.startsWith("agent:")) return "Agent 记录";
	return "系统记录";
}

async function loadPreview() {
	const token = beginRequest("preview");
	if (!token.conversationUuid || (token.scopeType === "agent_task" && !token.taskUuid)) {
		injectionPreview.value = "";
		previewRuntimeTokens.value = 0;
		previewShortBodyMaxChars.value = 0;
		previewIncludedCount.value = null;
		previewOmittedCount.value = 0;
		return;
	}
	try {
		const data = await Api.taskMemoryPreview(token.conversationUuid, requestScopeParams(token));
		if (!requestIsCurrent(token)) return;
		const preview = taskMemoryInjectionPreview(data);
		injectionPreview.value = preview.text;
		previewRuntimeTokens.value = preview.tokens;
		previewMaxTokens.value = preview.maxTokens;
		previewShortBodyMaxChars.value = preview.shortBodyMaxChars;
		previewIncludedCount.value = preview.includedCount;
		previewOmittedCount.value = preview.omittedCount;
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		injectionPreview.value = "";
		previewRuntimeTokens.value = 0;
		previewShortBodyMaxChars.value = 0;
		previewIncludedCount.value = null;
		previewOmittedCount.value = 0;
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
	const requestPage = page.value;
	const matchesFilters = () => requestIsCurrent(token) && requestQuery === query.value.trim()
		&& requestIncludeDeleted === includeDeleted.value && requestPage === page.value;
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
			offset: (requestPage - 1) * pageSize,
			limit: pageSize,
		}));
		if (!matchesFilters()) return;
		total.value = Number(data?.total || 0);
		if (requestPage > 1 && (requestPage - 1) * pageSize >= total.value) {
			page.value = Math.max(1, Math.ceil(total.value / pageSize));
			return;
		}
		items.value = Array.isArray(data?.items) ? data.items : [];
		const latestDetail = items.value.find(item => item.memoryUuid === detail.value?.memoryUuid);
		if (latestDetail && latestDetail.revision !== detail.value.revision) detailChanged.value = true;
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

function searchMemories() {
	window.clearTimeout(searchTimer);
	if (page.value !== 1) page.value = 1;
	else if (drawerOpen.value) void loadCurrent();
}

async function viewMemory(item, event) {
	const token = beginRequest("detail");
	if (event?.currentTarget) lastReadButton = event.currentTarget;
	detail.value = {...item};
	detailLoading.value = true;
	detailError.value = "";
	detailChanged.value = false;
	try {
		const data = await Api.taskMemory(token.conversationUuid, item.memoryUuid,
			requestScopeParams(token, {includeDeleted: 1}));
		if (!requestIsCurrent(token)) return;
		detail.value = data.memory;
		if (event) {
			await nextTick();
			if (requestIsCurrent(token)) detailHeading.value?.focus();
		}
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		detailError.value = "暂时无法读取正文，请重试。";
	} finally {
		if (requestIsCurrent(token)) detailLoading.value = false;
	}
}

function backToList() {
	beginRequest("detail");
	detail.value = null;
	detailLoading.value = false;
	detailError.value = "";
	void nextTick(() => { if (!detail.value && drawerOpen.value && lastReadButton?.isConnected) lastReadButton.focus(); });
}

async function closeEditor() {
	if (saving.value) return;
	const token = beginRequest("editor-close");
	if (JSON.stringify(form.value) !== editorBaseline.value) {
		try {
			await ElMessageBox.confirm("尚有未保存的修改，确定放弃吗？", "放弃修改", {
				confirmButtonText: "放弃修改", cancelButtonText: "继续编辑", customClass: "task-memory-confirm",
			});
		} catch { return; }
	}
	if (requestIsCurrent(token)) editorOpen.value = false;
}

async function openDrawer() {
	if (!usableConversationUuid.value) return;
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
	// Refresh read channels without invalidating an in-flight save or erasing its draft.
	if (detail.value?.memoryUuid === event.memoryUuid) detailChanged.value = true;
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
	beginRequest("detail");
	form.value = emptyForm();
	editorBaseline.value = JSON.stringify(form.value);
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
		if (detail.deletedAt) {
			ElMessage.warning("这条记忆已删除，请恢复后再编辑。");
			await viewMemory(detail);
			return;
		}
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
		editorBaseline.value = JSON.stringify(form.value);
		editorOpen.value = true;
		await nextTick();
	} catch (error) {
		if (!requestIsCurrent(token)) return;
		if (!(await recoverStaleMutation(error, token))) ElMessage.error(apiError(error));
	}
}

async function saveMemory() {
	if (saving.value || !canCreate.value) return;
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
		const result = mode === "create"
			? await Api.createTaskMemory(token.conversationUuid, payload)
			: await Api.updateTaskMemory(token.conversationUuid, memoryUuid, {
				...payload, revision: Number(form.value.revision || 0),
			});
		if (!requestIsCurrent(token)) return;
		editorOpen.value = false;
		if (result?.memory) {
			detail.value = result.memory;
			detailChanged.value = false;
			detailError.value = "";
		}
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
		if (detail.value?.memoryUuid === item.memoryUuid) backToList();
		ElMessage.success("已删除，可在已删除记录中恢复");
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
		if (detail.value?.memoryUuid === item.memoryUuid) await viewMemory({...item, deletedAt: 0});
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
watch(query, () => {
	window.clearTimeout(searchTimer);
	searchTimer = window.setTimeout(searchMemories, 250);
});
watch(page, () => {
	if (drawerOpen.value) void loadCurrent();
});
watch(includeDeleted, searchMemories);
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
defineExpose({open: openDrawer});
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
		size="min(42rem, 100vw)"
		append-to-body
		:with-header="false"
		:destroy-on-close="false"
		aria-labelledby="task-memory-drawer-title"
	>
		<header class="memory-drawer-header">
			<div>
				<h2 id="task-memory-drawer-title">任务记忆</h2>
				<p>会话专用资料与执行偏好；任务进度由任务与 Plan 管理。</p>
			</div>
			<button type="button" class="icon-action drawer-close" aria-label="关闭任务记忆" @click="drawerOpen = false"><Close/></button>
		</header>

		<el-tabs v-model="activeTab" class="memory-tabs" stretch>
			<el-tab-pane label="会话记忆" name="conversation"/>
			<el-tab-pane label="Agent 记忆" name="agent"/>
		</el-tabs>

		<section v-if="activeTab === 'agent'" class="task-picker" aria-labelledby="task-memory-task-label">
			<label id="task-memory-task-label" for="task-memory-task-select">查看任务</label>
			<el-select
				id="task-memory-task-select"
				v-model="selectedTaskUuid"
				aria-labelledby="task-memory-task-label"
				:loading="tasksLoading"
				placeholder="暂无 Agent 任务"
				filterable
				popper-class="task-memory-task-select-popper"
				class="memory-task-select"
			>
				<template #label><span>{{ taskLabel(selectedTask) }}</span></template>
				<el-option v-for="task in tasks" :key="task.taskUuid" :value="task.taskUuid" :label="taskSearchLabel(task)">
					<div class="task-option">
						<strong>{{ taskLabel(task) }}</strong>
						<small><span>{{ task.name || 'Agent' }} · {{ statusLabel(task.status) }}</span><time>{{ formatDate(task.updatedAt) }}</time></small>
					</div>
				</el-option>
			</el-select>
			<p v-if="selectedTask">{{ selectedTask.name || 'Agent' }}<span>·</span>{{ statusLabel(selectedTask.status) }}<span>·</span>{{ formatDate(selectedTask.updatedAt) }}</p>
		</section>

		<section v-show="!detail" class="memory-browse" aria-label="记忆列表">
			<div class="memory-toolbar">
				<el-input v-model="query" clearable :prefix-icon="Search" placeholder="搜索名称、说明或正文" aria-label="搜索任务记忆" @keyup.enter="searchMemories"/>
				<button type="button" class="icon-action" :class="{ refreshing: loading }" aria-label="刷新任务记忆" :disabled="loading" @click="loadCurrent()"><Refresh/></button>
				<el-button type="primary" class="primary-action" :icon="Plus" :disabled="!canCreate" @click="newMemory">新增</el-button>
			</div>
			<div class="memory-list-summary">
				<span>{{ query.trim() ? `找到 ${total} 条` : `${activeTotal} 条记忆` }}</span>
				<div class="deleted-toggle">
					<el-switch id="task-memory-show-deleted" v-model="includeDeleted" size="small" aria-labelledby="task-memory-show-deleted-label"/>
					<label id="task-memory-show-deleted-label" for="task-memory-show-deleted">显示已删除</label>
				</div>
			</div>
			<div class="memory-list" :aria-busy="loading ? 'true' : 'false'">
				<div v-if="!items.length && !loading" class="memory-empty" role="status">
					<CollectionTag/>
					<strong>{{ emptyTitle }}</strong>
					<p>{{ emptyDescription }}</p>
					<button v-if="query.trim()" type="button" class="text-action" @click="query = ''">清空搜索</button>
				</div>
				<article v-for="item in items" :key="item.memoryUuid" class="memory-row" :class="{ deleted: item.deletedAt }">
					<button type="button" class="memory-row-main" :aria-label="`查看 ${item.name}`" @click="viewMemory(item, $event)">
						<span class="memory-name-line"><strong>{{ item.name }}</strong><ArrowRight/></span>
						<span v-if="item.description" class="memory-description">{{ item.description }}</span>
						<span class="memory-meta">{{ sourceLabel(item) }}<span>·</span>{{ formatDate(item.updatedAt) }} 更新</span>
						<span class="memory-flags">
							<i v-if="item.deletedAt" class="danger">已删除</i>
							<i v-else-if="item.autoReinjectCatalog" class="on">自动提供给模型</i>
							<i v-else>按需读取</i>
							<i v-if="activeTab === 'conversation' && item.visibleToAgents">Agent 可读取</i>
						</span>
					</button>
					<div class="memory-row-actions">
						<button v-if="!item.deletedAt" type="button" :aria-label="`编辑 ${item.name}`" title="编辑" @click="editMemory(item)"><EditPen/></button>
						<button v-if="!item.deletedAt" type="button" class="danger" :aria-label="`删除 ${item.name}`" title="删除" @click="deleteMemory(item)"><Delete/></button>
						<button v-else type="button" :aria-label="`恢复 ${item.name}`" title="恢复" @click="restoreMemory(item)"><RefreshLeft/></button>
					</div>
				</article>
			</div>
			<div v-if="total > pageSize" class="memory-pagination">
				<span>共 {{ total }} 条</span>
				<el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="prev, pager, next" :pager-count="5" small/>
			</div>
		</section>

		<section v-if="detail" class="memory-detail" aria-label="记忆详情" :aria-busy="detailLoading ? 'true' : 'false'">
			<div class="memory-detail-toolbar">
				<button type="button" class="text-action back-action" @click="backToList"><ArrowLeft/>返回列表</button>
				<el-button v-if="detail.deletedAt" type="primary" class="primary-action" :icon="RefreshLeft" @click="restoreMemory(detail)">恢复记忆</el-button>
				<el-button v-else type="primary" class="primary-action" :icon="EditPen" :disabled="detailLoading || Boolean(detailError)" @click="editMemory(detail)">编辑</el-button>
			</div>
			<div class="memory-detail-scroll" tabindex="0" aria-label="记忆正文">
				<h3 ref="detailHeading" tabindex="-1">{{ detail.name }}</h3>
				<p v-if="detail.description" class="memory-detail-description">{{ detail.description }}</p>
				<div class="memory-detail-meta"><span>{{ sourceLabel(detail) }}</span><span>{{ formatDate(detail.updatedAt) }} 更新</span><span v-if="detail.deletedAt" class="danger">已删除</span></div>
				<div v-if="detailChanged" class="memory-notice" role="status">这条记忆有新版本。<button type="button" class="text-action" @click="viewMemory(detail)">查看最新内容</button></div>
				<div v-if="detailError" class="memory-notice" role="alert">{{ detailError }}<button type="button" class="text-action" @click="viewMemory(detail)">重新加载</button></div>
				<div v-else-if="detailLoading" class="memory-reading-placeholder" aria-label="正在读取正文"></div>
				<InteractionMarkdown v-else-if="detail.body" class="memory-body" :text="detail.body"/>
				<p v-else class="memory-no-body">这条记忆没有正文。</p>
				<details class="memory-technical">
					<summary>详细信息</summary>
					<dl>
						<dt>使用方式</dt><dd>{{ taskMemoryInjectionUsage(detail, previewShortBodyMaxChars) }}</dd>
						<template v-if="activeTab === 'conversation'"><dt>Agent 读取</dt><dd>{{ detail.visibleToAgents ? '允许同会话中已获授权的 Agent 读取' : '不向 Agent 共享' }}</dd></template>
						<dt>版本 / 大小</dt><dd>第 {{ detail.revision }} 版 · {{ formatBytes(detail.sizeBytes) }}</dd>
						<dt>记忆标识</dt><dd><code>{{ detail.memoryUuid }}</code></dd>
						<template v-if="detail.sourceTurnUuid"><dt>来源轮次</dt><dd><code>{{ detail.sourceTurnUuid }}</code></dd></template>
						<template v-if="detail.sourceRunUuid"><dt>来源运行</dt><dd><code>{{ detail.sourceRunUuid }}</code></dd></template>
					</dl>
				</details>
			</div>
		</section>

		<section v-if="!detail" class="injection-preview">
			<button type="button" class="preview-toggle" :aria-expanded="previewOpen ? 'true' : 'false'" aria-controls="task-memory-catalog-preview" @click="previewOpen = !previewOpen">
				<span>模型可见内容预览<span v-if="previewOmittedCount"> · 省略 {{ previewOmittedCount }} 条</span></span><small>约 {{ previewRuntimeTokens }} tokens</small><ArrowRight :class="{ expanded: previewOpen }"/>
			</button>
			<div v-if="previewOpen" id="task-memory-catalog-preview" class="preview-content">
				<p v-if="previewShortBodyMaxChars">按当前注入规则预览：不超过 {{ previewShortBodyMaxChars }} 字符的短正文直接提供，长资料按需读取；总预算 {{ previewMaxTokens }} tokens。</p>
				<p v-else>当前服务提供名称与说明，正文按需读取；总预算 {{ previewMaxTokens }} tokens。</p>
				<p v-if="previewIncludedCount !== null">已纳入 {{ previewIncludedCount }} 条<span v-if="previewOmittedCount">，因预算省略 {{ previewOmittedCount }} 条；未显示不代表没有记录</span>。Agent 可见内容也可能包含已共享的会话便笺。</p>
				<p v-if="scopeType !== 'conversation'">此处按允许 TaskMemory 工具预览。实际私有便笺是否提供取决于本轮有效授权；已共享的会话便笺不依赖此工具授权。</p>
				<pre tabindex="0" aria-label="模型可见原文">{{ injectionPreview || '暂无自动提供的内容' }}</pre>
			</div>
		</section>
	</el-drawer>

	<el-dialog
		v-model="editorOpen"
		class="task-memory-editor"
		:title="editorTitle"
		width="min(42rem, 96vw)"
		append-to-body
		:close-on-click-modal="false"
		:before-close="closeEditor"
	>
		<form class="memory-form" @submit.prevent="saveMemory">
			<label id="task-memory-name-label" for="task-memory-name">名称</label>
			<el-input id="task-memory-name" v-model="form.name" :disabled="saving" aria-labelledby="task-memory-name-label" maxlength="80" show-word-limit placeholder="例如：界面偏好、本次测试服务器"/>
			<label id="task-memory-description-label" for="task-memory-description">简短说明 <span>帮助定位较长资料，不必重复短正文</span></label>
			<el-input id="task-memory-description" v-model="form.description" :disabled="saving" aria-labelledby="task-memory-description-label" maxlength="200" show-word-limit type="textarea" :rows="2" resize="none" placeholder="用一两句话概括重点"/>
			<label id="task-memory-body-label" for="task-memory-body">正文 <span :class="{ danger: bodyBytes > 16 * 1024 }">{{ formatBytes(bodyBytes) }} / 16 KiB · 支持 Markdown</span></label>
			<el-input id="task-memory-body" v-model="form.body" :disabled="saving" aria-labelledby="task-memory-body-label" type="textarea" :rows="10" resize="none" placeholder="记录本会话使用的资料或持续偏好，例如：界面保持 macOS 风格和当前字号。不记录调查过程、测试流水或任务进度。"/>
			<div class="memory-form-switches">
				<div class="memory-switch-row">
					<div><label id="task-memory-auto-reinject-label" for="task-memory-auto-reinject">自动向模型提供</label><p v-if="previewShortBodyMaxChars">短正文（≤ {{ previewShortBodyMaxChars }} 字符）直接提供，长资料按需读取；受总预算限制。</p><p v-else>按当前服务的注入规则提供；实际内容可在预览中查看。</p></div>
					<el-switch id="task-memory-auto-reinject" v-model="form.autoReinjectCatalog" :disabled="saving" aria-labelledby="task-memory-auto-reinject-label"/>
				</div>
				<div v-if="scopeType === 'conversation'" class="memory-switch-row">
					<div><label id="task-memory-visible-agents-label" for="task-memory-visible-agents">允许 Agent 读取</label><p>共享便笺作为同会话 Agent 的工作输入；不授予记忆工具或修改权限。</p></div>
					<el-switch id="task-memory-visible-agents" v-model="form.visibleToAgents" :disabled="saving" aria-labelledby="task-memory-visible-agents-label"/>
				</div>
			</div>
		</form>
		<template #footer>
			<el-button :disabled="saving" @click="closeEditor">取消</el-button>
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

.memory-drawer-header { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; flex-shrink:0; }
.memory-drawer-header h2 { margin:0; color:var(--bear-ink, #18181b); font-size:18px; font-weight:650; line-height:1.5; }
.memory-drawer-header p { margin:.3rem 0 0; color:var(--tm-muted); font-size:12px; line-height:1.6; }
.memory-tabs { flex-shrink:0; margin-top:1rem; }
.task-picker { flex-shrink:0; margin:0 0 1rem; padding:.75rem; border-radius:.75rem; background:var(--tm-soft); }
.task-picker > label { display:block; margin-bottom:.4rem; color:var(--tm-muted); font-size:12px; }
.memory-task-select { width:100%; }
.task-picker p { display:flex; flex-wrap:wrap; gap:.4rem; margin:.5rem 0 0; font-size:12px; color:var(--tm-muted); }
.memory-browse { display:flex; flex-direction:column; flex:1; min-height:0; }
.memory-toolbar { display:flex; align-items:center; gap:.5rem; flex-shrink:0; }
.memory-toolbar > .el-input { flex:1; min-width:0; }
.icon-action { display:inline-flex; flex-shrink:0; height:2.1rem; align-items:center; justify-content:center; gap:.35rem; border:1px solid var(--tm-line); border-radius:.5rem; background:transparent; color:var(--tm-muted); cursor:pointer; font:inherit; font-size:13px; }
.icon-action { width:2.1rem; padding:0; }
.icon-action svg, .primary-action svg, .text-action svg { width:15px; height:15px; }
.primary-action.el-button { flex-shrink:0; height:34px; margin:0; padding:0 .8rem; border-radius:8px; font-family:inherit; font-size:13px; font-weight:500; }
.icon-action:hover { background:var(--tm-soft); color:var(--bear-ink); }
button:disabled:not(.el-button) { opacity:.5; cursor:not-allowed; }
button:focus-visible, summary:focus-visible, .memory-detail-scroll:focus-visible { outline:2px solid var(--bear-accent, #2563eb); outline-offset:2px; }
.memory-list-summary { display:flex; justify-content:space-between; align-items:center; gap:.5rem; flex-shrink:0; padding:.7rem 0; color:var(--tm-muted); font-size:12px; }
.deleted-toggle { display:flex; align-items:center; gap:.45rem; }
.deleted-toggle label { cursor:pointer; }
.memory-list { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; scrollbar-width:thin; padding:0 .25rem .5rem 0; }
.memory-empty { display:flex; min-height:13rem; height:100%; flex-direction:column; justify-content:center; align-items:center; padding:1rem; text-align:center; color:var(--tm-muted); box-sizing:border-box; }
.memory-empty > svg { width:30px; height:30px; margin-bottom:.85rem; opacity:.55; }
.memory-empty strong { color:var(--bear-ink); font-size:14px; font-weight:550; }
.memory-empty p { max-width:23rem; font-size:13px; line-height:1.75; margin:.5rem 0; }
.memory-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:.4rem; margin-bottom:.5rem; border:1px solid var(--tm-line); border-radius:.65rem; background:var(--tm-surface); }
.memory-row:hover { border-color:var(--tm-hover-line); background:var(--tm-soft); }
.memory-row.deleted .memory-name-line strong { color:var(--tm-muted); }
.memory-row-main { display:block; min-width:0; border:0; background:transparent; padding:.9rem 0 .9rem .9rem; text-align:left; cursor:pointer; font:inherit; }
.memory-name-line { display:flex; align-items:flex-start; gap:.5rem; }
.memory-name-line strong { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; flex:1; min-width:0; overflow:hidden; overflow-wrap:anywhere; color:var(--bear-ink, #18181b); font-size:14px; font-weight:600; line-height:1.5; }
.memory-name-line > svg { width:13px; height:13px; flex-shrink:0; margin-top:4px; color:var(--tm-muted); opacity:.6; }
.memory-description { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; margin-top:.4rem; overflow:hidden; overflow-wrap:anywhere; color:var(--tm-muted); font-size:13px; line-height:1.65; }
.memory-meta { display:flex; flex-wrap:wrap; align-items:baseline; gap:.35rem; margin-top:.6rem; color:var(--tm-muted); font-size:12px; line-height:1.5; }
.memory-flags { display:flex; flex-wrap:wrap; gap:.45rem; margin-top:.5rem; }
.memory-flags i { border-radius:.3rem; padding:.15rem .4rem; color:var(--tm-muted); background:var(--tm-soft); font-size:11px; line-height:1.6; font-style:normal; }
.memory-flags i.on { color:var(--bear-accent, #2563eb); background:var(--tm-accent-soft); }
.danger, .memory-flags i.danger { color:var(--tm-danger); }
.memory-row-actions { display:flex; flex-direction:column; gap:.25rem; padding:.65rem .5rem; }
.memory-row-actions button { display:grid; place-items:center; width:30px; height:30px; padding:0; border:0; border-radius:.4rem; background:transparent; color:var(--tm-muted); cursor:pointer; }
.memory-row-actions svg { width:15px; height:15px; }
.memory-row-actions button:hover { color:var(--bear-accent, #2563eb); background:var(--tm-accent-soft); }
.memory-row-actions button.danger:hover { color:var(--tm-danger); background:var(--tm-soft); }
.memory-pagination { display:flex; align-items:center; justify-content:space-between; gap:.5rem; flex-shrink:0; padding:.6rem 0; font-size:12px; color:var(--tm-muted); }
.memory-detail { display:flex; flex-direction:column; flex:1; min-height:0; }
.memory-detail-toolbar { display:flex; align-items:center; justify-content:space-between; gap:.5rem; flex-shrink:0; padding:0 0 1rem; }
.text-action { display:inline-flex; align-items:center; gap:.3rem; border:0; background:transparent; padding:.3rem 0; font:inherit; font-size:13px; color:var(--bear-accent, #2563eb); cursor:pointer; }
.back-action { color:var(--tm-muted); }
.memory-detail-scroll { min-height:0; overflow:auto; overscroll-behavior:contain; scrollbar-width:thin; padding:0 .4rem 1rem 0; }
.memory-detail h3 { margin:0; color:var(--bear-ink); font-size:18px; font-weight:600; line-height:1.6; overflow-wrap:anywhere; }
.memory-detail-description { margin:.55rem 0 0; color:var(--tm-muted); font-size:13px; line-height:1.8; overflow-wrap:anywhere; }
.memory-detail-meta { display:flex; flex-wrap:wrap; gap:.7rem; margin:.8rem 0 1.25rem; color:var(--tm-muted); font-size:12px; }
.memory-body { padding-top:1.25rem; border-top:1px solid var(--tm-line); font-size:14px; line-height:1.85; color:var(--bear-ink, #27272a); }
.memory-body :deep(h1), .memory-body :deep(h2), .memory-body :deep(h3) { font-size:16px; font-weight:600; line-height:1.7; }
.memory-no-body { color:var(--tm-muted); font-size:13px; padding:1rem 0; }
.memory-notice { margin:.75rem 0; padding:.6rem .8rem; border-radius:.5rem; background:var(--tm-soft); color:var(--tm-muted); font-size:13px; line-height:1.7; }
.memory-notice button { margin-left:.6rem; }
.memory-reading-placeholder { min-height:12rem; border-top:1px solid var(--tm-line); }
.memory-technical { margin-top:1.5rem; border-top:1px solid var(--tm-line); font-size:12px; color:var(--tm-muted); }
.memory-technical summary { padding:1rem 0 .6rem; cursor:pointer; }
.memory-technical dl { display:grid; grid-template-columns:5.5rem minmax(0,1fr); gap:.65rem 1rem; line-height:1.7; }
.memory-technical dt, .memory-technical dd { margin:0; overflow-wrap:anywhere; }
.memory-technical code { font-size:11px; }
.injection-preview { flex-shrink:0; margin-top:.35rem; border-top:1px solid var(--tm-line); }
.preview-toggle { display:flex; align-items:center; gap:.6rem; width:100%; padding:.85rem 0 .15rem; border:0; background:transparent; color:var(--tm-muted); font:inherit; font-size:12px; text-align:left; cursor:pointer; }
.preview-toggle small { margin-left:auto; font-size:11px; font-variant-numeric:tabular-nums; }
.preview-toggle svg { width:12px; height:12px; }
.preview-toggle svg.expanded { transform:rotate(90deg); }
.preview-content > p { font-size:12px; color:var(--tm-muted); line-height:1.6; }
.preview-content pre { max-height:16dvh; margin:.5rem 0 0; overflow:auto; padding:.75rem; border-radius:.5rem; color:var(--tm-muted); background:var(--tm-soft); font-size:11px; line-height:1.6; white-space:pre-wrap; overflow-wrap:anywhere; }
.memory-form { display:grid; gap:.5rem; }
.memory-form > label { display:flex; flex-wrap:wrap; justify-content:space-between; gap:.3rem; margin-top:.5rem; color:var(--bear-ink, #27272a); font-size:13px; font-weight:550; }
.memory-form > label:first-child { margin-top:0; }
.memory-form > label span { color:var(--tm-muted); font-size:12px; font-weight:400; }
.memory-form > label span.danger { color:var(--tm-danger); }
.memory-form-switches { display:grid; gap:.85rem; margin-top:.8rem; border-top:1px solid var(--tm-line); padding-top:1rem; }
.memory-switch-row { display:flex; align-items:center; justify-content:space-between; gap:1rem; }
.memory-switch-row label { color:var(--bear-ink); font-size:13px; cursor:pointer; }
.memory-switch-row p { margin:.3rem 0 0; color:var(--tm-muted); font-size:12px; line-height:1.6; }
@media (max-width: 760px) {
	.memory-row-main { padding:.75rem 0 .75rem .75rem; }
	.memory-detail h3 { font-size:17px; }
}
@media (max-height:600px) {
	.memory-drawer-header p, .task-picker > label, .task-picker p { display:none; }
	.memory-tabs { margin-top:.4rem; }
	.task-picker { margin-bottom:.5rem; padding:.4rem; }
	.memory-list-summary { padding:.4rem 0; }
}
@media (prefers-reduced-motion: reduce) { .task-memory-entry { transition:none; } }
</style>

<style>
.task-memory-drawer, .task-memory-editor {
	--bear-ink:#18181b; --bear-accent:#2563eb; --bear-muted:#71717a;
	--tm-muted:var(--bear-muted, #71717a); --tm-line:rgba(24,24,27,.09); --tm-hover-line:rgba(24,24,27,.18);
	--tm-surface:#fff; --tm-soft:#f7f7f8; --tm-accent-soft:#eff6ff; --tm-danger:#c2414b;
	font-family:inherit;
}
html.dark .task-memory-drawer, html.dark .task-memory-editor {
	--bear-ink:#e4e4e7; --bear-accent:#60a5fa;
	--tm-muted:#a1a1aa; --tm-line:rgba(255,255,255,.09); --tm-hover-line:rgba(255,255,255,.19);
	--tm-surface:#1d1e22; --tm-soft:#24252a; --tm-accent-soft:rgba(96,165,250,.1); --tm-danger:#fb8585;
}
.task-memory-drawer.el-drawer { height:100dvh; max-height:100dvh; background:var(--tm-surface); }
.task-memory-drawer .el-drawer__body { display:flex; flex-direction:column; min-height:0; overflow:hidden; padding:1.4rem 1.4rem max(1rem, env(safe-area-inset-bottom)); }
.task-memory-drawer .el-tabs__header { margin-bottom:1rem; }
.task-memory-drawer .el-tabs__content { display:none; }
.task-memory-drawer .el-tabs__item { font-size:13px; font-weight:550; }
.task-memory-drawer .el-input__inner, .task-memory-drawer .el-select__placeholder { font-size:13px; }
.task-memory-task-select-popper .el-select-dropdown__item { height:auto; min-height:58px; padding:9px 14px; line-height:1.5; }
.task-memory-task-select-popper .task-option { min-width:0; max-width:100%; }
.task-memory-task-select-popper .task-option strong { display:block; overflow:hidden; font-size:13px; font-weight:550; text-overflow:ellipsis; white-space:nowrap; }
.task-memory-task-select-popper .task-option small { display:flex; justify-content:space-between; gap:1rem; margin-top:3px; font-size:11px; font-weight:400; color:var(--el-text-color-secondary); }
.task-memory-task-select-popper .task-option small span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.task-memory-task-select-popper .task-option time { flex-shrink:0; }
.task-memory-editor.el-dialog { display:flex; flex-direction:column; margin:4dvh auto; max-height:92dvh; padding:1.25rem; border-radius:.85rem; overflow:hidden; background:var(--tm-surface); }
.task-memory-editor .el-dialog__header { flex-shrink:0; padding-bottom:1rem; }
.task-memory-editor .el-dialog__title { font-size:16px; font-weight:600; }
.task-memory-editor .el-dialog__body { min-height:0; overflow:auto; overscroll-behavior:contain; scrollbar-width:thin; padding:0 .25rem 0 0; }
.task-memory-editor .el-dialog__footer { flex-shrink:0; margin-top:1rem; padding-top:.85rem; border-top:1px solid var(--tm-line); }
.task-memory-editor .el-button, .task-memory-confirm .el-button { min-width:72px; height:34px; font-family:inherit; font-size:13px; font-weight:500; }
.task-memory-editor .el-input__inner, .task-memory-editor .el-textarea__inner { font-family:inherit; font-size:14px; line-height:1.7; }
.task-memory-editor .el-input__count, .task-memory-editor .el-textarea .el-input__count { font-size:11px; }
.task-memory-confirm .el-message-box__title { font-size:16px; }
.task-memory-confirm .el-message-box__message { font-size:14px; line-height:1.7; }
@media (max-width:760px) {
	.task-memory-drawer .el-drawer__body { padding:1rem 1rem max(.75rem, env(safe-area-inset-bottom)); }
	.task-memory-editor.el-dialog { padding:1rem; }
}
html.dark .task-memory-entry {
		border: 1px solid var(--bear-line, rgba(255, 255, 255, 0.145));
		background: rgba(29, 30, 34, 0.68);
		color: var(--bear-muted, #c6c6cd);
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.16);
	}

html.dark .task-memory-entry:hover {
		color: var(--bear-ink, #efeff2);
	}

html.dark .task-memory-entry.populated {
		border-color: rgba(96, 165, 250, 0.28);
		color: var(--bear-accent, #60a5fa);
	}

html.dark .task-memory-entry.active {
		background: var(--bear-ink, #232428);
		color: #ffffff;
		box-shadow: 0 10px 28px rgba(0, 0, 0, 0.18);
	}

html.dark .task-memory-entry:focus-visible {
		box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.22), 0 8px 24px rgba(0, 0, 0, 0.16);
	}

html.dark .task-memory-badge {
		border: 2px solid #3d3e46;
		color: #ffffff;
	}


@media(max-width:760px){html.dark .task-memory-entry{background:rgba(29,30,34,.9);}}
</style>
