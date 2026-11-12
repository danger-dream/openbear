<script setup>
import { ElMessageBox } from 'element-plus';
import { Close } from '@element-plus/icons-vue';
import MeshSheet from '../servers/mesh/MeshSheet.vue';
import '../servers/mesh/mesh.css';

// One surface, three looks: on a phone the servers page's bottom sheet (drag the header down to close),
// on a desktop a right-hand drawer for details or a centered dialog for forms.
// form: a drawer holding edits, which a click beside it does not close; unless dirty is given, which says
// whether there are edits yet: then any way of closing it does, asking first while there are.
const props = defineProps({modelValue: Boolean, title: {type: String, default: ''}, sub: {type: String, default: ''}, phone: Boolean,
  kind: {type: String, default: 'dialog'}, width: {type: String, default: '640px'}, size: {type: String, default: '440px'}, busy: Boolean, form: Boolean,
  dirty: {type: Boolean, default: null}});
const emit = defineEmits(['update:modelValue', 'closed']);
const guarded = () => props.dirty !== null;
async function leave() {
  if (props.busy) return false;
  if (!props.dirty) return true;
  try {
    await ElMessageBox.confirm('有修改还没保存，关闭后这些修改会丢失。', '放弃修改？', {confirmButtonText: '放弃修改', cancelButtonText: '继续编辑', type: 'warning'});
    return true;
  } catch { return false; }
}
async function hide() {
  if (guarded() ? await leave() : !props.busy) emit('update:modelValue', false);
}
async function guard(done) {
  if (guarded() ? await leave() : !props.busy) done();
}
</script>
<template>
  <MeshSheet v-if="phone" :model-value="modelValue" :title="title" panel-class="dd-sheet" :leave="leave" @update:model-value="v => emit('update:modelValue', v)" @closed="emit('closed')">
    <div class="dd-ui dd-surface">
      <p v-if="sub" class="dd-sub">{{ sub }}</p>
      <slot />
    </div>
  </MeshSheet>
  <el-drawer v-else-if="kind === 'drawer'" :model-value="modelValue" direction="rtl" :size="size" :title="title" :with-header="false" append-to-body destroy-on-close :before-close="guard"
    :close-on-click-modal="!form || dirty !== null"
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
