<script setup>
import {computed, nextTick, onBeforeUnmount, ref, watch} from "vue";
import draggable from "vuedraggable";
import FloatingPanel from "../references/FloatingPanel.vue";
import {scrollModelListAbove} from "./modelDragAutoScroll.js";
const props = defineProps({
  modelValue: {type: Array, default: () => []},
  models: {type: Array, default: () => []},
  disabled: {type: Boolean, default: false},
  label: {type: String, default: "摘要模型"},
  emptyLabel: {type: String, default: "使用当前执行模型"},
  footerText: {type: String, default: ""},
});
const emit = defineEmits(["update:modelValue"]);
const trigger = ref(null);
const floating = ref(null);
const orderScrollContainer = ref(null);
let stopOrderScroll = () => {};
function startOrderScroll() {
  stopOrderScroll();
  stopOrderScroll = scrollModelListAbove(orderScrollContainer.value);
}
function finishOrderScroll() {
  stopOrderScroll();
  stopOrderScroll = () => {};
}
onBeforeUnmount(finishOrderScroll);
const open = ref(false);
const query = ref("");
const input = ref(null);
const byKey = computed(() => new Map(props.models.map(model => [model.key, model])));
const selected = computed({
  get: () => props.modelValue.map(key => ({key, ...byKey.value.get(key)})),
  set: items => { if (!props.disabled) emit("update:modelValue", items.map(item => item.key)); },
});
const triggerLabel = computed(() => {
  const first = selected.value[0];
  return first ? first.label || first.model || first.key : props.emptyLabel;
});
const groups = computed(() => {
  const grouped = new Map();
  const needle = query.value.trim().toLowerCase();
  for (const model of props.models) {
    if (needle && !`${model.key} ${model.label || ''}`.toLowerCase().includes(needle)) continue;
    const provider = model.provider || model.key.split("/")[0];
    if (!grouped.has(provider)) grouped.set(provider, []);
    grouped.get(provider).push(model);
  }
  return Array.from(grouped, ([name, models]) => ({name, models}));
});
function toggle(key) {
  if (props.disabled) return;
  emit("update:modelValue", props.modelValue.includes(key) ? props.modelValue.filter(item => item !== key) : [...props.modelValue, key]);
}
function move(index, direction) {
  const items = [...props.modelValue];
  const target = index + direction;
  if (props.disabled || target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
  emit("update:modelValue", items);
}
function closePicker(restoreFocus = false) {
  finishOrderScroll();
  open.value = false;
  query.value = "";
  if (restoreFocus) trigger.value?.focus();
}
async function toggleOpen() {
  if (props.disabled) return;
  if (open.value) return closePicker();
  open.value = true;
  await nextTick();
  await floating.value?.position();
  await nextTick();
  if (open.value) input.value?.focus({preventScroll: true});
}
watch(() => props.disabled, disabled => { if (disabled) closePicker(); });
</script>

<template>
  <div class="model-order-picker">
    <button ref="trigger" class="model-picker-trigger" type="button" :disabled="props.disabled" :aria-expanded="open"
      aria-haspopup="dialog" :aria-label="`选择${props.label}：${triggerLabel}${selected.length > 1 ? `，共 ${selected.length} 个` : ''}`"
      @keydown.esc.stop="closePicker(true)" @click="toggleOpen">
      <span class="model-picker-trigger-label">{{ triggerLabel }}</span>
      <span v-if="selected.length > 1" class="model-picker-count">+{{ selected.length - 1 }}</span>
      <svg class="picker-chevron" :class="{'is-open': open}" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button>
    <FloatingPanel ref="floating" :open="open" :anchor="trigger" placement="bottom-end" :width="340" :label="`选择${props.label}`" @close="closePicker()">
      <div class="model-picker-dropdown" role="dialog" :aria-label="`选择${props.label}`" @keydown.esc.stop="closePicker(true)">
        <input ref="input" v-model="query" type="search" placeholder="搜索模型或渠道…" :aria-label="`搜索${props.label}`" class="model-picker-search" />
        <div ref="orderScrollContainer" class="model-picker-content">
          <section v-if="selected.length" class="model-order-section" :aria-label="`${props.label}尝试顺序`">
            <h5>尝试顺序 <span>拖动调整</span></h5>
            <draggable v-model="selected" item-key="key" handle=".model-order-grip"
              :disabled="props.disabled" :animation="160" ghost-class="model-order-ghost" class="model-order-list"
              :scroll="orderScrollContainer || true" :force-auto-scroll-fallback="true"
              :scroll-sensitivity="80" :scroll-speed="18" :bubble-scroll="false"
              @start="startOrderScroll" @end="finishOrderScroll">
              <template #item="{element, index}">
                <div class="model-order-item">
                  <span class="model-order-grip" aria-hidden="true" title="拖动调整尝试顺序"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg></span>
                  <span class="model-order-number">{{ index + 1 }}</span>
                  <span class="model-order-name" :class="{'is-unavailable': !byKey.has(element.key)}" :title="element.key">{{ element.label || element.model || element.key }}{{ byKey.has(element.key) ? '' : '（不可用）' }}</span>
                  <div class="model-order-actions">
                    <button type="button" :disabled="props.disabled || index === 0" :aria-label="`上移 ${element.key}`" title="上移" @click="move(index, -1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 6-6 6 6"/></svg></button>
                    <button type="button" :disabled="props.disabled || index === selected.length - 1" :aria-label="`下移 ${element.key}`" title="下移" @click="move(index, 1)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 10 6 6 6-6"/></svg></button>
                    <button type="button" :disabled="props.disabled" :aria-label="`移除 ${element.key}`" title="移除" @click="toggle(element.key)"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg></button>
                  </div>
                </div>
              </template>
            </draggable>
          </section>
          <div class="model-picker-options">
            <section v-for="group in groups" :key="group.name">
              <h5>{{ group.name }}</h5>
              <button v-for="model in group.models" :key="model.key" class="model-picker-option" type="button" :title="model.key"
                :disabled="props.disabled" :aria-pressed="props.modelValue.includes(model.key)" @click="toggle(model.key)">
                <span class="model-picker-check" :class="{'is-checked': props.modelValue.includes(model.key)}" aria-hidden="true"><svg v-if="props.modelValue.includes(model.key)" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg></span>
                <span class="model-picker-label">{{ model.label || model.model || model.key }}</span>
              </button>
            </section>
            <p v-if="!groups.length" class="model-picker-empty">没有匹配的可用模型</p>
          </div>
        </div>
        <div class="model-picker-dropdown-footer"><span>{{ props.footerText || (selected.length ? '按顺序尝试，最后回退当前执行模型。' : '未选候选时，使用当前执行模型。') }}</span><button type="button" @click="closePicker(true)">完成</button></div>
      </div>
    </FloatingPanel>
  </div>
</template>

<style scoped>
.model-order-picker { width: 100%; min-width: 0; font-size: 13px; }
.model-picker-trigger { box-sizing: border-box; width: 100%; height: 32px; display: flex; gap: 8px; align-items: center; padding: 0 10px; border: 1px solid var(--ob-border); border-radius: 7px; color: var(--ob-text); background: linear-gradient(var(--ob-surface), var(--ob-surface-soft)); box-shadow: var(--ob-shadow-inset); font: inherit; text-align: left; cursor: pointer; }
.model-picker-trigger:hover:not(:disabled) { border-color: var(--ob-border-strong); }
.model-picker-trigger:disabled { opacity: .6; cursor: default; }
.model-picker-trigger-label { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.model-picker-count { flex: 0 0 auto; padding: 0 5px; border-radius: 4px; background: var(--ob-surface-soft); font-size: 13px; line-height: 20px; color: var(--ob-text-subtle); font-variant-numeric: tabular-nums; }
.picker-chevron { display: block; flex: 0 0 14px; width: 14px; height: 14px; color: var(--ob-text-subtle); }
.picker-chevron.is-open { transform: rotate(180deg); }
.model-picker-dropdown { box-sizing: border-box; display: flex; flex: 1 1 auto; flex-direction: column; min-height: 0; max-height: inherit; padding: 8px; overflow: hidden; font-size: 13px; }
.model-picker-search { box-sizing: border-box; flex: 0 0 auto; width: 100%; height: 30px; padding: 0 9px; border: 1px solid var(--ob-border); border-radius: 6px; background: var(--ob-surface); color: inherit; font: inherit; outline: none; }
.model-picker-search:focus { border-color: var(--ob-text-subtle); box-shadow: 0 0 0 2px var(--ob-focus); }
.model-picker-content { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; margin-top: 5px; }
.model-picker-content h5 { display: flex; justify-content: space-between; gap: 8px; margin: 8px 6px 5px; color: var(--ob-text-subtle); font-size: 13px; font-weight: 400; }
.model-order-section { padding-bottom: 8px; border-bottom: 1px solid var(--ob-border); }
.model-order-section h5 span { color: var(--ob-text-muted); }
.model-picker-option { display: flex; gap: 8px; align-items: center; width: 100%; min-height: 30px; padding: 5px 6px; border: 0; border-radius: 6px; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.model-picker-option:hover { background: var(--ob-surface-soft); }
.model-picker-check { display: flex; align-items: center; justify-content: center; box-sizing: border-box; width: 15px; height: 15px; flex: 0 0 auto; border: 1px solid var(--ob-border); border-radius: 4px; background: var(--ob-surface); }
.model-picker-check.is-checked { border-color: var(--ob-blue); background: var(--ob-blue); color: var(--ob-text-inverse); }
.model-picker-label, .model-order-name { min-width: 0; flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.model-order-name.is-unavailable { color: var(--ob-warning); }
.model-picker-empty { margin: 12px 6px; line-height: 1.6; color: var(--ob-text-subtle); }
.model-picker-dropdown-footer { display: flex; flex: 0 0 auto; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 3px 0; margin-top: 6px; font-size: 13px; line-height: 1.5; color: var(--ob-text-subtle); border-top: 1px solid var(--ob-border); }
.model-picker-dropdown-footer button { flex: 0 0 auto; border: 0; padding: 3px; background: none; color: var(--ob-blue); font: inherit; cursor: pointer; }
.model-order-list { display: flex; flex-direction: column; gap: 3px; }
.model-order-item { display: flex; align-items: center; gap: 5px; padding: 2px 4px; min-height: 30px; background: var(--ob-surface-soft); border-radius: 6px; }
.model-order-grip { display: flex; flex: 0 0 14px; align-items: center; color: var(--ob-text-muted); cursor: grab; touch-action: none; }
.model-order-number { color: var(--ob-text-muted); min-width: 12px; font-variant-numeric: tabular-nums; }
.model-order-actions { display: flex; flex: 0 0 auto; gap: 1px; }
.model-order-actions button { display: flex; align-items: center; justify-content: center; border: 0; padding: 0; border-radius: 4px; width: 23px; height: 25px; background: transparent; color: var(--ob-text-subtle); cursor: pointer; }
.model-order-actions svg { display: block; width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.model-order-actions button:hover:not(:disabled) { background: var(--ob-surface-soft); color: var(--ob-text); }
.model-order-actions button:disabled { opacity: .3; cursor: default; }
.model-order-ghost { opacity: .35; }
button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 1px; }

</style>
