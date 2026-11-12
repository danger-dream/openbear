<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import {
  Check,
  CornerUpRight,
  ExternalLink,
  Globe,
  Pencil,
  Pin,
  Plus,
  Search,
  StickyNote,
  TextQuote,
  TextSelect,
  Trash2,
  X,
} from '@lucide/vue';
import { Api, apiError } from '../../api.js';
import { notesSettings, setNotesSelectionPopover } from '../../notesSettings.js';
import { clampOverlayPosition } from './notesSelection.js';
import { useUserNotes } from './notesStore.js';

const emit = defineEmits(['locate', 'insert-text']);
const store = useUserNotes();

const phone = ref(false);
const popoverRef = ref(null);
const triggerRef = ref(null);
const pcInputRef = ref(null);
const mobileInputRef = ref(null);
const sheetInputRef = ref(null);

const composeText = ref('');
const searchOpen = ref(false);
const searchQuery = ref('');
const expandedQuotes = ref({});
const editingUuid = ref('');
const editingContent = ref('');
const sheetForm = ref(null); // { mode: 'create' | 'rename', sheetUuid: '', title: '' }
const customPos = ref(null); // { x, y } when dragged on desktop
const togglingSelection = ref(false);

let media = null;
let hoverCloseTimer = 0;
let dragState = null;

// Mobile bottom-sheet follow-finger swipe-down state (mirrors WorkItemsDrawer / ExtensionsMenu)
const sheetOffset = ref(0);
const sheetDragging = ref(false);
const sheetClosing = ref(false);
let sheetGesture = null;

const enabled = computed(() => Boolean(notesSettings.enabled && store));
const sessionUsable = computed(() => Boolean(store?.usableConversationUuid.value));
const activeScope = computed(() => store?.state.activeScope || 'session');
const activeSheet = computed(() => store?.activeSheet.value || null);
const badgeCount = computed(() => store?.badgeCount.value || 0);

const currentNotes = computed(() => {
  if (!store) return [];
  const raw = activeScope.value === 'global'
    ? (activeSheet.value?.notes || [])
    : store.state.sessionNotes;
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return raw;
  return raw.filter((item) =>
    `${item.quote || ''}\n${item.content || ''}\n${item.conversationTitle || ''}`.toLowerCase().includes(q),
  );
});

const popoverStyle = computed(() => {
  if (!customPos.value) return undefined;
  return {
    left: `${customPos.value.x}px`,
    top: `${customPos.value.y}px`,
    right: 'auto',
  };
});

const mobileSheetStyle = computed(() =>
  sheetOffset.value
    ? {
        '--notes-drag-y': `${sheetOffset.value}px`,
        transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`,
      }
    : undefined,
);

function updateMedia() {
  phone.value = Boolean(media?.matches);
  if (phone.value && store) {
    store.desktopOpen.value = false;
  } else if (!phone.value && store) {
    store.mobileOpen.value = false;
  }
}

function clearHoverTimer() {
  window.clearTimeout(hoverCloseTimer);
  hoverCloseTimer = 0;
}

function onTriggerEnter() {
  if (phone.value || !store) return;
  clearHoverTimer();
  if (!store.state.globalSheets.length) void store.refresh(true);
  store.desktopOpen.value = true;
}

function scheduleHoverClose() {
  if (phone.value || !store || store.desktopPinned.value) return;
  clearHoverTimer();
  hoverCloseTimer = window.setTimeout(() => {
    const activeEl = document.activeElement;
    if (activeEl && popoverRef.value?.contains(activeEl) && ['INPUT', 'TEXTAREA'].includes(activeEl.tagName)) {
      return;
    }
    store.desktopOpen.value = false;
  }, 220);
}

function onTriggerClick() {
  if (!store) return;
  if (phone.value) {
    store.openPanel({ mobile: true });
    return;
  }
  clearHoverTimer();
  if (store.desktopOpen.value && store.desktopPinned.value) {
    closeDesktop();
  } else {
    store.desktopOpen.value = true;
    store.desktopMinimized.value = false;
    store.desktopPinned.value = true;
  }
}

function togglePin() {
  if (!store) return;
  store.desktopPinned.value = !store.desktopPinned.value;
  if (!store.desktopPinned.value) customPos.value = null;
}

function toggleMinimize() {
  if (!store) return;
  store.desktopMinimized.value = !store.desktopMinimized.value;
}

function closeDesktop() {
  if (!store) return;
  clearHoverTimer();
  store.desktopOpen.value = false;
  store.desktopPinned.value = false;
  store.desktopMinimized.value = false;
  customPos.value = null;
}

function switchScope(scope) {
  if (!store) return;
  store.state.activeScope = scope;
  editingUuid.value = '';
  sheetForm.value = null;
}

function toggleSearch() {
  searchOpen.value = !searchOpen.value;
  if (!searchOpen.value) searchQuery.value = '';
}

async function toggleSelectionPopover() {
  if (togglingSelection.value) return;
  const previous = Boolean(notesSettings.selectionPopover);
  const next = !previous;
  togglingSelection.value = true;
  setNotesSelectionPopover(next);
  try {
    const res = await Api.updateSetting('ui.notesSelectionPopover', next);
    if (res?.ok === false) throw new Error(res.error || '保存失败');
    ElMessage.success(next ? '已开启划词浮框' : '已关闭划词浮框');
  } catch (err) {
    setNotesSelectionPopover(previous);
    ElMessage.error(apiError(err));
  } finally {
    togglingSelection.value = false;
  }
}

function selectSheet(sheetUuid) {
  if (!store) return;
  store.state.activeSheetUuid = sheetUuid;
  sheetForm.value = null;
}

async function openSheetCreator() {
  sheetForm.value = { mode: 'create', sheetUuid: '', title: '' };
  await nextTick();
  sheetInputRef.value?.focus?.();
}

async function openSheetRenamer() {
  const sheet = activeSheet.value;
  if (!sheet) return;
  sheetForm.value = { mode: 'rename', sheetUuid: sheet.sheetUuid, title: sheet.title };
  await nextTick();
  sheetInputRef.value?.focus?.();
}

async function submitSheetForm() {
  if (!sheetForm.value || !store) return;
  const title = sheetForm.value.title.trim();
  if (!title) return;
  try {
    if (sheetForm.value.mode === 'create') {
      await store.createSheet(title);
    } else {
      await store.renameSheet(sheetForm.value.sheetUuid, title);
    }
    sheetForm.value = null;
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

async function confirmDeleteSheet() {
  const sheet = activeSheet.value;
  if (!sheet || !store) return;
  if ((sheet.notes || []).length > 0) {
    try {
      await ElMessageBox.confirm(
        `确定删除全局便签「${sheet.title}」及其中的 ${sheet.notes.length} 条记录吗？`,
        '删除全局便签',
        { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
      );
    } catch {
      return;
    }
  }
  try {
    await store.removeSheet(sheet.sheetUuid);
    ElMessage.success('已删除便签页');
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function toggleQuoteExpand(noteUuid) {
  expandedQuotes.value = {
    ...expandedQuotes.value,
    [noteUuid]: !expandedQuotes.value[noteUuid],
  };
}

function beginEditNote(note) {
  editingUuid.value = note.noteUuid;
  editingContent.value = note.content || '';
}

function cancelEditNote() {
  editingUuid.value = '';
  editingContent.value = '';
}

async function saveEditNote(note) {
  if (!store) return;
  const nextContent = editingContent.value.trim();
  if (!nextContent && !note.quote) {
    ElMessage.warning('便签内容不能为空');
    return;
  }
  try {
    await store.updateNote(note.noteUuid, { content: nextContent });
    cancelEditNote();
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

async function handleCopyToGlobal(note) {
  if (!store) return;
  try {
    await store.copyToGlobal(note.noteUuid);
    const targetName = activeSheet.value?.title || '全局便签';
    ElMessage.success(`已转存到「${targetName}」`);
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

async function handleDeleteNote(note) {
  if (!store) return;
  try {
    await store.removeNote(note.noteUuid);
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function handleInsertNote(note) {
  const parts = [];
  if (note.quote) {
    parts.push(
      note.quote
        .split('\n')
        .map((line) => `> ${line}`)
        .join('\n'),
    );
  }
  if (note.content) parts.push(note.content);
  const text = parts.join('\n').trim();
  if (!text) return;
  if (phone.value && store) store.mobileOpen.value = false;
  emit('insert-text', text);
}

function isCrossConversation(note) {
  if (!note?.conversationUuid) return false;
  return note.conversationUuid !== store?.usableConversationUuid.value;
}

function shortConvTitle(title) {
  const s = String(title || '原会话').trim();
  return s.length > 12 ? `${s.slice(0, 12)}…` : s;
}

function formatNoteTime(ms) {
  const n = Number(ms || 0);
  if (!n) return '';
  const date = new Date(n);
  const now = new Date();
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  const hm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  if (sameDay) return hm;
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${hm}`;
}

function locateSource(note) {
  if (!note?.opId) return;
  const reopenMobile = Boolean(phone.value && store?.mobileOpen.value);
  if (reopenMobile && store) store.mobileOpen.value = false;
  emit('locate', {
    opId: note.opId,
    conversationUuid: note.conversationUuid || store?.usableConversationUuid.value || '',
    turnNumber: note.turnNumber || 0,
    reopenMobile,
  });
}

async function submitCompose() {
  if (!store || store.state.saving) return;
  const text = composeText.value.trim();
  const pending = store.draftQuote.value;
  if (!text && !pending?.quote) return;
  if (activeScope.value === 'session' && !sessionUsable.value) {
    ElMessage.info('请先发送消息创建会话，或切到「全局便签」记录');
    return;
  }
  try {
    await store.createNote({
      scope: activeScope.value,
      opId: pending?.opId || '',
      turnUuid: pending?.turnUuid || '',
      turnNumber: pending?.turnNumber || 0,
      quote: pending?.quote || '',
      content: text,
    });
    composeText.value = '';
    store.clearDraftQuote();
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function onPcComposeKeydown(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    void submitCompose();
  }
}

// Desktop sticky header drag
function onHeaderPointerDown(event) {
  if (phone.value || event.button !== 0 || event.target.closest('button, input')) return;
  const el = popoverRef.value;
  if (!el || !store) return;
  const rect = el.getBoundingClientRect();
  dragState = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    origX: rect.left,
    origY: rect.top,
    width: rect.width,
    height: rect.height,
    moved: false,
  };
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function onHeaderPointerMove(event) {
  if (!dragState || event.pointerId !== dragState.pointerId) return;
  const dx = event.clientX - dragState.startX;
  const dy = event.clientY - dragState.startY;
  if (!dragState.moved && Math.hypot(dx, dy) < 4) return;
  dragState.moved = true;
  if (store) store.desktopPinned.value = true;
  customPos.value = clampOverlayPosition(
    dragState.origX + dx,
    dragState.origY + dy,
    dragState.width,
    Math.min(dragState.height, window.innerHeight - 24),
    window.innerWidth,
    window.innerHeight,
    8,
  );
}

function onHeaderPointerUp() {
  dragState = null;
}

// Mobile bottom sheet drag-to-close
function sheetDragStart(event) {
  if (sheetClosing.value || event.button > 0 || event.target.closest('button, input, textarea')) return;
  sheetGesture = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    lastY: event.clientY,
    lastT: event.timeStamp,
    speed: 0,
  };
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function sheetDragMove(event) {
  if (!sheetGesture || event.pointerId !== sheetGesture.id) return;
  const dx = event.clientX - sheetGesture.x;
  const dy = event.clientY - sheetGesture.y;
  if (!sheetDragging.value) {
    if (Math.hypot(dx, dy) < 4) return;
    if (dy < 0 || Math.abs(dx) > Math.abs(dy)) {
      sheetDragCancel();
      return;
    }
    sheetDragging.value = true;
  }
  sheetGesture.speed = (event.clientY - sheetGesture.lastY) / Math.max(1, event.timeStamp - sheetGesture.lastT);
  sheetGesture.lastY = event.clientY;
  sheetGesture.lastT = event.timeStamp;
  sheetOffset.value = Math.max(0, dy);
}

function sheetDragCancel() {
  sheetGesture = null;
  sheetDragging.value = false;
  sheetOffset.value = 0;
}

function sheetDragEnd() {
  if (!sheetGesture) return;
  if (sheetOffset.value >= 84 || (sheetOffset.value > 24 && sheetGesture.speed > 0.5)) {
    sheetGesture = null;
    sheetDragging.value = false;
    sheetClosing.value = true;
    if (store) store.mobileOpen.value = false;
  } else {
    sheetDragCancel();
  }
}

function onSheetClosed() {
  sheetClosing.value = false;
  sheetOffset.value = 0;
}

function onGlobalKeydown(event) {
  if (event.key === 'Escape' && store?.desktopOpen.value && !phone.value) {
    if (sheetForm.value) sheetForm.value = null;
    else if (editingUuid.value) cancelEditNote();
    else closeDesktop();
  }
}

watch(
  () => store?.draftQuote.value,
  async (val) => {
    if (!val) return;
    await nextTick();
    if (phone.value) mobileInputRef.value?.focus?.();
    else pcInputRef.value?.focus?.();
  },
);

onMounted(() => {
  media = window.matchMedia?.('(max-width: 760px)') || null;
  updateMedia();
  media?.addEventListener?.('change', updateMedia);
  document.addEventListener('keydown', onGlobalKeydown);
});

onBeforeUnmount(() => {
  clearHoverTimer();
  media?.removeEventListener?.('change', updateMedia);
  document.removeEventListener('keydown', onGlobalKeydown);
});
</script>

<template>
  <template v-if="enabled">
    <!-- PC 右侧浮动栏入口（与任务面板、搜索、任务记忆同列对齐） -->
    <div
      class="notes-entry-wrap"
      @mouseenter="onTriggerEnter"
      @mouseleave="scheduleHoverClose"
    >
      <button
        ref="triggerRef"
        type="button"
        class="notes-entry"
        :class="{
          active: store.desktopOpen.value,
          populated: badgeCount > 0,
        }"
        :title="store.desktopPinned.value ? '便签（已钉住，点击收起）' : '便签（悬停速览 · 点击钉住）'"
        :aria-label="badgeCount ? `打开便签，共 ${badgeCount} 条` : '打开便签'"
        aria-haspopup="dialog"
        :aria-expanded="store.desktopOpen.value ? 'true' : 'false'"
        @click="onTriggerClick"
      >
        <StickyNote class="notes-entry-icon" :stroke-width="1.65" aria-hidden="true" />
        <span v-if="badgeCount" class="notes-entry-badge" aria-hidden="true">
          {{ badgeCount > 99 ? '99+' : badgeCount }}
        </span>
      </button>
    </div>

    <!-- PC 轻量悬浮便签面板 -->
    <Teleport to="body">
      <Transition name="notes-float">
        <aside
          v-if="!phone && store.desktopOpen.value"
          ref="popoverRef"
          class="notes-sticky-popover"
          :class="{ 'is-pinned': store.desktopPinned.value, 'is-minimized': store.desktopMinimized.value }"
          :style="popoverStyle"
          role="dialog"
          aria-label="对话便签"
          @mouseenter="clearHoverTimer"
          @mouseleave="scheduleHoverClose"
        >
          <header
            class="notes-head"
            title="拖动可移动位置 · 双击折叠"
            @pointerdown="onHeaderPointerDown"
            @pointermove="onHeaderPointerMove"
            @pointerup="onHeaderPointerUp"
            @pointercancel="onHeaderPointerUp"
            @dblclick="toggleMinimize"
          >
            <div class="notes-seg" role="tablist" aria-label="便签范围">
              <button
                type="button"
                role="tab"
                :aria-selected="activeScope === 'session'"
                :class="{ active: activeScope === 'session' }"
                @click="switchScope('session')"
              >
                <span>本会话</span>
                <small v-if="store.sessionCount.value">{{ store.sessionCount.value }}</small>
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="activeScope === 'global'"
                :class="{ active: activeScope === 'global' }"
                @click="switchScope('global')"
              >
                <span>全局</span>
                <small v-if="store.state.globalSheets.length">{{ store.state.globalSheets.length }}</small>
              </button>
            </div>
            <div class="notes-head-actions">
              <button
                type="button"
                class="notes-icon-btn notes-sel-toggle"
                :class="{ active: notesSettings.selectionPopover, 'is-off': !notesSettings.selectionPopover }"
                :disabled="togglingSelection"
                :title="notesSettings.selectionPopover ? '划词浮框：已开启（点击关闭）' : '划词浮框：已关闭（点击开启）'"
                :aria-label="notesSettings.selectionPopover ? '关闭划词浮框' : '开启划词浮框'"
                :aria-pressed="notesSettings.selectionPopover ? 'true' : 'false'"
                @click="toggleSelectionPopover"
              >
                <TextSelect :size="14" :stroke-width="1.8" aria-hidden="true" />
              </button>
              <button
                type="button"
                class="notes-icon-btn"
                :class="{ active: searchOpen }"
                title="筛选便签"
                aria-label="筛选便签"
                @click="toggleSearch"
              >
                <Search :size="14" :stroke-width="1.8" aria-hidden="true" />
              </button>
              <button
                type="button"
                class="notes-icon-btn"
                :class="{ active: store.desktopPinned.value }"
                :title="store.desktopPinned.value ? '取消钉住' : '钉住便签'"
                :aria-label="store.desktopPinned.value ? '取消钉住便签' : '钉住便签'"
                @click="togglePin"
              >
                <Pin :size="14" :stroke-width="1.8" aria-hidden="true" />
              </button>
              <button type="button" class="notes-icon-btn" title="关闭" aria-label="关闭便签" @click="closeDesktop">
                <X :size="15" :stroke-width="1.8" aria-hidden="true" />
              </button>
            </div>
          </header>

          <div v-show="!store.desktopMinimized.value" class="notes-body">
            <!-- 全局多张便签页签栏 -->
            <div v-if="activeScope === 'global'" class="notes-sheets-bar">
              <div class="notes-sheets-scroll" role="tablist" aria-label="全局便签列表">
                <button
                  v-for="sheet in store.state.globalSheets"
                  :key="sheet.sheetUuid"
                  type="button"
                  role="tab"
                  class="notes-sheet-tab"
                  :class="{ active: activeSheet?.sheetUuid === sheet.sheetUuid }"
                  :aria-selected="activeSheet?.sheetUuid === sheet.sheetUuid"
                  @click="selectSheet(sheet.sheetUuid)"
                >
                  <span class="notes-sheet-name">{{ sheet.title }}</span>
                  <small v-if="sheet.notes?.length">{{ sheet.notes.length }}</small>
                </button>
                <button
                  type="button"
                  class="notes-sheet-add"
                  title="新建全局便签"
                  aria-label="新建全局便签"
                  @click="openSheetCreator"
                >
                  <Plus :size="13" :stroke-width="1.9" aria-hidden="true" />
                </button>
              </div>
              <div v-if="activeSheet" class="notes-sheet-tools">
                <button type="button" class="notes-mini-icon" title="重命名此便签" aria-label="重命名此便签" @click="openSheetRenamer">
                  <Pencil :size="12" :stroke-width="1.8" aria-hidden="true" />
                </button>
                <button
                  v-if="store.state.globalSheets.length > 1 || (activeSheet.notes && activeSheet.notes.length)"
                  type="button"
                  class="notes-mini-icon is-danger"
                  title="删除此便签"
                  aria-label="删除此便签"
                  @click="confirmDeleteSheet"
                >
                  <Trash2 :size="12" :stroke-width="1.8" aria-hidden="true" />
                </button>
              </div>
            </div>

            <!-- 新建 / 重命名全局便签内联输入 -->
            <form v-if="sheetForm && activeScope === 'global'" class="notes-sheet-form" @submit.prevent="submitSheetForm">
              <input
                ref="sheetInputRef"
                v-model="sheetForm.title"
                type="text"
                maxlength="60"
                :placeholder="sheetForm.mode === 'create' ? '新便签名称…' : '修改便签名称…'"
              />
              <button type="submit" class="notes-pill-btn is-primary" :disabled="!sheetForm.title.trim()">保存</button>
              <button type="button" class="notes-pill-btn" @click="sheetForm = null">取消</button>
            </form>

            <!-- 搜索筛选行 -->
            <div v-if="searchOpen" class="notes-search-row">
              <Search :size="13" :stroke-width="1.8" aria-hidden="true" />
              <input v-model="searchQuery" type="search" maxlength="100" placeholder="筛选摘录或便签内容…" aria-label="筛选便签" />
            </div>

            <!-- 列表区域 -->
            <div class="notes-list" role="list">
              <div v-if="activeScope === 'session' && !sessionUsable" class="notes-empty">
                <p>发送第一条消息创建会话后，可在此记录本会话专属便签。</p>
                <button type="button" class="notes-link-btn" @click="switchScope('global')">切换到全局便签</button>
              </div>
              <div v-else-if="!currentNotes.length" class="notes-empty">
                <p>{{ searchQuery.trim() ? '没有匹配的便签' : activeScope === 'session' ? '还没有本会话便签' : '这张全局便签还是空的' }}</p>
                <small v-if="!searchQuery.trim()">在对话中选中文字右键即可摘录，或在下方随手输入</small>
              </div>

              <article
                v-for="note in currentNotes"
                :key="note.noteUuid"
                role="listitem"
                class="notes-card"
              >
                <blockquote
                  v-if="note.quote"
                  class="notes-quote"
                  :class="{ expanded: expandedQuotes[note.noteUuid] }"
                  title="点击展开或收起摘录"
                  @click="toggleQuoteExpand(note.noteUuid)"
                >{{ note.quote }}</blockquote>

                <div v-if="editingUuid === note.noteUuid" class="notes-edit-box">
                  <textarea
                    v-model="editingContent"
                    rows="2"
                    maxlength="16000"
                    placeholder="补充便签说明…"
                    @keydown.meta.enter.prevent="saveEditNote(note)"
                    @keydown.ctrl.enter.prevent="saveEditNote(note)"
                  ></textarea>
                  <div class="notes-edit-actions">
                    <button type="button" class="notes-pill-btn" @click="cancelEditNote">取消</button>
                    <button type="button" class="notes-pill-btn is-primary" @click="saveEditNote(note)">保存</button>
                  </div>
                </div>
                <p v-else-if="note.content" class="notes-content">{{ note.content }}</p>

                <div class="notes-card-foot">
                  <button
                    v-if="note.opId && (!isCrossConversation(note) || note.conversationExists)"
                    type="button"
                    class="notes-jump-btn"
                    :title="isCrossConversation(note) ? `跳转到「${note.conversationTitle}」对应消息` : '跳转到原对话位置'"
                    @click="locateSource(note)"
                  >
                    <ExternalLink v-if="isCrossConversation(note)" :size="11.5" :stroke-width="1.85" aria-hidden="true" />
                    <CornerUpRight v-else :size="11.5" :stroke-width="1.85" aria-hidden="true" />
                    <span v-if="isCrossConversation(note)">
                      {{ shortConvTitle(note.conversationTitle) }}{{ note.turnNumber ? ` · #${note.turnNumber}` : '' }}
                    </span>
                    <span v-else>{{ note.turnNumber ? `第 ${note.turnNumber} 轮 · 原对话` : '原对话' }}</span>
                  </button>
                  <span v-else class="notes-time">
                    {{ note.opId && !note.conversationExists ? '原会话已删除' : formatNoteTime(note.updatedAtMs) }}
                  </span>

                  <div class="notes-card-acts">
                    <button type="button" class="notes-act-btn" title="引用到输入框" aria-label="引用到输入框" @click="handleInsertNote(note)">
                      <TextQuote :size="13" :stroke-width="1.75" aria-hidden="true" />
                    </button>
                    <button
                      v-if="note.scope === 'session'"
                      type="button"
                      class="notes-act-btn"
                      title="转存到全局便签"
                      aria-label="转存到全局便签"
                      @click="handleCopyToGlobal(note)"
                    >
                      <Globe :size="13" :stroke-width="1.75" aria-hidden="true" />
                    </button>
                    <button type="button" class="notes-act-btn" title="编辑" aria-label="编辑便签" @click="beginEditNote(note)">
                      <Pencil :size="12.5" :stroke-width="1.75" aria-hidden="true" />
                    </button>
                    <button type="button" class="notes-act-btn is-danger" title="删除" aria-label="删除便签" @click="handleDeleteNote(note)">
                      <Trash2 :size="12.5" :stroke-width="1.75" aria-hidden="true" />
                    </button>
                  </div>
                </div>
              </article>
            </div>

            <!-- 底部随手记输入栏 -->
            <div v-if="activeScope === 'global' || sessionUsable" class="notes-compose">
              <div v-if="store.draftQuote.value" class="notes-draft-quote">
                <span>
                  {{ store.draftQuote.value.turnNumber ? `引用第 ${store.draftQuote.value.turnNumber} 轮：` : '引用摘录：' }}{{ store.draftQuote.value.quote }}
                </span>
                <button type="button" aria-label="移除引用摘录" @click="store.clearDraftQuote()">
                  <X :size="13" :stroke-width="1.8" aria-hidden="true" />
                </button>
              </div>
              <div class="notes-compose-row">
                <textarea
                  ref="pcInputRef"
                  v-model="composeText"
                  rows="1"
                  maxlength="16000"
                  :placeholder="store.draftQuote.value ? '写一句批注（可留空直接保存）…' : '随手记一条，Enter 保存…'"
                  @keydown="onPcComposeKeydown"
                ></textarea>
                <button
                  type="button"
                  class="notes-submit-btn"
                  :disabled="store.state.saving || (!composeText.trim() && !store.draftQuote.value)"
                  @click="submitCompose"
                >
                  <Check :size="14" :stroke-width="2" aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>
        </aside>
      </Transition>
    </Teleport>

    <!-- 手机端跟手底部面板（Bottom Sheet） -->
    <el-drawer
      v-if="phone"
      v-model="store.mobileOpen.value"
      direction="btt"
      size="min(calc(var(--mobile-viewport-height, 100dvh) * .72), 34rem)"
      :with-header="false"
      append-to-body
      class="notes-mobile-drawer mobile-viewport-sheet"
      :class="{ 'is-dragging': sheetDragging }"
      modal-class="notes-mobile-overlay"
      :style="mobileSheetStyle"
      @closed="onSheetClosed"
    >
      <div
        class="notes-mobile-head"
        @pointerdown="sheetDragStart"
        @pointermove="sheetDragMove"
        @pointerup="sheetDragEnd"
        @pointercancel="sheetDragCancel"
      >
        <div class="notes-mobile-grip" aria-hidden="true"></div>
        <div class="notes-seg" role="tablist" aria-label="便签范围">
          <button
            type="button"
            role="tab"
            :aria-selected="activeScope === 'session'"
            :class="{ active: activeScope === 'session' }"
            @click="switchScope('session')"
          >
            <span>本会话</span>
            <small v-if="store.sessionCount.value">{{ store.sessionCount.value }}</small>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="activeScope === 'global'"
            :class="{ active: activeScope === 'global' }"
            @click="switchScope('global')"
          >
            <span>全局</span>
            <small v-if="store.state.globalSheets.length">{{ store.state.globalSheets.length }}</small>
          </button>
        </div>
        <div class="notes-head-actions">
          <button
            type="button"
            class="notes-mobile-icon-btn notes-sel-toggle"
            :class="{ active: notesSettings.selectionPopover, 'is-off': !notesSettings.selectionPopover }"
            :disabled="togglingSelection"
            :aria-label="notesSettings.selectionPopover ? '关闭划词浮框' : '开启划词浮框'"
            :aria-pressed="notesSettings.selectionPopover ? 'true' : 'false'"
            @click="toggleSelectionPopover"
          >
            <TextSelect :size="17" :stroke-width="1.8" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="notes-mobile-icon-btn"
            :class="{ active: searchOpen }"
            aria-label="筛选便签"
            @click="toggleSearch"
          >
            <Search :size="17" :stroke-width="1.8" aria-hidden="true" />
          </button>
          <button type="button" class="notes-mobile-icon-btn" aria-label="关闭便签" @click="store.mobileOpen.value = false">
            <X :size="19" :stroke-width="1.8" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div class="notes-body is-mobile">
        <div v-if="activeScope === 'global'" class="notes-sheets-bar">
          <div class="notes-sheets-scroll" role="tablist">
            <button
              v-for="sheet in store.state.globalSheets"
              :key="sheet.sheetUuid"
              type="button"
              role="tab"
              class="notes-sheet-tab"
              :class="{ active: activeSheet?.sheetUuid === sheet.sheetUuid }"
              @click="selectSheet(sheet.sheetUuid)"
            >
              <span class="notes-sheet-name">{{ sheet.title }}</span>
              <small v-if="sheet.notes?.length">{{ sheet.notes.length }}</small>
            </button>
            <button type="button" class="notes-sheet-add" aria-label="新建全局便签" @click="openSheetCreator">
              <Plus :size="15" :stroke-width="1.9" aria-hidden="true" />
            </button>
          </div>
          <div v-if="activeSheet" class="notes-sheet-tools">
            <button type="button" class="notes-mini-icon" aria-label="重命名此便签" @click="openSheetRenamer">
              <Pencil :size="14" :stroke-width="1.8" aria-hidden="true" />
            </button>
            <button
              v-if="store.state.globalSheets.length > 1 || (activeSheet.notes && activeSheet.notes.length)"
              type="button"
              class="notes-mini-icon is-danger"
              aria-label="删除此便签"
              @click="confirmDeleteSheet"
            >
              <Trash2 :size="14" :stroke-width="1.8" aria-hidden="true" />
            </button>
          </div>
        </div>

        <form v-if="sheetForm && activeScope === 'global'" class="notes-sheet-form is-mobile" @submit.prevent="submitSheetForm">
          <input
            ref="sheetInputRef"
            v-model="sheetForm.title"
            type="text"
            maxlength="60"
            :placeholder="sheetForm.mode === 'create' ? '新便签名称…' : '修改便签名称…'"
          />
          <button type="submit" class="notes-pill-btn is-primary" :disabled="!sheetForm.title.trim()">保存</button>
          <button type="button" class="notes-pill-btn" @click="sheetForm = null">取消</button>
        </form>

        <div v-if="searchOpen" class="notes-search-row is-mobile">
          <Search :size="15" :stroke-width="1.8" aria-hidden="true" />
          <input v-model="searchQuery" type="search" maxlength="100" placeholder="筛选摘录或便签内容…" aria-label="筛选便签" />
        </div>

        <div class="notes-list" role="list">
          <div v-if="activeScope === 'session' && !sessionUsable" class="notes-empty">
            <p>发送首条消息创建会话后，可记录本会话专属便签。</p>
            <button type="button" class="notes-link-btn" @click="switchScope('global')">切换到全局便签</button>
          </div>
          <div v-else-if="!currentNotes.length" class="notes-empty">
            <p>{{ searchQuery.trim() ? '没有匹配的便签' : activeScope === 'session' ? '还没有本会话便签' : '这张全局便签还是空的' }}</p>
            <small v-if="!searchQuery.trim()">可点每轮回复底部的便签图标一键摘录，或在下方随手记下</small>
          </div>

          <article v-for="note in currentNotes" :key="note.noteUuid" role="listitem" class="notes-card is-mobile">
            <blockquote
              v-if="note.quote"
              class="notes-quote"
              :class="{ expanded: expandedQuotes[note.noteUuid] }"
              @click="toggleQuoteExpand(note.noteUuid)"
            >{{ note.quote }}</blockquote>

            <div v-if="editingUuid === note.noteUuid" class="notes-edit-box is-mobile">
              <textarea v-model="editingContent" rows="2" maxlength="16000" placeholder="补充便签说明…"></textarea>
              <div class="notes-edit-actions">
                <button type="button" class="notes-pill-btn" @click="cancelEditNote">取消</button>
                <button type="button" class="notes-pill-btn is-primary" @click="saveEditNote(note)">保存</button>
              </div>
            </div>
            <p v-else-if="note.content" class="notes-content">{{ note.content }}</p>

            <div class="notes-card-foot">
              <button
                v-if="note.opId && (!isCrossConversation(note) || note.conversationExists)"
                type="button"
                class="notes-jump-btn"
                @click="locateSource(note)"
              >
                <ExternalLink v-if="isCrossConversation(note)" :size="12" :stroke-width="1.85" aria-hidden="true" />
                <CornerUpRight v-else :size="12" :stroke-width="1.85" aria-hidden="true" />
                <span v-if="isCrossConversation(note)">
                  {{ shortConvTitle(note.conversationTitle) }}{{ note.turnNumber ? ` · #${note.turnNumber}` : '' }}
                </span>
                <span v-else>{{ note.turnNumber ? `第 ${note.turnNumber} 轮 · 原对话` : '原对话' }}</span>
              </button>
              <span v-else class="notes-time">
                {{ note.opId && !note.conversationExists ? '原会话已删除' : formatNoteTime(note.updatedAtMs) }}
              </span>

              <div class="notes-card-acts is-always">
                <button type="button" class="notes-act-btn" aria-label="引用到输入框" @click="handleInsertNote(note)">
                  <TextQuote :size="15" :stroke-width="1.75" aria-hidden="true" />
                </button>
                <button
                  v-if="note.scope === 'session'"
                  type="button"
                  class="notes-act-btn"
                  aria-label="转存到全局便签"
                  @click="handleCopyToGlobal(note)"
                >
                  <Globe :size="15" :stroke-width="1.75" aria-hidden="true" />
                </button>
                <button type="button" class="notes-act-btn" aria-label="编辑便签" @click="beginEditNote(note)">
                  <Pencil :size="14.5" :stroke-width="1.75" aria-hidden="true" />
                </button>
                <button type="button" class="notes-act-btn is-danger" aria-label="删除便签" @click="handleDeleteNote(note)">
                  <Trash2 :size="14.5" :stroke-width="1.75" aria-hidden="true" />
                </button>
              </div>
            </div>
          </article>
        </div>

        <div v-if="activeScope === 'global' || sessionUsable" class="notes-compose is-mobile">
          <div v-if="store.draftQuote.value" class="notes-draft-quote">
            <span>
              {{ store.draftQuote.value.turnNumber ? `引用第 ${store.draftQuote.value.turnNumber} 轮：` : '引用摘录：' }}{{ store.draftQuote.value.quote }}
            </span>
            <button type="button" aria-label="移除引用摘录" @click="store.clearDraftQuote()">
              <X :size="15" :stroke-width="1.8" aria-hidden="true" />
            </button>
          </div>
          <div class="notes-compose-row">
            <textarea
              ref="mobileInputRef"
              v-model="composeText"
              rows="1"
              maxlength="16000"
              :placeholder="store.draftQuote.value ? '写一句批注（可留空直接保存）…' : '随手记一条…'"
            ></textarea>
            <button
              type="button"
              class="notes-submit-btn is-mobile"
              :disabled="store.state.saving || (!composeText.trim() && !store.draftQuote.value)"
              @click="submitCompose"
            >
              记下
            </button>
          </div>
        </div>
      </div>
    </el-drawer>
  </template>
</template>

<style scoped>
/* PC 右侧浮动栏图标（位于任务图标正上方一格，保持 30x30 与中性边框契约） */
.notes-entry-wrap {
  position: fixed;
  top: calc(var(--console-float-rail-top, calc(48% - 3.25rem)) - 4 * (var(--console-float-control-size, 30px) + var(--console-float-control-gap, 8px)));
  right: var(--console-float-rail-right, 1rem);
  z-index: 32;
  pointer-events: auto;
  transition: right .24s cubic-bezier(.22, 1, .36, 1);
}
.notes-entry {
  position: relative;
  display: grid;
  width: var(--console-float-control-size, 30px);
  height: var(--console-float-control-size, 30px);
  place-items: center;
  border: 1px solid transparent;
  border-radius: 7px;
  background: var(--ob-chat-bg);
  color: var(--ob-chat-subtle);
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease, color .15s ease;
}
.notes-entry:hover {
  color: var(--ob-chat-text);
  background: var(--ob-chat-hover);
}
.notes-entry.populated {
  border-color: var(--ob-chat-line);
  color: var(--ob-chat-text);
}
.notes-entry.active {
  background: var(--ob-chat-selected);
  border-color: var(--ob-chat-line);
  color: var(--ob-chat-text);
}
.notes-entry:focus-visible {
  box-shadow: 0 0 0 3px var(--ob-focus);
  outline: none;
}
.notes-entry-icon {
  width: 17px;
  height: 17px;
}
.notes-entry-badge {
  position: absolute;
  top: -.38rem;
  right: -.38rem;
  display: grid;
  min-width: 1rem;
  height: 1rem;
  place-items: center;
  border: 2px solid var(--ob-chat-bg);
  border-radius: 5px;
  background: var(--ob-chat-subtle);
  padding: 0 .2rem;
  color: var(--ob-chat-panel);
  font-size: 9.5px;
  font-weight: 600;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

/* PC 轻量悬浮便签面板：纯净中性毛玻璃质感 */
.notes-sticky-popover {
  position: fixed;
  top: max(74px, calc(48% - 13.5rem));
  right: calc(var(--console-float-rail-right, 1.15rem) + var(--console-float-control-size, 30px) + 10px);
  z-index: 1800;
  display: flex;
  flex-direction: column;
  width: 328px;
  max-height: min(452px, calc(100dvh - 104px));
  border: 1px solid var(--ob-border-strong);
  border-radius: 13px;
  background: rgb(var(--ob-surface-rgb) / .96);
  color: var(--ob-text);
  box-shadow: var(--ob-shadow-popover), var(--ob-shadow-inset);
  -webkit-backdrop-filter: blur(22px) saturate(1.1);
  backdrop-filter: blur(22px) saturate(1.1);
  overflow: hidden;
}
html.dark .notes-sticky-popover {
  background: rgb(var(--ob-surface-raised-rgb) / .97);
}
.notes-sticky-popover.is-minimized {
  max-height: none;
}

.notes-float-enter-active,
.notes-float-leave-active {
  transition: opacity .15s cubic-bezier(.22, 1, .36, 1), transform .15s cubic-bezier(.22, 1, .36, 1);
}
.notes-float-enter-from,
.notes-float-leave-to {
  opacity: 0;
  transform: translateX(6px) scale(.985);
}

.notes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--ob-border);
  cursor: grab;
  user-select: none;
  flex: none;
}
.notes-head:active {
  cursor: grabbing;
}
.notes-sticky-popover.is-minimized .notes-head {
  border-bottom-color: transparent;
}

.notes-seg {
  display: inline-flex;
  align-items: center;
  padding: 2px;
  border-radius: 8px;
  background: var(--ob-surface-soft);
}
.notes-seg button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 25px;
  padding: 0 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background .14s ease, color .14s ease;
}
.notes-seg button.active {
  background: var(--ob-surface);
  color: var(--ob-text-strong);
  font-weight: 550;
  box-shadow: var(--ob-shadow-panel);
}
.notes-seg small {
  font-size: 10.5px;
  color: var(--ob-text-muted);
  font-variant-numeric: tabular-nums;
}

.notes-head-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.notes-icon-btn {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text-subtle);
  cursor: pointer;
  transition: background .12s ease, color .12s ease;
}
.notes-icon-btn:hover {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.notes-icon-btn.active {
  background: var(--ob-surface-soft);
  color: var(--ob-text-strong);
}
.notes-sel-toggle {
  position: relative;
}
.notes-sel-toggle.active {
  color: var(--ob-blue);
  background: var(--ob-blue-soft);
}
.notes-sel-toggle.is-off {
  color: var(--ob-text-disabled);
}
.notes-sel-toggle.is-off::after {
  content: '';
  position: absolute;
  width: 15px;
  height: 1.5px;
  border-radius: 1px;
  background: currentColor;
  transform: rotate(-42deg);
  pointer-events: none;
}

.notes-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

/* 全局多张便签切换条 */
.notes-sheets-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--ob-border);
  flex: none;
}
.notes-sheets-scroll {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  flex: 1;
  overflow-x: auto;
  scrollbar-width: none;
}
.notes-sheets-scroll::-webkit-scrollbar {
  display: none;
}
.notes-sheet-tab {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  max-width: 132px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text-subtle);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}
.notes-sheet-tab:hover {
  background: var(--ob-hover);
  color: var(--ob-text);
}
.notes-sheet-tab.active {
  border-color: var(--ob-border-strong);
  background: var(--ob-surface-soft);
  color: var(--ob-text-strong);
  font-weight: 500;
}
.notes-sheet-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notes-sheet-tab small {
  font-size: 10px;
  color: var(--ob-text-muted);
  font-variant-numeric: tabular-nums;
}
.notes-sheet-add {
  flex: none;
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  border: 1px dashed var(--ob-border-strong);
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text-subtle);
  cursor: pointer;
}
.notes-sheet-add:hover {
  border-style: solid;
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.notes-sheet-tools {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
}
.notes-mini-icon {
  display: grid;
  width: 22px;
  height: 22px;
  place-items: center;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--ob-text-muted);
  cursor: pointer;
}
.notes-mini-icon:hover {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.notes-mini-icon.is-danger:hover {
  color: var(--ob-danger);
}

.notes-sheet-form {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--ob-border);
  flex: none;
}
.notes-sheet-form input {
  flex: 1;
  min-width: 0;
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 6px;
  background: var(--ob-surface);
  color: var(--ob-text);
  font: inherit;
  font-size: 12px;
  outline: none;
}
.notes-sheet-form input:focus {
  border-color: var(--ob-blue);
}

.notes-search-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--ob-border);
  color: var(--ob-text-muted);
  flex: none;
}
.notes-search-row input {
  flex: 1;
  min-width: 0;
  height: 24px;
  border: 0;
  background: transparent;
  color: var(--ob-text);
  font: inherit;
  font-size: 12px;
  outline: none;
}

.notes-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.notes-empty {
  padding: 28px 12px;
  text-align: center;
  color: var(--ob-text-subtle);
}
.notes-empty p {
  margin: 0;
  font-size: 12.5px;
  color: var(--ob-text);
}
.notes-empty small {
  display: block;
  margin-top: 5px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--ob-text-muted);
}
.notes-link-btn {
  margin-top: 8px;
  padding: 4px 10px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 6px;
  background: var(--ob-surface);
  color: var(--ob-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

/* 单条便签卡片 */
.notes-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border: 1px solid var(--ob-border);
  border-radius: 9px;
  background: var(--ob-surface);
  transition: border-color .14s ease;
}
html.dark .notes-card {
  background: rgb(var(--ob-surface-rgb) / .72);
}
.notes-card:hover {
  border-color: var(--ob-border-strong);
}

.notes-quote {
  margin: 0;
  padding: 1px 0 1px 8px;
  border-left: 2px solid var(--ob-border-strong);
  color: var(--ob-text-subtle);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}
.notes-quote.expanded {
  display: block;
  -webkit-line-clamp: unset;
}

.notes-content {
  margin: 0;
  color: var(--ob-text-strong);
  font-size: 12.5px;
  line-height: 1.58;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.notes-edit-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.notes-edit-box textarea {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 7px;
  background: var(--ob-bg);
  color: var(--ob-text);
  font: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  resize: vertical;
  outline: none;
}
.notes-edit-box textarea:focus {
  border-color: var(--ob-blue);
}
.notes-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 5px;
}

.notes-pill-btn {
  height: 25px;
  padding: 0 9px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 6px;
  background: var(--ob-surface);
  color: var(--ob-text);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}
.notes-pill-btn.is-primary {
  border-color: transparent;
  background: var(--ob-chat-button);
  color: var(--ob-chat-button-text);
}
.notes-pill-btn:disabled {
  opacity: .45;
  cursor: default;
}

.notes-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-height: 22px;
}
.notes-jump-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  height: 22px;
  padding: 0 6px;
  margin-left: -4px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--ob-text-subtle);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: background .12s ease, color .12s ease;
}
.notes-jump-btn span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notes-jump-btn:hover {
  background: var(--ob-surface-soft);
  color: var(--ob-blue);
}
.notes-time {
  color: var(--ob-text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.notes-card-acts {
  display: inline-flex;
  align-items: center;
  gap: 1px;
  margin-left: auto;
  opacity: .65;
  transition: opacity .12s ease;
}
.notes-card:hover .notes-card-acts,
.notes-card:focus-within .notes-card-acts,
.notes-card-acts.is-always {
  opacity: 1;
}
.notes-act-btn {
  display: grid;
  width: 23px;
  height: 23px;
  place-items: center;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--ob-text-muted);
  cursor: pointer;
  transition: background .12s ease, color .12s ease;
}
.notes-act-btn:hover {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
}
.notes-act-btn.is-danger:hover {
  color: var(--ob-danger);
}

/* 底部输入区 */
.notes-compose {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border-top: 1px solid var(--ob-border);
  background: rgb(var(--ob-bg-rgb) / .55);
  flex: none;
}
.notes-draft-quote {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--ob-surface-soft);
  color: var(--ob-text-subtle);
  font-size: 11px;
}
.notes-draft-quote span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notes-draft-quote button {
  display: grid;
  place-items: center;
  padding: 2px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--ob-text-muted);
  cursor: pointer;
}
.notes-compose-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
}
.notes-compose-row textarea {
  flex: 1;
  min-width: 0;
  min-height: 32px;
  max-height: 84px;
  padding: 6px 9px;
  border: 1px solid var(--ob-border);
  border-radius: 8px;
  background: var(--ob-surface);
  color: var(--ob-text);
  font: inherit;
  font-size: 12.5px;
  line-height: 1.45;
  resize: none;
  outline: none;
  transition: border-color .14s ease;
}
.notes-compose-row textarea:focus {
  border-color: var(--ob-border-strong);
}
.notes-submit-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: var(--ob-chat-button);
  color: var(--ob-chat-button-text);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  flex: none;
}
.notes-submit-btn:disabled {
  opacity: .4;
  cursor: default;
}

/* 手机端专属尺寸与防缩放适配 */
.notes-mobile-head {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 16px max(14px, env(safe-area-inset-right, 0px)) 8px max(14px, env(safe-area-inset-left, 0px));
  border-bottom: 1px solid var(--ob-border);
  touch-action: none;
  flex: none;
}
.notes-mobile-grip {
  position: absolute;
  top: 7px;
  left: calc(50% - 15px);
  width: 30px;
  height: 4px;
  border-radius: 4px;
  background: var(--ob-chat-border);
}
.notes-mobile-head .notes-seg button {
  height: 32px;
  padding: 0 12px;
  font-size: 13px;
}
.notes-mobile-icon-btn {
  display: grid;
  width: 40px;
  height: 40px;
  place-items: center;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--ob-text-subtle);
  cursor: pointer;
}
.notes-mobile-icon-btn.active {
  background: var(--ob-surface-soft);
  color: var(--ob-text-strong);
}
.notes-mobile-icon-btn.notes-sel-toggle.active {
  color: var(--ob-blue);
  background: var(--ob-blue-soft);
}
.notes-mobile-icon-btn.notes-sel-toggle.is-off {
  color: var(--ob-text-disabled);
}
.notes-body.is-mobile .notes-sheets-bar,
.notes-body.is-mobile .notes-list {
  padding-left: max(14px, env(safe-area-inset-left, 0px));
  padding-right: max(14px, env(safe-area-inset-right, 0px));
}
.notes-body.is-mobile .notes-sheet-tab {
  height: 30px;
  padding: 0 10px;
  font-size: 12.5px;
}
.notes-body.is-mobile .notes-sheet-add,
.notes-body.is-mobile .notes-mini-icon {
  width: 32px;
  height: 32px;
}
.notes-sheet-form.is-mobile input,
.notes-search-row.is-mobile input,
.notes-edit-box.is-mobile textarea,
.notes-compose.is-mobile textarea {
  font-size: 16px;
}
.notes-sheet-form.is-mobile input {
  height: 36px;
}
.notes-search-row.is-mobile {
  padding: 8px max(14px, env(safe-area-inset-right, 0px)) 8px max(14px, env(safe-area-inset-left, 0px));
}
.notes-search-row.is-mobile input {
  height: 30px;
}
.notes-card.is-mobile {
  padding: 10px 12px;
}
.notes-card.is-mobile .notes-act-btn {
  width: 34px;
  height: 34px;
}
.notes-card.is-mobile .notes-jump-btn {
  height: 30px;
  font-size: 12px;
}
.notes-compose.is-mobile {
  padding: 8px max(14px, env(safe-area-inset-right, 0px)) max(10px, env(safe-area-inset-bottom, 0px)) max(14px, env(safe-area-inset-left, 0px));
}
.notes-compose.is-mobile textarea {
  min-height: 38px;
}
.notes-submit-btn.is-mobile {
  width: auto;
  height: 38px;
  padding: 0 14px;
  border-radius: 10px;
  font-size: 13px;
}

@media (max-width: 760px) {
  .notes-entry-wrap {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .notes-float-enter-active,
  .notes-float-leave-active,
  .notes-entry {
    transition: none;
  }
}
</style>

<style>
.notes-mobile-drawer.el-drawer {
  background: var(--ob-surface-raised);
  color: var(--ob-text);
}
.notes-mobile-drawer.el-drawer.btt {
  border-radius: 16px 16px 0 0;
  transition: transform 280ms cubic-bezier(.22, .68, 0, 1);
}
html .notes-mobile-drawer.mobile-viewport-sheet > .el-drawer__body {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  padding: 0;
}
.notes-mobile-drawer.is-dragging {
  transition: none;
  user-select: none;
}
.notes-mobile-overlay.el-drawer-fade-leave-active {
  transition: background-color 280ms linear;
}
.notes-mobile-overlay.el-drawer-fade-leave-from .notes-mobile-drawer {
  transform: translateY(var(--notes-drag-y, 0px));
}
.notes-mobile-overlay.el-drawer-fade-leave-active .notes-mobile-drawer {
  pointer-events: none;
}
</style>
