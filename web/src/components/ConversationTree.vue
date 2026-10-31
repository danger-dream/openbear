<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
  Aim, Box, ArrowDown, ArrowRight, ChatLineRound, Check, Clock, Connection, Delete, DocumentCopy,
  EditPen, Finished, Folder, FolderAdd, FolderOpened, InfoFilled, Loading, MagicStick, MoreFilled,
  Plus, Refresh, RefreshLeft, Search, Star, StarFilled, Timer, SetUp, Tools, UserFilled, User,
} from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import { userStorage } from '../sessionBoundary.js';
import { MOBILE_VIEWPORT_QUERY } from "../mobileViewport.js";
import ModelSettings from "./ModelSettings.vue";
import PromptPolicyEditor from "./PromptPolicyEditor.vue";
import PromptImpactDialog from "./PromptImpactDialog.vue";
import {normalizePromptPolicy, promptPolicyFlags} from "./promptPolicy.js";
import "./conversationTreeProperties.css";
import ConversationPromptDialog from "./ConversationPromptDialog.vue";
import WebhookEditor from "./webhooks/WebhookEditor.vue";
import ConversationOverview from "./ConversationOverview.vue";
import GroupPropertiesDialog from "./GroupPropertiesDialog.vue";
import {groupTreeRows, leadGroupStatus} from "./conversationGroupsTree.js";
import {activityReadRequests, activityLabel, activityState, folderActivityCounts, isActivityWaiting, pendingConversationRows, pendingFolderCounts} from "../conversationActivity.js";
import {useRecentConversationRows} from "./conversationRecentRows.js";
import { treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop } from "./conversationTreeInteractions.js";
import { referenceCatalog, referenceItem, acceptActivityReadReceipt } from "../references/catalog.js";
import { REFERENCE_MIME, referenceToken } from "../references/codec.js";
import AnimatedConversationTitle from "./AnimatedConversationTitle.vue";
import ConversationSelectionBar from "./ConversationSelectionBar.vue";
import { automaticRow, batchMessage, rangeSelection, selectableRow } from "./conversationTreeSelection.js";
import { modelDefaultThinking, modelThinkingLevels } from "../views/consoleView/display.js";
import {
  RUN_DEFAULT_FIELDS, hasRunDefault, normalizedRunDefaults, resolvedRunDefaults,
  runDefaultOption, sparseRunDefaults, updateRunDefault,
} from "./folderRunDefaults.js";


const props = defineProps({
  activeConversationUuid: { type: String, default: "" },
  draftConversation: { type: Object, default: null },
  // Off while a page that must keep its own route (e.g. instances) is shown.
  autoOpenSelected: { type: Boolean, default: true },
});
const emit = defineEmits(["open", "new-conversation", "rows", "selected-folder", "refresh-list", "delete-conversation", "conversations-deleted", "folder-removed", "conversation-properties", "activity-status", "show-pending"]);

defineExpose({
  refresh: (options = {}) => refreshTree({ preserve: true, ...options }),
  revealConversation: (conversationUuid) => refreshTree({ preserve: true, conversationUuid, reveal: true }),
  selectedFolderId: () => selectedFolderId.value,
  openRootMenu,
  showProperties,
  revealDraft,
  revealFolder,
  forgetConversation,
  conversationPath: (conversationUuid) => {
    const row = knownConversationRows().find((item) => item.conversationUuid === conversationUuid);
    return row ? String(row.path || (row.folderId ? folderPath(row.folderId) : "临时会话")) : "";
  },
});

const activityItems = ref([]);
const recentItems = ref([]);
const hiddenConversationIds = ref([]);
const collabGroups = ref({});
const activityReadBusy = ref(false);
function savedSidebarView() {
  try { return userStorage(window.localStorage)?.getItem('openbear:conversation-sidebar-view') === 'folders' ? 'folders' : 'recent'; }
  catch { return 'recent'; }
}
const sidebarView = ref(savedSidebarView());
const sidebarScroll = {recent: 0, folders: 0, search: 0};
const locatingConversation = ref(false);
const recentClock = ref(Date.now());
let recentClockTimer;
const {rows: recentRowsRaw} = useRecentConversationRows({
  get items() { return activityItems.value; },
  get recentItems() { return recentItems.value; },
  get hiddenConversationIds() { return hiddenConversationIds.value; },
  get activeConversationUuid() { return props.activeConversationUuid; },
  get readVersions() { return referenceCatalog.activityReadVersions || new Map(); },
});
const recentConversationRows = computed(() => recentRowsRaw.value.map(row => ({...row, ...leadGroupStatus(collabGroups.value[row.conversationUuid])})));
// Match the unread marker's visibility without consuming in-progress results.
const activitySummaryRows = computed(() => [...new Map([...recentConversationRows.value, ...activityItems.value].map(row => [row.conversationUuid, row])).values()]);
const recentUnreadRows = computed(() => activitySummaryRows.value.filter(row =>
  row.activityUnread && !running(row) && !isTitleGenerating(row)
));
const recentUnreadCount = computed(() => recentUnreadRows.value.length);
const pendingRows = ref([]);
const recentWaitingCount = computed(() => pendingRows.value.length);
const recentRunningCount = computed(() => activitySummaryRows.value.filter(row => !isTitleGenerating(row) && !row.activityPending?.length && activityState(row) === 'running').length);
function recentLabel(row) {
  const state = row.activityPending?.length ? 'waiting' : activityState({...row, readWhileSelected: false});
  const label = activityLabel({...row, activityState: state, readWhileSelected: false});
  if (state === 'waiting' || state === 'running') return label;
  const settled = ['completed', 'idle', 'read'].includes(state);
  if (row.activityUnread) return settled ? '完成待查看' : `${label}待查看`;
  if (!settled) return label;
  const at = Number(row.lastInteractionAtMs || row.activityAtMs || 0);
  if (!at) return activityLabel(row);
  const elapsed = Math.max(0, recentClock.value - at);
  return elapsed < 60000 ? '刚刚' : elapsed < 3600000 ? `${Math.floor(elapsed / 60000)} 分钟前`
    : elapsed < 86400000 ? `${Math.floor(elapsed / 3600000)} 小时前` : `${Math.floor(elapsed / 86400000)} 天前`;
}
const rootFolders = ref([]);
const branchState = reactive({});
const expanded = ref(new Set());
const selectedFolderId = ref("");
const loading = ref(false);
const activeCount = ref(0);
const archivedCount = ref(0);
const temporaryCount = ref(0);
const initialized = ref(false);
const listRef = ref(null);
const query = ref("");
const searchRows = ref([]);
const searchLoading = ref(false);
const searchHasMore = ref(false);
const searchCursor = ref("");
let searchResultKey = "";
const searchArchived = ref(false);
const archiveUnlocked = ref(false);
const menu = ref({ open: false, x: 0, y: 0, row: null, recent: false });
const drag = ref({ row: null, target: null, zone: "", busy: false });
const moveInFlight = ref(false);
const movingRowId = ref("");
const titleGenerating = ref(new Set());
const rootDropTarget = { kind: "root", id: "__root" };
const promptDialog = ref(false);
const promptRow = ref(null);
const propertiesDialog = ref(false);
const propertiesLoading = ref(false);
const propertiesSaving = ref(false);
const propertiesTab = ref("context");
const webhookEditor = ref(null);
const webhookVisited = ref(false);
const propertyModelOptions = ref([]);
const propertiesRunDefaultsBaseline = ref({});
const propertiesForm = reactive({
  folderId: "", parentId: "", name: "", path: "", temporary: false, workspaceDir: "", promptMarkdown: "", hideFromRecent: false, hiddenFromRecentByAncestor: false,
  promptPolicy: promptPolicyFlags(), inheritedPromptPolicy: normalizePromptPolicy(), inheritedPromptSourcePath: "",
  runDefaults: {}, runInherited: {}, runFallback: {}, runResolved: {},
});
let propertiesRequestGeneration = 0;
let propertiesSaveGeneration = 0;
const moveDialog = ref(false);
const moveBusy = ref(false);
const moveMode = ref("move");
const moveRow = ref(null);
const moveFolderId = ref("");
const moveFolderQuery = ref("");
const moveUnarchive = ref(true);
const moveUpdateSnapshots = ref(true);
const allFolders = ref([]);
const waitingFolderCounts = computed(() => pendingFolderCounts(pendingRows.value, [
  ...allFolders.value, ...rootFolders.value,
  ...Object.values(branchState).flatMap(branch => (branch.items || []).filter(row => row.kind === 'folder')),
]));
function folderWaitingCount(row) { return waitingFolderCounts.value.get(row.kind === 'system' ? row.id : row.folderId) || 0; }
// Expanded directories leave these to their own rows; "待处理 N" stays as is.
const folderActivity = ref({});
function collapsedFolderActivity(row) {
  const id = row.kind === 'system' ? row.id : row.folderId;
  if (!id || isExpanded(id)) return null;
  const counts = folderActivity.value[id];
  return counts?.running || counts?.unread ? counts : null;
}
watch(() => pendingRows.value.map(row => `${row.conversationUuid}:${row.folderId}:${row.path}`).join('|'), async (key, _, onCleanup) => {
  if (!key) return;
  let cancelled = false;
  onCleanup(() => { cancelled = true; });
  try {
    const data = await Api.conversationTreeFolders();
    if (!cancelled) allFolders.value = data.items || [];
  } catch { /* Loaded branches still provide their known ancestor counts. */ }
});
const groupProperties = ref({ open: false, groupId: "" });
const impactDialog = ref(false);
const impactState = reactive({ action: "移动", impact: null, resolve: null });
let bootstrapGeneration = 0;
let statusRequestGeneration = 0;
let statusServerRevision = 0;
let latestStatusState = null;
let searchGeneration = 0;
let searchTimer = null;
let statusTimer = null;
let dragExpandTimer = null;
let dragExpandTarget = "";
const overview = ref({ open: false, row: null, anchor: null });
let overviewOpenTimer = null, overviewCloseTimer = null;
let menuAnchor = null;
function closeOverview() {
  clearTimeout(overviewOpenTimer); clearTimeout(overviewCloseTimer);
  overview.value = { open: false, row: null, anchor: null };
}
function keepOverview() { clearTimeout(overviewOpenTimer); clearTimeout(overviewCloseTimer); }
function enterOverview(event, row) {
  if (row.kind !== 'conversation' || row.local || event.pointerType === 'touch' || !window.matchMedia('(hover: hover)').matches || drag.value.row || moveInFlight.value || menu.value.open) return;
  keepOverview();
  // Activity rows include status/read actions outside the title button. Anchor
  // their overview to the full row, not inside that trailing status area.
  const anchor = event.currentTarget.querySelector('button.conversation') || event.currentTarget;
  if (overview.value.open && overview.value.row?.conversationUuid === row.conversationUuid && overview.value.anchor === anchor) return;
  closeOverview();
  overviewOpenTimer = setTimeout(() => {
    if (anchor.isConnected && !drag.value.row && !menu.value.open) overview.value = { open: true, row, anchor };
  }, 280);
}
function leaveOverview() {
  clearTimeout(overviewOpenTimer); clearTimeout(overviewCloseTimer);
  overviewCloseTimer = setTimeout(closeOverview, 160);
}

const branchKey = (parentId = "", systemNode = "") => systemNode || `folder:${parentId}`;
function stateFor(parentId = "", systemNode = "") {
  const key = branchKey(parentId, systemNode);
  if (!branchState[key]) branchState[key] = {
    parentId: String(parentId || ""), systemNode: String(systemNode || ""),
    items: [], nextCursor: "", hasMore: false, loading: false, indicating: false, error: "",
    generation: 0, loaded: false, pages: 0, locatedFolderIds: [],
  };
  return branchState[key];
}
function isExpanded(id) { return expanded.value.has(id); }
function setExpanded(id, value) {
  const next = new Set(expanded.value);
  if (value) next.add(id); else next.delete(id);
  expanded.value = next;
}
function running(row) { return Boolean(row?.running || row?.status === "running"); }
function hasFolderChildren(row) { return Number(row?.childFolderCount || 0) + Number(row?.conversationCount || 0) > 0; }
function liveConversationTitle(row) {
  if (!row || row.local) return row?.title || "新会话";
  const title = referenceItem({kind: "chat", id: row.conversationUuid})?.label || row.title || "新会话";
  // Under its group node a member needs only its own name; elsewhere keep the full title.
  return row.groupMember && row.groupName && title.endsWith(` · ${row.groupName}`) ? title.slice(0, -(row.groupName.length + 3)) : title;
}
function rowLabel(row) { return row?.kind === "folder" ? row.name : liveConversationTitle(row); }
function hasConversationMessages(row) { return Number(row?.lastConversationAt || row?.lastInteractionAtMs || 0) > 0; }
function isTitleGenerating(row) { return titleGenerating.value.has(String(row?.conversationUuid || "")); }
// Same icons as the Cron / Webhook entries in the main navigation.
function conversationOrigin(row) {
  switch (row?.origin || row?.originKind || row?.origin_kind) {
    case 'deployment': return {icon: SetUp, label: '实例部署会话'};
    case 'maintenance': return {icon: Tools, label: '实例维护会话'};
    case 'cron': return {icon: Clock, label: '定时任务创建'};
    case 'webhook': return {icon: Connection, label: '触发器创建'};
    case 'collab': return {icon: User, label: '小组成员'};
    default: return {icon: ChatLineRound, label: undefined};
  }
}
function setTitleGenerating(conversationUuid, generating) {
  const uuid = String(conversationUuid || "");
  if (!uuid) return;
  const next = new Set(titleGenerating.value);
  if (generating) next.add(uuid); else next.delete(uuid);
  titleGenerating.value = next;
}
function nodePath(row) { return String(row?.path || (row?.folderId ? folderPath(row.folderId) || '所属目录' : '临时会话')); }
function indentation(depth) { return `${Math.min(7, Math.max(0, Number(depth || 0))) * 14}px`; }
function rowLoading(row) {
  if (!["folder", "conversation", "system"].includes(row.kind)) return false;
  if (movingRowId.value && movingRowId.value === rowId(row)) return true;
  // A row indicates its OWN user-visible request, never a parent's request or
  // a background tree calibration. Global refresh has its toolbar indicator.
  const branch = row.kind === "system" ? branchState[branchKey("", row.systemNode)]
    : row.kind === "folder" ? branchState[branchKey(row.folderId)] : null;
  return Boolean(branch?.loading && branch.indicating);
}

function withDraft(items, folderId) {
  const rows = Array.isArray(items) ? items.slice() : [];
  const draft = props.draftConversation;
  if (draft && String(draft.folderId || "") === String(folderId || "") && !rows.some((item) => item.conversationUuid === draft.conversationUuid)) {
    rows.push({ ...draft, kind: "conversation", id: draft.conversationUuid, parentId: folderId, path: folderId ? folderPath(folderId) : "临时会话" });
    // A local draft stays below child folders and pinned conversations, at the
    // head of the unpinned group where its first send will persist it.
    rows.sort(compareTreeRows);
  }
  return rows;
}
function appendFolderRows(output, folders, depth) {
  for (const folder of folders || []) {
    output.push({ ...folder, depth });
    if (!isExpanded(folder.folderId)) continue;
    const branch = stateFor(folder.folderId);
    for (const child of withDraft(branch.items, folder.folderId)) {
      if (child.kind === "folder") appendFolderRows(output, [child], depth + 1);
      else appendConversationRow(output, child, depth + 1);
    }
    if (branch.error) output.push({ kind: "error", id: `error:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId, message: branch.error });
    else if ((branch.loaded || branch.pages > 0) && !withDraft(branch.items, folder.folderId).length) output.push({ kind: "empty", id: `empty:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId });
    if (branch.hasMore) output.push({ kind: "more", id: `more:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId, cursor: branch.nextCursor });
  }
}
function appendConversationRow(output, row, depth) {
  output.push({ ...row, depth, ...leadGroupStatus(collabGroups.value[row.conversationUuid]) });
  output.push(...groupTreeRows(collabGroups.value[row.conversationUuid], {
    depth, expanded: expanded.value, lookup: latestStatusState?.lookup, activityLookup: latestStatusState?.activityLookup,
  }));
}
const visibleRows = computed(() => {
  const output = [];
  appendFolderRows(output, rootFolders.value, 0);
  const temporary = { kind: "system", systemNode: "temporary", id: "__temporary", name: "临时会话", count: temporaryCount.value, depth: 0 };
  output.push(temporary);
  if (isExpanded("__temporary")) {
    const branch = stateFor("", "temporary");
    for (const row of withDraft(branch.items, "")) appendConversationRow(output, { ...row, parentId: "" }, 1);
    if (branch.error) output.push({ kind: "error", id: "error:temporary", depth: 1, systemNode: "temporary", message: branch.error });
    else if ((branch.loaded || branch.pages > 0) && !withDraft(branch.items, "").length) output.push({ kind: "empty", id: "empty:temporary", depth: 1, systemNode: "temporary" });
    if (branch.hasMore) output.push({ kind: "more", id: "more:temporary", depth: 1, systemNode: "temporary", cursor: branch.nextCursor });
  }
  const archive = { kind: "system", systemNode: "archive", id: "__archive", name: "已归档", count: archivedCount.value, depth: 0 };
  output.push(archive);
  if (isExpanded("__archive")) {
    const branch = stateFor("", "archive");
    for (const row of branch.items || []) output.push({ ...row, depth: 1, parentId: "__archive" });
    if (branch.error) output.push({ kind: "error", id: "error:archive", depth: 1, systemNode: "archive", message: branch.error });
    else if ((branch.loaded || branch.pages > 0) && !branch.items.length) output.push({ kind: "empty", id: "empty:archive", depth: 1, systemNode: "archive" });
    if (branch.hasMore) output.push({ kind: "more", id: "more:archive", depth: 1, systemNode: "archive", cursor: branch.nextCursor });
  }
  return output;
});
const isDirectoryView = computed(() => sidebarView.value === 'folders' && !query.value.trim());
const sidebarScrollKey = computed(() => query.value.trim() ? 'search' : sidebarView.value);
const displayRows = computed(() => {
  if (query.value.trim()) return searchRows.value.map(row => ({...row, depth: 0, search: true}));
  if (sidebarView.value === 'folders') return visibleRows.value;
  return recentConversationRows.value.map(row => ({...row, kind: 'conversation', recentAlias: true, depth: 0}));
});
const activeConversationRow = computed(() => displayRows.value.find(row => row.kind === 'conversation' && row.conversationUuid === props.activeConversationUuid) || null);
async function switchSidebarView(view) {
  if (!['recent', 'folders'].includes(view)) return;
  closeOverview(); closeMenu(); clearDrag();
  sidebarView.value = view;
  try { userStorage(window.localStorage)?.setItem('openbear:conversation-sidebar-view', view); } catch { /* storage may be unavailable */ }
  await nextTick();
}
async function sidebarTabKeydown(event) {
  const views = ['recent', 'folders'];
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const view = event.key === 'Home' ? 'recent' : event.key === 'End' ? 'folders' : views[1 - views.indexOf(sidebarView.value)];
  const tabList = event.currentTarget;
  await switchSidebarView(view);
  tabList.querySelector(`[data-sidebar-view="${view}"]`)?.focus();
}
async function showRecentActivity() {
  query.value = '';
  await switchSidebarView('recent');
}
async function revealInFolders(row = null) {
  const uuid = String(row?.conversationUuid || props.activeConversationUuid || '');
  if (!uuid || locatingConversation.value) return;
  locatingConversation.value = true;
  try {
    query.value = '';
    await switchSidebarView('folders');
    if (props.draftConversation?.conversationUuid === uuid) await revealDraft(props.draftConversation.folderId || '');
    else await refreshTree({preserve: true, conversationUuid: uuid, reveal: true, refreshLoaded: false});
  } finally { locatingConversation.value = false; }
}
watch(sidebarScrollKey, (key, previous) => {
  sidebarScroll[previous] = listRef.value?.scrollTop || 0;
  closeOverview(); closeMenu(); clearDrag();
  nextTick(() => { if (sidebarScrollKey.value === key && listRef.value) listRef.value.scrollTop = sidebarScroll[key] || 0; });
});
const selectedTargetLabel = computed(() => selectedFolderId.value ? folderPath(selectedFolderId.value) || "所选目录" : "临时会话");
const filteredMoveFolders = computed(() => {
  const q = moveFolderQuery.value.trim().toLowerCase();
  return allFolders.value.filter((folder) => !q || String(folder.path || "").toLowerCase().includes(q));
});

function everyKnownNode() {
  const nodes = [...rootFolders.value];
  for (const branch of Object.values(branchState)) nodes.push(...(branch.items || []));
  return nodes;
}
function knownConversationRows() {
  const map = new Map();
  for (const row of everyKnownNode()) if (row.kind === "conversation" && row.conversationUuid) map.set(row.conversationUuid, row);
  for (const row of [...activityItems.value, ...recentItems.value]) map.set(row.conversationUuid, row);
  if (props.draftConversation) map.set(props.draftConversation.conversationUuid, props.draftConversation);
  return [...map.values()];
}
function emitRows() { emit("rows", knownConversationRows()); }
function forgetConversation(conversationUuid) {
  bootstrapGeneration += 1;
  loading.value = false;
  searchGeneration += 1;
  searchLoading.value = false;
  for (const branch of Object.values(branchState)) {
    branch.generation += 1;
    branch.loading = false;
    branch.items = (branch.items || []).filter((row) => row.conversationUuid !== conversationUuid);
  }
  searchRows.value = searchRows.value.filter((row) => row.conversationUuid !== conversationUuid);
  activityItems.value = activityItems.value.filter(row => row.conversationUuid !== conversationUuid);
  recentItems.value = recentItems.value.filter(row => row.conversationUuid !== conversationUuid);
  emitRows();
}
function forgetFolder(folderId) {
  bootstrapGeneration += 1;
  loading.value = false;
  invalidateBranch(folderId);
  delete branchState[branchKey(folderId)];
  rootFolders.value = rootFolders.value.filter((row) => row.folderId !== folderId);
  for (const branch of Object.values(branchState)) {
    branch.generation += 1;
    branch.loading = false;
    branch.items = (branch.items || []).filter((row) => row.kind !== "folder" || row.folderId !== folderId);
    branch.locatedFolderIds = (branch.locatedFolderIds || []).filter((id) => id !== folderId);
  }
  setExpanded(folderId, false);
}
const compareTreeRows = compareTreeItems;
function mergeTreeRows(existing = [], incoming = []) {
  const merged = new Map((existing || []).map((row) => [rowId(row), row]));
  for (const row of incoming || []) if (rowId(row)) merged.set(rowId(row), row);
  return [...merged.values()].sort(compareTreeRows);
}
function sameRowShape(a, b) {
  // Status rows are JSON data. Arrays/objects arrive with fresh identities on
  // each packet, so compare their contents before replacing a published row.
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(b);
  if (keys.length !== Object.keys(a).length) return false;
  for (const key of keys) {
    if (!Object.hasOwn(a, key) || !sameRowShape(a[key], b[key])) return false;
  }
  return true;
}
function statusAdjustedRow(row) {
  if (!latestStatusState || !row) return row;
  if (row.kind === "folder") {
    const count = latestStatusState.folderConversationCounts?.[row.folderId];
    const next = { ...row, runningDescendantCount: Number(latestStatusState.folderRunningCounts?.[row.folderId] || 0),
      ...(Number.isFinite(count) ? { conversationCount: count } : {}) };
    return sameRowShape(row, next) ? row : next;
  }
  if (row.kind !== "conversation" || row.archived) return row;
  const status = latestStatusState.lookup.get(row.conversationUuid);
  const activity = latestStatusState.activityLookup.get(row.conversationUuid);
  const next = { ...row, ...(status || {}), ...(activity || {activityUnread: false, activityState: "", activityPending: []}), running: Boolean(status), status: status ? "running" : "idle",
    groupCount: 0, groupRunning: 0, groupMembers: 0, ...leadGroupStatus(latestStatusState.raw?.collabGroups?.[row.conversationUuid]) };
  return sameRowShape(row, next) ? row : next;
}
function mergeLocatedFolders(rows = []) {
  for (const raw of rows || []) {
    if (raw?.kind !== "folder" || !raw.folderId) continue;
    const row = statusAdjustedRow(raw);
    const parentId = String(row.parentId || "");
    if (!parentId) rootFolders.value = mergeTreeRows(rootFolders.value, [row]);
    else {
      const branch = stateFor(parentId);
      if (!branch.locatedFolderIds.includes(row.folderId)) branch.locatedFolderIds = [...branch.locatedFolderIds, row.folderId].slice(-100);
      branch.items = mergeTreeRows(branch.items, [row]);
    }
  }
}
function cacheLocatedConversation(raw) {
  // Active group members are listed only under their group node, never as folder siblings.
  if (!raw?.conversationUuid || (raw.origin === "collab" && !raw.archived)) return;
  const row = statusAdjustedRow(raw);
  const parentId = String(row.folderId || "");
  const system = row.archived ? "archive" : parentId ? "" : "temporary";
  const branch = stateFor(parentId, system);
  branch.items = mergeTreeRows(branch.items, [row]);
}
function mapKnownRows(transform) {
  rootFolders.value = rootFolders.value.map(transform).sort(compareTreeRows);
  for (const branch of Object.values(branchState)) branch.items = (branch.items || []).map(transform).sort(compareTreeRows);
  searchRows.value = searchRows.value.map(transform);
  activityItems.value = activityItems.value.map(transform);
  recentItems.value = recentItems.value.map(transform);
}
function rebaseKnownPaths(oldPath, newPath) {
  const before = String(oldPath || ""), after = String(newPath || "");
  if (!before || before === after) return;
  mapKnownRows(item => {
    const path = String(item?.path || "");
    if (path !== before && !path.startsWith(`${before} / `)) return item;
    return { ...item, path: `${after}${path.slice(before.length)}` };
  });
}
function adoptStatusConversation(raw) {
  const uuid = String(raw?.conversationUuid || "");
  if (!latestStatusState || !uuid) return;
  const organization = {};
  for (const key of ["title", "folderId", "parentId", "path", "pinned", "pinnedAt", "displayOrder", "archived", "archivedAt"]) {
    if (Object.hasOwn(raw, key)) organization[key] = raw[key];
  }
  for (const map of [latestStatusState.lookup, latestStatusState.activityLookup]) {
    if (map.has(uuid)) map.set(uuid, { ...map.get(uuid), ...organization });
  }
  for (const key of ["items", "activityItems", "recentItems"]) {
    if (!Array.isArray(latestStatusState.raw?.[key])) continue;
    latestStatusState.raw[key] = latestStatusState.raw[key].map(item =>
      item.conversationUuid === uuid ? { ...item, ...organization } : item);
  }
}
function applyMutationRow(original, raw) {
  if (!original || !raw) return;
  const id = rowId(original);
  const oldPath = String(original.path || "");
  const patch = original.kind === "folder"
    ? { ...raw, kind: "folder", id: original.folderId, folderId: original.folderId }
    : { ...raw, kind: "conversation", id: original.conversationUuid,
        conversationUuid: original.conversationUuid, parentId: String(raw.folderId ?? original.folderId ?? ""),
        path: String(original.path || (raw.folderId ? folderPath(raw.folderId) : "临时会话")) };
  if (original.kind === "conversation") adoptStatusConversation(patch);
  mapKnownRows(item => rowId(item) === id ? statusAdjustedRow({ ...item, ...patch }) : item);
  if (original.kind === "folder") rebaseKnownPaths(oldPath, String(raw.path || oldPath));
  emitRows();
}
function removeKnownTreeItem(row) {
  const id = rowId(row);
  rootFolders.value = rootFolders.value.filter(item => rowId(item) !== id);
  for (const branch of Object.values(branchState)) branch.items = (branch.items || []).filter(item => rowId(item) !== id);
}
function applyTargetOrder(kind, ids = []) {
  const order = new Map(ids.map((id, index) => [String(id), (index + 1) * 1024]));
  if (!order.size) return;
  mapKnownRows(item => {
    if (item.kind !== kind) return item;
    const id = kind === "folder" ? item.folderId : item.conversationUuid;
    return order.has(String(id)) ? { ...item, displayOrder: order.get(String(id)) } : item;
  });
}
function adoptOrganizationCounts(rows = []) {
  if (!latestStatusState) return;
  for (const row of rows) {
    if (row?.kind !== "folder" || !row.folderId || !Number.isFinite(Number(row.conversationCount))) continue;
    latestStatusState.folderConversationCounts[row.folderId] = Number(row.conversationCount);
    if (latestStatusState.raw?.folderConversationCounts) {
      latestStatusState.raw.folderConversationCounts[row.folderId] = Number(row.conversationCount);
    }
  }
}
function knownFolderIdPath(folderId, extraRows = []) {
  const byId = new Map();
  for (const row of [...everyKnownNode(), ...extraRows]) {
    if (row?.kind === "folder" && row.folderId) byId.set(String(row.folderId), row);
  }
  const path = [], seen = new Set();
  let current = String(folderId || "");
  while (current && byId.has(current) && !seen.has(current)) {
    seen.add(current); path.unshift(current); current = String(byId.get(current)?.parentId || "");
  }
  return path;
}
function rebaseStatusFolderPaths(original, newFolderIds) {
  if (!latestStatusState) return;
  const head = original.kind === "folder" && !newFolderIds.includes(original.folderId) ? [...newFolderIds, original.folderId] : newFolderIds;
  const rebase = item => {
    if (original.kind !== "folder") return item.conversationUuid === original.conversationUuid ? { ...item, folderPath: head } : item;
    const index = (item.folderPath || []).indexOf(original.folderId);
    return index < 0 ? item : { ...item, folderPath: [...head, ...item.folderPath.slice(index + 1)] };
  };
  for (const key of ["items", "activityItems", "recentItems"]) {
    if (Array.isArray(latestStatusState.raw?.[key])) latestStatusState.raw[key] = latestStatusState.raw[key].map(rebase);
  }
  for (const map of [latestStatusState.lookup, latestStatusState.activityLookup]) {
    for (const [uuid, item] of map) map.set(uuid, rebase(item));
  }
  activityItems.value = activityItems.value.map(rebase);
  folderActivity.value = folderActivityCounts([...(latestStatusState.raw?.items || []).map(withLocalRead), ...activityItems.value]);
}
function adjustRunningFolderCounts(ids, delta) {
  if (!latestStatusState || !delta) return;
  for (const id of ids) {
    const next = Math.max(0, Number(latestStatusState.folderRunningCounts[id] || 0) + delta);
    latestStatusState.folderRunningCounts[id] = next;
    if (latestStatusState.raw?.folderRunningCounts) latestStatusState.raw.folderRunningCounts[id] = next;
  }
}
function applyMoveOrganization(original, targetFolderId, organization = {}) {
  const oldPath = String(original.path || "");
  const folderRows = organization.folderItems || [];
  const oldFolderIds = knownFolderIdPath(original.folderId);
  const newFolderIds = Array.isArray(organization.folderPath) ? organization.folderPath.map(String) : [];
  const runningDelta = original.kind === "folder"
    ? Number(original.runningDescendantCount || 0) : (running(original) ? 1 : 0);
  const oldAncestors = original.kind === "folder" ? oldFolderIds.filter(id => id !== original.folderId) : oldFolderIds;
  const newAncestors = original.kind === "folder" ? newFolderIds.filter(id => id !== original.folderId) : newFolderIds;
  adjustRunningFolderCounts(oldAncestors, -runningDelta);
  adjustRunningFolderCounts(newAncestors, runningDelta);
  removeKnownTreeItem(original);
  adoptOrganizationCounts(folderRows);
  mergeLocatedFolders(folderRows);
  const moved = organization.item;
  if (moved?.kind === "folder") mergeLocatedFolders([moved]);
  else if (moved?.conversationUuid) {
    adoptStatusConversation(moved);
    cacheLocatedConversation(moved);
  }
  if (original.kind === "folder" && moved) rebaseKnownPaths(oldPath, String(moved.path || oldPath));
  if (Array.isArray(organization.folderPath)) rebaseStatusFolderPaths(original, newFolderIds);
  applyTargetOrder(original.kind, organization.targetOrder || []);
  if (original.kind === "conversation") {
    const oldParent = String(original.folderId || ""), nextParent = String(targetFolderId || "");
    if (!original.archived && oldParent !== nextParent) {
      if (!oldParent) temporaryCount.value = Math.max(0, temporaryCount.value - 1);
      if (!nextParent) temporaryCount.value += 1;
    }
    if (original.archived && moved && !moved.archived) {
      archivedCount.value = Math.max(0, archivedCount.value - 1);
      activeCount.value += 1;
    }
  }
  emitRows();
}
// Rebuild a branch only when at least one row actually changed, so a status
// refresh with no real change reuses every existing reference.
function remapRows(rows = []) {
  if (!rows.length) return rows;
  let changed = false;
  const next = rows.map((row) => {
    if (row.kind !== "conversation" && row.kind !== "folder") return row;
    if (row.kind === "conversation" && row.archived) return row;
    const adjusted = statusAdjustedRow(row);
    if (adjusted !== row) changed = true;
    return adjusted;
  });
  return changed ? next : rows;
}
function folderPath(folderId) {
  const match = everyKnownNode().find((row) => row.kind === "folder" && row.folderId === folderId)
    || allFolders.value.find((row) => row.folderId === folderId);
  return String(match?.path || match?.name || "");
}

function invalidateBranch(parentId = "", systemNode = "", { markUnloaded = true } = {}) {
  const branch = stateFor(parentId, systemNode);
  branch.generation += 1;
  branch.loading = false;
  branch.indicating = false;
  branch.error = "";
  if (markUnloaded) branch.loaded = false;
  return branch;
}
async function loadChildren(parentId = "", systemNode = "", {
  append = false, force = false, includeFolderId = "", indicate = true,
} = {}) {
  const branch = stateFor(parentId, systemNode);
  const included = systemNode ? "" : String(includeFolderId || "");
  const trackedFolderIds = [...new Set([...(branch.locatedFolderIds || []), ...(included ? [included] : [])])].slice(-100);
  const includedKnown = included && branch.items.some((row) => row.kind === "folder" && row.folderId === included);
  if (branch.loading && !force) {
    if (indicate) branch.indicating = true;
    return branch.items;
  }
  if (!force && branch.loaded && !append && (!included || includedKnown)) return branch.items;

  const generation = ++branch.generation;
  const previousPages = Math.max(0, Number(branch.pages || 0));
  const wantedPages = append ? 1 : Math.max(1, previousPages);
  let cursor = append ? String(branch.nextCursor || "") : "";
  let nextCursor = cursor;
  let hasMore = false;
  let fetchedPages = 0;
  let incoming = [];
  let returnedTrackedFolderIds = null;
  // A forced calibration may replace a still-visible request; preserve that
  // explicit indicator until the latest request completes or is invalidated.
  branch.indicating = indicate || (branch.loading && branch.indicating);
  branch.loading = true;
  branch.error = "";
  try {
    while (fetchedPages < wantedPages) {
      const data = await Api.conversationTreeChildren({
        ...(parentId ? { parentId } : {}),
        ...(systemNode ? { systemNode } : {}),
        ...(cursor ? { cursor } : {}),
        ...(included && fetchedPages === 0 ? { includeFolderId: included } : {}),
        ...(trackedFolderIds.length && fetchedPages === 0 ? { includeFolderIds: trackedFolderIds.join(",") } : {}),
        limit: 50,
      });
      if (generation !== branch.generation) return branch.items;
      if (systemNode && !isExpanded(`__${systemNode}`)) return branch.items;
      incoming = [...incoming, ...((Array.isArray(data.items) ? data.items : []).map(statusAdjustedRow))];
      if (fetchedPages === 0 && Array.isArray(data.includedFolderIds)) {
        returnedTrackedFolderIds = data.includedFolderIds.map(String).filter(Boolean);
      }
      nextCursor = String(data.nextCursor || "");
      hasMore = Boolean(data.hasMore);
      fetchedPages += 1;
      if (append || !hasMore || !nextCursor) break;
      cursor = nextCursor;
    }
    if (generation !== branch.generation) return branch.items;
    branch.items = mergeTreeRows(append ? branch.items : [], incoming);
    branch.nextCursor = nextCursor;
    branch.hasMore = hasMore;
    branch.loaded = true;
    branch.pages = append ? previousPages + fetchedPages : fetchedPages;
    if (!append && returnedTrackedFolderIds !== null) branch.locatedFolderIds = returnedTrackedFolderIds;
    emitRows();
    return branch.items;
  } catch (error) {
    if (generation === branch.generation) branch.error = apiError(error);
    return branch.items;
  } finally {
    if (generation === branch.generation) { branch.loading = false; branch.indicating = false; }
  }
}
async function refreshLoadedBranches() {
  const branches = Object.values(branchState).filter((branch) => branch.loaded || branch.pages > 0);
  await Promise.all(branches.map(async (branch) => {
    if (branch.systemNode && !isExpanded(`__${branch.systemNode}`)) {
      invalidateBranch(branch.parentId, branch.systemNode);
      return;
    }
    await loadChildren(branch.parentId, branch.systemNode, { force: true, indicate: false });
  }));
}
async function ensurePaths(paths = [], folderItems = []) {
  const unique = [];
  const seen = new Set();
  for (const path of paths) {
    const normalized = Array.isArray(path) ? path.map(String).filter(Boolean) : [];
    const key = normalized.join("/");
    if (!seen.has(key)) { seen.add(key); unique.push(normalized); }
  }
  mergeLocatedFolders(folderItems);
  for (const path of unique) {
    for (let index = 0; index < path.length; index += 1) {
      const folderId = path[index];
      setExpanded(folderId, true);
      await loadChildren(folderId, "", { includeFolderId: path[index + 1] || "" });
      mergeLocatedFolders(folderItems);
    }
  }
}
async function refreshTree({
  preserve = true, conversationUuid = "", reveal = false, refreshLoaded = true, trackActive = true,
} = {}) {
  const generation = ++bootstrapGeneration;
  const oldScrollTop = preserve ? listRef.value?.scrollTop : null;
  loading.value = true;
  try {
    const requested = trackActive ? String(conversationUuid || props.activeConversationUuid || "") : "";
    const explicit = requested && !requested.startsWith("local:") ? requested : "";
    const targetWasKnown = Boolean(explicit && everyKnownNode().some((row) => row.conversationUuid === explicit));
    let shouldRevealTarget = Boolean(reveal || (explicit && !targetWasKnown));
    const data = await Api.conversationTreeBootstrap(explicit ? { conversationUuid: explicit } : {});
    if (generation !== bootstrapGeneration) return;
    rootFolders.value = (Array.isArray(data.rootFolders) ? data.rootFolders : []).map(statusAdjustedRow);
    activeCount.value = Number(data.activeCount || 0);
    archivedCount.value = Number(data.archivedCount || 0);
    temporaryCount.value = Number(data.temporaryCount || 0);
    // A successfully returned bootstrap becomes the newest status calibration and
    // invalidates any poll that began against an older tree snapshot.
    statusRequestGeneration += 1;
    applyStatus(referenceCatalog.connected && referenceCatalog.treeStatus ? referenceCatalog.treeStatus : (data.running || {}));

    const selectedItem = trackActive ? data.selected?.item : null;
    // A mutation is not an instruction to follow a conversation into archive.
    if (selectedItem?.archived && !reveal && initialized.value) shouldRevealTarget = false;
    const selectedPath = data.selected?.folderPath || [];
    const selectedFolderItems = data.selected?.folderItems || [];
    const locatedFolders = data.locatedFolders || selectedFolderItems;
    if (!initialized.value || !preserve) {
      const initial = new Set();
      for (const path of data.initialExpandedPaths || []) for (const id of path || []) initial.add(id);
      if ((data.running?.items || []).some((item) => !item.folderId && !item.archived)) initial.add("__temporary");
      if (selectedItem && !selectedItem.folderId && !selectedItem.archived) initial.add("__temporary");
      if (selectedItem?.archived && explicit) initial.add("__archive");
      if (props.draftConversation && !props.draftConversation.folderId) initial.add("__temporary");
      expanded.value = initial;
      await ensurePaths(data.initialExpandedPaths || [], locatedFolders);
      if (initial.has("__temporary")) await loadChildren("", "temporary");
      if (initial.has("__archive") && explicit) {
        archiveUnlocked.value = true;
        await loadChildren("", "archive");
      }
      for (const runningItem of data.running?.items || []) cacheLocatedConversation(runningItem);
      if (selectedItem) cacheLocatedConversation(selectedItem);
      if (!props.autoOpenSelected) { /* keep the current page */ }
      else if (!props.activeConversationUuid && selectedItem?.conversationUuid) emit("open", selectedItem);
      else if (!props.activeConversationUuid && !selectedItem && !activeCount.value) emit("new-conversation", "");
      if (selectedItem && !selectedItem.archived) selectFolder(String(selectedItem.folderId || ""));
    } else {
      if (refreshLoaded) await refreshLoadedBranches();
      if (generation !== bootstrapGeneration) return;
      mergeLocatedFolders(locatedFolders);
      if (shouldRevealTarget && selectedItem?.archived) {
        setExpanded("__archive", true);
        archiveUnlocked.value = true;
        await loadChildren("", "archive", { force: true });
      } else if (shouldRevealTarget && selectedItem && !selectedPath.length) {
        setExpanded("__temporary", true);
        await loadChildren("", "temporary", { force: true });
      } else if (shouldRevealTarget && selectedPath.length) {
        await ensurePaths([selectedPath], selectedFolderItems);
      }
      if (selectedItem) cacheLocatedConversation(selectedItem);
      if (shouldRevealTarget && selectedItem && !selectedItem.archived) selectFolder(String(selectedItem.folderId || ""));
    }
    if (generation !== bootstrapGeneration) return;
    initialized.value = true;
    emitRows();
    await nextTick();
    if (shouldRevealTarget && selectedItem?.conversationUuid) {
      document.querySelector(`[data-tree-id="${CSS.escape(selectedItem.conversationUuid)}"]`)?.scrollIntoView({ block: "nearest" });
    } else if (oldScrollTop !== null && listRef.value) listRef.value.scrollTop = oldScrollTop;
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    if (generation === bootstrapGeneration) loading.value = false;
  }
}
function withLocalRead(item) {
  const read = Math.max(Number(item.activityReadVersion || 0), Number(referenceCatalog.activityReadVersions?.get(item.conversationUuid) || 0));
  return {...item, activityReadVersion: read, activityUnread: Number(item.activityVersion || 0) > read};
}
function applyStatus(data = {}) {
  const revision = Number(data.revision || 0);
  if (revision) statusServerRevision = Math.max(statusServerRevision, revision);
  const adjust = withLocalRead;
  hiddenConversationIds.value = data.hiddenConversationIds || [];
  activityItems.value = (data.activityItems || data.items || []).map(adjust).filter(item => !item.archived && (item.running || item.activityUnread || isActivityWaiting(item)));
  // Every running conversation, including automatic ones, plus user-visible unread results.
  folderActivity.value = folderActivityCounts([...(data.items || []).map(adjust), ...activityItems.value]);
  pendingRows.value = pendingConversationRows(data);
  emit('activity-status', data);
  // Recency is independent of completion/read state; server supplies the newest
  // fifteen actual interactions, including rows absent from the loaded tree pages.
  recentItems.value = (data.recentItems || []).map(adjust).filter(item => !item.archived && !item.local).slice(0, 15);
  collabGroups.value = data.collabGroups || {};
  latestStatusState = {
    raw: data,
    activityLookup: new Map([...activityItems.value, ...pendingRows.value].map(item => [item.conversationUuid, item])),
    lookup: new Map((data.items || []).map((item) => [item.conversationUuid, item])),
    folderRunningCounts: { ...(data.folderRunningCounts || {}) },
    folderConversationCounts: { ...(data.folderConversationCounts || {}) },
  };
  // One pass per branch instead of one pass per node. An unchanged refresh
  // preserves the published row and branch references instead of invalidating
  // every subtree merely because a new JSON packet arrived.
  rootFolders.value = remapRows(rootFolders.value);
  for (const branch of Object.values(branchState)) branch.items = remapRows(branch.items || []);
  searchRows.value = remapRows(searchRows.value);
  emitRows();
}
async function markActivityRead(items) {
  const requests = activityReadRequests(items);
  if (!requests.length || activityReadBusy.value) return;
  activityReadBusy.value = true;
  try {
    const receipt = await Api.readConversationActivity(requests);
    acceptActivityReadReceipt(receipt);
    if (latestStatusState) applyStatus(latestStatusState.raw);
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { activityReadBusy.value = false; }
}
const selecting = ref(false);
const selection = ref(new Map());
const selectionBusy = ref(false);
let selectionAnchor = "";
const selectedRows = computed(() => {
  const known = new Map(knownConversationRows().map(row => [row.conversationUuid, row]));
  return [...selection.value.values()].map(row => known.get(row.conversationUuid) || row);
});
const selectionScope = computed(() => sidebarView.value === "folders" && !query.value.trim() ? selectedFolderId.value : null);
const selectionUnarchive = computed(() => selectedRows.value.length > 0 && selectedRows.value.every(row => row.archived));
function isSelected(row) { return selection.value.has(row?.conversationUuid); }
function enterSelection(row = null) {
  closeMenu(); closeOverview();
  selecting.value = true;
  if (selectableRow(row)) { selection.value = new Map([[row.conversationUuid, row]]); selectionAnchor = row.conversationUuid; }
}
function exitSelection() { selecting.value = false; selection.value = new Map(); selectionAnchor = ""; }
function dropSelection(uuids) {
  const next = new Map(selection.value);
  for (const uuid of uuids) next.delete(uuid);
  selection.value = next;
}
function toggleSelected(row, event) {
  if (!selectableRow(row)) return;
  const next = new Map(selection.value);
  if (event?.shiftKey && selectionAnchor) {
    const lookup = new Map(displayRows.value.filter(selectableRow).map(item => [item.conversationUuid, item]));
    for (const uuid of rangeSelection(displayRows.value, selectionAnchor, row.conversationUuid)) next.set(uuid, lookup.get(uuid) || row);
  } else {
    if (next.has(row.conversationUuid)) next.delete(row.conversationUuid); else next.set(row.conversationUuid, row);
    selectionAnchor = row.conversationUuid;
  }
  selection.value = next;
}
function clickConversation(event, row) {
  if (!selecting.value && (event?.metaKey || event?.ctrlKey) && selectableRow(row)) return enterSelection(row);
  if (selecting.value) return toggleSelected(row, event);
  return row.search ? locateAndOpen(row) : activateRow(row);
}
async function selectScope(automatic = false) {
  const folderId = selectionScope.value;
  if (folderId === null || selectionBusy.value) return;
  const systemNode = folderId ? "" : "temporary";
  selectionBusy.value = true;
  try {
    const rows = [];
    let cursor = "";
    do {
      const data = await Api.conversationTreeChildren({ ...(folderId ? { parentId: folderId } : { systemNode }), limit: 100, ...(cursor ? { cursor } : {}) });
      rows.push(...(data.items || []).filter(item => item.kind === "conversation").map(statusAdjustedRow));
      const next = data.hasMore ? String(data.nextCursor || "") : "";
      if (!next || next === cursor) break;
      cursor = next;
    } while (cursor);
    const chosen = rows.filter(row => selectableRow(row) && (!automatic || automaticRow(row)));
    if (!chosen.length) { ElMessage.info(automatic ? "本目录没有自动创建的会话" : "本目录没有会话"); return; }
    const next = new Map(selection.value);
    for (const row of chosen) next.set(row.conversationUuid, row);
    selection.value = next;
    // Show every selected row so individual ones can still be unticked.
    setExpanded(folderId || "__temporary", true);
    let branch = stateFor(folderId, systemNode);
    if (!branch.loaded) await loadChildren(folderId, systemNode);
    while (branch.hasMore && !branch.error) {
      await loadChildren(folderId, systemNode, { append: true });
      branch = stateFor(folderId, systemNode);
    }
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { selectionBusy.value = false; }
}
function showBatchResult(verb, result) {
  const message = batchMessage(verb, result);
  ElMessage({ type: message.type, message: message.text });
}
async function batchDelete() {
  const ids = [...selection.value.keys()];
  if (!ids.length || selectionBusy.value) return;
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${ids.length} 个会话吗？所有消息和历史记录将被清除，不可恢复。`, "批量删除会话", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" });
  } catch { return; }
  selectionBusy.value = true;
  try {
    const result = await Api.batchConversations({ action: "delete", conversationIds: ids });
    const deleted = (result.results || []).filter(item => item.ok).map(item => item.conversationUuid);
    for (const uuid of deleted) forgetConversation(uuid);
    dropSelection(deleted);
    if (deleted.length) emit("conversations-deleted", deleted);
    await refreshTree({ preserve: true, trackActive: false });
    showBatchResult("删除", result);
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { selectionBusy.value = false; }
}
async function batchArchive() {
  const ids = [...selection.value.keys()];
  if (!ids.length || selectionBusy.value) return;
  const archiving = !selectionUnarchive.value;
  const activeRow = selectedRows.value.find(row => row.conversationUuid === props.activeConversationUuid);
  selectionBusy.value = true;
  try {
    const result = await Api.batchConversations({ action: archiving ? "archive" : "unarchive", conversationIds: ids });
    const changed = new Set((result.results || []).filter(item => item.ok && !item.unchanged).map(item => item.conversationUuid));
    for (const branch of Object.values(branchState)) {
      branch.items = (branch.items || []).filter(item => item.kind !== "conversation" || !changed.has(item.conversationUuid));
    }
    emitRows();
    dropSelection((result.results || []).filter(item => item.ok).map(item => item.conversationUuid));
    if (archiving && activeRow && changed.has(activeRow.conversationUuid)) {
      selectFolder(String(activeRow.folderId || ""));
      emit("new-conversation", String(activeRow.folderId || ""));
    }
    await refreshTree({ preserve: true, trackActive: false });
    if (!archiving && isExpanded("__archive")) await loadChildren("", "archive", { force: true });
    showBatchResult(archiving ? "归档" : "取消归档", result);
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { selectionBusy.value = false; }
}
async function batchMove(targetFolderId) {
  const ids = [...selection.value.keys()];
  const parents = new Set(selectedRows.value.map(row => String(row.folderId || "")));
  const impact = await Api.batchConversations({ action: "move", conversationIds: ids, targetFolderId, impactOnly: true });
  const choice = await chooseImpact("移动", impact);
  if (choice === null) return false;
  const result = await Api.batchConversations({ action: "move", conversationIds: ids, targetFolderId, updateSnapshots: choice === true });
  const moved = (result.results || []).filter(item => item.ok).map(item => item.conversationUuid);
  dropSelection(moved);
  if (moved.includes(props.activeConversationUuid)) selectFolder(String(targetFolderId || ""));
  for (const parent of parents) await invalidateAndRefresh(parent, targetFolderId);
  showBatchResult("移动", result);
  if (result.skippedRunningCount) ElMessage.warning(`${result.skippedRunningCount} 个运行中会话的快照本次跳过，不会延后更新`);
  return true;
}
function openActivityConversation(row) {
  // Opening the virtual alias does not expand/move/select its original folder.
  closeOverview();
  cacheLocatedConversation(row);
  emitRows();
  emit("open", row);
}
async function refreshStatus() {
  const request = ++statusRequestGeneration;
  try {
    const data = await Api.conversationTreeStatus();
    if (request !== statusRequestGeneration) return;
    // Bootstrap and polling share one client request order. The server timestamp is diagnostic only.
    applyStatus(data);
  } catch { /* a failed calibration must not mutate the current tree */ }
}
function scheduleStatus() {
  if (statusTimer) clearTimeout(statusTimer);
  if (referenceCatalog.connected && referenceCatalog.ready) return;
  const fast = knownConversationRows().some(running);
  statusTimer = window.setTimeout(async () => { await refreshStatus(); scheduleStatus(); }, fast ? 5000 : 20000);
}

async function toggleRow(row) {
  if (row.recentAlias) return;
  if (row.kind === "conversation" || row.kind === "group") { setExpanded(row.expandId, !isExpanded(row.expandId)); return; }
  const id = row.kind === "system" ? row.id : row.folderId;
  const opening = !isExpanded(id);
  setExpanded(id, opening);
  if (!opening) {
    const branch = row.kind === "system" ? stateFor("", row.systemNode) : stateFor(row.folderId);
    if (branch.loading) invalidateBranch(branch.parentId, branch.systemNode);
    return;
  }
  if (row.kind === "system") {
    if (row.systemNode === "archive") archiveUnlocked.value = true;
    await loadChildren("", row.systemNode);
  } else await loadChildren(row.folderId);
}
function selectFolder(folderId, notify = true) {
  selectedFolderId.value = String(folderId || "");
  if (notify) emit("selected-folder", selectedFolderId.value);
}
async function revealDraft(folderId = "") {
  const target = String(folderId || "");
  query.value = "";
  await switchSidebarView('folders');
  try {
    if (target) {
      const data = await Api.locateConversationFolderInTree(target);
      if (String(props.draftConversation?.folderId || "") !== target) return;
      await ensurePaths([data.folderPath || [target]], data.folderItems || []);
    } else {
      setExpanded("__temporary", true);
      await loadChildren("", "temporary");
    }
    if (!props.draftConversation || String(props.draftConversation.folderId || "") !== target) return;
    selectFolder(target);
    await nextTick();
    document.querySelector(`[data-tree-id="${CSS.escape(props.draftConversation.conversationUuid)}"]`)?.scrollIntoView({ block: "nearest" });
  } catch (error) { ElMessage.error(apiError(error)); }
}
async function revealFolder(folderId) {
  const target = String(folderId || "");
  if (!target) return;
  query.value = "";
  await switchSidebarView('folders');
  try {
    const data = await Api.locateConversationFolderInTree(target);
    await ensurePaths([data.folderPath || [target]], data.folderItems || []);
    setExpanded(target, true);
    await loadChildren(target);
    selectFolder(target);
    await nextTick();
    document.querySelector(`[data-tree-id="${CSS.escape(target)}"]`)?.scrollIntoView({ block: "nearest" });
  } catch (error) { ElMessage.error(apiError(error)); }
}
async function activateRow(row) {
  closeOverview();
  if (row.kind === "group") { await toggleRow(row); return; }
  if (row.kind === "folder") { selectFolder(row.folderId); await toggleRow(row); return; }
  if (row.kind === "system") {
    if (row.systemNode === "temporary") selectFolder("");
    await toggleRow(row);
    return;
  }
  if (row.kind !== "conversation") return;
  if (row.recentAlias) {
    const {recentAlias, depth, ...original} = row;
    openActivityConversation(original);
    return;
  }
  if (!row.archived && !row.groupMember) selectFolder(String(row.folderId || ""));
  emit("open", row);
}
async function locateAndOpen(row) {
  closeOverview();
  if (row.kind === "folder") {
    query.value = '';
    await switchSidebarView('folders');
    const data = await Api.locateConversationFolderInTree(row.folderId);
    await ensurePaths([data.folderPath || []], data.folderItems || [row]);
    selectFolder(row.folderId);
    await nextTick();
    document.querySelector(`[data-tree-id="${CSS.escape(row.folderId)}"]`)?.scrollIntoView({ block: "center" });
    return;
  }
  const data = await Api.locateConversationInTree(row.conversationUuid);
  if (data.item?.archived) {
    setExpanded("__archive", true);
    archiveUnlocked.value = true;
    await loadChildren("", "archive", { force: true });
  } else if (!data.folderPath?.length) {
    setExpanded("__temporary", true);
    await loadChildren("", "temporary");
  } else await ensurePaths([data.folderPath], data.folderItems || []);
  mergeLocatedFolders(data.folderItems || []);
  cacheLocatedConversation(data.item);
  emitRows();
  emit("open", data.item || row);
  if (!data.item?.archived) selectFolder(String(data.item?.folderId || ""));
  await nextTick();
  document.querySelector(`[data-tree-id="${CSS.escape(row.conversationUuid)}"]`)?.scrollIntoView({ block: "center" });
}

function searchRequestKey() {
  return JSON.stringify([query.value.trim(), Boolean(searchArchived.value && archiveUnlocked.value)]);
}
function invalidateSearch() {
  searchGeneration += 1;
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = 0;
  searchLoading.value = false;
  searchRows.value = [];
  searchCursor.value = "";
  searchHasMore.value = false;
  searchResultKey = "";
}
async function runSearch({ append = false } = {}) {
  const text = query.value.trim(), key = searchRequestKey();
  if (!text) { invalidateSearch(); return; }
  if (append && (searchLoading.value || !searchHasMore.value || !searchCursor.value || searchResultKey !== key)) return;
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = 0;
  const generation = ++searchGeneration;
  const current = () => generation === searchGeneration && searchRequestKey() === key;
  searchLoading.value = true;
  try {
    const data = await Api.conversationTreeSearch({ q: text, limit: 50, ...(append ? { cursor: searchCursor.value } : {}), ...(searchArchived.value && archiveUnlocked.value ? { archiveUnlocked: 1 } : {}) });
    if (!current()) return;
    const incoming = (data.items || []).map(statusAdjustedRow);
    searchRows.value = append ? [...searchRows.value, ...incoming] : incoming;
    searchCursor.value = String(data.nextCursor || "");
    searchHasMore.value = Boolean(data.hasMore);
    searchResultKey = key;
  } catch (error) {
    if (current()) ElMessage.error(apiError(error));
  } finally {
    if (current()) searchLoading.value = false;
  }
}
watch(query, () => {
  invalidateSearch();
  if (query.value.trim()) searchTimer = setTimeout(() => runSearch(), 260);
}, {flush: "sync"});
watch([searchArchived, archiveUnlocked], () => {
  invalidateSearch();
  if (query.value.trim()) void runSearch();
}, {flush: "sync"});
watch(() => props.draftConversation, () => emitRows(), { deep: true });

async function openMenu(event, row) {
  closeOverview();
  if (!["folder", "conversation", "system", "root", "group"].includes(row?.kind)) return;
  event?.preventDefault?.(); event?.stopPropagation?.();
  const recent = Boolean(row.recentAlias);
  if (row.recentAlias) { const {recentAlias, depth, ...original} = row; row = original; }
  const pad = 8;
  const pointerX = Math.max(pad, Number(event?.clientX || pad));
  const pointerY = Math.max(pad, Number(event?.clientY || pad));
  menuAnchor = event?.currentTarget || null;
  menu.value = { open: true, row, recent, x: pointerX, y: pointerY };
  await nextTick();
  if (!menu.value.open || String(menu.value.row?.id || rowId(menu.value.row) || "") !== String(row.id || rowId(row) || "")) return;
  const element = document.querySelector("[data-conversation-tree-menu]");
  const bounds = element?.getBoundingClientRect?.();
  if (!bounds) return;
  const viewport = window.matchMedia?.(MOBILE_VIEWPORT_QUERY)?.matches && window.visualViewport;
  const left = viewport?.offsetLeft || 0;
  const top = viewport?.offsetTop || 0;
  menu.value = {
    ...menu.value,
    x: Math.max(left + pad, Math.min(pointerX, left + (viewport?.width || window.innerWidth) - bounds.width - pad)),
    y: Math.max(top + pad, Math.min(pointerY, top + (viewport?.height || window.innerHeight) - bounds.height - pad)),
  };
}
function moreMenuKeydown(event) {
  // Keep native button activation, without the enclosing treeitem's Enter-open.
  // Escape, context-menu keys and application shortcuts still bubble normally.
  if (event.key === "Enter" || event.key === " ") event.stopPropagation();
}
function openMoreMenu(event, row) {
  if (event?.type === "contextmenu") return openMenu(event, row);
  if (menu.value.open && menuAnchor === event.currentTarget && menu.value.row?.kind === row.kind && rowId(menu.value.row) === rowId(row)) {
    event.preventDefault(); event.stopPropagation();
    closeMenu();
    return;
  }
  const rect = event.currentTarget.getBoundingClientRect();
  return openMenu({
    preventDefault: () => event.preventDefault(),
    stopPropagation: () => event.stopPropagation(),
    clientX: rect.right - 8,
    clientY: rect.bottom + 4,
    currentTarget: event.currentTarget,
  }, row);
}
function openRootMenu(event) { return openMenu(event, rootDropTarget); }
function closeMenu() { menu.value = { open: false, x: 0, y: 0, row: null, recent: false }; menuAnchor = null; }
async function promptFolder(parentId = "") {
  try {
    const { value } = await ElMessageBox.prompt("目录可继续包含任意深度的子目录和会话。", "新建目录", { inputPlaceholder: "目录名称", inputValidator: (v) => String(v || "").trim() ? true : "名称不能为空", confirmButtonText: "创建", cancelButtonText: "取消" });
    await Api.createConversationFolder({ name: String(value).trim(), parentId });
    if (parentId) {
      setExpanded(parentId, true);
      await invalidateAndRefresh(parentId, parentId);
    } else await refreshTree({ preserve: true, refreshLoaded: false });
    ElMessage.success("目录已创建");
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
}
async function renameRow(row) {
  try {
    const { value } = await ElMessageBox.prompt("", row.kind === "folder" ? "重命名目录" : "重命名会话", { inputValue: rowLabel(row), inputValidator: (v) => {
      const title = String(v || "").trim();
      if (!title) return "名称不能为空";
      return true;
    }, confirmButtonText: "保存", cancelButtonText: "取消" });
    const result = row.kind === "folder"
      ? await Api.updateConversationFolder(row.folderId, { name: String(value).trim() })
      : await Api.updateConversation(row.conversationUuid, { title: String(value).trim() });
    applyMutationRow(row, row.kind === "folder" ? result.folder : result.conversation);
    if (query.value.trim()) await runSearch();
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
}
async function togglePin(row) {
  let result;
  if (row.kind === "folder") result = await Api.updateConversationFolder(row.folderId, { pinned: !row.pinned });
  else if (row.pinned) result = await Api.unpinConversation(row.conversationUuid); else result = await Api.pinConversation(row.conversationUuid);
  applyMutationRow(row, row.kind === "folder" ? result.folder : result.conversation);
  if (query.value.trim()) await runSearch();
}
function setConversationTitleLocally(conversationUuid, title) {
  const uuid = String(conversationUuid || "");
  for (const branch of Object.values(branchState)) {
    for (const item of branch.items || []) if (item.conversationUuid === uuid) item.title = title;
  }
  for (const list of [searchRows.value, activityItems.value, recentItems.value]) {
    for (const item of list) if (item.conversationUuid === uuid) item.title = title;
  }
  emitRows();
}
async function generateConversationTitle(row) {
  const uuid = String(row?.conversationUuid || "");
  if (!uuid || row.local || isTitleGenerating(row)) return;
  if (running(row)) return ElMessage.warning("当前轮完成后再生成");
  if (!hasConversationMessages(row)) return ElMessage.warning("会话还没有可用于命名的内容");
  setTitleGenerating(uuid, true);
  const pendingMessage = ElMessage.info({ message: "正在生成会话名称…", duration: 0 });
  let result;
  try {
    result = await Api.generateConversationTitle(uuid);
    if (result?.title) setConversationTitleLocally(uuid, result.title);
  } finally {
    setTitleGenerating(uuid, false);
    pendingMessage?.close?.();
  }
  ElMessage.success(result?.changed ? "会话名称已生成" : "会话名称无需更新");
}
async function duplicateConversation(row) {
  if (running(row)) return ElMessage.warning("运行中的会话暂不能复制");
  const data = await Api.duplicateConversation(row.conversationUuid);
  const conversationUuid = data.conversation?.conversationUuid || data.state?.conversationUuid || "";
  if (conversationUuid) await refreshTree({ preserve: true, conversationUuid, reveal: true, refreshLoaded: false });
  else await refreshAffected(row);
  if (data.conversation) emit("open", data.conversation);
}
async function archiveConversation(row) {
  const archiving = !row.archived;
  const result = await Api.setConversationArchived(row.conversationUuid, archiving);
  const nextConversation = archiving && props.activeConversationUuid === row.conversationUuid ? result.nextConversation : null;
  for (const branch of Object.values(branchState)) {
    branch.items = (branch.items || []).filter((item) => item.conversationUuid !== row.conversationUuid);
  }
  emitRows();
  if (archiving && props.activeConversationUuid === row.conversationUuid) {
    selectFolder(String(row.folderId || ""));
    if (nextConversation?.conversationUuid) emit("open", nextConversation);
    else emit("new-conversation", String(row.folderId || ""));
    await nextTick();
  }
  // Refresh counts and previously loaded branches, but do not locate the newly
  // archived conversation or unlock/expand the archive node as a side effect.
  await refreshTree({ preserve: true, trackActive: false });
  if (nextConversation?.conversationUuid && props.activeConversationUuid === nextConversation.conversationUuid) await mergeLocatedItem(nextConversation);
  if (query.value.trim()) await runSearch();
}
function removeConversation(row) {
  if (running(row)) return ElMessage.warning("运行中的会话不能删除");
  // App owns local draft persistence and active conversation selection. Both
  // local and persisted deletions must go through that same lifecycle owner.
  emit("delete-conversation", row);
}
async function showProperties(row, initialTab = 'context') {
  if (propertiesDialog.value && webhookEditor.value && !await webhookEditor.value.canLeave()) return;
  const request = ++propertiesRequestGeneration;
  propertiesSaveGeneration += 1;
  propertiesSaving.value = false;
  propertiesTab.value = initialTab;
  webhookVisited.value = initialTab === 'trigger';
  propertyModelOptions.value = [];
  propertyModelsLoaded.value = false;
  resetPropertiesForm(row);
  const folderId = propertiesForm.folderId;
  propertiesDialog.value = true;
  propertiesLoading.value = true;
  const [propertiesResult, optionsResult] = await Promise.allSettled([
    Api.conversationFolderProperties(folderId),
    Api.rathOptions(),
  ]);
  if (request !== propertiesRequestGeneration || !propertiesDialog.value) return;
  try {
    if (propertiesResult.status === "rejected") throw propertiesResult.reason;
    const data = propertiesResult.value;
    if (optionsResult.status === "fulfilled") {
      propertyModelOptions.value = Array.isArray(optionsResult.value?.models) ? optionsResult.value.models : [];
      propertyModelsLoaded.value = true;
    } else {
      ElMessage.warning("模型选项加载失败；仍可编辑上下文，运行默认暂不可改");
    }
    Object.assign(propertiesForm, {
      folderId, parentId: String(row.parentId || ""), name: data.name, path: data.path,
      hideFromRecent: !!data.hideFromRecent, hiddenFromRecentByAncestor: !!data.hiddenFromRecentByAncestor,
      workspaceDir: data.workspace?.local || "", promptMarkdown: data.promptPolicy?.local?.text ?? data.prompt?.local ?? "",
      promptPolicy: promptPolicyFlags(data.promptPolicy?.local),
      inheritedPromptPolicy: normalizePromptPolicy(data.promptPolicy?.inherited || {text: data.prompt?.inherited}),
      inheritedPromptSourcePath: data.promptPolicy?.inheritedSourcePath || "",
      runDefaults: sparseRunDefaults(data.runDefaults?.local),
      runInherited: sparseRunDefaults(data.runDefaults?.inherited),
      runFallback: sparseRunDefaults(data.runDefaults?.fallback),
      runResolved: sparseRunDefaults(data.runDefaults?.resolved),
    });
    propertiesRunDefaultsBaseline.value = sparseRunDefaults(data.runDefaults?.local);
  } catch (error) {
    propertiesDialog.value = false;
    ElMessage.error(apiError(error));
  } finally {
    if (request === propertiesRequestGeneration) propertiesLoading.value = false;
  }
}
const folderPromptPolicy = computed({
  get: () => normalizePromptPolicy({...propertiesForm.promptPolicy, text: propertiesForm.promptMarkdown}),
  set: value => { if (!propertiesLoading.value) { propertiesForm.promptMarkdown = value.text; propertiesForm.promptPolicy = promptPolicyFlags(value); } },
});
const propertyModelsLoaded = ref(false);
const runDefaultsChanged = computed(() => JSON.stringify(sparseRunDefaults(propertiesForm.runDefaults))
  !== JSON.stringify(propertiesRunDefaultsBaseline.value));
const draftRunDefaults = computed(() => resolvedRunDefaults(
  propertiesForm.runDefaults,
  propertiesForm.runInherited,
  propertiesForm.runFallback,
));
const appliedRunDefaults = computed(() => {
  if (!propertyModelsLoaded.value) {
    const serverResolved = sparseRunDefaults(propertiesForm.runResolved);
    return Object.keys(serverResolved).length ? resolvedRunDefaults(serverResolved, {}, propertiesForm.runFallback) : draftRunDefaults.value;
  }
  return normalizedRunDefaults({
    local: propertiesForm.runDefaults,
    inherited: propertiesForm.runInherited,
    fallback: propertiesForm.runFallback,
    resolved: propertiesForm.runResolved,
  }, propertyModelOptions.value);
});
const inheritedRunDefaults = computed(() => Object.fromEntries(RUN_DEFAULT_FIELDS.map(field => {
  const local = {...propertiesForm.runDefaults}; delete local[field];
  return [field, normalizedRunDefaults({local,inherited:propertiesForm.runInherited,fallback:propertiesForm.runFallback,resolved:propertiesForm.runResolved},propertyModelOptions.value)[field]];
})));
const hasLocalRunDefaults = computed(() => Object.keys(sparseRunDefaults(propertiesForm.runDefaults)).length > 0);
function propertyModel(key) {
  return propertyModelOptions.value.find((model) => model.key === String(key || "")) || null;
}
const mainDefaultModelKey = computed(() => String(appliedRunDefaults.value.mainModel || ""));
const mainDefaultModel = computed(() => propertyModel(mainDefaultModelKey.value));
const mainThinkingOptions = computed(() => {
  const levels = modelThinkingLevels(mainDefaultModel.value);
  return levels.length ? levels : ["off"];
});
const agentDefaultModelKey = computed(() => {
  const configured = appliedRunDefaults.value.agentModel;
  return configured === "" ? mainDefaultModelKey.value : String(configured || "");
});
const agentDefaultModel = computed(() => propertyModel(agentDefaultModelKey.value));
const agentThinkingOptions = computed(() => modelThinkingLevels(agentDefaultModel.value));
function replaceLocalRunDefault(field, value) {
  propertiesForm.runDefaults = updateRunDefault(propertiesForm.runDefaults, field, runDefaultOption(value));
}
function cleanAgentDefaultsForModel() {
  if (hasRunDefault(propertiesForm.runDefaults, "agentThinkLevel")) {
    const value = propertiesForm.runDefaults.agentThinkLevel;
    if (value !== "" && !agentThinkingOptions.value.includes(value)) replaceLocalRunDefault("agentThinkLevel", "");
  }
  if (hasRunDefault(propertiesForm.runDefaults, "agentFastMode")
      && propertiesForm.runDefaults.agentFastMode === true && !agentDefaultModel.value?.supportsFast) {
    replaceLocalRunDefault("agentFastMode", false);
  }
}
function cleanMainDefaultsForModel() {
  if (hasRunDefault(propertiesForm.runDefaults, "mainThinkingLevel")) {
    const value = propertiesForm.runDefaults.mainThinkingLevel;
    if (!mainThinkingOptions.value.includes(value)) {
      const modelDefault = modelDefaultThinking(mainDefaultModel.value);
      const nextValue = mainThinkingOptions.value.includes(modelDefault) ? modelDefault : mainThinkingOptions.value.at(-1) || "off";
      replaceLocalRunDefault("mainThinkingLevel", nextValue);
    }
  }
  if (hasRunDefault(propertiesForm.runDefaults, "mainFastMode")
      && propertiesForm.runDefaults.mainFastMode === true && !mainDefaultModel.value?.supportsFast) {
    replaceLocalRunDefault("mainFastMode", false);
  }
  cleanAgentDefaultsForModel();
}
function setRunDefault(field, selection) {
  propertiesForm.runDefaults = updateRunDefault(propertiesForm.runDefaults, field, selection);
  if (field === "mainModel") cleanMainDefaultsForModel();
  else if (field === "agentModel") cleanAgentDefaultsForModel();
}
function clearRunDefaults() {
  propertiesForm.runDefaults = {};
}
function runDefaultsValidationError() {
  if (!propertyModelsLoaded.value) return "";
  const local = propertiesForm.runDefaults;
  if (hasRunDefault(local, "mainModel")) {
    if (typeof local.mainModel !== "string" || !local.mainModel) return "主会话模型不能留空；可选择模型或恢复继承";
    if (propertyModelsLoaded.value && !propertyModel(local.mainModel)) return `主会话模型 ${local.mainModel} 已不可用，请更换或恢复继承`;
  }
  if (hasRunDefault(local, "mainThinkingLevel")
      && (typeof local.mainThinkingLevel !== "string" || !mainThinkingOptions.value.includes(local.mainThinkingLevel))) {
    return "主会话思考强度不受当前生效模型支持，请重新选择或恢复继承";
  }
  if (hasRunDefault(local, "mainFastMode")) {
    if (typeof local.mainFastMode !== "boolean") return "主会话 Fast 必须明确选择开启或关闭";
    if (local.mainFastMode && propertyModelsLoaded.value && !mainDefaultModel.value?.supportsFast) return "当前主会话模型不支持 Fast";
  }
  if (hasRunDefault(local, "agentModel")) {
    if (typeof local.agentModel !== "string") return "Agent 模型配置无效";
    if (local.agentModel && propertyModelsLoaded.value && !propertyModel(local.agentModel)) return `Agent 模型 ${local.agentModel} 已不可用，请更换或恢复继承`;
  }
  if (hasRunDefault(local, "agentThinkLevel")
      && (typeof local.agentThinkLevel !== "string" || (local.agentThinkLevel !== "" && !agentThinkingOptions.value.includes(local.agentThinkLevel)))) {
    return "Agent 思考强度不受当前生效模型支持，请重新选择或恢复继承";
  }
  if (hasRunDefault(local, "agentFastMode")) {
    if (![true, false, null].includes(local.agentFastMode)) return "Agent Fast 配置无效";
    if (local.agentFastMode === true && propertyModelsLoaded.value && !agentDefaultModel.value?.supportsFast) return "当前 Agent 生效模型不支持 Fast";
  }
  return "";
}
function resetPropertiesForm(row) {
  propertiesRunDefaultsBaseline.value = {};
  Object.assign(propertiesForm, {
    folderId: row.systemNode === "temporary" ? "__temporary" : String(row.folderId || ""),
    temporary: row.systemNode === "temporary",
    parentId: String(row.parentId || ""), name: String(row.name || ""), path: String(row.path || ""),
    workspaceDir: "", promptMarkdown: "", hideFromRecent: false, hiddenFromRecentByAncestor: false,
    promptPolicy: promptPolicyFlags(), inheritedPromptPolicy: normalizePromptPolicy(), inheritedPromptSourcePath: "",
    runDefaults: {}, runInherited: {}, runFallback: {}, runResolved: {},
  });
}
async function closeProperties(done) {
  if (webhookEditor.value && !await webhookEditor.value.canLeave()) return;
  propertiesDialog.value = false;
  propertiesRequestGeneration += 1;
  if (typeof done === 'function') done();
}
watch(propertiesDialog, (open) => {
  if (!open) propertiesRequestGeneration += 1;
});
watch(propertiesTab, async () => {
  if (propertiesTab.value === 'trigger') webhookVisited.value = true;
  await nextTick();
  document.querySelector(".folder-properties-dialog .el-dialog__body")?.scrollTo({ top: 0 });
});
function navigatePropertiesTab(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')];
  const index = tabs.indexOf(event.target.closest('[role="tab"]'));
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  propertiesTab.value = tabs[next].dataset.tab;
  tabs[next].focus();
}
function chooseImpact(action, impact) {
  if (!Number(impact?.affectedCount || 0)) return Promise.resolve(false);
  impactState.action = action;
  impactState.impact = impact;
  impactDialog.value = true;
  return new Promise((resolve) => { impactState.resolve = resolve; });
}
function finishImpact(value) {
  impactDialog.value = false;
  const resolve = impactState.resolve;
  impactState.resolve = null;
  resolve?.(value);
}
async function saveProperties() {
  if (propertiesSaving.value || webhookEditor.value?.saving) return;
  // Context-only edits must not revalidate/rewrite stored defaults that became
  // unavailable after a model or parent change. An explicit defaults edit still
  // validates the full replacement; reverting to the loaded values is a no-op.
  const saveRunDefaults = propertyModelsLoaded.value && runDefaultsChanged.value;
  const validationError = saveRunDefaults ? runDefaultsValidationError() : "";
  if (validationError) return ElMessage.warning(validationError);
  const request = propertiesRequestGeneration;
  const save = ++propertiesSaveGeneration;
  const folderId = propertiesForm.folderId;
  const temporary = propertiesForm.temporary;
  propertiesSaving.value = true;
  const payload = {
    ...(!temporary ? { workspaceDir: propertiesForm.workspaceDir, hideFromRecent: propertiesForm.hideFromRecent } : {}),
    promptMarkdown: propertiesForm.promptMarkdown,
    promptPolicy: promptPolicyFlags(propertiesForm.promptPolicy),
    ...(saveRunDefaults ? { runDefaults: sparseRunDefaults(propertiesForm.runDefaults) } : {}),
  };
  const draftAtSubmit = JSON.stringify(propertiesForm);
  try {
    const impact = await Api.conversationFolderPropertiesImpact(folderId, payload);
    if (request !== propertiesRequestGeneration || !propertiesDialog.value) return;
    const choice = await chooseImpact("保存", impact);
    if (choice === null || request !== propertiesRequestGeneration || !propertiesDialog.value) return;
    const result = await Api.updateConversationFolderProperties(folderId, { ...payload, updateSnapshots: choice === true });
    window.dispatchEvent(new CustomEvent("openbear:folder-properties-changed", { detail: { folderId } }));
    // Saving another tab must not destroy trigger edits (or an in-flight Key response).
    if (request === propertiesRequestGeneration) {
      const leave = !webhookEditor.value || await webhookEditor.value.canLeave();
      if (leave && request === propertiesRequestGeneration && JSON.stringify(propertiesForm) === draftAtSubmit) propertiesDialog.value = false;
    }
    if (!temporary) {
      if (result.folder) mergeLocatedFolders([result.folder]);
      else {
        // Compatibility with an older server during rolling upgrades.
        const located = await Api.locateConversationFolderInTree(folderId);
        mergeLocatedFolders(located.folderItems || []);
      }
      emitRows();
      await refreshStatus();
    }
    const skipped = Number(result.skippedRunningCount || 0);
    const later = request === propertiesRequestGeneration && JSON.stringify(propertiesForm) !== draftAtSubmit;
    ElMessage.success((choice === true ? `属性已保存，更新 ${result.updatedCount || 0} 个快照${skipped ? `；运行中跳过 ${skipped} 个` : ""}` : "属性已保存；已有快照保持不变") + (later ? "；后续修改仍未保存，已保留草稿" : ""));
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { if (save === propertiesSaveGeneration) propertiesSaving.value = false; }
}

async function loadAllFolders() {
  const data = await Api.conversationTreeFolders();
  allFolders.value = Array.isArray(data.items) ? data.items : [];
}
function folderAncestorIds(folderId) {
  const byId = new Map(allFolders.value.map((folder) => [folder.folderId, folder]));
  const path = [], seen = new Set();
  let current = folderId;
  while (current && byId.has(current) && !seen.has(current)) { seen.add(current); path.unshift(current); current = byId.get(current)?.parentId || ""; }
  return path;
}
function invalidMoveTarget(folder) {
  const row = moveRow.value;
  if (!row || row.kind !== "folder") return false;
  return folder.folderId === row.folderId || folderAncestorIds(folder.folderId).includes(row.folderId);
}
async function showMove(row, mode = "move") {
  moveMode.value = mode;
  moveRow.value = row;
  moveFolderId.value = String(row.parentId === "__archive" ? row.folderId || "" : row.parentId ?? row.folderId ?? "");
  moveFolderQuery.value = "";
  moveUnarchive.value = true;
  moveUpdateSnapshots.value = true;
  await loadAllFolders();
  moveDialog.value = true;
}
async function submitMove() {
  const row = moveRow.value;
  if (!row) return;
  moveBusy.value = true;
  try {
    if (moveMode.value === "batch") {
      if (await batchMove(moveFolderId.value)) moveDialog.value = false;
      return;
    }
    if (moveMode.value === "delete") {
      const impact = await Api.deleteConversationFolder(row.folderId, { targetFolderId: moveFolderId.value, impactOnly: true });
      await ElMessageBox.confirm("目录中的子目录与会话会迁移到所选位置；聊天内容不会删除。", `删除目录「${row.name}」`, { type: "warning", confirmButtonText: "迁移并删除", cancelButtonText: "取消" });
      const choice = await chooseImpact("删除并迁移", impact);
      if (choice === null) return;
      const result = await Api.deleteConversationFolder(row.folderId, { targetFolderId: moveFolderId.value, updateSnapshots: choice === true });
      forgetFolder(row.folderId);
      if (selectedFolderId.value === row.folderId) selectFolder(moveFolderId.value);
      emit("folder-removed", { folderId: row.folderId, targetFolderId: moveFolderId.value });
      await nextTick();
      ElMessage.success(`目录已删除，聊天内容已迁移${result.skippedRunningCount ? `；运行中快照跳过 ${result.skippedRunningCount} 个` : ""}`);
    } else {
      const archivedMove = row.kind === "conversation" && row.archived;
      const options = archivedMove ? { unarchive: moveUnarchive.value, updateSnapshots: moveUpdateSnapshots.value } : null;
      if (!await moveTreeItem(row, moveFolderId.value, "", "", options)) return;
    }
    moveDialog.value = false;
    if (moveMode.value === "delete") {
      await invalidateAndRefresh(treeItemParent(row), moveFolderId.value, null);
    }
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
  finally { moveBusy.value = false; }
}
async function moveTreeItem(row, targetFolderId, beforeId = "", afterId = "", options = null) {
  if (moveInFlight.value) return false;
  const archivedOptions = row.kind === "conversation" && row.archived && options;
  const request = { kind: row.kind, id: rowId(row), targetFolderId, beforeId, afterId,
    ...(archivedOptions ? { unarchive: options.unarchive === true } : {}) };
  const currentParent = treeItemParent(row);
  if (currentParent === String(targetFolderId || "") && !beforeId && !afterId &&
      !(archivedOptions && (options.unarchive || options.updateSnapshots))) return true;
  moveInFlight.value = true;
  movingRowId.value = rowId(row);
  try {
    let updateSnapshots = archivedOptions ? options.updateSnapshots === true : false;
    if (!archivedOptions && currentParent !== String(targetFolderId || "")) {
      const impact = await Api.conversationTreeMoveImpact(request);
      const choice = await chooseImpact("移动", impact);
      if (choice === null) return false;
      updateSnapshots = choice === true;
    }
    const result = await Api.moveConversationTreeItem({ ...request, updateSnapshots });
    applyMoveOrganization(row, targetFolderId, result.organization || {});
    if (row.kind === "conversation" && row.conversationUuid === props.activeConversationUuid) selectFolder(String(targetFolderId || ""));
    if (query.value.trim()) await runSearch();
    if (result.skippedRunningCount) ElMessage.warning(`已移动；${result.skippedRunningCount} 个运行中会话的快照本次跳过，不会延后更新`);
    return true;
  } finally { moveInFlight.value = false; movingRowId.value = ""; }
}
async function mergeLocatedItem(row) {
  if (!row) return;
  try {
    if (row.kind === "folder") {
      const data = await Api.locateConversationFolderInTree(row.folderId);
      mergeLocatedFolders(data.folderItems || []);
    } else if (row.conversationUuid) {
      const data = await Api.locateConversationInTree(row.conversationUuid);
      mergeLocatedFolders(data.folderItems || []);
      cacheLocatedConversation(data.item);
    }
    emitRows();
  } catch { /* A just-deleted item has no exact row to merge. */ }
}
async function invalidateAndRefresh(oldParent, newParent, locatedRow = null) {
  for (const parent of new Set([String(oldParent || ""), String(newParent || "")])) {
    const systemNode = parent ? "" : "temporary";
    const branch = stateFor(parent, systemNode);
    const shouldReload = branch.loaded || branch.loading || (parent ? isExpanded(parent) : isExpanded("__temporary"));
    invalidateBranch(parent, systemNode);
    if (shouldReload) await loadChildren(parent, systemNode, { force: true });
  }
  await refreshTree({ preserve: true, refreshLoaded: false });
  await refreshStatus();
  await mergeLocatedItem(locatedRow);
  if (query.value.trim()) await runSearch();
}
async function refreshAffected(row) {
  const parent = row.kind === "folder" ? String(row.parentId || "") : String(row.folderId || "");
  await invalidateAndRefresh(parent, parent, row);
}

async function runMenuAction(action) {
  const { row, recent } = menu.value; closeMenu(); if (!row) return;
  try {
    if (action === "locate") await revealInFolders(row);
    else if (action === "new-sibling" && row.kind === "conversation") {
      const folderId = String(row.folderId || "");
      selectFolder(folderId);
      emit("new-conversation", folderId, { revealInFolders: !recent });
    }
    else if (action === "new-conversation") {
      const folderId = row.kind === "folder" ? String(row.folderId || "") : "";
      selectFolder(folderId);
      emit("new-conversation", folderId);
    }
    else if (action === "new-folder") await promptFolder(row.kind === "folder" ? row.folderId : "");
    else if (action === "generate-title") await generateConversationTitle(row);
    else if (action === "rename") await renameRow(row);
    else if (action === "pin") await togglePin(row);
    else if (action === "move") await showMove(row);
    else if (action === "cron" && row.kind === "folder" && row.folderId) {
      window.dispatchEvent(new CustomEvent('openbear:open-cron', {detail:{folderId:String(row.folderId)}}));
    }
    else if (action === "properties" && row.kind === "group") groupProperties.value = { open: true, groupId: row.groupId };
    else if (action === "properties") await showProperties(row);
    else if (action === "conversation-properties") emit('conversation-properties', row);
    else if (action === "refresh-prompt" && !row.local && !running(row)) {
      promptRow.value = { ...row };
      promptDialog.value = true;
    }
    else if (action === "delete-folder") await showMove(row, "delete");
    else if (action === "duplicate") await duplicateConversation(row);
    else if (action === "archive") await archiveConversation(row);
    else if (action === "delete-conversation") await removeConversation(row);
    else if (action === "select") {
      if (row.kind === "folder") selectFolder(row.folderId);
      else if (row.kind === "system") selectFolder("");
      enterSelection(row);
    }
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
}
async function openBatchMove() {
  if (selection.value.size) await showMove({ kind: "conversation", batch: true, folderId: selectedFolderId.value }, "batch");
}

function dragStart(event, row) {
  closeOverview();
  if (moveInFlight.value || !["folder", "conversation"].includes(row.kind) || row.local || (row.kind === 'folder' && (query.value || row.archived))) { event.preventDefault(); return; }
  closeMenu();
  // Recent aliases can export a reference, never become a tree-move source.
  const canMove = !row.recentAlias && !query.value && !row.archived;
  drag.value = { row: canMove ? row : null, target: null, zone: "", busy: false };
  if (!row.recentAlias) event.dataTransfer.setData('application/x-openbear-tree', row.kind);
  if (row.kind === 'conversation') {
    const reference = {kind:'chat',id:row.conversationUuid,label:row.title || '新会话',scope:'full'};
    event.dataTransfer.effectAllowed = canMove ? 'copyMove' : 'copy';
    event.dataTransfer.setData(REFERENCE_MIME, JSON.stringify(reference));
    event.dataTransfer.setData('text/plain', referenceToken(reference));
  } else {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', rowId(row));
  }
}
function dropIntent(event, target) {
  if (!isDirectoryView.value) return null;
  const rect = event.currentTarget.getBoundingClientRect();
  const ratio = (event.clientY - rect.top) / Math.max(1, rect.height);
  return resolveTreeDrop(drag.value.row, target, ratio, everyKnownNode());
}
function clearDropTarget() {
  if (dragExpandTimer) clearTimeout(dragExpandTimer);
  dragExpandTimer = null; dragExpandTarget = "";
  drag.value.target = null; drag.value.zone = "";
}
function dragOver(event, target) {
  event.stopPropagation();
  const intent = moveInFlight.value ? null : dropIntent(event, target);
  if (!intent) {
    clearDropTarget();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "none";
    return;
  }
  event.preventDefault();
  drag.value.target = target;
  drag.value.zone = intent.zone;
  if (intent.zone === "inside" && target.kind === "folder" && !isExpanded(target.folderId)) {
    if (dragExpandTarget !== target.folderId) {
      if (dragExpandTimer) clearTimeout(dragExpandTimer);
      dragExpandTarget = target.folderId;
      dragExpandTimer = setTimeout(() => {
        if (drag.value.target?.folderId === target.folderId && drag.value.zone === "inside") void toggleRow(target);
      }, 520);
    }
  } else if (dragExpandTimer) {
    clearTimeout(dragExpandTimer); dragExpandTimer = null; dragExpandTarget = "";
  }
  const list = listRef.value;
  if (list) {
    const bounds = list.getBoundingClientRect();
    if (event.clientY < bounds.top + 34) list.scrollTop -= 10;
    else if (event.clientY > bounds.bottom - 34) list.scrollTop += 10;
  }
  event.dataTransfer.dropEffect = "move";
}
async function drop(event, target) {
  event.preventDefault(); event.stopPropagation();
  const source = drag.value.row;
  // Recompute from the actual release location, never from a previous hover.
  const intent = moveInFlight.value ? null : dropIntent(event, target);
  if (!source || !intent) return clearDrag();
  clearDropTarget();
  drag.value.busy = true;
  try {
    await moveTreeItem(source, intent.targetFolderId, intent.beforeId, intent.afterId);
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { clearDrag(); }
}
function clearDrag() {
  clearDropTarget();
  drag.value = { row: null, target: null, zone: "", busy: false };
}
function rowKeydown(event, row) {
  if (selecting.value && selectableRow(row) && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); toggleSelected(row, event); return; }
  if (selecting.value && event.key === "Escape" && !menu.value.open) { exitSelection(); return; }
  if (!row.recentAlias && row.expandId && (event.key === "ArrowRight" || event.key === "ArrowLeft") && (event.key === "ArrowRight") !== isExpanded(row.expandId)) { event.preventDefault(); void toggleRow(row); return; }
  if (event.key === "Enter") { event.preventDefault(); void (row.search ? locateAndOpen(row) : activateRow(row)); }
  else if (event.key === "ArrowRight" && ["folder", "system"].includes(row.kind) && !isExpanded(row.kind === "system" ? row.id : row.folderId)) { event.preventDefault(); void toggleRow(row); }
  else if (event.key === "ArrowLeft" && ["folder", "system"].includes(row.kind) && isExpanded(row.kind === "system" ? row.id : row.folderId)) { event.preventDefault(); void toggleRow(row); }
  else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    openMenu({ preventDefault(){}, stopPropagation(){}, clientX: rect.right - 8, clientY: rect.top + 8 }, row);
  }
  else if (event.key === "Escape") { closeMenu(); clearDrag(); }
}
function globalKeydown(event) { if (event.key === "Escape") { closeMenu(); clearDrag(); if (impactDialog.value) finishImpact(null); } }

watch(() => props.activeConversationUuid, closeOverview);
watch(query, closeOverview);
watch(() => [...displayRows.value, ...activityItems.value, ...recentItems.value], rows => {
  if (overview.value.open && !rows.some(row => row.conversationUuid === overview.value.row?.conversationUuid)) closeOverview();
});
watch(() => referenceCatalog.treeStatus, data => { if (data) { statusRequestGeneration += 1; applyStatus(data); } });
watch(() => referenceCatalog.activityReadVersions, () => { if (latestStatusState) applyStatus(latestStatusState.raw); });
watch(() => [referenceCatalog.connected, referenceCatalog.ready], scheduleStatus);
onMounted(async () => {
  recentClockTimer = window.setInterval(() => { recentClock.value = Date.now(); }, 60000);
  window.addEventListener("keydown", globalKeydown);
  window.addEventListener("blur", closeOverview);
  await refreshTree({ preserve: false });
  scheduleStatus();
});
onBeforeUnmount(() => {
  window.clearInterval(recentClockTimer);
  closeOverview();
  window.removeEventListener("blur", closeOverview);
  window.removeEventListener("keydown", globalKeydown);
  invalidateSearch();
  if (statusTimer) clearTimeout(statusTimer);
  if (dragExpandTimer) clearTimeout(dragExpandTimer);
  if (impactState.resolve) finishImpact(null);
});
</script>

<template>
  <section class="conversation-tree" aria-label="会话导航" @contextmenu.self="openRootMenu">
    <header class="tree-toolbar">
      <div class="tree-title"><el-icon><ChatLineRound /></el-icon><strong>会话</strong><span>{{ activeCount }}</span></div>
      <div class="tree-tools">
        <el-dropdown trigger="click" placement="bottom-end">
          <button class="tree-tool primary" type="button" title="新建会话或目录" aria-label="新建会话或目录"><el-icon><Plus /></el-icon></button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="selectedFolderId" @click="emit('new-conversation', selectedFolderId)"><el-icon><ChatLineRound /></el-icon>新建会话 · {{ selectedTargetLabel }}</el-dropdown-item>
              <el-dropdown-item @click="promptFolder(selectedFolderId)"><el-icon><FolderAdd /></el-icon>{{ selectedFolderId ? `新建目录 · ${selectedTargetLabel}` : '新建根级目录' }}</el-dropdown-item>
              <el-dropdown-item divided @click="emit('new-conversation', '')"><el-icon><ChatLineRound /></el-icon>新建临时会话</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <button class="tree-tool" :class="{ active: selecting }" type="button" :title="selecting ? '退出多选' : '多选会话'" :aria-label="selecting ? '退出多选' : '多选会话'" :aria-pressed="selecting" @click="selecting ? exitSelection() : enterSelection()"><el-icon><Finished /></el-icon></button>
        <button class="tree-tool" type="button" title="刷新目录树" :disabled="loading" @click="refreshTree({ preserve: true })"><el-icon :class="loading && 'is-spinning'"><Refresh /></el-icon></button>
      </div>
    </header>
    <div class="sidebar-activity" role="group" aria-label="会话动态">
      <button class="activity-cell recent-waiting-count" :class="{'is-empty': !recentWaitingCount}" type="button" :aria-label="`查看待处理会话，共 ${recentWaitingCount} 个`" @click="emit('show-pending')">待处理 {{ recentWaitingCount }}</button>
      <div class="activity-cell sidebar-unread-cell" :class="{'is-empty': !recentUnreadCount}">
        <button class="sidebar-unread-count" type="button" :aria-label="`查看最近会话动态，未读 ${recentUnreadCount} 个`" @click="showRecentActivity">未读 {{ recentUnreadCount }}</button>
        <button v-if="recentUnreadCount" class="recent-read-all" type="button" :disabled="activityReadBusy" aria-label="全部标为已读" title="全部标为已读" @click="markActivityRead(recentUnreadRows)"><el-icon><Check /></el-icon></button>
      </div>
      <button class="activity-cell sidebar-running-count" :class="{'is-empty': !recentRunningCount}" type="button" :aria-label="`查看最近会话动态，运行中 ${recentRunningCount} 个`" @click="showRecentActivity">运行 {{ recentRunningCount }}</button>
    </div>
    <div class="sidebar-tabs" role="tablist" aria-label="会话视图" @keydown="sidebarTabKeydown">
      <button id="conversation-recent-tab" data-sidebar-view="recent" type="button" role="tab" aria-controls="conversation-sidebar-panel" :aria-selected="sidebarView === 'recent'" :tabindex="sidebarView === 'recent' ? 0 : -1" @click="switchSidebarView('recent')">最近</button>
      <button id="conversation-folders-tab" data-sidebar-view="folders" type="button" role="tab" aria-controls="conversation-sidebar-panel" :aria-selected="sidebarView === 'folders'" :tabindex="sidebarView === 'folders' ? 0 : -1" @click="switchSidebarView('folders')">目录</button>
    </div>
    <div class="tree-search">
      <el-icon :class="{ 'is-spinning': searchLoading }"><component :is="searchLoading ? Loading : Search" /></el-icon>
      <input v-model="query" aria-label="搜索目录或会话" placeholder="搜索目录或会话…" />
      <button v-if="archiveUnlocked" type="button" :class="{ active: searchArchived }" :aria-pressed="searchArchived" title="搜索已归档会话" @click="searchArchived = !searchArchived"><el-icon><Box /></el-icon></button>
      <button v-if="query" type="button" title="清空搜索" @click="query = ''">×</button>
    </div>

    <div id="conversation-sidebar-panel" class="sidebar-panel" role="tabpanel" :aria-labelledby="`conversation-${sidebarView}-tab`">
    <div ref="listRef" class="tree-list" :class="{ 'drop-root': drag.target?.kind === 'root', 'recent-list': sidebarView === 'recent' && !query.trim() }" :role="isDirectoryView ? 'tree' : 'list'" :aria-label="query.trim() ? '搜索结果' : isDirectoryView ? '会话和目录' : '最近会话'"
      @contextmenu.prevent.stop="openRootMenu" @dragover.self="dragOver($event, rootDropTarget)" @drop.self="drop($event, rootDropTarget)"
      @dragleave.self="clearDropTarget" @scroll.passive="closeOverview">
      <div v-if="loading && !initialized" class="tree-placeholder"><el-icon class="is-spinning"><Loading /></el-icon> 正在定位最近会话…</div>
      <template v-else>
        <div
          v-for="row in displayRows" :key="`${row.recentAlias ? 'recent:' : ''}${row.kind}:${row.id || rowId(row)}`"
          :data-tree-id="row.recentAlias ? `recent:${row.conversationUuid}` : row.id || rowId(row)" :data-kind="row.kind"
          class="tree-row-wrap" :class="[`kind-${row.kind}`, { 'is-chat-active-row': row.kind === 'conversation' && activeConversationRow === row, 'drop-inside': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'inside', 'drop-before': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'before', 'drop-after': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'after' }]"
          :style="{ '--indent': indentation(row.depth) }"
          :role="isDirectoryView ? 'treeitem' : 'listitem'" :aria-level="isDirectoryView ? Number(row.depth || 0) + 1 : undefined" :aria-busy="rowLoading(row) || isTitleGenerating(row)"
          :aria-expanded="['folder','system'].includes(row.kind) ? String(isExpanded(row.kind === 'system' ? row.id : row.folderId)) : !row.recentAlias && row.expandId ? String(isExpanded(row.expandId)) : undefined"
          :tabindex="['folder','conversation','system','group'].includes(row.kind) ? 0 : -1"
          :draggable="!selecting && !row.local && !moveInFlight && !row.groupMember && (row.kind === 'conversation' || (row.kind === 'folder' && !query && !row.archived))"
          @keydown="rowKeydown($event, row)" @contextmenu="openMenu($event, row)"
          @pointerenter="enterOverview($event, row)" @pointerleave="leaveOverview"
          @dragstart="dragStart($event, row)" @dragover="dragOver($event, row)" @drop="drop($event, row)" @dragend="clearDrag"
        >
          <template v-if="['folder','system'].includes(row.kind)">
            <button class="tree-chevron" type="button" :aria-label="isExpanded(row.kind === 'system' ? row.id : row.folderId) ? '折叠' : '展开'" @click.stop="toggleRow(row)">
              <el-icon><component :is="isExpanded(row.kind === 'system' ? row.id : row.folderId) ? ArrowDown : ArrowRight" /></el-icon>
            </button>
            <button class="tree-node-main" type="button" :class="{ 'is-folder-target': (row.kind === 'folder' && selectedFolderId === row.folderId) || (row.kind === 'system' && row.systemNode === 'temporary' && selectedFolderId === '') }" :title="row.path || row.name" @click="row.search ? locateAndOpen(row) : activateRow(row)" @contextmenu="openMenu($event, row)">
              <el-icon class="node-icon" :class="{ 'is-spinning': rowLoading(row) }"><component :is="rowLoading(row) ? Loading : row.kind === 'system' ? (row.systemNode === 'archive' ? Box : ChatLineRound) : (isExpanded(row.folderId) ? FolderOpened : Folder)" /></el-icon>
              <span class="node-label">{{ row.name }}</span>
              <span v-if="row.pinned" class="node-star" title="同级置顶"><el-icon><StarFilled /></el-icon></span>
              <span v-if="folderWaitingCount(row)" class="tree-waiting-state" :aria-label="`${folderWaitingCount(row)} 个会话待处理`">待处理 {{ folderWaitingCount(row) }}</span>
              <span v-if="collapsedFolderActivity(row)?.running" class="running-leaf" role="img" :aria-label="`${collapsedFolderActivity(row).running} 个会话运行中`" :title="`${collapsedFolderActivity(row).running} 个会话运行中`"></span>
              <span v-if="collapsedFolderActivity(row)?.unread" class="conversation-unread-dot" role="img" :aria-label="`${collapsedFolderActivity(row).unread} 个会话有未读结果`" :title="`${collapsedFolderActivity(row).unread} 个会话有未读结果`"></span>
              <span class="node-count" :aria-label="`${row.kind === 'system' ? row.count : Number(row.conversationCount || 0)} 个会话`">{{ row.kind === 'system' ? row.count : Number(row.conversationCount || 0) }}</span>
            </button>
          </template>

          <template v-else-if="row.kind === 'group'">
            <button class="tree-chevron" type="button" :aria-label="isExpanded(row.expandId) ? '折叠小组' : '展开小组'" @click.stop="toggleRow(row)">
              <el-icon><component :is="isExpanded(row.expandId) ? ArrowDown : ArrowRight" /></el-icon>
            </button>
            <button class="tree-node-main group-node" type="button" :title="row.description || row.name" @click="activateRow(row)" @contextmenu="openMenu($event, row)">
              <el-icon class="node-icon" :class="{ 'is-working': row.runningCount > 0 }"><UserFilled /></el-icon>
              <span class="node-label">{{ row.name }}</span>
              <span v-if="row.runningCount" class="group-running-state" role="status" :aria-label="`${row.runningCount} 位成员运行中`">运行中 {{ row.runningCount }}/{{ row.memberCount }}</span>
              <span v-else class="node-count" :aria-label="`${row.memberCount} 位成员`">{{ row.memberCount }}</span>
            </button>
          </template>

          <template v-else-if="row.kind === 'conversation'">
            <button v-if="selecting && selectableRow(row)" class="tree-check" :class="{ checked: isSelected(row) }" type="button" role="checkbox" :aria-checked="String(isSelected(row))" :aria-label="`选择 ${liveConversationTitle(row)}`" tabindex="-1" @click.stop="toggleSelected(row, $event)"><span><el-icon v-if="isSelected(row)"><Check /></el-icon></span></button>
            <button v-if="row.groupCount && !row.recentAlias && !row.search" class="tree-chevron" type="button" :aria-label="isExpanded(row.expandId) ? '收起小组' : '展开小组'" @click.stop="toggleRow(row)">
              <el-icon><component :is="isExpanded(row.expandId) ? ArrowDown : ArrowRight" /></el-icon>
            </button>
            <span v-else-if="!row.recentAlias && !(selecting && selectableRow(row))" class="tree-leaf-spacer"></span>
            <button class="tree-node-main conversation" type="button" :class="{ 'is-chat-active': activeConversationRow === row, 'is-title-generating': isTitleGenerating(row) }" :title="isTitleGenerating(row) ? '正在生成会话名称' : (row.local ? row.title : undefined)" @click="clickConversation($event, row)" @contextmenu="openMenu($event, row)">
              <el-icon class="node-icon" :class="{ 'is-spinning': rowLoading(row) || isTitleGenerating(row), 'is-working': running(row) && !isActivityWaiting(row) && !rowLoading(row) && !isTitleGenerating(row) }" :title="conversationOrigin(row).label" :aria-label="conversationOrigin(row).label"><component :is="rowLoading(row) || isTitleGenerating(row) ? Loading : conversationOrigin(row).icon" /></el-icon>
              <span class="node-copy"><span class="node-label"><AnimatedConversationTitle :text="liveConversationTitle(row)" :identity="row.conversationUuid" /></span><small v-if="row.search || row.recentAlias">{{ nodePath(row) }}</small></span>
              <span v-if="row.pinned" class="node-star"><el-icon><StarFilled /></el-icon></span>
              <span v-if="row.groupRunning && !running(row)" class="group-running-state" role="status" :title="`小组成员运行中 ${row.groupRunning}/${row.groupMembers}`" :aria-label="`小组成员运行中 ${row.groupRunning}/${row.groupMembers}`">小组运行 {{ row.groupRunning }}/{{ row.groupMembers }}</span>
              <span v-if="isActivityWaiting(row)" class="tree-waiting-state" role="status">{{ activityLabel(row) }}</span>
              <span v-else-if="isTitleGenerating(row)" class="title-generating-state" role="status" aria-live="polite">命名中</span>
              <span v-else-if="running(row)" class="running-leaf" role="img" :aria-label="`${activityLabel(row)}${row.activityUnread ? ' · 未读' : ''}`" :title="`${row.currentStatus || activityLabel(row)}${row.activityUnread ? ' · 未读' : ''}`"></span>
              <span v-else-if="row.activityUnread" class="conversation-unread-dot" aria-label="未读" :title="`${activityLabel(row)} · 未读`"></span>
              <span v-if="row.recentAlias && !isTitleGenerating(row) && !isActivityWaiting(row)" class="recent-row-status" :class="{'is-waiting': row.activityPending?.length}">{{ recentLabel(row) }}</span>
            </button>
          </template>

          <button v-else-if="row.kind === 'more'" class="tree-inline-action" type="button" :disabled="stateFor(row.parentId || '', row.systemNode || '').loading" @click="loadChildren(row.parentId || '', row.systemNode || '', { append: true })">加载更多</button>
          <button v-else-if="row.kind === 'error'" class="tree-inline-action error" type="button" @click="loadChildren(row.parentId || '', row.systemNode || '', { force: true })">加载失败，点击重试</button>
          <span v-else class="tree-muted">此目录为空</span>
          <button v-if="!selecting && ['folder', 'conversation', 'system', 'group'].includes(row.kind)"
            class="tree-touch-more" type="button" :aria-label="`${row.name || rowLabel(row)}：更多操作`"
            aria-haspopup="menu" :aria-expanded="menu.open && rowId(menu.row) === rowId(row)"
            @pointerdown.stop @keydown="moreMenuKeydown" @dragstart.prevent.stop @click.stop="openMoreMenu($event, row)">
            <el-icon><MoreFilled /></el-icon>
          </button>
        </div>
        <button v-if="query && searchHasMore" class="search-more" type="button" :disabled="searchLoading" @click="runSearch({ append: true })">{{ searchLoading ? '加载中…' : '更多搜索结果' }}</button>
        <div v-else-if="query && !searchLoading && !searchRows.length" class="tree-placeholder">没有匹配的目录或会话</div>
        <div v-else-if="!query.trim() && sidebarView === 'recent' && !displayRows.length" class="tree-placeholder">暂无最近会话</div>
      </template>
    </div>
    </div>
    <ConversationSelectionBar v-if="selecting" :count="selection.size" :unread-count="selectedRows.filter(row => row.activityUnread).length" :scope-label="selectionScope === null ? '' : selectedTargetLabel" :unarchive="selectionUnarchive" :busy="selectionBusy || activityReadBusy"
      @select-scope="selectScope()" @select-automatic="selectScope(true)" @clear="selection = new Map()" @done="exitSelection"
      @read="markActivityRead(selectedRows.filter(row => row.activityUnread))" @move="openBatchMove" @archive="batchArchive" @delete="batchDelete" />

    <ConversationOverview :open="overview.open" :row="overview.row" :anchor="overview.anchor" @close="closeOverview" @enter="keepOverview" @leave="leaveOverview"/>

    <Teleport to="body">
      <!-- Keep the shield until click completes so a touch cannot reopen the trigger beneath it. -->
      <div v-if="menu.open" class="tree-menu-shield" @pointerdown.self.stop @click.self.prevent.stop="closeMenu" @contextmenu.prevent.self.stop="closeMenu">
        <div data-conversation-tree-menu class="tree-context-menu" :style="{ left: `${menu.x}px`, top: `${menu.y}px` }" role="menu" :aria-label="menu.row?.kind === 'folder' ? `${menu.row.name}目录菜单` : menu.row?.kind === 'root' ? '会话列表菜单' : menu.row?.kind === 'system' ? `${menu.row.name}菜单` : `${menu.row?.title || '会话'}菜单`" @pointerdown.stop @click.stop @contextmenu.prevent.stop>
          <template v-if="menu.row?.kind === 'folder'">
            <button role="menuitem" @click="runMenuAction('new-conversation')"><el-icon><ChatLineRound /></el-icon><span>在此新建会话</span></button>
            <button role="menuitem" @click="runMenuAction('new-folder')"><el-icon><FolderAdd /></el-icon><span>新建子目录</span></button>
            <hr />
            <button role="menuitem" @click="runMenuAction('rename')"><el-icon><EditPen /></el-icon><span>重命名</span></button>
            <button role="menuitem" @click="runMenuAction('pin')"><el-icon><component :is="menu.row?.pinned ? Star : StarFilled" /></el-icon><span>{{ menu.row?.pinned ? '取消置顶' : '置顶' }}</span></button>
            <button role="menuitem" @click="runMenuAction('move')"><el-icon><FolderOpened /></el-icon><span>移动到…</span></button>
            <button role="menuitem" @click="runMenuAction('select')"><el-icon><Finished /></el-icon><span>多选会话</span></button>
            <button role="menuitem" @click="runMenuAction('cron')"><el-icon><Timer /></el-icon><span>定时任务</span></button>
            <button role="menuitem" @click="runMenuAction('properties')"><el-icon><InfoFilled /></el-icon><span>目录属性</span></button>
            <hr />
            <button class="danger" role="menuitem" @click="runMenuAction('delete-folder')"><el-icon><Delete /></el-icon><span>删除目录</span></button>
          </template>
          <template v-else-if="['system', 'root'].includes(menu.row?.kind)">
            <button v-if="menu.row?.systemNode === 'temporary' || menu.row?.kind === 'root'" role="menuitem" @click="runMenuAction('new-conversation')"><el-icon><ChatLineRound /></el-icon><span>新建临时会话</span></button>
            <button role="menuitem" @click="runMenuAction('new-folder')"><el-icon><FolderAdd /></el-icon><span>新建根级目录</span></button>
            <button v-if="menu.row?.systemNode === 'temporary'" role="menuitem" @click="runMenuAction('select')"><el-icon><Finished /></el-icon><span>多选会话</span></button>
            <template v-if="menu.row?.systemNode === 'temporary'">
              <hr />
              <button role="menuitem" @click="runMenuAction('properties')"><el-icon><InfoFilled /></el-icon><span>临时会话属性</span></button>
            </template>
          </template>
          <template v-else-if="menu.row?.kind === 'group'">
            <button role="menuitem" @click="runMenuAction('properties')"><el-icon><InfoFilled /></el-icon><span>小组属性</span></button>
          </template>
          <template v-else>
            <button role="menuitem" @click="runMenuAction('new-sibling')"><el-icon><ChatLineRound /></el-icon><span>新建同级会话</span></button>
            <button v-if="menu.row?.activityUnread" role="menuitem" :disabled="activityReadBusy" @click="markActivityRead([menu.row]); closeMenu()"><el-icon><Check /></el-icon><span>标为已读</span></button>
            <button role="menuitem" :disabled="menu.row?.local || running(menu.row) || !hasConversationMessages(menu.row) || isTitleGenerating(menu.row)" @click="runMenuAction('generate-title')"><el-icon :class="{'is-spinning': isTitleGenerating(menu.row)}"><component :is="isTitleGenerating(menu.row) ? Loading : MagicStick" /></el-icon><span>{{ isTitleGenerating(menu.row) ? '正在生成…' : '生成会话名称' }}</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('rename')"><el-icon><EditPen /></el-icon><span>重命名</span></button>
            <button role="menuitem" :disabled="menu.row?.local || running(menu.row)" @click="runMenuAction('duplicate')"><el-icon><DocumentCopy /></el-icon><span>复制会话</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('pin')"><el-icon><component :is="menu.row?.pinned ? Star : StarFilled" /></el-icon><span>{{ menu.row?.pinned ? '取消置顶' : '置顶' }}</span></button>
            <hr />
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('select')"><el-icon><Finished /></el-icon><span>多选</span></button>
            <button v-if="menu.recent" role="menuitem" :disabled="locatingConversation" @click="runMenuAction('locate')"><el-icon><Aim /></el-icon><span>在目录中定位</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('move')"><el-icon><FolderOpened /></el-icon><span>移动到…</span></button>
            <button role="menuitem" :disabled="menu.row?.local || running(menu.row)" @click="runMenuAction('refresh-prompt')"><el-icon><Refresh /></el-icon><span>更新系统提示词…</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('archive')"><el-icon><component :is="menu.row?.archived ? RefreshLeft : Box" /></el-icon><span>{{ menu.row?.archived ? '取消归档' : '归档' }}</span></button>
            <button role="menuitem" @click="runMenuAction('conversation-properties')"><el-icon><InfoFilled /></el-icon><span>会话属性</span></button>
            <hr />
            <button class="danger" role="menuitem" :disabled="running(menu.row)" @click="runMenuAction('delete-conversation')"><el-icon><Delete /></el-icon><span>删除会话</span></button>
          </template>
        </div>
      </div>
    </Teleport>

    <ConversationPromptDialog v-model="promptDialog" :conversation="promptRow" />

    <el-dialog v-model="propertiesDialog" :title="propertiesForm.temporary ? '临时会话属性' : '目录属性'" width="min(820px, calc(100vw - 24px))" append-to-body class="folder-properties-dialog mobile-viewport-dialog" :class="{'is-trigger': propertiesTab === 'trigger'}" :before-close="closeProperties" :close-on-click-modal="false" :close-on-press-escape="!propertiesSaving" :show-close="!propertiesSaving && !webhookEditor?.saving">
      <template #header>
        <div class="folder-properties-heading">
          <h2 :title="propertiesForm.path">{{ propertiesForm.name || (propertiesForm.temporary ? '临时会话属性' : '目录属性') }}</h2>
        </div>
        <nav class="property-segmented" role="tablist" aria-label="属性设置分类" @keydown="navigatePropertiesTab">
          <button id="folder-properties-tab-context" type="button" role="tab" data-tab="context" :aria-selected="propertiesTab === 'context'" aria-controls="folder-properties-panel-context" :tabindex="propertiesTab === 'context' ? 0 : -1" @click="propertiesTab = 'context'">{{ propertiesForm.temporary ? '注入上下文' : '目录上下文' }}</button>
          <button id="folder-properties-tab-defaults" type="button" role="tab" data-tab="defaults" :aria-selected="propertiesTab === 'defaults'" aria-controls="folder-properties-panel-defaults" :tabindex="propertiesTab === 'defaults' ? 0 : -1" @click="propertiesTab = 'defaults'">默认模型</button>
          <button v-if="!propertiesForm.temporary" id="folder-properties-tab-trigger" type="button" role="tab" data-tab="trigger" :aria-selected="propertiesTab === 'trigger'" aria-controls="folder-properties-panel-trigger" :tabindex="propertiesTab === 'trigger' ? 0 : -1" @click="propertiesTab = 'trigger'">触发器</button>
        </nav>
      </template>
      <div v-loading="propertiesLoading" class="property-form">
        <div class="property-panels">
          <section v-show="propertiesTab === 'context'" id="folder-properties-panel-context" role="tabpanel" aria-labelledby="folder-properties-tab-context">
            <div class="property-tab-panel context-properties">
              <section v-if="!propertiesForm.temporary" class="property-section">
                <label class="property-field">
                  <span>不显示在最近会话</span>
                  <el-switch v-model="propertiesForm.hideFromRecent" aria-label="不显示在最近会话" :disabled="propertiesSaving" />
                  <small>此目录及其子目录的会话不显示在最近会话，运行和未读状态不受影响。<template v-if="propertiesForm.hiddenFromRecentByAncestor">上级目录已启用隐藏。</template></small>
                </label>
              </section>
              <section v-if="!propertiesForm.temporary" class="property-section" aria-labelledby="folder-workspace-label">
                <label class="property-field">
                  <span id="folder-workspace-label">本节点工作目录 <em>清空即继承</em></span>
                  <el-input v-model="propertiesForm.workspaceDir" aria-labelledby="folder-workspace-label" placeholder="例如 /home/user/projects/my-project" clearable />
                </label>

              </section>
              <section class="property-section" aria-label="提示词配置">
                <PromptPolicyEditor v-model="folderPromptPolicy" :disabled="propertiesLoading" :folder-id="propertiesForm.folderId" :inherited-policy="propertiesForm.inheritedPromptPolicy" :inherited-source-path="propertiesForm.inheritedPromptSourcePath" />
              </section>

            </div>
          </section>
          <section v-show="propertiesTab === 'defaults'" id="folder-properties-panel-defaults" role="tabpanel" aria-labelledby="folder-properties-tab-defaults">
            <div class="property-tab-panel run-defaults-panel">
              <el-alert v-if="!propertiesLoading && !propertyModelsLoaded" title="模型选项暂不可用，运行默认保持只读" type="warning" :closable="false" show-icon />
              <ModelSettings :model-value="propertiesForm.runDefaults" :effective="appliedRunDefaults" :inherited="inheritedRunDefaults" :models="propertyModelOptions" inheritable :inherit-label="propertiesForm.temporary ? '使用系统默认' : '继承目录'" :disabled="propertiesLoading || !propertyModelsLoaded || propertiesSaving" @change="setRunDefault" />
            </div>
          </section>
          <section v-if="webhookVisited && propertiesDialog && !propertiesForm.temporary" v-show="propertiesTab === 'trigger'" id="folder-properties-panel-trigger" role="tabpanel" aria-labelledby="folder-properties-tab-trigger" class="wh-fill">
            <WebhookEditor ref="webhookEditor" :scope="{type:'folder', id:propertiesForm.folderId}" :title="propertiesForm.name" :active="propertiesTab === 'trigger'" @cancel="closeProperties" />
          </section>
        </div>
      </div>
      <template #footer>
        <div class="property-footer">
          <div class="property-footer-meta" v-if="propertiesTab === 'defaults'">
            <span class="property-footer-note" :title="propertiesForm.temporary ? '每项独立设置，未设置时使用原有默认；只影响以后新建的临时会话，不影响项目目录。' : '每项独立向上继承；跟随主模型、模型默认和跟随主会话都是明确设置，不等于继承。仅影响以后新建的会话，不会批量更改已有会话。'"><el-icon><InfoFilled /></el-icon>仅新会话生效</span>
            <el-button link :disabled="!hasLocalRunDefaults || propertiesLoading || !propertyModelsLoaded || propertiesSaving" @click="clearRunDefaults">{{ propertiesForm.temporary ? '全部使用原有默认' : '全部恢复继承' }}</el-button>
          </div>
          <div class="property-footer-buttons"><el-button :disabled="propertiesSaving" @click="closeProperties">取消</el-button><el-button type="primary" :loading="propertiesSaving" :disabled="propertiesLoading" @click="saveProperties">保存</el-button></div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="moveDialog" class="mobile-viewport-dialog" width="min(560px, calc(100vw - 24px))" append-to-body :title="moveMode === 'delete' ? '迁移目录内容后删除' : moveMode === 'batch' ? `移动 ${selection.size} 个会话到…` : '移动到…'" :close-on-click-modal="false">
      <el-input v-model="moveFolderQuery" clearable placeholder="搜索完整目录路径" :prefix-icon="Search" />
      <div class="folder-picker" role="listbox">
        <button type="button" :class="{ selected: moveFolderId === '' }" @click="moveFolderId = ''"><el-icon><Folder /></el-icon><span>{{ moveRow?.kind === 'conversation' ? '临时会话' : '根级目录' }}</span></button>
        <button v-for="folder in filteredMoveFolders" :key="folder.folderId" type="button" :disabled="invalidMoveTarget(folder)" :class="{ selected: moveFolderId === folder.folderId }" @click="moveFolderId = folder.folderId"><el-icon><Folder /></el-icon><span>{{ folder.path }}</span></button>
      </div>
      <div v-if="moveMode === 'move' && moveRow?.kind === 'conversation' && moveRow?.archived" class="move-archive-options">
        <div><el-checkbox v-model="moveUnarchive">取消归档</el-checkbox><p>移动后恢复为普通会话。</p></div>
        <div><el-checkbox v-model="moveUpdateSnapshots">更新系统提示词</el-checkbox><p>按目标目录和当前模板重新组装，工具定义同步为当前版本，现有提示词缓存将失效；运行中的会话跳过更新。</p></div>
      </div>
      <template #footer><el-button @click="moveDialog = false">取消</el-button><el-button type="primary" :loading="moveBusy" @click="submitMove">{{ moveMode === 'delete' ? '下一步' : '移动' }}</el-button></template>
    </el-dialog>

    <GroupPropertiesDialog v-model="groupProperties.open" :group-id="groupProperties.groupId" />

    <PromptImpactDialog :model-value="impactDialog" :action="impactState.action" :impact="impactState.impact" @choose="finishImpact" />
  </section>
</template>

<style scoped>
.folder-context-strategy { padding: 16px; margin-top: 16px; background: var(--ob-surface-soft); border: 1px solid var(--ob-border); border-radius: 12px; }
.folder-context-strategy-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
.folder-context-strategy-head strong { font-size:14px; font-weight:500; }
.folder-context-strategy-head span { font-size:13px; color:var(--ob-text-subtle); }

.conversation-tree { display:flex; min-height:0; flex:1; flex-direction:column; color:var(--ob-chat-text); font-family:inherit; }
.conversation-tree button,.conversation-tree input { font-family:inherit; }
.tree-list.drop-root { background:rgb(var(--ob-blue-rgb) / .045); outline:1px dashed rgb(var(--ob-blue-rgb) / .4); outline-offset:-3px; }
.tree-toolbar { display:flex; align-items:center; justify-content:space-between; padding:0 11px 6px 13px; }
.tree-title { display:flex; align-items:baseline; gap:6px; font-size:12px; color:var(--ob-text-subtle); }
.tree-title > .el-icon { align-self:center; }
.tree-title span { font-size:10px; font-weight:500; color:var(--ob-text-muted); }
.tree-tools { display:flex; gap:3px; }
.tree-tool { display:grid; width:27px; height:27px; place-items:center; border:0; border-radius:8px; background:transparent; color:var(--ob-text-subtle); cursor:pointer; }
.tree-tool:hover,.tree-tool:focus-visible { background:var(--ob-surface-soft); color:var(--ob-text-strong); outline:none; }
.tree-tool.primary { color:var(--ob-chat-subtle); }
.tree-tool.active { background:var(--ob-hover); color:var(--ob-text-strong); }
.tree-check { display:grid; width:24px; height:27px; flex:0 0 auto; place-items:center; padding:0; border:0; background:transparent; cursor:pointer; }
.tree-check > span { display:grid; width:14px; height:14px; place-items:center; border:1.5px solid var(--ob-border-strong); border-radius:4px; color:var(--ob-text-inverse); font-size:10px; }
.tree-check.checked > span { border-color:var(--ob-blue); background:var(--ob-blue); }
.sidebar-tabs { display:flex; flex:none; gap:3px; margin:0 10px 8px; padding:3px; border-radius:8px; background:var(--ob-hover); }
.sidebar-tabs button { flex:1; min-width:0; min-height:28px; padding:3px 8px; border:0; border-radius:6px; background:transparent; color:var(--ob-text-muted); font-size:12px; cursor:pointer; }
.sidebar-tabs button[aria-selected="true"] { background:var(--ob-surface-raised); color:var(--ob-text); box-shadow:0 1px 3px rgb(0 0 0 / .06); }
.sidebar-tabs button:focus-visible { outline:2px solid var(--ob-blue); outline-offset:-2px; }
.sidebar-activity { display:grid; flex:none; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:2px; height:27px; align-items:stretch; margin:0 11px 5px; }
.activity-cell { display:flex; min-width:0; min-height:27px; align-items:center; justify-content:center; gap:2px; border:0; border-radius:6px; background:transparent; color:var(--ob-text-subtle); font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap; }
button.activity-cell,.sidebar-unread-count { padding:3px 6px; cursor:pointer; }
.activity-cell:hover { background:var(--ob-hover); }
.activity-cell.is-empty,.activity-cell.is-empty .sidebar-unread-count { color:var(--ob-text-muted); }
.recent-waiting-count { color:var(--ob-warning); }
.sidebar-unread-cell { position:relative; }
.sidebar-unread-count { display:flex; flex:1; height:100%; align-items:center; justify-content:center; border:0; border-radius:6px; background:transparent; color:var(--ob-blue); font:inherit; white-space:nowrap; }
.sidebar-unread-cell .recent-read-all { position:absolute; top:50%; right:3px; transform:translateY(-50%); }
.activity-cell:focus-visible,.sidebar-unread-count:focus-visible { outline:2px solid var(--ob-focus); outline-offset:-2px; }
.sidebar-panel { display:flex; flex:1; min-height:0; flex-direction:column; }
.recent-list .tree-node-main { height:auto; min-height:43px; padding:5px 7px; }
.tree-search { display:flex; align-items:center; gap:6px; margin:0 10px 7px; padding:0 9px; height:31px; border:1px solid var(--ob-border-soft); border-radius:9px; background:var(--ob-chat-hover); color:var(--ob-chat-muted); }
.tree-search:focus-within { border-color:var(--ob-border-strong); }
.tree-search input { min-width:0; flex:1; border:0; outline:0; background:transparent; color:var(--ob-text); font-size:12px; }
.tree-search button { display:grid; place-items:center; width:20px; height:20px; padding:0; border:0; border-radius:5px; background:transparent; color:var(--ob-text-muted); cursor:pointer; }
.tree-search button:hover,.tree-search button.active { background:var(--ob-surface-soft); color:var(--ob-blue); }
.tree-list { min-height:0; flex:1; overflow:auto; padding:0 7px 10px; scrollbar-width:thin; overscroll-behavior:contain; }
.tree-row-wrap { position:relative; display:flex; width:100%; min-height:30px; align-items:center; padding-left:var(--indent); border-radius:8px; outline:none; }
.tree-row-wrap:focus-visible { box-shadow:inset 0 0 0 2px rgb(var(--ob-blue-rgb) / .45); }
.tree-row-wrap.drop-inside { background:rgb(var(--ob-blue-rgb) / .13); box-shadow:inset 0 0 0 1px rgb(var(--ob-blue-rgb) / .48); }
.tree-row-wrap.drop-before::before,.tree-row-wrap.drop-after::after { content:""; position:absolute; z-index:2; left:calc(var(--indent) + 7px); right:7px; height:2px; border-radius:2px; background:var(--ob-blue); }
.tree-row-wrap.drop-before::before { top:-1px; }.tree-row-wrap.drop-after::after { bottom:-1px; }
.tree-chevron { display:grid; width:24px; height:27px; place-items:center; border:0; border-radius:7px; background:transparent; color:var(--ob-text-muted); cursor:pointer; }
.tree-chevron { flex:0 0 auto; }
.tree-chevron:hover { background:var(--ob-hover); color:var(--ob-text-subtle); }
.tree-node-main { display:flex; width:100%; min-width:0; height:29px; flex:1 1 auto; align-items:center; gap:6px; padding:0 5px; border:0; border-radius:7px; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.tree-node-main:hover { background:var(--ob-chat-hover); }
.tree-node-main.conversation { color:var(--ob-chat-subtle); }
.tree-node-main.is-folder-target { background:var(--ob-hover); color:var(--ob-text); }
.tree-node-main.is-chat-active { background:var(--ob-chat-selected); color:var(--ob-chat-text); }
.tree-node-main:focus-visible,.tree-chevron:focus-visible { outline:2px solid var(--ob-chat-subtle); outline-offset:-2px; }
.node-icon { flex:0 0 auto; color:var(--ob-text-subtle); }
/* Same rotating top/right border as the work-detail button; the glyph itself
   stays still and the absolute ring does not alter row/icon geometry. */
.node-icon.is-working { position:relative; color:var(--ob-blue); }
.node-icon.is-working::before { content:""; pointer-events:none; position:absolute; inset:-3px; border:1.5px solid transparent; border-top-color:var(--ob-blue); border-right-color:rgb(var(--ob-blue-rgb) / .42); border-radius:50%; animation:tree-work-border-spin .9s linear infinite; }
@keyframes tree-work-border-spin { to { transform:rotate(360deg); } }

.node-label { min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; font-weight:400; line-height:1.4; }
.node-copy { display:flex; min-width:0; flex:1; flex-direction:column; line-height:1.4; }
.node-copy small { overflow:hidden; margin-top:2px; color:var(--ob-text-muted); font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
.node-star { display:grid; color:var(--ob-warning); font-size:11px; }
.node-count { color:var(--ob-text-muted); font-size:10px; }
.group-running-state { flex:none; padding:1px 5px; border-radius:5px; background:rgb(var(--ob-blue-rgb) / .1); color:var(--ob-blue); font-size:10px; line-height:18px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.tree-node-main.group-node { color:var(--ob-text-subtle); }
.title-generating-state { flex:0 0 auto; padding:1px 5px; border-radius:5px; background:rgb(var(--ob-blue-rgb) / .1); color:var(--ob-blue); font-size:10px; font-weight:600; line-height:16px; white-space:nowrap; }

.running-leaf { width:6px; height:6px; flex:0 0 6px; margin-right:2px; border-radius:50%; background:var(--ob-blue); box-shadow:0 0 0 3px rgb(var(--ob-blue-rgb) / .12); animation:tree-pulse 1.8s ease-in-out infinite; }

.conversation-unread-dot { width:6px; height:6px; flex:0 0 6px; margin-right:2px; border-radius:50%; background:var(--ob-blue); }
.tree-leaf-spacer { width:24px; flex:0 0 auto; }
.tree-inline-action,.search-more { margin-left:28px; border:0; background:transparent; color:var(--ob-blue); font-size:11px; cursor:pointer; }
.tree-inline-action.error { color:var(--ob-danger); }.tree-muted { display:flex; align-items:center; gap:5px; padding-left:6px; color:var(--ob-text-muted); font-size:11px; }
.tree-placeholder { margin:8px; padding:14px 8px; border-radius:9px; background:var(--ob-surface-soft); color:var(--ob-text-muted); font-size:11px; text-align:center; }
.search-more { width:calc(100% - 24px); margin:7px 12px; padding:7px; border-radius:8px; background:rgb(var(--ob-blue-rgb) / .08); }
.is-spinning { animation:tree-spin .8s linear infinite; }
@keyframes tree-spin { to { transform:rotate(360deg); } } @keyframes tree-pulse { 50% { opacity:.35; transform:scale(.8); } }
/* Keep the running ring: it is an operational status indicator, not decoration. */
@media (prefers-reduced-motion: reduce) { .is-spinning,.running-leaf { animation:none; } }
@media (pointer:coarse) { .tree-row-wrap { min-height:36px; } .tree-node-main { height:34px; } }
.tree-touch-more { display:none; }
.recent-row-status { flex:0 1 auto; min-width:0; max-width:42%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; color:var(--ob-text-subtle); }
.recent-row-status.is-waiting { color:var(--ob-warning); }
.tree-waiting-state { flex:none; padding:1px 5px; border-radius:5px; background:rgb(var(--ob-warning-rgb) / .1); color:var(--ob-warning); font-size:10px; line-height:18px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.recent-read-all { display:grid; flex:0 0 20px; width:20px; height:20px; padding:0; place-items:center; border:0; border-radius:5px; background:transparent; color:var(--ob-text-subtle); font-size:12px; cursor:pointer; }
.recent-read-all:hover { background:var(--ob-hover); color:var(--ob-text); }
.recent-read-all:focus-visible { outline:2px solid var(--ob-blue); outline-offset:-2px; }
.recent-read-all:disabled { opacity:.4; cursor:default; }
@media (max-width:760px), (hover:none) and (pointer:coarse) {
  .sidebar-activity { height:44px; }
  .sidebar-tabs button,.activity-cell { min-height:44px; }
  .recent-read-all { width:28px; height:44px; flex-basis:28px; }
  .sidebar-unread-cell .recent-read-all { right:1px; }
  .tree-row-wrap { min-height:44px; }
  .tree-row-wrap.is-chat-active-row { background:var(--ob-chat-selected); color:var(--ob-chat-text); }
  .tree-row-wrap.is-chat-active-row .tree-node-main { background:transparent; color:inherit; }
  .tree-node-main { min-height:44px; }
  .tree-touch-more { display:grid; width:44px; height:44px; flex:0 0 44px; place-items:center; border:0; border-radius:8px; background:transparent; color:inherit; font-size:14px; }
  .tree-touch-more:focus-visible { outline:2px solid var(--ob-blue); outline-offset:-3px; }
  .tree-touch-more:active { background:var(--ob-selected); }
}
@media (max-width:760px) {
  .tree-chevron { height:44px; }
  .tree-check { width:32px; height:44px; }
  .tree-check > span { width:18px; height:18px; font-size:12px; }
}
</style>

<style>
.tree-menu-shield { position:fixed; inset:0; z-index:3200; background:transparent; }
.tree-context-menu { font-family:inherit; font-size:12px; font-weight:400; line-height:1.4; position:fixed; width:min(194px,calc(100vw - 16px)); max-height:calc(100vh - 16px); overflow:auto; padding:5px; border:1px solid rgb(var(--ob-border-rgb) / .11); border-radius:10px; background:rgb(var(--ob-surface-raised-rgb) / .97); color:var(--ob-text); box-shadow:var(--ob-shadow-popover); backdrop-filter:blur(18px) saturate(1.3); }
.tree-context-menu button { font:inherit; display:grid; grid-template-columns:16px 1fr; align-items:center; gap:7px; width:100%; min-height:28px; padding:4px 8px; border:0; border-radius:7px; background:transparent; color:inherit; text-align:left; cursor:pointer; }
@media (max-width:760px), (hover:none) and (pointer:coarse) {
  .tree-context-menu { max-height:calc(var(--mobile-viewport-height, 100dvh) - 16px); }
  .tree-context-menu button { min-height:44px; }
}
.tree-context-menu button:hover:not(:disabled) { background:var(--ob-blue); color:var(--ob-text-inverse); }.tree-context-menu button:disabled { opacity:.38; cursor:not-allowed; }.tree-context-menu button.danger { color:var(--ob-danger); }.tree-context-menu button.danger:hover:not(:disabled) { background:var(--ob-danger); color:var(--ob-text-inverse); }
.tree-context-menu hr { height:1px; margin:5px 7px; border:0; background:var(--ob-hover); }
.impact-copy { color:var(--el-text-color-secondary); font-size:12px; line-height:1.65; }
.move-archive-options { display:grid; gap:10px; margin-top:16px; padding-top:14px; border-top:1px solid var(--el-border-color-lighter); }
.move-archive-options .el-checkbox { height:auto; font-family:inherit; }
.move-archive-options .el-checkbox__label { font-size:13px; line-height:1.6; font-weight:500; }
.move-archive-options p { margin:2px 0 0 22px; font-size:12px; line-height:1.6; color:var(--el-text-color-secondary); }
.folder-picker { max-height:330px; overflow:auto; margin-top:10px; padding:5px; border:1px solid var(--el-border-color-light); border-radius:9px; }.folder-picker button { display:flex; width:100%; align-items:center; gap:7px; padding:7px 9px; border:0; border-radius:7px; background:transparent; color:var(--el-text-color-primary); text-align:left; cursor:pointer; }.folder-picker button:hover:not(:disabled),.folder-picker button.selected { background:var(--el-color-primary-light-9); color:var(--el-color-primary); }.folder-picker button:disabled { opacity:.35; cursor:not-allowed; }.folder-picker span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }

</style>
