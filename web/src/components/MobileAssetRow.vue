<script setup>
const props = defineProps({
  title: { type: String, default: "" }, summary: { type: String, default: "" },
  reference: { type: String, default: "" }, meta: { type: String, default: "" },
  enabled: Boolean, archived: Boolean, expanded: Boolean, selected: Boolean,
  mode: { type: String, default: "browse" },
});
const emit = defineEmits(["open", "more", "select"]);
</script>

<template>
  <div class="mobile-asset-row" :class="{ 'is-selected': props.selected }">
    <button v-if="props.mode === 'select'" type="button" class="asset-select" role="checkbox" :aria-checked="props.selected" :aria-label="`选择${props.title}`" @click="emit('select', !props.selected)"><span><svg v-if="props.selected" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 8 3 3 5-6" /></svg></span></button>
    <button type="button" class="asset-row-main" :disabled="props.mode === 'sort'" @click="props.mode === 'select' ? emit('select', !props.selected) : emit('open')">
      <span class="asset-row-heading"><span class="asset-row-title">{{ props.title }}</span><span class="asset-row-state" :class="{ 'is-off': !props.enabled || props.archived }"><i></i>{{ props.archived ? '已归档' : !props.enabled ? '未注入' : props.expanded ? '展开' : '启用' }}</span></span>
      <span class="asset-row-summary">{{ props.summary || '暂无摘要' }}</span>
      <span class="asset-row-meta"><code :title="props.reference">{{ props.reference }}</code><span :title="props.meta">{{ props.meta }}</span></span>
    </button>
    <button v-if="props.mode === 'sort'" type="button" class="drag-handle asset-row-handle" :aria-label="`拖动排序${props.title}`" @click.stop><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6h.01M16 6h.01M8 12h.01M16 12h.01M8 18h.01M16 18h.01" /></svg></button>
    <button v-else-if="props.mode === 'browse'" type="button" class="asset-row-more" :aria-label="`${props.title}的操作`" aria-haspopup="dialog" @click="emit('more')"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></svg></button>
  </div>
</template>

<style scoped>
.mobile-asset-row { position: relative; display: flex; align-items: center; min-width: 0; min-height: 77px; padding: 0 0 0 13px; color: var(--ob-chat-text); background: var(--ob-chat-panel); }
.mobile-asset-row + .mobile-asset-row::before { position: absolute; top: 0; right: 0; left: 13px; height: 1px; background: var(--ob-chat-line); content: ''; }
.mobile-asset-row.is-selected { background: var(--ob-chat-selected); }
.mobile-asset-row button { border: 0; background: transparent; color: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.mobile-asset-row button:focus-visible { outline: 2px solid var(--ob-focus); outline-offset: -2px; }
.asset-row-main { display: block; flex: 1; min-width: 0; padding: 11px 0 10px; text-align: left; }
.asset-row-main:disabled { cursor: default; }
.asset-row-heading { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.asset-row-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 500; line-height: 18px; }
.asset-row-state { display: inline-flex; flex: none; align-items: center; gap: 4px; color: var(--ob-chat-muted); font-size: 9px; font-weight: 400; white-space: nowrap; }
.asset-row-state i { width: 4px; height: 4px; border-radius: 50%; background: var(--ob-success); }
.asset-row-state.is-off i { background: var(--ob-chat-muted); }
.asset-row-summary { display: block; overflow: hidden; color: var(--ob-chat-subtle); font-size: 11px; line-height: 16px; white-space: nowrap; text-overflow: ellipsis; }
.asset-row-meta { display: flex; align-items: center; gap: 7px; margin-top: 3px; color: var(--ob-chat-muted); font-size: 9.5px; line-height: 14px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.asset-row-meta code { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 9px; }
.asset-row-meta > span { flex: none; max-width: 48%; margin-left: auto; overflow: hidden; text-overflow: ellipsis; }
.asset-row-more { display: grid; flex: none; align-self: stretch; place-items: center; width: 38px; min-height: 48px; padding: 0; }
.asset-row-more svg { width: 15px; height: 15px; fill: none; stroke: var(--ob-chat-muted); stroke-width: 1.4; }
.mobile-asset-row .asset-row-handle { display: grid; flex: none; align-self: stretch; place-items: center; width: 35px; height: auto; margin: 0; touch-action: none; }
.asset-row-handle svg { width: 16px; height: 16px; fill: none; stroke: var(--ob-chat-muted); stroke-width: 3; stroke-linecap: round; pointer-events: none; }
.asset-select { display: grid; flex: none; place-items: center; width: 28px; height: 44px; margin-right: 7px; padding: 0; }
.asset-select > span { display: grid; place-items: center; width: 17px; height: 17px; border: 1px solid var(--ob-chat-muted); border-radius: 50%; }
.is-selected .asset-select > span { border-color: var(--ob-chat-button); background: var(--ob-chat-button); color: var(--ob-chat-button-text); }
.asset-select svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.5; }
</style>
