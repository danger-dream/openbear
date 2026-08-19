<script setup>
import {DataAnalysis, Money, Stopwatch, Timer} from "@element-plus/icons-vue";
import BearLogo from "../../components/BearLogo.vue";
import {fmtElapsedFromStart} from "./display.js";
import {vElapsed} from "./elapsedDirective.js";

const props = defineProps({
	title: {type: String, default: ""},
	subtitle: {type: String, default: ""},
	running: {type: Boolean, default: false},
	runStartedAt: {type: Number, default: 0},
	status: {type: String, default: "就绪"},
	contextDisplay: {type: String, default: "—"},
	tokensText: {type: String, default: "0"},
	durationText: {type: String, default: "0s"},
	costText: {type: String, default: "$0.0000"},
});
</script>

<template>
	<header class="console-header">
		<div class="min-w-0 flex flex-1 items-center gap-3 overflow-hidden">
			<div class="header-orb">
				<BearLogo/>
			</div>
			<div class="min-w-0 flex-1 overflow-hidden leading-tight">
				<div class="flex min-w-0 items-center gap-2">
					<h1 class="block max-w-full truncate text-base font-semibold" :title="props.title">
						{{ props.title }}
					</h1>
				</div>
				<div class="mt-0.5 truncate text-[11px] text-[#6b7280]">{{ props.subtitle }}</div>
			</div>
		</div>
		<div class="header-metrics">
			<div class="header-chip" :class="props.running ? 'header-chip-live' : ''">
				<Stopwatch/>
				<span class="chip-label">运行状态</span>
				<strong v-if="props.running" v-elapsed="{ startAt: props.runStartedAt, active: props.running }">
					{{ fmtElapsedFromStart(props.runStartedAt) }}
				</strong>
				<strong v-else>{{ props.status }}</strong>
			</div>
			<div class="header-chip">
				<DataAnalysis/>
				<span class="chip-label">总 Tokens</span>
				<strong>{{ props.tokensText }}</strong>
			</div>
			<div class="header-chip">
				<Timer/>
				<span class="chip-label">总耗时</span>
				<strong>{{ props.durationText }}</strong>
			</div>
			<div class="header-chip">
				<Money/>
				<span class="chip-label">总花费</span>
				<strong>{{ props.costText }}</strong>
			</div>
		</div>
	</header>
</template>

<style scoped>
.console-header {
	display: flex;
	min-height: 4rem;
	flex-shrink: 0;
	align-items: center;
	justify-content: space-between;
	gap: 1rem;
	border-bottom: 1px solid rgba(15, 23, 42, .08);
	background: rgba(255, 255, 255, .86);
	padding: .72rem 1rem;
	backdrop-filter: blur(18px);
}

.header-orb {
	display: grid;
	width: 2.15rem;
	height: 2.15rem;
	place-items: center;
	border: 1px solid #e5e7eb;
	border-radius: .9rem;
	background: linear-gradient(145deg, #fffaf1, #ffffff);
	padding: .36rem;
	box-shadow: inset 0 1px 0 rgba(255, 255, 255, .95), 0 10px 26px rgba(91, 58, 34, .10);
}

.header-chip {
	display: inline-flex;
	align-items: center;
	white-space: nowrap;
}

.header-metrics {
	display: flex;
	flex: 0 1 auto;
	min-width: 18rem;
	max-width: min(64%, 54rem);
	flex-wrap: nowrap;
	align-items: center;
	justify-content: flex-end;
	gap: .25rem;
	overflow-x: auto;
	overflow-y: hidden;
	border: 1px solid rgba(15, 23, 42, .06);
	border-radius: 1.05rem;
	background: rgba(248, 250, 252, .72);
	padding: .16rem;
	box-shadow: inset 0 1px 0 rgba(255, 255, 255, .86);
	scrollbar-width: none;
}

.header-metrics::-webkit-scrollbar {
	display: none;
}

.header-chip {
	height: 1.72rem;
	gap: .26rem;
	border-radius: .86rem;
	padding: 0 .46rem;
	color: #71717a;
	font-size: 10.5px;
	letter-spacing: -.01em;
	transition: background .16s ease, color .16s ease;
}

.header-chip svg {
	width: .76rem;
	height: .76rem;
	flex: 0 0 auto;
	color: #a1a1aa;
	stroke-width: 1.8;
}

.header-chip .chip-label {
	color: #a1a1aa;
	font-size: 10px;
	font-weight: 520;
}

.header-chip strong {
	color: #52525b;
	font-size: 10.8px;
	font-weight: 680;
	letter-spacing: -.015em;
}

.header-chip-live {
	background: rgba(16, 185, 129, .09);
	color: #047857;
}

.header-chip-live svg,
.header-chip-live .chip-label {
	color: #10b981;
}

.header-chip-live strong {
	color: #047857;
	font-weight: 720;
}

@media (max-width: 1120px) {
	.console-header {
		padding-inline: .8rem;
	}
}

@media (max-width: 760px) {
	.console-header {
		min-height: 3.5rem;
		gap: .5rem;
		padding: .5rem .75rem;
	}

	.header-orb {
		display: none;
	}

	.header-metrics {
		min-width: 0;
		max-width: 9rem;
		flex: 0 0 auto;
	}

	.header-metrics .header-chip:nth-child(n+2) {
		display: none;
	}
}
</style>
