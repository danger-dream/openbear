<script setup>
import {computed, nextTick, ref, watch} from "vue";
import {shortText} from "./display.js";
import {useMessageVisibility} from './messageVisibility.js';
const visibility = useMessageVisibility();

const props = defineProps({
	turns: {type: Array, default: () => []},
	activeTurnIndex: {type: Number, default: 0},
	running: {type: Boolean, default: false},
});
const emit = defineEmits(["scroll-to-turn"]);
const rail = ref(null);
const filter = ref("");
const filteredTurns = computed(() => props.turns
	.map((turn, index) => ({turn, index}))
	.filter(({turn, index}) => !filter.value.trim() || `${index + 1} ${visibility.userContent(turn)}`.toLowerCase().includes(filter.value.trim().toLowerCase())));

watch(() => [props.activeTurnIndex, props.turns.length], async () => {
	await nextTick();
	rail.value?.querySelector(".turn-minimap-dot.active")?.scrollIntoView({block: "nearest"});
}, {immediate: true, flush: "post"});

function turnNavLabel(turn, index = 0) {
	const text = shortText(visibility.userContent(turn), 40);
	return text || `第 ${index + 1} 轮对话`;
}
</script>

<template>
	<nav v-if="props.turns.length >= 3" class="turn-minimap" aria-label="对话快速导航">
		<div ref="rail" class="turn-minimap-rail">
			<el-tooltip
				v-for="(turn, idx) in props.turns"
				:key="`rail-${turn.id}`"
				:content="turnNavLabel(turn, idx)"
				placement="left"
				:show-after="260"
			>
				<button
					type="button"
					class="turn-minimap-dot"
					:class="{ active: props.activeTurnIndex === idx, running: turn.isLatest && props.running }"
					:aria-label="turnNavLabel(turn, idx)"
					@click.stop="emit('scroll-to-turn', idx)"
				><span></span></button>
			</el-tooltip>
		</div>
		<div class="turn-minimap-popover">
			<input v-if="props.turns.length > 8" v-model="filter" class="turn-nav-filter" type="search" placeholder="筛选轮次" aria-label="筛选对话轮次"/>
			<div v-if="!filteredTurns.length" class="turn-nav-empty">没有匹配的轮次</div>
			<button
				v-for="{turn, index: idx} in filteredTurns"
				:key="`nav-${turn.id}`"
				type="button"
				class="turn-nav-row"
				:class="{ active: props.activeTurnIndex === idx, running: turn.isLatest && props.running }"
				@click.stop="emit('scroll-to-turn', idx)"
			>
				<span class="turn-nav-index">{{ idx + 1 }}</span>
				<span class="turn-nav-title">{{ turnNavLabel(turn, idx) }}</span>
			</button>
		</div>
	</nav>
</template>

<style scoped>
.turn-minimap {
	position: fixed;
	top: var(--console-float-minimap-top, calc(48% + 2.55rem));
	right: var(--console-float-rail-right, 1rem);
	z-index: 32;
	display: flex;
	align-items: center;
	justify-content: flex-end;
	padding-left: 2.25rem;
	pointer-events: auto;
	transition: right .24s cubic-bezier(.22, 1, .36, 1);
}

.turn-minimap-rail {
	display: grid;
	grid-auto-rows: .42rem;
	gap: .22rem;
	max-height: calc(8 * .42rem + 7 * .22rem + .84rem);
	overflow-y: auto;
	overscroll-behavior: contain;
	scrollbar-width: none;
	border-radius: 0;
	background: transparent;
	padding: .42rem .3rem;
	backdrop-filter: none;
	box-shadow: none;
}

.turn-minimap-rail::-webkit-scrollbar {
	display: none;
}

.turn-minimap-dot {
	display: grid;
	width: 1.2rem;
	height: .42rem;
	place-items: center;
	border: 0;
	background: transparent;
	padding: 0;
	cursor: pointer;
}

.turn-minimap-dot span {
	display: block;
	width: .86rem;
	height: 2px;
	border-radius: 999px;
	background: var(--ob-text-muted);
	transition: width .15s ease, height .15s ease, background .15s ease;
}

.turn-minimap-dot:hover span {
	width: 1.05rem;
	background: var(--ob-text-muted);
}

.turn-minimap-dot.active span {
	width: 1.16rem;
	height: 3px;
	background: var(--ob-text-muted);
}

.turn-minimap-dot.running span {
	background: var(--ob-blue);
	box-shadow: 0 0 0 3px rgb(var(--ob-blue-rgb) / 0.1);
}

.turn-minimap-popover {
	position: absolute;
	top: 50%;
	right: 1.95rem;
	width: 18rem;
	max-height: min(28rem, 68vh);
	overflow-y: auto;
	transform: translate(4px, -50%) scale(.98);
	transform-origin: right center;
	border: 1px solid var(--ob-border);
	border-radius: 1rem;
	background: var(--ob-surface-raised);
	padding: .42rem;
	box-shadow: var(--ob-shadow-popover);
	opacity: 0;
	pointer-events: none;
	backdrop-filter: blur(20px);
	transition: opacity .16s ease, transform .16s ease;
}

.turn-minimap:hover .turn-minimap-popover,
.turn-minimap:focus-within .turn-minimap-popover {
	opacity: 1;
	pointer-events: auto;
	transform: translate(0, -50%) scale(1);
}

.turn-nav-filter {
	width: 100%;
	border: 1px solid var(--ob-border);
	border-radius: .65rem;
	background: transparent;
	padding: .48rem .65rem;
	color: inherit;
	font: inherit;
	font-size: 12px;
}

.turn-nav-filter:focus-visible {
	outline: 2px solid var(--ob-blue);
	outline-offset: 1px;
}

.turn-nav-empty {
	padding: .7rem;
	font-size: 12px;
	color: var(--ob-text-subtle);
}

.turn-nav-row {
	display: grid;
	grid-template-columns: 1.55rem minmax(0, 1fr);
	align-items: center;
	gap: .42rem;
	width: 100%;
	border: 0;
	border-radius: .72rem;
	background: transparent;
	padding: .46rem .52rem;
	color: var(--ob-text);
	text-align: left;
	cursor: pointer;
}

.turn-nav-row:hover {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}

.turn-nav-row.active {
	background: var(--ob-text-strong);
	color: var(--ob-text-inverse);
}

.turn-nav-row.running:not(.active) {
	color: var(--ob-blue);
}

.turn-nav-index {
	display: grid;
	width: 1.28rem;
	height: 1.28rem;
	place-items: center;
	border-radius: 999px;
	background: var(--ob-surface-soft);
	color: var(--ob-text-subtle);
	font-size: 10px;
	font-weight: 780;
}

.turn-nav-row.active .turn-nav-index {
	background: var(--ob-text-strong);
	color: var(--ob-text-inverse);
}

.turn-nav-title {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 12px;
	font-weight: 560;
}

@media (max-width: 760px) {
	.turn-minimap {
		display: none;
	}
}
</style>

<style>
/* OpenBear system dark theme */
html.dark .turn-nav-filter {
		border-color: var(--ob-border);
	}
</style>
