<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ChevronRight, ListChecks} from "@lucide/vue";
import WorkItemStatusIcon from "./WorkItemStatusIcon.vue";
import {STATUS_LABELS, useWorkItems} from "./workItems.js";

// The running turn's scope, kept in the composer so new transcript output never pushes it away.
const store = useWorkItems();
const open = ref(false);
const root = ref(null);
const list = ref(null);
let touchStartY = null;

const focus = computed(() => store?.state.running && store.state.focus?.numbers?.length ? store.state.focus : null);
const items = computed(() => focus.value ? store.focusItems() : []);
const done = computed(() => items.value.filter(item => item.status === "done").length);
const working = computed(() => store?.currentItems() || []);
const current = computed(() => working.value[0] || null);
const label = computed(() => (focus.value?.numbers || []).map(number => `#${number} ${store.find(number)?.title || ""}`.trim()).join("、"));
const percent = computed(() => items.value.length ? Math.round(done.value / items.value.length * 100) : 0);

watch(focus, value => { if (!value) open.value = false; });

function openItem(number) {
	store?.open(number);
}
// Folds back once attention moves elsewhere: a tap or click outside, focus
// leaving it (the editor taking the keyboard on a phone), or Escape.
function outside(target) {
	return Boolean(target) && !root.value?.contains(target);
}
function onPointerDown(event) {
	if (open.value && outside(event.target)) open.value = false;
}
function onFocusIn(event) {
	if (open.value && outside(event.target)) open.value = false;
}
function onKeydown(event) {
	if (open.value && event.key === "Escape") open.value = false;
}
// Pulling the open list down from its top folds it, as with the bottom sheets.
function onTouchStart(event) {
	touchStartY = (list.value?.scrollTop || 0) <= 0 ? event.touches[0]?.clientY ?? null : null;
}
function onTouchMove(event) {
	if (touchStartY === null) return;
	if ((event.touches[0]?.clientY ?? touchStartY) - touchStartY > 48) {
		touchStartY = null;
		open.value = false;
	}
}
function onTouchEnd() {
	touchStartY = null;
}
onMounted(() => {
	document.addEventListener("pointerdown", onPointerDown, true);
	document.addEventListener("focusin", onFocusIn);
	document.addEventListener("keydown", onKeydown);
});
onBeforeUnmount(() => {
	document.removeEventListener("pointerdown", onPointerDown, true);
	document.removeEventListener("focusin", onFocusIn);
	document.removeEventListener("keydown", onKeydown);
});
</script>

<template>
	<div v-if="focus" ref="root" class="wi-dock" :class="{open}">
		<div v-if="open" ref="list" class="wi-dock-list" role="list" aria-label="本轮执行范围"
		     @touchstart.passive="onTouchStart" @touchmove.passive="onTouchMove" @touchend="onTouchEnd" @touchcancel="onTouchEnd">
			<div class="wi-dock-cap">本轮执行范围 · {{ label }}</div>
			<div v-for="item in items" :key="item.number" role="listitem" class="wi-dock-row"
			     :class="{'is-current': store.isCurrent(item.number), 'is-done': item.status === 'done'}"
			     tabindex="0" @click="openItem(item.number)" @keydown.enter.prevent="openItem(item.number)">
				<WorkItemStatusIcon :status="store.isCurrent(item.number) ? 'doing' : item.status" :spinning="store.isCurrent(item.number)"/>
				<span class="wi-dock-num">#{{ item.number }}</span>
				<span class="wi-dock-tx">{{ item.title }}</span>
				<span v-if="store.isCurrent(item.number)" class="wi-dock-now"><i class="wi-dock-dot"></i>正在做</span>
				<span v-else-if="['ask', 'blocked'].includes(item.status)" class="wi-dock-attn" :class="`tone-${item.status}`">{{ STATUS_LABELS[item.status] }}</span>
			</div>
		</div>
		<div class="wi-dock-row-main">
			<button type="button" class="wi-dock-main" :aria-expanded="open" :aria-label="open ? '收起本轮任务进度' : '展开本轮任务进度'" @click="open = !open">
				<ListChecks class="wi-dock-icon" :stroke-width="1.7" aria-hidden="true"/>
				<b>{{ label }}</b>
				<span class="wi-dock-cnt">{{ done }}/{{ items.length }}</span>
				<span class="wi-dock-bar" aria-hidden="true"><i :style="{width: `${percent}%`}"></i></span>
				<span class="wi-dock-sep" aria-hidden="true">·</span>
				<span class="wi-dock-now"><i class="wi-dock-dot"></i>{{ current ? '正在做' : '执行中' }}</span>
				<span class="wi-dock-cur"><template v-if="current">#{{ current.number }} {{ current.title }}</template></span>
				<span v-if="working.length > 1" class="wi-dock-more">等 {{ working.length }} 项</span>
				<ChevronRight class="wi-dock-chev" :stroke-width="1.8" aria-hidden="true"/>
			</button>
			<button type="button" class="wi-dock-open" @click="store.open(current?.number || '')">打开任务</button>
		</div>
	</div>
</template>

<style scoped>
.wi-dock { margin: -2px -2px 6px; padding: 0 0 4px; border-bottom: 1px solid var(--ob-chat-line); }
.wi-dock-list { max-height: 212px; overflow: auto; overscroll-behavior: contain; padding: 2px 0 6px; }
.wi-dock-cap { padding: 2px 6px 6px; overflow: hidden; font-size: 12px; color: var(--ob-chat-subtle); text-overflow: ellipsis; white-space: nowrap; }
.wi-dock-row { display: flex; align-items: flex-start; gap: .45rem; min-height: 32px; padding: .3rem .5rem; font-size: 12.5px; line-height: 1.55; color: var(--ob-chat-text); cursor: pointer; }
.wi-dock-row:hover { background: var(--ob-chat-hover); }
.wi-dock-row:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: -2px; }
.wi-dock-num { flex: none; color: var(--ob-chat-muted); font-size: 12px; font-variant-numeric: tabular-nums; line-height: 20px; }
.wi-dock-tx { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.wi-dock-row.is-done .wi-dock-tx { color: var(--ob-text-muted); }
.wi-dock-row.is-done .wi-dock-num, .wi-dock-row.is-done :deep(.wi-status-icon) { color: var(--ob-text-disabled); }
.wi-dock-row.is-current .wi-dock-num, .wi-dock-row.is-current :deep(.wi-status-icon) { color: var(--ob-success); }
.wi-dock-row.is-current .wi-dock-tx { color: var(--ob-success); font-weight: 600; }
.wi-dock-attn { flex: none; font-size: 12px; line-height: 20px; color: var(--ob-chat-subtle); }
.wi-dock-attn.tone-ask { color: var(--ob-orange); }
.wi-dock-attn.tone-blocked { color: var(--ob-danger); }
.wi-dock-row-main { display: flex; align-items: center; gap: 6px; min-height: 36px; }
.wi-dock-main { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; min-height: 36px; padding: 0 6px; border: 0; background: transparent; color: var(--ob-chat-text); font: inherit; font-size: 12.5px; text-align: left; cursor: pointer; }
.wi-dock-icon { width: 15px; height: 15px; flex: none; color: var(--ob-chat-subtle); }
.wi-dock-main b { flex: none; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.wi-dock-cnt { flex: none; color: var(--ob-chat-subtle); font-variant-numeric: tabular-nums; }
.wi-dock-bar { flex: none; width: 56px; height: 4px; border-radius: 2px; background: var(--ob-surface-soft); overflow: hidden; }
.wi-dock-bar i { display: block; height: 100%; background: var(--ob-text-subtle); border-radius: 2px; }
.wi-dock-sep { flex: none; color: var(--ob-chat-muted); }
.wi-dock-now { flex: none; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; line-height: 20px; color: var(--ob-success); white-space: nowrap; }
.wi-dock-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: wi-dock-pulse 1.4s ease-in-out infinite; }
.wi-dock-cur { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-success); }
.wi-dock-more { flex: none; color: var(--ob-success); white-space: nowrap; }
.wi-dock-chev { width: 14px; height: 14px; flex: none; color: var(--ob-chat-subtle); transform: rotate(-90deg); transition: transform .15s; }
.wi-dock.open .wi-dock-chev { transform: rotate(90deg); }
.wi-dock-main:hover .wi-dock-chev { color: var(--ob-chat-text); }
.wi-dock-open { flex: none; height: 30px; padding: 0 6px; border: 0; background: transparent; color: var(--ob-blue); font: inherit; font-size: 12px; cursor: pointer; }
button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 1px; }
@keyframes wi-dock-pulse { 50% { opacity: .35; } }
@media (max-width: 760px) {
	.wi-dock-bar, .wi-dock-sep { display: none; }
	.wi-dock:not(.open) .wi-dock-open { display: none; }
	.wi-dock-open { height: 36px; }
	.wi-dock-row { min-height: 40px; }
	.wi-dock-main b { max-width: 45%; }
}
@media (prefers-reduced-motion: reduce) { .wi-dock-dot { animation: none; } .wi-dock-chev { transition: none; } }
</style>
