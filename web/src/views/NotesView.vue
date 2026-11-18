<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  ChevronLeft,
  ChevronRight,
  ChevronsDownUp,
  Crosshair,
  Download,
  Edit3,
  FilePlus2,
  FolderInput,
  FolderPlus,
  ListTree,
  Lock,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pin,
  Plus,
  RefreshCw,
  Search,
  Smile,
  Trash2,
  Upload,
  X,
} from '@lucide/vue';
import AdminPageHeader from '../components/AdminPageHeader.vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import { REFERENCE_MIME, referenceToken } from '../references/codec.js';
import { useAdminPhone } from '../adminViewport.js';
import { Api, apiError } from '../api.js';
import NoteIcon from '../notes/NoteIcon.vue';
import SiyuanLikeEditor from '../notes/SiyuanLikeEditor.vue';
import NoteImportDialog from '../notes/NoteImportDialog.vue';
import NoteMoveDialog from '../notes/NoteMoveDialog.vue';
import { extractOutlineFromMarkdown } from '../notes/markdownCodec.js';

const TREE_EMOJIS = [
  '🏠', '👷', '🔑', '📁', '📝', '💡',
  '🚀', '🛠️', '🌐', '📡', '⚡', '🔒',
  '📊', '🧪', '🎯', '📌', '📚', '🧩',
  '🐳', '🐧', '🪟', '🤖', '⭐', '🔥',
];

const EXPANDED_STORAGE_KEY = 'openbear:kb-notes:expanded';
const OPEN_TABS_STORAGE_KEY = 'openbear:kb-notes:tabs';
const ACTIVE_NOTE_STORAGE_KEY = 'openbear:kb-notes:active';

const emit = defineEmits(['mobile-header-ready']);
const isPhone = useAdminPhone();

const treeItems = ref([]);
const treeLoading = ref(false);
const treeFilter = ref('');
const treeCollapsed = ref(false);
const treeWidth = ref(268);

const expandedIds = ref(loadExpandedIds());
const openTabIds = ref(loadStoredArray(OPEN_TABS_STORAGE_KEY));
const activeNoteUuid = ref(loadStoredString(ACTIVE_NOTE_STORAGE_KEY));
const noteDetails = reactive({}); // noteUuid -> full note detail
const savingStates = reactive({}); // noteUuid -> 'saved' | 'saving' | 'dirty' | 'error' | 'conflict'
const conflictNotes = {}; // noteUuid -> latest server version while a conflict is unresolved
const UNSAVED_STATES = new Set(['dirty', 'saving', 'error', 'conflict']);
const detailLoading = ref(false);

const mobileEditing = ref(false);
const mobileOutlineOpen = ref(false);

const editorRef = ref(null);
const importDialogOpen = ref(false);
const importParent = ref({ uuid: '', title: '' });

const moveDialogOpen = ref(false);
const movingRow = ref(null);
const moveBusy = ref(false);

const quickSearchOpen = ref(false);
const quickSearchQuery = ref('');
const quickSearchResults = ref([]);
const quickSearchLoading = ref(false);
const quickSearchIndex = ref(0);
let quickSearchTimer = 0;

const contextMenu = ref({ open: false, anchor: null, row: null });
const treeIconPicker = ref({ open: false, anchor: null, row: null });
const treeScrollRef = ref(null);
const dragState = reactive({ draggingUuid: '', targetUuid: '', zone: '' }); // zone: 'before' | 'inside' | 'after'
let dragExpandTimer = 0;

const saveTimers = new Map();
const inflightSaves = new Map();
const conflictPrompts = new Set();

function hasUnsavedChanges(noteUuid) {
  return UNSAVED_STATES.has(savingStates[noteUuid]);
}

function loadExpandedIds() {
  try {
    const raw = JSON.parse(localStorage.getItem(EXPANDED_STORAGE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

function persistExpandedIds() {
  try {
    localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify([...expandedIds.value]));
  } catch { /* ignore */ }
}

function loadStoredArray(key) {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string' && x) : [];
  } catch {
    return [];
  }
}

function loadStoredString(key) {
  try {
    return String(localStorage.getItem(key) || '');
  } catch {
    return '';
  }
}

function persistTabs() {
  try {
    localStorage.setItem(OPEN_TABS_STORAGE_KEY, JSON.stringify(openTabIds.value));
    localStorage.setItem(ACTIVE_NOTE_STORAGE_KEY, activeNoteUuid.value || '');
  } catch { /* ignore */ }
}

const byUuid = computed(() => {
  const m = new Map();
  for (const item of treeItems.value) m.set(item.noteUuid, item);
  return m;
});

const childrenByParent = computed(() => {
  const map = new Map();
  for (const item of treeItems.value) {
    const p = item.parentUuid || '';
    if (!map.has(p)) map.set(p, []);
    map.get(p).push(item);
  }
  return map;
});

const visibleTreeRows = computed(() => {
  const q = treeFilter.value.trim().toLowerCase();
  if (q) {
    return treeItems.value
      .filter((item) => `${item.title} ${item.summary} ${item.path}`.toLowerCase().includes(q))
      .map((item) => ({ ...item, filtered: true }));
  }
  const out = [];
  const walk = (parentUuid, depth) => {
    for (const item of childrenByParent.value.get(parentUuid) || []) {
      out.push({ ...item, depth });
      if (item.childCount > 0 && expandedIds.value.has(item.noteUuid)) {
        walk(item.noteUuid, depth + 1);
      }
    }
  };
  walk('', 0);
  return out;
});

const openTabs = computed(() =>
  openTabIds.value
    .map((uuid) => noteDetails[uuid] || byUuid.value.get(uuid))
    .filter(Boolean),
);

const activeNote = computed(() => (activeNoteUuid.value ? noteDetails[activeNoteUuid.value] || null : null));
const mobileOutlineItems = computed(() => extractOutlineFromMarkdown(activeNote.value?.content || ''));

function hasTabCustomIcon(item) {
  const raw = String(item?.icon || '').trim();
  return Boolean(raw && raw !== '📄' && raw !== '📓' && raw !== '📁');
}

function collapseAllTree() {
  expandedIds.value = new Set();
  persistExpandedIds();
}

function locateActiveInTree() {
  const target = activeNoteUuid.value;
  if (!target) return;
  let cur = byUuid.value.get(target)?.parentUuid || '';
  const seen = new Set();
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    expandedIds.value.add(cur);
    cur = byUuid.value.get(cur)?.parentUuid || '';
  }
  persistExpandedIds();
  nextTick(() => {
    const el = treeScrollRef.value?.querySelector?.(`[data-note-uuid="${target}"]`);
    el?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
  });
}

function openTreeIconPicker(event, row) {
  event?.stopPropagation?.();
  contextMenu.value.open = false;
  if (row?.isReadonly) {
    ElMessage.warning('该笔记已开启只读锁定');
    return;
  }
  const rect = event?.currentTarget?.getBoundingClientRect?.();
  const anchor = rect
    ? { getBoundingClientRect: () => rect }
    : contextMenu.value.anchor;
  treeIconPicker.value = { open: true, anchor, row };
}

async function applyTreeIcon(emoji) {
  const row = treeIconPicker.value.row;
  treeIconPicker.value.open = false;
  if (!row) return;
  try {
    if (!(await flushSave(row.noteUuid))) return;
    const res = await Api.updateKbNote(row.noteUuid, { icon: emoji });
    if (res?.note) {
      syncSavedMeta(row.noteUuid, res.note);
      if (noteDetails[row.noteUuid]) noteDetails[row.noteUuid].icon = res.note.icon;
      const idx = treeItems.value.findIndex((x) => x.noteUuid === row.noteUuid);
      if (idx >= 0) treeItems.value[idx] = { ...treeItems.value[idx], icon: res.note.icon };
    }
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function syncMobileHeaderState() {
  emit('mobile-header-ready', true);
}

function onAdminHeaderReady(ready) {
  emit('mobile-header-ready', Boolean(ready || (isPhone.value && mobileEditing.value)));
}

watch([isPhone, mobileEditing], syncMobileHeaderState, { immediate: true });

async function loadTree({ selectFirstIfEmpty = true } = {}) {
  treeLoading.value = true;
  try {
    const data = await Api.kbNotesTree();
    treeItems.value = data.items || [];
    const validSet = new Set(treeItems.value.map((i) => i.noteUuid));
    openTabIds.value = openTabIds.value.filter((id) => validSet.has(id));

    for (const item of treeItems.value) {
      if (noteDetails[item.noteUuid]) {
        Object.assign(noteDetails[item.noteUuid], {
          parentUuid: item.parentUuid,
          childCount: item.childCount,
          path: item.path,
          pinned: item.pinned,
        });
      }
    }

    if (activeNoteUuid.value && validSet.has(activeNoteUuid.value)) {
      await openNote(activeNoteUuid.value, { enterMobileEditor: false });
    } else if (selectFirstIfEmpty && treeItems.value.length && !isPhone.value) {
      await openNote(treeItems.value[0].noteUuid, { enterMobileEditor: false });
    } else if (!validSet.has(activeNoteUuid.value)) {
      activeNoteUuid.value = openTabIds.value[0] || '';
      persistTabs();
    }
  } catch (err) {
    ElMessage.error(`加载笔记目录失败：${apiError(err)}`);
  } finally {
    treeLoading.value = false;
  }
}

function expandAncestorsOf(noteUuid) {
  let cur = byUuid.value.get(noteUuid);
  let changed = false;
  while (cur?.parentUuid) {
    if (!expandedIds.value.has(cur.parentUuid)) {
      expandedIds.value.add(cur.parentUuid);
      changed = true;
    }
    cur = byUuid.value.get(cur.parentUuid);
  }
  if (changed) persistExpandedIds();
}

function toggleExpand(noteUuid) {
  if (expandedIds.value.has(noteUuid)) {
    expandedIds.value.delete(noteUuid);
  } else {
    expandedIds.value.add(noteUuid);
  }
  persistExpandedIds();
}

async function openNote(noteUuid, { enterMobileEditor = true, focusTitle = false } = {}) {
  if (!noteUuid) return;
  if (activeNoteUuid.value && activeNoteUuid.value !== noteUuid) {
    void flushSave(activeNoteUuid.value);
  }
  activeNoteUuid.value = noteUuid;
  if (!openTabIds.value.includes(noteUuid)) {
    openTabIds.value = [...openTabIds.value.slice(-11), noteUuid];
  }
  expandAncestorsOf(noteUuid);
  persistTabs();
  if (isPhone.value && enterMobileEditor) {
    mobileEditing.value = true;
  }

  detailLoading.value = !noteDetails[noteUuid];
  try {
    const res = await Api.kbNote(noteUuid);
    if (res?.note) {
      if (!hasUnsavedChanges(noteUuid)) {
        noteDetails[noteUuid] = res.note;
        savingStates[noteUuid] = 'saved';
      }
      if (focusTitle) {
        nextTick(() => editorRef.value?.focusTitle?.());
      }
    }
  } catch (err) {
    ElMessage.error(apiError(err));
  } finally {
    detailLoading.value = false;
  }
}

async function closeTab(noteUuid, event) {
  event?.stopPropagation();
  if (!(await flushSave(noteUuid))) {
    ElMessage.warning('这篇笔记还有未保存的修改，已保留标签页');
    return;
  }
  const idx = openTabIds.value.indexOf(noteUuid);
  if (idx < 0) return;
  const nextTabs = openTabIds.value.filter((id) => id !== noteUuid);
  openTabIds.value = nextTabs;
  if (activeNoteUuid.value === noteUuid) {
    const fallback = nextTabs[Math.min(idx, nextTabs.length - 1)] || '';
    activeNoteUuid.value = fallback;
    if (fallback) void openNote(fallback, { enterMobileEditor: false });
  }
  persistTabs();
}

function scheduleSave(noteUuid) {
  // Keep editing locally until the user resolves the conflict.
  if (savingStates[noteUuid] === 'conflict') return;
  savingStates[noteUuid] = 'dirty';
  const existing = saveTimers.get(noteUuid);
  if (existing) window.clearTimeout(existing);
  const timer = window.setTimeout(() => {
    saveTimers.delete(noteUuid);
    void flushSave(noteUuid);
  }, 550);
  saveTimers.set(noteUuid, timer);
}

// Resolves true once nothing for this note is left unsaved.
async function flushSave(noteUuid) {
  const timer = saveTimers.get(noteUuid);
  if (timer) {
    window.clearTimeout(timer);
    saveTimers.delete(noteUuid);
  }
  if (inflightSaves.has(noteUuid)) {
    await inflightSaves.get(noteUuid);
    return flushSave(noteUuid);
  }
  const detail = noteDetails[noteUuid];
  const state = savingStates[noteUuid];
  if (!detail) return true;
  if (state === 'conflict') {
    void resolveConflict(noteUuid);
    return false;
  }
  if (state !== 'dirty' && state !== 'error') return true;
  const run = saveNote(noteUuid, detail);
  inflightSaves.set(noteUuid, run);
  try {
    return await run;
  } finally {
    inflightSaves.delete(noteUuid);
  }
}

async function saveNote(noteUuid, detail) {
  const snapshotContent = detail.content;
  const snapshotTitle = detail.title || '未命名笔记';
  const snapshotIcon = detail.icon || '';
  savingStates[noteUuid] = 'saving';
  try {
    const res = await Api.updateKbNote(noteUuid, {
      title: snapshotTitle,
      icon: snapshotIcon,
      content: snapshotContent,
      expectedRevision: detail.revision,
    });
    if (res?.note) {
      detail.revision = res.note.revision;
      detail.updatedAtMs = res.note.updatedAtMs;
      detail.charCount = res.note.charCount;
      detail.summary = res.note.summary;
      const treeNode = treeItems.value.find((i) => i.noteUuid === noteUuid);
      if (treeNode) {
        treeNode.title = res.note.title;
        treeNode.icon = res.note.icon;
        treeNode.summary = res.note.summary;
        treeNode.charCount = res.note.charCount;
        treeNode.revision = res.note.revision;
      }
      const mutatedWhileSaving =
        detail.content !== snapshotContent || (detail.title || '未命名笔记') !== snapshotTitle || (detail.icon || '') !== snapshotIcon;
      savingStates[noteUuid] = mutatedWhileSaving ? 'dirty' : 'saved';
      if (mutatedWhileSaving) scheduleSave(noteUuid);
      return !mutatedWhileSaving;
    }
    savingStates[noteUuid] = 'error';
    return false;
  } catch (err) {
    const status = Number(err?.response?.status || 0);
    if (status === 409 && err?.response?.data?.currentNote) {
      savingStates[noteUuid] = 'conflict';
      conflictNotes[noteUuid] = err.response.data.currentNote;
      void resolveConflict(noteUuid);
    } else {
      savingStates[noteUuid] = 'error';
      ElMessage.error(`保存失败：${apiError(err)}`);
    }
    return false;
  }
}

// Neither choice discards text: overwriting keeps mine, loading the latest
// first stores mine as a sibling copy. Closing the prompt keeps editing local.
async function resolveConflict(noteUuid) {
  const detail = noteDetails[noteUuid];
  const latest = conflictNotes[noteUuid];
  if (!detail || !latest || conflictPrompts.has(noteUuid)) return;
  conflictPrompts.add(noteUuid);
  let action = 'close';
  try {
    await ElMessageBox.confirm(
      `「${detail.title || '未命名笔记'}」已在其他窗口或设备上被修改。覆盖会用你当前的内容替换对方的修改；载入最新版本时，你的内容会另存为一篇副本。`,
      '笔记版本冲突',
      {
        type: 'warning',
        confirmButtonText: '用我的版本覆盖',
        cancelButtonText: '载入最新版本',
        distinguishCancelAndClose: true,
        closeOnClickModal: false,
      },
    );
    action = 'overwrite';
  } catch (choice) {
    action = choice === 'cancel' ? 'load' : 'close';
  } finally {
    conflictPrompts.delete(noteUuid);
  }
  if (action === 'close' || noteDetails[noteUuid] !== detail) return;
  if (action === 'overwrite') {
    detail.revision = latest.revision;
    delete conflictNotes[noteUuid];
    savingStates[noteUuid] = 'dirty';
    void flushSave(noteUuid);
    return;
  }
  const copyTitle = `${detail.title || '未命名笔记'}（冲突副本）`;
  try {
    await Api.createKbNote({
      parentUuid: latest.parentUuid || '',
      afterUuid: noteUuid,
      title: copyTitle,
      icon: detail.icon || '',
      content: detail.content || '',
    });
  } catch (err) {
    ElMessage.error(`未能保存副本，已保留你的修改：${apiError(err)}`);
    return;
  }
  delete conflictNotes[noteUuid];
  noteDetails[noteUuid] = latest;
  savingStates[noteUuid] = 'saved';
  await loadTree({ selectFirstIfEmpty: false });
  ElMessage.success(`已载入最新版本，你的内容已另存为「${copyTitle}」`);
}

// Tree-side edits bump the revision too; adopt it so the next save is not a false conflict.
function syncSavedMeta(noteUuid, note) {
  const detail = noteDetails[noteUuid];
  if (detail && note && !hasUnsavedChanges(noteUuid)) {
    detail.revision = note.revision;
    detail.updatedAtMs = note.updatedAtMs;
  }
}

function onEditorChangeTitle(newTitle) {
  const detail = activeNote.value;
  if (!detail) return;
  detail.title = newTitle;
  const treeNode = treeItems.value.find((i) => i.noteUuid === detail.noteUuid);
  if (treeNode) treeNode.title = newTitle || '未命名笔记';
  scheduleSave(detail.noteUuid);
}

function onEditorChangeIcon(newIcon) {
  const detail = activeNote.value;
  if (!detail) return;
  detail.icon = newIcon;
  const treeNode = treeItems.value.find((i) => i.noteUuid === detail.noteUuid);
  if (treeNode) treeNode.icon = newIcon;
  scheduleSave(detail.noteUuid);
}

function onEditorChangeContent(newContent) {
  const detail = activeNote.value;
  if (!detail) return;
  detail.content = newContent;
  scheduleSave(detail.noteUuid);
}

async function onToggleReadonly(nextReadonly) {
  const detail = activeNote.value;
  if (!detail) return;
  if (!(await flushSave(detail.noteUuid))) return;
  try {
    const res = await Api.updateKbNote(detail.noteUuid, { isReadonly: nextReadonly });
    if (res?.note) {
      noteDetails[detail.noteUuid] = res.note;
      const treeNode = treeItems.value.find((i) => i.noteUuid === detail.noteUuid);
      if (treeNode) treeNode.isReadonly = res.note.isReadonly;
      ElMessage.success(nextReadonly ? '已锁定为只读' : '已解锁编辑');
    }
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

async function createNote({ parentUuid = '', afterUuid = '', title = '未命名笔记', icon = '' } = {}) {
  try {
    const res = await Api.createKbNote({ parentUuid, afterUuid, title, icon });
    const created = res?.note;
    if (!created) return;
    noteDetails[created.noteUuid] = created;
    savingStates[created.noteUuid] = 'saved';
    if (parentUuid) {
      expandedIds.value.add(parentUuid);
      persistExpandedIds();
    }
    await loadTree({ selectFirstIfEmpty: false });
    await openNote(created.noteUuid, { enterMobileEditor: true, focusTitle: true });
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function openRowMenu(event, row) {
  event.preventDefault();
  event.stopPropagation();
  const rect = event.currentTarget?.getBoundingClientRect?.();
  const x = event.clientX || rect?.left || 120;
  const y = event.clientY || rect?.bottom || 120;
  contextMenu.value = {
    open: true,
    row,
    anchor: {
      getBoundingClientRect: () => ({ x, y, left: x, right: x + 1, top: y, bottom: y + 1, width: 1, height: 1 }),
    },
  };
}

async function renameRow(row) {
  contextMenu.value.open = false;
  if (row.isReadonly) {
    ElMessage.warning('该笔记已开启只读锁定，请先解锁');
    return;
  }
  try {
    const { value } = await ElMessageBox.prompt('', '重命名笔记', {
      inputValue: row.title,
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputValidator: (v) => (String(v || '').trim() ? true : '标题不能为空'),
    });
    if (!(await flushSave(row.noteUuid))) return;
    const res = await Api.updateKbNote(row.noteUuid, { title: value.trim() });
    if (res?.note) {
      syncSavedMeta(row.noteUuid, res.note);
      if (noteDetails[row.noteUuid]) noteDetails[row.noteUuid].title = res.note.title;
      await loadTree({ selectFirstIfEmpty: false });
    }
  } catch { /* cancelled */ }
}

async function togglePinRow(row) {
  contextMenu.value.open = false;
  try {
    if (!(await flushSave(row.noteUuid))) return;
    const res = await Api.updateKbNote(row.noteUuid, { pinned: !row.pinned });
    syncSavedMeta(row.noteUuid, res?.note);
    await loadTree({ selectFirstIfEmpty: false });
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function openMoveForRow(row) {
  contextMenu.value.open = false;
  movingRow.value = row;
  moveDialogOpen.value = true;
}

async function confirmMoveDialog(targetParentUuid) {
  if (!movingRow.value) return;
  moveBusy.value = true;
  try {
    const data = await Api.moveKbNote(movingRow.value.noteUuid, { targetParentUuid });
    treeItems.value = data.items || [];
    if (targetParentUuid) {
      expandedIds.value.add(targetParentUuid);
      persistExpandedIds();
    }
    if (activeNoteUuid.value && noteDetails[activeNoteUuid.value]) {
      const refreshed = await Api.kbNote(activeNoteUuid.value);
      if (refreshed?.note) noteDetails[activeNoteUuid.value].ancestors = refreshed.note.ancestors;
    }
    moveDialogOpen.value = false;
    ElMessage.success('移动成功');
  } catch (err) {
    ElMessage.error(apiError(err));
  } finally {
    moveBusy.value = false;
  }
}

function exportNote(row, withChildren = false) {
  contextMenu.value.open = false;
  const url = `/api/kb-notes/items/${encodeURIComponent(row.noteUuid)}/export${withChildren ? '?children=1' : ''}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

async function deleteRow(row) {
  contextMenu.value.open = false;
  if (row.isReadonly) {
    ElMessage.warning('该笔记已开启只读锁定，请先解锁再删除');
    return;
  }
  let mode = 'cascade';
  try {
    if (row.childCount > 0) {
      await ElMessageBox.confirm(
        `「${row.title}」下包含子笔记。确定连同全部子笔记一并删除吗？（如需保留子笔记，可先将子笔记移出）`,
        '删除笔记及子项',
        {
          type: 'warning',
          confirmButtonText: '全部删除',
          cancelButtonText: '取消',
        },
      );
    } else {
      await ElMessageBox.confirm(`确定删除笔记「${row.title}」吗？删除后不可恢复。`, '删除笔记', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      });
    }
  } catch {
    return;
  }

  try {
    const res = await Api.deleteKbNote(row.noteUuid, mode);
    const deletedSet = new Set(res?.deletedUuids || [row.noteUuid]);
    for (const id of deletedSet) {
      delete noteDetails[id];
      delete savingStates[id];
    }
    openTabIds.value = openTabIds.value.filter((id) => !deletedSet.has(id));
    if (deletedSet.has(activeNoteUuid.value)) {
      activeNoteUuid.value = openTabIds.value[0] || '';
      if (isPhone.value && !activeNoteUuid.value) mobileEditing.value = false;
    }
    persistTabs();
    await loadTree({ selectFirstIfEmpty: !isPhone.value });
    ElMessage.success('已删除');
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

// Drag & Drop reordering + hierarchy nesting + @note reference export
function onRowDragStart(event, row) {
  dragState.draggingUuid = row.noteUuid;
  dragState.targetUuid = '';
  dragState.zone = '';
  event.dataTransfer.effectAllowed = 'copyMove';
  event.dataTransfer.setData('application/x-openbear-kb-note', row.noteUuid);
  const refObj = { kind: 'note', id: row.noteUuid, label: row.title || '未命名笔记', scope: 'full' };
  event.dataTransfer.setData(REFERENCE_MIME, JSON.stringify(refObj));
  event.dataTransfer.setData('text/plain', referenceToken(refObj));
}

function isDescendantOrSelf(candidateTargetUuid, draggingUuid) {
  if (!draggingUuid || !candidateTargetUuid) return false;
  let cur = candidateTargetUuid;
  const seen = new Set();
  while (cur && !seen.has(cur)) {
    if (cur === draggingUuid) return true;
    seen.add(cur);
    cur = byUuid.value.get(cur)?.parentUuid || '';
  }
  return false;
}

function onRowDragOver(event, row) {
  const dragging = dragState.draggingUuid;
  if (!dragging || treeFilter.value.trim() || isDescendantOrSelf(row.noteUuid, dragging)) {
    dragState.targetUuid = '';
    dragState.zone = '';
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const rect = event.currentTarget.getBoundingClientRect();
  const ratio = (event.clientY - rect.top) / Math.max(1, rect.height);
  const zone = ratio < 0.26 ? 'before' : ratio > 0.74 ? 'after' : 'inside';
  dragState.targetUuid = row.noteUuid;
  dragState.zone = zone;

  window.clearTimeout(dragExpandTimer);
  if (zone === 'inside' && row.childCount > 0 && !expandedIds.value.has(row.noteUuid)) {
    dragExpandTimer = window.setTimeout(() => {
      expandedIds.value.add(row.noteUuid);
      persistExpandedIds();
    }, 500);
  }
}

async function onRowDrop(event, row) {
  event.preventDefault();
  event.stopPropagation();
  window.clearTimeout(dragExpandTimer);
  const dragging = dragState.draggingUuid;
  const zone = dragState.zone;
  dragState.draggingUuid = '';
  dragState.targetUuid = '';
  dragState.zone = '';
  if (!dragging || !zone || isDescendantOrSelf(row.noteUuid, dragging)) return;

  try {
    let payload;
    if (zone === 'inside') {
      payload = { targetParentUuid: row.noteUuid };
      expandedIds.value.add(row.noteUuid);
      persistExpandedIds();
    } else if (zone === 'before') {
      payload = { targetParentUuid: row.parentUuid || '', beforeUuid: row.noteUuid };
    } else {
      payload = { targetParentUuid: row.parentUuid || '', afterUuid: row.noteUuid };
    }
    const data = await Api.moveKbNote(dragging, payload);
    treeItems.value = data.items || [];
    if (activeNoteUuid.value === dragging) {
      const refreshed = await Api.kbNote(dragging);
      if (refreshed?.note) noteDetails[dragging] = refreshed.note;
    }
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function clearDragState() {
  window.clearTimeout(dragExpandTimer);
  dragState.draggingUuid = '';
  dragState.targetUuid = '';
  dragState.zone = '';
}

function startSidebarResize(event) {
  event.preventDefault();
  const startX = event.clientX;
  const startW = treeWidth.value;
  const onMove = (e) => {
    treeWidth.value = Math.max(210, Math.min(440, startW + (e.clientX - startX)));
  };
  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

// Global Ctrl/Cmd+P full-text search
watch(quickSearchQuery, (q) => {
  window.clearTimeout(quickSearchTimer);
  const trimmed = q.trim();
  if (!trimmed) {
    quickSearchResults.value = treeItems.value.slice(0, 20);
    quickSearchIndex.value = 0;
    return;
  }
  quickSearchLoading.value = true;
  quickSearchTimer = window.setTimeout(async () => {
    try {
      const res = await Api.kbNotesSearch(trimmed, 40);
      quickSearchResults.value = res.items || [];
      quickSearchIndex.value = 0;
    } catch {
      quickSearchResults.value = [];
    } finally {
      quickSearchLoading.value = false;
    }
  }, 160);
});

function openQuickSearch() {
  quickSearchQuery.value = '';
  quickSearchResults.value = treeItems.value.slice(0, 20);
  quickSearchIndex.value = 0;
  quickSearchOpen.value = true;
}

function pickQuickSearchResult(item = quickSearchResults.value[quickSearchIndex.value]) {
  if (!item) return;
  quickSearchOpen.value = false;
  void openNote(item.noteUuid, { enterMobileEditor: true });
}

function onGlobalKeydown(event) {
  const mod = event.metaKey || event.ctrlKey;
  if (mod && event.key.toLowerCase() === 'p') {
    event.preventDefault();
    openQuickSearch();
    return;
  }
  if (mod && event.key.toLowerCase() === 's' && activeNoteUuid.value) {
    event.preventDefault();
    // Failures and conflicts report themselves; only a real save is confirmed.
    void flushSave(activeNoteUuid.value).then((saved) => {
      if (saved) ElMessage.success({ message: '已保存', duration: 900 });
    });
  }
}

function flushAllSaves() {
  for (const uuid of Object.keys(savingStates)) {
    if (savingStates[uuid] === 'dirty' || savingStates[uuid] === 'error') void flushSave(uuid);
  }
}

function onBeforeUnload(event) {
  if (!Object.keys(savingStates).some(hasUnsavedChanges)) return;
  flushAllSaves();
  event.preventDefault();
  event.returnValue = '';
}

function onExternalOpenNote(event) {
  const targetUuid = event.detail?.noteUuid || event.detail?.id;
  if (targetUuid) {
    void loadTree({ selectFirstIfEmpty: false }).then(() => openNote(targetUuid, { enterMobileEditor: true }));
  }
}

function openImportDialog(parentRow = null) {
  contextMenu.value.open = false;
  importParent.value = parentRow
    ? { uuid: parentRow.noteUuid, title: parentRow.path || parentRow.title }
    : { uuid: '', title: '' };
  importDialogOpen.value = true;
}

async function onImported(res) {
  await loadTree({ selectFirstIfEmpty: false });
  const firstUuid = res?.firstNoteUuid || res?.rootNoteUuids?.[0] || treeItems.value[0]?.noteUuid;
  if (firstUuid) {
    expandedIds.value.add(firstUuid);
    persistExpandedIds();
    await openNote(firstUuid, { enterMobileEditor: !isPhone.value });
  }
}

onMounted(() => {
  const urlNote = new URLSearchParams(window.location.search).get('note') || '';
  const pendingUuid = window.__openbearPendingKbNoteUuid || urlNote;
  if (pendingUuid) {
    window.__openbearPendingKbNoteUuid = '';
    void loadTree({ selectFirstIfEmpty: false }).then(() => openNote(pendingUuid, { enterMobileEditor: true }));
  } else {
    void loadTree();
  }
  window.addEventListener('keydown', onGlobalKeydown);
  window.addEventListener('openbear:open-kb-note', onExternalOpenNote);
  window.addEventListener('beforeunload', onBeforeUnload);
});

onDeactivated(flushAllSaves);

let activatedBefore = false;
onActivated(() => {
  syncMobileHeaderState();
  const pendingUuid = window.__openbearPendingKbNoteUuid;
  if (pendingUuid) {
    window.__openbearPendingKbNoteUuid = '';
    void loadTree({ selectFirstIfEmpty: false }).then(() => openNote(pendingUuid, { enterMobileEditor: true }));
  } else if (activatedBefore && activeNoteUuid.value && !hasUnsavedChanges(activeNoteUuid.value)) {
    // A capture elsewhere may have appended to the open note while this page was hidden.
    void openNote(activeNoteUuid.value, { enterMobileEditor: false });
  }
  activatedBefore = true;
});

onBeforeUnmount(() => {
  emit('mobile-header-ready', false);
  window.removeEventListener('keydown', onGlobalKeydown);
  window.removeEventListener('openbear:open-kb-note', onExternalOpenNote);
  window.removeEventListener('beforeunload', onBeforeUnload);
  flushAllSaves();
});
</script>

<template>
  <section class="kb-notes-page" aria-label="笔记">
    <!-- Mobile Header when in Tree list mode -->
    <AdminPageHeader
      v-if="isPhone && !mobileEditing"
      title="笔记"
      :subtitle="`${treeItems.length} 篇文档`"
      @mobile-header-ready="onAdminHeaderReady"
    >
      <template #mobile-navigation><slot name="mobile-navigation" /></template>
      <template #actions>
        <el-button size="small" @click="openQuickSearch">全文搜索</el-button>
        <el-button size="small" @click="openImportDialog()">导入</el-button>
      </template>
      <template #primary>
        <el-button type="primary" size="small" @click="createNote()">新建</el-button>
      </template>
    </AdminPageHeader>

    <div class="kb-notes-body" :class="{ 'is-phone': isPhone }">
      <!-- Left Infinite-depth Document Tree -->
      <aside
        v-show="isPhone ? !mobileEditing : !treeCollapsed"
        class="kb-tree-pane"
        :style="!isPhone ? { width: `${treeWidth}px` } : undefined"
        aria-label="笔记文档树"
      >
        <header v-if="!isPhone" class="kb-tree-header">
          <div class="kb-tree-title">
            <strong>文档树</strong>
            <span v-if="treeItems.length" class="kb-tree-count">{{ treeItems.length }}</span>
          </div>
          <div class="kb-tree-tools">
            <button
              v-if="activeNoteUuid"
              type="button"
              class="kb-tool-btn"
              title="定位当前打开的文档"
              aria-label="定位当前文档"
              @click="locateActiveInTree"
            >
              <Crosshair :size="13.5" />
            </button>
            <button
              v-if="expandedIds.size > 0"
              type="button"
              class="kb-tool-btn"
              title="折叠全部目录"
              aria-label="折叠全部目录"
              @click="collapseAllTree"
            >
              <ChevronsDownUp :size="13.5" />
            </button>
            <button type="button" class="kb-tool-btn" title="全局搜索 (Ctrl+P)" aria-label="全局全文搜索" @click="openQuickSearch">
              <Search :size="13.5" />
            </button>
            <button type="button" class="kb-tool-btn" title="导入 Markdown / ZIP" aria-label="导入笔记" @click="openImportDialog()">
              <Upload :size="13.5" />
            </button>
            <button type="button" class="kb-tool-btn" title="新建顶层笔记" aria-label="新建顶层笔记" @click="createNote()">
              <Plus :size="14.5" />
            </button>
            <button type="button" class="kb-tool-btn" title="收起侧栏" aria-label="收起文档树" @click="treeCollapsed = true">
              <PanelLeftClose :size="13.5" />
            </button>
          </div>
        </header>

        <div class="kb-tree-filter">
          <Search :size="12.5" class="kb-filter-icon" />
          <input v-model="treeFilter" type="search" placeholder="筛选文档…" aria-label="过滤笔记标题" />
          <button v-if="treeFilter" type="button" class="kb-filter-clear" aria-label="清空过滤" @click="treeFilter = ''">×</button>
        </div>

        <div
          ref="treeScrollRef"
          class="kb-tree-scroll"
          role="tree"
          @dragover.self.prevent
          @drop.self="clearDragState"
        >
          <div v-if="treeLoading && !treeItems.length" class="kb-tree-empty">正在读取笔记…</div>
          <div v-else-if="!visibleTreeRows.length" class="kb-tree-empty">
            <template v-if="treeFilter">没有匹配的笔记</template>
            <template v-else>
              <p>还没有笔记</p>
              <div class="kb-tree-empty-actions">
                <el-button size="small" type="primary" @click="createNote()">新建笔记</el-button>
                <el-button size="small" @click="openImportDialog()">导入 Markdown</el-button>
              </div>
            </template>
          </div>

          <div
            v-for="row in visibleTreeRows"
            :key="row.noteUuid"
            :data-note-uuid="row.noteUuid"
            class="kb-tree-row"
            :class="{
              'is-active': activeNoteUuid === row.noteUuid,
              'drop-before': dragState.targetUuid === row.noteUuid && dragState.zone === 'before',
              'drop-inside': dragState.targetUuid === row.noteUuid && dragState.zone === 'inside',
              'drop-after': dragState.targetUuid === row.noteUuid && dragState.zone === 'after',
            }"
            :style="{ '--kb-depth': row.filtered ? 0 : Math.min(row.depth || 0, 9) }"
            role="treeitem"
            :aria-selected="activeNoteUuid === row.noteUuid"
            :draggable="!isPhone && !treeFilter"
            @dragstart="onRowDragStart($event, row)"
            @dragover="onRowDragOver($event, row)"
            @drop="onRowDrop($event, row)"
            @dragend="clearDragState"
            @contextmenu="openRowMenu($event, row)"
          >
            <button
              v-if="row.childCount > 0 && !row.filtered"
              type="button"
              class="kb-row-chevron"
              :class="{ 'is-open': expandedIds.has(row.noteUuid) }"
              :aria-label="expandedIds.has(row.noteUuid) ? '收起子笔记' : '展开子笔记'"
              @click.stop="toggleExpand(row.noteUuid)"
            >
              <ChevronRight :size="12" />
            </button>
            <span v-else class="kb-row-spacer" />

            <button
              type="button"
              class="kb-row-icon-btn"
              title="修改图标"
              aria-label="修改图标"
              @click.stop="openTreeIconPicker($event, row)"
            >
              <NoteIcon
                :icon="row.icon"
                :has-children="row.childCount > 0"
                :is-root="!row.parentUuid"
                :expanded="expandedIds.has(row.noteUuid)"
                :size="15"
              />
            </button>

            <button
              type="button"
              class="kb-row-main"
              :title="row.path"
              @click="openNote(row.noteUuid, { enterMobileEditor: true })"
            >
              <span class="kb-row-title">{{ row.title }}</span>
              <span v-if="row.pinned" class="kb-row-pin" title="已置顶">★</span>
              <Lock v-if="row.isReadonly" :size="11" class="kb-row-lock" title="只读锁定" />
            </button>

            <span v-if="row.childCount > 0" class="kb-row-subcount">{{ row.childCount }}</span>

            <div class="kb-row-actions">
              <button
                type="button"
                class="kb-row-act is-more"
                title="更多操作"
                aria-label="更多操作"
                @click.stop="openRowMenu($event, row)"
              >
                <MoreHorizontal :size="13.5" />
              </button>
              <button
                type="button"
                class="kb-row-act"
                title="新建子笔记"
                aria-label="新建子笔记"
                @click.stop="createNote({ parentUuid: row.noteUuid })"
              >
                <Plus :size="14" />
              </button>
            </div>
          </div>
        </div>

        <div v-if="!isPhone" class="kb-tree-resizer" @pointerdown="startSidebarResize" />
      </aside>

      <!-- Right Main Editor Area -->
      <main v-show="!isPhone || mobileEditing" class="kb-main-pane">
        <!-- Desktop Multi-Tab Bar -->
        <div v-if="!isPhone && openTabs.length" class="kb-tabs-bar" role="tablist" aria-label="已打开的笔记">
          <button
            v-if="treeCollapsed"
            type="button"
            class="kb-tool-btn kb-expand-tree-btn"
            title="展开文档树"
            aria-label="展开文档树"
            @click="treeCollapsed = false"
          >
            <PanelLeftOpen :size="15" />
          </button>

          <div class="kb-tabs-track">
            <div
              v-for="tabItem in openTabs"
              :key="tabItem.noteUuid"
              class="kb-tab"
              :class="{ 'is-active': activeNoteUuid === tabItem.noteUuid }"
              role="tab"
              :aria-selected="activeNoteUuid === tabItem.noteUuid"
              @click="openNote(tabItem.noteUuid)"
              @auxclick.middle.prevent="closeTab(tabItem.noteUuid, $event)"
            >
              <NoteIcon v-if="hasTabCustomIcon(tabItem)" :icon="tabItem.icon" :size="14" class="kb-tab-icon" />
              <span class="kb-tab-title">{{ tabItem.title || '未命名笔记' }}</span>
              <span v-if="savingStates[tabItem.noteUuid] === 'dirty'" class="kb-tab-dirty" title="正在等待自动保存" />
              <span
                v-else-if="savingStates[tabItem.noteUuid] === 'error' || savingStates[tabItem.noteUuid] === 'conflict'"
                class="kb-tab-dirty is-error"
                :title="savingStates[tabItem.noteUuid] === 'conflict' ? '版本冲突，修改尚未保存' : '保存失败，修改尚未保存'"
              />
              <button
                type="button"
                class="kb-tab-close"
                aria-label="关闭标签页"
                @click="closeTab(tabItem.noteUuid, $event)"
              >
                <X :size="12" />
              </button>
            </div>
          </div>

          <button type="button" class="kb-tool-btn" title="新建笔记" aria-label="新建笔记" @click="createNote()">
            <Plus :size="15" />
          </button>
        </div>

        <!-- Editor or Empty Welcome -->
        <SiyuanLikeEditor
          v-if="activeNote"
          ref="editorRef"
          :note="activeNote"
          :saving-state="savingStates[activeNote.noteUuid] || 'saved'"
          :phone="isPhone"
          @change-title="onEditorChangeTitle"
          @change-icon="onEditorChangeIcon"
          @change-content="onEditorChangeContent"
          @toggle-readonly="onToggleReadonly"
          @navigate-note="openNote($event, { enterMobileEditor: true })"
          @export-note="exportNote($event, false)"
        >
          <template #leading>
            <button
              v-if="isPhone"
              type="button"
              class="sy-icon-btn"
              aria-label="返回笔记列表"
              @click="flushSave(activeNote.noteUuid); mobileEditing = false"
            >
              <ChevronLeft :size="17" />
              <span>列表</span>
            </button>
            <button
              v-else-if="treeCollapsed && !openTabs.length"
              type="button"
              class="sy-icon-btn"
              title="展开文档树"
              @click="treeCollapsed = false"
            >
              <PanelLeftOpen :size="15" />
            </button>
          </template>
          <template #trailing>
            <button
              v-if="isPhone"
              type="button"
              class="sy-icon-btn"
              aria-label="打开文档大纲"
              @click="mobileOutlineOpen = true"
            >
              <ListTree :size="15" />
            </button>
          </template>
        </SiyuanLikeEditor>

        <div v-else class="kb-welcome">
          <button
            v-if="!isPhone && treeCollapsed"
            type="button"
            class="kb-welcome-expand"
            @click="treeCollapsed = false"
          >
            <PanelLeftOpen :size="15" /> 展开文档树
          </button>
          <div class="kb-welcome-card">
            <h2>全局树状笔记</h2>
            <p>无限层级复合文档树 + 所见即所得 Markdown 编辑器，支持表格直编、代码块高亮复制、<code>@</code> 引用与会话摘录溯源。</p>
            <div class="kb-welcome-actions">
              <el-button type="primary" @click="createNote()">新建第一篇笔记</el-button>
              <el-button @click="openImportDialog()">导入 Markdown / ZIP</el-button>
            </div>
          </div>
        </div>
      </main>
    </div>

    <!-- Tree Context Menu -->
    <FloatingPanel
      :open="contextMenu.open"
      :anchor="contextMenu.anchor"
      placement="bottom-start"
      :width="196"
      label="笔记操作菜单"
      @close="contextMenu.open = false"
    >
      <div v-if="contextMenu.row" class="kb-ctx-menu" role="menu">
        <button type="button" role="menuitem" @click="contextMenu.open = false; createNote({ parentUuid: contextMenu.row.noteUuid })">
          <FilePlus2 :size="14" /><span>新建子笔记</span>
        </button>
        <button type="button" role="menuitem" @click="contextMenu.open = false; createNote({ parentUuid: contextMenu.row.parentUuid || '', afterUuid: contextMenu.row.noteUuid })">
          <FolderPlus :size="14" /><span>新建同级笔记</span>
        </button>
        <hr />
        <button type="button" role="menuitem" @click="treeIconPicker = { open: true, anchor: contextMenu.anchor, row: contextMenu.row }; contextMenu.open = false">
          <Smile :size="14" /><span>修改图标…</span>
        </button>
        <button type="button" role="menuitem" @click="renameRow(contextMenu.row)">
          <Edit3 :size="14" /><span>重命名</span>
        </button>
        <button type="button" role="menuitem" @click="togglePinRow(contextMenu.row)">
          <Pin :size="14" /><span>{{ contextMenu.row.pinned ? '取消置顶' : '同级置顶' }}</span>
        </button>
        <button type="button" role="menuitem" @click="openMoveForRow(contextMenu.row)">
          <FolderInput :size="14" /><span>移动到…</span>
        </button>
        <button type="button" role="menuitem" @click="openImportDialog(contextMenu.row)">
          <Upload :size="14" /><span>导入子笔记…</span>
        </button>
        <hr />
        <button type="button" role="menuitem" @click="exportNote(contextMenu.row, false)">
          <Download :size="14" /><span>导出 Markdown (.md)</span>
        </button>
        <button v-if="contextMenu.row.childCount > 0" type="button" role="menuitem" @click="exportNote(contextMenu.row, true)">
          <Download :size="14" /><span>连同子笔记导出 (.zip)</span>
        </button>
        <hr />
        <button type="button" class="is-danger" role="menuitem" @click="deleteRow(contextMenu.row)">
          <Trash2 :size="14" /><span>删除笔记</span>
        </button>
      </div>
    </FloatingPanel>

    <!-- Tree Inline Icon Picker -->
    <FloatingPanel
      :open="treeIconPicker.open"
      :anchor="treeIconPicker.anchor"
      placement="bottom-start"
      :width="244"
      label="选择笔记图标"
      @close="treeIconPicker.open = false"
    >
      <div class="sy-emoji-grid">
        <button
          v-for="emo in TREE_EMOJIS"
          :key="emo"
          type="button"
          class="sy-emoji-cell"
          @click="applyTreeIcon(emo)"
        >
          {{ emo }}
        </button>
        <button
          v-if="treeIconPicker.row?.icon"
          type="button"
          class="sy-emoji-clear"
          @click="applyTreeIcon('')"
        >
          恢复默认图标
        </button>
      </div>
    </FloatingPanel>

    <!-- Quick Full-Text Search Dialog (Ctrl+P) -->
    <el-dialog
      v-model="quickSearchOpen"
      title="全文搜索笔记"
      width="min(560px, calc(100vw - 24px))"
      append-to-body
      class="mobile-viewport-dialog"
    >
      <el-input
        v-model="quickSearchQuery"
        clearable
        autofocus
        placeholder="输入标题或正文关键词（支持多词空格组合）…"
        @keydown.down.prevent="quickSearchIndex = (quickSearchIndex + 1) % Math.max(1, quickSearchResults.length)"
        @keydown.up.prevent="quickSearchIndex = (quickSearchIndex - 1 + Math.max(1, quickSearchResults.length)) % Math.max(1, quickSearchResults.length)"
        @keydown.enter.prevent="pickQuickSearchResult()"
      />
      <div class="kb-search-results" role="listbox">
        <div v-if="quickSearchLoading" class="kb-tree-empty">搜索中…</div>
        <div v-else-if="!quickSearchResults.length" class="kb-tree-empty">没有找到匹配的笔记</div>
        <button
          v-for="(item, idx) in quickSearchResults"
          :key="item.noteUuid"
          type="button"
          class="kb-search-hit"
          :class="{ 'is-selected': idx === quickSearchIndex }"
          @pointermove="quickSearchIndex = idx"
          @click="pickQuickSearchResult(item)"
        >
          <div class="kb-search-hit-head">
            <span class="kb-search-hit-title">
              <NoteIcon :icon="item.icon" :has-children="item.childCount > 0" :size="14" />
              <span>{{ item.title }}</span>
            </span>
            <small>{{ item.path }}</small>
          </div>
          <p v-if="item.snippet || item.summary" class="kb-search-hit-snippet">{{ item.snippet || item.summary }}</p>
        </button>
      </div>
    </el-dialog>

    <!-- Mobile Outline Drawer -->
    <el-drawer
      v-model="mobileOutlineOpen"
      direction="btt"
      size="min(62dvh, 420px)"
      title="文档大纲"
      append-to-body
      class="kb-mobile-outline-sheet"
    >
      <div v-if="mobileOutlineItems.length" class="sy-outline-list">
        <button
          v-for="h in mobileOutlineItems"
          :key="`${h.index}:${h.text}`"
          type="button"
          class="sy-outline-item"
          :style="{ '--toc-level': h.level }"
          @click="mobileOutlineOpen = false"
        >
          {{ h.text }}
        </button>
      </div>
      <div v-else class="sy-outline-empty">正文中输入 # 标题后自动生成大纲</div>
    </el-drawer>

    <NoteImportDialog
      v-model="importDialogOpen"
      :target-parent-uuid="importParent.uuid"
      :target-parent-title="importParent.title"
      @imported="onImported"
    />

    <NoteMoveDialog
      v-model="moveDialogOpen"
      :moving-note="movingRow"
      :tree-items="treeItems"
      :busy="moveBusy"
      @confirm="confirmMoveDialog"
    />
  </section>
</template>

<style scoped>
.kb-notes-page {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  background: var(--ob-surface, #ffffff);
  color: var(--ob-text, #222222);
}

.kb-notes-page svg {
  display: inline-block;
  vertical-align: middle;
  flex-shrink: 0;
}

.kb-notes-body {
  display: flex;
  flex: 1 1 0%;
  min-width: 0;
  min-height: 0;
}

/* SiYuan-inspired subtle muted Tree Sidebar */
.kb-tree-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: none;
  min-height: 0;
  border-right: 1px solid var(--ob-border);
  background: var(--ob-bg);
  user-select: none;
}

.kb-notes-body.is-phone .kb-tree-pane {
  width: 100%;
  flex: 1 1 0%;
  border-right: 0;
  background: var(--ob-surface, #ffffff);
}

.kb-tree-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 35px;
  padding: 0 8px 0 12px;
  gap: 4px;
  flex: none;
}

.kb-tree-title {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  color: var(--ob-text, #222222);
}

.kb-tree-title strong {
  font-weight: 600;
}

.kb-tree-count {
  font-size: 11px;
  color: var(--ob-text-muted, #8c959f);
  font-variant-numeric: tabular-nums;
}

.kb-tree-tools {
  display: flex;
  align-items: center;
  gap: 1px;
}

.kb-tool-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--ob-text-subtle, #5f6368);
  opacity: 0.78;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease, opacity 0.12s ease;
}

.kb-tool-btn:hover {
  opacity: 1;
  background: var(--ob-hover, rgba(127, 127, 127, 0.13));
  color: var(--ob-text, #222222);
}

/* Sleek low-interference filter input */
.kb-tree-filter {
  position: relative;
  display: flex;
  align-items: center;
  margin: 0 8px 5px;
  padding: 0 8px;
  height: 27px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: rgb(var(--ob-border-rgb) / 0.055);
  transition: border-color 0.14s ease, background 0.14s ease;
}

.kb-tree-filter:focus-within {
  background: var(--ob-surface);
  border-color: rgb(var(--ob-blue-rgb) / 0.48);
}

.kb-filter-icon {
  color: var(--ob-text-muted, #8c959f);
  flex: none;
}

.kb-tree-filter input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: none;
  background: transparent;
  color: inherit;
  font-size: 12px;
  padding: 0 5px;
}

.kb-tree-filter input::placeholder {
  color: var(--ob-text-muted, #8c959f);
}

.kb-filter-clear {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--ob-text-muted, #8c959f);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
}

.kb-filter-clear:hover {
  color: var(--ob-text, #222222);
  background: var(--ob-hover, rgba(127, 127, 127, 0.15));
}

.kb-tree-scroll {
  flex: 1 1 0%;
  min-height: 0;
  overflow-y: auto;
  padding: 2px 6px 18px;
}

/* SiYuan exact 28px tree row with 16px per-level indent */
.kb-tree-row {
  position: relative;
  display: flex;
  align-items: center;
  min-height: 28px;
  margin: 1px 0;
  padding-left: calc(var(--kb-depth, 0) * 16px + 2px);
  padding-right: 3px;
  border-radius: 5px;
  color: var(--ob-text, #222222);
  transition: background 0.1s ease;
}

.kb-tree-row:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.09));
}

.kb-tree-row.is-active {
  background: var(--ob-selected);
  color: var(--ob-text);
  font-weight: 500;
}

.kb-tree-row.drop-inside {
  outline: 1.5px solid var(--ob-blue, #2563eb);
  background: rgba(37, 99, 235, 0.08);
}

.kb-tree-row.drop-before::before,
.kb-tree-row.drop-after::after {
  content: '';
  position: absolute;
  left: calc(var(--kb-depth, 0) * 16px + 8px);
  right: 6px;
  height: 2px;
  background: var(--ob-blue, #2563eb);
  pointer-events: none;
}

.kb-tree-row.drop-before::before { top: -1px; }
.kb-tree-row.drop-after::after { bottom: -1px; }

.kb-row-chevron,
.kb-row-spacer {
  width: 18px;
  height: 20px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.kb-row-chevron {
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--ob-text-muted, #8c959f);
  cursor: pointer;
  padding: 0;
  transition: background 0.12s ease, color 0.12s ease;
}

.kb-row-chevron svg {
  transition: transform 0.15s ease;
}

.kb-row-chevron.is-open svg {
  transform: rotate(90deg);
}

.kb-row-chevron:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.16));
  color: var(--ob-text, #222222);
}

.kb-row-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  margin-right: 3px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  cursor: pointer;
  flex: none;
  transition: background 0.12s ease;
}

.kb-row-icon-btn:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.16));
}

.kb-row-main {
  flex: 1 1 0%;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 4px 0 1px;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
  line-height: 20px;
  text-align: left;
  cursor: pointer;
}

.kb-row-title {
  flex: 1 1 0%;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-row-pin {
  font-size: 10px;
  color: #eab308;
  flex: none;
}

.kb-row-lock {
  color: var(--ob-warning, #d97706);
  flex: none;
}

.kb-row-subcount {
  font-size: 11px;
  color: var(--ob-text-muted, #8c959f);
  opacity: 0.72;
  flex: none;
  padding: 0 5px;
  font-variant-numeric: tabular-nums;
}

.kb-row-actions {
  display: none;
  align-items: center;
  gap: 1px;
  flex: none;
}

/* On desktop hover: swap subcount with actions so row title NEVER jumps */
@media (hover: hover) {
  .kb-tree-row:hover .kb-row-subcount,
  .kb-tree-row:focus-within .kb-row-subcount {
    display: none;
  }

  .kb-tree-row:hover .kb-row-actions,
  .kb-tree-row:focus-within .kb-row-actions {
    display: flex;
  }
}

.kb-row-act {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 21px;
  height: 21px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--ob-text-subtle, #5f6368);
  cursor: pointer;
  padding: 0;
  transition: background 0.12s ease, color 0.12s ease;
}

.kb-row-act:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.18));
  color: var(--ob-text, #222222);
}

.kb-tree-resizer {
  position: absolute;
  top: 0;
  right: -3px;
  bottom: 0;
  width: 6px;
  cursor: col-resize;
  z-index: 5;
}

.kb-tree-resizer:hover {
  background: rgb(var(--ob-blue-rgb) / 0.28);
}

.kb-tree-empty {
  padding: 28px 14px;
  text-align: center;
  font-size: 12.5px;
  color: var(--ob-text-muted, #8c959f);
}

.kb-tree-empty-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 10px;
}

/* Right Main Pane & SiYuan-style Tab Bar */
.kb-main-pane {
  display: flex;
  flex-direction: column;
  flex: 1 1 0%;
  min-width: 0;
  min-height: 0;
}

.kb-tabs-bar {
  display: flex;
  align-items: stretch;
  gap: 2px;
  height: 35px;
  padding: 0 6px;
  border-bottom: 1px solid var(--ob-border);
  background: var(--ob-bg);
  flex: none;
}

.kb-tabs-bar > .kb-tool-btn {
  align-self: center;
}

.kb-tabs-track {
  display: flex;
  align-items: stretch;
  gap: 1px;
  flex: 1 1 0%;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}

.kb-tabs-track::-webkit-scrollbar {
  display: none;
}

.kb-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 196px;
  padding: 0 7px 0 11px;
  font-size: 12.5px;
  color: var(--ob-text-subtle, #5f6368);
  cursor: pointer;
  white-space: nowrap;
  flex: none;
  user-select: none;
  transition: background 0.12s ease, color 0.12s ease;
}

.kb-tab:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.08));
  color: var(--ob-text, #222222);
}

.kb-tab.is-active {
  background: var(--ob-surface, #ffffff);
  color: var(--ob-text, #222222);
  font-weight: 500;
}

.kb-tab.is-active::after {
  content: '';
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 0;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: var(--ob-blue, #2563eb);
}

.kb-tab-title {
  overflow: hidden;
  text-overflow: ellipsis;
}

.kb-tab-dirty {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ob-blue, #2563eb);
  flex: none;
}

.kb-tab-dirty.is-error {
  background: var(--ob-danger);
}

/* Inactive tabs hide close button until hover (SiYuan tab style) */
.kb-tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  opacity: 0;
  cursor: pointer;
  padding: 0;
  transition: opacity 0.12s ease, background 0.12s ease;
}

.kb-tab.is-active .kb-tab-close {
  opacity: 0.52;
}

.kb-tab:hover .kb-tab-close {
  opacity: 0.68;
}

.kb-tab-close:hover {
  opacity: 1 !important;
  background: var(--ob-hover, rgba(127, 127, 127, 0.18));
}

.kb-welcome {
  position: relative;
  flex: 1 1 0%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.kb-welcome-expand {
  position: absolute;
  top: 12px;
  left: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--ob-border, rgba(127, 127, 127, 0.22));
  border-radius: 7px;
  background: transparent;
  color: var(--ob-text-subtle, #6e7781);
  font-size: 12px;
  cursor: pointer;
}

.kb-welcome-card {
  max-width: 440px;
  text-align: center;
}

.kb-welcome-card h2 {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: 650;
}

.kb-welcome-card p {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.65;
  color: var(--ob-text-subtle, #6e7781);
}

.kb-welcome-actions {
  display: flex;
  justify-content: center;
  gap: 10px;
}

/* Context Menu & Search Results */
.kb-ctx-menu {
  padding: 4px;
  display: flex;
  flex-direction: column;
}

.kb-ctx-menu button {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 10px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--ob-text, #222222);
  font-size: 12.5px;
  cursor: pointer;
  text-align: left;
}

.kb-ctx-menu button:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.12));
}

.kb-ctx-menu button.is-danger {
  color: var(--ob-danger, #dc2626);
}

.kb-ctx-menu hr {
  border: 0;
  border-top: 1px solid var(--ob-border-soft, rgba(127, 127, 127, 0.15));
  margin: 4px 2px;
}

.kb-search-results {
  margin-top: 10px;
  max-height: 340px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.kb-search-hit {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--ob-text, #222222);
  text-align: left;
  cursor: pointer;
}

.kb-search-hit.is-selected,
.kb-search-hit:hover {
  background: var(--ob-hover, rgba(127, 127, 127, 0.12));
}

.kb-search-hit-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
  font-weight: 500;
}

.kb-search-hit-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.kb-search-hit-head small {
  font-size: 11px;
  color: var(--ob-text-muted, #8c959f);
  font-weight: 400;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-search-hit-snippet {
  margin: 0;
  font-size: 12px;
  color: var(--ob-text-subtle, #6e7781);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.is-spinning {
  animation: kb-spin 0.9s linear infinite;
}

@keyframes kb-spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 760px), (any-pointer: coarse) {
  .kb-tree-row {
    min-height: 42px;
  }
  .kb-row-main {
    height: 42px;
    font-size: 14px;
  }
  .kb-row-chevron,
  .kb-row-spacer {
    width: 30px;
    height: 36px;
  }
  .kb-row-icon-btn {
    width: 28px;
    height: 28px;
  }
  .kb-row-actions {
    display: flex;
  }
  .kb-row-act {
    width: 32px;
    height: 32px;
  }
  .kb-tree-filter {
    height: 36px;
    margin: 4px 10px 6px;
  }
  .kb-tree-filter input {
    font-size: 16px;
  }
}
</style>
