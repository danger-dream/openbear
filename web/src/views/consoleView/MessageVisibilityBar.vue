<script setup>
import {onBeforeUnmount, onMounted, watch} from 'vue';
import {Close, Hide} from '@element-plus/icons-vue';
import {useMessageVisibility} from './messageVisibility.js';
const props = defineProps({workDetailOpen: {type: Boolean, default: false}});
const visibility = useMessageVisibility();
const {selecting, selected, busy, undoIds} = visibility;
let timer;
watch(undoIds, ids => { clearTimeout(timer); if (ids.length) timer = setTimeout(() => { undoIds.value = []; }, 6000); });
function onKeydown(event) {
  if (event.key !== 'Escape' || event.defaultPrevented || !selecting.value || busy.value || visibility.managing.value || !window.matchMedia('(min-width: 761px)').matches) return;
  event.preventDefault();
  visibility.cancelSelection();
}
onMounted(() => window.addEventListener('keydown', onKeydown));
onBeforeUnmount(() => { clearTimeout(timer); window.removeEventListener('keydown', onKeydown); });
</script>
<template>
  <div v-if="selecting || undoIds.length" class="message-visibility-bar" :class="{'is-work-open': props.workDetailOpen}" role="status">
    <template v-if="selecting">
      <span>已选 <strong>{{ selected.size }}</strong> 条</span>
      <div class="visibility-bar-actions"><button type="button" :disabled="busy" @click="visibility.cancelSelection">取消</button><button type="button" class="visibility-hide-selected" :disabled="busy || !selected.size" @click="visibility.hideSelected"><Hide/>{{ busy ? '正在隐藏…' : '隐藏所选' }}</button></div>
    </template>
    <template v-else><span>已隐藏 {{ undoIds.length }} 条内容</span><div class="visibility-bar-actions"><button type="button" :disabled="busy" @click="visibility.undo">撤销</button><button type="button" class="visibility-bar-close" aria-label="关闭提示" @click="undoIds = []"><Close/></button></div></template>
  </div>
</template>
<style scoped>
.message-visibility-bar { flex: none; display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 8px 16px; border-top: 1px solid var(--el-border-color-lighter); background: var(--el-bg-color); color: var(--el-text-color-regular); font-size: 12px; }
.visibility-bar-actions { display: flex; align-items: center; gap: 8px; }
.visibility-bar-actions button { display: inline-flex; justify-content: center; align-items: center; gap: 6px; min-height: 36px; padding: 0 12px; border: 0; border-radius: 8px; background: var(--el-fill-color-light); color: var(--el-text-color-regular); cursor: pointer; }
.visibility-bar-actions .visibility-hide-selected { background: var(--el-text-color-primary); color: var(--el-bg-color); }
.visibility-bar-actions .visibility-bar-close { padding: 0; width: 36px; background: transparent; }
.visibility-bar-actions svg { width: 15px; height: 15px; }
.visibility-bar-actions button:disabled { opacity: .5; cursor: default; }
button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
@media (min-width: 761px) {
  /* The column owns the anchor; this never participates in chat/composer flex sizing. */
  .message-visibility-bar {
    position: absolute; z-index: 21; left: 50%; bottom: calc(var(--console-composer-height, 135px) + 14px);
    transform: translateX(-50%); width: max-content; max-width: calc(100% - 32px);
    min-height: 44px; box-sizing: border-box; padding: 5px 6px 5px 16px; gap: 20px;
    border: 1px solid var(--el-border-color-lighter); border-radius: 14px;
    background: var(--el-bg-color-overlay); box-shadow: var(--ob-shadow-panel);
  }
  .message-visibility-bar > span { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .message-visibility-bar strong { color: var(--el-text-color-primary); font-weight: 600; }
  .visibility-bar-actions { gap: 4px; }
  .visibility-bar-actions button { min-height: 32px; padding: 0 10px; background: transparent; font-size: 12px; }
  .visibility-bar-actions button:hover:not(:disabled) { background: var(--el-fill-color-light); }
  .visibility-bar-actions .visibility-hide-selected { background: var(--el-text-color-primary); color: var(--el-bg-color); }
  .visibility-bar-actions .visibility-hide-selected:hover:not(:disabled) { background: var(--el-text-color-regular); }
  .visibility-bar-actions .visibility-bar-close { width: 32px; }
}
@media (max-width: 760px) {
  .message-visibility-bar {
    position: absolute; z-index: 60; left: 50%; bottom: calc(var(--console-composer-height, 135px) + 10px);
    transform: translateX(-50%); width: max-content; max-width: calc(100% - 24px); box-sizing: border-box;
    gap: 12px; padding: 5px 6px 5px 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 18px;
    background: var(--el-bg-color-overlay); box-shadow: var(--ob-shadow-panel);
  }
  .message-visibility-bar.is-work-open { display: none; }
  .message-visibility-bar > span { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .message-visibility-bar strong { color: var(--el-text-color-primary); font-weight: 600; }
  .visibility-bar-actions { gap: 2px; }
  .visibility-bar-actions button { min-height: 44px; padding: 0 10px; border-radius: 12px; background: transparent; font-size: 12px; white-space: nowrap; }
  .visibility-bar-actions .visibility-hide-selected { background: var(--ob-text-strong); color: var(--ob-text-inverse); }
  .visibility-bar-actions .visibility-bar-close { width: 44px; }
}
</style>
