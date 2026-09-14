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
.lazy-view-state { display: grid; flex: 1; min-height: 140px; min-width: 0; place-items: center; padding: 24px; color: #71717a; }
.lazy-view-card { width: min(100%, 380px); padding: 22px; border: 1px solid #e5e5ea; border-radius: 12px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
strong { color: #3f3f46; font-size: 14px; font-weight: 600; }
p { margin: 8px 0 0; font-size: 13px; line-height: 1.65; }
button { margin-top: 16px; min-height: 36px; border: 1px solid #d4d4d8; border-radius: 8px; padding: 6px 14px; background: #fafafa; color: #3f3f46; font-size: 13px; }
button:hover { background: #f4f4f5; }
button:focus-visible { outline: 2px solid #007aff; outline-offset: 3px; }
:global(html.dark) .lazy-view-card { border-color: #36363b; background: #232326; }
:global(html.dark) strong, :global(html.dark) button { color: #e4e4e7; }
:global(html.dark) button { border-color: #52525b; background: #303034; }
@media (max-width: 760px), (hover: none) and (pointer: coarse) { button { min-height: 44px; } }
</style>
