<script setup>
import { ref } from 'vue';
import { Info } from '@lucide/vue';

// The ⓘ that explains a setting, in a tooltip over the panel so nothing moves when it opens:
// hovered or focused with a mouse; on a touch screen tapped open, and closed by another tap on it or anywhere else.
// Enter or Space toggles it from the keyboard either way.
defineProps({text: {type: String, required: true}, label: {type: String, required: true}});
const hover = typeof window !== 'undefined' && Boolean(window.matchMedia?.('(hover: hover) and (pointer: fine)').matches);
const open = ref(false);
// Clear of the screen's edges on a phone.
const POPPER = {modifiers: [{name: 'preventOverflow', options: {padding: 8}}, {name: 'flip', options: {padding: 8}}]};
</script>
<template>
  <el-tooltip :content="text" :trigger="hover ? ['hover', 'focus'] : 'click'" effect="light" placement="top" :show-after="hover ? 120 : 0" :hide-after="0"
    :persistent="false" popper-class="mesh-help-tip" :popper-options="POPPER" @show="open = true" @hide="open = false">
    <button type="button" class="mesh-help-toggle" :data-open="open || undefined" :aria-label="`${label}：说明`"><Info :size="13" aria-hidden="true" /></button>
  </el-tooltip>
</template>
