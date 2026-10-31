<script setup>
import {computed} from "vue";
import {ChevronRight} from "@lucide/vue";
import WorkItemStatusIcon from "./WorkItemStatusIcon.vue";
import {STATUS_LABELS, leaves, statusOf, useWorkItems} from "./workItems.js";

defineOptions({name: "WorkItemTree"});
const props = defineProps({
	nodes: {type: Array, default: () => []},
	parent: {type: String, default: ""},
	depth: {type: Number, default: 0},
	expanded: {type: Object, required: true},
	droppedOpen: {type: Object, required: true},
	focusRoots: {type: Array, default: () => []},
});
const emit = defineEmits(["open", "toggle", "toggle-dropped"]);
const store = useWorkItems();

const live = computed(() => props.nodes.filter(node => node.status !== "dropped" || node.children?.length));
const dropped = computed(() => props.nodes.filter(node => node.status === "dropped" && !node.children?.length));
// Rows keep a toggle's room only when a sibling has one to align with.
const toggles = computed(() => live.value.some(node => node.children?.length));

function progress(node) {
	const items = leaves(node).filter(leaf => leaf.status !== "dropped");
	return {done: items.filter(leaf => leaf.status === "done").length, total: items.length};
}
function rowStatus(node) {
	if (!node.children?.length) return node.status;
	const {done, total} = progress(node);
	return total && done === total ? "done" : done || leaves(node).some(leaf => leaf.status === "doing") ? "doing" : "todo";
}
function current(node) {
	return Boolean(store?.isCurrent(node.number)) && !node.children?.length && !["done", "dropped"].includes(node.status);
}
function finished(node) {
	return rowStatus(node) === "done";
}
function attention(node) {
	return !node.children?.length && ["ask", "blocked"].includes(node.status) ? STATUS_LABELS[node.status] : "";
}
function indent(depth) {
	return {paddingLeft: `${0.5 + depth * 1.25}rem`};
}
</script>

<template>
	<template v-for="node in live" :key="node.number">
		<div class="wi-row" :class="{'is-current': current(node), 'is-done': finished(node) && !current(node)}" :style="indent(props.depth)"
		     role="button" tabindex="0" :aria-label="`#${node.number} ${node.title}，${current(node) ? '正在做' : STATUS_LABELS[statusOf(node)] || ''}`"
		     @click="emit('open', node.number)" @keydown.enter.prevent="emit('open', node.number)" @keydown.space.prevent="emit('open', node.number)">
			<button v-if="node.children?.length" type="button" class="wi-tog" :class="{open: props.expanded[node.number]}"
			        :aria-label="props.expanded[node.number] ? '收起' : '展开'" :aria-expanded="Boolean(props.expanded[node.number])"
			        @click.stop="emit('toggle', node.number)"><ChevronRight :stroke-width="1.8" aria-hidden="true"/></button>
			<span v-else-if="toggles" class="wi-pad" aria-hidden="true"></span>
			<WorkItemStatusIcon :status="current(node) ? 'doing' : rowStatus(node)" :spinning="current(node)"/>
			<span class="wi-num">#{{ node.number }}</span>
			<span class="wi-tx" :class="{strike: node.status === 'dropped'}">{{ node.title }}</span>
			<span v-if="current(node)" class="wi-now"><i class="wi-dot" aria-hidden="true"></i>正在做</span>
			<span v-if="node.children?.length" class="wi-cnt">{{ progress(node).done }}/{{ progress(node).total }}</span>
			<span v-if="attention(node)" class="wi-attn" :class="`tone-${node.status}`">{{ attention(node) }}</span>
			<i v-if="props.focusRoots.includes(node.number)" class="wi-tag">本轮范围</i>
		</div>
		<WorkItemTree v-if="node.children?.length && props.expanded[node.number]" :nodes="node.children" :parent="node.number"
		              :depth="props.depth + 1" :expanded="props.expanded" :dropped-open="props.droppedOpen" :focus-roots="props.focusRoots"
		              @open="emit('open', $event)" @toggle="emit('toggle', $event)" @toggle-dropped="emit('toggle-dropped', $event)"/>
	</template>
	<template v-if="dropped.length">
		<button type="button" class="wi-agg" :class="{open: props.droppedOpen[props.parent]}" :style="indent(props.depth)"
		        :aria-expanded="Boolean(props.droppedOpen[props.parent])" @click="emit('toggle-dropped', props.parent)">
			<span class="wi-aggc"><ChevronRight :stroke-width="1.8" aria-hidden="true"/></span>按决定不做 {{ dropped.length }} 项
		</button>
		<template v-if="props.droppedOpen[props.parent]">
			<div v-for="node in dropped" :key="node.number" class="wi-row is-done" :style="indent(props.depth)" role="button" tabindex="0"
			     @click="emit('open', node.number)" @keydown.enter.prevent="emit('open', node.number)">
				<span v-if="toggles" class="wi-pad" aria-hidden="true"></span>
				<WorkItemStatusIcon status="dropped"/>
				<span class="wi-num">#{{ node.number }}</span>
				<span class="wi-tx strike">{{ node.title }}</span>
			</div>
		</template>
	</template>
</template>

<style scoped>
.wi-row { display: flex; align-items: flex-start; gap: .45rem; width: 100%; min-height: 34px; padding: .38rem .5rem; border: 0; background: transparent; text-align: left; cursor: pointer; font-size: 13px; line-height: 1.55; color: var(--ob-text); box-sizing: border-box; }
.wi-row:hover { background: var(--ob-surface-soft); }
.wi-row:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: -2px; }
.wi-tog { display: grid; place-items: center; width: 20px; height: 20px; flex: none; padding: 0; border: 0; background: transparent; color: var(--ob-text-muted); cursor: pointer; }
.wi-tog:hover { color: var(--ob-text-strong); }
.wi-tog svg { width: 13px; height: 13px; transition: transform .15s; }
.wi-tog.open svg { transform: rotate(90deg); }
.wi-pad { display: inline-block; width: 20px; flex: none; }
.wi-num { flex: none; color: var(--ob-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; line-height: 20px; }
.wi-tx { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.wi-tx.strike { color: var(--ob-text-muted); text-decoration: line-through; text-decoration-color: var(--ob-text-disabled); }
.wi-cnt { flex: none; font-size: 12px; color: var(--ob-text-muted); font-variant-numeric: tabular-nums; line-height: 20px; }
.wi-tag { flex: none; font-size: 11px; line-height: 20px; color: var(--ob-text-subtle); font-style: normal; white-space: nowrap; }
.wi-attn { flex: none; font-size: 12px; line-height: 20px; white-space: nowrap; color: var(--ob-text-subtle); }
.wi-attn.tone-ask { color: var(--ob-orange); }
.wi-attn.tone-blocked { color: var(--ob-danger); }
.wi-now { flex: none; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; line-height: 20px; color: var(--ob-success); white-space: nowrap; }
.wi-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: wi-pulse 1.4s ease-in-out infinite; }
.wi-row.is-done .wi-tx, .wi-row.is-done .wi-cnt { color: var(--ob-text-muted); }
.wi-row.is-done .wi-num, .wi-row.is-done :deep(.wi-status-icon) { color: var(--ob-text-disabled); }
.wi-row.is-current .wi-num, .wi-row.is-current :deep(.wi-status-icon) { color: var(--ob-success); }
.wi-row.is-current .wi-tx { color: var(--ob-success); font-weight: 600; }
.wi-agg { display: flex; align-items: center; gap: .45rem; width: 100%; min-height: 32px; padding: .3rem .5rem; border: 0; background: transparent; color: var(--ob-text-subtle); font-size: 12px; cursor: pointer; text-align: left; }
.wi-agg:hover { background: var(--ob-surface-soft); }
.wi-aggc { display: inline-grid; place-items: center; width: 20px; height: 20px; flex: none; }
.wi-agg svg { width: 12px; height: 12px; transition: transform .15s; }
.wi-agg.open svg { transform: rotate(90deg); }
@keyframes wi-pulse { 50% { opacity: .35; } }
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.wi-row { min-height: 42px; padding-top: .55rem; padding-bottom: .55rem; }
	.wi-tog { width: 28px; height: 28px; margin: -4px 0; }
	.wi-pad, .wi-aggc { width: 28px; }
}
@media (prefers-reduced-motion: reduce) { .wi-dot { animation: none; } }
</style>
