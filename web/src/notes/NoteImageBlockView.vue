<script setup>
import { computed } from 'vue';
import { NodeViewWrapper } from '@tiptap/vue-3';
import { Maximize2, Trash2 } from '@lucide/vue';

const props = defineProps({
  node: { type: Object, required: true },
  updateAttributes: { type: Function, required: true },
  deleteNode: { type: Function, required: true },
  selected: { type: Boolean, default: false },
  editor: { type: Object, required: true },
});

const src = computed(() => String(props.node.attrs?.src || ''));
const alt = computed({
  get: () => String(props.node.attrs?.alt || ''),
  set: (val) => props.updateAttributes({ alt: val }),
});
const isEditable = computed(() => Boolean(props.editor?.isEditable));

function openPreview() {
  if (!src.value) return;
  props.editor?.storage?.imageBlock?.onPreview?.(src.value);
}
</script>

<template>
  <NodeViewWrapper class="sy-image-block" :class="{ 'is-selected': selected }" contenteditable="false">
    <figure class="sy-image-figure">
      <div class="sy-image-frame">
        <img :src="src" :alt="alt" loading="lazy" @click="openPreview" />
        <div class="sy-image-actions">
          <button type="button" title="放大预览" aria-label="放大预览图片" @mousedown.prevent @click.stop="openPreview">
            <Maximize2 :size="14" />
          </button>
          <button v-if="isEditable" type="button" class="is-danger" title="删除图片" aria-label="删除图片" @mousedown.prevent @click.stop="deleteNode()">
            <Trash2 :size="14" />
          </button>
        </div>
      </div>
      <input
        v-if="isEditable && (alt || selected)"
        v-model="alt"
        type="text"
        class="sy-image-caption-input"
        placeholder="添加图片说明…"
        aria-label="图片说明"
        @keydown.stop
      />
      <figcaption v-else-if="alt" class="sy-image-caption">{{ alt }}</figcaption>
    </figure>
  </NodeViewWrapper>
</template>
