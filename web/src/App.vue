<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import ConsoleView from "./views/consoleView/ConsoleView.vue";
import {createAttachmentDraftStorage} from "./views/consoleView/attachmentDraftStorage.js";
import { defineLazyView } from "./lazyView.js";
import { installMobileViewport } from "./mobileViewport.js";
import LoginView from "./views/LoginView.vue";
import BearLogoPreview from "./components/BearLogoPreview.vue";
import ConversationTree from "./components/ConversationTree.vue";
import MobileSidebarResources from "./components/MobileSidebarResources.vue";
import "./components/sidebarResources.css";
import {activityInteractionTarget} from "./conversationActivity.js";
import ConsoleMarkdown from "./views/consoleView/ConsoleMarkdown.vue";
import draggable from "vuedraggable";
import { ElMessage, ElMessageBox, ElNotification } from "element-plus";
import { Box, ChatLineRound, Check, Close, DataAnalysis, Delete, DocumentCopy, EditPen, Loading, Monitor, Moon, MoreFilled, Plus, Refresh, RefreshLeft, Setting, Star, StarFilled, Sunny } from "@element-plus/icons-vue";
import { Api, apiError } from "./api";
import {
  isLocalConversationRow as isLocalConversation,
  mergeConversationRows,
  normalizeConversationRows,
} from "./conversationOrdering.js";
import { dragAutoScrollOptions } from "./utils/dragScroll";
import {documentTitle as browserDocumentTitle} from "./pageTitle.js";
import { frontendMismatch } from "./versionSync.js";
import { getThemeState, setThemeMode, subscribeTheme } from "./theme.js";
import ReferencePicker from "./references/ReferencePicker.vue";
import ReferenceInspector from "./references/ReferenceInspector.vue";
import ArtifactPreview from "./artifacts/ArtifactPreview.vue";
import { referenceCatalog, referenceItem, startReferenceCatalog, stopReferenceCatalog } from "./references/catalog.js";

const THEME_OPTIONS = [
  { value: "light", label: "浅色", hint: "始终使用浅色", icon: Sunny },
  { value: "dark", label: "深色", hint: "始终使用深色", icon: Moon },
  { value: "auto", label: "自动", hint: "跟随系统外观", icon: Monitor },
];
const themeState = ref(getThemeState());
const themeMode = computed(() => themeState.value.mode);
const themeModeMeta = computed(() => THEME_OPTIONS.find((item) => item.value === themeMode.value) || THEME_OPTIONS[2]);
const themeButtonTitle = computed(() => `主题：${themeModeMeta.value.label}${themeMode.value === "auto" ? `（当前${themeState.value.dark ? "深色" : "浅色"}）` : ""}。点击切换`);
const stopThemeSubscription = subscribeTheme((state) => { themeState.value = state; });
function chooseThemeMode(mode) {
  themeState.value = setThemeMode(mode);
}

const MemoryView = defineLazyView(() => import("./views/MemoryView.vue"), "记忆管理");
const SecretsView = defineLazyView(() => import("./views/SecretsView.vue"), "凭证库");
const DocsView = defineLazyView(() => import("./views/DocsView.vue"), "文档库");
const SkillsView = defineLazyView(() => import("./views/SkillsView.vue"), "Skills");
const McpView = defineLazyView(() => import("./views/McpView.vue"), "MCP 管理");
const SettingsHubView = defineLazyView(() => import("./views/SettingsHubView.vue"), "设置");
const StatisticsView = defineLazyView(() => import("./views/StatisticsView.vue"), "数据统计");

const nav = [
  { key: "memory", label: "记忆管理", shortLabel: "记忆", icon: "Collection", component: MemoryView },
  { key: "secrets", label: "凭证库", shortLabel: "凭证", icon: "Key", component: SecretsView },
  { key: "docs", label: "文档库", shortLabel: "文档", icon: "Files", component: DocsView },
  { key: "skills", label: "Skills", shortLabel: "Skills", icon: "MagicStick", component: SkillsView },
  { key: "mcp", label: "MCP 管理", shortLabel: "MCP", icon: "Connection", component: McpView },
  { key: "settings", label: "设置", shortLabel: "设置", icon: "Setting", component: SettingsHubView },
  { key: "statistics", label: "数据统计", shortLabel: "统计", icon: "DataAnalysis", component: StatisticsView, headerOnly: true },
];
const pageToPath = {
  console: "/chat",
  memory: "/memory",
  secrets: "/secrets",
  docs: "/docs",
  skills: "/skills",
  mcp: "/mcp",
  settings: "/settings",
  statistics: "/statistics",
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
  "/statistics": "statistics",
};

const desktopNav = computed(() => nav.filter((n) => n.key !== "settings" && !n.headerOnly));
const active = ref("console");
const referenceShelf = shallowRef({open:false,kind:'',anchor:null});
let referenceShelfTimer = null;
function closeReferenceShelf(){window.clearTimeout(referenceShelfTimer);referenceShelf.value={...referenceShelf.value,open:false};}
function keepReferenceShelf(){window.clearTimeout(referenceShelfTimer);}
function leaveReferenceShelf(){window.clearTimeout(referenceShelfTimer);referenceShelfTimer=window.setTimeout(closeReferenceShelf,200);}
function showReferenceShelf(event,key,keyboard=false){
  const kind={memory:'mem',secrets:'secret',docs:'doc'}[key];
  if(!kind||active.value!=='console'||(!keyboard&&!window.matchMedia('(hover: hover) and (pointer: fine)').matches))return;
  // Keep the shelf outside the entire launcher, not over the next grid column.
  const anchor=event.currentTarget.closest('.sidebar-desktop-nav')||event.currentTarget;window.clearTimeout(referenceShelfTimer);
  const delay = keyboard ? 0 : (referenceShelf.value.open && referenceShelf.value.kind !== kind ? 500 : 220);
  referenceShelfTimer=window.setTimeout(()=>{referenceShelf.value={open:true,kind,anchor};}, delay);
}
function referenceNavKey(event,key){if(event.key==='ArrowRight'&&['memory','secrets','docs'].includes(key)){event.preventDefault();showReferenceShelf(event,key,true);}}
function insertShelfReference(reference){window.dispatchEvent(new CustomEvent('openbear:insert-reference',{detail:{reference}}));closeReferenceShelf();}
watch(active,closeReferenceShelf);
const channelStatsText = ref("系统就绪");
async function refreshChannelStats() {
  try {
    const data = await Api.settings();
    const count = Number(data?.providerCount || 0);
    if (count > 0) {
      channelStatsText.value = `${count} 渠道就绪`;
    }
  } catch {}
}
const memoryType = ref("identity");
const settingsSection = ref("channels");
const pageHeaderReady = ref(false);
// Keep fallback navigation available while a newly selected lazy page loads.
watch(active, () => { pageHeaderReady.value = false; });
const appVersion = ref("");
const versionInfo = ref(null);
const versionDialogOpen = ref(false);
const versionBusy = ref(false);
const versionUpdating = ref(false);
const isLoginPath = window.location.pathname === "/login";
const VERSION_POLL_MS = 30000;
let versionPollTimer = null;
let versionRequestInFlight = false;
let refreshPromptOpen = false;
let lastPromptedFrontend = "";
const frontendRefreshRequired = ref(false);
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
const conversationTreeRef = ref(null);
const consoleViewRef = ref(null);
const attachmentDrafts = createAttachmentDraftStorage();
const deletingConversations = new Set();
const activeConversationUuid = ref("");
const selectedFolderId = ref("");
const DRAFT_FOLDER_STORAGE_KEY = "openbear.console.draftFolder.v1";
function readDraftFolderId() {
  try {
    return String(window.localStorage.getItem(DRAFT_FOLDER_STORAGE_KEY) || "");
  } catch {
    return "";
  }
}
const draftFolderId = ref(readDraftFolderId());
function setDraftFolderId(folderId = "") {
  draftFolderId.value = String(folderId || "");
  try {
    if (draftFolderId.value) window.localStorage.setItem(DRAFT_FOLDER_STORAGE_KEY, draftFolderId.value);
    else window.localStorage.removeItem(DRAFT_FOLDER_STORAGE_KEY);
  } catch { /* Storage denial must not discard the in-memory draft target. */ }
}
const sidebarOpen = ref(false);
const showArchivedConversations = ref(false);
const conversationDragActive = ref(false);
const conversationOrderSaving = ref(false);
const conversationMenu = ref({ open: false, x: 0, y: 0, row: null });
const conversationSearchQuery = ref("");
const displayedConversations = computed(() => {
  const list = conversations.value || [];
  const q = String(conversationSearchQuery.value || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter((row) => {
    const title = String(conversationTitle(row) || "").toLowerCase();
    return title.includes(q);
  });
});
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
function localConversation(folderId = draftFolderId.value) {
  return {
    local: true,
    conversationUuid: LOCAL_CONVERSATION_UUID,
    title: "新会话",
    status: "draft",
    currentStatus: "未发送",
    running: false,
    folderId: String(folderId || ""),
    parentId: String(folderId || ""),
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
  if (active.value === "settings") {
    params.set("section", settingsSection.value || "channels");
    // Keep a system-setting deep link through the initial route normalization.
    if (settingsSection.value === "system-settings" && window.location.pathname === "/settings") {
      const setting = new URLSearchParams(window.location.search).get("setting");
      if (setting) params.set("setting", setting);
    }
  }
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
function conversationTitle(row) {
  if (!row) return "新会话";
  if (!row.local && row.conversationUuid) {
    const catalog = referenceItem({kind: "chat", id: row.conversationUuid});
    if (catalog?.label) return catalog.label;
  }
  return row.title || "新会话";
}
const activeConversationPath = computed(() => conversationTreeRef.value?.conversationPath(activeConversationUuid.value) || "");
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
    row.folderId || "",
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
  if (conversationTreeRef.value?.refresh) {
    await conversationTreeRef.value.refresh(options);
    return true;
  }
  return false;
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
function focusLocalConversation(folderId = draftFolderId.value) {
  active.value = "console";
  const existing = conversations.value.find(isLocalConversation);
  const target = existing ? String(existing.folderId || "") : String(folderId || "");
  setDraftFolderId(target);
  setConversationsIfChanged([localConversation(target), ...conversations.value.filter((row) => !isLocalConversation(row))]);
  activeConversationUuid.value = LOCAL_CONVERSATION_UUID;
  closeSidebar();
  syncRoute();
  void refreshConsoleAfterPropSync();
  void nextTick().then(() => conversationTreeRef.value?.revealDraft(target));
}
async function startConsoleNewSession() {
  await handleTreeNewConversation(selectedFolderId.value);
}
async function handleTreeNewConversation(folderId = "") {
  const target = String(folderId || "");
  const existing = conversations.value.find(isLocalConversation);
  if (existing && String(existing.folderId || "") !== target) {
    try {
      await ElMessageBox.confirm(
        `当前未发送草稿属于「${existing.folderId ? '另一目录' : '临时会话'}」。是否明确把该草稿（含文字和附件）改到新的目标？`,
        "更改草稿归属",
        { confirmButtonText: "更改归属", cancelButtonText: "保持原归属", type: "warning" },
      );
      setDraftFolderId(target);
      setConversationsIfChanged(conversations.value.map((row) => isLocalConversation(row) ? { ...row, folderId: target, parentId: target } : row));
    } catch { /* Repeated new keeps the existing draft and its original folder. */ }
  } else if (!existing) setDraftFolderId(target);
  focusLocalConversation(draftFolderId.value);
  ElMessage.success(existing ? "已聚焦未发送的新会话" : "已开启新会话");
}
function handleTreeRows(rows = []) {
  const draft = conversations.value.find(isLocalConversation);
  const next = [...(draft ? [draft] : []), ...(Array.isArray(rows) ? rows.filter((row) => !isLocalConversation(row)) : [])];
  setConversationsIfChanged(next);
}
function handleTreeSelectedFolder(folderId = "") { selectedFolderId.value = String(folderId || ""); }
function handleTreeFolderRemoved({ folderId, targetFolderId = "" }) {
  const target = String(targetFolderId || "");
  if (selectedFolderId.value === folderId) selectedFolderId.value = target;
  const draft = currentDraftConversation();
  if (draft && String(draft.folderId || "") === folderId) {
    setDraftFolderId(target);
    setConversationsIfChanged(conversations.value.map((row) => isLocalConversation(row) ? { ...row, folderId: target, parentId: target } : row));
    if (activeConversationUuid.value === draft.conversationUuid) void nextTick().then(() => conversationTreeRef.value?.revealDraft(target));
  }
}
async function handleTreeOpen(row) {
  if (!row?.conversationUuid) return;
  const target = activityInteractionTarget(row);
  await openConversation(row);
  await nextTick();
  if (target && activeConversationUuid.value === target.conversationUuid) {
    consoleViewRef.value?.focusPendingInteraction(target);
  }
}
function currentDraftConversation() { return conversations.value.find(isLocalConversation) || null; }
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
    if (uuid) {
      active.value = "console";
      activeConversationUuid.value = uuid;
      syncRoute();
      await nextTick();
      if (conversationTreeRef.value?.revealConversation) await conversationTreeRef.value.revealConversation(uuid);
      else await loadConversations({ conversationUuid: uuid, reveal: true });
      void refreshConsoleAfterPropSync();
    } else await loadConversations();
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

function discardConversationDraft(conversationUuid) {
  if (consoleViewRef.value?.discardConversationDraft) {
    consoleViewRef.value.discardConversationDraft(conversationUuid);
    return;
  }
  // The console may be unmounted while a settings page is open. Remove only
  // this conversation's persisted text and files, leaving other drafts intact.
  try {
    const key = "openbear.console.drafts.v1";
    const drafts = JSON.parse(window.localStorage.getItem(key) || "{}");
    if (drafts && typeof drafts === "object" && !Array.isArray(drafts) && Object.hasOwn(drafts, conversationUuid)) {
      delete drafts[conversationUuid];
      window.localStorage.setItem(key, JSON.stringify(drafts));
    }
  } catch { /* Unavailable storage must not block an in-memory removal. */ }
  return attachmentDrafts.remove(conversationUuid);
}
async function conversationAfterRemoval(row) {
  const folderId = String(row.folderId || "");
  const eligible = (item) => item.conversationUuid !== row.conversationUuid && !isLocalConversation(item) && !item.archived;
  const siblings = conversations.value.filter((item) => !item.archived && !isLocalConversation(item) && String(item.folderId || "") === folderId);
  const index = siblings.findIndex((item) => item.conversationUuid === row.conversationUuid);
  const known = index >= 0 ? siblings[index + 1] || siblings[index - 1] : siblings.find(eligible);
  if (known && eligible(known)) return known;
  // Lazy branches may have other conversations beyond loaded pages. A local
  // deletion needs no DELETE request, but can still choose a persisted sibling.
  try {
    let cursor = "";
    do {
      const data = await Api.conversationTreeChildren({ ...(folderId ? { parentId: folderId } : { systemNode: "temporary" }), limit: 50, ...(cursor ? { cursor } : {}) });
      const candidate = (data.items || []).find((item) => item.kind === "conversation" && eligible(item));
      if (candidate) return candidate;
      const next = data.hasMore ? String(data.nextCursor || "") : "";
      if (!next || next === cursor) break;
      cursor = next;
    } while (cursor);
  } catch { /* Successful deletion stays successful if successor lookup fails. */ }
  return conversations.value.find(eligible) || null;
}
async function deleteConversation(row) {
  const uuid = row?.conversationUuid;
  if (!uuid || deletingConversations.has(uuid)) return;
  if (isRunning(row)) return ElMessage.warning("运行中的会话不能删除");
  deletingConversations.add(uuid);
  try {
    const local = isLocalConversation(row);
    try {
      await ElMessageBox.confirm(
        local ? "确定移除这个未发送的新会话吗？其中的草稿文字和待发送附件会一并清除。" : `确定删除会话「${conversationTitle(row)}」吗？所有消息和历史记录将被清除。`,
        "删除会话",
        { confirmButtonText: "删除", cancelButtonText: "取消", type: "warning" },
      );
    } catch { return; }
    const current = conversations.value.find((item) => item.conversationUuid === uuid);
    if (local && !current) return; // It may have become a persisted conversation while the dialog was open.
    if (isRunning(current || row)) return ElMessage.warning("运行中的会话不能删除");
    if (!local) await Api.deleteConversation(uuid);
    const wasActive = activeConversationUuid.value === uuid;
    const next = wasActive ? await conversationAfterRemoval(row) : null;
    // Clear the mounted editor before changing its prop; otherwise its navigation
    // watcher would save the deleted text back into local storage.
    // The unmounted fallback must finish deleting stored files before a fresh
    // local:new composer can mount and read that same key again.
    await discardConversationDraft(uuid);
    setConversationsIfChanged(conversations.value.filter((item) => item.conversationUuid !== uuid));
    if (local) setDraftFolderId("");
    await nextTick();
    conversationTreeRef.value?.forgetConversation(uuid);
    let revealed = false;
    if (activeConversationUuid.value === uuid) {
      if (next?.conversationUuid) {
        activeConversationUuid.value = next.conversationUuid;
        selectedFolderId.value = String(next.folderId || "");
        syncRoute({ replace: true });
        await nextTick();
        await conversationTreeRef.value?.revealConversation(next.conversationUuid);
        revealed = true;
      } else focusLocalConversation();
    }
    if (!local && !revealed) await loadConversations({ trackActive: false });
    ElMessage.success(local ? "草稿会话已移除" : "会话已删除");
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally { deletingConversations.delete(uuid); }
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
async function handleConsoleConversationCreated(uuid) {
  if (!uuid) return;
  if (isLocalConversation(uuid)) {
    focusLocalConversation();
    return;
  }
  setConversationsIfChanged(conversations.value.filter((row) => !isLocalConversation(row)));
  setDraftFolderId("");
  activeConversationUuid.value = uuid;
  syncRoute({ replace: true });
  await nextTick();
  if (conversationTreeRef.value?.revealConversation) await conversationTreeRef.value.revealConversation(uuid);
  else await loadConversations({ conversationUuid: uuid, reveal: true });
}
function handleConsoleRefreshList() { void loadConversations(); }
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

async function requestFrontendRefresh() {
  if (isLoginPath || refreshPromptOpen) return;
  refreshPromptOpen = true;
  try {
    await ElMessageBox.confirm(
      "服务已更新，请刷新以载入配套前端。聊天文字草稿沿用浏览器本地保存；未提交的附件、交互答案和设置请先保存或复制。不会自动刷新当前页面。",
      "前端版本已变化",
      { confirmButtonText: "已处理未提交内容，刷新", cancelButtonText: "保留当前编辑", type: "info", closeOnClickModal: false },
    );
    await nextTick(); // Flush the existing console draft watcher before navigation.
    window.location.reload();
  } catch { /* Keep mounted views and their unsubmitted input intact. */ }
  finally { refreshPromptOpen = false; }
}

function observeFrontendVersion(data) {
  const mismatch = frontendMismatch(data);
  if (mismatch === null) return;
  frontendRefreshRequired.value = Boolean(mismatch);
  if (!mismatch) lastPromptedFrontend = "";
  else if (mismatch !== lastPromptedFrontend) {
    lastPromptedFrontend = mismatch;
    void requestFrontendRefresh();
  }
}

function checkVersionOnResume() {
  if (!isLoginPath && document.visibilityState === "visible") void loadVersionInfo();
}

function handlePreloadError(event) {
  if (isLoginPath) return;
  event.preventDefault();
  frontendRefreshRequired.value = true;
  void requestFrontendRefresh();
}

let lastNotifiedVersionResult = "";
async function applyVersionInfo(data) {
  if (!data || isLoginPath) return;
  versionInfo.value = {...(versionInfo.value || {}), ...data};
  appVersion.value = data.version || "";
  observeFrontendVersion(data);
  const banner = resultBanner(data.lastResult);
  const resultKey = JSON.stringify([data.lastResult?.status,data.lastResult?.toVersion,data.lastResult?.finishedAt]);
  if (banner && !versionUpdating.value && resultKey !== lastNotifiedVersionResult) {
    lastNotifiedVersionResult = resultKey;
    const status = data.lastResult?.status;
    ElNotification({title: status === "success" ? "更新完成" : "上次更新未成功", message: banner,
      type: status === "success" ? "success" : "warning", duration: 8000});
    try { await Api.ackSystemUpdate(); } catch { /* later refresh can acknowledge */ }
  }
  if (versionUpdating.value) {
    const phase = data.phase || "idle";
    if (["idle", "done"].includes(phase) && data.lastResult?.status === "success" && data.lastResult.toVersion === data.version) {
      versionUpdating.value = false;
      ElMessage.success(data.lastResult.message || `已更新到 v${data.version}`);
    } else if (["idle", "done"].includes(phase) && data.lastResult && ["rolled_back", "failed"].includes(data.lastResult.status)) {
      versionUpdating.value = false;
      ElMessage.error(data.lastResult.message || "更新失败");
    }
  }
}
watch(() => referenceCatalog.version, data => { if (data) void applyVersionInfo(data); });
watch(() => [referenceCatalog.connected, referenceCatalog.ready], scheduleVersionPoll);
async function loadVersionInfo() {
  if (isLoginPath || versionRequestInFlight) return;
  versionRequestInFlight = true;
  try { await applyVersionInfo(await Api.systemVersion()); }
  catch { if (!appVersion.value) appVersion.value = ""; }
  finally { versionRequestInFlight = false; }
}

function scheduleVersionPoll() {
  if (isLoginPath) return;
  if (versionPollTimer) window.clearTimeout(versionPollTimer);
  if (referenceCatalog.connected && referenceCatalog.ready) return;
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
if (isLocalConversation(activeConversationUuid.value)) {
  setConversationsIfChanged([localConversation(draftFolderId.value)]);
}

watch(pageDocumentTitle, (title) => {
  if (typeof document !== "undefined") document.title = title;
}, {immediate: true});

let stopMobileViewport = null;
onMounted(() => {
  if (!isLoginPath) stopMobileViewport = installMobileViewport({
    beforeChange: () => consoleViewRef.value?.captureMobileViewportAnchor(),
    afterChange: anchor => consoleViewRef.value?.restoreMobileViewportAnchor(anchor),
  });
  window.addEventListener("popstate", applyRouteFromLocation);
  window.addEventListener("openbear:conversations-refresh", handleExternalConversationsRefresh);
  window.addEventListener("click", closeConversationMenu);
  window.addEventListener("scroll", closeConversationMenu, true);
  window.addEventListener("resize", closeConversationMenu);
  window.addEventListener("keydown", handleConversationMenuKeydown);
  if (!isLoginPath) {
    startReferenceCatalog();
    void loadVersionInfo().finally(scheduleVersionPoll);
    void refreshChannelStats();
    window.addEventListener("focus", checkVersionOnResume);
    window.addEventListener("pageshow", checkVersionOnResume);
    document.addEventListener("visibilitychange", checkVersionOnResume);
    window.addEventListener("vite:preloadError", handlePreloadError);
  }
});
onBeforeUnmount(() => {
  stopMobileViewport?.();
  closeReferenceShelf();
  stopReferenceCatalog({clear:true});
  stopThemeSubscription();
  window.removeEventListener("popstate", applyRouteFromLocation);
  window.removeEventListener("openbear:conversations-refresh", handleExternalConversationsRefresh);
  window.removeEventListener("click", closeConversationMenu);
  window.removeEventListener("scroll", closeConversationMenu, true);
  window.removeEventListener("resize", closeConversationMenu);
  window.removeEventListener("keydown", handleConversationMenuKeydown);
  if (conversationsRefreshTimer) window.clearTimeout(conversationsRefreshTimer);
  if (versionPollTimer) window.clearTimeout(versionPollTimer);
  window.removeEventListener("focus", checkVersionOnResume);
  window.removeEventListener("pageshow", checkVersionOnResume);
  document.removeEventListener("visibilitychange", checkVersionOnResume);
  window.removeEventListener("vite:preloadError", handlePreloadError);
});
</script>

<template>
  <LoginView v-if="isLoginPath" />
  <div v-else class="app-shell h-full flex" :class="{'is-console': active === 'console', 'is-settings': active === 'settings' && pageHeaderReady, 'is-admin': ['memory', 'secrets', 'docs', 'skills', 'mcp'].includes(active) && pageHeaderReady}">
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
      <div class="mobile-app-brand">
        <span class="mobile-brand-logo"><BearLogoPreview /></span>
        <strong>{{ nav.find(item => item.key === active)?.label || 'OpenBear' }}</strong>
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
      class="app-sidebar w-[280px] shrink-0 flex flex-col border-r border-ob-border bg-ob-sidebar p-3 text-ob-strong"
      :class="{'is-open': sidebarOpen}"
      @contextmenu.self="conversationTreeRef?.openRootMenu($event)"
    >
      <div class="sidebar-heading mb-3 flex items-center gap-2 rounded-2xl px-2 py-2">
        <div class="sidebar-brand-logo grid h-9 w-9 shrink-0 place-items-center bg-transparent">
          <BearLogoPreview />
        </div>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[14px] font-semibold leading-tight">OpenBear</div>
          <div class="sidebar-caption mt-0.5 truncate text-[10px] leading-tight text-ob-muted">Web 控制台</div>
        </div>
        <div class="sidebar-meta-actions flex shrink-0 items-center gap-1">
          <button
            type="button"
            class="theme-entry statistics-entry"
            :class="{'is-active': active === 'statistics'}"
            title="数据统计"
            aria-label="打开数据统计"
            :aria-current="active === 'statistics' ? 'page' : undefined"
            data-testid="statistics-entry"
            @click="selectNav('statistics')"
          >
            <el-icon :size="14"><DataAnalysis /></el-icon>
          </button>
          <el-dropdown trigger="click" placement="bottom-end" @command="chooseThemeMode">
            <button
              type="button"
              class="theme-entry"
              :title="themeButtonTitle"
              :aria-label="themeButtonTitle"
              aria-haspopup="menu"
              data-testid="theme-menu-trigger"
            >
              <el-icon :size="14"><component :is="themeModeMeta.icon" /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu class="theme-mode-menu" aria-label="选择主题模式">
                <el-dropdown-item
                  v-for="item in THEME_OPTIONS"
                  :key="item.value"
                  :command="item.value"
                  :class="{'is-theme-selected': themeMode === item.value}"
                  role="menuitemradio"
                  :aria-checked="themeMode === item.value ? 'true' : 'false'"
                  :data-theme-mode="item.value"
                >
                  <el-icon :size="15"><component :is="item.icon" /></el-icon>
                  <span class="theme-menu-copy"><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
                  <el-icon v-if="themeMode === item.value" class="theme-menu-check" :size="14"><Check /></el-icon>
                  <span v-else class="theme-menu-check-placeholder" aria-hidden="true"></span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button type="button" class="sidebar-close theme-entry" aria-label="关闭导航" @click="closeSidebar"><el-icon :size="16"><Close/></el-icon></button>
        </div>
      </div>

      <button
        type="button"
        class="sidebar-new-session mb-2 flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm font-medium text-ob-text transition-colors hover:bg-ob-soft/60"
        @click="startConsoleNewSession"
      >
        <el-icon :size="16" class="text-ob-subtle"><Plus /></el-icon>
        <span>新会话</span>
      </button>

      <nav class="sidebar-desktop-nav sidebar-resource-grid text-sm" aria-label="资源与设置">
        <button
          v-for="n in desktopNav"
          :key="n.key"
          type="button"
          :aria-current="active === n.key ? 'page' : undefined"
          @click="closeReferenceShelf(); selectNav(n.key)"
          @pointerenter="showReferenceShelf($event,n.key)"
          @pointerleave="leaveReferenceShelf"
          @keydown="referenceNavKey($event,n.key)"
          class="sidebar-resource-tile"
        >
          <el-icon class="sidebar-resource-icon" :size="16" aria-hidden="true"><component :is="n.icon" /></el-icon>
          <span class="sidebar-resource-label">{{ n.shortLabel || n.label }}</span>
        </button>
      </nav>
      <ReferencePicker :open="referenceShelf.open" :anchor="referenceShelf.anchor" :kind="referenceShelf.kind" :current-conversation="activeConversationUuid" placement="right-start" searchable allow-drag @select="insertShelfReference" @close="closeReferenceShelf" @enter="keepReferenceShelf" @leave="leaveReferenceShelf"/>
      <ReferenceInspector :current-conversation="activeConversationUuid"/>
      <ArtifactPreview :navigation-key="`${active}:${activeConversationUuid}`"/>

      <div class="sidebar-conversations -mx-3 mt-3 flex min-h-0 flex-1 flex-col border-t border-ob-border pt-2" @contextmenu.self="conversationTreeRef?.openRootMenu($event)">
        <ConversationTree
          ref="conversationTreeRef"
          :active-conversation-uuid="activeConversationUuid"
          :draft-conversation="currentDraftConversation()"
          @open="handleTreeOpen"
          @new-conversation="handleTreeNewConversation"
          @rows="handleTreeRows"
          @selected-folder="handleTreeSelectedFolder"
          @refresh-list="handleConsoleRefreshList"
          @delete-conversation="deleteConversation"
          @folder-removed="handleTreeFolderRemoved"
        />
        <!-- legacy flat-list implementation retained below only as source compatibility; hidden and not mounted -->
        <div v-if="false">
        <!-- 会话头部工具栏 -->
        <div class="mb-1.5 flex items-center justify-between px-4">
          <div class="flex items-center gap-1.5 text-xs font-semibold text-ob-subtle">
            <el-icon :size="14" class="text-ob-muted"><ChatLineRound /></el-icon>
            <span>会话</span>
            <span class="text-[11px] font-normal text-ob-muted">({{ conversations.length }})</span>
          </div>
          <div class="flex items-center gap-0.5">
            <button
              type="button"
              class="session-tool-btn"
              :class="showArchivedConversations ? 'is-active' : ''"
              :aria-pressed="showArchivedConversations"
              :title="showArchivedConversations ? '隐藏已归档会话' : '显示已归档会话'"
              @click="toggleShowArchivedConversations"
            >
              <el-icon :size="14"><Box /></el-icon>
            </button>
            <button
              type="button"
              class="session-tool-btn"
              :disabled="conversationsLoading"
              title="刷新会话列表"
              @click="loadConversations"
            >
              <el-icon :size="14" :class="conversationsLoading && 'animate-spin'"><Refresh /></el-icon>
            </button>
          </div>
        </div>

        <!-- 快速搜索会话栏 -->
        <div class="px-3 mb-2">
          <div class="relative flex items-center">
            <input
              v-model="conversationSearchQuery"
              class="session-search-input"
              placeholder="搜索会话标题…"
            />
            <span class="session-search-icon" aria-hidden="true">🔍</span>
            <button
              v-if="conversationSearchQuery"
              type="button"
              class="session-search-clear"
              title="清空搜索"
              @click="conversationSearchQuery = ''"
            >×</button>
          </div>
        </div>

        <div ref="conversationListRef" class="min-h-0 flex-1 overflow-y-auto px-2 py-1 space-y-1 scrollbar-none" :class="conversationDragActive && 'select-none'">
          <div v-if="conversationsLoading && !conversations.length" class="mx-2 rounded-xl bg-ob-soft/70 p-4 text-center text-xs text-ob-muted">加载中…</div>
          <div v-else-if="!conversations.length" class="mx-2 rounded-xl bg-ob-soft/70 p-4 text-center text-xs text-ob-muted">暂无会话</div>
          <div v-else-if="conversationSearchQuery && !displayedConversations.length" class="mx-2 rounded-xl bg-ob-soft/70 p-4 text-center text-xs text-ob-muted">未找到匹配「{{ conversationSearchQuery }}」的会话</div>
          <draggable
            v-else
            v-model="conversations"
            item-key="conversationUuid"
            class="space-y-1"
            :animation="160"
            :disabled="Boolean(conversationSearchQuery) || conversationsLoading || conversationOrderSaving"
            :move="canMoveConversation"
            :filter="'.conversation-menu-trigger, .session-hover-btn'"
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
                v-show="!conversationSearchQuery || displayedConversations.some(c => c.conversationUuid === row.conversationUuid)"
                :data-conversation-uuid="row.conversationUuid"
                class="session-card group relative rounded-xl transition-all"
                :class="[
                  active === 'console' && activeConversationUuid === row.conversationUuid ? 'is-active' : 'is-inactive',
                  row.pinned ? 'is-pinned' : '',
                  isRunning(row) ? 'is-running' : '',
                ]"
                @contextmenu="openConversationMenu($event, row)"
              >
                <!-- 激活态左侧指示条 -->
                <span v-if="active === 'console' && activeConversationUuid === row.conversationUuid" class="session-active-bar" aria-hidden="true"></span>

                <button class="block w-full min-w-0 overflow-hidden px-3 py-1.5 text-left" @click="openConversation(row)">
                  <!-- 标题行 -->
                  <div class="flex min-w-0 items-center gap-1.5 leading-tight">
                    <span v-if="row.pinned" class="session-pinned-star shrink-0" title="置顶会话">
                      <el-icon :size="12"><StarFilled /></el-icon>
                    </span>
                    <span class="min-w-0 flex-1 truncate text-xs session-card-title font-medium leading-tight" :title="conversationTitle(row)">
                      {{ conversationTitle(row) }}
                    </span>
                    <!-- 运行中指示器：呼吸绿点 + 标签 -->
                    <span v-if="isRunning(row)" class="session-running-badge shrink-0" title="正在执行任务">
                      <span class="running-ping-dot"></span>
                      <span>运行中</span>
                    </span>
                  </div>

                  <!-- 第二行信息元数据：创建时间 + 统计指标 (消息数、花费) -->
                  <div class="mt-0.5 flex min-w-0 items-center justify-between text-[10.5px] leading-tight text-ob-muted">
                    <span class="shrink-0 session-date">{{ createdTime(row) }}</span>
                    <span class="session-metrics-pill shrink-0" :title="conversationStats(row)">
                      <span class="truncate">{{ conversationStats(row) }}</span>
                      <el-icon v-if="isRunning(row)" :size="11" class="shrink-0 animate-spin text-emerald-600 ml-0.5" title="正在运行"><Loading /></el-icon>
                    </span>
                  </div>
                </button>

                <!-- 悬停快捷操作组 -->
                <div class="session-hover-actions absolute right-1.5 top-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    type="button"
                    class="session-hover-btn"
                    :title="row.pinned ? '取消置顶' : '置顶会话'"
                    @click.stop="togglePinConversation(row)"
                  >
                    <el-icon :size="12"><component :is="row.pinned ? StarFilled : Star" /></el-icon>
                  </button>
                  <button
                    type="button"
                    class="session-hover-btn conversation-menu-trigger"
                    title="会话菜单"
                    @click.stop="openConversationMenu($event, row)"
                  >
                    <el-icon :size="13"><MoreFilled /></el-icon>
                  </button>
                </div>
              </div>
            </template>
          </draggable>
        </div>
        </div>
      </div>
      <footer class="sidebar-desktop-footer">
        <button
          type="button"
          class="sidebar-footer-settings"
          :class="{ 'is-active': active === 'settings' }"
          :aria-current="active === 'settings' ? 'page' : undefined"
          title="系统与通道设置"
          @click="closeReferenceShelf(); selectNav('settings')"
        >
          <el-icon :size="15"><Setting /></el-icon>
          <span>系统设置</span>
        </button>
        <div class="sidebar-footer-status">
          <button type="button" class="version-entry sidebar-version" :class="{'has-update': versionInfo?.updateAvailable}" :title="versionInfo?.updateAvailable ? '有新版本，点击查看' : '查看版本'" @click="openVersionDialog">v{{ appVersion || '…' }}<span v-if="versionInfo?.updateAvailable" class="version-dot" aria-hidden="true"></span></button>
        </div>
      </footer>
      <MobileSidebarResources :items="nav.filter(item => !item.headerOnly)" :active="active" :sidebar-open="sidebarOpen" @select="closeReferenceShelf(); selectNav($event)">
        <template #footer><div class="sidebar-mobile-status"><span class="status-indicator-dot"></span><span>{{ channelStatsText }}</span><button type="button" class="version-entry sidebar-version" :class="{'has-update': versionInfo?.updateAvailable}" :title="versionInfo?.updateAvailable ? '有新版本，点击查看' : '查看版本'" @click="openVersionDialog">v{{ appVersion || '…' }}<span v-if="versionInfo?.updateAvailable" class="version-dot" aria-hidden="true"></span></button></div></template>
      </MobileSidebarResources>
    </aside>

    <Teleport v-if="false" to="body">
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
      <el-alert v-if="frontendRefreshRequired" type="info" :closable="false" show-icon class="shrink-0" data-testid="frontend-refresh-required">
        <template #title>
          页面版本已过期，请先处理未提交内容，再刷新以免接口不兼容。
          <el-button link type="primary" @click="requestFrontendRefresh">刷新页面</el-button>
        </template>
      </el-alert>
      <ConsoleView
        v-if="active === 'console'"
        ref="consoleViewRef"
        :conversation-uuid="activeConversationUuid"
        :canonical-title="activeConversationTitle"
        :conversation-path="activeConversationPath"
        :navigation-obscured="sidebarOpen"
        :folder-id="isLocalConversation(activeConversationUuid) ? draftFolderId : selectedFolderId"
        @conversation-created="handleConsoleConversationCreated"
        @conversations-refresh="handleConsoleRefreshList"
      >
        <template #mobile-navigation>
          <button
            type="button"
            class="mobile-sidebar-toggle"
            :aria-expanded="sidebarOpen"
            aria-controls="openbear-sidebar"
            aria-label="打开导航"
            @click="sidebarOpen = true"
          ><span></span><span></span><span></span></button>
        </template>
      </ConsoleView>
      <MemoryView v-else-if="active === 'memory'" :active-type="memoryType" @type-changed="handleMemoryTypeChanged" @mobile-header-ready="pageHeaderReady = $event">
        <template #mobile-navigation>
          <button
            type="button"
            class="mobile-sidebar-toggle"
            :aria-expanded="sidebarOpen"
            aria-controls="openbear-sidebar"
            aria-label="打开导航"
            @click="sidebarOpen = true"
          ><span></span><span></span><span></span></button>
        </template>
      </MemoryView>
      <component
        v-else
        :is="activeView"
        :section="settingsSection"
        :navigation-obscured="sidebarOpen"
        @section-changed="handleSettingsSectionChanged"
        @mobile-header-ready="pageHeaderReady = $event"
      >
        <template #mobile-navigation>
          <button
            v-if="['settings', 'secrets', 'docs', 'skills', 'mcp'].includes(active)"
            type="button"
            class="mobile-sidebar-toggle"
            :aria-expanded="sidebarOpen"
            aria-controls="openbear-sidebar"
            aria-label="打开导航"
            @click="sidebarOpen = true"
          ><span></span><span></span><span></span></button>
        </template>
      </component>
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
/* This top-level action is phone-only; desktop keeps the conversation tree actions. */
.sidebar-new-session { display: none; }
.app-sidebar { background: var(--ob-chat-sidebar); color: var(--ob-chat-text); border-color: var(--ob-chat-line); }
.sidebar-heading { min-height: 53px; margin-bottom: 7px; padding-inline: 5px; }
.sidebar-brand-logo { width: 29px; height: 29px; }
.sidebar-conversations { border-color: var(--ob-chat-line); }
.theme-entry.sidebar-close { display: none; }
.sidebar-version { display: inline-flex; flex: none; align-items: center; gap: 3px; border: 0; background: transparent; padding: 4px 0; color: var(--ob-chat-muted); font-size: 9px; cursor: pointer; }
.sidebar-version.has-update { color: var(--ob-warning); }
.sidebar-mobile-status { display: flex; align-items: center; gap: 6px; padding: 0 8px; color: var(--ob-chat-muted); font-size: 9px; }
.sidebar-mobile-status .sidebar-version { margin-left: auto; }
.sidebar-mobile-status .status-indicator-dot { width: 4px; height: 4px; }
@media (min-width: 761px) and (max-width: 1250px) { .app-sidebar { width: 250px; } }

.theme-entry {
  display: grid;
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-chat-subtle);
  box-shadow: none;
  cursor: pointer;
  transition: border-color .14s ease, background .14s ease, color .14s ease;
}
.theme-entry:hover,
.theme-entry:focus-visible {
  border-color: var(--ob-border-strong);
  background: var(--ob-surface);
  color: var(--ob-text);
  outline: none;
}
.theme-entry:focus-visible {
  box-shadow: 0 0 0 3px var(--ob-focus);
}
.statistics-entry.is-active {
  border-color: var(--ob-focus);
  background: var(--ob-selected);
  color: var(--ob-blue);
}
.version-entry {
  cursor: pointer;
}
.version-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--ob-warning);
  box-shadow: 0 0 0 3px var(--ob-warning-soft);
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
  border: 1px solid var(--ob-border-strong);
  border-radius: 12px;
  background:
    rgb(var(--ob-surface-raised-rgb) / .95);
  color: var(--ob-text);
  box-shadow:
    0 28px 70px rgb(var(--ob-shadow-rgb) / .24),
    0 8px 22px rgb(var(--ob-shadow-rgb) / .12),
    var(--ob-shadow-inset);
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
  color: var(--ob-text);
  font-size: 14px;
  line-height: 1.25;
  text-align: left;
  outline: none;
}

.context-menu-item :deep(.el-icon) {
  color: var(--ob-text-subtle);
  font-size: 15px;
}

.context-menu-item:hover:not(:disabled),
.context-menu-item:focus-visible:not(:disabled) {
  background: var(--ob-blue);
  color: var(--ob-text-inverse);
}

.context-menu-item:hover:not(:disabled) :deep(.el-icon),
.context-menu-item:focus-visible:not(:disabled) :deep(.el-icon),
.context-menu-item:hover:not(:disabled) .context-menu-shortcut,
.context-menu-item:focus-visible:not(:disabled) .context-menu-shortcut {
  color: rgb(var(--ob-text-inverse-rgb) / .84);
}

.context-menu-item.danger:hover:not(:disabled),
.context-menu-item.danger:focus-visible:not(:disabled) {
  background: var(--ob-danger);
}

.context-menu-item:disabled {
  color: var(--ob-text-disabled);
  cursor: default;
}

.context-menu-item:disabled :deep(.el-icon),
.context-menu-item:disabled .context-menu-shortcut {
  color: var(--ob-text-disabled);
}

.context-menu-shortcut {
  color: var(--ob-text-subtle);
  font-size: 13px;
  letter-spacing: .01em;
}

.context-menu-separator {
  height: 1px;
  margin: 5px 6px;
  background: var(--ob-border);
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
    height: calc(48px + env(safe-area-inset-top, 0px));
    align-items: center;
    gap: 10px;
    border-bottom: 1px solid var(--ob-border);
    background: rgb(var(--ob-header-rgb) / .92);
    padding: env(safe-area-inset-top, 0px) max(12px, env(safe-area-inset-right, 0px)) 0 max(12px, env(safe-area-inset-left, 0px));
    box-shadow: 0 1px 8px rgb(var(--ob-shadow-rgb) / .04);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .mobile-sidebar-toggle {
    display: grid;
    width: 44px;
    height: 44px;
    flex: 0 0 auto;
    place-content: center;
    gap: 4px;
    border: 1px solid var(--ob-border);
    border-radius: 11px;
    background: var(--ob-surface);
    color: var(--ob-text);
    box-shadow: 0 4px 14px var(--ob-border);
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
    color: var(--ob-text);
    font-size: 13px;
  }

  .mobile-brand-logo {
    display: grid;
    width: 25px;
    height: 25px;
    place-items: center;
    border-radius: 8px;
    background: transparent;
    padding: 4px;
    box-shadow: inset 0 0 0 1px var(--ob-border);
  }

  .app-main {
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    padding-top: calc(48px + env(safe-area-inset-top, 0px));
  }

  /* Mounted combined headers own navigation; lazy loading keeps the app bar. */
  .app-shell.is-console .mobile-app-bar,
  .app-shell.is-settings .mobile-app-bar,
  .app-shell.is-admin .mobile-app-bar { display: none; }
  .app-shell.is-console .app-main,
  .app-shell.is-settings .app-main,
  .app-shell.is-admin .app-main { padding-top: 0; }
  .app-shell.is-console .mobile-sidebar-toggle,
  .app-shell.is-settings .mobile-sidebar-toggle,
  .app-shell.is-admin .mobile-sidebar-toggle {
    border: 0;
    background: transparent;
    box-shadow: none;
  }

  .app-sidebar {
    position: absolute;
    z-index: 100;
    inset: 0 auto 0 0;
    padding-top: calc(12px + env(safe-area-inset-top, 0px));
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 0px));
    width: min(350px, calc(100vw - 48px)) !important;
    max-width: calc(100vw - 48px);
    visibility: hidden;
    pointer-events: none;
    transform: translateX(-102%);
    box-shadow: 18px 0 52px rgb(var(--ob-shadow-rgb) / .18);
    transition: transform .2s cubic-bezier(.2, .8, .2, 1), visibility 0s linear .2s;
    will-change: transform;
  }

  /* Mobile sidebar hierarchy: compact identity + one primary action, tree,
     then an on-demand resource launcher. Desktop classes remain untouched. */
  .app-sidebar .sidebar-heading {
    flex: 0 0 44px;
    height: 44px;
    margin-bottom: 4px;
    padding: 0;
  }
  .app-sidebar .sidebar-brand-logo { width: 28px; height: 28px; padding: 0; }
  .app-sidebar .sidebar-caption { display: block; }
  .app-sidebar .sidebar-desktop-nav { display: none; }
  .app-sidebar .sidebar-new-session {
    display: flex;
    flex: 0 0 44px;
    height: 44px;
    margin-bottom: 0;
    padding: 0 8px;
    gap: 10px;
  }
  .app-sidebar .sidebar-conversations { margin-top: 8px; }
  .app-sidebar .theme-entry { width: 44px; height: 44px; border: 0; background: transparent; box-shadow: none; }
  .app-sidebar .version-entry { min-height: 30px; padding: 0 4px; border: 0; background: transparent; box-shadow: none; }
  .app-sidebar .sidebar-close { display: grid; }

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
    background: var(--ob-mask);
    padding: 0;
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
  }
}

/* === 会话快速搜索栏 === */
.session-search-input {
  width: 100%;
  height: 28px;
  padding: 0 24px 0 26px;
  font-size: 11.5px;
  background: var(--ob-surface-soft);
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--ob-text);
  outline: none;
  transition: all 0.15s ease;
}
.session-search-input:focus {
  background: var(--ob-surface);
  border-color: var(--ob-border-strong);
  box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / .04), 0 0 0 2px var(--ob-focus);
}
.session-search-icon {
  position: absolute;
  left: 8px;
  font-size: 11px;
  color: var(--ob-text-muted);
  pointer-events: none;
}
.session-search-clear {
  position: absolute;
  right: 6px;
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border: 0;
  background: transparent;
  color: var(--ob-text-muted);
  font-size: 12px;
  border-radius: 4px;
  cursor: pointer;
}
.session-search-clear:hover {
  color: var(--ob-text);
  background: var(--ob-hover);
}

/* === 会话卡片 === */
.session-card {
  position: relative;
  border: 1px solid transparent;
  transition: background-color 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;
}
.session-card.is-inactive:hover {
  background: var(--ob-hover);
}
.session-card.is-active {
  background: var(--ob-surface);
  border-color: var(--ob-hover);
  box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / .04), 0 1px 2px rgb(var(--ob-shadow-rgb) / .02);
}
.session-card.is-active .session-card-title {
  color: var(--ob-text-strong);
  font-weight: 600;
}
.session-card.is-inactive .session-card-title {
  color: var(--ob-text);
}
.session-active-bar {
  position: absolute;
  left: 0;
  top: 5px;
  bottom: 5px;
  width: 3px;
  border-radius: 999px;
  background: var(--ob-text-strong);
}
.session-pinned-star {
  color: var(--ob-warning);
}
.session-card.is-running {
  background: var(--ob-success-soft);
  border-color: var(--ob-success);
}
.session-running-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 16px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--ob-success-soft);
  color: var(--ob-success);
  font-size: 9.5px;
  font-weight: 600;
  line-height: 1;
}
.running-ping-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--ob-success);
}
.session-metrics-pill {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 10.5px;
  color: var(--ob-text-subtle);
  font-variant-numeric: tabular-nums;
}
.session-tool-btn {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border-radius: 7px;
  border: 0;
  background: transparent;
  color: var(--ob-text-subtle);
  cursor: pointer;
  transition: all 0.14s ease;
}
.session-tool-btn:hover {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.session-tool-btn.is-active {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.session-hover-btn {
  display: grid;
  width: 20px;
  height: 20px;
  place-items: center;
  border: 0;
  border-radius: 5px;
  background: var(--ob-surface);
  color: var(--ob-text-subtle);
  box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / .08);
  cursor: pointer;
  transition: all 0.14s ease;
}
.session-hover-btn:hover {
  background: var(--ob-surface);
  color: var(--ob-text-strong);
  transform: scale(1.08);
}
</style>


<style>
.theme-mode-menu {
  min-width: 190px;
  padding: 5px;
}
.theme-mode-menu .el-dropdown-menu__item {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) 16px;
  gap: 9px;
  min-height: 42px;
  border-radius: 8px;
  padding: 6px 9px;
}
.theme-mode-menu .el-dropdown-menu__item.is-theme-selected {
  background: var(--ob-surface-soft);
  color: var(--ob-text-strong);
}
.theme-menu-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
  line-height: 1.15;
}
.theme-menu-copy strong {
  font-size: 13px;
  font-weight: 650;
}
.theme-menu-copy small {
  color: var(--ob-text-muted);
  font-size: 10px;
  font-weight: 400;
}
.theme-menu-check,
.theme-menu-check-placeholder {
  width: 14px;
  justify-self: end;
}
.theme-menu-check { color: var(--ob-blue); }
.version-dialog.el-dialog {
  --el-dialog-padding-primary: 0;
  padding: 0 !important;
  overflow: hidden;
  border-radius: 20px;
  background: var(--ob-surface-raised);
  box-shadow: var(--ob-shadow-dialog);
}
.version-dialog .el-dialog__header {
  margin: 0;
  padding: 18px 22px 12px;
  border-bottom: 1px solid var(--ob-border);
}
.version-dialog .el-dialog__body {
  padding: 0;
}
.version-dialog .el-dialog__footer {
  padding: 12px 22px 16px;
  border-top: 1px solid var(--ob-border);
}
.version-dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-right: 28px;
}
.version-dialog-kicker {
  color: var(--ob-text-subtle);
  font-size: 11px;
  font-weight: 650;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.version-dialog-title {
  margin: 2px 0 0;
  color: var(--ob-text-strong);
  font-size: 17px;
  font-weight: 680;
  letter-spacing: -.02em;
}
.version-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: var(--ob-surface);
  padding: 0 9px;
  color: var(--ob-text-subtle);
  font-size: 11px;
  font-weight: 650;
}
.version-chip.is-update {
  border-color: var(--ob-warning);
  background: var(--ob-warning-soft);
  color: var(--ob-warning);
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
  border: 1px solid var(--ob-border);
  border-radius: 14px;
  background: var(--ob-surface);
  padding: 10px 12px;
}
.version-hero-item span {
  display: block;
  color: var(--ob-text-subtle);
  font-size: 11px;
}
.version-hero-item strong {
  display: block;
  margin-top: 3px;
  color: var(--ob-text-strong);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -.03em;
}
.version-hero-arrow {
  color: var(--ob-text-muted);
  font-size: 16px;
}
.version-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-bottom: 12px;
  color: var(--ob-text-subtle);
  font-size: 12px;
}
.version-meta a {
  color: var(--ob-blue);
  text-decoration: none;
}
.version-meta a:hover {
  text-decoration: underline;
}
.version-callout {
  margin-bottom: 12px;
  border-radius: 12px;
  background: var(--ob-surface-soft);
  padding: 10px 12px;
  color: var(--ob-text);
  font-size: 13px;
  line-height: 1.5;
}
.version-callout.is-warn {
  background: var(--ob-warning-soft);
  color: var(--ob-warning);
}
.version-notes {
  overflow: hidden;
  border: 1px solid var(--ob-border);
  border-radius: 14px;
  background: var(--ob-surface);
}
.version-notes-head {
  padding: 10px 14px 8px;
  border-bottom: 1px solid var(--ob-border);
  color: var(--ob-text-subtle);
  font-size: 12px;
  font-weight: 650;
}
.version-notes-md {
  max-height: min(48vh, 420px);
  overflow: auto;
  padding: 12px 16px 16px;
  color: var(--ob-text);
  font-size: 14px;
  line-height: 1.65;
}
.version-notes-empty {
  padding: 28px 16px;
  color: var(--ob-text-muted);
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
  border: 1px solid var(--ob-border-strong);
  border-radius: 10px;
  background: var(--ob-surface);
  padding: 0 14px;
  color: var(--ob-text);
  font-size: 13px;
}
.version-btn:hover:not(:disabled) {
  background: var(--ob-surface-soft);
}
.version-btn.is-primary {
  border-color: var(--ob-border-strong);
  background: var(--ob-text-strong);
  color: var(--ob-text-inverse);
}
.version-btn.is-primary:hover:not(:disabled) {
  background: var(--ob-text-strong);
}
.version-btn:disabled {
  opacity: .55;
  cursor: default;
}

</style>
