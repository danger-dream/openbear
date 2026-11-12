<script setup>
import { computed } from 'vue';
import { sideParts } from './meshHelpers.js';
import MeshNodeName from './MeshNodeName.vue';

// One side of an access rule as names: nodes as MeshNodeName (flag, opens its page), networks in mono; more than
// three shortened as sideText does ("A、B 等 5 台").
const props = defineProps({side: {type: Object, default: null}, members: {type: Array, default: () => []}, nets: {type: String, default: 'prefixes'}});
const parts = computed(() => sideParts(props.side, props.members, props.nets));
const shown = computed(() => parts.value.length > 3 ? parts.value.slice(0, 2) : parts.value);
const more = computed(() => parts.value.length > 3 ? ` 等 ${parts.value.length} ${parts.value.every(p => p.kind === 'members') ? '台' : '项'}` : '');
</script>
<template>
  <span class="m3-side-names"><template v-if="parts.length"><template v-for="(p, i) in shown" :key="`${p.kind}:${p.value}`">{{ i ? '、' : '' }}<MeshNodeName
    v-if="p.kind === 'members'" :id="p.value" :name="p.text" /><span v-else :class="{mono: p.kind === 'prefixes' || p.kind === 'subnets'}">{{ p.text }}</span></template>{{ more }}</template><template v-else>（未选择）</template></span>
</template>
