<script setup>
import { computed, onBeforeUnmount, ref } from 'vue';
import { EdgeLabelRenderer } from '@vue-flow/core';
import { edgeGeometry } from './meshGraph.js';

// A link drawn between the borders of two nodes (not fixed handles), bent when
// several links join the same two nodes. The label is a button that selects it.
// A quiet link (not in use) is thin and faint with no label; pointing at it shows it as usual.
const props = defineProps({id: {type: String, required: true}, sourceNode: {type: Object, required: true}, targetNode: {type: Object, required: true},
  data: {type: Object, default: () => ({})}});
defineOptions({inheritAttrs: false});
function box(node) {
  const {x, y} = node.computedPosition || node.position || {x: 0, y: 0};
  const w = node.dimensions?.width || 0, h = node.dimensions?.height || 0;
  return {w, h, cx: x + w / 2, cy: y + h / 2};
}
const geo = computed(() => {
  const a = box(props.sourceNode), b = box(props.targetNode);
  if (!a.w || !b.w) return null;
  const {s, c, t, label} = edgeGeometry(a, b, props.data.offset, props.data.labelAt ?? 0.5);
  const path = `M ${s.x} ${s.y} Q ${c.x} ${c.y} ${t.x} ${t.y}`;
  const arrow = (tip, from) => {
    const ux = tip.x - from.x, uy = tip.y - from.y, l = Math.hypot(ux, uy) || 1, nx = ux / l, ny = uy / l;
    return `M ${tip.x} ${tip.y} L ${tip.x - nx * 8 - ny * 4} ${tip.y - ny * 8 + nx * 4} L ${tip.x - nx * 8 + ny * 4} ${tip.y - ny * 8 - nx * 4} Z`;
  };
  return {path, label,
    end: props.data.arrow === 'end' || props.data.arrow === 'both' ? arrow(t, c) : '', start: props.data.arrow === 'both' ? arrow(s, c) : ''};
});
// Pointing at the line or its label; leaving waits a moment, so moving from the line onto its label keeps it up.
const hover = ref(false);
let leaveTimer;
function enter() { clearTimeout(leaveTimer); hover.value = true; }
function leave() { clearTimeout(leaveTimer); leaveTimer = setTimeout(() => { hover.value = false; }, 150); }
onBeforeUnmount(() => clearTimeout(leaveTimer));
const quiet = computed(() => Boolean(props.data.quiet) && !hover.value);
</script>
<template>
  <g v-if="geo" class="mesh-edge" :data-kind="data.kind" :data-look="data.look || undefined" :data-live="data.live || undefined" :data-selected="data.selected || undefined" :data-dim="data.dim || undefined"
    :data-path="data.onPath || undefined" :data-quiet="quiet || undefined" @mouseenter="enter" @mouseleave="leave">
    <path class="mesh-edge-hit" :d="geo.path" />
    <path class="mesh-edge-line" :d="geo.path" />
    <path v-if="geo.end" class="mesh-edge-arrow" :d="geo.end" />
    <path v-if="geo.start" class="mesh-edge-arrow" :d="geo.start" />
  </g>
  <EdgeLabelRenderer v-if="geo && data.label && !data.labelHidden && !quiet">
    <button type="button" class="mesh-edge-label nodrag nopan" :data-kind="data.kind" :data-look="data.look || undefined" :data-live="data.live || undefined" :data-selected="data.selected || undefined" :data-dim="data.dim || undefined" :data-flash="data.flash || undefined"
      :data-path="data.onPath || undefined" :data-edge="id"
      :style="{transform: `translate(-50%, -50%) translate(${geo.label.x}px, ${geo.label.y}px)`}" :aria-label="data.aria || data.label" :title="data.title || ''" @click.stop="data.select?.()"
      @mouseenter="enter" @mouseleave="leave">{{ data.label }}</button>
  </EdgeLabelRenderer>
</template>
