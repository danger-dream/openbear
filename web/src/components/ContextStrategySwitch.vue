<script setup>
const props = defineProps({
  modelValue: {type: String, default: "sliding_window"},
  disabled: {type: Boolean, default: false},
  inherit: {type: Boolean, default: false},
  inheritedLabel: {type: String, default: ""},
});
const emit = defineEmits(["update:modelValue"]);
</script>

<template>
  <div class="context-strategy-switch">
    <div class="strategy-segments" role="group" aria-label="上下文压缩策略">
      <button v-if="props.inherit" type="button" :disabled="props.disabled" :aria-pressed="props.modelValue === 'inherit'"
        :class="{'is-active': props.modelValue === 'inherit'}" @click="emit('update:modelValue', 'inherit')">继承默认</button>
      <button type="button" :disabled="props.disabled" :aria-pressed="props.modelValue === 'sliding_window'"
        :class="{'is-active': props.modelValue === 'sliding_window'}" @click="emit('update:modelValue', 'sliding_window')">滑动窗口</button>
      <button type="button" :disabled="props.disabled" :aria-pressed="props.modelValue === 'model_summary'"
        :class="{'is-active': props.modelValue === 'model_summary'}" @click="emit('update:modelValue', 'model_summary')">模型摘要</button>
    </div>
    <p class="strategy-caption" aria-live="polite">{{ props.modelValue === 'inherit'
      ? `跟随${props.inheritedLabel || '上级目录或系统设置'}`
      : props.modelValue === 'model_summary' ? '由模型整理历史，保留摘要与近期原文' : '保留近期原文，按需查历史，不调用摘要模型' }}</p>
  </div>
</template>

<style scoped>
.context-strategy-switch { min-width: 0; }
.strategy-segments { display: flex; padding: 3px; gap: 3px; background: var(--ob-surface-soft); border-radius: 9px; box-shadow: var(--ob-shadow-inset); }
.strategy-segments button { flex: 1; min-width: 0; padding: 7px 10px; border: 0; border-radius: 7px; background: transparent; color: var(--ob-text-subtle); font: inherit; font-size: 14px; font-weight: 500; line-height: 20px; white-space: nowrap; cursor: pointer; transition: background .15s, box-shadow .15s, color .15s; }
.strategy-segments button:hover:not(:disabled) { color: var(--ob-text-strong); background: var(--ob-surface-soft); }
.strategy-segments button.is-active { color: var(--ob-text-strong); background: var(--ob-surface); box-shadow: var(--ob-shadow-inset); }
.strategy-segments button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
.strategy-segments button:disabled { opacity: .55; cursor: wait; }
.strategy-caption { margin: 8px 1px 0; color: var(--ob-text-subtle); font-size: 13px; line-height: 1.6; }

</style>
