<script setup>
import {computed} from "vue";
import AnimatedConversationTitle from "../../components/AnimatedConversationTitle.vue";
import {conversationPathText, fmtElapsedClockMs, headerDurationParts} from "./display.js";
import {vElapsed} from "./elapsedDirective.js";

const props = defineProps({
	title: {type: String, default: ""},
	titleIdentity: {type: String, default: ""},
	conversationPath: {type: String, default: ""},
	running: {type: Boolean, default: false},
	runStartedAt: {type: Number, default: 0},
	status: {type: String, default: "就绪"},
	contextDisplay: {type: String, default: "—"},
	tokensText: {type: String, default: "0"},
	tokensDetail: {type: String, default: ""},
	cachePercentText: {type: String, default: "—"},
	durationMs: {type: Number, default: 0},
	costText: {type: String, default: "$0.0000"},
});
const pathText = computed(() => conversationPathText(props.conversationPath));
const tokenValue = computed(() => props.tokensText.replace(/[KM]$/, ""));
const tokenUnit = computed(() => props.tokensText.match(/[KM]$/)?.[0] || "");
const durationParts = computed(() => headerDurationParts(props.durationMs));
const runStatusText = computed(() => props.status && props.status !== "就绪" ? props.status : "运行中");
</script>

<template>
	<header class="console-header">
		<div class="header-mobile-navigation"><slot name="mobile-navigation"/></div>
		<div class="header-identity min-w-0 flex flex-1 items-center gap-3 overflow-hidden">
			<div class="header-title-stack min-w-0 flex-1 overflow-hidden leading-tight">
				<div class="header-subtitle truncate" :title="pathText" :aria-label="`所在目录 ${pathText}`">{{ pathText }}</div>
				<div class="header-title-row flex min-w-0 items-center gap-2">
					<h1 class="header-title block max-w-full truncate" :title="props.title">
						<AnimatedConversationTitle :text="props.title" :identity="props.titleIdentity" />
					</h1>
				</div>
				<Transition name="header-mobile-run">
					<div v-if="props.running" class="header-mobile-run">
						<i aria-hidden="true"></i>
						<span class="header-mobile-run-clock" title="本次运行耗时" v-elapsed="{ startAt: props.runStartedAt, active: props.running, format: 'hours-clock', fallback: '00:00' }">00:00</span>
						<span class="header-mobile-run-sep" aria-hidden="true">·</span>
						<span class="header-mobile-run-status" role="status">{{ runStatusText }}</span>
					</div>
				</Transition>
			</div>
		</div>
		<dl class="header-metrics" aria-label="会话运行及累计统计">
			<div class="header-metric">
				<dt>运行状态</dt>
				<dd>
					<span class="header-status" :class="{'is-running': props.running}" role="status">
						<i aria-hidden="true"></i>{{ props.running ? '运行中' : props.status }}
					</span>
					<span v-if="props.running" class="header-run-clock" title="本次运行耗时" v-elapsed="{ startAt: props.runStartedAt, active: props.running, format: 'clock', fallback: '00:00' }">
						{{ props.runStartedAt ? fmtElapsedClockMs(Date.now() - props.runStartedAt) : '00:00' }}
					</span>
				</dd>
			</div>
			<div class="header-metric" :title="props.tokensDetail">
				<dt>总 Tokens</dt>
				<dd><span class="header-number">{{ tokenValue }}<span class="header-unit">{{ tokenUnit }}</span></span></dd>
			</div>
			<div class="header-metric" title="会话累计读缓存 Tokens / 累计完整输入 Tokens（含读缓存与写缓存）">
				<dt>缓存命中</dt>
				<dd><span class="header-number">{{ props.cachePercentText }}</span></dd>
			</div>
			<div class="header-metric">
				<dt>总耗时</dt>
				<dd><span v-for="(part, index) in durationParts" :key="index" class="header-number">{{ part.value }}<span class="header-unit">{{ part.unit }}</span></span></dd>
			</div>
			<div class="header-metric">
				<dt>总花费</dt>
				<dd><span class="header-number">{{ props.costText }}</span></dd>
			</div>
		</dl>
		<div class="header-mobile-actions"><slot name="mobile-actions"/></div>
	</header>
</template>

<style scoped>
.header-mobile-navigation,
.header-mobile-actions,
.header-mobile-run { display: none; }

.console-header {
	position: relative;
	z-index: 26;
	isolation: isolate;
	display: flex;
	min-height: 66px;
	flex-shrink: 0;
	align-items: center;
	justify-content: space-between;
	gap: 28px;
	border-bottom: 1px solid var(--ob-chat-line);
	background: transparent;
	color: var(--ob-chat-text);
	padding: 12px 25px;
}

.console-header::before {
	content: '';
	position: absolute;
	inset: 0 0 -18px;
	z-index: -1;
	pointer-events: none;
	background: rgb(var(--ob-surface-rgb) / .55);
	-webkit-backdrop-filter: blur(24px) saturate(1.12);
	backdrop-filter: blur(24px) saturate(1.12);
	-webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 18px), transparent);
	mask-image: linear-gradient(to bottom, #000 calc(100% - 18px), transparent);
}

.header-subtitle {
	color: var(--ob-chat-muted);
	font-size: 10.5px;
	font-weight: 400;
	line-height: 16px;
}

.header-title { margin: 3px 0 0; font-size: 13px; font-weight: 500; line-height: 20px; }

.header-metrics {
	display: flex;
	flex: none;
	align-items: center;
	gap: 16px;
	margin: 0;
}

.header-metric {
	min-width: 0;
	padding-left: 16px;
	border-left: 1px solid var(--ob-chat-line);
}

.header-metric:first-child { border-left: 0; padding-left: 0; }

.header-metric dt {
	margin: 0 0 3px;
	color: var(--ob-chat-muted);
	font-size: 9.5px;
	font-weight: 400;
	line-height: 15px;
	white-space: nowrap;
}

.header-metric dd {
	display: flex;
	align-items: center;
	gap: 7px;
	margin: 0;
	color: var(--ob-chat-text);
	font-size: 12px;
	font-weight: 400;
	line-height: 19px;
	font-variant-numeric: tabular-nums lining-nums;
	white-space: nowrap;
	letter-spacing: .01em;
}

.header-number { font-size: 12px; letter-spacing: 0; }
.header-unit { margin-left: 1px; color: var(--ob-chat-muted); font-size: 10.5px; font-weight: 400; }

.header-status {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	border-radius: 5px;
	color: var(--ob-chat-muted);
	font-size: 11.5px;
	font-weight: 500;
	line-height: 24px;
}
.header-status i { width: 5px; height: 5px; flex: none; border-radius: 50%; background: currentColor; }
.header-status.is-running { padding: 0 7px; color: var(--ob-success); background: rgb(var(--ob-success-rgb) / .09); }
.header-run-clock { color: var(--ob-chat-muted); font-size: 11px; font-weight: 400; letter-spacing: 0; }

@media (min-width: 761px) and (max-width: 1120px) {
	.console-header { gap: 20px; padding-inline: 18px; }
	.header-metrics { gap: 12px; }
	.header-metric { padding-left: 12px; }
	.header-metric:first-child { padding-left: 0; }
}

@media (max-width: 760px) {
	.console-header {
		box-sizing: border-box;
		height: calc(60px + env(safe-area-inset-top, 0px));
		min-height: calc(60px + env(safe-area-inset-top, 0px));
		gap: 5px;
		padding: env(safe-area-inset-top, 0px) .5rem 0;
		border-bottom-color: var(--ob-chat-line);
	}

	.header-mobile-navigation { display: flex; flex: 0 0 44px; }
	.header-mobile-actions { display: flex; flex: 0 0 88px; }
	.header-title-stack { position: relative; padding-right: 12px; }
	.header-subtitle { display: none; }
	.header-title-row { flex: 1; overflow: hidden; }
	.header-title { margin-top: 0; }
	/* Clock first: phase labels change length, the clock must not move with them. */
	.header-mobile-run {
		display: flex;
		align-items: center;
		gap: 5px;
		max-height: 15px;
		margin-top: 1px;
		overflow: hidden;
		color: var(--ob-chat-muted);
		font-size: 10.5px;
		line-height: 15px;
		font-variant-numeric: tabular-nums lining-nums;
		white-space: nowrap;
	}
	.header-mobile-run i { width: 5px; height: 5px; flex: none; border-radius: 50%; background: var(--ob-success); animation: header-run-pulse 1.6s ease-in-out infinite; }
	.header-mobile-run-clock { flex: none; color: var(--ob-success); font-weight: 500; }
	.header-mobile-run-sep { flex: none; }
	.header-mobile-run-status { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.header-mobile-run-enter-active,
	.header-mobile-run-leave-active { transition: max-height .18s ease, margin-top .18s ease, opacity .18s ease; }
	.header-mobile-run-enter-from,
	.header-mobile-run-leave-to { max-height: 0; margin-top: 0; opacity: 0; }
	.header-metrics { display: none; }
}

@keyframes header-run-pulse { 50% { opacity: .35; } }

@media (prefers-reduced-motion: reduce) {
	.header-mobile-run i { animation: none; }
	.header-mobile-run-enter-active,
	.header-mobile-run-leave-active { transition: none; }
}
</style>
