<script setup>
import { computed } from 'vue';
import { NodeViewContent, NodeViewWrapper } from '@tiptap/vue-3';

const props = defineProps({
  node: { type: Object, required: true },
  updateAttributes: { type: Function, required: true },
  editor: { type: Object, required: true },
});

const checked = computed(() => Boolean(props.node.attrs?.checked));
const isEditable = computed(() => Boolean(props.editor?.isEditable));

function onToggle(event) {
  if (!isEditable.value) {
    event.preventDefault();
    return;
  }
  props.updateAttributes({ checked: Boolean(event.target.checked) });
}
</script>

<template>
  <NodeViewWrapper as="li" class="sy-task-item" :data-checked="checked ? 'true' : 'false'">
    <label class="sy-task-checkbox" contenteditable="false">
      <input
        type="checkbox"
        :checked="checked"
        :disabled="!isEditable"
        aria-label="完成状态"
        @change="onToggle"
      />
    </label>
    <NodeViewContent class="sy-task-content" />
  </NodeViewWrapper>
</template>
