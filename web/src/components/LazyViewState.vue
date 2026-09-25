<script setup>
defineProps({ label: { type: String, default: "页面" }, failed: Boolean });
defineEmits(["retry"]);
</script>

<template>
  <div class="lazy-view-state" :role="failed ? 'alert' : 'status'" :aria-busy="!failed">
    <div class="lazy-view-card">
      <strong>{{ failed ? `${label}加载失败` : `正在加载${label}…` }}</strong>
      <template v-if="failed">
        <p>网络暂不可用，或页面版本过期、旧资源已被替换。可重试加载；若仍失败，请先保存草稿和未提交设置，再手动刷新页面。</p>
        <button type="button" @click="$emit('retry')">重试加载</button>
      </template>
      <p v-else>首次打开可能需要片刻。</p>
    </div>
  </div>
</template>

<style scoped>
.lazy-view-state { display: grid; flex: 1; min-height: 140px; min-width: 0; place-items: center; padding: 24px; color: var(--ob-text-subtle); }
.lazy-view-card { width: min(100%, 380px); padding: 22px; border: 1px solid var(--ob-border); border-radius: 12px; background: var(--ob-surface); box-shadow: 0 1px 3px rgb(var(--ob-shadow-rgb) / .04); }
strong { color: var(--ob-text-strong); font-size: 14px; font-weight: 600; }
p { margin: 8px 0 0; font-size: 13px; line-height: 1.65; }
button { margin-top: 16px; min-height: 36px; border: 1px solid var(--ob-border); border-radius: 8px; padding: 6px 14px; background: var(--ob-surface); color: var(--ob-text); font-size: 13px; }
button:hover { background: var(--ob-surface-soft); }
button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 3px; }

@media (max-width: 760px), (hover: none) and (pointer: coarse) { button { min-height: 44px; } }
</style>
