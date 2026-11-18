<script setup>
import { ref } from 'vue';
import {
  AtSign,
  Bold,
  Code,
  Highlighter,
  Image as ImageIcon,
  IndentDecrease,
  IndentIncrease,
  Italic,
  Keyboard,
  Link as LinkIcon,
  Maximize,
  Minimize,
  Plus,
  Redo2,
  RemoveFormatting,
  Strikethrough,
  Table2,
  Underline,
  Undo2,
} from '@lucide/vue';
import FloatingPanel from '../references/FloatingPanel.vue';

const TEXT_COLORS = [
  { label: '默认字色', value: '' },
  { label: '红色', value: '#e03131' },
  { label: '橙色', value: '#e8590c' },
  { label: '琥珀', value: '#d97706' },
  { label: '绿色', value: '#2f9e44' },
  { label: '青蓝', value: '#0ca678' },
  { label: '蓝色', value: '#1971c2' },
  { label: '紫色', value: '#7048e8' },
  { label: '灰色', value: '#868e96' },
];

const BG_COLORS = [
  { label: '无背景', value: '' },
  { label: '浅红背景', value: 'rgba(224, 49, 49, 0.16)' },
  { label: '浅橙背景', value: 'rgba(232, 89, 12, 0.16)' },
  { label: '浅黄背景', value: 'rgba(245, 158, 11, 0.22)' },
  { label: '浅绿背景', value: 'rgba(47, 158, 68, 0.16)' },
  { label: '浅青背景', value: 'rgba(12, 166, 120, 0.16)' },
  { label: '浅蓝背景', value: 'rgba(25, 113, 194, 0.16)' },
  { label: '浅紫背景', value: 'rgba(112, 72, 232, 0.16)' },
  { label: '浅灰背景', value: 'rgba(134, 142, 150, 0.2)' },
];

const INSERT_BLOCKS = [
  { id: 'h1', icon: 'H1', label: '一级标题', shortcut: '#' },
  { id: 'h2', icon: 'H2', label: '二级标题', shortcut: '##' },
  { id: 'h3', icon: 'H3', label: '三级标题', shortcut: '###' },
  { id: 'todo', icon: '☑', label: '待办清单', shortcut: '[]' },
  { id: 'bullet', icon: '•', label: '无序列表', shortcut: '-' },
  { id: 'ordered', icon: '1.', label: '有序列表', shortcut: '1.' },
  { id: 'quote', icon: '❝', label: '引用块', shortcut: '>' },
  { id: 'code', icon: '</>', label: '代码块', shortcut: '```' },
  { id: 'table', icon: '⊞', label: '表格 (3×3)', shortcut: '|a|b|' },
  { id: 'hr', icon: '—', label: '分割线', shortcut: '---' },
];

const props = defineProps({
  editor: { type: Object, default: null },
  canUndo: { type: Boolean, default: false },
  canRedo: { type: Boolean, default: false },
  fullscreen: { type: Boolean, default: false },
  activeColor: { type: String, default: '#e03131' },
});

const emit = defineEmits([
  'insert-block',
  'open-link',
  'trigger-memo',
  'upload-image',
  'insert-reference',
  'toggle-fullscreen',
]);

const insertBtnRef = ref(null);
const insertOpen = ref(false);
const colorBtnRef = ref(null);
const colorOpen = ref(false);

function pickInsert(id) {
  insertOpen.value = false;
  emit('insert-block', id);
}

function applyTextColor(color) {
  props.editor?.chain().focus().setTextStyleAppearance({ color }).run();
  colorOpen.value = false;
}

function applyBgColor(bg) {
  props.editor?.chain().focus().setTextStyleAppearance({ bg }).run();
  colorOpen.value = false;
}
</script>

<template>
  <div class="sy-format-toolbar" role="toolbar" aria-label="编辑格式工具栏">
    <div class="sy-format-scroll">
      <!-- 1. Undo / Redo -->
      <button
        type="button"
        class="sy-fmt-btn"
        :disabled="!canUndo"
        title="撤销 Ctrl+Z"
        aria-label="撤销"
        @mousedown.prevent
        @click="editor?.chain().focus().undoHistory().run()"
      >
        <Undo2 :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :disabled="!canRedo"
        title="重做 Ctrl+Y"
        aria-label="重做"
        @mousedown.prevent
        @click="editor?.chain().focus().redoHistory().run()"
      >
        <Redo2 :size="14.5" />
      </button>

      <span class="sy-fmt-sep" />

      <!-- 2. Insert Block & Link -->
      <button
        ref="insertBtnRef"
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': insertOpen }"
        title="插入块 (/)"
        aria-label="插入块"
        @mousedown.prevent
        @click="insertOpen = !insertOpen"
      >
        <Plus :size="15" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('link') }"
        title="链接 Ctrl+K"
        aria-label="插入或编辑链接"
        @mousedown.prevent
        @click="emit('open-link', $event.currentTarget)"
      >
        <LinkIcon :size="14.5" />
      </button>

      <span class="sy-fmt-sep" />

      <!-- 3. Appearance & Text Marks -->
      <button
        ref="colorBtnRef"
        type="button"
        class="sy-fmt-btn sy-fmt-color-btn"
        :class="{ 'is-active': colorOpen || editor?.isActive('textStyle') }"
        title="外观（字体颜色与背景色）"
        aria-label="字体颜色与背景色"
        @mousedown.prevent
        @click="colorOpen = !colorOpen"
      >
        <span class="sy-fmt-letter-a">A</span>
        <span class="sy-fmt-color-bar" :style="{ backgroundColor: activeColor || '#e03131' }" />
      </button>

      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('bold') }"
        title="粗体 Ctrl+B"
        aria-label="粗体"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleBold().run()"
      >
        <Bold :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('italic') }"
        title="斜体 Ctrl+I"
        aria-label="斜体"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleItalic().run()"
      >
        <Italic :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('underline') }"
        title="下划线 Ctrl+U"
        aria-label="下划线"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleUnderline().run()"
      >
        <Underline :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('strike') }"
        title="删除线"
        aria-label="删除线"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleStrike().run()"
      >
        <Strikethrough :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('highlight') }"
        title="标记高亮 Ctrl+Shift+H"
        aria-label="标记高亮"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleHighlight().run()"
      >
        <Highlighter :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn sy-fmt-script-btn"
        :class="{ 'is-active': editor?.isActive('superscript') }"
        title="上标"
        aria-label="上标"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleSuperscript().run()"
      >
        <span>X<sup>2</sup></span>
      </button>
      <button
        type="button"
        class="sy-fmt-btn sy-fmt-script-btn"
        :class="{ 'is-active': editor?.isActive('subscript') }"
        title="下标"
        aria-label="下标"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleSubscript().run()"
      >
        <span>X<sub>2</sub></span>
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        title="清除行内格式 Ctrl+\"
        aria-label="清除行内格式"
        @mousedown.prevent
        @click="editor?.chain().focus().clearInlineMarks().run()"
      >
        <RemoveFormatting :size="14.5" />
      </button>

      <span class="sy-fmt-sep" />

      <!-- 4. Code, Kbd, Reference, Inline Memo (M) -->
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('code') }"
        title="行内代码 Ctrl+E"
        aria-label="行内代码"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleCode().run()"
      >
        <Code :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': editor?.isActive('kbd') }"
        title="键盘按键 Ctrl+Shift+K"
        aria-label="键盘按键"
        @mousedown.prevent
        @click="editor?.chain().focus().toggleKbd().run()"
      >
        <Keyboard :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        title="引用资源 (@)"
        aria-label="插入引用资源"
        @mousedown.prevent
        @click="emit('insert-reference')"
      >
        <AtSign :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn sy-fmt-memo-btn"
        :class="{ 'is-active': editor?.isActive('inlineMemo') }"
        title="备注 Ctrl+Alt+M"
        aria-label="备注"
        @mousedown.prevent
        @click="emit('trigger-memo', $event.currentTarget)"
      >
        <svg viewBox="0 0 18 18" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.35">
          <circle cx="9" cy="9" r="6.8" stroke-dasharray="2.1 1.4" />
          <path d="M6.2 11.7V6.3L9 9.6l2.8-3.3v5.4" stroke-width="1.45" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="none" />
        </svg>
      </button>

      <span class="sy-fmt-sep" />

      <!-- 5. List Outdent/Indent, Table, Image, Fullscreen -->
      <button
        type="button"
        class="sy-fmt-btn"
        title="减少缩进 Shift+Tab"
        aria-label="减少缩进"
        @mousedown.prevent
        @click="editor?.chain().focus().outdentCurrent().run()"
      >
        <IndentDecrease :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        title="增加缩进 Tab"
        aria-label="增加缩进"
        @mousedown.prevent
        @click="editor?.chain().focus().indentCurrent().run()"
      >
        <IndentIncrease :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        title="插入表格 (3×3)"
        aria-label="插入表格"
        @mousedown.prevent
        @click="emit('insert-block', 'table')"
      >
        <Table2 :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        title="上传图片"
        aria-label="上传图片"
        @mousedown.prevent
        @click="emit('upload-image')"
      >
        <ImageIcon :size="14.5" />
      </button>
      <button
        type="button"
        class="sy-fmt-btn"
        :class="{ 'is-active': fullscreen }"
        :title="fullscreen ? '退出全屏编辑 (Esc)' : '全屏专注编辑'"
        :aria-label="fullscreen ? '退出全屏编辑' : '全屏专注编辑'"
        @mousedown.prevent
        @click="emit('toggle-fullscreen')"
      >
        <Minimize v-if="fullscreen" :size="14.5" />
        <Maximize v-else :size="14.5" />
      </button>
    </div>

    <!-- Insert Block Popover -->
    <FloatingPanel
      :open="insertOpen"
      :anchor="insertBtnRef"
      placement="bottom-start"
      :width="220"
      label="插入内容块"
      @close="insertOpen = false"
    >
      <div class="sy-slash-list">
        <button
          v-for="item in INSERT_BLOCKS"
          :key="item.id"
          type="button"
          class="sy-slash-item"
          @mousedown.prevent
          @click="pickInsert(item.id)"
        >
          <span class="sy-slash-icon">{{ item.icon }}</span>
          <span class="sy-slash-copy"><strong>{{ item.label }}</strong></span>
          <span class="sy-slash-shortcut">{{ item.shortcut }}</span>
        </button>
      </div>
    </FloatingPanel>

    <!-- Appearance Color Popover -->
    <FloatingPanel
      :open="colorOpen"
      :anchor="colorBtnRef"
      placement="bottom-start"
      :width="244"
      label="外观颜色"
      @close="colorOpen = false"
    >
      <div class="sy-color-palette">
        <div class="sy-color-section-title">字体颜色</div>
        <div class="sy-color-swatches">
          <button
            v-for="c in TEXT_COLORS"
            :key="`fg-${c.label}`"
            type="button"
            class="sy-color-swatch"
            :title="c.label"
            :aria-label="c.label"
            @mousedown.prevent
            @click="applyTextColor(c.value)"
          >
            <span class="sy-swatch-a" :style="{ color: c.value || 'var(--ob-fg)' }">A</span>
          </button>
        </div>

        <div class="sy-color-section-title">背景颜色</div>
        <div class="sy-color-swatches">
          <button
            v-for="b in BG_COLORS"
            :key="`bg-${b.label}`"
            type="button"
            class="sy-color-swatch"
            :title="b.label"
            :aria-label="b.label"
            :style="{ backgroundColor: b.value || 'transparent' }"
            @mousedown.prevent
            @click="applyBgColor(b.value)"
          >
            <span class="sy-swatch-a">A</span>
          </button>
        </div>
      </div>
    </FloatingPanel>
  </div>
</template>
