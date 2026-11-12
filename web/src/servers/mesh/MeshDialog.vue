<script setup>
import MeshSheet from './MeshSheet.vue';

// An editor over the page: a dialog on desktops, the bottom sheet on phones (its footer at the end of the sheet).
defineProps({modelValue: Boolean, title: {type: String, default: ''}, phone: Boolean, width: {type: String, default: '560px'}});
const emit = defineEmits(['update:modelValue', 'closed']);
</script>
<template>
  <MeshSheet v-if="phone" :model-value="modelValue" :title="title" phone-size="88%" @update:model-value="emit('update:modelValue', $event)" @closed="emit('closed')">
    <div class="m3-tokens m3-dialog-body"><slot /></div>
    <div v-if="$slots.footer" class="m3-tokens m3-sheet-foot"><slot name="footer" /></div>
  </MeshSheet>
  <el-dialog v-else :model-value="modelValue" :title="title" :width="width" append-to-body destroy-on-close class="instance-ui servers-ui mesh3-dialog"
    @update:model-value="emit('update:modelValue', $event)" @closed="emit('closed')">
    <div class="m3-tokens m3-dialog-body"><slot /></div>
    <template v-if="$slots.footer" #footer><slot name="footer" /></template>
  </el-dialog>
</template>
