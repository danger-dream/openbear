<script setup>
import { computed } from 'vue';

const props = defineProps({
  icon: { type: String, default: '' },
  hasChildren: { type: Boolean, default: false },
  isRoot: { type: Boolean, default: false },
  expanded: { type: Boolean, default: false },
  size: { type: Number, default: 15 },
});

// Treat SiYuan's generic default folder/page unicode fallbacks smoothly while keeping real custom emojis
const customEmoji = computed(() => {
  const raw = String(props.icon || '').trim();
  if (!raw || raw === '📄' || raw === '📓') return '';
  return raw;
});

const kind = computed(() => {
  if (customEmoji.value === '📁' || customEmoji.value === '📂') return 'folder';
  if (customEmoji.value) return 'emoji';
  if (props.isRoot && props.hasChildren) return 'notebook';
  if (props.hasChildren) return 'doc-parent';
  return 'doc';
});
</script>

<template>
  <span
    class="sy-note-icon"
    :class="`is-${kind}`"
    :style="{ '--sy-icon-size': `${size}px` }"
    aria-hidden="true"
  >
    <span v-if="kind === 'emoji'" class="sy-note-emoji">{{ customEmoji }}</span>

    <!-- Folder (SiYuan warm subtle folder) -->
    <svg
      v-else-if="kind === 'folder'"
      :width="size"
      :height="size"
      viewBox="0 0 16 16"
      fill="none"
    >
      <path
        v-if="expanded"
        d="M1.75 4.25C1.75 3.42 2.42 2.75 3.25 2.75H6.1L7.45 4.25H12.75C13.58 4.25 14.25 4.92 14.25 5.75V6.5H3.1L1.75 12.25V4.25Z"
        fill="currentColor"
        fill-opacity="0.22"
        stroke="currentColor"
        stroke-width="1.25"
        stroke-linejoin="round"
      />
      <path
        v-if="expanded"
        d="M1.75 12.5L3.15 6.5H14.5L13.1 12.5C12.96 13.08 12.45 13.5 11.85 13.5H2.95C2.17 13.5 1.58 12.78 1.75 12.02V12.5Z"
        fill="currentColor"
        fill-opacity="0.35"
        stroke="currentColor"
        stroke-width="1.25"
        stroke-linejoin="round"
      />
      <path
        v-else
        d="M1.75 4.25C1.75 3.42 2.42 2.75 3.25 2.75H6.15L7.5 4.25H12.75C13.58 4.25 14.25 4.92 14.25 5.75V11.75C14.25 12.58 13.58 13.25 12.75 13.25H3.25C2.42 13.25 1.75 12.58 1.75 11.75V4.25Z"
        fill="currentColor"
        fill-opacity="0.28"
        stroke="currentColor"
        stroke-width="1.25"
        stroke-linejoin="round"
      />
    </svg>

    <!-- Top-level Notebook -->
    <svg
      v-else-if="kind === 'notebook'"
      :width="size"
      :height="size"
      viewBox="0 0 16 16"
      fill="none"
    >
      <rect x="3" y="2" width="10.25" height="12" rx="1.8" fill="currentColor" fill-opacity="0.1" stroke="currentColor" stroke-width="1.25" />
      <path d="M5.5 2V14M1.85 5H3M1.85 8H3M1.85 11H3" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" />
      <path d="M7.75 5.75H10.75M7.75 8.25H10.25" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" />
    </svg>

    <!-- Composite Document (has children) -->
    <svg
      v-else-if="kind === 'doc-parent'"
      :width="size"
      :height="size"
      viewBox="0 0 16 16"
      fill="none"
    >
      <path
        d="M3.25 2.25H9.6L12.75 5.4V13.25C12.75 13.8 12.3 14.25 11.75 14.25H3.25C2.7 14.25 2.25 13.8 2.25 13.25V3.25C2.25 2.7 2.7 2.25 3.25 2.25Z"
        fill="currentColor"
        fill-opacity="0.09"
        stroke="currentColor"
        stroke-width="1.2"
        stroke-linejoin="round"
      />
      <path d="M9.5 2.35V5.5H12.65" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M4.75 8H10.25M4.75 10.5H8.75" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" />
    </svg>

    <!-- SiYuan-style Clean Lined Document (#iconFile) -->
    <svg
      v-else
      :width="size"
      :height="size"
      viewBox="0 0 16 16"
      fill="none"
    >
      <path
        d="M3.5 2.25H9.65L12.75 5.35V13.25C12.75 13.8 12.3 14.25 11.75 14.25H3.5C2.95 14.25 2.5 13.8 2.5 13.25V3.25C2.5 2.7 2.95 2.25 3.5 2.25Z"
        stroke="currentColor"
        stroke-width="1.2"
        stroke-linejoin="round"
      />
      <path d="M9.5 2.35V5.5H12.65" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M5 8.1H10.3M5 10.6H8.8" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-opacity="0.78" />
    </svg>
  </span>
</template>

<style scoped>
.sy-note-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: calc(var(--sy-icon-size, 15px) + 3px);
  height: calc(var(--sy-icon-size, 15px) + 3px);
  flex: none;
  line-height: 1;
  user-select: none;
}

.sy-note-icon svg {
  display: block;
  flex: none;
}

.sy-note-emoji {
  font-size: calc(var(--sy-icon-size, 15px) - 0.5px);
  line-height: 1;
  font-family: "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif;
}

.sy-note-icon.is-doc {
  color: rgb(var(--ob-info-rgb, 100 116 139) / 0.85);
}

.sy-note-icon.is-doc-parent,
.sy-note-icon.is-notebook {
  color: rgb(var(--ob-blue-rgb, 37 99 235) / 0.78);
}

.sy-note-icon.is-folder {
  color: #eab308;
}
</style>
