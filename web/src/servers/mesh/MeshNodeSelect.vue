<script setup>
import { computed } from 'vue';
import { ensureFlagFont, flag } from '../serverHelpers.js';

// Picking a node, the same everywhere: each option with its flag (native selects cannot use the flag font),
// searchable by name. options: [{value, label, region?, note?, group?, disabled?}]; options with a group are
// shown under it, groups in the order they first appear. An action picker (＋ 添加…) keeps modelValue '' and
// acts on change.
const props = defineProps({modelValue: {type: String, default: ''}, options: {type: Array, default: () => []}, placeholder: {type: String, default: '选一个节点…'},
  ariaLabel: {type: String, default: ''}, clearable: Boolean, width: {type: String, default: '240px'}});
const emit = defineEmits(['update:modelValue', 'change']);
ensureFlagFont();
const chosen = computed(() => props.options.find(o => o.value === props.modelValue) || null);
const groups = computed(() => {
  if (!props.options.some(o => o.group)) return null;
  const out = new Map();
  for (const o of props.options) { const key = o.group || ''; if (!out.has(key)) out.set(key, []); out.get(key).push(o); }
  return [...out].map(([label, list]) => ({label, list}));
});
function pick(value) { emit('update:modelValue', value ?? ''); emit('change', value ?? ''); }
</script>
<template>
  <el-select class="m3-node-select" :model-value="modelValue || ''" :placeholder="placeholder" filterable :clearable="clearable" popper-class="m3-tokens m3-node-pop"
    :aria-label="ariaLabel" :style="{width}" @update:model-value="pick">
    <template v-if="chosen" #label><span class="m3-ns"><span v-if="flag(chosen.region)" class="sv-flag" aria-hidden="true">{{ flag(chosen.region) }}</span>{{ chosen.label }}</span></template>
    <template v-if="groups">
      <el-option-group v-for="g in groups" :key="g.label" :label="g.label">
        <el-option v-for="o in g.list" :key="o.value" :value="o.value" :label="o.label" :disabled="o.disabled">
          <span class="m3-ns"><span v-if="flag(o.region)" class="sv-flag" aria-hidden="true">{{ flag(o.region) }}</span>{{ o.label }}<small v-if="o.note" class="m3-faint">{{ o.note }}</small></span>
        </el-option>
      </el-option-group>
    </template>
    <template v-else>
      <el-option v-for="o in options" :key="o.value" :value="o.value" :label="o.label" :disabled="o.disabled">
        <span class="m3-ns"><span v-if="flag(o.region)" class="sv-flag" aria-hidden="true">{{ flag(o.region) }}</span>{{ o.label }}<small v-if="o.note" class="m3-faint">{{ o.note }}</small></span>
      </el-option>
    </template>
  </el-select>
</template>
