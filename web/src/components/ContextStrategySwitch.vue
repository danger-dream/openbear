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
.strategy-segments { display: flex; padding: 3px; gap: 3px; background: #e9ebee; border-radius: 9px; box-shadow: inset 0 1px 2px #18212d08; }
.strategy-segments button { flex: 1; min-width: 0; padding: 7px 10px; border: 0; border-radius: 7px; background: transparent; color: #626873; font: inherit; font-size: 14px; font-weight: 500; line-height: 20px; white-space: nowrap; cursor: pointer; transition: background .15s, box-shadow .15s, color .15s; }
.strategy-segments button:hover:not(:disabled) { color: #222933; background: #ffffff70; }
.strategy-segments button.is-active { color: #252c36; background: #fff; box-shadow: 0 1px 3px #10182620, 0 0 0 1px #10182605; }
.strategy-segments button:focus-visible { outline: 2px solid #4385ef; outline-offset: 2px; }
.strategy-segments button:disabled { opacity: .55; cursor: wait; }
.strategy-caption { margin: 8px 1px 0; color: #737b88; font-size: 13px; line-height: 1.6; }
:global(html.dark) .strategy-segments { background: #202329; }
:global(html.dark) .strategy-segments button { color: #a8b0bc; }
:global(html.dark) .strategy-segments button.is-active { background: #41464f; color: #f3f4f6; box-shadow: 0 1px 3px #0005; }
:global(html.dark) .strategy-segments button:hover:not(:disabled) { background: #41464f80; color: #f3f4f6; }
:global(html.dark) .strategy-caption { color: #a1a9b7; }
</style>
