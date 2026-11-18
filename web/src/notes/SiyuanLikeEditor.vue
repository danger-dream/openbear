<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { Editor, EditorContent } from '@tiptap/vue-3';
import { TextSelection } from '@tiptap/pm/state';
import { isInTable } from '@tiptap/pm/tables';
import { ElMessage } from 'element-plus';
import {
  AlignCenter,
  AlignLeft,
  AlignRight,
  ArrowDown,
  ArrowUp,
  Bold,
  ChevronRight,
  ClipboardCopy,
  Code,
  Copy,
  CornerDownLeft,
  Download,
  FileCode2,
  Highlighter,
  Image as ImageIcon,
  Italic,
  Layers,
  Link as LinkIcon,
  ListTree,
  Lock,
  Maximize2,
  Minimize2,
  Pin,
  Quote,
  Scissors,
  SmilePlus,
  Strikethrough,
  Trash2,
  Underline,
  Unlock,
  X,
} from '@lucide/vue';
import AdaptiveMdEditor from '../components/AdaptiveMdEditor.vue';
import ImageLightbox from '../artifacts/ImageLightbox.vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import ReferencePicker from '../references/ReferencePicker.vue';
import { REFERENCE_MIME, normalizeReference, referencesInText } from '../references/codec.js';
import { mentionAtPosition } from '../references/mention.js';
import { Api, apiError } from '../api.js';
import NoteBlockGutterIcon from './NoteBlockGutterIcon.vue';
import NoteIcon from './NoteIcon.vue';
import NoteTopToolbar from './NoteTopToolbar.vue';
import {
  blockSelectionPluginKey,
  canUndoRedo,
  createNoteEditorExtensions,
  deleteBlockAtPos,
  duplicateBlockAtPos,
  findInlineMemoRangeAtPos,
  insertParagraphAroundBlock,
  moveBlockByOffset,
  moveBlockToTopLevelPos,
  resolveBlockTargetAtPos,
  serializeSingleBlockMarkdown,
} from './editorSchema.js';
import {
  computeDocumentStats,
  docToMarkdown,
  extractOutlineFromMarkdown,
  looksLikeStructuredMarkdown,
  markdownToDoc,
} from './markdownCodec.js';
import './siyuanEditor.css';

const PRESET_EMOJIS = [
  '📄', '📝', '📁', '📓', '🏠', '👷',
  '🔑', '💡', '🚀', '🛠️', '🌐', '📦',
  '📊', '⚡', '🔒', '🧪', '🎯', '📌',
  '📚', '🧩', '🐳', '🐧', '🤖', '⭐',
];

const SLASH_COMMANDS = [
  { id: 'h1', label: '一级标题', desc: '大章节标题', icon: 'H1', shortcut: '#', keywords: 'h1 heading 标题 yijibiaoti' },
  { id: 'h2', label: '二级标题', desc: '中章节标题', icon: 'H2', shortcut: '##', keywords: 'h2 heading 标题 erjibiaoti' },
  { id: 'h3', label: '三级标题', desc: '小节标题', icon: 'H3', shortcut: '###', keywords: 'h3 heading 标题 sanjibiaoti' },
  { id: 'todo', label: '待办清单', desc: '可勾选任务项', icon: '☑', shortcut: '[]', keywords: 'todo task checkbox 待办 清单 renwu' },
  { id: 'bullet', label: '无序列表', desc: '圆点项目列表', icon: '•', shortcut: '-', keywords: 'list ul 无序 列表 liebiao' },
  { id: 'ordered', label: '有序列表', desc: '数字编号列表', icon: '1.', shortcut: '1.', keywords: 'ordered ol 有序 编号列表' },
  { id: 'code', label: '代码块', desc: '支持语法高亮与复制', icon: '</>', shortcut: '```', keywords: 'code block 代码块 daima' },
  { id: 'table', label: '表格', desc: '插入 3×3 可视化表格', icon: '⊞', shortcut: '|a|b|', keywords: 'table grid 表格 biaoge' },
  { id: 'quote', label: '引用块', desc: '提示或摘录段落', icon: '❝', shortcut: '>', keywords: 'quote blockquote 引用 yinyong' },
  { id: 'hr', label: '分割线', desc: '水平分隔线', icon: '—', shortcut: '---', keywords: 'hr divider 分割线 fengexian' },
  { id: 'image', label: '上传图片', desc: '选择本地图片插入', icon: '🖼', shortcut: '', keywords: 'image photo picture 图片 tupian' },
  { id: 'ref', label: '引用资源', desc: '插入会话/笔记/文档/服务器引用', icon: '@', shortcut: '@', keywords: 'ref mention 引用 会话 笔记' },
];

const props = defineProps({
  note: { type: Object, required: true },
  savingState: { type: String, default: 'saved' }, // 'saved' | 'saving' | 'dirty' | 'error' | 'conflict'
  phone: { type: Boolean, default: false },
});

const emit = defineEmits([
  'change-title',
  'change-icon',
  'change-content',
  'toggle-readonly',
  'navigate-note',
  'export-note',
]);

const editorMode = ref('wysiwyg'); // 'wysiwyg' | 'source'
const widePaper = ref(false);
const isFullscreen = ref(false);
const outlineOpen = ref(false);
const emojiOpen = ref(false);
const emojiBtnRef = ref(null);
const titleInputRef = ref(null);
const fileInputRef = ref(null);
const scrollStageRef = ref(null);
const paperRef = ref(null);
const memoTextareaRef = ref(null);

const editor = shallowRef(null);
const inTable = ref(false);
const historyState = ref({ undo: false, redo: false });
const activeTextStyleColor = ref('#e03131');
const activeBlockBreadcrumb = ref(null); // { kind, level }
const activeHeadingText = ref('');
const selectedBlockPos = ref(null);
const gutterTarget = ref(null); // { pos, kind, level, top, left, parentContainer }
const hoveredBlockPos = ref(null);
const titleHeaderHovered = ref(false);
let titleHoverLeaveTimer = null;
let gutterLeaveTimer = null;
let paperResizeObserver = null;
const blockMenu = ref({ open: false, anchor: null, pos: null, kind: 'paragraph', level: 1, parentContainer: null });
const blockDrag = ref({ active: false, fromPos: null, targetPos: null, lineTop: 0 });
const memoPopover = ref({
  open: false,
  pinned: false,
  locked: false,
  anchor: null,
  from: null,
  to: null,
  label: '',
  text: '',
});
let memoHoverOpenTimer = null;
let memoHoverCloseTimer = null;
const uploadingImage = ref(false);
const lightboxUrls = ref([]);
const lightboxIndex = ref(0);
const lightboxOpen = ref(false);

// Floating selection bubble state
const bubble = ref({ open: false, anchor: null, linkEditing: false, linkUrl: '' });
// Slash menu state
const slash = ref({ open: false, query: '', from: 0, to: 0, selected: 0, anchor: null });
// @ Reference picker state
const mention = ref(null);
const refPickerRef = ref(null);

let syncingFromProp = false;
let lastEmittedMarkdown = String(props.note?.content || '');

const isReadonly = computed(() => Boolean(props.note?.isReadonly));
const localTitle = computed({
  get: () => String(props.note?.title || ''),
  set: (val) => emit('change-title', String(val || '').replace(/[\r\n]+/g, ' ')),
});

function syncTitleHeight() {
  const el = titleInputRef.value;
  if (!el) return;
  el.style.height = '0px';
  el.style.height = `${Math.max(44, el.scrollHeight)}px`;
}

function onTitleHeaderEnter() {
  if (titleHoverLeaveTimer) {
    window.clearTimeout(titleHoverLeaveTimer);
    titleHoverLeaveTimer = null;
  }
  titleHeaderHovered.value = true;
}

function onTitleHeaderLeave() {
  if (titleHoverLeaveTimer) window.clearTimeout(titleHoverLeaveTimer);
  titleHoverLeaveTimer = window.setTimeout(() => {
    titleHoverLeaveTimer = null;
    if (!emojiOpen.value) {
      titleHeaderHovered.value = false;
    }
  }, 280);
}
const sourceMarkdown = computed({
  get: () => String(props.note?.content || ''),
  set: (val) => {
    lastEmittedMarkdown = val;
    emit('change-content', val);
  },
});

const hasCustomIcon = computed(() => {
  const raw = String(props.note?.icon || '').trim();
  return Boolean(raw && raw !== '📄' && raw !== '📓');
});

const outlineItems = computed(() => extractOutlineFromMarkdown(props.note?.content || ''));
const stats = computed(() => {
  const base = computeDocumentStats(props.note?.content || '');
  const blockCount = editor.value?.state?.doc?.childCount || (props.note?.content ? props.note.content.split(/\n{2,}/).length : 1);
  return { ...base, blocks: blockCount };
});

const currentReferences = computed(() => referencesInText(props.note?.content || ''));

const saveStatusLabel = computed(() => {
  if (props.savingState === 'saving') return '保存中…';
  if (props.savingState === 'dirty') return '待保存';
  if (props.savingState === 'error') return '保存失败';
  if (props.savingState === 'conflict') return '版本冲突，未保存';
  return '';
});

const filteredSlashCommands = computed(() => {
  const q = slash.value.query.trim().toLowerCase();
  if (!q) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter((item) =>
    `${item.label} ${item.desc} ${item.keywords}`.toLowerCase().includes(q),
  );
});

function coordsAnchor(view, pos) {
  const coords = view.coordsAtPos(Math.max(0, Math.min(pos, view.state.doc.content.size)));
  return {
    getBoundingClientRect: () => ({
      x: coords.left,
      y: coords.top,
      left: coords.left,
      right: coords.right,
      top: coords.top,
      bottom: coords.bottom,
      width: Math.max(1, coords.right - coords.left),
      height: Math.max(1, coords.bottom - coords.top),
    }),
  };
}

function selectionRectAnchor(view) {
  const { from, to } = view.state.selection;
  const start = view.coordsAtPos(from);
  const end = view.coordsAtPos(to);
  const left = Math.min(start.left, end.left);
  const right = Math.max(start.right, end.right);
  const top = Math.min(start.top, end.top);
  const bottom = Math.max(start.bottom, end.bottom);
  return {
    getBoundingClientRect: () => ({
      x: left,
      y: top,
      left,
      right,
      top,
      bottom,
      width: Math.max(8, right - left),
      height: Math.max(8, bottom - top),
    }),
  };
}

function collectNoteImages() {
  const urls = [];
  const doc = editor.value?.state?.doc || markdownToDoc(props.note?.content || '');
  const walk = (node) => {
    if (!node) return;
    const typeName = node.type?.name || node.type;
    if ((typeName === 'imageBlock' || typeName === 'inlineImage') && node.attrs?.src) {
      urls.push(node.attrs.src);
    }
    const children = node.content?.content || node.content || [];
    if (Array.isArray(children)) children.forEach(walk);
  };
  walk(doc);
  return [...new Set(urls)];
}

function openImageLightbox(clickedUrl) {
  const urls = collectNoteImages();
  const list = urls.length ? urls : [clickedUrl];
  const idx = Math.max(0, list.indexOf(clickedUrl));
  lightboxUrls.value = list;
  lightboxIndex.value = idx;
  lightboxOpen.value = true;
}

function detectSlashAtSelection(instance) {
  if (!instance || !instance.isEditable || instance.view.composing || !instance.isFocused) {
    slash.value.open = false;
    return;
  }
  const { selection } = instance.state;
  if (!selection.empty) {
    slash.value.open = false;
    return;
  }
  const { $from } = selection;
  if ($from.parent.type.name !== 'paragraph') {
    slash.value.open = false;
    return;
  }
  const textBefore = $from.parent.textBetween(0, $from.parentOffset, '\0', '\ufffc');
  const match = textBefore.match(/(?:^|\s)\/([a-zA-Z0-9\u4e00-\u9fff_-]{0,20})$/);
  if (!match) {
    slash.value.open = false;
    return;
  }
  const slashLen = match[1].length + 1;
  const from = selection.from - slashLen;
  slash.value = {
    open: true,
    query: match[1],
    from,
    to: selection.from,
    selected: 0,
    anchor: coordsAnchor(instance.view, from),
  };
}

function detectMentionAtSelection(instance) {
  if (!instance || !instance.isEditable || instance.view.composing || !instance.isFocused) {
    mention.value = null;
    return;
  }
  const { selection } = instance.state;
  if (!selection.empty) {
    mention.value = null;
    return;
  }
  if ($isInsideCode(selection.$from)) {
    mention.value = null;
    return;
  }
  const match = mentionAtPosition(selection.$from);
  if (!match) {
    mention.value = null;
    return;
  }
  mention.value = {
    ...match,
    anchor: coordsAnchor(instance.view, selection.from),
  };
}

function $isInsideCode($pos) {
  if ($pos.parent.type.spec.code) return true;
  return $pos.marks().some((m) => m.type.name === 'code');
}

function resolveDomForBlockPos(view, target) {
  if (!view || !target) return null;
  let dom = view.nodeDOM(target.pos);
  if (!dom || !(dom instanceof HTMLElement)) {
    const coords = view.coordsAtPos(Math.min(target.pos + 1, view.state.doc.content.size));
    const hit = document.elementFromPoint(coords.left + 8, coords.top + 6);
    dom = hit?.closest?.('.ProseMirror > *, li, blockquote, table, .sy-code-block, .sy-image-block') || null;
  }
  return dom instanceof HTMLElement ? dom : null;
}

function computeGutterGeometry(view, target) {
  const paperEl = paperRef.value;
  if (!view || !paperEl || !target) return null;
  const dom = resolveDomForBlockPos(view, target);
  if (!dom) return null;

  const paperRect = paperEl.getBoundingClientRect();
  const blockRect = dom.getBoundingClientRect();
  if (blockRect.height === 0 && blockRect.width === 0) return null;

  // For list items, place gutter to the left of the parent <ul>/<ol> so it never overlaps the bullet marker
  let alignLeftRect = blockRect;
  if (target.kind === 'listItem' || target.kind === 'orderedList' || target.kind === 'taskItem') {
    const parentListEl = dom.closest('ul, ol');
    if (parentListEl) alignLeftRect = parentListEl.getBoundingClientRect();
  }

  let offsetY = 2;
  if (target.kind === 'heading') {
    offsetY = target.level === 1 ? 8 : target.level === 2 ? 5 : 3;
  } else if (target.kind === 'codeBlock') {
    offsetY = 3;
  } else if (target.kind === 'table') {
    offsetY = 4;
  } else if (target.kind === 'horizontalRule') {
    offsetY = -11;
  }

  const top = Math.max(0, Math.round(blockRect.top - paperRect.top + offsetY));
  const rawLeft = Math.round(alignLeftRect.left - paperRect.left - 28);
  const left = Math.max(-52, rawLeft);

  return {
    pos: target.pos,
    kind: target.kind,
    level: target.level || 1,
    parentContainer: target.parentContainer || null,
    top,
    left,
  };
}

function refreshGutterFromPriority() {
  const instance = editor.value;
  if (!instance || !instance.view || editorMode.value !== 'wysiwyg') {
    gutterTarget.value = null;
    return;
  }
  const { state, view } = instance;
  const selState = blockSelectionPluginKey.getState(state);
  // Positions remembered before the document was replaced may lie beyond it.
  const inDoc = (pos) => typeof pos === 'number' && pos >= 0 && pos < state.doc.content.size;
  selectedBlockPos.value = inDoc(selState?.pos) ? selState.pos : null;

  // Priority: 1) hovered block, 2) block-selected node, 3) focused cursor block
  let target = null;
  if (inDoc(hoveredBlockPos.value) && state.doc.nodeAt(hoveredBlockPos.value)) {
    target = resolveBlockTargetAtPos(state.doc, hoveredBlockPos.value);
  }
  if (!target && typeof selectedBlockPos.value === 'number') {
    const selNode = state.doc.nodeAt(selectedBlockPos.value);
    if (selNode) {
      target = {
        pos: selectedBlockPos.value,
        endPos: selectedBlockPos.value + selNode.nodeSize,
        node: selNode,
        kind: selNode.type.name,
        level: Number(selNode.attrs?.level || 1),
        parentContainer: null,
      };
    }
  }
  if (!target && instance.isFocused) {
    target = resolveBlockTargetAtPos(state.doc, state.selection.from);
  }

  gutterTarget.value = target ? computeGutterGeometry(view, target) : null;
}

function findBlockTargetFromMouseEvent(event) {
  const instance = editor.value;
  if (!instance?.view) return null;
  const { view } = instance;
  const rawEl = event.target instanceof Element ? event.target : null;

  // Walk closest rendered block element inside ProseMirror first (works for codeBlock headers, tables, images, etc.)
  const blockEl = rawEl?.closest?.(
    '.sy-code-block, .sy-image-block, .tableWrapper, table, li.sy-task-item, li, blockquote, h1, h2, h3, h4, h5, h6, p, hr',
  );
  if (blockEl && view.dom.contains(blockEl)) {
    try {
      const domPos = view.posAtDOM(blockEl, 0);
      const resolved = resolveBlockTargetAtPos(view.state.doc, domPos);
      if (resolved) return resolved;
    } catch {
      // Fallback to coords below
    }
  }

  // Fallback for left/right margin hover: probe horizontally inside ProseMirror at current Y
  const pmRect = view.dom.getBoundingClientRect();
  if (event.clientY >= pmRect.top - 4 && event.clientY <= pmRect.bottom + 4) {
    const probeX = Math.max(pmRect.left + 14, Math.min(event.clientX, pmRect.right - 14));
    const hit = view.posAtCoords({ left: probeX, top: event.clientY });
    if (hit && typeof hit.pos === 'number') {
      return resolveBlockTargetAtPos(view.state.doc, hit.pos);
    }
  }
  return null;
}

function onPaperMouseMove(event) {
  if (blockDrag.value.active) return;
  const rawEl = event.target instanceof Element ? event.target : null;
  if (rawEl?.closest?.('.sy-block-gutter')) {
    if (gutterLeaveTimer) {
      window.clearTimeout(gutterLeaveTimer);
      gutterLeaveTimer = null;
    }
    return;
  }
  if (rawEl?.closest?.('.sy-doc-header')) {
    return;
  }

  // Sticky left-lane protection: when cursor moves left toward the currently visible block gutter button, keep it steady!
  const view = editor.value?.view;
  if (view && gutterTarget.value && paperRef.value) {
    const pmRect = view.dom.getBoundingClientRect();
    const paperRect = paperRef.value.getBoundingClientRect();
    const gutterClientTop = paperRect.top + gutterTarget.value.top;
    if (
      event.clientX <= pmRect.left + 8
      && event.clientX >= paperRect.left - 56
      && event.clientY >= gutterClientTop - 22
      && event.clientY <= gutterClientTop + 44
    ) {
      if (gutterLeaveTimer) {
        window.clearTimeout(gutterLeaveTimer);
        gutterLeaveTimer = null;
      }
      return;
    }
  }

  const target = findBlockTargetFromMouseEvent(event);
  if (target) {
    if (gutterLeaveTimer) {
      window.clearTimeout(gutterLeaveTimer);
      gutterLeaveTimer = null;
    }
    if (hoveredBlockPos.value !== target.pos) {
      hoveredBlockPos.value = target.pos;
    }
    refreshGutterFromPriority();
  } else if (hoveredBlockPos.value !== null && !gutterLeaveTimer) {
    gutterLeaveTimer = window.setTimeout(() => {
      gutterLeaveTimer = null;
      hoveredBlockPos.value = null;
      refreshGutterFromPriority();
    }, 260);
  }
}

function onPaperMouseLeave() {
  if (hoveredBlockPos.value !== null) {
    if (gutterLeaveTimer) window.clearTimeout(gutterLeaveTimer);
    gutterLeaveTimer = window.setTimeout(() => {
      gutterLeaveTimer = null;
      hoveredBlockPos.value = null;
      refreshGutterFromPriority();
    }, 260);
  }
}

const BLOCK_KIND_LABELS = {
  paragraph: '段落',
  heading: '标题',
  bulletList: '无序列表',
  orderedList: '有序列表',
  listItem: '列表项',
  taskList: '任务列表',
  taskItem: '任务项',
  codeBlock: '代码块',
  table: '表格',
  blockquote: '引用块',
  imageBlock: '图片块',
  horizontalRule: '分割线',
};

const CONVERTIBLE_KINDS = new Set([
  'paragraph',
  'heading',
  'bulletList',
  'orderedList',
  'listItem',
  'taskList',
  'taskItem',
  'blockquote',
  'codeBlock',
]);

function selectBlockAndFocus(pos) {
  const instance = editor.value;
  if (!instance || typeof pos !== 'number') return false;
  bubble.value.open = false;
  slash.value.open = false;
  instance.commands.selectBlockAt(pos);
  instance.view.dom.focus({ preventScroll: true });
  refreshGutterFromPriority();
  return true;
}

function openBlockMenuAt(target, pointOrRect) {
  if (!target || typeof target.pos !== 'number') return;
  selectBlockAndFocus(target.pos);
  const anchor = pointOrRect?.left !== undefined
    ? {
        getBoundingClientRect: () => ({
          x: pointOrRect.left,
          y: pointOrRect.top,
          left: pointOrRect.left,
          right: pointOrRect.right ?? pointOrRect.left + 1,
          top: pointOrRect.top,
          bottom: pointOrRect.bottom ?? pointOrRect.top + 1,
          width: Math.max(1, (pointOrRect.right ?? pointOrRect.left + 1) - pointOrRect.left),
          height: Math.max(1, (pointOrRect.bottom ?? pointOrRect.top + 1) - pointOrRect.top),
        }),
      }
    : null;
  blockMenu.value = {
    open: true,
    anchor,
    pos: target.pos,
    kind: target.kind || 'paragraph',
    level: target.level || 1,
    parentContainer: target.parentContainer || null,
  };
}

function onGutterLeftClick(event, target) {
  event.preventDefault();
  event.stopPropagation();
  if (!target) return;
  // First click selects the whole block cleanly (ready for Backspace/Delete/Ctrl+C); clicking an already-selected block's gutter opens the block menu!
  if (selectedBlockPos.value === target.pos && !blockMenu.value.open) {
    const rect = event.currentTarget?.getBoundingClientRect?.();
    openBlockMenuAt(target, rect || { left: event.clientX, top: event.clientY });
    return;
  }
  blockMenu.value.open = false;
  selectBlockAndFocus(target.pos);
}

function onGutterRightClick(event, target) {
  event.preventDefault();
  event.stopPropagation();
  if (!target) return;
  const rect = event.currentTarget?.getBoundingClientRect?.();
  openBlockMenuAt(target, rect || { left: event.clientX, top: event.clientY });
}

function resolveMemoFromDomElement(memoEl) {
  const instance = editor.value;
  if (!instance?.view || !(memoEl instanceof HTMLElement)) return null;
  let range = null;
  try {
    const domStart = instance.view.posAtDOM(memoEl, 0);
    const probe = Math.min(domStart + Math.min(1, (memoEl.textContent || '').length), instance.state.doc.content.size);
    range = findInlineMemoRangeAtPos(instance.state.doc, probe)
      || findInlineMemoRangeAtPos(instance.state.doc, domStart);
  } catch {
    range = null;
  }
  return {
    anchor: memoEl,
    from: range?.from ?? null,
    to: range?.to ?? null,
    label: range?.text || memoEl.textContent || '',
    text: range?.memo ?? memoEl.getAttribute('data-inline-memo') ?? '',
  };
}

function clearMemoHoverTimers() {
  if (memoHoverOpenTimer) {
    window.clearTimeout(memoHoverOpenTimer);
    memoHoverOpenTimer = null;
  }
  if (memoHoverCloseTimer) {
    window.clearTimeout(memoHoverCloseTimer);
    memoHoverCloseTimer = null;
  }
}

function closeMemoPopover(force = false) {
  clearMemoHoverTimers();
  if (!force && memoPopover.value.pinned) return;
  memoPopover.value.open = false;
  memoPopover.value.locked = false;
  if (force) memoPopover.value.pinned = false;
}

function openMemoPopoverData(info, { locked = true, focusInput = false } = {}) {
  if (!info) return;
  clearMemoHoverTimers();
  memoPopover.value = {
    open: true,
    pinned: memoPopover.value.pinned,
    locked,
    anchor: info.anchor,
    from: info.from,
    to: info.to,
    label: info.label || '',
    text: info.text || '',
  };
  if (focusInput && !isReadonly.value) {
    nextTick(() => {
      memoTextareaRef.value?.focus();
    });
  }
}

function onProtyleMouseOver(event) {
  if (props.phone) return;
  const memoEl = event.target instanceof Element ? event.target.closest('.sy-inline-memo') : null;
  if (!memoEl) return;
  if (memoHoverCloseTimer) {
    window.clearTimeout(memoHoverCloseTimer);
    memoHoverCloseTimer = null;
  }
  if (memoPopover.value.pinned || memoPopover.value.locked) return;
  const info = resolveMemoFromDomElement(memoEl);
  if (!info) return;
  if (memoPopover.value.open && memoPopover.value.from === info.from) return;
  if (memoHoverOpenTimer) window.clearTimeout(memoHoverOpenTimer);
  memoHoverOpenTimer = window.setTimeout(() => {
    memoHoverOpenTimer = null;
    if (!memoPopover.value.pinned && !memoPopover.value.locked) {
      openMemoPopoverData(info, { locked: false, focusInput: false });
    }
  }, 150);
}

function onProtyleMouseOut(event) {
  if (props.phone) return;
  const memoEl = event.target instanceof Element ? event.target.closest('.sy-inline-memo') : null;
  if (!memoEl) return;
  if (memoHoverOpenTimer) {
    window.clearTimeout(memoHoverOpenTimer);
    memoHoverOpenTimer = null;
  }
  if (memoPopover.value.pinned || memoPopover.value.locked) return;
  scheduleMemoHoverClose();
}

function cancelMemoHoverClose() {
  if (memoHoverCloseTimer) {
    window.clearTimeout(memoHoverCloseTimer);
    memoHoverCloseTimer = null;
  }
}

function scheduleMemoHoverClose() {
  if (memoPopover.value.pinned || memoPopover.value.locked) return;
  cancelMemoHoverClose();
  memoHoverCloseTimer = window.setTimeout(() => {
    memoHoverCloseTimer = null;
    if (!memoPopover.value.pinned && !memoPopover.value.locked) {
      memoPopover.value.open = false;
    }
  }, 220);
}

function onProtyleClick(event) {
  const memoEl = event.target instanceof Element ? event.target.closest('.sy-inline-memo') : null;
  if (!memoEl) return;
  const info = resolveMemoFromDomElement(memoEl);
  if (!info) return;
  openMemoPopoverData(info, { locked: true, focusInput: false });
}

function triggerInlineMemo(fallbackAnchor = null) {
  const instance = editor.value;
  if (!instance?.view) return false;
  const { state, view } = instance;
  const { selection, doc } = state;

  // 1. Already inside an inlineMemo? Open it for viewing / editing!
  const existing = findInlineMemoRangeAtPos(doc, selection.from)
    || (!selection.empty ? findInlineMemoRangeAtPos(doc, selection.from + 1) : null);
  if (existing) {
    bubble.value.open = false;
    openMemoPopoverData(
      {
        anchor: coordsAnchor(view, existing.from),
        from: existing.from,
        to: existing.to,
        label: existing.text,
        text: existing.memo,
      },
      { locked: true, focusInput: !isReadonly.value },
    );
    return true;
  }

  if (isReadonly.value) return false;

  // 2. Non-empty text selection: create inlineMemo and focus textarea
  if (!selection.empty) {
    const { from, to } = selection;
    const selectedText = doc.textBetween(from, to, ' ');
    instance.chain().focus().setInlineMemo('').run();
    bubble.value.open = false;
    openMemoPopoverData(
      {
        anchor: selectionRectAnchor(view) || fallbackAnchor,
        from,
        to,
        label: selectedText,
        text: '',
      },
      { locked: true, focusInput: true },
    );
    return true;
  }

  ElMessage.info('请先选中需要添加备注的文字（或按 Ctrl+Alt+M）');
  return true;
}

function onMemoInput(event) {
  const val = String(event.target?.value ?? '');
  memoPopover.value.text = val;
  const instance = editor.value;
  const { from, to } = memoPopover.value;
  if (!instance || isReadonly.value || typeof from !== 'number' || typeof to !== 'number') return;
  const memoType = instance.state.schema.marks.inlineMemo;
  if (!memoType || to > instance.state.doc.content.size) return;
  const tr = instance.state.tr
    .removeMark(from, to, memoType)
    .addMark(from, to, memoType.create({ memo: val }));
  instance.view.dispatch(tr);
}

async function copyMemoContent() {
  await writeClipboardText(memoPopover.value.text || '');
  ElMessage.success('已复制备注内容');
}

function removeCurrentMemo() {
  const instance = editor.value;
  const { from, to } = memoPopover.value;
  if (!instance || isReadonly.value || typeof from !== 'number' || typeof to !== 'number') return;
  const memoType = instance.state.schema.marks.inlineMemo;
  if (memoType && to <= instance.state.doc.content.size) {
    const tr = instance.state.tr.removeMark(from, to, memoType);
    instance.view.dispatch(tr);
  }
  closeMemoPopover(true);
}

function onProtyleContextMenu(event) {
  if (event.shiftKey) return;
  const instance = editor.value;
  if (!instance?.view) return;

  // SiYuan Inline Memo right-click: show floating memo popover directly!
  const memoEl = event.target instanceof Element ? event.target.closest('.sy-inline-memo') : null;
  if (memoEl) {
    event.preventDefault();
    event.stopPropagation();
    blockMenu.value.open = false;
    bubble.value.open = false;
    const info = resolveMemoFromDomElement(memoEl);
    openMemoPopoverData(info, { locked: true, focusInput: false });
    return;
  }

  const { selection } = instance.state;
  // Keep native browser copy/lookup menu if user actively highlighted an inline text range
  if (!selection.empty && selectedBlockPos.value === null) return;

  const target = findBlockTargetFromMouseEvent(event);
  if (!target) return;
  event.preventDefault();
  event.stopPropagation();
  closeMemoPopover(false);
  openBlockMenuAt(target, { left: event.clientX, top: event.clientY });
}

async function writeClipboardText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    return true;
  }
}

function turnSelectedBlockInto(targetType, attrs = {}) {
  const instance = editor.value;
  const pos = blockMenu.value.pos;
  blockMenu.value.open = false;
  if (!instance || isReadonly.value || typeof pos !== 'number') return;
  const node = instance.state.doc.nodeAt(pos);
  if (!node) return;

  const plainText = node.textContent || '';
  const { schema } = instance.state;
  let replacement = null;
  const inlineText = plainText ? [schema.text(plainText)] : [];

  if (targetType === 'paragraph') {
    replacement = schema.nodes.paragraph.create(null, inlineText);
  } else if (targetType === 'heading') {
    replacement = schema.nodes.heading.create({ level: attrs.level || 2 }, inlineText);
  } else if (targetType === 'codeBlock') {
    replacement = schema.nodes.codeBlock.create({ language: '' }, inlineText);
  } else if (targetType === 'blockquote') {
    replacement = schema.nodes.blockquote.create(null, [schema.nodes.paragraph.create(null, inlineText)]);
  } else if (targetType === 'bulletList') {
    replacement = schema.nodes.bulletList.create(null, [
      schema.nodes.listItem.create(null, [schema.nodes.paragraph.create(null, inlineText)]),
    ]);
  } else if (targetType === 'orderedList') {
    replacement = schema.nodes.orderedList.create({ start: 1 }, [
      schema.nodes.listItem.create(null, [schema.nodes.paragraph.create(null, inlineText)]),
    ]);
  } else if (targetType === 'taskList') {
    replacement = schema.nodes.taskList.create(null, [
      schema.nodes.taskItem.create({ checked: false }, [schema.nodes.paragraph.create(null, inlineText)]),
    ]);
  }
  if (!replacement) return;

  // If converting a single listItem inside a multi-item list, only convert if top-level or replace single-item list
  const $pos = instance.state.doc.resolve(pos);
  let from = pos;
  let to = pos + node.nodeSize;
  if ($pos.depth >= 1 && ['bulletList', 'orderedList', 'taskList'].includes($pos.parent.type.name) && $pos.parent.childCount === 1) {
    from = $pos.before($pos.depth);
    to = $pos.after($pos.depth);
  } else if ($pos.depth >= 1 && ['listItem', 'taskItem'].includes(node.type.name)) {
    // Lift single list item out if converting inside multi-item list: focus inside it and run chain
    const textPos = Math.min(pos + 2, instance.state.doc.content.size);
    instance.chain().focus(textPos).liftListItem(node.type.name).run();
    return;
  }

  const tr = instance.state.tr.replaceWith(from, to, replacement);
  tr.setMeta(blockSelectionPluginKey, { pos: from }).setMeta('keepBlockSelection', true);
  instance.view.dispatch(tr.scrollIntoView());
  instance.view.dom.focus({ preventScroll: true });
  refreshGutterFromPriority();
}

async function runBlockMenuAction(action) {
  const instance = editor.value;
  const pos = blockMenu.value.pos;
  const parentContainer = blockMenu.value.parentContainer;
  blockMenu.value.open = false;
  if (!instance || typeof pos !== 'number') return;
  const node = instance.state.doc.nodeAt(pos);
  if (!node) return;

  if (action === 'copy-md') {
    const md = serializeSingleBlockMarkdown(node);
    await writeClipboardText(md);
    ElMessage.success('已复制块 Markdown');
    return;
  }
  if (action === 'copy-text') {
    await writeClipboardText(node.textContent || '');
    ElMessage.success('已复制纯文本');
    return;
  }
  if (action === 'select-parent' && parentContainer) {
    selectBlockAndFocus(parentContainer.pos);
    return;
  }
  if (isReadonly.value) return;

  if (action === 'cut') {
    const md = serializeSingleBlockMarkdown(node);
    await writeClipboardText(md);
    deleteBlockAtPos(instance.state, instance.view.dispatch, pos);
    hoveredBlockPos.value = null;
    refreshGutterFromPriority();
    return;
  }
  if (action === 'duplicate') {
    duplicateBlockAtPos(instance.state, instance.view.dispatch, pos);
    instance.view.dom.focus({ preventScroll: true });
    refreshGutterFromPriority();
    return;
  }
  if (action === 'insert-before') {
    insertParagraphAroundBlock(instance.state, instance.view.dispatch, pos, 'before');
    refreshGutterFromPriority();
    return;
  }
  if (action === 'insert-after') {
    insertParagraphAroundBlock(instance.state, instance.view.dispatch, pos, 'after');
    refreshGutterFromPriority();
    return;
  }
  if (action === 'move-up') {
    moveBlockByOffset(instance.state, instance.view.dispatch, pos, -1);
    instance.view.dom.focus({ preventScroll: true });
    refreshGutterFromPriority();
    return;
  }
  if (action === 'move-down') {
    moveBlockByOffset(instance.state, instance.view.dispatch, pos, 1);
    instance.view.dom.focus({ preventScroll: true });
    refreshGutterFromPriority();
    return;
  }
  if (action === 'delete') {
    deleteBlockAtPos(instance.state, instance.view.dispatch, pos);
    hoveredBlockPos.value = null;
    refreshGutterFromPriority();
  }
}

// Block Gutter Drag-and-Drop Reorder
function onGutterDragStart(event, target) {
  if (isReadonly.value || !target) {
    event.preventDefault();
    return;
  }
  selectBlockAndFocus(target.pos);
  blockDrag.value = { active: true, fromPos: target.pos, targetPos: null, lineTop: 0 };
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('application/x-openbear-note-block', String(target.pos));
}

function onPaperDragOver(event) {
  if (!blockDrag.value.active || !editor.value?.view || !paperRef.value) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const { view } = editor.value;
  const { doc } = view.state;
  const paperRect = paperRef.value.getBoundingClientRect();

  // Snap to nearest top-level block boundary
  let bestPos = 0;
  let bestDist = Infinity;
  let bestTop = 0;
  doc.forEach((child, offset) => {
    const dom = view.nodeDOM(offset);
    if (!(dom instanceof HTMLElement)) return;
    const r = dom.getBoundingClientRect();
    const dTop = Math.abs(event.clientY - r.top);
    if (dTop < bestDist) {
      bestDist = dTop;
      bestPos = offset;
      bestTop = r.top - paperRect.top - 2;
    }
    const dBottom = Math.abs(event.clientY - r.bottom);
    if (dBottom < bestDist) {
      bestDist = dBottom;
      bestPos = offset + child.nodeSize;
      bestTop = r.bottom - paperRect.top + 2;
    }
  });
  blockDrag.value.targetPos = bestPos;
  blockDrag.value.lineTop = Math.max(0, Math.round(bestTop));
}

function onPaperDrop(event) {
  if (!blockDrag.value.active || !editor.value?.view) return;
  event.preventDefault();
  const { fromPos, targetPos } = blockDrag.value;
  blockDrag.value = { active: false, fromPos: null, targetPos: null, lineTop: 0 };
  if (typeof fromPos === 'number' && typeof targetPos === 'number') {
    moveBlockToTopLevelPos(editor.value.state, editor.value.view.dispatch, fromPos, targetPos);
    editor.value.view.dom.focus({ preventScroll: true });
    refreshGutterFromPriority();
  }
}

function onGutterDragEnd() {
  blockDrag.value = { active: false, fromPos: null, targetPos: null, lineTop: 0 };
}

function onBlockMenuGlobalKeydown(event) {
  if (event.key === 'Escape' && isFullscreen.value && !blockMenu.value.open && !memoPopover.value.open) {
    event.preventDefault();
    isFullscreen.value = false;
    return;
  }
  if (!blockMenu.value.open) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    blockMenu.value.open = false;
    return;
  }
  if ((event.key === 'Backspace' || event.key === 'Delete') && !isReadonly.value) {
    event.preventDefault();
    void runBlockMenuAction('delete');
    return;
  }
  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'c') {
    event.preventDefault();
    void runBlockMenuAction('copy-md');
    return;
  }
  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'x' && !isReadonly.value) {
    event.preventDefault();
    void runBlockMenuAction('cut');
  }
}

function updateFloatingStates() {
  const instance = editor.value;
  if (!instance) return;
  inTable.value = isInTable(instance.state);
  historyState.value = canUndoRedo(instance.state);
  const styleAttrs = instance.getAttributes('textStyle');
  if (styleAttrs?.color) activeTextStyleColor.value = styleAttrs.color;

  // Track nearest preceding heading & current block icon for SiYuan-style breadcrumb trail
  const { selection, doc } = instance.state;
  const curTarget = resolveBlockTargetAtPos(doc, selection.from);
  activeBlockBreadcrumb.value = curTarget ? { kind: curTarget.kind, level: curTarget.level } : null;
  let foundHeading = '';
  doc.forEach((node, offset) => {
    if (offset <= selection.from && node.type.name === 'heading') {
      const txt = node.textContent?.trim();
      if (txt) foundHeading = txt;
    }
  });
  activeHeadingText.value = foundHeading;
  refreshGutterFromPriority();

  detectSlashAtSelection(instance);
  detectMentionAtSelection(instance);

  if (
    !instance.isEditable
    || selection.empty
    || instance.isActive('codeBlock')
    || selection.node
  ) {
    if (!bubble.value.linkEditing) bubble.value.open = false;
    return;
  }
  bubble.value = {
    open: true,
    anchor: selectionRectAnchor(instance.view),
    linkEditing: false,
    linkUrl: instance.getAttributes('link')?.href || '',
  };
}

async function uploadAndInsertFiles(files) {
  const instance = editor.value;
  if (!instance || !instance.isEditable) return;
  const imageFiles = [...files].filter((f) => f && String(f.type || '').startsWith('image/'));
  if (!imageFiles.length) return;
  uploadingImage.value = true;
  try {
    for (const file of imageFiles) {
      const res = await Api.uploadKbNoteAsset(file, props.note?.noteUuid || '');
      const asset = res?.asset;
      if (asset?.url) {
        instance
          .chain()
          .focus()
          .insertImageBlock({
            src: asset.url,
            alt: (file.name || '').replace(/\.[^.]+$/, ''),
          })
          .run();
      }
    }
  } catch (err) {
    ElMessage.error(`图片上传失败：${apiError(err)}`);
  } finally {
    uploadingImage.value = false;
  }
}

function insertReferences(input, range = null) {
  const instance = editor.value;
  if (!instance || !instance.isEditable) return;
  const values = (Array.isArray(input) ? input : [input]).map(normalizeReference).filter(Boolean);
  if (!values.length) return;
  const target = range || mention.value || instance.state.selection;
  const from = Math.min(Number(target.from), instance.state.doc.content.size);
  const to = Math.min(Number(target.to ?? target.from), instance.state.doc.content.size);
  instance
    .chain()
    .focus()
    .insertContentAt(
      { from, to },
      values.flatMap((value) => [{ type: 'reference', attrs: value }, { type: 'text', text: ' ' }]),
    )
    .run();
  mention.value = null;
}

function runSlashCommand(cmd = filteredSlashCommands.value[slash.value.selected]) {
  const instance = editor.value;
  if (!cmd || !instance) return;
  const { from, to } = slash.value;
  slash.value.open = false;
  instance.chain().focus().deleteRange({ from, to }).run();

  switch (cmd.id) {
    case 'h1':
      instance.chain().focus().setHeading({ level: 1 }).run();
      break;
    case 'h2':
      instance.chain().focus().setHeading({ level: 2 }).run();
      break;
    case 'h3':
      instance.chain().focus().setHeading({ level: 3 }).run();
      break;
    case 'todo':
      instance.chain().focus().toggleTaskList().run();
      break;
    case 'bullet':
      instance.chain().focus().toggleBulletList().run();
      break;
    case 'ordered':
      instance.chain().focus().toggleOrderedList().run();
      break;
    case 'code':
      instance.chain().focus().setCodeBlock({ language: 'bash' }).run();
      break;
    case 'table':
      instance.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
      break;
    case 'quote':
      instance.chain().focus().toggleBlockquote().run();
      break;
    case 'hr':
      instance.chain().focus().setHorizontalRule().run();
      break;
    case 'image':
      fileInputRef.value?.click();
      break;
    case 'ref':
      instance.chain().focus().insertContent('@').run();
      updateFloatingStates();
      break;
    default:
      break;
  }
}

function mountTiptap() {
  editor.value?.destroy();
  const initialDoc = markdownToDoc(props.note?.content || '');
  lastEmittedMarkdown = String(props.note?.content || '');
  editor.value = new Editor({
    editable: !isReadonly.value,
    content: initialDoc,
    extensions: createNoteEditorExtensions({
      onImagePreview: openImageLightbox,
      onTriggerMemoShortcut: () => triggerInlineMemo(),
    }),
    editorProps: {
      attributes: {
        class: 'sy-protyle-content',
        'aria-label': '笔记正文编辑器',
        spellcheck: 'false',
      },
      handleKeyDown(view, event) {
        if (event.isComposing || view.composing || event.keyCode === 229) return false;
        if (mention.value && refPickerRef.value?.key(event)) return true;
        if (slash.value.open) {
          const list = filteredSlashCommands.value;
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            slash.value.selected = (slash.value.selected + 1) % Math.max(1, list.length);
            return true;
          }
          if (event.key === 'ArrowUp') {
            event.preventDefault();
            slash.value.selected = (slash.value.selected - 1 + Math.max(1, list.length)) % Math.max(1, list.length);
            return true;
          }
          if (event.key === 'Enter' || event.key === 'Tab') {
            if (list.length) {
              event.preventDefault();
              runSlashCommand(list[slash.value.selected]);
              return true;
            }
          }
          if (event.key === 'Escape') {
            event.preventDefault();
            slash.value.open = false;
            return true;
          }
        }
        return false;
      },
      handlePaste(view, event) {
        if (!editor.value?.isEditable) return false;
        const files = event.clipboardData?.files;
        if (files?.length && [...files].some((f) => String(f.type || '').startsWith('image/'))) {
          event.preventDefault();
          void uploadAndInsertFiles(files);
          return true;
        }
        const customRef = event.clipboardData?.getData(REFERENCE_MIME);
        if (customRef) {
          try {
            const parsed = JSON.parse(customRef);
            if (normalizeReference(parsed)) {
              event.preventDefault();
              insertReferences(parsed);
              return true;
            }
          } catch {
            // Fall through
          }
        }
        const html = event.clipboardData?.getData('text/html');
        const plain = event.clipboardData?.getData('text/plain') || '';
        const { $from } = view.state.selection;
        if (!$from.parent.type.spec.code && !html && looksLikeStructuredMarkdown(plain)) {
          event.preventDefault();
          const parsedDoc = markdownToDoc(plain);
          if (parsedDoc.content?.length) {
            editor.value.chain().focus().insertContent(parsedDoc.content).run();
            return true;
          }
        }
        return false;
      },
      handleDrop(view, event) {
        if (!editor.value?.isEditable) return false;
        const rawRef = event.dataTransfer?.getData(REFERENCE_MIME);
        if (rawRef) {
          event.preventDefault();
          try {
            const parsed = JSON.parse(rawRef);
            const coords = view.posAtCoords({ left: event.clientX, top: event.clientY });
            insertReferences(parsed, coords ? { from: coords.pos, to: coords.pos } : null);
          } catch {
            // Ignore malformed drop payload
          }
          return true;
        }
        const files = event.dataTransfer?.files;
        if (files?.length && [...files].some((f) => String(f.type || '').startsWith('image/'))) {
          event.preventDefault();
          void uploadAndInsertFiles(files);
          return true;
        }
        return false;
      },
      handleClickOn(_view, _pos, node) {
        if (node?.type?.name === 'inlineImage' && node.attrs?.src) {
          openImageLightbox(node.attrs.src);
          return true;
        }
        return false;
      },
    },
    onUpdate({ editor: instance }) {
      if (syncingFromProp) return;
      const md = docToMarkdown(instance.getJSON());
      lastEmittedMarkdown = md;
      emit('change-content', md);
      updateFloatingStates();
    },
    onSelectionUpdate() {
      updateFloatingStates();
    },
    onTransaction() {
      if (editor.value?.state) {
        historyState.value = canUndoRedo(editor.value.state);
      }
      refreshGutterFromPriority();
    },
    onBlur() {
      window.setTimeout(() => {
        if (!editor.value?.isFocused && !bubble.value.linkEditing) {
          bubble.value.open = false;
          slash.value.open = false;
        }
        refreshGutterFromPriority();
      }, 150);
    },
  });
}

watch(
  () => [props.note?.noteUuid, props.note?.content],
  ([newUuid, newContent], [oldUuid] = []) => {
    const incoming = String(newContent || '');
    if (newUuid !== oldUuid) {
      bubble.value.open = false;
      slash.value.open = false;
      blockMenu.value.open = false;
      closeMemoPopover(true);
      hoveredBlockPos.value = null;
      selectedBlockPos.value = null;
      mention.value = null;
      lastEmittedMarkdown = incoming;
      if (editor.value) {
        syncingFromProp = true;
        editor.value.commands.setContent(markdownToDoc(incoming), { emitUpdate: false });
        syncingFromProp = false;
      }
      activeHeadingText.value = '';
      if (scrollStageRef.value) scrollStageRef.value.scrollTop = 0;
      nextTick(syncTitleHeight);
      return;
    }
    if (incoming !== lastEmittedMarkdown && editor.value && editorMode.value === 'wysiwyg') {
      blockMenu.value.open = false;
      hoveredBlockPos.value = null;
      selectedBlockPos.value = null;
      syncingFromProp = true;
      editor.value.commands.setContent(markdownToDoc(incoming), { emitUpdate: false });
      lastEmittedMarkdown = incoming;
      syncingFromProp = false;
    }
  },
);

watch(isReadonly, (ro) => {
  editor.value?.setEditable(!ro);
  if (ro) {
    bubble.value.open = false;
    slash.value.open = false;
    mention.value = null;
  }
});

function switchMode(targetMode) {
  if (editorMode.value === targetMode) return;
  if (targetMode === 'wysiwyg' && editor.value) {
    syncingFromProp = true;
    editor.value.commands.setContent(markdownToDoc(props.note?.content || ''), { emitUpdate: false });
    syncingFromProp = false;
  }
  bubble.value.open = false;
  slash.value.open = false;
  mention.value = null;
  editorMode.value = targetMode;
}

watch(
  () => [props.note?.title, widePaper.value, editorMode.value, isFullscreen.value],
  () => {
    nextTick(syncTitleHeight);
  },
);

function onTitleKeydown(event) {
  if (event.key === 'Enter') {
    event.preventDefault();
    if (editorMode.value === 'wysiwyg' && editor.value) {
      editor.value.commands.focus('start');
    }
    return;
  }
  if (event.key === 'ArrowDown') {
    const el = event.target;
    if (el && el.selectionStart === el.value.length) {
      event.preventDefault();
      if (editorMode.value === 'wysiwyg' && editor.value) {
        editor.value.commands.focus('start');
      }
    }
  }
}

function chooseEmoji(emoji) {
  emit('change-icon', emoji);
  emojiOpen.value = false;
}

function openLinkInput(customAnchor = null) {
  const instance = editor.value;
  if (!instance) return;
  const currentHref = instance.getAttributes('link')?.href || '';
  if (instance.state.selection.empty && !currentHref) {
    ElMessage.info('请先选中需要添加链接的文字');
    return;
  }
  bubble.value = {
    open: true,
    anchor: customAnchor || bubble.value.anchor || selectionRectAnchor(instance.view),
    linkEditing: true,
    linkUrl: currentHref || 'https://',
  };
}

function runToolbarInsertBlock(blockId) {
  const instance = editor.value;
  if (!instance || isReadonly.value) return;
  switch (blockId) {
    case 'h1':
      instance.chain().focus().toggleHeading({ level: 1 }).run();
      break;
    case 'h2':
      instance.chain().focus().toggleHeading({ level: 2 }).run();
      break;
    case 'h3':
      instance.chain().focus().toggleHeading({ level: 3 }).run();
      break;
    case 'todo':
      instance.chain().focus().toggleTaskList().run();
      break;
    case 'bullet':
      instance.chain().focus().toggleBulletList().run();
      break;
    case 'ordered':
      instance.chain().focus().toggleOrderedList().run();
      break;
    case 'quote':
      instance.chain().focus().toggleBlockquote().run();
      break;
    case 'code':
      instance.chain().focus().toggleCodeBlock({ language: 'bash' }).run();
      break;
    case 'table':
      instance.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
      break;
    case 'hr':
      instance.chain().focus().setHorizontalRule().run();
      break;
    default:
      break;
  }
}

function triggerToolbarReference() {
  const instance = editor.value;
  if (!instance || isReadonly.value) return;
  instance.chain().focus().insertContent('@').run();
  updateFloatingStates();
}

function applyLink() {
  const instance = editor.value;
  if (!instance) return;
  const href = bubble.value.linkUrl.trim();
  if (!href || href === 'https://') {
    instance.chain().focus().unsetLink().run();
  } else if (!instance.chain().focus().setLink({ href }).run()) {
    ElMessage.warning('只支持 http(s)、mailto、tel 或相对地址的链接');
    return;
  }
  bubble.value.linkEditing = false;
}

function jumpToHeading(headingIndex) {
  if (editorMode.value !== 'wysiwyg') {
    switchMode('wysiwyg');
  }
  nextTick(() => {
    const container = scrollStageRef.value;
    if (!container) return;
    const headings = container.querySelectorAll('.ProseMirror h1, .ProseMirror h2, .ProseMirror h3, .ProseMirror h4, .ProseMirror h5, .ProseMirror h6');
    const target = headings[headingIndex];
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
}

function onFileSelected(event) {
  const files = event.target?.files;
  if (files?.length) {
    void uploadAndInsertFiles(files);
  }
  if (event.target) event.target.value = '';
}

function onExternalInsertRef(event) {
  if (editorMode.value === 'wysiwyg' && editor.value?.isEditable) {
    insertReferences(event.detail?.reference || event.detail);
  }
}

onMounted(() => {
  mountTiptap();
  nextTick(() => {
    syncTitleHeight();
    if (typeof ResizeObserver !== 'undefined' && paperRef.value) {
      paperResizeObserver = new ResizeObserver(() => syncTitleHeight());
      paperResizeObserver.observe(paperRef.value);
    }
  });
  window.addEventListener('openbear:insert-note-reference', onExternalInsertRef);
  window.addEventListener('keydown', onBlockMenuGlobalKeydown);
});

onBeforeUnmount(() => {
  clearMemoHoverTimers();
  if (titleHoverLeaveTimer) window.clearTimeout(titleHoverLeaveTimer);
  if (gutterLeaveTimer) window.clearTimeout(gutterLeaveTimer);
  paperResizeObserver?.disconnect();
  paperResizeObserver = null;
  window.removeEventListener('openbear:insert-note-reference', onExternalInsertRef);
  window.removeEventListener('keydown', onBlockMenuGlobalKeydown);
  editor.value?.destroy();
});

defineExpose({
  focusTitle: () => titleInputRef.value?.focus(),
  focusBody: () => editor.value?.commands.focus('end'),
});
</script>

<template>
  <div class="sy-editor-shell" :class="{ 'is-fullscreen': isFullscreen }">
    <!-- Top bar: breadcrumbs + mode switch + lock + TOC + export -->
    <div class="sy-editor-topbar">
      <div class="sy-topbar-leading">
        <slot name="leading" />
      </div>
      <nav class="sy-breadcrumbs" aria-label="笔记路径">
        <template v-for="anc in (note.ancestors || [])" :key="anc.noteUuid">
          <button
            type="button"
            class="sy-crumb-btn"
            :title="anc.title"
            @click="emit('navigate-note', anc.noteUuid)"
          >
            <NoteIcon :icon="anc.icon" :has-children="true" :size="13" />
            <span>{{ anc.title }}</span>
          </button>
          <ChevronRight :size="12" class="sy-crumb-sep" />
        </template>
        <span class="sy-crumb-current" :title="note.title">
          <NoteIcon :icon="note.icon" :has-children="Boolean(note.childCount)" :size="13" />
          <span>{{ note.title || '未命名笔记' }}</span>
        </span>
        <template v-if="activeHeadingText">
          <ChevronRight :size="12" class="sy-crumb-sep" />
          <span class="sy-crumb-heading" :title="activeHeadingText">{{ activeHeadingText }}</span>
        </template>
        <template v-if="activeBlockBreadcrumb && editorMode === 'wysiwyg'">
          <ChevronRight :size="12" class="sy-crumb-sep" />
          <span class="sy-crumb-block-icon" :title="`当前块：${BLOCK_KIND_LABELS[activeBlockBreadcrumb.kind] || '段落'}`">
            <NoteBlockGutterIcon :kind="activeBlockBreadcrumb.kind" :level="activeBlockBreadcrumb.level" />
          </span>
        </template>
      </nav>

      <div class="sy-topbar-actions">
        <span
          v-if="uploadingImage || saveStatusLabel"
          class="sy-save-badge"
          :class="{
            'is-saving': savingState === 'saving' || uploadingImage,
            'is-dirty': savingState === 'dirty',
            'is-error': savingState === 'error' || savingState === 'conflict',
          }"
          role="status"
        >
          {{ uploadingImage ? '上传图片中…' : saveStatusLabel }}
        </span>

        <div class="sy-mode-segmented" role="tablist" aria-label="编辑模式">
          <button
            type="button"
            role="tab"
            :aria-selected="editorMode === 'wysiwyg'"
            :class="{ 'is-active': editorMode === 'wysiwyg' }"
            @click="switchMode('wysiwyg')"
          >
            所见即所得
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="editorMode === 'source'"
            :class="{ 'is-active': editorMode === 'source' }"
            @click="switchMode('source')"
          >
            源码
          </button>
        </div>

        <button
          v-if="!phone && editorMode === 'wysiwyg'"
          type="button"
          class="sy-icon-btn"
          :class="{ 'is-active': widePaper }"
          :title="widePaper ? '切换标准阅读宽度' : '切换超宽画布（适合大表格）'"
          :aria-label="widePaper ? '切换标准阅读宽度' : '切换超宽画布'"
          @click="widePaper = !widePaper"
        >
          <Minimize2 v-if="widePaper" :size="15" />
          <Maximize2 v-else :size="15" />
        </button>

        <button
          type="button"
          class="sy-icon-btn"
          :class="{ 'is-locked': isReadonly }"
          :title="isReadonly ? '当前只读锁定，点击解锁编辑' : '锁定为只读防止误改'"
          :aria-label="isReadonly ? '解锁编辑' : '开启只读锁定'"
          :aria-pressed="isReadonly"
          @click="emit('toggle-readonly', !isReadonly)"
        >
          <Lock v-if="isReadonly" :size="15" />
          <Unlock v-else :size="15" />
        </button>

        <button
          v-if="!phone"
          type="button"
          class="sy-icon-btn"
          :class="{ 'is-active': outlineOpen }"
          title="文档大纲"
          aria-label="文档大纲"
          :aria-pressed="outlineOpen"
          @click="outlineOpen = !outlineOpen"
        >
          <ListTree :size="15" />
        </button>

        <button
          type="button"
          class="sy-icon-btn"
          title="导出 Markdown"
          aria-label="导出 Markdown"
          @click="emit('export-note', note)"
        >
          <Download :size="15" />
        </button>
        <slot name="trailing" />
      </div>
    </div>

    <!-- SiYuan-style Top Formatting Toolbar (hidden when locked readonly or in raw source mode) -->
    <NoteTopToolbar
      v-if="editorMode === 'wysiwyg' && !isReadonly"
      :editor="editor"
      :can-undo="historyState.undo"
      :can-redo="historyState.redo"
      :fullscreen="isFullscreen"
      :active-color="activeTextStyleColor"
      @insert-block="runToolbarInsertBlock"
      @open-link="openLinkInput"
      @trigger-memo="triggerInlineMemo"
      @upload-image="fileInputRef?.click()"
      @insert-reference="triggerToolbarReference"
      @toggle-fullscreen="isFullscreen = !isFullscreen"
    />

    <!-- Main workspace -->
    <div class="sy-editor-workspace">
      <!-- Floating Contextual Table toolbar inside workspace (zero vertical shift!) -->
      <div
        v-if="inTable && editorMode === 'wysiwyg' && !isReadonly"
        class="sy-table-bar"
        role="toolbar"
        aria-label="表格操作"
      >
        <button type="button" title="在上方插入行" @mousedown.prevent @click="editor?.chain().focus().addRowBefore().run()">+上行</button>
        <button type="button" title="在下方插入行" @mousedown.prevent @click="editor?.chain().focus().addRowAfter().run()">+下行</button>
        <button type="button" title="删除当前行" @mousedown.prevent @click="editor?.chain().focus().deleteRow().run()">删行</button>
        <span class="sy-bar-sep" />
        <button type="button" title="在左侧插入列" @mousedown.prevent @click="editor?.chain().focus().addColumnBefore().run()">+左列</button>
        <button type="button" title="在右侧插入列" @mousedown.prevent @click="editor?.chain().focus().addColumnAfter().run()">+右列</button>
        <button type="button" title="删除当前列" @mousedown.prevent @click="editor?.chain().focus().deleteColumn().run()">删列</button>
        <span class="sy-bar-sep" />
        <button type="button" title="切换首行表头" @mousedown.prevent @click="editor?.chain().focus().toggleHeaderRow().run()">表头</button>
        <button type="button" title="列左对齐" aria-label="列左对齐" @mousedown.prevent @click="editor?.chain().focus().setColumnAlignment('left').run()"><AlignLeft :size="13" /></button>
        <button type="button" title="列居中对齐" aria-label="列居中对齐" @mousedown.prevent @click="editor?.chain().focus().setColumnAlignment('center').run()"><AlignCenter :size="13" /></button>
        <button type="button" title="列右对齐" aria-label="列右对齐" @mousedown.prevent @click="editor?.chain().focus().setColumnAlignment('right').run()"><AlignRight :size="13" /></button>
        <span class="sy-bar-sep" />
        <button type="button" class="is-danger" title="删除整张表格" aria-label="删除表格" @mousedown.prevent @click="editor?.chain().focus().deleteTable().run()"><Trash2 :size="13" /></button>
      </div>

      <div
        v-show="editorMode === 'wysiwyg'"
        ref="scrollStageRef"
        class="sy-scroll-stage"
        @mousemove="onPaperMouseMove"
        @mouseleave="onPaperMouseLeave"
      >
        <div
          ref="paperRef"
          class="sy-paper"
          :class="{ 'is-wide': widePaper }"
          @dragover="onPaperDragOver"
          @drop="onPaperDrop"
        >
          <!-- SiYuan Protyle Left Block Gutter Handle (hover / focus / block selection) -->
          <div
            v-if="!phone && gutterTarget"
            class="sy-block-gutter"
            :class="{ 'is-selected': selectedBlockPos === gutterTarget.pos }"
            :style="{ top: `${gutterTarget.top}px`, left: `${gutterTarget.left}px` }"
            contenteditable="false"
          >
            <button
              v-if="gutterTarget.parentContainer"
              type="button"
              class="sy-block-gutter-btn is-parent"
              :class="{ 'is-selected': selectedBlockPos === gutterTarget.parentContainer.pos }"
              :title="`点击选中外层${BLOCK_KIND_LABELS[gutterTarget.parentContainer.kind] || '容器'} · 右键打开菜单`"
              :aria-label="`选中外层${BLOCK_KIND_LABELS[gutterTarget.parentContainer.kind] || '容器'}`"
              @mousedown.stop
              @click="onGutterLeftClick($event, gutterTarget.parentContainer)"
              @contextmenu="onGutterRightClick($event, gutterTarget.parentContainer)"
            >
              <NoteBlockGutterIcon :kind="gutterTarget.parentContainer.kind" />
            </button>

            <button
              type="button"
              class="sy-block-gutter-btn"
              :class="{ 'is-selected': selectedBlockPos === gutterTarget.pos }"
              :draggable="!isReadonly"
              :title="`${BLOCK_KIND_LABELS[gutterTarget.kind] || '块'}：左键整块选中（按 Delete 删除）· 右键块菜单 · 拖拽移动`"
              :aria-label="`${BLOCK_KIND_LABELS[gutterTarget.kind] || '块'}操作手柄`"
              @mousedown.stop
              @click="onGutterLeftClick($event, gutterTarget)"
              @contextmenu="onGutterRightClick($event, gutterTarget)"
              @dragstart="onGutterDragStart($event, gutterTarget)"
              @dragend="onGutterDragEnd"
            >
              <NoteBlockGutterIcon :kind="gutterTarget.kind" :level="gutterTarget.level" />
            </button>
          </div>

          <!-- Drag-and-drop block insertion indicator line -->
          <div
            v-if="blockDrag.active && blockDrag.targetPos !== null"
            class="sy-block-drop-line"
            :style="{ top: `${blockDrag.lineTop}px` }"
          />

          <!-- SiYuan Protyle Header: large icon above title when set, subtle hover-only icon action when empty -->
          <div
            class="sy-doc-header"
            :class="{ 'has-icon': hasCustomIcon, 'is-gutter-visible': titleHeaderHovered || emojiOpen }"
            @mouseenter="onTitleHeaderEnter"
            @mouseleave="onTitleHeaderLeave"
          >
            <div v-if="hasCustomIcon" class="sy-doc-icon-hero-row">
              <button
                ref="emojiBtnRef"
                type="button"
                class="sy-doc-icon-hero"
                :disabled="isReadonly"
                :title="isReadonly ? '只读模式下不可修改图标' : '点击更换或移除图标'"
                aria-label="更换笔记图标"
                @click="!isReadonly && (emojiOpen = !emojiOpen)"
              >
                {{ note.icon }}
              </button>
            </div>

            <div class="sy-doc-title-row">
              <button
                v-if="!hasCustomIcon && !isReadonly"
                ref="emojiBtnRef"
                type="button"
                class="sy-doc-gutter-icon"
                title="设置笔记图标"
                aria-label="设置笔记图标"
                @mouseenter="onTitleHeaderEnter"
                @click="emojiOpen = !emojiOpen"
              >
                <SmilePlus :size="16" />
              </button>

              <textarea
                ref="titleInputRef"
                v-model="localTitle"
                rows="1"
                class="sy-doc-title-input"
                :readonly="isReadonly"
                placeholder="无标题笔记"
                aria-label="笔记标题"
                @input="syncTitleHeight"
                @keydown="onTitleKeydown"
              />
            </div>
          </div>

          <!-- Protyle-like WYSIWYG body -->
          <div
            class="sy-protyle"
            @contextmenu="onProtyleContextMenu"
            @mouseover="onProtyleMouseOver"
            @mouseout="onProtyleMouseOut"
            @click="onProtyleClick"
          >
            <EditorContent v-if="editor" :editor="editor" />
          </div>
        </div>
      </div>

      <!-- Monaco / Adaptive Raw Markdown Source Mode -->
      <div v-if="editorMode === 'source'" class="sy-source-stage">
        <AdaptiveMdEditor
          v-model="sourceMarkdown"
          :read-only="isReadonly"
          completion-mode="memory"
        />
      </div>

      <!-- Desktop Right Outline (TOC) -->
      <aside v-if="!phone && outlineOpen" class="sy-outline-aside" aria-label="文档大纲">
        <div class="sy-outline-header">
          <span>大纲</span>
          <span>{{ outlineItems.length }}</span>
        </div>
        <div v-if="outlineItems.length" class="sy-outline-list">
          <button
            v-for="h in outlineItems"
            :key="`${h.index}:${h.text}`"
            type="button"
            class="sy-outline-item"
            :style="{ '--toc-level': h.level }"
            :title="h.text"
            @click="jumpToHeading(h.index)"
          >
            {{ h.text }}
          </button>
        </div>
        <div v-else class="sy-outline-empty">正文中输入 # 标题后自动生成大纲</div>
      </aside>
    </div>

    <!-- Bottom statusbar (SiYuan style) -->
    <footer class="sy-statusbar">
      <span>{{ isReadonly ? '只读视图' : '输入 / 唤出块菜单 · 输入 @ 引用会话或资源' }}</span>
      <div class="sy-statusbar-stats">
        <span>字符 {{ stats.chars.toLocaleString() }}</span>
        <span>字 {{ stats.words.toLocaleString() }}</span>
        <span>块 {{ stats.blocks.toLocaleString() }}</span>
      </div>
    </footer>

    <input ref="fileInputRef" type="file" accept="image/*" multiple hidden @change="onFileSelected" />

    <!-- SiYuan-style Block Context Menu (Gutter click / Right-click) -->
    <FloatingPanel
      :open="blockMenu.open"
      :anchor="blockMenu.anchor"
      placement="bottom-start"
      :width="238"
      label="块操作菜单"
      @close="blockMenu.open = false"
    >
      <div class="sy-block-menu" role="menu">
        <div class="sy-block-menu-head">
          <span>{{ BLOCK_KIND_LABELS[blockMenu.kind] || '内容块' }}</span>
          <small>已整块选中</small>
        </div>

        <template v-if="!isReadonly && CONVERTIBLE_KINDS.has(blockMenu.kind)">
          <div class="sy-block-turn-bar" role="group" aria-label="转换为块类型">
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'paragraph' }" title="转为正文段落" @click="turnSelectedBlockInto('paragraph')">
              <NoteBlockGutterIcon kind="paragraph" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'heading' && blockMenu.level === 1 }" title="转为一级标题 H1" @click="turnSelectedBlockInto('heading', { level: 1 })">
              <NoteBlockGutterIcon kind="heading" :level="1" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'heading' && blockMenu.level === 2 }" title="转为二级标题 H2" @click="turnSelectedBlockInto('heading', { level: 2 })">
              <NoteBlockGutterIcon kind="heading" :level="2" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'heading' && blockMenu.level === 3 }" title="转为三级标题 H3" @click="turnSelectedBlockInto('heading', { level: 3 })">
              <NoteBlockGutterIcon kind="heading" :level="3" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'bulletList' || blockMenu.kind === 'listItem' }" title="转为无序列表" @click="turnSelectedBlockInto('bulletList')">
              <NoteBlockGutterIcon kind="bulletList" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'orderedList' }" title="转为有序列表" @click="turnSelectedBlockInto('orderedList')">
              <NoteBlockGutterIcon kind="orderedList" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'taskList' || blockMenu.kind === 'taskItem' }" title="转为待办清单" @click="turnSelectedBlockInto('taskList')">
              <NoteBlockGutterIcon kind="taskList" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'blockquote' }" title="转为引用块" @click="turnSelectedBlockInto('blockquote')">
              <NoteBlockGutterIcon kind="blockquote" />
            </button>
            <button type="button" :class="{ 'is-current': blockMenu.kind === 'codeBlock' }" title="转为代码块" @click="turnSelectedBlockInto('codeBlock')">
              <NoteBlockGutterIcon kind="codeBlock" />
            </button>
          </div>
          <hr />
        </template>

        <button type="button" role="menuitem" @click="runBlockMenuAction('copy-md')">
          <Copy :size="13.5" />
          <span>复制 Markdown</span>
          <kbd>Ctrl+C</kbd>
        </button>
        <button type="button" role="menuitem" @click="runBlockMenuAction('copy-text')">
          <ClipboardCopy :size="13.5" />
          <span>{{ blockMenu.kind === 'codeBlock' ? '复制代码内容' : '复制纯文本' }}</span>
        </button>

        <template v-if="blockMenu.parentContainer">
          <button type="button" role="menuitem" @click="runBlockMenuAction('select-parent')">
            <Layers :size="13.5" />
            <span>选中整个{{ BLOCK_KIND_LABELS[blockMenu.parentContainer.kind] || '外层容器' }}</span>
          </button>
        </template>

        <template v-if="!isReadonly">
          <button type="button" role="menuitem" @click="runBlockMenuAction('cut')">
            <Scissors :size="13.5" />
            <span>剪切块</span>
            <kbd>Ctrl+X</kbd>
          </button>
          <button type="button" role="menuitem" @click="runBlockMenuAction('duplicate')">
            <FileCode2 :size="13.5" />
            <span>创建块副本</span>
            <kbd>Ctrl+D</kbd>
          </button>
          <hr />
          <button type="button" role="menuitem" @click="runBlockMenuAction('insert-before')">
            <ArrowUp :size="13.5" />
            <span>在上方插入空行</span>
          </button>
          <button type="button" role="menuitem" @click="runBlockMenuAction('insert-after')">
            <CornerDownLeft :size="13.5" />
            <span>在下方插入空行</span>
            <kbd>Enter</kbd>
          </button>
          <button type="button" role="menuitem" @click="runBlockMenuAction('move-up')">
            <ArrowUp :size="13.5" />
            <span>上移块</span>
            <kbd>Alt+↑</kbd>
          </button>
          <button type="button" role="menuitem" @click="runBlockMenuAction('move-down')">
            <ArrowDown :size="13.5" />
            <span>下移块</span>
            <kbd>Alt+↓</kbd>
          </button>
          <hr />
          <button type="button" class="is-danger" role="menuitem" @click="runBlockMenuAction('delete')">
            <Trash2 :size="13.5" />
            <span>删除{{ BLOCK_KIND_LABELS[blockMenu.kind] || '块' }}</span>
            <kbd>Del</kbd>
          </button>
        </template>
      </div>
    </FloatingPanel>

    <!-- Emoji Picker Floating Panel -->
    <FloatingPanel :open="emojiOpen" :anchor="emojiBtnRef" placement="bottom-start" :width="244" label="选择笔记图标" @close="emojiOpen = false">
      <div class="sy-emoji-grid">
        <button
          v-for="emo in PRESET_EMOJIS"
          :key="emo"
          type="button"
          class="sy-emoji-cell"
          @click="chooseEmoji(emo)"
        >
          {{ emo }}
        </button>
        <button v-if="note.icon" type="button" class="sy-emoji-clear" @click="chooseEmoji('')">清除自定义图标</button>
      </div>
    </FloatingPanel>

    <!-- Selection Bubble Menu -->
    <FloatingPanel :open="bubble.open" :anchor="bubble.anchor" placement="top" :width="bubble.linkEditing ? 310 : 0" label="文本格式" @close="bubble.open = false; bubble.linkEditing = false">
      <div v-if="bubble.linkEditing" class="sy-link-popover">
        <input
          v-model="bubble.linkUrl"
          type="url"
          placeholder="https://..."
          aria-label="链接地址"
          @keydown.enter.prevent="applyLink"
          @keydown.esc.prevent="bubble.linkEditing = false"
        />
        <button type="button" class="sy-icon-btn is-active" @mousedown.prevent @click="applyLink">确定</button>
      </div>
      <div v-else class="sy-bubble-toolbar" role="toolbar" aria-label="选区格式">
        <button type="button" :class="{ 'is-active': editor?.isActive('bold') }" title="粗体 (Ctrl+B)" @mousedown.prevent @click="editor?.chain().focus().toggleBold().run()"><Bold :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('italic') }" title="斜体 (Ctrl+I)" @mousedown.prevent @click="editor?.chain().focus().toggleItalic().run()"><Italic :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('underline') }" title="下划线 (Ctrl+U)" @mousedown.prevent @click="editor?.chain().focus().toggleUnderline().run()"><Underline :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('strike') }" title="删除线" @mousedown.prevent @click="editor?.chain().focus().toggleStrike().run()"><Strikethrough :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('highlight') }" title="标记高亮" @mousedown.prevent @click="editor?.chain().focus().toggleHighlight().run()"><Highlighter :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('code') }" title="行内代码 (Ctrl+E)" @mousedown.prevent @click="editor?.chain().focus().toggleCode().run()"><Code :size="14" /></button>
        <button type="button" :class="{ 'is-active': editor?.isActive('inlineMemo') }" title="备注 (Ctrl+Alt+M)" aria-label="备注" @mousedown.prevent @click="triggerInlineMemo()">
          <svg viewBox="0 0 18 18" width="14.5" height="14.5" fill="none" stroke="currentColor" stroke-width="1.35">
            <circle cx="9" cy="9" r="6.8" stroke-dasharray="2.1 1.4" />
            <path d="M6.2 11.7V6.3L9 9.6l2.8-3.3v5.4" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="none" />
          </svg>
        </button>
        <button type="button" :class="{ 'is-active': editor?.isActive('link') }" title="超链接" @mousedown.prevent @click="openLinkInput()"><LinkIcon :size="14" /></button>
      </div>
    </FloatingPanel>

    <!-- SiYuan-style Inline Memo Floating Popover (备注悬浮框：右键 / 悬停 / 点击 / Ctrl+Alt+M) -->
    <FloatingPanel
      :open="memoPopover.open"
      :anchor="memoPopover.anchor"
      placement="bottom-start"
      :width="468"
      label="备注内容"
      @close="closeMemoPopover(false)"
      @enter="cancelMemoHoverClose"
      @leave="scheduleMemoHoverClose"
    >
      <div class="sy-memo-popover">
        <div class="sy-memo-header">
          <div class="sy-memo-title">
            <span>备注</span>
            <small v-if="memoPopover.label">{{ memoPopover.label }}</small>
          </div>
          <div class="sy-memo-actions">
            <button
              type="button"
              class="sy-memo-icon-btn"
              title="复制备注内容"
              aria-label="复制备注内容"
              @click="copyMemoContent"
            >
              <Copy :size="13.5" />
            </button>
            <button
              type="button"
              class="sy-memo-icon-btn"
              :class="{ 'is-active': memoPopover.pinned }"
              :title="memoPopover.pinned ? '取消钉住' : '钉住悬浮框'"
              :aria-label="memoPopover.pinned ? '取消钉住' : '钉住悬浮框'"
              @click="memoPopover.pinned = !memoPopover.pinned; memoPopover.locked = true"
            >
              <Pin :size="13.5" />
            </button>
            <button
              v-if="!isReadonly && typeof memoPopover.from === 'number'"
              type="button"
              class="sy-memo-icon-btn is-danger"
              title="移除备注（保留原文字）"
              aria-label="移除备注"
              @click="removeCurrentMemo"
            >
              <Trash2 :size="13.5" />
            </button>
            <button
              type="button"
              class="sy-memo-icon-btn"
              title="关闭"
              aria-label="关闭备注"
              @click="closeMemoPopover(true)"
            >
              <X :size="14" />
            </button>
          </div>
        </div>
        <textarea
          ref="memoTextareaRef"
          :value="memoPopover.text"
          class="sy-memo-textarea"
          :readonly="isReadonly || typeof memoPopover.from !== 'number'"
          rows="6"
          placeholder="输入隐藏备注内容（如密钥、账号说明、补充注解…）"
          aria-label="备注正文"
          @focus="memoPopover.locked = true"
          @input="onMemoInput"
        />
      </div>
    </FloatingPanel>

    <!-- Slash Command Menu -->
    <FloatingPanel :open="slash.open && filteredSlashCommands.length > 0" :anchor="slash.anchor" placement="bottom-start" :width="268" label="插入块菜单" @close="slash.open = false">
      <div class="sy-slash-list" role="listbox" aria-label="块类型">
        <button
          v-for="(cmd, idx) in filteredSlashCommands"
          :key="cmd.id"
          type="button"
          class="sy-slash-item"
          :class="{ 'is-selected': idx === slash.selected }"
          role="option"
          :aria-selected="idx === slash.selected"
          @pointermove="slash.selected = idx"
          @mousedown.prevent
          @click="runSlashCommand(cmd)"
        >
          <span class="sy-slash-icon">{{ cmd.icon }}</span>
          <span class="sy-slash-copy">
            <strong>{{ cmd.label }}</strong>
            <small>{{ cmd.desc }}</small>
          </span>
          <span v-if="cmd.shortcut" class="sy-slash-shortcut">{{ cmd.shortcut }}</span>
        </button>
      </div>
    </FloatingPanel>

    <!-- @ Reference Picker -->
    <ReferencePicker
      ref="refPickerRef"
      :open="Boolean(mention)"
      :anchor="mention?.anchor"
      :kind="mention?.kind || ''"
      :query="mention?.query || ''"
      :selected-references="currentReferences"
      @select="insertReferences($event)"
      @close="mention = null"
    />

    <!-- Image Lightbox -->
    <ImageLightbox
      v-if="lightboxOpen"
      :url-list="lightboxUrls"
      :initial-index="lightboxIndex"
      @close="lightboxOpen = false"
    />
  </div>
</template>
