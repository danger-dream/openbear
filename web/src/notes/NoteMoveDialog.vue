<script setup>
import { computed, ref, watch } from 'vue';
import { Search } from '@element-plus/icons-vue';
import NoteIcon from './NoteIcon.vue';

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  movingNote: { type: Object, default: null },
  treeItems: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits(['update:modelValue', 'confirm']);

const query = ref('');
const targetParentUuid = ref('');

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
});

watch(
  () => [props.modelValue, props.movingNote?.noteUuid],
  ([open]) => {
    if (open) {
      query.value = '';
      targetParentUuid.value = props.movingNote?.parentUuid || '';
    }
  },
);

const blockedUuids = computed(() => {
  const blocked = new Set();
  const selfUuid = props.movingNote?.noteUuid;
  if (!selfUuid) return blocked;
  blocked.add(selfUuid);
  const childrenMap = new Map();
  for (const item of props.treeItems) {
    const p = item.parentUuid || '';
    if (!childrenMap.has(p)) childrenMap.set(p, []);
    childrenMap.get(p).push(item.noteUuid);
  }
  const stack = [selfUuid];
  while (stack.length) {
    const cur = stack.pop();
    for (const sub of childrenMap.get(cur) || []) {
      if (!blocked.has(sub)) {
        blocked.add(sub);
        stack.push(sub);
      }
    }
  }
  return blocked;
});

const candidates = computed(() => {
  const q = query.value.trim().toLowerCase();
  return props.treeItems.filter((item) => {
    if (!q) return true;
    return `${item.title} ${item.path}`.toLowerCase().includes(q);
  });
});
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="movingNote ? `移动「${movingNote.title}」到…` : '移动笔记'"
    width="min(520px, calc(100vw - 24px))"
    append-to-body
    class="mobile-viewport-dialog"
  >
    <el-input v-model="query" clearable placeholder="搜索目标上级笔记路径…" :prefix-icon="Search" />
    <div class="kb-move-list" role="listbox" aria-label="目标位置">
      <button
        type="button"
        class="kb-move-option"
        :class="{ 'is-selected': targetParentUuid === '' }"
        @click="targetParentUuid = ''"
      >
        <span>🏠</span>
        <span>根级顶层（无上级）</span>
      </button>
      <button
        v-for="item in candidates"
        :key="item.noteUuid"
        type="button"
        class="kb-move-option"
        :class="{ 'is-selected': targetParentUuid === item.noteUuid }"
        :disabled="blockedUuids.has(item.noteUuid)"
        @click="targetParentUuid = item.noteUuid"
      >
        <NoteIcon :icon="item.icon" :has-children="Boolean(item.childCount)" :size="15" />
        <span class="kb-move-path">{{ item.path }}</span>
      </button>
    </div>
    <template #footer>
      <el-button :disabled="busy" @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="emit('confirm', targetParentUuid)">移动</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-move-list {
  margin-top: 10px;
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--ob-border, rgba(127, 127, 127, 0.22));
  border-radius: 8px;
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kb-move-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text, #1f2328);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.kb-move-option:hover:not(:disabled) {
  background: var(--ob-hover, rgba(127, 127, 127, 0.12));
}
.kb-move-option.is-selected {
  background: var(--ob-blue-soft, rgba(37, 99, 235, 0.13));
  color: var(--ob-blue, #2563eb);
  font-weight: 500;
}
.kb-move-option:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.kb-move-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
