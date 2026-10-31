<script setup>
import { computed, ref, watch } from 'vue';
import { Close } from '@element-plus/icons-vue';

// The phone's bottom sheet, as on the servers page: it follows a downward drag
// on its header and slides out from where it was let go while the overlay fades with it.
const props = defineProps({modelValue: Boolean, title: {type: String, default: ''}, phoneSize: {type: String, default: 'auto'}, panelClass: {type: String, default: ''}});
const emit = defineEmits(['update:modelValue', 'closed']);
const closing = ref(false), offset = ref(0), dragging = ref(false);
let gesture = null;
const style = computed(() => offset.value ? {'--mesh-drag-y': `${offset.value}px`, transform: closing.value ? undefined : `translateY(${offset.value}px)`} : undefined);
watch(() => props.modelValue, open => { if (open) { closing.value = false; offset.value = 0; } });

function close() { gesture = null; dragging.value = false; closing.value = true; }
function closed() {
  if (closing.value) emit('update:modelValue', false);
  closing.value = false; offset.value = 0;
  emit('closed');
}
function start(event) {
  if (closing.value || event.button > 0 || event.target.closest('button, input, select, textarea, a')) return;
  gesture = {id: event.pointerId, x: event.clientX, y: event.clientY, lastY: event.clientY, lastT: event.timeStamp, speed: 0};
  event.currentTarget.setPointerCapture?.(event.pointerId);
}
function move(event) {
  if (!gesture || event.pointerId !== gesture.id) return;
  const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
  if (!dragging.value) {
    if (Math.hypot(dx, dy) < 4) return;
    if (dy < 0 || Math.abs(dx) > Math.abs(dy)) { cancel(); return; }
    dragging.value = true;
  }
  const dt = Math.max(1, event.timeStamp - gesture.lastT);
  gesture.speed = (event.clientY - gesture.lastY) / dt;
  gesture.lastY = event.clientY; gesture.lastT = event.timeStamp;
  offset.value = Math.max(0, dy);
}
function cancel() { gesture = null; dragging.value = false; offset.value = 0; }
function end() {
  if (!gesture) return;
  if (offset.value >= 88 || (offset.value > 24 && gesture.speed > 0.5)) close(); else cancel();
}
</script>
<template>
  <el-drawer :model-value="modelValue && !closing" direction="btt" :size="phoneSize" :title="title" :with-header="false" append-to-body destroy-on-close
    :class="['instance-ui', 'servers-ui', 'mesh-sheet', 'mobile-asset-sheet', 'mobile-viewport-sheet', panelClass, {'is-dragging': dragging}]" modal-class="mesh-sheet-overlay" :style="style"
    @update:model-value="v => { if (!v) close(); }" @closed="closed">
    <header class="mesh-sheet-head" @pointerdown="start" @pointermove="move" @pointerup="end" @pointercancel="cancel">
      <i class="mobile-asset-grip" aria-hidden="true"></i>
      <h2>{{ title }}</h2>
      <button type="button" class="mesh-sheet-close" aria-label="关闭" @click="close"><el-icon><Close /></el-icon></button>
    </header>
    <div class="mesh-sheet-body"><slot /></div>
  </el-drawer>
</template>
