<script setup>
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue';
import { bgpInfo, bgpLayout, bgpTree, pathThrough } from './bgpLayout.js';
import BgpTreeItem from './BgpTreeItem.vue';

// The graph at the width it is given, scaled down to fit whole without scrolling and centred when it is
// narrower. A box under the pointer, or tapped, lights every path through it; the line under the graph
// then says where its lines come from and how many RIS peers each carries (the slot's words otherwise).
// On a phone the same network is a tree, read top to bottom, with the same line under it.
const props = defineProps({data: {type: Object, required: true}, phone: Boolean});
const id = useId();
const box = ref(null), width = ref(0), hover = ref(null), pinned = ref(null);
const layout = computed(() => bgpLayout(props.data));
const tree = computed(() => props.phone ? bgpTree(props.data) : []);
const scale = computed(() => width.value ? Math.min(1, width.value / layout.value.width) : 1);
const focus = computed(() => pinned.value ?? hover.value);
const lit = computed(() => focus.value != null && !props.phone ? pathThrough(layout.value.lines, String(focus.value)) : null);
const more = computed(() => layout.value.nodes.find(n => n.key === focus.value) || null);
const info = computed(() => {
  if (focus.value == null) return null;
  if (more.value?.more) return {title: more.value.name, rows: more.value.more.map(m => `AS${m.asn}${m.name ? ` ${m.name}` : ''}`)};
  return {...bgpInfo(props.data, Number(focus.value)), asn: Number(focus.value)};
});
const rect = r => ({left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px`});
const lineClass = l => lit.value ? (lit.value.lines.has(l) ? 'is-lit' : 'is-dim') : '';
const nodeClass = n => lit.value ? (lit.value.keys.has(n.key) ? 'is-lit' : 'is-dim') : '';
const keyOf = n => n.asn ?? n.key;
function pick(key) { pinned.value = pinned.value === key ? null : key; }
let observer = null;
watch(box, el => {
  observer?.disconnect();
  observer = null;
  width.value = el?.getBoundingClientRect().width || 0;
  if (!el || typeof ResizeObserver === 'undefined') return;
  observer = new ResizeObserver(([entry]) => { width.value = entry.contentRect.width; });
  observer.observe(el);
}, {flush: 'post'});
onBeforeUnmount(() => observer?.disconnect());
</script>
<template>
  <div class="bg-wrap">
    <ul v-if="phone" class="bg-tree is-root" aria-label="上游">
      <li v-for="item in tree" :key="item.asn"><BgpTreeItem :item="item" :focus="focus" @pick="pick" /></li>
    </ul>
    <div v-else ref="box" class="bg-graph" :style="{height: `${Math.ceil(layout.height * scale)}px`}">
      <div class="bg-canvas" :style="{width: `${layout.width}px`, height: `${layout.height}px`, left: `${Math.max(0, (width - layout.width * scale) / 2)}px`,
        transform: scale < 1 ? `scale(${scale})` : null}" @pointerleave="hover = null">
        <div v-if="layout.origin" class="bg-cluster is-origin" :style="rect(layout.origin)"><span>源</span></div>
        <div v-if="layout.band" class="bg-cluster is-band" :style="rect(layout.band)"><span>Tier 1</span></div>
        <svg class="bg-lines" :width="layout.width" :height="layout.height" :viewBox="`0 0 ${layout.width} ${layout.height}`" aria-hidden="true">
          <defs>
            <marker :id="`${id}-a`" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" class="bg-head" /></marker>
            <marker :id="`${id}-b`" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" class="bg-head is-lit" /></marker>
          </defs>
          <path v-for="l in layout.lines" :key="`${l.from}>${l.to}`" :d="l.d" :class="lineClass(l)" :marker-end="`url(#${id}-${lineClass(l) === 'is-lit' ? 'b' : 'a'})`" />
        </svg>
        <div v-for="n in layout.nodes" :key="n.key" class="bg-node" :class="nodeClass(n)" :data-kind="n.kind" :data-asn="n.asn || null" :style="rect(n)"
          tabindex="0" role="button" :aria-label="n.asn ? `AS${n.asn} ${n.name}` : n.name" :aria-pressed="pinned === keyOf(n)"
          @pointerenter="hover = keyOf(n)" @focus="hover = keyOf(n)" @blur="hover = null" @click="pick(keyOf(n))" @keydown.enter.prevent="pick(keyOf(n))">
          <a v-if="n.asn" class="bg-asn" :href="`https://bgp.tools/as/${n.asn}`" target="_blank" rel="noopener noreferrer" title="在 bgp.tools 打开" @click.stop>AS{{ n.asn }}</a>
          <span class="bg-name">{{ n.name }}</span>
        </div>
      </div>
    </div>
    <p class="bg-foot" :class="{'is-info': info}" role="status">
      <template v-if="info">
        <strong>{{ info.title }}</strong><span v-for="row in info.rows" :key="row">{{ row }}</span>
        <a v-if="info.asn" :href="`https://bgp.tools/as/${info.asn}`" target="_blank" rel="noopener noreferrer">bgp.tools ↗</a>
      </template>
      <slot v-else />
    </p>
  </div>
</template>
