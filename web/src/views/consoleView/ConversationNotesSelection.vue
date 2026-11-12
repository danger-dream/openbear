<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Copy, Globe, PenLine, StickyNote, X } from '@lucide/vue';
import { apiError } from '../../api.js';
import { copyTextToClipboard } from '../../utils/clipboard.js';
import { notesSelectionPopoverEnabled, notesSettings } from '../../notesSettings.js';
import { clampOverlayPosition, extractTranscriptSelection } from './notesSelection.js';
import { useUserNotes } from './notesStore.js';

const props = defineProps({
  scroller: { type: Object, default: null },
});

const notes = useUserNotes();
const phone = ref(false);
const contextMenu = ref(null); // { x, y, quote, opId, turnNumber }
const floatingBar = ref(null); // { x, y, quote, opId, turnNumber }
const menuRef = ref(null);
const barRef = ref(null);

let media = null;
let selectionTimer = 0;

const sessionAvailable = computed(() => Boolean(notes?.usableConversationUuid.value));

function closeAll() {
  contextMenu.value = null;
  floatingBar.value = null;
}

function updateMedia() {
  phone.value = Boolean(media?.matches);
  closeAll();
}

function readSelection() {
  if (!notesSettings.enabled || !props.scroller || typeof window === 'undefined') return null;
  return extractTranscriptSelection(window.getSelection?.(), props.scroller);
}

function onContextMenu(event) {
  if (!notesSettings.enabled || !props.scroller) return;
  const captured = readSelection();
  if (!captured) return;
  event.preventDefault();
  floatingBar.value = null;
  const pos = clampOverlayPosition(
    event.clientX + 4,
    event.clientY + 4,
    188,
    154,
    window.innerWidth,
    window.innerHeight,
  );
  contextMenu.value = { ...captured, x: pos.x, y: pos.y };
}

function showDesktopFloatingBar() {
  if (!notesSelectionPopoverEnabled() || phone.value || contextMenu.value) return;
  const captured = readSelection();
  if (!captured || !captured.rect || (!captured.rect.width && !captured.rect.height)) {
    floatingBar.value = null;
    return;
  }
  const barW = 216;
  const barH = 34;
  const rawX = captured.rect.left + captured.rect.width / 2 - barW / 2;
  const rawY = captured.rect.top >= barH + 16 ? captured.rect.top - barH - 8 : captured.rect.bottom + 8;
  const pos = clampOverlayPosition(rawX, rawY, barW, barH, window.innerWidth, window.innerHeight);
  floatingBar.value = { ...captured, x: pos.x, y: pos.y };
}

function onMouseUp(event) {
  if (phone.value || !notesSelectionPopoverEnabled()) return;
  if (menuRef.value?.contains(event.target) || barRef.value?.contains(event.target)) return;
  window.clearTimeout(selectionTimer);
  selectionTimer = window.setTimeout(showDesktopFloatingBar, 16);
}

function onSelectionChange() {
  window.clearTimeout(selectionTimer);
  if (!notesSettings.enabled) {
    closeAll();
    return;
  }
  selectionTimer = window.setTimeout(() => {
    const captured = readSelection();
    if (!captured) {
      floatingBar.value = null;
      return;
    }
    if (phone.value && notesSelectionPopoverEnabled()) {
      floatingBar.value = { ...captured, x: 0, y: 0 };
    }
  }, phone.value ? 120 : 40);
}

function onPointerDown(event) {
  const target = event.target;
  if (contextMenu.value && !menuRef.value?.contains(target)) {
    contextMenu.value = null;
  }
  if (floatingBar.value && !barRef.value?.contains(target) && !phone.value) {
    floatingBar.value = null;
  }
}

function onKeyDown(event) {
  if (event.key === 'Escape' && (contextMenu.value || floatingBar.value)) {
    closeAll();
  }
}

function clearDomSelection() {
  try {
    window.getSelection?.()?.removeAllRanges();
  } catch {
    // Ignore selection clearing errors on restricted browsers.
  }
}

async function copySelected(item) {
  if (!item?.quote) return;
  closeAll();
  try {
    await copyTextToClipboard(item.quote);
    ElMessage.success('已复制选中内容');
  } catch {
    ElMessage.error('复制失败，请使用快捷键复制');
  }
}

async function saveSelection(item, scope) {
  if (!item?.quote || !notes) return;
  const payload = { ...item };
  closeAll();
  clearDomSelection();
  try {
    await notes.createNote({
      scope,
      opId: payload.opId,
      turnNumber: payload.turnNumber,
      quote: payload.quote,
    });
    ElMessage.success(scope === 'global' ? '已记入全局便签' : '已记入本会话便签');
  } catch (err) {
    ElMessage.error(apiError(err));
  }
}

function annotateSelection(item) {
  if (!item?.quote || !notes) return;
  const payload = { ...item };
  closeAll();
  clearDomSelection();
  notes.beginAnnotatedNote(payload, { mobile: phone.value });
}

watch(
  () => [notesSettings.enabled, notesSettings.selectionPopover],
  ([enabled, selOn]) => {
    if (!enabled) closeAll();
    else if (!selOn) floatingBar.value = null;
  },
);

watch(
  () => props.scroller,
  (el, prev) => {
    prev?.removeEventListener?.('contextmenu', onContextMenu);
    prev?.removeEventListener?.('scroll', closeAll);
    el?.addEventListener?.('contextmenu', onContextMenu);
    el?.addEventListener?.('scroll', closeAll, { passive: true });
  },
  { immediate: true },
);

onMounted(() => {
  media = window.matchMedia?.('(max-width: 760px), (hover: none) and (pointer: coarse)') || null;
  updateMedia();
  media?.addEventListener?.('change', updateMedia);
  document.addEventListener('mouseup', onMouseUp);
  document.addEventListener('selectionchange', onSelectionChange);
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKeyDown);
});

onBeforeUnmount(() => {
  window.clearTimeout(selectionTimer);
  props.scroller?.removeEventListener?.('contextmenu', onContextMenu);
  props.scroller?.removeEventListener?.('scroll', closeAll);
  media?.removeEventListener?.('change', updateMedia);
  document.removeEventListener('mouseup', onMouseUp);
  document.removeEventListener('selectionchange', onSelectionChange);
  document.removeEventListener('pointerdown', onPointerDown, true);
  document.removeEventListener('keydown', onKeyDown);
});
</script>

<template>
  <Teleport to="body">
    <!-- 选中文字右键菜单（开启便签功能后默认生效） -->
    <div
      v-if="contextMenu"
      ref="menuRef"
      class="notes-ctx-menu"
      role="menu"
      aria-label="选中文字便签操作"
      :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
      @contextmenu.prevent
    >
      <button type="button" class="notes-ctx-item" role="menuitem" @click="copySelected(contextMenu)">
        <Copy :size="14" :stroke-width="1.75" aria-hidden="true" />
        <span>复制选中内容</span>
      </button>
      <div class="notes-ctx-sep" role="separator"></div>
      <button
        v-if="sessionAvailable"
        type="button"
        class="notes-ctx-item"
        role="menuitem"
        @click="saveSelection(contextMenu, 'session')"
      >
        <StickyNote :size="14" :stroke-width="1.75" aria-hidden="true" />
        <span>记入本会话便签</span>
      </button>
      <button type="button" class="notes-ctx-item" role="menuitem" @click="saveSelection(contextMenu, 'global')">
        <Globe :size="14" :stroke-width="1.75" aria-hidden="true" />
        <span>记入全局便签</span>
      </button>
      <button type="button" class="notes-ctx-item" role="menuitem" @click="annotateSelection(contextMenu)">
        <PenLine :size="14" :stroke-width="1.75" aria-hidden="true" />
        <span>加批注后记入…</span>
      </button>
    </div>

    <!-- PC 端按需划词极简工具条（仅系统设置开启「启用划词功能」后显示） -->
    <div
      v-if="floatingBar && !phone"
      ref="barRef"
      class="notes-sel-toolbar"
      role="toolbar"
      aria-label="划词记入便签"
      :style="{ left: `${floatingBar.x}px`, top: `${floatingBar.y}px` }"
      @mousedown.prevent
    >
      <button
        v-if="sessionAvailable"
        type="button"
        class="notes-sel-btn"
        @click="saveSelection(floatingBar, 'session')"
      >
        <StickyNote :size="13.5" :stroke-width="1.8" aria-hidden="true" />
        <span>记便签</span>
      </button>
      <span v-if="sessionAvailable" class="notes-sel-div" aria-hidden="true"></span>
      <button type="button" class="notes-sel-btn" @click="saveSelection(floatingBar, 'global')">
        <Globe :size="13.5" :stroke-width="1.8" aria-hidden="true" />
        <span>全局</span>
      </button>
      <span class="notes-sel-div" aria-hidden="true"></span>
      <button type="button" class="notes-sel-btn" @click="annotateSelection(floatingBar)">
        <PenLine :size="13.5" :stroke-width="1.8" aria-hidden="true" />
        <span>批注</span>
      </button>
    </div>
  </Teleport>

  <!-- 手机端按需划词底部操作条（仅开启「启用划词功能」后显示，避开系统原生复制菜单） -->
  <div v-if="floatingBar && phone" ref="barRef" class="notes-mobile-sel-bar" role="region" aria-label="选中文字记入便签">
    <span class="notes-mobile-sel-count">已选 <strong>{{ floatingBar.quote.length }}</strong> 字</span>
    <div class="notes-mobile-sel-actions">
      <button v-if="sessionAvailable" type="button" class="is-primary" @click="saveSelection(floatingBar, 'session')">
        记便签
      </button>
      <button type="button" @click="saveSelection(floatingBar, 'global')">全局</button>
      <button type="button" @click="annotateSelection(floatingBar)">批注</button>
      <button type="button" class="is-close" aria-label="关闭选词条" @click="floatingBar = null">
        <X :size="15" :stroke-width="1.8" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.notes-ctx-menu {
  position: fixed;
  z-index: 2200;
  min-width: 176px;
  padding: 5px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 11px;
  background: rgb(var(--ob-surface-rgb) / .95);
  color: var(--ob-text);
  box-shadow: var(--ob-shadow-popover), var(--ob-shadow-inset);
  -webkit-backdrop-filter: blur(20px) saturate(1.1);
  backdrop-filter: blur(20px) saturate(1.1);
  animation: notes-pop-in .12s cubic-bezier(.22, 1, .36, 1);
}
html.dark .notes-ctx-menu {
  background: rgb(var(--ob-surface-raised-rgb) / .96);
}
.notes-ctx-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  height: 31px;
  padding: 0 9px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--ob-text);
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
  white-space: nowrap;
  transition: background .12s ease, color .12s ease;
}
.notes-ctx-item svg {
  flex: none;
  color: var(--ob-text-subtle);
  transition: color .12s ease;
}
.notes-ctx-item:hover,
.notes-ctx-item:focus-visible {
  background: var(--ob-hover);
  color: var(--ob-text-strong);
  outline: none;
}
.notes-ctx-item:hover svg,
.notes-ctx-item:focus-visible svg {
  color: var(--ob-text-strong);
}
.notes-ctx-sep {
  height: 1px;
  margin: 4px 6px;
  background: var(--ob-border);
}

.notes-sel-toolbar {
  position: fixed;
  z-index: 2150;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  height: 32px;
  padding: 3px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 9px;
  background: rgb(var(--ob-surface-rgb) / .95);
  color: var(--ob-text);
  box-shadow: var(--ob-shadow-popover), var(--ob-shadow-inset);
  -webkit-backdrop-filter: blur(20px) saturate(1.1);
  backdrop-filter: blur(20px) saturate(1.1);
  animation: notes-pop-in .12s cubic-bezier(.22, 1, .36, 1);
}
html.dark .notes-sel-toolbar {
  background: rgb(var(--ob-surface-raised-rgb) / .96);
}
.notes-sel-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background .12s ease, color .12s ease;
}
.notes-sel-btn svg {
  flex: none;
  color: var(--ob-text-subtle);
}
.notes-sel-btn:hover,
.notes-sel-btn:focus-visible {
  background: var(--ob-surface-soft);
  color: var(--ob-text-strong);
  outline: none;
}
.notes-sel-btn:hover svg,
.notes-sel-btn:focus-visible svg {
  color: var(--ob-text-strong);
}
.notes-sel-div {
  width: 1px;
  height: 13px;
  background: var(--ob-border);
}

.notes-mobile-sel-bar {
  position: absolute;
  z-index: 58;
  left: 50%;
  bottom: calc(var(--console-composer-height, 135px) + 10px);
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  width: max-content;
  max-width: calc(100% - 24px);
  padding: 5px 6px 5px 14px;
  border: 1px solid var(--ob-chat-border);
  border-radius: 18px;
  background: rgb(var(--ob-surface-rgb) / .96);
  color: var(--ob-text);
  box-shadow: var(--ob-shadow-popover);
  -webkit-backdrop-filter: blur(18px);
  backdrop-filter: blur(18px);
  font-size: 12px;
}
html.dark .notes-mobile-sel-bar {
  background: rgb(var(--ob-surface-raised-rgb) / .96);
}
.notes-mobile-sel-count {
  white-space: nowrap;
  color: var(--ob-text-subtle);
  font-variant-numeric: tabular-nums;
}
.notes-mobile-sel-count strong {
  color: var(--ob-text-strong);
  font-weight: 600;
}
.notes-mobile-sel-actions {
  display: flex;
  align-items: center;
  gap: 3px;
}
.notes-mobile-sel-actions button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 10px;
  border: 0;
  border-radius: 11px;
  background: transparent;
  color: var(--ob-text);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}
.notes-mobile-sel-actions button.is-primary {
  background: var(--ob-text-strong);
  color: var(--ob-text-inverse);
}
.notes-mobile-sel-actions button.is-close {
  width: 34px;
  padding: 0;
  color: var(--ob-text-subtle);
}

@keyframes notes-pop-in {
  from { opacity: 0; transform: translateY(3px) scale(.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

@media (prefers-reduced-motion: reduce) {
  .notes-ctx-menu, .notes-sel-toolbar { animation: none; }
}
</style>
