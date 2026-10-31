<script setup>
import { nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, useId, watch } from "vue";
import { useAdminPhone } from "../adminViewport.js";

const props = defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: "" },
  description: { type: String, default: "" },
  inlineActions: { type: Boolean, default: false },
});
const emit = defineEmits(["mobile-header-ready"]);
const isPhone = useAdminPhone();
const root = ref(null);
const moreButton = ref(null);
const actions = ref(null);
const moreOpen = ref(false);
const actionsId = `admin-header-actions-${useId()}`;

async function toggleMore() {
  moreOpen.value = !moreOpen.value;
  if (moreOpen.value) {
    await nextTick();
    actions.value?.querySelector('button:not(:disabled), input:not(:disabled), a[href]')?.focus();
  }
}
function closeMore(restoreFocus = false) {
  moreOpen.value = false;
  if (restoreFocus) moreButton.value?.focus();
}
function closeOutside(event) {
  if (moreOpen.value && !root.value?.contains(event.target)) closeMore();
}
function closeAfterAction(event) {
  // Checkboxes and switches stay open so their changed state remains visible.
  if (event.target.closest('button:not([role="switch"]), a[href]')) closeMore();
}
function closeOnFocusLeave(event) {
  if (event.relatedTarget && !root.value?.contains(event.relatedTarget)) closeMore();
}
watch(isPhone, () => closeMore());
onMounted(() => {
  document.addEventListener("pointerdown", closeOutside);
  emit("mobile-header-ready", true);
});
onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", closeOutside);
  emit("mobile-header-ready", false);
});
// A kept-alive page is not mounted again when it comes back.
onActivated(() => emit("mobile-header-ready", true));
onDeactivated(() => { closeMore(); emit("mobile-header-ready", false); });
</script>

<template>
  <header ref="root" class="admin-page-header" :class="{ 'has-inline-actions': props.inlineActions }" @keydown.esc.stop="closeMore(true)" @focusout="closeOnFocusLeave">
    <div class="admin-header-navigation"><slot name="mobile-navigation" /></div>
    <div class="admin-header-identity">
      <h1 class="admin-header-title">{{ props.title }}</h1>
      <p v-if="props.subtitle" class="admin-header-subtitle" :title="props.subtitle">{{ props.subtitle }}</p>
    </div>
    <div
      v-show="!isPhone || props.inlineActions || moreOpen"
      :id="actionsId"
      ref="actions"
      class="admin-header-actions"
      :class="{ 'is-open': moreOpen }"
      :aria-label="`${props.title}辅助操作`"
      @click="closeAfterAction"
    >
      <p v-if="props.description" class="admin-header-description">{{ props.description }}</p>
      <slot name="actions" />
    </div>
    <div class="admin-header-primary"><slot name="primary" /></div>
    <button
      v-if="!props.inlineActions"
      ref="moreButton"
      type="button"
      class="admin-header-more"
      :aria-label="`${props.title}更多操作`"
      :aria-expanded="moreOpen"
      :aria-controls="actionsId"
      @click="toggleMore"
    ><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg></button>
  </header>
</template>

<style scoped>
.admin-page-header {
  position: relative;
  z-index: 20;
  display: flex;
  flex: none;
  align-items: center;
  gap: 12px;
  box-sizing: border-box;
  height: 66px;
  min-width: 0;
  padding: 0 25px;
  border-bottom: 1px solid var(--ob-chat-line);
  background: var(--ob-chat-bg);
  color: var(--ob-chat-text);
}
.admin-header-navigation, .admin-header-more, .admin-header-description { display: none; }
.admin-header-identity { flex: 1 1 0%; min-width: 0; }
.admin-header-title { margin: 0; font-size: 13px; font-weight: 500; line-height: 20px; white-space: nowrap; }
.admin-header-subtitle { margin: 2px 0 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-chat-subtle); font-size: 10.5px; line-height: 16px; }
.admin-header-actions, .admin-header-primary { display: flex; flex: none; align-items: center; gap: 10px; }
.admin-page-header :deep(.el-button), .admin-page-header :deep(.admin-header-link) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  height: 30px;
  min-height: 30px;
  margin: 0;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-chat-subtle);
  font-size: 11.5px;
  font-weight: 400;
  box-shadow: none;
  white-space: nowrap;
}
.admin-page-header :deep(.el-button:not(:disabled):hover), .admin-page-header :deep(.admin-header-link:hover) { background: var(--ob-chat-hover); color: var(--ob-chat-text); }
.admin-header-primary :deep(.el-button) { background: var(--ob-chat-selected); color: var(--ob-chat-text); font-weight: 500; }
.admin-page-header :deep(.el-button.is-disabled) { opacity: .45; }
.admin-page-header :deep(.el-checkbox) { margin-right: 0; }
.admin-page-header :deep(.el-checkbox__label), .admin-page-header :deep(.el-switch__label) { font-size: 11.5px; color: var(--ob-chat-subtle); }
.admin-page-header :deep(.el-switch__label--left) { display: none; }
.admin-page-header :deep(.el-button:focus-visible), .admin-page-header :deep(.admin-header-link:focus-visible), .admin-header-more:focus-visible { outline: 2px solid var(--ob-focus); outline-offset: 2px; }
@media (min-width: 761px) and (max-width: 1120px) {
  .admin-page-header { gap: 8px; padding-inline: 18px; }
  .admin-header-actions { gap: 6px; }
}
@media (max-width: 760px) {
  .admin-page-header { height: calc(60px + env(safe-area-inset-top, 0px)); padding: env(safe-area-inset-top, 0px) 8px 0; gap: 5px; }
  .admin-header-navigation { display: flex; flex: 0 0 44px; }
  .admin-header-subtitle { display: none; }
  .admin-header-primary { margin-left: auto; }
  .admin-header-primary :deep(.el-button) { height: 32px; min-height: 32px; padding-inline: 9px; }
  .admin-header-more { display: grid; flex: 0 0 44px; width: 44px; height: 44px; place-items: center; border: 0; border-radius: 8px; color: var(--ob-chat-subtle); background: transparent; }
  .admin-header-more[aria-expanded="true"] { color: var(--ob-chat-text); background: var(--ob-chat-hover); }
  .admin-header-more svg { width: 18px; height: 18px; fill: currentColor; }
  .admin-header-actions { position: absolute; top: calc(100% + 4px); right: 10px; display: flex; flex-direction: column; align-items: stretch; gap: 4px; width: min(270px, calc(100vw - 20px)); max-height: 60dvh; overflow-y: auto; padding: 8px; border: 1px solid var(--ob-chat-line); border-radius: 10px; background: var(--ob-chat-panel); box-shadow: var(--ob-shadow-popover); }
  .admin-header-actions :deep(.el-button), .admin-header-actions :deep(.admin-header-link), .admin-header-actions :deep(.el-checkbox), .admin-header-actions :deep(.el-switch) { justify-content: flex-start; width: 100%; height: auto; min-height: 38px; margin: 0; padding: 0 9px; font-size: 12px; }
  .admin-header-actions :deep(.el-switch) { box-sizing: border-box; }
  .admin-header-actions :deep(.el-checkbox__label), .admin-header-actions :deep(.el-switch__label) { font-size: 12px; }
  .has-inline-actions .admin-header-actions { position: static; flex-direction: row; align-items: center; gap: 2px; width: auto; max-height: none; overflow: visible; padding: 0; border: 0; background: transparent; box-shadow: none; }
  .has-inline-actions .admin-header-actions :deep(.el-button) { width: auto; min-height: 44px; padding-inline: 8px; }
  .has-inline-actions .admin-header-description { display: none; }
  .admin-header-description { display: block; margin: 0 0 4px; padding: 5px 9px 9px; border-bottom: 1px solid var(--ob-chat-line); font-size: 11px; line-height: 1.65; color: var(--ob-chat-subtle); }
}
</style>
