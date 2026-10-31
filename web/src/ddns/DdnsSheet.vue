<script setup>
import { Close } from '@element-plus/icons-vue';
import MeshSheet from '../servers/mesh/MeshSheet.vue';
import '../servers/mesh/mesh.css';

// One surface, three looks: on a phone the servers page's bottom sheet (drag the header down to close),
// on a desktop a right-hand drawer for details or a centered dialog for forms.
const props = defineProps({modelValue: Boolean, title: {type: String, default: ''}, sub: {type: String, default: ''}, phone: Boolean,
  kind: {type: String, default: 'dialog'}, width: {type: String, default: '640px'}, busy: Boolean});
const emit = defineEmits(['update:modelValue', 'closed']);
function hide() { emit('update:modelValue', false); }
function guard(done) { if (!props.busy) done(); }
</script>
<template>
  <MeshSheet v-if="phone" :model-value="modelValue" :title="title" panel-class="dd-sheet" @update:model-value="v => emit('update:modelValue', v)" @closed="emit('closed')">
    <div class="dd-ui dd-surface">
      <p v-if="sub" class="dd-sub">{{ sub }}</p>
      <slot />
    </div>
  </MeshSheet>
  <el-drawer v-else-if="kind === 'drawer'" :model-value="modelValue" direction="rtl" size="440px" :with-header="false" append-to-body destroy-on-close
    class="instance-ui servers-ui dd-ui dd-drawer" @update:model-value="v => emit('update:modelValue', v)" @closed="emit('closed')">
    <header class="dd-drawer-head">
      <div class="dd-drawer-title"><slot name="title"><h2>{{ title }}</h2></slot><p v-if="sub" class="dd-sub">{{ sub }}</p></div>
      <button type="button" class="ix-icon-button dd-close" aria-label="关闭" @click="hide"><el-icon><Close /></el-icon></button>
    </header>
    <div class="dd-drawer-body dd-surface"><slot /></div>
  </el-drawer>
  <el-dialog v-else :model-value="modelValue" :title="title" :width="width" append-to-body destroy-on-close :close-on-click-modal="false" :before-close="guard"
    class="admin-dialog instance-dialog instance-ui servers-ui dd-ui dd-dialog mobile-viewport-dialog" @update:model-value="v => emit('update:modelValue', v)" @closed="emit('closed')">
    <div class="dd-surface">
      <p v-if="sub" class="dd-sub">{{ sub }}</p>
      <slot />
    </div>
  </el-dialog>
</template>
