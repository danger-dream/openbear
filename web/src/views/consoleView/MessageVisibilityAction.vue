<script setup>
import {computed, ref} from 'vue';
import {Check, Hide, MoreFilled, Select, Files} from '@element-plus/icons-vue';
import {useMessageVisibility, visibilityOperationId} from './messageVisibility.js';
const props = defineProps({
  target: {type: Object, required: true},
  turn: {type: Object, default: null},
  desktopPlacement: {type: String, default: 'inline'},
  mobileLongPress: {type: Boolean, default: false},
});
const visibility = useMessageVisibility();
const {selecting, selected, busy} = visibility;
const open = ref(false);
const hasAssistantTargets = computed(() => props.turn && visibility.assistantTargets(props.turn).length > 0);
function hideTurn() { open.value = false; void visibility.hideAssistantTurn(props.turn); }
function hide() { open.value = false; void visibility.hide([props.target]); }
function select() { open.value = false; visibility.startSelection(props.target); }
</script>

<template>
  <span v-if="visibility.canTarget(props.target)" class="message-visibility-action"
        :class="[`placement-${props.desktopPlacement}`, {'is-selecting': selecting, 'is-open': open, 'uses-long-press': props.mobileLongPress}]">
    <label v-if="selecting" class="visibility-select" title="选择消息">
      <input type="checkbox" :checked="selected.has(visibilityOperationId(props.target))" aria-label="选择消息" :disabled="busy" @change="visibility.toggle(props.target)"/>
      <span class="visibility-check" aria-hidden="true"><Check/></span>
    </label>
    <template v-else>
    <button v-if="!props.mobileLongPress" type="button" class="visibility-more visibility-mobile-more" aria-label="更多消息操作" aria-haspopup="dialog" :disabled="busy" @click="visibility.openMobileMenu(props.target, props.turn)"><MoreFilled/></button>
    <el-popover v-model:visible="open" trigger="click" placement="bottom-start" :width="164" :show-arrow="false" popper-class="visibility-action-popover">
      <template #reference><button type="button" class="visibility-more visibility-desktop-more" aria-label="更多消息操作" :aria-expanded="open" :disabled="busy"><MoreFilled/></button></template>
      <div class="visibility-action-menu">
        <button type="button" @click="hide"><Hide/><span>隐藏这条内容</span></button>
        <button v-if="hasAssistantTargets" type="button" class="visibility-hide-turn" :disabled="busy" title="隐藏本轮已有的模型回复和工作详情，保留用户提问与插话" @click="hideTurn"><Files/><span>隐藏本轮模型回复</span></button>
        <button type="button" @click="select"><Select/><span>选择多条</span></button>
      </div>
    </el-popover>
    </template>
  </span>
</template>

<style scoped>
.message-visibility-action { display: inline-flex; vertical-align: middle; flex: none; }
.visibility-more, .visibility-select { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; padding: 0; border: 0; border-radius: 7px; background: transparent; color: var(--el-text-color-secondary); cursor: pointer; }
.visibility-more:hover { background: var(--el-fill-color-light); color: var(--el-text-color-primary); }
.visibility-more svg { width: 15px; height: 15px; }
.visibility-mobile-more { display: none; }
.visibility-more:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 1px; }
.visibility-select input { width: 17px; height: 17px; accent-color: var(--el-color-primary); cursor: pointer; }
.visibility-check, .visibility-action-menu .visibility-hide-turn { display: none; }
.visibility-action-menu { display: grid; gap: 2px; }
.visibility-action-menu button { display: flex; align-items: center; gap: 9px; min-height: 40px; padding: 0 9px; border: 0; border-radius: 7px; background: transparent; color: var(--el-text-color-primary); font-size: 13px; cursor: pointer; }
.visibility-action-menu button:hover { background: var(--el-fill-color-light); }
.visibility-action-menu svg { width: 16px; height: 16px; color: var(--el-text-color-secondary); }
@media (min-width: 761px) {
  /* Dock beside the first line, never create a footer row just for this action. */
  .message-visibility-action.placement-gutter,
  .message-visibility-action.placement-footer.is-selecting {
    position: absolute; left: -30px; top: 0; z-index: 3; display: flex;
  }
  .message-visibility-action.placement-footer:not(.is-selecting) .visibility-more { width: 22px; height: 22px; }
  .visibility-select { position: relative; width: 24px; height: 26px; }
  .visibility-select input { position: absolute; inset: 0; width: 24px; height: 26px; margin: 0; opacity: 0; z-index: 1; }
  .visibility-check { display: inline-flex; box-sizing: border-box; align-items: center; justify-content: center; width: 17px; height: 17px; border: 1.5px solid var(--el-border-color-darker); border-radius: 50%; background: var(--el-bg-color); color: var(--ob-text-inverse); transition: border-color .12s ease, background .12s ease; }
  .visibility-check svg { width: 11px; height: 11px; opacity: 0; stroke: currentColor; stroke-width: 1; }
  .visibility-select:hover .visibility-check { border-color: var(--ob-border-strong); }
  .visibility-select input:checked + .visibility-check { border-color: var(--ob-text-subtle); background: var(--ob-text-subtle); }
  .visibility-select input:checked + .visibility-check svg { opacity: 1; }
  .visibility-select input:focus-visible + .visibility-check { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
  .visibility-select input:disabled { cursor: wait; }
  .visibility-select input:disabled + .visibility-check { opacity: .5; }
  .visibility-action-menu .visibility-hide-turn { display: flex; }
}
@media (min-width: 761px) and (hover: hover) and (pointer: fine) {
  .message-visibility-action { opacity: 0; pointer-events: none; transition: opacity .12s ease; }
  .message-visibility-action.is-selecting,
  .message-visibility-action.is-open,
  .message-visibility-action:focus-within { opacity: 1; pointer-events: auto; }
}
@media (max-width: 760px), (pointer: coarse) {
  .visibility-more, .visibility-select { width: 44px; height: 44px; }
  .visibility-action-menu button { min-height: 44px; }
}
@media (max-width: 760px) {
  /* Phone menus belong to the row's long press. Only selection owns a rail. */
  .message-visibility-action.uses-long-press:not(.is-selecting) { display: none; }
  .message-visibility-action.placement-gutter,
  .message-visibility-action.placement-footer.is-selecting { position: absolute; left: -12px; top: calc(var(--visibility-line-center, 12px) - 22px); z-index: 3; }
  .message-visibility-action.placement-footer.is-selecting { top: 0; }
  .visibility-desktop-more { display: none; }
  .visibility-mobile-more { display: inline-flex; }
  .visibility-more { border-radius: 50%; -webkit-tap-highlight-color: transparent; }
  .visibility-more:active { background: var(--el-fill-color); }
  .visibility-select { position: relative; border-radius: 50%; -webkit-tap-highlight-color: transparent; }
  .visibility-select input { position: absolute; inset: 0; width: 44px; height: 44px; margin: 0; opacity: 0; z-index: 1; }
  .visibility-check { display: inline-flex; box-sizing: border-box; align-items: center; justify-content: center; width: 20px; height: 20px; border: 1.5px solid var(--el-border-color-darker); border-radius: 50%; background: var(--el-bg-color); color: var(--ob-text-inverse); transition: border-color .12s ease, background .12s ease; }
  .visibility-check svg { width: 12px; height: 12px; opacity: 0; stroke: currentColor; stroke-width: 1; }
  .visibility-select input:checked + .visibility-check { border-color: var(--ob-text-subtle); background: var(--ob-text-subtle); }
  .visibility-select input:checked + .visibility-check svg { opacity: 1; }
  .visibility-select input:focus-visible + .visibility-check { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
  .visibility-select input:disabled + .visibility-check { opacity: .5; }
}
</style>
<style>
.visibility-action-popover.el-popper { padding: 5px; border-radius: 11px; }
@media (min-width: 761px) {
  .visibility-action-popover.el-popper { min-width: 194px; }
}
</style>
