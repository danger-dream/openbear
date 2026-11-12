<script setup>
import MeshNodeName from './MeshNodeName.vue';

// The node pairs an automatic link makes faster: from where to where, the round trip without it and with it.
// pairs: [{src, dst, before, after}] the most first; count: all of them, when more than are listed.
const props = defineProps({pairs: {type: Array, default: () => []}, count: {type: Number, default: 0}});
const ms = v => `${Math.round(v)}`;
</script>
<template>
  <div class="m3-gains">
    <ul>
      <li v-for="g in pairs" :key="`${g.src}|${g.dst}`">
        <span class="m3-gains-who"><MeshNodeName :id="g.src" /><span class="m3-faint"> → </span><MeshNodeName :id="g.dst" /></span>
        <span class="m3-gains-ms"><s>{{ ms(g.before) }}</s> → <b>{{ ms(g.after) }}</b> ms</span>
        <span class="m3-gains-cut">−{{ ms(g.before - g.after) }} ms</span>
      </li>
    </ul>
    <small v-if="props.count > pairs.length" class="m3-faint">另有 {{ props.count - pairs.length }} 对变快得少一些，没有列出</small>
  </div>
</template>
