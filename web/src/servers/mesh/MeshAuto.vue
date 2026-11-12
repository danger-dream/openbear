<script setup>
// Under a field the mesh can work out: "自动：why" while it does; once set by hand, what the mesh would
// set and why, and the way back to it.
defineProps({auto: Boolean, reason: {type: String, default: ''}, value: {type: String, default: ''}, same: Boolean});
const emit = defineEmits(['restore']);
</script>
<template>
  <small v-if="auto" class="m3-auto">自动{{ reason ? `：${reason}` : '' }}</small>
  <small v-else class="m3-auto" data-set>已改 · {{ same ? '和自动值相同' : `自动值是 ${value}${reason ? `（${reason}）` : ''}` }} ·
    <button type="button" class="ix-text-button" @click="emit('restore')">恢复自动</button></small>
</template>
