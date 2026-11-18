<script setup>
defineProps({
  kind: { type: String, default: 'paragraph' },
  level: { type: Number, default: 1 },
});
</script>

<template>
  <span class="sy-gutter-svg" aria-hidden="true">
    <!-- Heading H1..H6 -->
    <span v-if="kind === 'heading'" class="sy-gutter-h">H{{ level || 1 }}</span>

    <!-- CodeBlock: SiYuan exact <...> icon -->
    <svg v-else-if="kind === 'codeBlock'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <path d="M5.2 4.5L1.9 8L5.2 11.5" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M10.8 4.5L14.1 8L10.8 11.5" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="6.2" cy="8" r="0.75" fill="currentColor" />
      <circle cx="8" cy="8" r="0.75" fill="currentColor" />
      <circle cx="9.8" cy="8" r="0.75" fill="currentColor" />
    </svg>

    <!-- BulletList / ListItem: SiYuan exact bullet + lines icon -->
    <svg v-else-if="kind === 'bulletList' || kind === 'listItem'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <circle cx="2.8" cy="4.2" r="1" fill="currentColor" />
      <circle cx="2.8" cy="8" r="1" fill="currentColor" />
      <circle cx="2.8" cy="11.8" r="1" fill="currentColor" />
      <path d="M5.6 4.2H13.4M5.6 8H13.4M5.6 11.8H13.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
    </svg>

    <!-- OrderedList -->
    <svg v-else-if="kind === 'orderedList'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <path d="M2.2 3.4H3.2V6.2M2 6.2H3.6M6 4.6H13.5M6 8.2H13.5M6 11.8H13.5" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round" />
      <path d="M2 9.8C2.2 9.3 3.4 9.3 3.6 9.9C3.8 10.5 2.1 11.5 2 12.4H3.8" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" />
    </svg>

    <!-- TaskList / TaskItem -->
    <svg v-else-if="kind === 'taskList' || kind === 'taskItem'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <rect x="2.2" y="2.8" width="4.4" height="4.4" rx="1" stroke="currentColor" stroke-width="1.2" />
      <path d="M3.3 5L4.2 5.9L5.7 4.1" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round" />
      <rect x="2.2" y="9" width="4.4" height="4.4" rx="1" stroke="currentColor" stroke-width="1.2" />
      <path d="M8.5 5H13.6M8.5 11.2H13.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
    </svg>

    <!-- Table -->
    <svg v-else-if="kind === 'table'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2.5" width="12" height="11" rx="1.6" stroke="currentColor" stroke-width="1.25" />
      <path d="M2 6.2H14M2 9.8H14M6.2 2.5V13.5M10.2 2.5V13.5" stroke="currentColor" stroke-width="1.15" />
    </svg>

    <!-- Blockquote -->
    <svg v-else-if="kind === 'blockquote'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <path d="M6.4 4.5H4.2C3.4 4.5 2.8 5.1 2.8 5.9V8.1C2.8 8.9 3.4 9.5 4.2 9.5H5.6L4.6 12H6.1L7.2 9.3V5.3C7.2 4.8 6.8 4.5 6.4 4.5Z" fill="currentColor" fill-opacity="0.85" />
      <path d="M12.4 4.5H10.2C9.4 4.5 8.8 5.1 8.8 5.9V8.1C8.8 8.9 9.4 9.5 10.2 9.5H11.6L10.6 12H12.1L13.2 9.3V5.3C13.2 4.8 12.8 4.5 12.4 4.5Z" fill="currentColor" fill-opacity="0.85" />
    </svg>

    <!-- Image -->
    <svg v-else-if="kind === 'imageBlock'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2.8" width="12" height="10.4" rx="1.6" stroke="currentColor" stroke-width="1.25" />
      <circle cx="5.4" cy="6" r="1.1" fill="currentColor" />
      <path d="M2.6 12.2L6.2 8.8L8.9 11.2L10.9 9.5L13.4 12.2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
    </svg>

    <!-- Horizontal Rule -->
    <svg v-else-if="kind === 'horizontalRule'" width="15" height="15" viewBox="0 0 16 16" fill="none">
      <path d="M2 8H14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
    </svg>

    <!-- Paragraph (SiYuan clean text lines icon) -->
    <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path d="M2.5 4.2H13.5M2.5 8H13.5M2.5 11.8H9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
    </svg>
  </span>
</template>

<style scoped>
.sy-gutter-svg {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}

.sy-gutter-h {
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: -0.03em;
}
</style>
