<script setup>
import {computed, nextTick, ref, watch} from "vue";
import draggable from "vuedraggable";
import FloatingPanel from "../references/FloatingPanel.vue";
const props = defineProps({
  modelValue: {type: Array, default: () => []},
  models: {type: Array, default: () => []},
  disabled: {type: Boolean, default: false},
});
const emit = defineEmits(["update:modelValue"]);
const trigger = ref(null);
const floating = ref(null);
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
  return first ? first.label || first.model || first.key : "使用当前执行模型";
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
      aria-haspopup="dialog" :aria-label="`选择摘要模型：${triggerLabel}${selected.length > 1 ? `，共 ${selected.length} 个` : ''}`"
      @keydown.esc.stop="closePicker(true)" @click="toggleOpen">
      <span class="model-picker-trigger-label">{{ triggerLabel }}</span>
      <span v-if="selected.length > 1" class="model-picker-count">+{{ selected.length - 1 }}</span>
      <svg class="picker-chevron" :class="{'is-open': open}" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button>
    <FloatingPanel ref="floating" :open="open" :anchor="trigger" placement="bottom-end" :width="340" label="选择摘要模型" @close="closePicker()">
      <div class="model-picker-dropdown" role="dialog" aria-label="选择摘要模型" @keydown.esc.stop="closePicker(true)">
        <input ref="input" v-model="query" type="search" placeholder="搜索模型或渠道…" aria-label="搜索摘要模型" class="model-picker-search" />
        <div class="model-picker-content">
          <section v-if="selected.length" class="model-order-section" aria-label="摘要模型尝试顺序">
            <h5>尝试顺序 <span>拖动调整</span></h5>
            <draggable v-model="selected" item-key="key" handle=".model-order-grip"
              :disabled="props.disabled" :animation="160" ghost-class="model-order-ghost" class="model-order-list">
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
        <div class="model-picker-dropdown-footer"><span>{{ selected.length ? '按顺序尝试，最后回退当前执行模型。' : '未选候选时，使用当前执行模型。' }}</span><button type="button" @click="closePicker(true)">完成</button></div>
      </div>
    </FloatingPanel>
  </div>
</template>

<style scoped>
.model-order-picker { width: 100%; min-width: 0; font-size: 13px; }
.model-picker-trigger { box-sizing: border-box; width: 100%; height: 32px; display: flex; gap: 8px; align-items: center; padding: 0 10px; border: 1px solid #d5d9e0; border-radius: 7px; color: #394454; background: linear-gradient(#fff, #f7f8fa); box-shadow: 0 1px 2px #15233a08; font: inherit; text-align: left; cursor: pointer; }
.model-picker-trigger:hover:not(:disabled) { border-color: #b6becb; }
.model-picker-trigger:disabled { opacity: .6; cursor: default; }
.model-picker-trigger-label { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.model-picker-count { flex: 0 0 auto; padding: 0 5px; border-radius: 4px; background: #edf0f4; font-size: 13px; line-height: 20px; color: #718096; font-variant-numeric: tabular-nums; }
.picker-chevron { display: block; flex: 0 0 14px; width: 14px; height: 14px; color: #7a8594; }
.picker-chevron.is-open { transform: rotate(180deg); }
.model-picker-dropdown { box-sizing: border-box; display: flex; flex: 1 1 auto; flex-direction: column; min-height: 0; max-height: inherit; padding: 8px; overflow: hidden; font-size: 13px; }
.model-picker-search { box-sizing: border-box; flex: 0 0 auto; width: 100%; height: 30px; padding: 0 9px; border: 1px solid #dbe0e6; border-radius: 6px; background: #fff; color: inherit; font: inherit; outline: none; }
.model-picker-search:focus { border-color: #78a6ef; box-shadow: 0 0 0 2px #4385ef18; }
.model-picker-content { flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; margin-top: 5px; }
.model-picker-content h5 { display: flex; justify-content: space-between; gap: 8px; margin: 8px 6px 5px; color: #7b8593; font-size: 13px; font-weight: 400; }
.model-order-section { padding-bottom: 8px; border-bottom: 1px solid #e7e9ee; }
.model-order-section h5 span { color: #929ba8; }
.model-picker-option { display: flex; gap: 8px; align-items: center; width: 100%; min-height: 30px; padding: 5px 6px; border: 0; border-radius: 6px; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.model-picker-option:hover { background: #eaf1fc; }
.model-picker-check { display: flex; align-items: center; justify-content: center; box-sizing: border-box; width: 15px; height: 15px; flex: 0 0 auto; border: 1px solid #cbd1db; border-radius: 4px; background: #fff; }
.model-picker-check.is-checked { border-color: #4385e7; background: #4385e7; color: #fff; }
.model-picker-label, .model-order-name { min-width: 0; flex: 1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.model-order-name.is-unavailable { color: #a17644; }
.model-picker-empty { margin: 12px 6px; line-height: 1.6; color: #818a98; }
.model-picker-dropdown-footer { display: flex; flex: 0 0 auto; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 3px 0; margin-top: 6px; font-size: 13px; line-height: 1.5; color: #7b8594; border-top: 1px solid #e7e9ee; }
.model-picker-dropdown-footer button { flex: 0 0 auto; border: 0; padding: 3px; background: none; color: #3575d4; font: inherit; cursor: pointer; }
.model-order-list { display: flex; flex-direction: column; gap: 3px; }
.model-order-item { display: flex; align-items: center; gap: 5px; padding: 2px 4px; min-height: 30px; background: #f1f3f6; border-radius: 6px; }
.model-order-grip { display: flex; flex: 0 0 14px; align-items: center; color: #99a2ae; cursor: grab; touch-action: none; }
.model-order-number { color: #8e98a7; min-width: 12px; font-variant-numeric: tabular-nums; }
.model-order-actions { display: flex; flex: 0 0 auto; gap: 1px; }
.model-order-actions button { display: flex; align-items: center; justify-content: center; border: 0; padding: 0; border-radius: 4px; width: 23px; height: 25px; background: transparent; color: #768295; cursor: pointer; }
.model-order-actions svg { display: block; width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.model-order-actions button:hover:not(:disabled) { background: #e3e8f0; color: #263c5a; }
.model-order-actions button:disabled { opacity: .3; cursor: default; }
.model-order-ghost { opacity: .35; }
button:focus-visible { outline: 2px solid #4385ef; outline-offset: 1px; }
:global(html.dark) .model-picker-trigger { background: #333841; border-color: #505865; color: #e4e8ef; }
:global(html.dark) .model-picker-count { background: #424955; color: #c0c9d5; }
:global(html.dark) .model-picker-search { background: #22272e; border-color: #535d6a; color: #e4e8ef; }
:global(html.dark) .model-order-item { background: #2f353e; }
:global(html.dark) .model-picker-option:hover { background: #3b4f6d; }
:global(html.dark) .model-picker-content h5, :global(html.dark) .model-picker-dropdown-footer { color: #a3adbb; }
:global(html.dark) .model-order-section, :global(html.dark) .model-picker-dropdown-footer { border-color: #414955; }
:global(html.dark) .model-picker-check:not(.is-checked) { background: #252a32; border-color: #66717f; }
:global(html.dark) .model-order-actions button { color: #a8b3c2; }
:global(html.dark) .model-order-actions button:hover:not(:disabled) { color: #e4e8ef; background: #475261; }
</style>
