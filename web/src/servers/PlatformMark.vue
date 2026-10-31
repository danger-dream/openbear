<script setup>
import { computed } from 'vue';
import { platformOf } from './serverHelpers.js';

// Marks the servers that are not Linux, which is most of them; the title
// carries the full release name.
const props = defineProps({item: {type: Object, required: true}});
const platform = computed(() => platformOf(props.item));
const label = computed(() => props.item.node?.info?.os || props.item.ssh?.os || {windows: 'Windows', darwin: 'macOS'}[platform.value] || '');
</script>
<template>
  <span v-if="platform === 'windows' || platform === 'darwin'" class="sv-platform" :data-platform="platform" :title="label" role="img" :aria-label="label">
    <svg v-if="platform === 'windows'" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5.6 10.4 4.6v7H3zM11.4 4.4 21 3v8.6h-9.6zM3 12.6h7.4v7L3 18.5zM11.4 12.6H21V21l-9.6-1.4z" /></svg>
    <svg v-else viewBox="0 0 24 24" aria-hidden="true"><path d="M16.4 12.7c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9-.7 0-1.8-.8-3-.8-1.5 0-3 .9-3.8 2.3-1.6 2.8-.4 7 1.2 9.3.8 1.1 1.7 2.4 2.9 2.3 1.2 0 1.6-.7 3-.7s1.8.7 3 .7c1.3 0 2.1-1.1 2.8-2.3.9-1.3 1.3-2.6 1.3-2.7 0 0-2.5-1-2.5-3.7zM14.1 5.8c.6-.8 1.1-1.8.9-2.8-.9 0-2 .6-2.6 1.4-.6.7-1.1 1.7-1 2.7 1 .1 2-.5 2.7-1.3z" /></svg>
  </span>
</template>
