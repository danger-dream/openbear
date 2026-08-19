<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import ConsoleView from "./views/consoleView/ConsoleView.vue";
import MemoryView from "./views/MemoryView.vue";
import SecretsView from "./views/SecretsView.vue";
import DocsView from "./views/DocsView.vue";
import SkillsView from "./views/SkillsView.vue";
import McpView from "./views/McpView.vue";
import SettingsHubView from "./views/SettingsHubView.vue";
import LoginView from "./views/LoginView.vue";
import BearLogo from "./components/BearLogo.vue";
import ConsoleMarkdown from "./views/consoleView/ConsoleMarkdown.vue";
import draggable from "vuedraggable";
import { ElMessage, ElMessageBox, ElNotification } from "element-plus";
import { Box, ChatLineRound, Delete, DocumentCopy, EditPen, Loading, MoreFilled, Plus, Refresh, RefreshLeft, Star, StarFilled } from "@element-plus/icons-vue";
import { Api, apiError } from "./api";
import {
  isLocalConversationRow as isLocalConversation,
  mergeConversationRows,
  normalizeConversationRows,
} from "./conversationOrdering.js";
import { dragAutoScrollOptions } from "./utils/dragScroll";
import {documentTitle as browserDocumentTitle} from "./pageTitle.js";

const nav = [
  { key: "memory", label: "记忆管理", icon: "Collection", component: MemoryView },
  { key: "secrets", label: "凭证库", icon: "Key", component: SecretsView },
  { key: "docs", label: "文档库", icon: "Files", component: DocsView },
  { key: "skills", label: "Skills", icon: "MagicStick", component: SkillsView },
  { key: "mcp", label: "MCP 管理", icon: "Connection", component: McpView },
  { key: "settings", label: "设置", icon: "Setting", component: SettingsHubView },
];
const pageToPath = {
  console: "/chat",
  memory: "/memory",
  secrets: "/secrets",
  docs: "/docs",
  skills: "/skills",
  mcp: "/mcp",
  settings: "/settings",
};
const pathToPage = {
  "/": "console",
  "/chat": "console",
  "/memory": "memory",
  "/secrets": "secrets",
  "/docs": "docs",
  "/skills": "skills",
  "/mcp": "mcp",
  "/settings": "settings",
};

const active = ref("console");
const memoryType = ref("identity");
const settingsSection = ref("channels");
const appVersion = ref("");
const versionInfo = ref(null);
const versionDialogOpen = ref(false);
const versionBusy = ref(false);
const versionUpdating = ref(false);
const isLoginPath = window.location.pathname === "/login";
const VERSION_POLL_MS = 30000;
let versionPollTimer = null;
const releaseNotes = computed(() => {
  const raw = String(versionInfo.value?.latest?.body || "");
  return raw.replace(/^生效方式预告：[^\n]*\n*/u, "").trim();
});
const updateEffectLabel = computed(() => {
  if (!versionInfo.value?.updateAvailable) return "";
  return versionInfo.value?.latest?.requiresRestart === false ? "预告：刷新即可" : "预告：可能需要重启";
});
function formatPublishedAt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}
const activeView = computed(() => nav.find((x) => x.key === active.value)?.component || ConsoleView);
const conversations = ref([]);
const conversationsLoading = ref(false);
const conversationListRef = ref(null);
const activeConversationUuid = ref("");
const sidebarOpen = ref(false);
const showArchivedConversations = ref(false);
const conversationDragActive = ref(false);
const conversationOrderSaving = ref(false);
const conversationMenu = ref({ open: false, x: 0, y: 0, row: null });
const LOCAL_CONVERSATION_UUID = "local:new";
const CONVERSATION_REFRESH_ACTIVE_MS = 12000;
const CONVERSATION_REFRESH_IDLE_MS = 60000;
let conversationsRefreshTimer = null;
let conversationsRequestInFlight = false;
let conversationsReloadQueued = false;
let conversationsListEpoch = 0;
let conversationDragSnapshot = null;
let suppressConversationOpenUntil = 0;

function items(data) { return Array.isArray(data?.items) ? data.items : []; }
function localConversation() {
  return {
    local: true,
    conversationUuid: LOCAL_CONVERSATION_UUID,
    title: "新会话",
    status: "draft",
    currentStatus: "未发送",
    running: false,
    createdAt: Math.floor(Date.now() / 1000),
    messageCount: 0,
    costUsd: 0,
  };
}
function currentRouteConversationUuid() {
  if (window.location.pathname !== "/chat") return "";
  return new URLSearchParams(window.location.search).get("id") || "";
}
function routeForCurrentState() {
  const path = pageToPath[active.value] || "/chat";
  const params = new URLSearchParams();
  if (active.value === "console" && activeConversationUuid.value) params.set("id", activeConversationUuid.value);
  if (active.value === "memory") params.set("type", memoryType.value || "identity");
  if (active.value === "settings") params.set("section", settingsSection.value || "channels");
  const query = params.toString();
  return `${path}${query ? `?${query}` : ""}`;
}
function syncRoute(options = {}) {
  if (isLoginPath) return;
  const next = routeForCurrentState();
  const current = `${window.location.pathname}${window.location.search}`;
  if (next === current) return;
  const method = options.replace ? "replaceState" : "pushState";
  window.history[method]({}, "", next);
}
function applyRouteFromLocation(options = {}) {
  if (isLoginPath) return;
  const url = new URL(window.location.href);
  const page = pathToPage[url.pathname] || "console";
  active.value = page;
  if (page === "console") activeConversationUuid.value = url.searchParams.get("id") || activeConversationUuid.value || "";
  if (page === "memory") memoryType.value = url.searchParams.get("type") || "identity";
  if (page === "settings") settingsSection.value = url.searchParams.get("section") || "channels";
  if (options.replaceUnknown || !pathToPage[url.pathname]) syncRoute({ replace: true });
}
function closeSidebar() {
  sidebarOpen.value = false;
}
function selectNav(key) {
  active.value = key;
  closeSidebar();
  syncRoute();
}
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return d.toLocaleString("zh-CN", sameDay
    ? { hour: "2-digit", minute: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function conversationTitle(row) { return row?.title || "新会话"; }
const activeConversationTitle = computed(() => conversationTitle(
  conversations.value.find((row) => row.conversationUuid === activeConversationUuid.value),
));
const pageDocumentTitle = computed(() => browserDocumentTitle({
  page: isLoginPath ? "login" : active.value,
  conversationTitle: activeConversationTitle.value,
  settingsSection: settingsSection.value,
}));
function isRunning(row) { return Boolean(row?.running || row?.status === "running"); }
function createdTime(row) { return isLocalConversation(row) ? "未发送" : fmtTime(row?.createdAt); }
function formatCost(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "$0";
  if (n < 0.0001) return "<$0.0001";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}
function conversationStats(row) {
  if (isLocalConversation(row)) return "待发送";
  const count = Number(row?.messageCount || 0);
  return `${count}条 · ${formatCost(row?.costUsd)}`;
}
function conversationRowSignature(row) {
  if (!row) return "";
  return [
    row.local ? "local" : "remote",
    row.conversationUuid || "",
    row.title || "",
    row.status || "",
    row.currentStatus || "",
    row.running ? "1" : "0",
    Number(row.createdAt || 0) || 0,
    Number(row.updatedAt || 0) || 0,
    Number(row.pinnedAt || 0) || 0,
    row.pinned ? "pinned" : "unpinned",
    row.archived ? "archived" : "active",
    Number(row.archivedAt || 0) || 0,
    row.displayOrder === null || row.displayOrder === undefined ? "" : Number(row.displayOrder),
    Number(row.messageCount || 0) || 0,
    Number(row.costUsd || 0) || 0,
  ].join("¦");
}
function conversationListSignature(list = []) {
  return (Array.isArray(list) ? list : []).map(conversationRowSignature).join("\n");
}
function setConversationsIfChanged(next) {
  const normalized = normalizeConversationRows(next);
  if (conversationListSignature(conversations.value) === conversationListSignature(normalized)) return false;
  conversations.value = normalized;
  return true;
}
async function scrollConversationRowIntoView(conversationUuid) {
  if (!conversationUuid) return;
  await nextTick();
  const list = conversationListRef.value;
  if (!list) return;
  const row = Array.from(list.querySelectorAll("[data-conversation-uuid]"))
    .find((element) => element.dataset.conversationUuid === conversationUuid);
  if (!row) return;

  const listBounds = list.getBoundingClientRect();
  const rowBounds = row.getBoundingClientRect();
  const rowTop = list.scrollTop + rowBounds.top - listBounds.top;
  const rowHeight = rowBounds.height || row.offsetHeight || 0;
  const maxScrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
  const nextScrollTop = Math.min(
    maxScrollTop,
    Math.max(0, rowTop - Math.max(0, (list.clientHeight - rowHeight) / 2)),
  );
  const behavior = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
  list.scrollTo({ top: nextScrollTop, behavior });
}
function flushQueuedConversationsReload() {
  if (
    isLoginPath
    || !conversationsReloadQueued
    || conversationsRequestInFlight
    || conversationDragActive.value
    || conversationOrderSaving.value
  ) return;
  conversationsReloadQueued = false;
  void loadConversations({ silent: true });
}
function scheduleConversationsRefresh(delayMs = CONVERSATION_REFRESH_IDLE_MS) {
  if (conversationsRefreshTimer) window.clearTimeout(conversationsRefreshTimer);
  conversationsRefreshTimer = window.setTimeout(async () => {
    conversationsRefreshTimer = null;
    if (document.visibilityState === "visible") await loadConversations({ silent: true });
    scheduleConversationsRefresh(conversations.value.some((row) => isRunning(row)) ? CONVERSATION_REFRESH_ACTIVE_MS : CONVERSATION_REFRESH_IDLE_MS);
  }, Math.max(1000, Number(delayMs || 0)));
}
async function loadConversations(options = {}) {
  if (isLoginPath) return false;
  if (conversationDragActive.value || conversationOrderSaving.value || conversationsRequestInFlight) {
    conversationsReloadQueued = true;
    return false;
  }
  const silent = Boolean(options?.silent);
  const requestEpoch = conversationsListEpoch;
  const includeArchived = Boolean(showArchivedConversations.value);
  conversationsRequestInFlight = true;
  if (!silent) conversationsLoading.value = true;
  try {
    const data = await Api.conversations(includeArchived ? { includeArchived: 1 } : {});
    if (requestEpoch !== conversationsListEpoch || conversationDragActive.value || conversationOrderSaving.value) {
      conversationsReloadQueued = true;
      return false;
    }
    const remoteRows = items(data);
    const routeConversationUuid = currentRouteConversationUuid();
    const nextConversations = mergeConversationRows(conversations.value, remoteRows, {
      ensureLocal: isLocalConversation(activeConversationUuid.value)
        || (!remoteRows.length && !routeConversationUuid && !activeConversationUuid.value),
      createLocalRow: localConversation,
    });
    const localRows = nextConversations.filter(isLocalConversation);
    setConversationsIfChanged(nextConversations);
    if (isLocalConversation(activeConversationUuid.value)) return true;
    if (routeConversationUuid && routeConversationUuid === activeConversationUuid.value) return true;
    if (!activeConversationUuid.value || !conversations.value.some((x) => x.conversationUuid === activeConversationUuid.value)) {
      activeConversationUuid.value = data.activeConversationUuid || remoteRows[0]?.conversationUuid || localRows[0]?.conversationUuid || "";
      if (active.value === "console") syncRoute({ replace: true });
    }
    return true;
  } catch (error) {
    if (!silent) ElMessage.error(apiError(error));
    return false;
  } finally {
    conversationsRequestInFlight = false;
    if (!silent) conversationsLoading.value = false;
    flushQueuedConversationsReload();
  }
}
function isSortableConversation(row) {
  return Boolean(row?.conversationUuid) && !isLocalConversation(row);
}
function canMoveConversation(event) {
  const dragged = event?.draggedContext?.element;
  const related = event?.relatedContext?.element;
  return Boolean(
    isSortableConversation(dragged)
    && isSortableConversation(related)
    && Boolean(dragged.pinned) === Boolean(related.pinned),
  );
}
function handleConversationDragStart() {
  closeConversationMenu();
  conversationDragSnapshot = conversations.value.slice();
  conversationDragActive.value = true;
  conversationsListEpoch += 1;
}
async function handleConversationDragEnd(event) {
  const snapshot = conversationDragSnapshot || conversations.value.slice();
  conversationDragSnapshot = null;
  conversationDragActive.value = false;
  const oldIndex = Number(event?.oldIndex);
  const newIndex = Number(event?.newIndex);
  if (!Number.isInteger(oldIndex) || !Number.isInteger(newIndex) || oldIndex === newIndex) {
    flushQueuedConversationsReload();
    return;
  }

  suppressConversationOpenUntil = Date.now() + 300;
  const row = conversations.value[newIndex];
  if (!isSortableConversation(row)) {
    conversations.value = snapshot;
    flushQueuedConversationsReload();
    return;
  }
  const group = conversations.value.filter(
    (candidate) => isSortableConversation(candidate) && Boolean(candidate.pinned) === Boolean(row.pinned),
  );
  const rowIndex = group.findIndex((candidate) => candidate.conversationUuid === row.conversationUuid);
  const before = rowIndex > 0 ? group[rowIndex - 1] : null;
  const after = rowIndex >= 0 && rowIndex < group.length - 1 ? group[rowIndex + 1] : null;
  if (!before && !after) {
    conversations.value = snapshot;
    flushQueuedConversationsReload();
    return;
  }

  conversationOrderSaving.value = true;
  try {
    await Api.reorderConversation(row.conversationUuid, {
      beforeConversationUuid: before?.conversationUuid || "",
      afterConversationUuid: after?.conversationUuid || "",
    });
  } catch (error) {
    conversations.value = snapshot;
    ElMessage.error(apiError(error));
  } finally {
    conversationOrderSaving.value = false;
    conversationsListEpoch += 1;
    await loadConversations({ silent: true });
    flushQueuedConversationsReload();
  }
}
async function toggleShowArchivedConversations() {
  showArchivedConversations.value = !showArchivedConversations.value;
  conversationsListEpoch += 1;
  await loadConversations();
}
function refreshConsole() { window.dispatchEvent(new CustomEvent("openbear:console-refresh")); }
async function refreshConsoleAfterPropSync() {
  await nextTick();
  refreshConsole();
}
function focusLocalConversation() {
  active.value = "console";
  setConversationsIfChanged([localConversation(), ...conversations.value.filter((row) => !isLocalConversation(row))]);
  activeConversationUuid.value = LOCAL_CONVERSATION_UUID;
  closeSidebar();
  syncRoute();
  void refreshConsoleAfterPropSync();
}
async function startConsoleNewSession() {
  const existed = conversations.value.some(isLocalConversation);
  focusLocalConversation();
  ElMessage.success(existed ? "已聚焦未发送的新会话" : "已开启新会话");
}
async function openConversation(row) {
  if (!row?.conversationUuid || Date.now() < suppressConversationOpenUntil) return;
  active.value = "console";
  try {
    activeConversationUuid.value = row.conversationUuid;
    closeSidebar();
    syncRoute();
  } catch (error) {
    ElMessage.error(apiError(error));
  }
}
async function renameConversation(row) {
  if (!row?.conversationUuid) return;
  if (isLocalConversation(row)) {
    ElMessage.info("草稿会话发送后才能重命名");
    return;
  }
  try {
    const { value } = await ElMessageBox.prompt("请输入新的会话名称", "重命名会话", {
      inputValue: conversationTitle(row),
      inputPlaceholder: "会话名称",
      inputValidator: (value) => String(value || "").trim() ? true : "名称不能为空",
      confirmButtonText: "保存",
      cancelButtonText: "取消",
    });
    const title = String(value || "").trim();
    if (!title || title === conversationTitle(row)) return;
    await Api.updateConversation(row.conversationUuid, { title });
    await loadConversations();
    ElMessage.success("会话已重命名");
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    ElMessage.error(apiError(error));
  }
}

async function duplicateConversation(row) {
  if (!row?.conversationUuid) return;
  if (isLocalConversation(row)) {
    ElMessage.info("草稿会话发送后才能复制");
    return;
  }
  if (isRunning(row)) {
    ElMessage.warning("运行中的会话暂不能复制，等当前任务结束后再试");
    return;
  }
  try {
    const data = await Api.duplicateConversation(row.conversationUuid);
    const uuid = data.conversation?.conversationUuid || data.state?.conversationUuid || "";
    await loadConversations();
    if (uuid) {
      active.value = "console";
      activeConversationUuid.value = uuid;
      syncRoute();
      void refreshConsoleAfterPropSync();
    }
    ElMessage.success("会话已复制");
  } catch (error) {
    const code = apiError(error);
    if (code === "conversation_is_active") ElMessage.warning("运行中的会话暂不能复制");
    else ElMessage.error(code);
  }
}

async function togglePinConversation(row) {
  if (!row?.conversationUuid) return;
  if (isLocalConversation(row)) {
    ElMessage.info("草稿会话发送后才能置顶");
    return;
  }
  const pinning = !row.pinned;
  try {
    if (pinning) await Api.pinConversation(row.conversationUuid);
    else await Api.unpinConversation(row.conversationUuid);
    await loadConversations();
    await scrollConversationRowIntoView(row.conversationUuid);
    ElMessage.success(pinning ? "已置顶" : "已取消置顶");
  } catch (error) {
    ElMessage.error(apiError(error));
  }
}

async function toggleArchiveConversation(row) {
  if (!row?.conversationUuid) return;
  if (isLocalConversation(row)) {
    ElMessage.info("草稿会话发送后才能归档");
    return;
  }
  const archiving = !row.archived;
  try {
    await Api.setConversationArchived(row.conversationUuid, archiving);
    conversationsListEpoch += 1;
    await loadConversations();
    ElMessage.success(archiving ? "已归档" : "已取消归档");
  } catch (error) {
    ElMessage.error(apiError(error));
  }
}

async function deleteConversation(row) {
  if (!row?.conversationUuid) return;
  try {
    await ElMessageBox.confirm(
      `确定删除会话「${conversationTitle(row)}」吗？所有消息和历史记录将被清除。`,
      "删除会话",
      { confirmButtonText: "删除", cancelButtonText: "取消", type: "warning" }
    );
  } catch { return; }
  try {
    if (isLocalConversation(row)) {
      setConversationsIfChanged(conversations.value.filter((item) => item.conversationUuid !== row.conversationUuid));
      if (activeConversationUuid.value === row.conversationUuid) {
        const remaining = conversations.value.filter((item) => !isLocalConversation(item));
        activeConversationUuid.value = remaining[0]?.conversationUuid || "";
        if (!activeConversationUuid.value) focusLocalConversation();
        syncRoute({ replace: true });
      }
      ElMessage.success("草稿会话已移除");
      return;
    }
    await Api.deleteConversation(row.conversationUuid);
    if (activeConversationUuid.value === row.conversationUuid) {
      const remaining = conversations.value.filter((r) => r.conversationUuid !== row.conversationUuid && !isLocalConversation(r));
      activeConversationUuid.value = remaining[0]?.conversationUuid || "";
      if (!activeConversationUuid.value) focusLocalConversation();
      syncRoute({ replace: true });
    }
    await loadConversations();
    ElMessage.success("会话已删除");
  } catch (error) {
    ElMessage.error(apiError(error));
  }
}
function closeConversationMenu() {
  if (!conversationMenu.value.open) return;
  conversationMenu.value = { open: false, x: 0, y: 0, row: null };
}
function openConversationMenu(event, row) {
  if (!row?.conversationUuid) return;
  event?.preventDefault?.();
  event?.stopPropagation?.();
  const width = 218;
  const rowIsLocal = isLocalConversation(row);
  const rowIsRunning = isRunning(row);
  const height = rowIsLocal ? 184 : rowIsRunning ? 212 : 224;
  const padding = 8;
  const x = Math.min(Math.max(padding, Number(event?.clientX || 0)), Math.max(padding, window.innerWidth - width - padding));
  const y = Math.min(Math.max(padding, Number(event?.clientY || 0)), Math.max(padding, window.innerHeight - height - padding));
  conversationMenu.value = { open: true, x, y, row };
}
async function runConversationMenuAction(action) {
  const row = conversationMenu.value.row;
  closeConversationMenu();
  if (!row) return;
  if (action === "rename") await renameConversation(row);
  else if (action === "duplicate") await duplicateConversation(row);
  else if (action === "pin") await togglePinConversation(row);
  else if (action === "archive") await toggleArchiveConversation(row);
  else if (action === "delete") await deleteConversation(row);
}
function handleConversationMenuKeydown(event) {
  if (event.key !== "Escape") return;
  closeConversationMenu();
  closeSidebar();
}
function handleConsoleConversationCreated(uuid) {
  if (!uuid) return;
  if (isLocalConversation(uuid)) {
    focusLocalConversation();
    return;
  }
  setConversationsIfChanged(conversations.value.filter((row) => !isLocalConversation(row)));
  activeConversationUuid.value = uuid;
  syncRoute({ replace: true });
  void loadConversations();
}
function handleConsoleRefreshList() { void loadConversations(); scheduleConversationsRefresh(CONVERSATION_REFRESH_ACTIVE_MS); }
function handleExternalConversationsRefresh() { handleConsoleRefreshList(); }
function handleMemoryTypeChanged(type) {
  memoryType.value = type || "identity";
  if (active.value === "memory") syncRoute();
}
function handleSettingsSectionChanged(section) {
  settingsSection.value = section || "channels";
  if (active.value === "settings") syncRoute();
}

function formatBusySummary(running) {
  if (!running) return "";
  const parts = [];
  if (running.openbearRuns) parts.push(`OpenBear ${running.openbearRuns} 个`);
  if (running.rathTasks) parts.push(`Rath ${running.rathTasks} 个`);
  if (running.childProcesses) parts.push(`子进程 ${running.childProcesses} 个`);
  if (running.operations) parts.push(`操作 ${running.operations} 个`);
  return parts.join("、");
}

function resultBanner(result) {
  if (!result || result.acked) return "";
  if (result.status === "success") return result.message || `已更新到 v${result.toVersion || ""}`;
  if (result.status === "rolled_back") return result.message || `更新失败，已回滚到 v${result.fromVersion || ""}`;
  if (result.status === "failed") return result.message || "更新失败";
  return "";
}

async function loadVersionInfo() {
  try {
    const data = await Api.systemVersion();
    versionInfo.value = data;
    appVersion.value = data.version || "";
    const banner = resultBanner(data.lastResult);
    if (banner && !versionUpdating.value) {
      const status = data.lastResult?.status;
      ElNotification({
        title: status === "success" ? "更新完成" : "上次更新未成功",
        message: banner,
        type: status === "success" ? "success" : "warning",
        duration: 8000,
      });
      try { await Api.ackSystemUpdate(); } catch { /* ignore */ }
    }
    if (versionUpdating.value) {
      const phase = data.phase || "idle";
      if (data.version && data.latest?.version && data.version === data.latest.version && !data.updateAvailable) {
        versionUpdating.value = false;
        ElMessage.success(data.lastResult?.message || `已更新到 v${data.version}`);
        if (data.lastResult?.status === "success" && data.lastResult?.requiresRestart === false) {
          try {
            await ElMessageBox.confirm("前端已更新，刷新页面即可生效。", "刷新页面", {
              confirmButtonText: "刷新",
              cancelButtonText: "稍后",
              type: "success",
            });
            window.location.reload();
          } catch { /* later */ }
        }
      } else if (["idle", "done"].includes(phase) && data.lastResult && ["rolled_back", "failed"].includes(data.lastResult.status)) {
        versionUpdating.value = false;
        ElMessage.error(data.lastResult.message || "更新失败");
      }
    }
  } catch {
    if (!appVersion.value) appVersion.value = "";
  }
}

function scheduleVersionPoll() {
  if (versionPollTimer) window.clearTimeout(versionPollTimer);
  const delay = versionUpdating.value ? 2000 : VERSION_POLL_MS;
  versionPollTimer = window.setTimeout(() => {
    void loadVersionInfo().finally(scheduleVersionPoll);
  }, delay);
}

function openVersionDialog() {
  versionDialogOpen.value = true;
  void loadVersionInfo();
}

async function startSystemUpdate() {
  const info = versionInfo.value;
  if (!info?.updateAvailable) return;
  const latest = info.latest?.version || "";
  const previewRestart = info.latest?.requiresRestart !== false;
  if (info.dirtyWorktree) {
    try {
      await ElMessageBox.confirm(
        "当前目录是带未提交改动的 git 工作区。继续更新会用发行包覆盖这些文件。",
        "覆盖工作区",
        { confirmButtonText: "仍然更新", cancelButtonText: "取消", type: "warning" },
      );
    } catch { return; }
  }
  let force = false;
  if (info.running?.busy && previewRestart) {
    try {
      await ElMessageBox.confirm(
        `当前还有运行中的任务（${formatBusySummary(info.running) || "忙碌"}）。停止更新，还是强制更新？强制更新会中断这些任务。`,
        "有任务在运行",
        { confirmButtonText: "强制更新", cancelButtonText: "停止更新", type: "warning" },
      );
      force = true;
    } catch { return; }
  }
  try {
    await ElMessageBox.confirm(
      previewRestart
        ? `确认更新到 v${latest}？后端变更时服务会重启。`
        : `确认更新到 v${latest}？若现场只换了前端，刷新即可。`,
      "确认更新",
      { confirmButtonText: "开始更新", cancelButtonText: "取消", type: "warning" },
    );
  } catch { return; }
  versionBusy.value = true;
  try {
    const result = await Api.systemUpdate({
      confirm: true,
      force,
      allowDirty: Boolean(info.dirtyWorktree),
    });
    versionUpdating.value = true;
    versionDialogOpen.value = false;
    ElMessage.info(result.previewRequiresRestart === false ? "正在更新前端，请稍候…" : "正在更新，服务可能即将重启，请稍候刷新。");
    scheduleVersionPoll();
  } catch (error) {
    const code = apiError(error);
    if (code === "system_busy") ElMessage.warning("当前有任务在运行，请选择强制更新或稍后再试");
    else if (code === "dirty_worktree") ElMessage.warning("工作区有未提交改动，已拒绝覆盖");
    else if (code === "update_in_progress") ElMessage.warning("已有更新在进行");
    else ElMessage.error(code);
  } finally {
    versionBusy.value = false;
    void loadVersionInfo();
  }
}

applyRouteFromLocation({ replaceUnknown: true });

watch(pageDocumentTitle, (title) => {
  if (typeof document !== "undefined") document.title = title;
}, {immediate: true});

onMounted(() => {
  window.addEventListener("popstate", applyRouteFromLocation);
  window.addEventListener("openbear:conversations-refresh", handleExternalConversationsRefresh);
  window.addEventListener("click", closeConversationMenu);
  window.addEventListener("scroll", closeConversationMenu, true);
  window.addEventListener("resize", closeConversationMenu);
  window.addEventListener("keydown", handleConversationMenuKeydown);
  void loadConversations();
  scheduleConversationsRefresh(CONVERSATION_REFRESH_IDLE_MS);
  void loadVersionInfo().finally(scheduleVersionPoll);
});
onBeforeUnmount(() => {
  window.removeEventListener("popstate", applyRouteFromLocation);
  window.removeEventListener("openbear:conversations-refresh", handleExternalConversationsRefresh);
  window.removeEventListener("click", closeConversationMenu);
  window.removeEventListener("scroll", closeConversationMenu, true);
  window.removeEventListener("resize", closeConversationMenu);
  window.removeEventListener("keydown", handleConversationMenuKeydown);
  if (conversationsRefreshTimer) window.clearTimeout(conversationsRefreshTimer);
  if (versionPollTimer) window.clearTimeout(versionPollTimer);
});
</script>

<template>
  <LoginView v-if="isLoginPath" />
  <div v-else class="app-shell h-full flex">
    <div class="mobile-app-bar">
      <button
        type="button"
        class="mobile-sidebar-toggle"
        :aria-expanded="sidebarOpen"
        aria-controls="openbear-sidebar"
        aria-label="打开导航"
        title="打开导航"
        @click="sidebarOpen = true"
      >
        <span></span><span></span><span></span>
      </button>
      <div class="mobile-app-brand" aria-hidden="true">
        <span class="mobile-brand-logo"><BearLogo /></span>
        <strong>OpenBear</strong>
      </div>
    </div>
    <button
      v-if="sidebarOpen"
      type="button"
      class="app-sidebar-backdrop"
      aria-label="关闭导航"
      @click="closeSidebar"
    ></button>
    <aside
      id="openbear-sidebar"
      class="app-sidebar w-[300px] shrink-0 flex flex-col border-r border-zinc-200 bg-zinc-50/90 p-3 text-zinc-950"
      :class="{'is-open': sidebarOpen}"
    >
      <div class="mb-3 flex items-center gap-2 rounded-2xl px-2 py-2">
        <div class="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white p-1.5 ring-1 ring-zinc-200 shadow-sm">
          <BearLogo />
        </div>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[15px] font-semibold leading-tight">OpenBear</div>
          <div class="mt-0.5 truncate text-[12px] leading-tight text-zinc-500">Web 控制台</div>
        </div>
        <button
          type="button"
          class="version-entry inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-medium leading-none tracking-wide shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]"
          :class="versionInfo?.updateAvailable ? 'border-amber-300 bg-amber-50 text-amber-700' : 'border-zinc-200/80 bg-white/70 text-zinc-500'"
          :title="versionInfo?.updateAvailable ? '有新版本，点击查看' : '查看版本'"
          @click="openVersionDialog"
        >
          <span class="font-mono text-[11px]">v{{ appVersion || "…" }}</span>
          <span v-if="versionInfo?.updateAvailable" class="version-dot" aria-hidden="true"></span>
        </button>
      </div>

      <button class="mb-2 flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm font-medium text-zinc-800 hover:bg-zinc-200/70" @click="startConsoleNewSession">
        <el-icon :size="16"><Plus /></el-icon>
        新会话
      </button>

      <nav class="space-y-1 text-sm">
        <button
          v-for="n in nav"
          :key="n.key"
          @click="selectNav(n.key)"
          class="w-full flex items-center gap-3 px-3 py-2 rounded-xl text-left transition-colors"
          :class="active === n.key ? 'bg-zinc-200/80 text-zinc-950 font-medium' : 'text-zinc-700 hover:bg-zinc-200/60'"
        >
          <el-icon :size="16"><component :is="n.icon" /></el-icon>
          <span class="truncate">{{ n.label }}</span>
        </button>
      </nav>

      <div class="-mx-3 mt-3 flex min-h-0 flex-1 flex-col border-t border-zinc-200/80 pt-2">
        <div class="mb-2 flex items-center justify-between px-6">
          <div class="flex items-center gap-2 text-xs font-medium text-zinc-500">
            <el-icon :size="16"><ChatLineRound /></el-icon>
            <span>会话</span>
          </div>
          <div class="flex items-center gap-1">
            <button
              type="button"
              class="grid h-8 w-8 place-items-center rounded-xl text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900"
              :class="showArchivedConversations ? 'bg-zinc-200/80 text-zinc-900' : ''"
              :aria-pressed="showArchivedConversations"
              :title="showArchivedConversations ? '隐藏已归档会话' : '显示已归档会话'"
              @click="toggleShowArchivedConversations"
            >
              <el-icon :size="16"><Box /></el-icon>
            </button>
            <button class="grid h-8 w-8 place-items-center rounded-xl text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 disabled:opacity-50" :disabled="conversationsLoading" title="刷新会话" @click="loadConversations">
              <el-icon :size="16" :class="conversationsLoading && 'animate-spin'"><Refresh /></el-icon>
            </button>
          </div>
        </div>
        <div ref="conversationListRef" class="min-h-0 flex-1 overflow-y-auto py-1" :class="conversationDragActive && 'select-none'">
          <div v-if="conversationsLoading && !conversations.length" class="mx-3 rounded-2xl bg-zinc-100 p-3 text-xs text-zinc-500">加载中…</div>
          <div v-else-if="!conversations.length" class="mx-3 rounded-2xl bg-zinc-100 p-3 text-xs text-zinc-500">暂无会话</div>
          <draggable
            v-else
            v-model="conversations"
            item-key="conversationUuid"
            class="space-y-1"
            :animation="160"
            :disabled="conversationsLoading || conversationOrderSaving"
            :move="canMoveConversation"
            :filter="'.conversation-menu-trigger'"
            :prevent-on-filter="false"
            :delay="120"
            :delay-on-touch-only="true"
            ghost-class="conversation-drag-ghost"
            v-bind="dragAutoScrollOptions"
            :scroll="conversationListRef"
            @start="handleConversationDragStart"
            @end="handleConversationDragEnd"
          >
            <template #item="{ element: row }">
              <div
                :data-conversation-uuid="row.conversationUuid"
                class="group relative mx-2 rounded-xl transition"
                :class="[
                  active === 'console' && activeConversationUuid === row.conversationUuid ? 'bg-zinc-200/80 text-zinc-950' : 'text-zinc-700 hover:bg-zinc-100',
                  row.pinned ? 'before:absolute before:left-0 before:top-2 before:bottom-2 before:w-0.5 before:rounded-full before:bg-amber-400' : '',
                  isRunning(row) ? 'ring-1 ring-inset ring-emerald-200/80 bg-emerald-50/70' : '',
                ]"
                @contextmenu="openConversationMenu($event, row)"
              >
                <button class="block w-full min-w-0 overflow-hidden px-4 py-2.5 text-left text-sm" @click="openConversation(row)">
                  <div class="flex min-w-0 items-center gap-2">
                    <el-icon v-if="row.pinned" class="shrink-0 text-amber-500" :size="13"><StarFilled /></el-icon>
                    <div class="min-w-0 flex-1 truncate" :title="conversationTitle(row)">{{ conversationTitle(row) }}</div>
                    <span v-if="isRunning(row)" class="shrink-0 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[11px] leading-none text-emerald-700">运行中</span>
                  </div>
                  <div class="mt-0.5 flex min-w-0 items-center gap-2 text-[12px] leading-5 text-zinc-500">
                    <span class="shrink-0 whitespace-nowrap">{{ createdTime(row) }}</span>
                    <span class="ml-auto flex max-w-[11rem] shrink-0 items-center justify-end gap-1 whitespace-nowrap text-zinc-500">
                      <span class="min-w-0 truncate">{{ conversationStats(row) }}</span>
                      <el-icon v-if="isRunning(row)" :size="12" class="shrink-0 animate-spin" title="正在运行"><Loading /></el-icon>
                    </span>
                  </div>
                </button>
                <button
                  class="conversation-menu-trigger absolute right-1.5 top-1.5 grid h-6 w-6 place-items-center rounded-lg text-zinc-400 opacity-0 transition hover:bg-zinc-200 hover:text-zinc-800 group-hover:opacity-100"
                  title="会话菜单"
                  @click.stop="openConversationMenu($event, row)"
                >
                  <el-icon :size="14"><MoreFilled /></el-icon>
                </button>
              </div>
            </template>
          </draggable>
        </div>
      </div>
    </aside>

    <Teleport to="body">
      <div
        v-if="conversationMenu.open"
        class="conversation-menu-backdrop"
        @contextmenu.prevent="closeConversationMenu"
      >
        <div
          class="conversation-context-menu"
          :style="{ left: `${conversationMenu.x}px`, top: `${conversationMenu.y}px` }"
          role="menu"
          @click.stop
          @contextmenu.prevent
        >
          <button
            class="context-menu-item"
            :disabled="isLocalConversation(conversationMenu.row)"
            role="menuitem"
            @click="runConversationMenuAction('rename')"
          >
            <el-icon><EditPen /></el-icon>
            <span>重命名</span>
            <span class="context-menu-shortcut">⌘R</span>
          </button>
          <button
            class="context-menu-item"
            :disabled="isLocalConversation(conversationMenu.row) || isRunning(conversationMenu.row)"
            role="menuitem"
            @click="runConversationMenuAction('duplicate')"
          >
            <el-icon><DocumentCopy /></el-icon>
            <span>复制会话</span>
            <span class="context-menu-shortcut">⌘D</span>
          </button>
          <button
            class="context-menu-item"
            :disabled="isLocalConversation(conversationMenu.row)"
            role="menuitem"
            @click="runConversationMenuAction('pin')"
          >
            <el-icon><component :is="conversationMenu.row?.pinned ? Star : StarFilled" /></el-icon>
            <span>{{ conversationMenu.row?.pinned ? '取消置顶' : '置顶' }}</span>
          </button>
          <button
            class="context-menu-item"
            :disabled="isLocalConversation(conversationMenu.row)"
            role="menuitem"
            @click="runConversationMenuAction('archive')"
          >
            <el-icon><component :is="conversationMenu.row?.archived ? RefreshLeft : Box" /></el-icon>
            <span>{{ conversationMenu.row?.archived ? '取消归档' : '归档' }}</span>
          </button>
          <div class="context-menu-separator"></div>
          <button
            class="context-menu-item danger"
            :disabled="isRunning(conversationMenu.row)"
            role="menuitem"
            @click="runConversationMenuAction('delete')"
          >
            <el-icon><Delete /></el-icon>
            <span>删除</span>
            <span class="context-menu-shortcut">⌫</span>
          </button>
        </div>
      </div>
    </Teleport>

    <main class="app-main flex-1 min-w-0 flex flex-col bg-macbg text-mactext">
      <ConsoleView
        v-if="active === 'console'"
        :conversation-uuid="activeConversationUuid"
        @conversation-created="handleConsoleConversationCreated"
        @conversations-refresh="handleConsoleRefreshList"
      />
      <MemoryView v-else-if="active === 'memory'" :active-type="memoryType" @type-changed="handleMemoryTypeChanged" />
      <component
        v-else
        :is="activeView"
        :section="settingsSection"
        @section-changed="handleSettingsSectionChanged"
      />
    </main>

    <el-dialog
      v-model="versionDialogOpen"
      width="720px"
      top="8vh"
      append-to-body
      class="version-dialog"
      :show-close="true"
    >
      <template #header>
        <div class="version-dialog-head">
          <div>
            <div class="version-dialog-kicker">OpenBear</div>
            <h2 class="version-dialog-title">版本与更新</h2>
          </div>
          <span v-if="versionInfo?.updateAvailable" class="version-chip is-update">有可用更新</span>
          <span v-else class="version-chip">已是最新</span>
        </div>
      </template>
      <div class="version-dialog-body">
        <div class="version-hero">
          <div class="version-hero-item">
            <span>当前</span>
            <strong>v{{ versionInfo?.version || appVersion || "…" }}</strong>
          </div>
          <div class="version-hero-arrow" aria-hidden="true">→</div>
          <div class="version-hero-item">
            <span>{{ versionInfo?.updateAvailable ? "最新发行版" : "已安装" }}</span>
            <strong>v{{ versionInfo?.latest?.version || versionInfo?.version || appVersion || "…" }}</strong>
          </div>
        </div>
        <div class="version-meta">
          <span v-if="versionInfo?.latest?.publishedAt">发布于 {{ formatPublishedAt(versionInfo.latest.publishedAt) }}</span>
          <span v-if="updateEffectLabel">{{ updateEffectLabel }}，现场以本机文件对比为准</span>
          <a v-if="versionInfo?.latest?.htmlUrl" :href="versionInfo.latest.htmlUrl" target="_blank" rel="noreferrer">在 GitHub 查看</a>
        </div>
        <div v-if="versionInfo?.dirtyWorktree" class="version-callout is-warn">
          当前是带未提交改动的 git 工作区，继续更新会用发行包覆盖这些文件。
        </div>
        <div v-if="versionInfo?.lastResult && !versionInfo.lastResult.acked" class="version-callout">
          上次结果：{{ versionInfo.lastResult.message }}
        </div>
        <section class="version-notes">
          <div class="version-notes-head">更新说明</div>
          <div v-if="releaseNotes" class="version-notes-md">
            <ConsoleMarkdown :text="releaseNotes" />
          </div>
          <div v-else class="version-notes-empty">
            {{ versionInfo?.latest ? "这份发行版没有说明。" : "还没有正式发行版。打 tag 后，这里会直接显示 GitHub Release 的 Markdown。" }}
          </div>
        </section>
      </div>
      <template #footer>
        <div class="version-dialog-foot">
          <button type="button" class="version-btn" @click="versionDialogOpen = false">关闭</button>
          <button
            v-if="versionInfo?.updateAvailable"
            type="button"
            class="version-btn is-primary"
            :disabled="versionBusy || versionUpdating"
            @click="startSystemUpdate"
          >
            {{ versionBusy || versionUpdating ? "更新中…" : "更新到 v" + (versionInfo.latest?.version || "") }}
          </button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>



<style scoped>
.version-entry {
  cursor: pointer;
}
.version-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: #d97706;
  box-shadow: 0 0 0 3px rgba(217, 119, 6, .16);
}
.conversation-drag-ghost {
  opacity: .45;
}

.conversation-menu-backdrop {
  position: fixed;
  inset: 0;
  z-index: 3200;
  background: transparent;
}

.conversation-context-menu {
  position: fixed;
  width: 218px;
  padding: 6px;
  border: 1px solid rgba(0, 0, 0, .12);
  border-radius: 12px;
  background:
    linear-gradient(180deg, rgba(255,255,255,.88), rgba(246,246,247,.82));
  color: #1f2933;
  box-shadow:
    0 28px 70px rgba(15, 23, 42, .24),
    0 8px 22px rgba(15, 23, 42, .12),
    inset 0 1px 0 rgba(255, 255, 255, .75);
  backdrop-filter: blur(22px) saturate(1.55);
  -webkit-backdrop-filter: blur(22px) saturate(1.55);
  transform-origin: top left;
  animation: mac-context-in .11s cubic-bezier(.2,.8,.2,1);
}

.context-menu-item {
  display: grid;
  grid-template-columns: 20px 1fr auto;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 34px;
  padding: 6px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #1f2937;
  font-size: 14px;
  line-height: 1.25;
  text-align: left;
  outline: none;
}

.context-menu-item :deep(.el-icon) {
  color: #6b7280;
  font-size: 15px;
}

.context-menu-item:hover:not(:disabled),
.context-menu-item:focus-visible:not(:disabled) {
  background: linear-gradient(180deg, #2f8cff, #0a73ff);
  color: #fff;
}

.context-menu-item:hover:not(:disabled) :deep(.el-icon),
.context-menu-item:focus-visible:not(:disabled) :deep(.el-icon),
.context-menu-item:hover:not(:disabled) .context-menu-shortcut,
.context-menu-item:focus-visible:not(:disabled) .context-menu-shortcut {
  color: rgba(255, 255, 255, .84);
}

.context-menu-item.danger:hover:not(:disabled),
.context-menu-item.danger:focus-visible:not(:disabled) {
  background: linear-gradient(180deg, #ff5d5d, #ef2f2f);
}

.context-menu-item:disabled {
  color: rgba(107, 114, 128, .42);
  cursor: default;
}

.context-menu-item:disabled :deep(.el-icon),
.context-menu-item:disabled .context-menu-shortcut {
  color: rgba(107, 114, 128, .32);
}

.context-menu-shortcut {
  color: #6b7280;
  font-size: 13px;
  letter-spacing: .01em;
}

.context-menu-separator {
  height: 1px;
  margin: 5px 6px;
  background: rgba(0, 0, 0, .10);
}

@keyframes mac-context-in {
  from {
    opacity: 0;
    transform: scale(.965) translateY(-2px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.mobile-app-bar,
.app-sidebar-backdrop {
  display: none;
}

@media (max-width: 760px) {
  .app-shell {
    position: relative;
    width: 100%;
    min-width: 0;
    overflow: hidden;
  }

  .mobile-app-bar {
    position: absolute;
    z-index: 80;
    top: 0;
    right: 0;
    left: 0;
    display: flex;
    height: 48px;
    align-items: center;
    gap: 10px;
    border-bottom: 1px solid rgba(15, 23, 42, .08);
    background: rgba(255, 255, 255, .92);
    padding: 0 12px;
    box-shadow: 0 1px 8px rgba(15, 23, 42, .04);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .mobile-sidebar-toggle {
    display: grid;
    width: 34px;
    height: 34px;
    flex: 0 0 auto;
    place-content: center;
    gap: 4px;
    border: 1px solid rgba(15, 23, 42, .10);
    border-radius: 11px;
    background: #fff;
    color: #3f3f46;
    box-shadow: 0 4px 14px rgba(15, 23, 42, .08);
  }

  .mobile-sidebar-toggle span {
    width: 14px;
    height: 1.5px;
    border-radius: 999px;
    background: currentColor;
  }

  .mobile-app-brand {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 7px;
    color: #27272a;
    font-size: 13px;
  }

  .mobile-brand-logo {
    display: grid;
    width: 25px;
    height: 25px;
    place-items: center;
    border-radius: 8px;
    background: #fffaf1;
    padding: 4px;
    box-shadow: inset 0 0 0 1px rgba(15, 23, 42, .08);
  }

  .app-main {
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    padding-top: 48px;
  }

  .app-sidebar {
    position: absolute;
    z-index: 100;
    inset: 0 auto 0 0;
    width: min(300px, calc(100vw - 48px)) !important;
    max-width: calc(100vw - 48px);
    visibility: hidden;
    pointer-events: none;
    transform: translateX(-102%);
    box-shadow: 18px 0 52px rgba(15, 23, 42, .18);
    transition: transform .2s cubic-bezier(.2, .8, .2, 1), visibility 0s linear .2s;
    will-change: transform;
  }

  .app-sidebar.is-open {
    visibility: visible;
    pointer-events: auto;
    transform: translateX(0);
    transition-delay: 0s;
  }

  .app-sidebar-backdrop {
    position: absolute;
    z-index: 90;
    inset: 0;
    display: block;
    border: 0;
    background: rgba(15, 23, 42, .28);
    padding: 0;
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
  }
}
</style>

<style>
.version-dialog.el-dialog {
  --el-dialog-padding-primary: 0;
  padding: 0 !important;
  overflow: hidden;
  border-radius: 20px;
  background: linear-gradient(180deg, rgba(255, 255, 255, .98), rgba(246, 246, 247, .96));
  box-shadow: 0 24px 80px rgba(24, 24, 27, .22), inset 0 1px 0 rgba(255, 255, 255, .9);
}
.version-dialog .el-dialog__header {
  margin: 0;
  padding: 18px 22px 12px;
  border-bottom: 1px solid rgba(228, 228, 231, .82);
}
.version-dialog .el-dialog__body {
  padding: 0;
}
.version-dialog .el-dialog__footer {
  padding: 12px 22px 16px;
  border-top: 1px solid rgba(228, 228, 231, .75);
}
.version-dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-right: 28px;
}
.version-dialog-kicker {
  color: #71717a;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.version-dialog-title {
  margin: 2px 0 0;
  color: #18181b;
  font-size: 17px;
  font-weight: 680;
  letter-spacing: -.02em;
}
.version-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  border: 1px solid rgba(228, 228, 231, .95);
  border-radius: 999px;
  background: #fff;
  padding: 0 9px;
  color: #52525b;
  font-size: 11px;
  font-weight: 650;
}
.version-chip.is-update {
  border-color: #fcd34d;
  background: #fffbeb;
  color: #b45309;
}
.version-dialog-body {
  padding: 16px 22px 8px;
}
.version-hero {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 10px;
  align-items: center;
  margin-bottom: 12px;
}
.version-hero-item {
  min-width: 0;
  border: 1px solid rgba(228, 228, 231, .95);
  border-radius: 14px;
  background: #fff;
  padding: 10px 12px;
}
.version-hero-item span {
  display: block;
  color: #71717a;
  font-size: 11px;
}
.version-hero-item strong {
  display: block;
  margin-top: 3px;
  color: #18181b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -.03em;
}
.version-hero-arrow {
  color: #a1a1aa;
  font-size: 16px;
}
.version-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-bottom: 12px;
  color: #71717a;
  font-size: 12px;
}
.version-meta a {
  color: #2563eb;
  text-decoration: none;
}
.version-meta a:hover {
  text-decoration: underline;
}
.version-callout {
  margin-bottom: 12px;
  border-radius: 12px;
  background: #f4f4f5;
  padding: 10px 12px;
  color: #3f3f46;
  font-size: 13px;
  line-height: 1.5;
}
.version-callout.is-warn {
  background: #fff7ed;
  color: #9a3412;
}
.version-notes {
  overflow: hidden;
  border: 1px solid rgba(228, 228, 231, .95);
  border-radius: 14px;
  background: #fff;
}
.version-notes-head {
  padding: 10px 14px 8px;
  border-bottom: 1px solid rgba(228, 228, 231, .8);
  color: #52525b;
  font-size: 12px;
  font-weight: 650;
}
.version-notes-md {
  max-height: min(48vh, 420px);
  overflow: auto;
  padding: 12px 16px 16px;
  color: #27272a;
  font-size: 14px;
  line-height: 1.65;
}
.version-notes-empty {
  padding: 28px 16px;
  color: #a1a1aa;
  font-size: 13px;
  text-align: center;
}
.version-dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.version-btn {
  height: 32px;
  border: 1px solid rgba(82, 82, 91, .16);
  border-radius: 10px;
  background: #fff;
  padding: 0 14px;
  color: #27272a;
  font-size: 13px;
}
.version-btn:hover:not(:disabled) {
  background: #f4f4f5;
}
.version-btn.is-primary {
  border-color: rgba(82, 82, 91, .22);
  background: linear-gradient(180deg, #3f3f46, #27272a);
  color: #fff;
}
.version-btn.is-primary:hover:not(:disabled) {
  background: #18181b;
}
.version-btn:disabled {
  opacity: .55;
  cursor: default;
}
</style>
