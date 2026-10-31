<script setup>
import MeshHelp from './MeshHelp.vue';

// One setting in the panel: its name on the left, the control on the right. What the setting
// does is in a tooltip on the ⓘ next to the name (MeshHelp), so the panel shows the values first;
// what needs attention (errors, warnings) goes in the default slot under the control and is always shown.
// stack: the name above the control, for controls that need the whole width.
defineProps({label: {type: String, required: true}, help: {type: String, default: ''}, stack: Boolean});
</script>
<template>
  <div class="mesh-field" :class="{'is-stack': stack}">
    <div class="mesh-field-label">
      <span>{{ label }}</span>
      <MeshHelp v-if="help" :text="help" :label="label" />
    </div>
    <div class="mesh-field-ctl">
      <slot />
    </div>
  </div>
</template>
