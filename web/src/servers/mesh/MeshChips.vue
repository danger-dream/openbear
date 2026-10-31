<script setup>
import { computed, nextTick, ref, useId, watch } from 'vue';
import { Close } from '@element-plus/icons-vue';
import FloatingPanel from '../../references/FloatingPanel.vue';
import '../SuggestInput.vue';
import { noAutofill } from '../serverHelpers.js';

// A short list of words or networks, typed like element-plus's InputTag: Enter, a comma or leaving the
// field turns the text into a chip; ✕ removes one, Backspace in the empty field the last one. A value that
// fails `validate` stays in the field with the reason under it; with `live`, `validate(value, {typing: true})`
// is also asked while typing, for what is already wrong. `suggestions` (strings or {value, note}) drop down as typed.
// ElInputTag itself has neither per-value checks nor suggestions, and drops what was typed on a bad value.
const props = defineProps({modelValue: {type: Array, default: () => []}, label: {type: String, required: true}, placeholder: {type: String, default: '添加'},
  validate: {type: Function, default: null}, mono: Boolean, max: {type: Number, default: 32}, maxlength: {type: Number, default: 128},
  suggestions: {type: Array, default: () => []}, inputmode: {type: String, default: 'text'}, live: Boolean});
const emit = defineEmits(['update:modelValue', 'change']);
const draft = ref(''), problem = ref(''), input = ref(null), open = ref(false), active = ref(-1), width = ref(240);
const listId = `mesh-chips-${useId()}`;
const SPLIT = /[,，、\n]/;

const options = computed(() => {
  const seen = new Set(props.modelValue);
  return props.suggestions.map(o => typeof o === 'string' ? {value: o, note: ''} : o?.value ? {value: String(o.value), note: String(o.note || '')} : null)
    .filter(o => o && !seen.has(o.value) && seen.add(o.value));
});
const matches = computed(() => {
  const q = draft.value.trim().toLowerCase();
  return q ? options.value.filter(o => o.value.toLowerCase().includes(q) || o.note.toLowerCase().includes(q)) : options.value;
});
const shown = computed(() => open.value && matches.value.length > 0 && props.modelValue.length < props.max);
watch(matches, list => { if (active.value >= list.length) active.value = -1; });

function set(list) { emit('update:modelValue', list); emit('change', list); }
function problemOf(value, list) {
  if (list.includes(value)) return '';
  return props.validate?.(value) || (list.length >= props.max ? `最多 ${props.max} 项` : '');
}
// Adds what is typed (or `values`, then `rest` stays typed); the first value that fails stays in the field.
function commit(values = [draft.value], rest = '') {
  let list = [...props.modelValue], error = '', left = [];
  for (const [i, raw] of values.entries()) {
    const value = raw.trim();
    if (!value) continue;
    error = problemOf(value, list);
    if (error) { left = values.slice(i).map(v => v.trim()).filter(Boolean); break; }
    if (!list.includes(value)) list = [...list, value];
  }
  problem.value = error;
  draft.value = [...left, rest].filter(Boolean).join(', ') || rest;
  if (list.length !== props.modelValue.length) set(list);
  return !error;
}
function typed(event) {
  const text = event.target.value;
  if (SPLIT.test(text)) {
    const parts = text.split(SPLIT), rest = parts.pop();
    // Vue keeps the DOM value when the bound value does not change; set it back by hand.
    commit(parts, rest.trimStart());
    event.target.value = draft.value;
    return;
  }
  draft.value = text;
  problem.value = props.live ? props.validate?.(text.trim(), {typing: true}) || '' : '';
  open.value = true; active.value = -1;
}
function pick(value) {
  if (commit([value])) close();
  nextTick(() => input.value?.focus());
}
function remove(value) { set(props.modelValue.filter(v => v !== value)); }
// At least wide enough for a network and its note.
function show() { width.value = Math.max(input.value?.closest('.sv-chips-edit')?.offsetWidth || 240, 280); open.value = true; }
function close() { open.value = false; active.value = -1; }
function key(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!options.value.length) return;
    event.preventDefault();
    if (!shown.value) { show(); return; }
    const n = matches.value.length, step = event.key === 'ArrowDown' ? 1 : -1;
    active.value = active.value < 0 ? (step > 0 ? 0 : n - 1) : (active.value + step + n) % n;
  } else if (event.key === 'Enter') {
    event.preventDefault();
    if (shown.value && active.value >= 0) pick(matches.value[active.value].value);
    else commit();
  } else if (event.key === 'Backspace' && !draft.value && props.modelValue.length) {
    event.preventDefault();
    set(props.modelValue.slice(0, -1));
  } else if (event.key === 'Escape' && shown.value) {
    // Only the list closes, not the panel around it.
    event.preventDefault(); event.stopPropagation();
    close();
  } else if (event.key === 'Tab') close();
}
function blur() { close(); commit(); }
</script>
<template>
  <div class="ob-ctl mesh-chips">
    <div class="sv-chips-edit" @click.self="input?.focus()">
      <span v-for="value in modelValue" :key="value" class="sv-chip" :class="{mono}">{{ value }}<button type="button" :aria-label="`删除 ${value}`" @click="remove(value)"><el-icon><Close /></el-icon></button></span>
      <input ref="input" v-bind="noAutofill" :value="draft" :class="{mono, invalid: problem}" :placeholder="modelValue.length >= max ? '' : placeholder" :aria-label="label" :maxlength="maxlength"
        :inputmode="inputmode" :role="suggestions.length ? 'combobox' : undefined" autocomplete="off" :aria-autocomplete="suggestions.length ? 'list' : undefined" :aria-expanded="suggestions.length ? shown : undefined" :aria-controls="suggestions.length ? listId : undefined"
        :aria-activedescendant="shown && active >= 0 ? `${listId}-${active}` : undefined" :aria-invalid="Boolean(problem) || undefined"
        @input="typed" @focus="show" @click="show" @keydown="key" @blur="blur" />
    </div>
    <small v-if="problem" class="ob-bad" role="alert">{{ problem }}</small>
    <FloatingPanel :open="shown" :anchor="input" placement="bottom-start" :width="width" :label="`${label}：候选`" @close="close">
      <ul :id="listId" class="ob-suggest-list" role="listbox" :aria-label="`${label}：候选`">
        <li v-for="(option, i) in matches" :id="`${listId}-${i}`" :key="option.value" role="option" :aria-selected="i === active" :class="{'is-active': i === active}"
          @pointerdown.prevent="pick(option.value)" @pointerenter="active = i"><template v-if="option.note"><span class="ob-suggest-value">{{ option.value }}</span><small class="ob-suggest-note">{{ option.note }}</small></template><template v-else>{{ option.value }}</template></li>
      </ul>
    </FloatingPanel>
  </div>
</template>
