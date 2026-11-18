<script setup>
import { msText } from './meshHelpers.js';
import MeshNodeName from './MeshNodeName.vue';

// One pair's own routes (自建路线) in their order, each its nodes with their flags, its state and round trip:
// on a node's page and on the lines page alike. routes: [{p, nodes, state, rtt}]; self: the node whose page
// shows it, named without a link. Slots: actions by the pair's names, route ({route, index}) after a route.
defineProps({a: {type: String, required: true}, b: {type: String, required: true}, routes: {type: Array, required: true}, self: {type: String, default: ''}});
</script>
<template>
  <section class="m3-rt-pair">
    <div class="m3-rt-pair-h">
      <span><MeshNodeName :id="a" :plain="a === self" /> ⇄ <MeshNodeName :id="b" :plain="b === self" /></span>
      <span class="ob-grow" />
      <slot name="actions" />
    </div>
    <ol class="m3-rt-list">
      <li v-for="(r, i) in routes" :key="r.p.id" :data-off="r.p.on === false || undefined">
        <span class="m3-no">{{ i + 1 }}</span>
        <span class="m3-rt-chain"><template v-for="(n, k) in r.nodes" :key="n"><span v-if="k" class="m3-faint"> → </span><MeshNodeName :id="n" :plain="n === self" /></template></span>
        <span class="m3-rt-state" :data-tone="r.state.tone">{{ r.state.text }}<template v-if="r.rtt != null && r.state.tone !== 'bad'"> · {{ msText(r.rtt) }}</template></span>
        <slot name="route" :route="r" :index="i" />
      </li>
    </ol>
    <p class="m3-hint">{{ routes.length > 1 ? `按顺序用第一条通的；这 ${routes.length} 条都不通时` : '不通时' }}走自动选路，回程原路返回。</p>
  </section>
</template>
