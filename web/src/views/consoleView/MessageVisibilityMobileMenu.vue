<script setup>
import {computed, onBeforeUnmount, onMounted} from 'vue';
import {Files, Hide, Select} from '@element-plus/icons-vue';
import {useMessageVisibility} from './messageVisibility.js';
const visibility = useMessageVisibility();
const {mobileMenu, busy} = visibility;
const open = computed({get: () => Boolean(mobileMenu.value), set: value => { if (!value) mobileMenu.value = null; }});
const assistantCount = computed(() => visibility.assistantTargets(mobileMenu.value?.turn).length);
let media;
function updateMedia() { if (!media.matches) open.value = false; }
onMounted(() => { media = window.matchMedia('(max-width: 760px)'); media.addEventListener('change', updateMedia); });
onBeforeUnmount(() => media?.removeEventListener('change', updateMedia));
function choose(action) {
  const menu = mobileMenu.value;
  if (!menu || busy.value) return;
  open.value = false;
  if (action === 'turn') void visibility.hideAssistantTurn(menu.turn);
  else if (visibility.canTarget(menu.target) && !visibility.isHidden(menu.target)) {
    if (action === 'select') visibility.startSelection(menu.target);
    else void visibility.hide([menu.target]);
  }
}
</script>

<template>
  <el-drawer v-model="open" direction="btt" size="auto" title="消息操作" class="visibility-mobile-sheet" append-to-body destroy-on-close>
    <div v-if="mobileMenu?.preview" class="visibility-sheet-target" aria-label="当前操作的消息">
      <span>正在操作 · {{ mobileMenu.preview.label }}</span>
      <p>{{ mobileMenu.preview.text }}</p>
    </div>
    <div class="visibility-sheet-actions">
      <button v-if="assistantCount" type="button" class="visibility-sheet-turn" :disabled="busy" @click="choose('turn')">
        <span class="visibility-sheet-icon"><Files/></span><span><strong>隐藏本轮模型回复</strong><small>含工作详情 · 保留你的提问和插话</small></span><span class="visibility-sheet-count">{{ assistantCount }} 条</span>
      </button>
      <button type="button" :disabled="busy" @click="choose('single')"><span class="visibility-sheet-icon"><Hide/></span><span><strong>只隐藏这条内容</strong></span></button>
      <button type="button" class="visibility-sheet-select" :disabled="busy" @click="choose('select')"><span class="visibility-sheet-icon"><Select/></span><span><strong>选择多条</strong><small>点选消息，再统一隐藏</small></span></button>
    </div>
    <p class="visibility-sheet-note">只影响页面显示，不删除记录或改变 AI 上下文</p>
  </el-drawer>
</template>

<style scoped>
.visibility-sheet-target { margin: 4px 8px 10px; padding: 10px 12px; border-left: 3px solid var(--ob-border); border-radius: 3px 10px 10px 3px; background: var(--el-fill-color-light); }
.visibility-sheet-target > span { color: var(--el-text-color-secondary); font-size: 11px; line-height: 18px; }
.visibility-sheet-target p { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; margin: 3px 0 0; color: var(--el-text-color-primary); font-size: 12px; line-height: 19px; }
.visibility-sheet-actions { display: grid; gap: 4px; }
.visibility-sheet-actions button { display: flex; align-items: center; gap: 12px; width: 100%; min-height: 60px; padding: 10px 8px; border: 0; border-radius: 12px; background: transparent; color: var(--el-text-color-primary); text-align: left; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.visibility-sheet-actions button:active { background: var(--el-fill-color-light); }
.visibility-sheet-actions button:disabled { opacity: .5; }
.visibility-sheet-icon { display: grid; place-items: center; width: 36px; height: 36px; flex: none; border-radius: 11px; background: var(--el-fill-color-light); color: var(--ob-text-subtle); }
.visibility-sheet-icon svg { width: 19px; height: 19px; }
.visibility-sheet-actions strong { display: block; font-size: 14px; line-height: 22px; font-weight: 550; }
.visibility-sheet-actions small { display: block; margin-top: 2px; font-size: 11px; line-height: 17px; color: var(--el-text-color-secondary); }
.visibility-sheet-count { margin-left: auto; flex: none; color: var(--el-text-color-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.visibility-sheet-select { margin-top: 4px; }
.visibility-sheet-note { margin: 12px 8px 2px; padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter); color: var(--el-text-color-secondary); font-size: 11px; line-height: 18px; }
button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
</style>
<style>
.visibility-mobile-sheet.el-drawer { max-height: calc(var(--mobile-viewport-height, 100dvh) - 12px); border-radius: 22px 22px 0 0; }
.visibility-mobile-sheet .el-drawer__header { margin: 0; padding: 8px 16px 0 24px; align-items: center; }
.visibility-mobile-sheet .el-drawer__title { font-size: 13px; font-weight: 600; color: var(--el-text-color-secondary); }
.visibility-mobile-sheet .el-drawer__close-btn { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; }
.visibility-mobile-sheet .el-drawer__body { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 4px 16px max(16px, env(safe-area-inset-bottom, 0px)); }
</style>
