<script setup>
import { computed, nextTick, ref, useId } from 'vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import { noAutofill } from './serverHelpers.js';

// A text input that offers existing values; the browser's datalist popup
// cannot be styled to match the rest of the console. An option is a string, or
// {value, note} where the note (say, the server an address belongs to) is shown and matched too.
const props = defineProps({modelValue: {type: String, default: ''}, options: {type: Array, default: () => []}});
const emit = defineEmits(['update:modelValue']);
defineOptions({inheritAttrs: false});
const input = ref(null), open = ref(false), active = ref(-1), width = ref(240);
const listId = `ob-suggest-${useId()}`;

const items = computed(() => {
  const seen = new Set();
  return props.options.map(o => typeof o === 'string' ? {value: o, note: ''} : o?.value ? {value: String(o.value), note: String(o.note || '')} : null)
    .filter(o => o && !seen.has(o.value) && seen.add(o.value));
});
const matches = computed(() => {
  const q = props.modelValue.trim().toLowerCase();
  const all = items.value;
  // An exact match still lists every option, so another one can be picked.
  return !q || all.some(o => o.value.toLowerCase() === q) ? all : all.filter(o => o.value.toLowerCase().includes(q) || o.note.toLowerCase().includes(q));
});
const shown = computed(() => open.value && matches.value.length > 0);

function show() {
  if (!props.options.length) return;
  width.value = input.value?.offsetWidth || 240;
  open.value = true;
  active.value = matches.value.findIndex(o => o.value === props.modelValue);
}
function close() { open.value = false; active.value = -1; }
function pick(value) {
  emit('update:modelValue', value);
  close();
  nextTick(() => input.value?.focus());
}
function typed(event) {
  emit('update:modelValue', event.target.value);
  if (!open.value) show();
  active.value = -1;
}
function key(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!shown.value) { show(); return; }
    const n = matches.value.length, step = event.key === 'ArrowDown' ? 1 : -1;
    active.value = active.value < 0 ? (step > 0 ? 0 : n - 1) : (active.value + step + n) % n;
  } else if (event.key === 'Enter' && shown.value && active.value >= 0) {
    event.preventDefault();
    pick(matches.value[active.value].value);
  } else if (event.key === 'Escape' && shown.value) {
    // Only the list closes, not the dialog around it.
    event.preventDefault();
    event.stopPropagation();
    close();
  } else if (event.key === 'Tab') close();
}
</script>
<template>
  <input ref="input" v-bind="{...$attrs, ...noAutofill}" class="ob-suggest-input" :value="modelValue" role="combobox" autocomplete="off"
    aria-autocomplete="list" :aria-expanded="shown" :aria-controls="listId" :aria-activedescendant="shown && active >= 0 ? `${listId}-${active}` : undefined"
    @input="typed" @click="shown ? close() : show()" @keydown="key" @blur="close" />
  <FloatingPanel :open="shown" :anchor="input" placement="bottom-start" :width="width" label="候选项" @close="close">
    <ul :id="listId" class="ob-suggest-list" role="listbox">
      <li v-for="(option, i) in matches" :id="`${listId}-${i}`" :key="option.value" role="option" :aria-selected="option.value === modelValue"
        :class="{'is-active': i === active}" @pointerdown.prevent="pick(option.value)" @pointerenter="active = i"><template v-if="option.note"><span class="ob-suggest-value">{{ option.value }}</span><small class="ob-suggest-note">{{ option.note }}</small></template><template v-else>{{ option.value }}</template></li>
    </ul>
  </FloatingPanel>
</template>
<style>
.ob-suggest-list { margin: 0; padding: 4px; overflow-y: auto; list-style: none; }
.ob-suggest-list > li { display: flex; align-items: center; min-height: 30px; padding: 4px 10px; overflow: hidden; border-radius: 6px; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.ob-suggest-list > li.is-active { background: var(--ob-chat-hover); }
.ob-suggest-list > li[aria-selected='true'] { font-weight: 600; }
.ob-suggest-list .ob-suggest-value { flex: none; font-family: var(--ix-mono, ui-monospace, monospace); }
.ob-suggest-list .ob-suggest-note { flex: 1 1 auto; min-width: 0; margin-left: 10px; overflow: hidden; color: var(--ob-chat-subtle); font-size: 12px; font-weight: 400; text-align: right; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 760px) {
  .ob-suggest-list > li { min-height: 40px; font-size: 15px; }
}
</style>
