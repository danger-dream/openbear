<script setup>
import {shortText} from "./display.js";

const props = defineProps({
	turns: {type: Array, default: () => []},
	activeTurnIndex: {type: Number, default: 0},
	running: {type: Boolean, default: false},
});
const emit = defineEmits(["scroll-to-turn"]);

function turnNavLabel(turn, index = 0) {
	const text = shortText(turn?.user?.content || "", 40);
	return text || `第 ${index + 1} 轮对话`;
}
</script>

<template>
	<nav v-if="props.turns.length >= 3" class="turn-minimap" aria-label="对话快速导航">
		<div class="turn-minimap-rail">
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
			<button
				v-for="(turn, idx) in props.turns"
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
	gap: .22rem;
	border-radius: 999px;
	background: rgba(255, 255, 255, .62);
	padding: .42rem .3rem;
	backdrop-filter: blur(14px);
	box-shadow: 0 8px 24px rgba(15, 23, 42, .06);
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
	background: #d4d4d8;
	transition: width .15s ease, height .15s ease, background .15s ease;
}

.turn-minimap-dot:hover span {
	width: 1.05rem;
	background: #a1a1aa;
}

.turn-minimap-dot.active span {
	width: 1.16rem;
	height: 3px;
	background: #18181b;
}

.turn-minimap-dot.running span {
	background: #2563eb;
	box-shadow: 0 0 0 3px rgba(37, 99, 235, .10);
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
	border: 1px solid rgba(15, 23, 42, .10);
	border-radius: 1rem;
	background: rgba(255, 255, 255, .94);
	padding: .42rem;
	box-shadow: 0 18px 52px rgba(15, 23, 42, .15);
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
	color: #52525b;
	text-align: left;
	cursor: pointer;
}

.turn-nav-row:hover {
	background: #f4f4f5;
	color: #18181b;
}

.turn-nav-row.active {
	background: #18181b;
	color: #fff;
}

.turn-nav-row.running:not(.active) {
	color: #1d4ed8;
}

.turn-nav-index {
	display: grid;
	width: 1.28rem;
	height: 1.28rem;
	place-items: center;
	border-radius: 999px;
	background: #f4f4f5;
	color: #71717a;
	font-size: 10px;
	font-weight: 780;
}

.turn-nav-row.active .turn-nav-index {
	background: rgba(255, 255, 255, .18);
	color: #fff;
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
