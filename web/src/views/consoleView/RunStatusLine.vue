<script setup>
import {computed, ref} from "vue";
import {cachePct, fmtCost, fmtTokens, tokenPartsFromStats} from "./display.js";
import {vElapsed} from "./elapsedDirective.js";
import "./conversationWork.css";

const props = defineProps({
	startedAt: {type: Number, default: 0},
	// The running turn's live stats; null until its first report.
	stats: {type: Object, default: null},
});
const open = ref(false);

// Settled calls plus the one streaming now: its reported prompt and estimated output.
const parts = computed(() => {
	const settled = tokenPartsFromStats(props.stats);
	const live = props.stats?.inflightUsage;
	if (!live) return settled;
	const cacheRead = Number(live.cacheReadTokens || 0);
	const prompt = Number(live.inputTokens || 0) + cacheRead + Number(live.cacheWriteTokens || 0);
	return {input: settled.input + prompt, output: settled.output + Number(live.outputTokens || 0), cache: settled.cache + cacheRead};
});
const total = computed(() => parts.value.input + parts.value.output);
const hasUsage = computed(() => total.value > 0);
const cacheRate = computed(() => cachePct(parts.value.cache, parts.value.input));
const cost = computed(() => fmtCost(props.stats?.costUsd));

function fmtTps(value) {
	const tps = Number(value || 0);
	if (!(tps > 0) || !Number.isFinite(tps)) return "";
	return `${tps >= 10 ? Math.round(tps) : tps.toFixed(1)}t/s`;
}
const avgTps = computed(() => fmtTps(props.stats?.avgTps));
const peakTps = computed(() => fmtTps(props.stats?.peakTps));
const liveTps = computed(() => fmtTps(props.stats?.liveTps));
// While the model streams, its current speed; otherwise the turn's average, marked as such.
const speed = computed(() => (liveTps.value ? {text: liveTps.value, live: true}
	: avgTps.value ? {text: `均 ${avgTps.value}`, live: false} : null));
const exact = value => Number(value || 0).toLocaleString("en-US");
const usageLabel = computed(() => `本轮 Tokens ${fmtTokens(total.value)}，缓存命中 ${cacheRate.value}，点击查看明细`);
</script>

<template>
	<div class="run-status" role="status">
		<svg class="run-status-runner" viewBox="0 0 24 24" aria-hidden="true">
			<circle cx="14.6" cy="4.4" r="2.1"/>
			<path class="runner-body" d="M13.4 7.6 11.4 13.8"/>
			<path class="runner-arm runner-arm-a" d="M12.9 9.2 15.6 11.2 17.6 9.8"/>
			<path class="runner-arm runner-arm-b" d="M12.9 9.2 9.9 10.4 8.6 12.8"/>
			<path class="runner-leg runner-leg-a" d="M11.4 13.8 14.2 16.6 13 20.6"/>
			<path class="runner-leg runner-leg-b" d="M11.4 13.8 9.2 17.2 6.2 18.2"/>
		</svg>
		<span class="run-status-clock" title="本轮已运行"
		      v-elapsed="{startAt: props.startedAt, active: true, format: 'hours-clock', fallback: '00:00'}">00:00</span>
		<template v-if="hasUsage">
			<span class="run-status-sep" aria-hidden="true">·</span>
			<el-popover v-model:visible="open" trigger="click" placement="top-start" :width="'min(260px, calc(100vw - 24px))'"
			            :show-arrow="false" :offset="8" popper-class="run-status-popover">
				<template #reference>
					<button type="button" class="run-status-usage" :aria-expanded="open" :aria-label="usageLabel">
						<span class="run-status-compact">{{ fmtTokens(total) }}（{{ cacheRate }}）<template v-if="cost !== '—'"> · {{ cost }}</template></span>
						<span class="run-status-wide">↑{{ fmtTokens(parts.input) }} · ↓{{ fmtTokens(parts.output) }} · 缓存 {{ fmtTokens(parts.cache) }}（{{ cacheRate }}）<template v-if="cost !== '—'"> · {{ cost }}</template></span>
					</button>
				</template>
				<section class="run-status-detail" aria-label="本轮用量" @keydown.esc.stop="open = false">
					<header><strong>本轮用量</strong><span>已运行 <span v-elapsed="{startAt: props.startedAt, active: true, format: 'hours-clock', fallback: '00:00'}">00:00</span></span></header>
					<dl>
						<div><dt>输入 Tokens</dt><dd>{{ exact(parts.input) }}</dd></div>
						<div><dt>输出 Tokens</dt><dd>{{ exact(parts.output) }}</dd></div>
						<div><dt>缓存 Tokens</dt><dd>{{ exact(parts.cache) }}</dd></div>
						<div><dt>缓存命中率</dt><dd>{{ cacheRate }}</dd></div>
						<div><dt>本轮金额</dt><dd>{{ cost }}</dd></div>
						<div><dt>当前速度</dt><dd>{{ liveTps || "没在输出" }}</dd></div>
						<div><dt>平均速度</dt><dd>{{ avgTps || "—" }}</dd></div>
						<div v-if="peakTps"><dt>最快一次</dt><dd>{{ peakTps }}</dd></div>
					</dl>
					<p>输入已包含缓存，含子 Agent 用量。进行中的调用：服务方已给出的输入按实际值，否则按上下文估算；输出按已收到的内容估算；调用结束后换成实际用量。当前速度取最近 3 秒，平均速度按已完成的调用计算。</p>
				</section>
			</el-popover>
		</template>
		<template v-if="speed">
			<span class="run-status-sep" aria-hidden="true">·</span>
			<span class="run-status-tps" :class="{'is-idle': !speed.live}"
			      :title="speed.live ? '实时输出速度' : '模型此刻没有在输出，显示本轮平均速度'">{{ speed.text }}</span>
		</template>
	</div>
</template>

<style scoped>
.run-status {
	display: flex;
	align-items: center;
	gap: .3rem;
	min-width: 0;
	min-height: 1.6rem;
	padding: 0 .3rem .35rem;
	color: var(--ob-chat-subtle);
	font-size: 11.5px;
	font-variant-numeric: tabular-nums;
	white-space: nowrap;
}

.run-status-runner {
	width: 1.05rem;
	height: 1.05rem;
	flex: none;
	color: rgb(var(--ob-blue-rgb));
	fill: none;
	stroke: currentColor;
	stroke-width: 2;
	stroke-linecap: round;
	stroke-linejoin: round;
	animation: runner-bob .6s ease-in-out infinite;
}

.run-status-runner circle {
	fill: currentColor;
	stroke: none;
}

.runner-arm,
.runner-leg {
	transform-box: view-box;
	animation: runner-swing .6s ease-in-out infinite alternate;
}

.runner-arm { transform-origin: 12.9px 9.2px; }
.runner-leg { transform-origin: 11.4px 13.8px; }
.runner-arm-b,
.runner-leg-a { animation-direction: alternate-reverse; }

@keyframes runner-swing {
	from { transform: rotate(-28deg); }
	to { transform: rotate(28deg); }
}

@keyframes runner-bob {
	0%, 100% { transform: translateY(0); }
	50% { transform: translateY(-1px); }
}

.run-status-clock {
	color: var(--ob-chat-text);
}

.run-status-sep {
	color: var(--ob-chat-muted);
}

.run-status-tps.is-idle {
	color: var(--ob-chat-muted);
}

.run-status-usage {
	display: inline-flex;
	min-width: 0;
	align-items: center;
	border-radius: .4rem;
	margin: 0 -.2rem;
	padding: .1rem .2rem;
	color: inherit;
	transition: background-color .15s, color .15s;
}

.run-status-usage > span {
	overflow: hidden;
	text-overflow: ellipsis;
}

.run-status-usage:hover,
.run-status-usage[aria-expanded="true"] {
	background: var(--ob-chat-hover);
	color: var(--ob-chat-text);
}

.run-status-usage:focus-visible {
	outline: 2px solid var(--ob-focus);
	outline-offset: 1px;
}

.run-status-compact { display: none; }

.run-status-detail { padding: 14px; color: var(--work-text); font-size: 12px; line-height: 1.6; }
.run-status-detail header { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
.run-status-detail strong { font-size: 13px; font-weight: 550; }
.run-status-detail header > span { color: var(--work-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.run-status-detail dl { display: grid; gap: 6px; margin: 0; }
.run-status-detail dl > div { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
.run-status-detail dt { color: var(--work-muted); }
.run-status-detail dd { margin: 0; font-variant-numeric: tabular-nums; }
.run-status-detail p { margin: 10px 0 0; border-top: 1px solid var(--work-rule); padding-top: 8px; color: var(--work-faint); font-size: 11px; }

@media (max-width: 760px) {
	.run-status { min-height: 2rem; padding-bottom: .25rem; }
	.run-status-usage { min-height: 2rem; }
	.run-status-compact { display: inline; }
	.run-status-wide { display: none; }
}

@media (prefers-reduced-motion: reduce) {
	.run-status-runner,
	.runner-arm,
	.runner-leg { animation: none; }
}
</style>

<style>
.el-popover.el-popper.run-status-popover { padding: 0; border: 1px solid var(--work-rule); border-radius: 12px; background: var(--work-panel); box-shadow: var(--ob-shadow-popover); }
</style>
