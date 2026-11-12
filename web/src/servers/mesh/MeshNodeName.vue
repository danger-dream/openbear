<script setup>
import { computed, inject } from 'vue';
import { flag } from '../serverHelpers.js';
import { mesh } from './meshStore.js';
import { MESH_ITEMS, MESH_OPEN_NODE, nameOf } from './meshHelpers.js';

// A node by name with its flag; a click opens its page. plain: shown only, where leaving is not wanted (the join wizard).
// Inside a clickable row the click opens the node, not the row. stacked: the flag above the name (the matrix's heads).
const props = defineProps({id: {type: String, required: true}, name: {type: String, default: ''}, plain: Boolean, stacked: Boolean});
const open = inject(MESH_OPEN_NODE, null);
const items = inject(MESH_ITEMS, computed(() => []));
const region = computed(() => String((mesh.work?.members || []).find(m => m.serverId === props.id)?.region || items.value.find(i => i.id === props.id)?.region || '').toUpperCase());
const label = computed(() => props.name || nameOf(mesh.work?.members || [], props.id));
const link = computed(() => Boolean(open) && !props.plain);
// A link takes its click and keys from the row around it; plain, they reach the control it sits in.
function click(e) { if (link.value) { e.stopPropagation(); open(props.id); } }
function key(e) { if (link.value && (e.key === 'Enter' || e.key === ' ')) e.stopPropagation(); }
</script>
<template>
  <component :is="link ? 'button' : 'span'" class="m3-node-name" v-bind="link ? {type: 'button', title: `打开 ${label}`} : {}" @click="click" @keydown="key"><span v-if="flag(region)" class="sv-flag" :class="{'m3-mx-flag': stacked}"
    aria-hidden="true">{{ flag(region) }}</span><span v-if="stacked" class="m3-mx-name">{{ label }}</span><template v-else>{{ label }}</template></component>
</template>
