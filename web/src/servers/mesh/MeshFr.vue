<script setup>
// One row of a node page section: the label on the left (with a note under it), the control on the right,
// then a hint, what the mesh would set by itself and why, and any errors.
// auto: {value, why} — shown as "自动：why", or with changed as "已改 · 自动值是 value（why）· 恢复自动".
defineProps({label: {type: String, default: ''}, sub: {type: String, default: ''}, hint: {type: String, default: ''}, auto: {type: Object, default: null},
  changed: Boolean, restorable: Boolean, errors: {type: Array, default: () => []}});
const emit = defineEmits(['restore']);
</script>
<template>
  <div class="m3-fr">
    <div class="m3-fl"><slot name="label">{{ label }}</slot><small v-if="sub">{{ sub }}</small></div>
    <div class="m3-fc">
      <slot />
      <p v-if="hint || $slots.hint" class="m3-hint"><slot name="hint">{{ hint }}</slot></p>
      <p v-if="auto && changed" class="m3-autoline is-changed">已改 · 自动值是 {{ auto.value }}{{ auto.why ? `（${auto.why}）` : '' }}
        <button v-if="restorable" type="button" class="ix-text-button" @click="emit('restore')">恢复自动</button></p>
      <p v-else-if="auto" class="m3-autoline">自动：{{ auto.why || auto.value }}</p>
      <small v-for="e in errors" :key="e" class="ob-bad m3-fr-error">{{ e }}</small>
    </div>
  </div>
</template>
