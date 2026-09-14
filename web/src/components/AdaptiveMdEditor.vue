<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { useAdminPhone } from '../adminViewport.js';
import { resizePhoneTextarea } from './phoneTextarea.js';

defineOptions({ inheritAttrs: false });
const props = defineProps({ modelValue: { type: String, default: '' }, mobileFlow: Boolean });
const emit = defineEmits(['update:modelValue']);
const model = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) });
const isAdminPhone = useAdminPhone();
const advanced = ref(false);
const nativeInput = ref(null);
const MdEditor = defineAsyncComponent(() => import('./MdEditor.vue'));
let observer;
let observedWidth = 0;
const resize = () => resizePhoneTextarea(nativeInput.value, props.mobileFlow);
watch(nativeInput, element => {
  observer?.disconnect();
  observedWidth = 0;
  if (!element) return;
  nextTick(resize);
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width || 0;
      // Ignore our own height updates, but resize after orientation or pane reveal.
      if (width && width !== observedWidth) { observedWidth = width; resize(); }
    });
    observer.observe(element);
  }
}, { flush: 'post' });
watch(() => [props.modelValue, props.mobileFlow], () => nextTick(resize), { flush: 'post' });
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
  <div class="adaptive-md-editor" :class="{ 'is-phone': isAdminPhone, 'is-flow': isAdminPhone && mobileFlow, 'is-advanced': advanced }">
    <div v-if="isAdminPhone" class="phone-editor-tools" role="group" aria-label="编辑方式">
      <button type="button" :aria-pressed="!advanced" @click="advanced = false">文本</button>
      <button type="button" :aria-pressed="advanced" title="切换 Monaco，使用代码补全" @click="advanced = true">代码补全</button>
    </div>
    <textarea v-if="isAdminPhone && !advanced" ref="nativeInput" v-model="model" class="phone-native-editor" aria-label="正文编辑" spellcheck="false" autocomplete="off" autocorrect="off" autocapitalize="off" @input="resize" />
    <div v-else class="adaptive-code-editor"><MdEditor v-bind="$attrs" v-model="model" /></div>
  </div>
</template>

<style scoped>
.adaptive-md-editor, .adaptive-code-editor { width: 100%; height: 100%; min-width: 0; min-height: 0; }
@media (max-width: 760px) {
  .adaptive-md-editor.is-phone { display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: var(--el-bg-color); }
  .phone-editor-tools { display: flex; flex: none; align-items: center; gap: 3px; padding: 3px 6px; border-bottom: 1px solid var(--el-border-color-lighter); }
  .phone-editor-tools button { min-height: 28px; padding: 0 8px; border-radius: 5px; color: var(--el-text-color-secondary); font-size: 11px; }
  .phone-editor-tools button[aria-pressed="true"] { color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
  .phone-native-editor { display: block; width: 100%; flex: 1 1 0%; min-height: 0; resize: none; border: 0; outline: none; border-radius: 0; padding: 10px; font: 13px/1.75 'SF Mono', Menlo, Consolas, monospace; color: var(--el-text-color-primary); background: transparent; overflow-y: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
  .phone-native-editor::-webkit-scrollbar { display: none; }
  .adaptive-code-editor { flex: 1 1 0%; }
  .adaptive-md-editor.is-flow { height: auto; overflow: visible; }
  .is-flow .phone-native-editor { flex: none; min-height: 200px; overflow: hidden; }
  .is-flow .adaptive-code-editor { flex: none; height: 360px; }
  button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 1px; }
}
</style>
