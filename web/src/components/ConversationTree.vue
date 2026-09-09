<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
  Box, ArrowDown, ArrowRight, ChatLineRound, Delete, DocumentCopy,
  EditPen, Folder, FolderAdd, FolderOpened, InfoFilled, Loading,
  Plus, Refresh, RefreshLeft, Search, Star, StarFilled,
} from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import MdEditor from "./MdEditor.vue";
import ConversationPromptDialog from "./ConversationPromptDialog.vue";
import ConversationOverview from "./ConversationOverview.vue";
import { treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop } from "./conversationTreeInteractions.js";
import { referenceCatalog } from "../references/catalog.js";
import { REFERENCE_MIME, referenceToken } from "../references/codec.js";

const props = defineProps({
  activeConversationUuid: { type: String, default: "" },
  draftConversation: { type: Object, default: null },
});
const emit = defineEmits(["open", "new-conversation", "rows", "selected-folder", "refresh-list", "delete-conversation", "folder-removed"]);

defineExpose({
  refresh: (options = {}) => refreshTree({ preserve: true, ...options }),
  revealConversation: (conversationUuid) => refreshTree({ preserve: true, conversationUuid, reveal: true }),
  selectedFolderId: () => selectedFolderId.value,
  openRootMenu,
  revealDraft,
  forgetConversation,
});

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
const searchArchived = ref(false);
const archiveUnlocked = ref(false);
const menu = ref({ open: false, x: 0, y: 0, row: null });
const drag = ref({ row: null, target: null, zone: "", busy: false });
const moveInFlight = ref(false);
const movingRowId = ref("");
const rootDropTarget = { kind: "root", id: "__root" };
const promptDialog = ref(false);
const promptRow = ref(null);
const propertiesDialog = ref(false);
const propertiesLoading = ref(false);
const propertiesSaving = ref(false);
const propertiesForm = reactive({ folderId: "", parentId: "", name: "", path: "", workspaceDir: "", promptMarkdown: "", workspaceEffective: "", workspaceSource: "", promptEffective: "", promptSource: "" });
const moveDialog = ref(false);
const moveBusy = ref(false);
const moveMode = ref("move");
const moveRow = ref(null);
const moveFolderId = ref("");
const moveFolderQuery = ref("");
const moveUnarchive = ref(true);
const moveUpdateSnapshots = ref(true);
const allFolders = ref([]);
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
function closeOverview() {
  clearTimeout(overviewOpenTimer); clearTimeout(overviewCloseTimer);
  overview.value = { open: false, row: null, anchor: null };
}
function keepOverview() { clearTimeout(overviewOpenTimer); clearTimeout(overviewCloseTimer); }
function enterOverview(event, row) {
  if (row.kind !== 'conversation' || row.local || event.pointerType === 'touch' || !window.matchMedia('(hover: hover)').matches || drag.value.row || moveInFlight.value || menu.value.open) return;
  keepOverview();
  if (overview.value.open && overview.value.row?.conversationUuid === row.conversationUuid) return;
  closeOverview();
  const anchor = event.currentTarget.querySelector('button.conversation') || event.currentTarget;
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
function rowLabel(row) { return row?.kind === "folder" ? row.name : row?.title || "新会话"; }
function nodePath(row) { return String(row?.path || (row?.folderId ? "" : "临时会话")); }
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
    rows.unshift({ ...draft, kind: "conversation", id: draft.conversationUuid, parentId: folderId, path: folderId ? folderPath(folderId) : "临时会话" });
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
      else output.push({ ...child, depth: depth + 1 });
    }
    if (branch.error) output.push({ kind: "error", id: `error:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId, message: branch.error });
    else if ((branch.loaded || branch.pages > 0) && !withDraft(branch.items, folder.folderId).length) output.push({ kind: "empty", id: `empty:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId });
    if (branch.hasMore) output.push({ kind: "more", id: `more:${folder.folderId}`, depth: depth + 1, parentId: folder.folderId, cursor: branch.nextCursor });
  }
}
const visibleRows = computed(() => {
  const output = [];
  appendFolderRows(output, rootFolders.value, 0);
  const temporary = { kind: "system", systemNode: "temporary", id: "__temporary", name: "临时会话", count: temporaryCount.value, depth: 0 };
  output.push(temporary);
  if (isExpanded("__temporary")) {
    const branch = stateFor("", "temporary");
    for (const row of withDraft(branch.items, "")) output.push({ ...row, depth: 1, parentId: "" });
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
const displayRows = computed(() => query.value.trim() ? searchRows.value.map((row) => ({ ...row, depth: 0, search: true })) : visibleRows.value);
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
function statusAdjustedRow(row) {
  if (!latestStatusState || !row) return row;
  if (row.kind === "folder") {
    const count = latestStatusState.folderConversationCounts?.[row.folderId];
    return { ...row, runningDescendantCount: Number(latestStatusState.folderRunningCounts?.[row.folderId] || 0),
      ...(Number.isFinite(count) ? { conversationCount: count } : {}) };
  }
  if (row.kind !== "conversation" || row.archived) return row;
  const status = latestStatusState.lookup.get(row.conversationUuid);
  return status ? { ...row, ...status, status: "running", running: true } : { ...row, running: false, status: "idle" };
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
  if (!raw?.conversationUuid) return;
  const row = statusAdjustedRow(raw);
  const parentId = String(row.folderId || "");
  const system = row.archived ? "archive" : parentId ? "" : "temporary";
  const branch = stateFor(parentId, system);
  branch.items = mergeTreeRows(branch.items, [row]);
}
function updateKnownNode(id, patch) {
  const update = (rows) => rows.map((row) => rowId(row) === id ? { ...row, ...patch } : row);
  rootFolders.value = update(rootFolders.value);
  for (const branch of Object.values(branchState)) branch.items = update(branch.items || []);
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
      if (!props.activeConversationUuid && selectedItem?.conversationUuid) emit("open", selectedItem);
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
function applyStatus(data = {}) {
  const revision = Number(data.revision || 0);
  if (revision) statusServerRevision = Math.max(statusServerRevision, revision);
  latestStatusState = {
    lookup: new Map((data.items || []).map((item) => [item.conversationUuid, item])),
    folderRunningCounts: { ...(data.folderRunningCounts || {}) },
    folderConversationCounts: { ...(data.folderConversationCounts || {}) },
  };
  for (const row of everyKnownNode()) {
    if (row.kind === "conversation" && !row.archived) {
      const status = latestStatusState.lookup.get(row.conversationUuid);
      updateKnownNode(row.conversationUuid, status ? { ...status, status: "running", running: true } : { running: false, status: "idle" });
    } else if (row.kind === "folder") {
      updateKnownNode(row.folderId, statusAdjustedRow(row));
    }
  }
  searchRows.value = searchRows.value.map(row => row.kind === "folder" ? statusAdjustedRow(row) : row);
  emitRows();
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
async function activateRow(row) {
  closeOverview();
  if (row.kind === "folder") { selectFolder(row.folderId); await toggleRow(row); return; }
  if (row.kind === "system") {
    if (row.systemNode === "temporary") selectFolder("");
    await toggleRow(row);
    return;
  }
  if (row.kind !== "conversation") return;
  if (!row.archived) selectFolder(String(row.folderId || ""));
  emit("open", row);
}
async function locateAndOpen(row) {
  closeOverview();
  if (row.kind === "folder") {
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

async function runSearch({ append = false } = {}) {
  const text = query.value.trim();
  if (!text) { searchRows.value = []; searchHasMore.value = false; return; }
  const generation = ++searchGeneration;
  searchLoading.value = true;
  try {
    const data = await Api.conversationTreeSearch({ q: text, limit: 50, ...(append && searchCursor.value ? { cursor: searchCursor.value } : {}), ...(searchArchived.value && archiveUnlocked.value ? { archiveUnlocked: 1 } : {}) });
    if (generation !== searchGeneration || query.value.trim() !== text) return;
    const incoming = (data.items || []).map(row => row.kind === "folder" ? statusAdjustedRow(row) : row);
    searchRows.value = append ? [...searchRows.value, ...incoming] : incoming;
    searchCursor.value = String(data.nextCursor || "");
    searchHasMore.value = Boolean(data.hasMore);
  } catch (error) {
    if (generation === searchGeneration) ElMessage.error(apiError(error));
  } finally {
    if (generation === searchGeneration) searchLoading.value = false;
  }
}
watch(query, () => {
  searchGeneration += 1;
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(), 260);
});
watch(searchArchived, () => { if (query.value.trim()) void runSearch(); });
watch(() => props.draftConversation, () => emitRows(), { deep: true });

async function openMenu(event, row) {
  closeOverview();
  if (!["folder", "conversation", "system", "root"].includes(row?.kind)) return;
  event?.preventDefault?.(); event?.stopPropagation?.();
  const pad = 8;
  const pointerX = Math.max(pad, Number(event?.clientX || pad));
  const pointerY = Math.max(pad, Number(event?.clientY || pad));
  menu.value = { open: true, row, x: pointerX, y: pointerY };
  await nextTick();
  if (!menu.value.open || String(menu.value.row?.id || rowId(menu.value.row) || "") !== String(row.id || rowId(row) || "")) return;
  const element = document.querySelector("[data-conversation-tree-menu]");
  const bounds = element?.getBoundingClientRect?.();
  if (!bounds) return;
  menu.value = {
    ...menu.value,
    x: Math.max(pad, Math.min(pointerX, window.innerWidth - bounds.width - pad)),
    y: Math.max(pad, Math.min(pointerY, window.innerHeight - bounds.height - pad)),
  };
}
function openRootMenu(event) { return openMenu(event, rootDropTarget); }
function closeMenu() { menu.value = { open: false, x: 0, y: 0, row: null }; }
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
    const { value } = await ElMessageBox.prompt("", row.kind === "folder" ? "重命名目录" : "重命名会话", { inputValue: rowLabel(row), inputValidator: (v) => String(v || "").trim() ? true : "名称不能为空", confirmButtonText: "保存", cancelButtonText: "取消" });
    if (row.kind === "folder") await Api.updateConversationFolder(row.folderId, { name: String(value).trim() });
    else await Api.updateConversation(row.conversationUuid, { title: String(value).trim() });
    await refreshAffected(row);
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
}
async function togglePin(row) {
  if (row.kind === "folder") await Api.updateConversationFolder(row.folderId, { pinned: !row.pinned });
  else if (row.pinned) await Api.unpinConversation(row.conversationUuid); else await Api.pinConversation(row.conversationUuid);
  await refreshAffected(row);
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
async function showProperties(row) {
  propertiesDialog.value = true;
  propertiesLoading.value = true;
  try {
    const data = await Api.conversationFolderProperties(row.folderId);
    Object.assign(propertiesForm, {
      folderId: row.folderId, parentId: String(row.parentId || ""), name: data.name, path: data.path,
      workspaceDir: data.workspace?.local || "", promptMarkdown: data.prompt?.local || "",
      workspaceEffective: data.workspace?.effective || "", workspaceSource: data.workspace?.sourcePath || "",
      promptEffective: data.prompt?.effective || "", promptSource: data.prompt?.sourcePath || "",
    });
  } catch (error) { propertiesDialog.value = false; ElMessage.error(apiError(error)); }
  finally { propertiesLoading.value = false; }
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
  propertiesSaving.value = true;
  const payload = { workspaceDir: propertiesForm.workspaceDir, promptMarkdown: propertiesForm.promptMarkdown };
  try {
    const impact = await Api.conversationFolderPropertiesImpact(propertiesForm.folderId, payload);
    const choice = await chooseImpact("保存", impact);
    if (choice === null) return;
    const result = await Api.updateConversationFolderProperties(propertiesForm.folderId, { ...payload, updateSnapshots: choice === true });
    propertiesDialog.value = false;
    const located = await Api.locateConversationFolderInTree(propertiesForm.folderId);
    mergeLocatedFolders(located.folderItems || []);
    emitRows();
    const skipped = Number(result.skippedRunningCount || 0);
    ElMessage.success(choice === true ? `属性已保存，更新 ${result.updatedCount || 0} 个快照${skipped ? `；运行中跳过 ${skipped} 个` : ""}` : "属性已保存；已有快照保持不变");
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { propertiesSaving.value = false; }
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
      if (archivedMove) {
        // Archive is a separate cached branch, not the conversation's folderId.
        const archive = invalidateBranch("", "archive");
        archive.items = archive.items.filter(item => item.conversationUuid !== row.conversationUuid);
        if (isExpanded("__archive")) await loadChildren("", "archive", { force: true, indicate: false });
      }
    }
    moveDialog.value = false;
    await invalidateAndRefresh(
      treeItemParent(row), moveFolderId.value,
      moveMode.value === "delete" ? null : row,
    );
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
    if (row.kind === "conversation" && row.conversationUuid === props.activeConversationUuid) selectFolder(String(targetFolderId || ""));
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
  await mergeLocatedItem(locatedRow);
  if (query.value.trim()) await runSearch();
}
async function refreshAffected(row) {
  const parent = row.kind === "folder" ? String(row.parentId || "") : String(row.folderId || "");
  await invalidateAndRefresh(parent, parent, row);
}

async function runMenuAction(action) {
  const row = menu.value.row; closeMenu(); if (!row) return;
  try {
    if (action === "new-conversation") {
      const folderId = row.kind === "folder" ? String(row.folderId || "") : "";
      selectFolder(folderId);
      emit("new-conversation", folderId);
    }
    else if (action === "new-folder") await promptFolder(row.kind === "folder" ? row.folderId : "");
    else if (action === "rename") await renameRow(row);
    else if (action === "pin") await togglePin(row);
    else if (action === "move") await showMove(row);
    else if (action === "properties") await showProperties(row);
    else if (action === "refresh-prompt" && !row.local && !running(row)) {
      promptRow.value = { ...row };
      promptDialog.value = true;
    }
    else if (action === "delete-folder") await showMove(row, "delete");
    else if (action === "duplicate") await duplicateConversation(row);
    else if (action === "archive") await archiveConversation(row);
    else if (action === "delete-conversation") await removeConversation(row);
  } catch (error) { if (!['cancel', 'close'].includes(error)) ElMessage.error(apiError(error)); }
}

function dragStart(event, row) {
  closeOverview();
  if (moveInFlight.value || !["folder", "conversation"].includes(row.kind) || row.local || (row.kind === 'folder' && (query.value || row.archived))) { event.preventDefault(); return; }
  closeMenu();
  const canMove = !query.value && !row.archived;
  drag.value = { row: canMove ? row : null, target: null, zone: "", busy: false };
  event.dataTransfer.setData('application/x-openbear-tree', row.kind);
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
    if (await moveTreeItem(source, intent.targetFolderId, intent.beforeId, intent.afterId)) {
      await invalidateAndRefresh(treeItemParent(source), intent.targetFolderId, source);
    }
  } catch (error) { ElMessage.error(apiError(error)); }
  finally { clearDrag(); }
}
function clearDrag() {
  clearDropTarget();
  drag.value = { row: null, target: null, zone: "", busy: false };
}
function rowKeydown(event, row) {
  if (event.key === "Enter") { event.preventDefault(); void activateRow(row); }
  else if (event.key === "ArrowRight" && ["folder", "system"].includes(row.kind) && !isExpanded(row.kind === "system" ? row.id : row.folderId)) { event.preventDefault(); void toggleRow(row); }
  else if (event.key === "ArrowLeft" && ["folder", "system"].includes(row.kind) && isExpanded(row.kind === "system" ? row.id : row.folderId)) { event.preventDefault(); void toggleRow(row); }
  else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect(); openMenu({ preventDefault(){}, stopPropagation(){}, clientX: rect.right - 8, clientY: rect.top + 8 }, row); }
  else if (event.key === "Escape") { closeMenu(); clearDrag(); }
}
function globalKeydown(event) { if (event.key === "Escape") { closeMenu(); clearDrag(); if (impactDialog.value) finishImpact(null); } }

watch(() => props.activeConversationUuid, closeOverview);
watch(query, closeOverview);
watch(displayRows, rows => {
  if (overview.value.open && !rows.some(row => row.conversationUuid === overview.value.row?.conversationUuid)) closeOverview();
});
watch(() => referenceCatalog.treeStatus, data => { if (data) { statusRequestGeneration += 1; applyStatus(data); } });
watch(() => [referenceCatalog.connected, referenceCatalog.ready], scheduleStatus);
onMounted(async () => {
  window.addEventListener("keydown", globalKeydown);
  window.addEventListener("blur", closeOverview);
  await refreshTree({ preserve: false });
  scheduleStatus();
});
onBeforeUnmount(() => {
  closeOverview();
  window.removeEventListener("blur", closeOverview);
  window.removeEventListener("keydown", globalKeydown);
  if (searchTimer) clearTimeout(searchTimer);
  if (statusTimer) clearTimeout(statusTimer);
  if (dragExpandTimer) clearTimeout(dragExpandTimer);
  if (impactState.resolve) finishImpact(null);
});
</script>

<template>
  <section class="conversation-tree" aria-label="会话目录树" @contextmenu.self="openRootMenu">
    <header class="tree-toolbar">
      <div class="tree-title"><el-icon><ChatLineRound /></el-icon><strong>会话</strong><span>{{ activeCount }}</span></div>
      <div class="tree-tools">
        <el-dropdown trigger="click" placement="bottom-end">
          <button class="tree-tool primary" type="button" :title="`新建到：${selectedTargetLabel}`"><el-icon><Plus /></el-icon></button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="emit('new-conversation', selectedFolderId)"><el-icon><ChatLineRound /></el-icon>新建会话 · {{ selectedTargetLabel }}</el-dropdown-item>
              <el-dropdown-item @click="promptFolder(selectedFolderId)"><el-icon><FolderAdd /></el-icon>新建目录 · {{ selectedTargetLabel }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <button class="tree-tool" type="button" title="刷新目录树" :disabled="loading" @click="refreshTree({ preserve: true })"><el-icon :class="loading && 'is-spinning'"><Refresh /></el-icon></button>
      </div>
    </header>
    <div class="tree-search">
      <el-icon :class="{ 'is-spinning': searchLoading }"><component :is="searchLoading ? Loading : Search" /></el-icon>
      <input v-model="query" aria-label="搜索目录或会话" placeholder="搜索目录或会话…" />
      <button v-if="archiveUnlocked" type="button" :class="{ active: searchArchived }" :aria-pressed="searchArchived" title="搜索已归档会话" @click="searchArchived = !searchArchived"><el-icon><Box /></el-icon></button>
      <button v-if="query" type="button" title="清空搜索" @click="query = ''">×</button>
    </div>

    <div ref="listRef" class="tree-list" :class="{ 'drop-root': drag.target?.kind === 'root' }" role="tree" aria-label="会话和目录"
      @contextmenu.prevent.stop="openRootMenu" @dragover.self="dragOver($event, rootDropTarget)" @drop.self="drop($event, rootDropTarget)"
      @dragleave.self="clearDropTarget" @scroll.passive="closeOverview">
      <div v-if="loading && !initialized" class="tree-placeholder"><el-icon class="is-spinning"><Loading /></el-icon> 正在定位最近会话…</div>
      <template v-else>
        <div
          v-for="row in displayRows" :key="`${row.kind}:${row.id || rowId(row)}`"
          :data-tree-id="row.id || rowId(row)" :data-kind="row.kind"
          class="tree-row-wrap" :class="[`kind-${row.kind}`, { 'drop-inside': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'inside', 'drop-before': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'before', 'drop-after': drag.target && rowId(drag.target) === rowId(row) && drag.zone === 'after' }]"
          :style="{ '--indent': indentation(row.depth) }"
          role="treeitem" :aria-level="Number(row.depth || 0) + 1" :aria-busy="rowLoading(row)"
          :aria-expanded="['folder','system'].includes(row.kind) ? String(isExpanded(row.kind === 'system' ? row.id : row.folderId)) : undefined"
          :tabindex="['folder','conversation','system'].includes(row.kind) ? 0 : -1"
          :draggable="!row.local && !moveInFlight && (row.kind === 'conversation' || (row.kind === 'folder' && !query && !row.archived))"
          @keydown="rowKeydown($event, row)" @contextmenu="openMenu($event, row)"
          @pointerenter="enterOverview($event, row)" @pointerleave="leaveOverview"
          @dragstart="dragStart($event, row)" @dragover="dragOver($event, row)" @drop="drop($event, row)" @dragend="clearDrag"
        >
          <template v-if="['folder','system'].includes(row.kind)">
            <button class="tree-chevron" type="button" :aria-label="isExpanded(row.kind === 'system' ? row.id : row.folderId) ? '折叠' : '展开'" @click.stop="toggleRow(row)">
              <el-icon><component :is="isExpanded(row.kind === 'system' ? row.id : row.folderId) ? ArrowDown : ArrowRight" /></el-icon>
            </button>
            <button class="tree-node-main" type="button" :class="{ 'is-folder-target': (row.kind === 'folder' && selectedFolderId === row.folderId) || (row.kind === 'system' && row.systemNode === 'temporary' && selectedFolderId === '') }" :title="row.path || row.name" @click="activateRow(row)" @contextmenu="openMenu($event, row)">
              <el-icon class="node-icon" :class="{ 'is-spinning': rowLoading(row) }"><component :is="rowLoading(row) ? Loading : row.kind === 'system' ? (row.systemNode === 'archive' ? Box : ChatLineRound) : (isExpanded(row.folderId) ? FolderOpened : Folder)" /></el-icon>
              <span class="node-label">{{ row.name }}</span>
              <span v-if="row.hasLocalWorkspace || row.hasLocalPrompt" class="property-dot" :title="`${row.hasLocalWorkspace ? '本节点设置工作目录' : ''}${row.hasLocalWorkspace && row.hasLocalPrompt ? '；' : ''}${row.hasLocalPrompt ? '本节点设置提示词' : ''}`"><el-icon><InfoFilled /></el-icon></span>
              <span v-if="row.pinned" class="node-star" title="同级置顶"><el-icon><StarFilled /></el-icon></span>
              <span v-if="Number(row.runningDescendantCount || 0)" class="running-count" :aria-label="`${row.runningDescendantCount} 个后代会话运行中`">{{ row.runningDescendantCount }} 运行中</span>
              <span class="node-count" :aria-label="`${row.kind === 'system' ? row.count : Number(row.conversationCount || 0)} 个会话`">{{ row.kind === 'system' ? row.count : Number(row.conversationCount || 0) }}</span>
            </button>
          </template>

          <template v-else-if="row.kind === 'conversation'">
            <span class="tree-leaf-spacer"></span>
            <button class="tree-node-main conversation" type="button" :class="{ 'is-chat-active': activeConversationUuid === row.conversationUuid }" :title="row.local ? row.title : undefined" @click="row.search ? locateAndOpen(row) : activateRow(row)" @contextmenu="openMenu($event, row)">
              <el-icon class="node-icon" :class="{ 'is-spinning': rowLoading(row), 'is-working': running(row) && !rowLoading(row) }"><component :is="rowLoading(row) ? Loading : ChatLineRound" /></el-icon>
              <span class="node-copy"><span class="node-label">{{ row.title }}</span><small v-if="row.search">{{ row.path }}</small></span>
              <span v-if="row.pinned" class="node-star"><el-icon><StarFilled /></el-icon></span>
              <span v-if="running(row)" class="running-leaf" :title="row.currentStatus || '运行中'"><i></i><span>运行中</span></span>
            </button>
          </template>

          <button v-else-if="row.kind === 'more'" class="tree-inline-action" type="button" :disabled="stateFor(row.parentId || '', row.systemNode || '').loading" @click="loadChildren(row.parentId || '', row.systemNode || '', { append: true })">加载更多</button>
          <button v-else-if="row.kind === 'error'" class="tree-inline-action error" type="button" @click="loadChildren(row.parentId || '', row.systemNode || '', { force: true })">加载失败，点击重试</button>
          <span v-else class="tree-muted">此目录为空</span>
        </div>
        <button v-if="query && searchHasMore" class="search-more" type="button" :disabled="searchLoading" @click="runSearch({ append: true })">{{ searchLoading ? '加载中…' : '更多搜索结果' }}</button>
        <div v-else-if="query && !searchLoading && !searchRows.length" class="tree-placeholder">没有匹配的目录或会话</div>
      </template>
    </div>

    <ConversationOverview :open="overview.open" :row="overview.row" :anchor="overview.anchor" @close="closeOverview" @enter="keepOverview" @leave="leaveOverview"/>

    <Teleport to="body">
      <div v-if="menu.open" class="tree-menu-shield" @pointerdown.self="closeMenu" @click.self="closeMenu" @contextmenu.prevent.self="closeMenu">
        <div data-conversation-tree-menu class="tree-context-menu" :style="{ left: `${menu.x}px`, top: `${menu.y}px` }" role="menu" :aria-label="menu.row?.kind === 'folder' ? `${menu.row.name}目录菜单` : menu.row?.kind === 'root' ? '会话列表菜单' : menu.row?.kind === 'system' ? `${menu.row.name}菜单` : `${menu.row?.title || '会话'}菜单`" @pointerdown.stop @click.stop @contextmenu.prevent.stop>
          <template v-if="menu.row?.kind === 'folder'">
            <button role="menuitem" @click="runMenuAction('new-conversation')"><el-icon><ChatLineRound /></el-icon><span>在此新建会话</span></button>
            <button role="menuitem" @click="runMenuAction('new-folder')"><el-icon><FolderAdd /></el-icon><span>新建子目录</span></button>
            <hr />
            <button role="menuitem" @click="runMenuAction('rename')"><el-icon><EditPen /></el-icon><span>重命名</span></button>
            <button role="menuitem" @click="runMenuAction('pin')"><el-icon><component :is="menu.row?.pinned ? Star : StarFilled" /></el-icon><span>{{ menu.row?.pinned ? '取消置顶' : '置顶' }}</span></button>
            <button role="menuitem" @click="runMenuAction('move')"><el-icon><FolderOpened /></el-icon><span>移动到…</span></button>
            <button role="menuitem" @click="runMenuAction('properties')"><el-icon><InfoFilled /></el-icon><span>目录属性</span></button>
            <hr />
            <button class="danger" role="menuitem" @click="runMenuAction('delete-folder')"><el-icon><Delete /></el-icon><span>删除目录</span></button>
          </template>
          <template v-else-if="['system', 'root'].includes(menu.row?.kind)">
            <button v-if="menu.row?.systemNode === 'temporary' || menu.row?.kind === 'root'" role="menuitem" @click="runMenuAction('new-conversation')"><el-icon><ChatLineRound /></el-icon><span>新建临时会话</span></button>
            <button role="menuitem" @click="runMenuAction('new-folder')"><el-icon><FolderAdd /></el-icon><span>新建根级目录</span></button>
          </template>
          <template v-else>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('rename')"><el-icon><EditPen /></el-icon><span>重命名</span></button>
            <button role="menuitem" :disabled="menu.row?.local || running(menu.row)" @click="runMenuAction('duplicate')"><el-icon><DocumentCopy /></el-icon><span>复制会话</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('pin')"><el-icon><component :is="menu.row?.pinned ? Star : StarFilled" /></el-icon><span>{{ menu.row?.pinned ? '取消置顶' : '置顶' }}</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('move')"><el-icon><FolderOpened /></el-icon><span>移动到…</span></button>
            <button role="menuitem" :disabled="menu.row?.local || running(menu.row)" @click="runMenuAction('refresh-prompt')"><el-icon><Refresh /></el-icon><span>更新系统提示词…</span></button>
            <button role="menuitem" :disabled="menu.row?.local" @click="runMenuAction('archive')"><el-icon><component :is="menu.row?.archived ? RefreshLeft : Box" /></el-icon><span>{{ menu.row?.archived ? '取消归档' : '归档' }}</span></button>
            <hr />
            <button class="danger" role="menuitem" :disabled="running(menu.row)" @click="runMenuAction('delete-conversation')"><el-icon><Delete /></el-icon><span>删除会话</span></button>
          </template>
        </div>
      </div>
    </Teleport>

    <ConversationPromptDialog v-model="promptDialog" :conversation="promptRow" />

    <el-dialog v-model="propertiesDialog" width="min(680px, calc(100vw - 24px))" append-to-body class="folder-properties-dialog" :close-on-click-modal="false">
      <template #header>
        <div class="folder-properties-heading">
          <h2>目录属性 · {{ propertiesForm.name }}</h2>
          <p>{{ propertiesForm.path }}</p>
        </div>
      </template>
      <div v-loading="propertiesLoading" class="property-form">
        <section class="property-section" aria-labelledby="folder-workspace-label">
          <label class="property-field">
            <span id="folder-workspace-label">本节点工作目录 <em>清空即继承</em></span>
            <el-input v-model="propertiesForm.workspaceDir" aria-labelledby="folder-workspace-label" placeholder="例如 /home/user/projects/my-project" clearable />
          </label>
          <div class="effective-value">
            <b>继承后有效值</b>
            <code>{{ propertiesForm.workspaceEffective || '—' }}</code>
            <small>来源：{{ propertiesForm.workspaceSource }}</small>
          </div>
        </section>
        <section class="property-section" aria-labelledby="folder-prompt-label">
          <!-- Monaco owns its input focus. A wrapping label redirects clicks to its hidden IME textarea. -->
          <div class="property-field" role="group" aria-labelledby="folder-prompt-label">
            <span id="folder-prompt-label">本节点注入提示词（Markdown） <em>清空即继承；就近覆盖，不累加</em></span>
            <div class="folder-prompt-editor"><MdEditor v-model="propertiesForm.promptMarkdown" language="markdown" completion-mode="none" /></div>
          </div>
          <div class="effective-value">
            <b>当前继承值</b>
            <pre>{{ propertiesForm.promptEffective || '未设置' }}</pre>
            <small>来源：{{ propertiesForm.promptSource }}</small>
          </div>
        </section>
        <p class="property-note">属性只提供给主会话模板变量 <code>folderWorkspaceDir</code> / <code>folderPrompt</code>。不会创建目录、改变工具 cwd、公共 workspace、产物根或 Agent 快照。</p>
      </div>
      <template #footer><el-button @click="propertiesDialog = false">取消</el-button><el-button type="primary" :loading="propertiesSaving" @click="saveProperties">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="moveDialog" width="min(560px, calc(100vw - 24px))" append-to-body :title="moveMode === 'delete' ? '迁移目录内容后删除' : '移动到…'" :close-on-click-modal="false">
      <el-input v-model="moveFolderQuery" clearable placeholder="搜索完整目录路径" :prefix-icon="Search" />
      <div class="folder-picker" role="listbox">
        <button type="button" :class="{ selected: moveFolderId === '' }" @click="moveFolderId = ''"><el-icon><Folder /></el-icon><span>{{ moveRow?.kind === 'conversation' ? '临时会话' : '根级目录' }}</span></button>
        <button v-for="folder in filteredMoveFolders" :key="folder.folderId" type="button" :disabled="invalidMoveTarget(folder)" :class="{ selected: moveFolderId === folder.folderId }" @click="moveFolderId = folder.folderId"><el-icon><Folder /></el-icon><span>{{ folder.path }}</span></button>
      </div>
      <div v-if="moveMode === 'move' && moveRow?.kind === 'conversation' && moveRow?.archived" class="move-archive-options">
        <div><el-checkbox v-model="moveUnarchive">取消归档</el-checkbox><p>移动后恢复为普通会话。</p></div>
        <div><el-checkbox v-model="moveUpdateSnapshots">更新系统提示词</el-checkbox><p>按目标目录和当前模板重新组装，现有提示词缓存将失效；运行中的会话跳过更新。</p></div>
      </div>
      <template #footer><el-button @click="moveDialog = false">取消</el-button><el-button type="primary" :loading="moveBusy" @click="submitMove">{{ moveMode === 'delete' ? '下一步' : '移动' }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="impactDialog" width="min(600px, calc(100vw - 24px))" append-to-body :show-close="false" :close-on-click-modal="false" :close-on-press-escape="false" title="是否同时更新已有会话的系统提示词？">
      <div class="impact-copy">
        <p>此次{{ impactState.action }}影响 <strong>{{ impactState.impact?.affectedCount || 0 }}</strong> 个已有会话：可更新 {{ impactState.impact?.updatableCount || 0 }} 个；运行中 {{ impactState.impact?.runningCount || 0 }} 个，本次将跳过。<span v-if="impactState.impact?.archivedCount">其中已归档 {{ impactState.impact.archivedCount }} 个。</span></p>
        <p>更新会用当前模板和各会话自己的当前参数重新组装完整系统提示词。这会使对应提示词/Provider continuation 缓存失效，可能增加后续输入开销和延迟。</p>
        <p>聊天历史、TaskMemory 和文件不会删除。Agent 模板与快照不受影响。运行中目标在提交锁内重查，跳过后不会自动延后更新。</p>
      </div>
      <template #footer><el-button @click="finishImpact(null)">取消</el-button><el-button @click="finishImpact(false)">仅{{ impactState.action }}，不更新</el-button><el-button type="primary" @click="finishImpact(true)">{{ impactState.action }}并更新可更新会话</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.conversation-tree { display:flex; min-height:0; flex:1; flex-direction:column; color:#3f3f46; font-family:inherit; }
.conversation-tree button,.conversation-tree input { font-family:inherit; }
.tree-list.drop-root { background:rgba(37,99,235,.045); outline:1px dashed rgba(37,99,235,.4); outline-offset:-3px; }
.tree-toolbar { display:flex; align-items:center; justify-content:space-between; padding:0 11px 6px 13px; }
.tree-title { display:flex; align-items:baseline; gap:6px; font-size:12px; color:#52525b; }
.tree-title > .el-icon { align-self:center; }
.tree-title span { font-size:10px; font-weight:500; color:#a1a1aa; }
.tree-tools { display:flex; gap:3px; }
.tree-tool { display:grid; width:27px; height:27px; place-items:center; border:0; border-radius:8px; background:transparent; color:#71717a; cursor:pointer; }
.tree-tool:hover,.tree-tool:focus-visible { background:#e4e4e7; color:#18181b; outline:none; }
.tree-tool.primary { color:#2563eb; }
.tree-search { display:flex; align-items:center; gap:6px; margin:0 10px 7px; padding:0 9px; height:31px; border:1px solid #dedee3; border-radius:9px; background:rgba(255,255,255,.78); color:#a1a1aa; }
.tree-search input { min-width:0; flex:1; border:0; outline:0; background:transparent; color:#27272a; font-size:12px; }
.tree-search button { display:grid; place-items:center; width:20px; height:20px; padding:0; border:0; border-radius:5px; background:transparent; color:#a1a1aa; cursor:pointer; }
.tree-search button:hover,.tree-search button.active { background:#e4e4e7; color:#2563eb; }
.tree-list { min-height:0; flex:1; overflow:auto; padding:0 7px 10px; scrollbar-width:thin; overscroll-behavior:contain; }
.tree-row-wrap { position:relative; display:flex; width:100%; min-height:30px; align-items:center; padding-left:var(--indent); border-radius:8px; outline:none; }
.tree-row-wrap:focus-visible { box-shadow:inset 0 0 0 2px rgba(37,99,235,.45); }
.tree-row-wrap.drop-inside { background:rgba(37,99,235,.13); box-shadow:inset 0 0 0 1px rgba(37,99,235,.48); }
.tree-row-wrap.drop-before::before,.tree-row-wrap.drop-after::after { content:""; position:absolute; z-index:2; left:calc(var(--indent) + 7px); right:7px; height:2px; border-radius:2px; background:#2563eb; }
.tree-row-wrap.drop-before::before { top:-1px; }.tree-row-wrap.drop-after::after { bottom:-1px; }
.tree-chevron { display:grid; width:24px; height:27px; place-items:center; border:0; border-radius:7px; background:transparent; color:#a1a1aa; cursor:pointer; }
.tree-chevron { flex:0 0 auto; }
.tree-chevron:hover { background:rgba(161,161,170,.18); color:#52525b; }
.tree-node-main { display:flex; width:100%; min-width:0; height:29px; flex:1 1 auto; align-items:center; gap:6px; padding:0 5px; border:0; border-radius:7px; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.tree-node-main:hover { background:rgba(228,228,231,.58); }
.tree-node-main.is-folder-target { background:rgba(24,24,27,.055); color:#27272a; }
.tree-node-main.is-chat-active { background:rgba(37,99,235,.075); color:#1d4ed8; }
.tree-node-main:focus-visible,.tree-chevron:focus-visible { outline:2px solid rgba(37,99,235,.5); outline-offset:-2px; }
.node-icon { flex:0 0 auto; color:#71717a; }
/* Same rotating top/right border as the work-detail button; the glyph itself
   stays still and the absolute ring does not alter row/icon geometry. */
.node-icon.is-working { position:relative; color:#2563eb; }
.node-icon.is-working::before { content:""; pointer-events:none; position:absolute; inset:-3px; border:1.5px solid transparent; border-top-color:#2563eb; border-right-color:rgba(37,99,235,.42); border-radius:50%; animation:tree-work-border-spin .9s linear infinite; }
@keyframes tree-work-border-spin { to { transform:rotate(360deg); } }
html.dark .node-icon.is-working { color:#93b9f7; }
html.dark .node-icon.is-working::before { border-top-color:#93b9f7; border-right-color:rgba(147,185,247,.42); }
.node-label { min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; font-weight:500; line-height:1.4; }
.node-copy { display:flex; min-width:0; flex:1; flex-direction:column; line-height:1.4; }
.node-copy small { overflow:hidden; margin-top:2px; color:#a1a1aa; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
.node-star { display:grid; color:#d97706; font-size:11px; }
.node-count { color:#a1a1aa; font-size:10px; }
.property-dot { display:grid; color:#0f766e; font-size:11px; }
.running-count { padding:2px 5px; border-radius:999px; background:#dcfce7; color:#15803d; font-size:9px; font-weight:650; white-space:nowrap; }
.running-leaf { display:flex; align-items:center; gap:4px; color:#15803d; font-size:9px; font-weight:650; }
.running-leaf i { width:6px; height:6px; border-radius:50%; background:#22c55e; box-shadow:0 0 0 3px rgba(34,197,94,.13); animation:tree-pulse 1.8s ease-in-out infinite; }
.tree-leaf-spacer { width:24px; flex:0 0 auto; }
.tree-inline-action,.search-more { margin-left:28px; border:0; background:transparent; color:#2563eb; font-size:11px; cursor:pointer; }
.tree-inline-action.error { color:#dc2626; }.tree-muted { display:flex; align-items:center; gap:5px; padding-left:6px; color:#a1a1aa; font-size:11px; }
.tree-placeholder { margin:8px; padding:14px 8px; border-radius:9px; background:rgba(228,228,231,.45); color:#a1a1aa; font-size:11px; text-align:center; }
.search-more { width:calc(100% - 24px); margin:7px 12px; padding:7px; border-radius:8px; background:rgba(37,99,235,.08); }
.is-spinning { animation:tree-spin .8s linear infinite; }
@keyframes tree-spin { to { transform:rotate(360deg); } } @keyframes tree-pulse { 50% { opacity:.35; transform:scale(.8); } }
/* Keep the running ring: it is an operational status indicator, not decoration. */
@media (prefers-reduced-motion: reduce) { .is-spinning,.running-leaf i { animation:none; } }
@media (pointer:coarse) { .tree-row-wrap { min-height:36px; } .tree-node-main { height:34px; } }
</style>

<style>
.tree-menu-shield { position:fixed; inset:0; z-index:3200; background:transparent; }
.tree-context-menu { font-family:inherit; font-size:12px; font-weight:400; line-height:1.4; position:fixed; width:min(194px,calc(100vw - 16px)); max-height:calc(100vh - 16px); overflow:auto; padding:5px; border:1px solid rgba(0,0,0,.11); border-radius:10px; background:rgba(250,250,250,.97); color:#27272a; box-shadow:0 16px 42px rgba(15,23,42,.2),0 4px 12px rgba(15,23,42,.08); backdrop-filter:blur(18px) saturate(1.3); }
.tree-context-menu button { font:inherit; display:grid; grid-template-columns:16px 1fr; align-items:center; gap:7px; width:100%; min-height:28px; padding:4px 8px; border:0; border-radius:7px; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.tree-context-menu button:hover:not(:disabled) { background:#2563eb; color:white; }.tree-context-menu button:disabled { opacity:.38; cursor:not-allowed; }.tree-context-menu button.danger { color:#dc2626; }.tree-context-menu button.danger:hover:not(:disabled) { background:#dc2626; color:white; }
.tree-context-menu hr { height:1px; margin:5px 7px; border:0; background:rgba(161,161,170,.28); }
/* Local dialog styling: keep the app font, and leave other dialogs/editors alone. */
.folder-properties-dialog.el-dialog {
  --el-dialog-padding-primary:0;
  display:flex; flex-direction:column; max-height:90vh; max-height:90dvh; margin:5vh auto; margin:5dvh auto; padding:0;
  overflow:hidden; border:1px solid var(--el-border-color-lighter); border-radius:14px;
  font-family:inherit; font-size:13px; line-height:1.6; color:var(--el-text-color-primary);
}
.folder-properties-dialog .el-dialog__header { flex:none; margin:0; padding:18px 52px 16px 22px; border-bottom:1px solid var(--el-border-color-lighter); }
.folder-properties-dialog .el-dialog__headerbtn { top:15px; right:14px; width:28px; height:28px; border-radius:7px; }
.folder-properties-dialog .el-dialog__headerbtn:hover { background:var(--el-fill-color-light); }
.folder-properties-dialog .folder-properties-heading h2 { margin:0; font-size:16px; font-weight:600; line-height:1.5; overflow-wrap:anywhere; }
.folder-properties-dialog .folder-properties-heading p { margin:4px 0 0; color:var(--el-text-color-secondary); font-size:12px; line-height:1.6; overflow-wrap:anywhere; }
.folder-properties-dialog .el-dialog__body { min-height:0; padding:20px 22px; overflow:auto; overscroll-behavior:contain; }
.folder-properties-dialog .el-dialog__footer { flex:none; padding:14px 22px; border-top:1px solid var(--el-border-color-lighter); }
.folder-properties-dialog .el-button { min-width:72px; height:34px; font-family:inherit; font-size:13px; font-weight:500; }
.folder-properties-dialog .property-form { display:flex; min-height:220px; flex-direction:column; gap:20px; }
.folder-properties-dialog .property-section { display:flex; min-width:0; flex-direction:column; gap:10px; }
.folder-properties-dialog .property-section + .property-section { padding-top:20px; border-top:1px solid var(--el-border-color-lighter); }
.folder-properties-dialog .property-field { display:block; min-width:0; }
.folder-properties-dialog .property-field > span { display:block; margin-bottom:9px; font-size:13px; font-weight:600; line-height:1.6; }
.folder-properties-dialog .property-field em { display:block; margin-top:2px; color:var(--el-text-color-secondary); font-size:12px; font-style:normal; font-weight:400; }
.folder-properties-dialog .el-input { font-family:inherit; font-size:13px; }
.folder-properties-dialog .el-input__wrapper { min-height:36px; }
.folder-properties-dialog .el-input__inner { font-family:inherit; }
.folder-properties-dialog .folder-prompt-editor { height:220px; min-height:160px; }
.folder-properties-dialog .effective-value { display:flex; min-width:0; flex-direction:column; gap:5px; padding:10px 12px; border-radius:8px; background:var(--el-fill-color-light); }
.folder-properties-dialog .effective-value b { color:var(--el-text-color-secondary); font-size:12px; font-weight:500; }
.folder-properties-dialog .effective-value code,.folder-properties-dialog .effective-value pre { min-width:0; max-height:112px; overflow:auto; margin:0; color:var(--el-text-color-primary); font-size:13px; line-height:1.65; white-space:pre-wrap; overflow-wrap:anywhere; }
.folder-properties-dialog .effective-value code { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.folder-properties-dialog .effective-value pre { font-family:inherit; }
.folder-properties-dialog .effective-value small { color:var(--el-text-color-secondary); font-size:12px; line-height:1.6; overflow-wrap:anywhere; }
.folder-properties-dialog .property-note { margin:0; color:var(--el-text-color-secondary); font-size:12px; line-height:1.7; overflow-wrap:anywhere; }
.folder-properties-dialog .property-note code { color:var(--el-text-color-regular); font-size:inherit; }
.folder-properties-dialog .el-dialog__body,.folder-properties-dialog .effective-value code,.folder-properties-dialog .effective-value pre { scrollbar-width:none; -ms-overflow-style:none; }
.folder-properties-dialog .el-dialog__body::-webkit-scrollbar,.folder-properties-dialog .effective-value code::-webkit-scrollbar,.folder-properties-dialog .effective-value pre::-webkit-scrollbar { display:none; }
@media (max-width:560px) {
  .folder-properties-dialog.el-dialog { max-height:calc(100vh - 24px); max-height:calc(100dvh - 24px); margin:12px auto; }
  .folder-properties-dialog .el-dialog__header { padding:16px 48px 14px 16px; }
  .folder-properties-dialog .el-dialog__headerbtn { top:13px; right:12px; }
  .folder-properties-dialog .el-dialog__body { padding:16px; }
  .folder-properties-dialog .el-dialog__footer { padding:12px 16px; }
  .folder-properties-dialog .folder-prompt-editor { height:200px; }
}
.impact-copy { color:var(--el-text-color-secondary); font-size:12px; line-height:1.65; }
.move-archive-options { display:grid; gap:10px; margin-top:16px; padding-top:14px; border-top:1px solid var(--el-border-color-lighter); }
.move-archive-options .el-checkbox { height:auto; font-family:inherit; }
.move-archive-options .el-checkbox__label { font-size:13px; line-height:1.6; font-weight:500; }
.move-archive-options p { margin:2px 0 0 22px; font-size:12px; line-height:1.6; color:var(--el-text-color-secondary); }
.folder-picker { max-height:330px; overflow:auto; margin-top:10px; padding:5px; border:1px solid var(--el-border-color-light); border-radius:9px; }.folder-picker button { display:flex; width:100%; align-items:center; gap:7px; padding:7px 9px; border:0; border-radius:7px; background:transparent; color:var(--el-text-color-primary); text-align:left; cursor:pointer; }.folder-picker button:hover:not(:disabled),.folder-picker button.selected { background:var(--el-color-primary-light-9); color:var(--el-color-primary); }.folder-picker button:disabled { opacity:.35; cursor:not-allowed; }.folder-picker span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }
html.dark .conversation-tree { color:#d4d4d8; } html.dark .tree-tool:hover,html.dark .tree-tool:focus-visible,html.dark .tree-node-main:hover { background:rgba(63,63,70,.58); color:#fafafa; } html.dark .tree-search { border-color:#3f3f46; background:rgba(24,24,27,.8); } html.dark .tree-search input { color:#f4f4f5; } html.dark .tree-node-main.is-folder-target { background:rgba(161,161,170,.13); color:#f4f4f5; } html.dark .tree-node-main.is-chat-active { background:rgba(59,130,246,.14); color:#bfdbfe; } html.dark .running-count { background:rgba(22,101,52,.35); color:#86efac; } html.dark .tree-placeholder { background:rgba(63,63,70,.42); } html.dark .tree-context-menu { border-color:rgba(255,255,255,.12); background:rgba(39,39,42,.97); color:#f4f4f5; }
</style>
