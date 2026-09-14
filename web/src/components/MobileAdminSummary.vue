<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue';
defineProps({ items: { type: Array, default: () => [] } });
const root = ref(null);
function closeOutside(event) {
  if (root.value?.open && !root.value.contains(event.target)) root.value.open = false;
}
onMounted(() => document.addEventListener('pointerdown', closeOutside));
onBeforeUnmount(() => document.removeEventListener('pointerdown', closeOutside));
</script>

<template>
  <details ref="root" class="admin-mobile-only admin-summary" @keydown.esc="root.open = false">
    <summary><span><slot /></span><span class="admin-summary-disclosure">详情 <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg></span></summary>
    <dl class="admin-summary-details">
      <div v-for="item in items" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value ?? '—' }}</dd></div>
    </dl>
  </details>
</template>
