<script setup>
import {ArrowRight, Bell, CircleCheck, CircleClose, MagicStick, Refresh, Tools} from "@element-plus/icons-vue";
import AgentEventCard from "./AgentEventCard.vue";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import ConsoleToolEvent from "./ConsoleToolEvent.vue";
import ConsoleUserInteractionEvent from "./ConsoleUserInteractionEvent.vue";
import {isAgentEvent, modelRetryReasonLabel, toolResultKey, toolStatus} from "./display.js";
import {answerContent, hasMeaningfulAnswerText} from "./markdown.js";
import {isUserInteractionEvent} from "./userInteractionPresentation.js";

const props = defineProps({
	event: {type: Object, required: true},
	conversationUuid: {type: String, default: ""},
	turnId: {type: [String, Number], required: true},
	index: {type: Number, required: true},
	autoScrollLocked: {type: Boolean, default: false},
	retryCancelPending: {type: Boolean, default: false},
	liveTextTarget: {type: String, default: ""},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	activeToolResultIndex: {type: Function, required: true},
	agentPreviewOnly: {type: Boolean, default: false},
	compact: {type: Boolean, default: false},
	showReasoning: {type: Boolean, default: true},
});
const emit = defineEmits(["details-toggle", "reasoning-toggle", "select-tool-result", "cancel-retry"]);

function scrollReasoningBodyToEnd(el, active) {
	if (!active || !el) return;
	const now = performance.now();
	const run = () => {
		el._openbearReasoningScrollFrame = 0;
		el._openbearReasoningScrollAt = performance.now();
		el.scrollTop = el.scrollHeight;
	};
	if (el._openbearReasoningScrollFrame || el._openbearReasoningScrollTimer) return;
	const wait = Math.max(0, 80 - (now - Number(el._openbearReasoningScrollAt || 0)));
	el._openbearReasoningScrollTimer = window.setTimeout(() => {
		el._openbearReasoningScrollTimer = 0;
		el._openbearReasoningScrollFrame = window.requestAnimationFrame(run);
	}, wait);
}

const vReasoningAutoscroll = {
	mounted(el, binding) {
		scrollReasoningBodyToEnd(el, binding.value);
	},
	updated(el, binding) {
		scrollReasoningBodyToEnd(el, binding.value);
	},
	beforeUnmount(el) {
		if (el?._openbearReasoningScrollTimer) window.clearTimeout(el._openbearReasoningScrollTimer);
		if (el?._openbearReasoningScrollFrame) window.cancelAnimationFrame(el._openbearReasoningScrollFrame);
	},
};

function key(kind, ...rest) {
	return props.detailKey(props.turnId, kind, props.index, ...rest);
}

function isOpen(kind, ...rest) {
	return props.isDetailOpen(key(kind, ...rest));
}

function toggleDetails(event, kind, ...rest) {
	emit("details-toggle", event, key(kind, ...rest));
}

function toggleReasoning(event) {
	emit("reasoning-toggle", event, key("reasoning"), Boolean(props.event?.reasoningActive));
}

function groupEvents() {
	return Array.isArray(props.event?.events) ? props.event.events : [];
}

function groupRunning() {
	return groupEvents().some((item) => toolStatus(item) === "running");
}

function onAgentDetailsToggle(event, detail) {
	emit("details-toggle", event, detail);
}

function retryAttemptLabel(event = props.event) {
	const retry = event?.retry || {};
	const attempt = Number(retry.attempt || 0);
	const maximum = Number(retry.maxAttempts || 0);
	if (attempt && maximum) return `${attempt}/${maximum}`;
	return attempt ? String(attempt) : "—";
}

function retryReasonLabel(event = props.event) {
	return modelRetryReasonLabel(event?.retry || {});
}

function retryWaitLabel(event = props.event) {
	const waitMs = Number(event?.retry?.waitMs || 0);
	if (!waitMs) return "";
	if (waitMs < 1000) return `等待 ${Math.round(waitMs)}ms`;
	const seconds = waitMs / 1000;
	return `等待 ${seconds < 10 ? seconds.toFixed(1).replace(/\.0$/, "") : Math.round(seconds)} 秒`;
}

function retryStatusLabel(event = props.event) {
	const retry = event?.retry || {};
	if (retry.active) return "等待重试";
	const status = String(retry.status || "");
	if (["resumed", "completed", "complete"].includes(status)) return "已恢复";
	if (["cancelled", "canceled", "stopped"].includes(status)) return "已取消";
	if (status === "failed") return "重试失败";
	return status || "已恢复";
}

function retryOutcomeMeta(event = props.event) {
	const retry = event?.retry || {};
	const status = String(retry.status || "");
	if (retry.active) return {icon: CircleClose, tone: "waiting"};
	if (["resumed", "completed", "complete"].includes(status) || !status) return {icon: CircleCheck, tone: "success"};
	if (["cancelled", "canceled", "stopped"].includes(status)) return {icon: CircleClose, tone: "cancelled"};
	return {icon: CircleClose, tone: "failed"};
}
</script>

<template>
	<div v-if="props.event.kind === 'answer'" class="answer-block"
	     :class="{ 'answer-has-text': hasMeaningfulAnswerText(props.event.message.content || '') }">
		<details
			v-if="props.showReasoning && props.event.message.reasoning"
			:key="`${key('reasoning')}-${props.event.reasoningActive ? 'live' : 'done'}`"
			class="reasoning-card tool-event"
			:class="{ 'process-live': props.event.reasoningActive }"
			:open="props.event.reasoningActive || isOpen('reasoning')"
			@toggle="toggleReasoning"
		>
			<summary @click="props.event.reasoningActive && $event.preventDefault()">
				<MagicStick class="inline-icon"/>
				<span>思考过程</span>
				<span class="disclosure-icon"><ArrowRight/></span>
			</summary>
			<div v-if="props.event.reasoningActive || isOpen('reasoning')" class="reasoning-body"
			     v-reasoning-autoscroll="props.event.reasoningActive && props.autoScrollLocked">
				<ConsoleMarkdown :text="props.event.message.reasoning" :live="props.liveTextTarget === 'reasoning'"/>
			</div>
		</details>
		<ConsoleMarkdown v-if="hasMeaningfulAnswerText(props.event.message.content || '')" class="answer-text"
		                 :text="answerContent(props.event.message.content || '')"
		                 :live="props.liveTextTarget === 'answer'"/>
	</div>
	
	<div v-else-if="props.event.kind === 'model_retry'" class="tool-event retry-inline-event" :class="{'process-live': props.event.retry?.active}" role="status" aria-live="polite">
		<div class="live-process-summary retry-inline-summary">
			<span class="tool-icon retry-fixed-icon" aria-hidden="true"><Refresh/></span>
			<span class="tool-name retry-inline-name">
				<span>模型重试</span>
				<span class="retry-outcome-icon" :class="`is-${retryOutcomeMeta().tone}`" aria-hidden="true">
					<component :is="retryOutcomeMeta().icon"/>
				</span>
				<span class="retry-inline-attempt">{{ retryAttemptLabel() }}</span>
			</span>
			<span class="tool-preview">{{ [retryReasonLabel(), retryWaitLabel()].filter(Boolean).join(' · ') || retryStatusLabel() }}</span>
			<span class="retry-inline-state">{{ retryStatusLabel() }}</span>
			<button
				v-if="props.event.retry?.active && props.event.retry?.cancellable"
				type="button"
				class="retry-inline-cancel"
				:disabled="props.retryCancelPending"
				@click.stop="emit('cancel-retry', props.event)"
			>{{ props.retryCancelPending ? '取消中…' : '取消重试' }}</button>
		</div>
	</div>

	<div v-else-if="props.event.kind === 'live_status'" class="tool-event live-status-event"
	     :class="{ 'process-live': props.event.active !== false && !props.event.queued, 'agent-notice-event': props.event.agentNotice, 'thinking-only-event': props.event.persistentRunIndicator }">
		<div class="live-process-summary" :class="{ 'thinking-only-summary': props.event.persistentRunIndicator }">
			<template v-if="props.event.persistentRunIndicator">
				<span class="thinking-dots" aria-label="正在思考">
					<span></span><span></span><span></span>
				</span>
			</template>
			<template v-else>
				<span v-if="props.event.agentNotice" class="tool-icon agent-notice-icon"><Bell/></span>
				<span v-else-if="props.event.notification" class="tool-icon notification-icon"><CircleCheck/></span>
				<span v-else class="live-tool-dot"></span>
				<span class="tool-name">{{ props.event.status || '正在思考 ...' }}</span>
				<span v-if="props.event.queued || props.event.interruption" class="tool-preview">{{ props.event.preview || (props.event.queued ? '等待当前运行处理' : '已交给主会话') }}</span>
				<span v-else-if="props.event.active !== false" class="thinking-dots" aria-label="正在思考">
					<span></span><span></span><span></span>
				</span>
				<span v-else-if="props.event.preview" class="tool-preview">{{ props.event.preview }}</span>
			</template>
		</div>
	</div>
	
	<div v-else-if="props.event.kind === 'live_tool'" class="tool-event live-inline-tool process-live">
		<div class="live-process-summary">
			<span class="tool-icon"><Tools/></span>
			<span class="tool-name">{{ props.event.name }}</span>
			<span class="tool-preview">{{ props.event.preview }}</span>
		</div>
	</div>
	
	<AgentEventCard
		v-else-if="props.event.kind === 'live_agent'"
		:event="props.event"
		:conversation-uuid="props.conversationUuid"
		:turn-id="props.turnId"
		:index="props.index"
		:detail-key="props.detailKey"
		:is-detail-open="props.isDetailOpen"
		:on-details-toggle="onAgentDetailsToggle"
		:preview-only="props.agentPreviewOnly"
	/>
	
	<details
		v-else-if="props.event.kind === 'tool_group'"
		class="tool-group-event"
		:class="{ 'process-live': groupRunning() }"
		:open="props.event.open || isOpen('tool_group')"
		@toggle="toggleDetails($event, 'tool_group')"
	>
		<summary>
			<span class="tool-group-icon"><Tools/></span>
			<strong>连续工具调用</strong>
			<span class="disclosure-icon"><ArrowRight/></span>
			<em>共 {{ groupEvents().length }} 次</em>
		</summary>
		<div v-if="isOpen('tool_group')" class="tool-group-stack">
			<ConsoleToolEvent
				v-for="(item, gidx) in groupEvents()"
				:key="`${toolResultKey(item)}-${gidx}`"
				:event="item"
				:conversation-uuid="props.conversationUuid"
				:open="isOpen('tool_group_item', gidx)"
				:active-index="props.activeToolResultIndex(item)"
				@toggle="toggleDetails($event, 'tool_group_item', gidx)"
				@select-tab="emit('select-tool-result', item, $event)"
			/>
		</div>
	</details>
	
	<ConsoleUserInteractionEvent
		v-else-if="isUserInteractionEvent(props.event)"
		:event="props.event"
		:conversation-uuid="props.conversationUuid"
		:open="isOpen('user_interaction')"
		:compact="props.compact"
		@toggle="toggleDetails($event, 'user_interaction')"
	/>

	<AgentEventCard
		v-else-if="props.event.kind === 'tool' && isAgentEvent(props.event)"
		:event="props.event"
		:conversation-uuid="props.conversationUuid"
		:turn-id="props.turnId"
		:index="props.index"
		:detail-key="props.detailKey"
		:is-detail-open="props.isDetailOpen"
		:on-details-toggle="onAgentDetailsToggle"
		:preview-only="props.agentPreviewOnly"
	/>
	
	<ConsoleToolEvent
		v-else-if="props.event.kind === 'tool'"
		:event="props.event"
		:conversation-uuid="props.conversationUuid"
		:open="isOpen('tool')"
		:active-index="props.activeToolResultIndex(props.event)"
		@toggle="toggleDetails($event, 'tool')"
		@select-tab="emit('select-tool-result', props.event, $event)"
	/>
</template>

<style scoped>
.inline-icon {
	display: inline-block;
	flex: 0 0 auto;
	width: 1em;
	height: 1em;
	margin-right: 0.3rem;
	vertical-align: -0.13em;
}

.disclosure-icon {
	display: none;
	width: 1rem;
	height: 1rem;
	flex: 0 0 auto;
	place-items: center;
	color: #64748b;
}

summary:hover > .disclosure-icon,
summary:focus-visible > .disclosure-icon,
details[open] > summary > .disclosure-icon {
	display: grid;
}

.disclosure-icon svg {
	width: .72rem;
	height: .72rem;
	transition: transform .14s ease;
}

details[open] > summary > .disclosure-icon svg {
	transform: rotate(90deg);
}

details[open] > summary > .disclosure-icon {
	color: #4338ca;
}

.answer-block + .answer-block {
	margin-top: 0.7rem;
}

.answer-text + .reasoning-card {
	margin-top: 0.55rem;
}

.reasoning-card + .answer-text {
	margin-top: 0.75rem;
}

.tool-event + .answer-block,
.tool-group-event + .answer-block,
.agent-event-card + .answer-block {
	margin-top: 0.16rem;
}

.answer-block:not(.answer-has-text) + .tool-event,
.answer-block:not(.answer-has-text) + .tool-group-event {
	margin-top: 0.16rem;
}

.answer-block + .agent-event-card {
	margin-top: 0.72rem;
}

.reasoning-card,
.tool-event,
.tool-group-event {
	border: 0;
	border-radius: 0;
	background: transparent;
	box-shadow: none;
	overflow: visible;
}

.reasoning-card {
	margin: .16rem 0;
	color: #71717a;
	font-size: 12px;
}

.reasoning-card summary {
	display: flex;
	align-items: center;
	width: auto;
	gap: .32rem;
	color: #71717a;
	font-weight: 500;
	cursor: pointer;
	user-select: none;
	min-height: 1.45rem;
	padding: .04rem 0;
	list-style: none;
}

.reasoning-card summary::-webkit-details-marker,
.tool-group-event > summary::-webkit-details-marker {
	display: none;
}

.reasoning-card summary .inline-icon {
	width: .78rem;
	height: .78rem;
	color: #94a3b8;
}

.reasoning-body {
	margin: .12rem 0 .2rem 0;
	border: 0;
	border-radius: 0;
	background: transparent;
	padding: 0;
	color: #52525b;
	max-height: min(42vh, 360px);
	overflow: auto;
	overscroll-behavior: contain;
	scroll-behavior: smooth;
	scrollbar-width: thin;
}

.reasoning-body::-webkit-scrollbar {
	width: 8px;
	height: 8px;
}

.reasoning-body::-webkit-scrollbar-thumb {
	border-radius: 999px;
	background: #cbd5e1;
}

.reasoning-body::-webkit-scrollbar-track {
	background: transparent;
}

.live-tool-dot {
	margin-top: .42rem;
	width: .38rem;
	height: .38rem;
	flex: 0 0 auto;
	border-radius: 999px;
	background: #a1a1aa;
	box-shadow: none;
}

.live-process-summary {
	display: grid;
	grid-template-columns: auto minmax(0, auto) minmax(4rem, 1fr);
	align-items: center;
	gap: .34rem;
	max-width: 100%;
	min-width: 0;
	min-height: 1.45rem;
	padding: .04rem 0 .04rem .08rem;
	color: #71717a;
	font-family: inherit;
	font-size: 12px;
	line-height: 1.45;
	overflow: visible;
}

.live-process-summary.thinking-only-summary {
	display: inline-flex;
	grid-template-columns: none;
	width: auto;
	min-height: 1.15rem;
	padding-left: 0;
}

.live-process-summary .live-tool-dot {
	margin-top: 0;
}

.live-process-summary .tool-icon {
	display: grid;
	width: .92rem;
	height: .92rem;
	place-items: center;
	color: #a1a1aa;
}

.live-process-summary .tool-icon svg {
	width: .78rem;
	height: .78rem;
}

.live-process-summary .notification-icon {
	color: #16a34a;
}

.live-process-summary .agent-notice-icon {
	color: #a1a1aa;
}

.live-process-summary .agent-notice-icon svg {
	width: .74rem;
	height: .74rem;
}

.live-process-summary .tool-name {
	display: block;
	min-width: 0;
	max-width: 100%;
	overflow: hidden;
	color: #64748b;
	font-size: inherit;
	font-weight: 520;
	letter-spacing: 0;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.live-process-summary .tool-preview {
	display: block;
	min-width: 0;
	overflow: hidden;
	color: #9ca3af;
	font-size: inherit;
	line-height: inherit;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.retry-inline-summary {
	grid-template-columns: .92rem minmax(0, auto) minmax(4rem, 1fr) auto auto;
}

.retry-fixed-icon {
	flex: 0 0 auto;
}

.live-process-summary .retry-inline-name {
	display: inline-flex;
	align-items: center;
	gap: .22rem;
	font-variant-numeric: tabular-nums;
}

.retry-inline-attempt {
	color: #64748b;
}

.retry-outcome-icon {
	display: inline-grid;
	width: .72rem;
	height: .72rem;
	flex: 0 0 auto;
	place-items: center;
}

.retry-outcome-icon svg {
	display: block;
	width: .7rem;
	height: .7rem;
}

.retry-outcome-icon.is-waiting {
	color: #d97706;
}

.retry-outcome-icon.is-success {
	color: #16a34a;
}

.retry-outcome-icon.is-cancelled {
	color: #94a3b8;
}

.retry-outcome-icon.is-failed {
	color: #dc2626;
}

.retry-inline-state {
	border-radius: 999px;
	background: #f1f5f9;
	padding: .1rem .42rem;
	color: #64748b;
	font-size: 10px;
	font-weight: 600;
	white-space: nowrap;
}

.process-live .retry-inline-state {
	background: #fff7ed;
	color: #c2410c;
}

.retry-inline-cancel {
	border: 0;
	border-radius: 6px;
	background: transparent;
	padding: .15rem .38rem;
	color: #b45309;
	font-family: inherit;
	font-size: 10.5px;
	cursor: pointer;
	white-space: nowrap;
}

.retry-inline-cancel:hover:not(:disabled) {
	background: #fff7ed;
	color: #9a3412;
}

.retry-inline-cancel:disabled {
	cursor: wait;
	opacity: .55;
}

.thinking-dots {
	position: relative;
	display: inline-flex;
	align-items: center;
	gap: .22rem;
	width: max-content;
	min-width: 1.74rem;
	height: 1.05rem;
	overflow: hidden;
	border-radius: 999px;
	padding: 0 .2rem;
	isolation: isolate;
}

.thinking-dots::after {
	content: "";
	position: absolute;
	inset: -45% -70%;
	z-index: -1;
	background: linear-gradient(105deg, transparent 28%, rgba(255, 255, 255, .88) 43%, rgba(148, 163, 184, .18) 50%, transparent 66%);
	transform: translateX(-46%);
	animation: thinkingSunSweep 2.05s ease-in-out infinite;
}

.thinking-dots span {
	display: block;
	width: .34rem;
	height: .34rem;
	border-radius: 999px;
	background: #a1a1aa;
	box-shadow: 0 0 0 0 rgba(161, 161, 170, .20), 0 0 10px rgba(148, 163, 184, .18);
	animation: thinkingDotBreathe 1.18s ease-in-out infinite;
}

.thinking-dots span:nth-child(2) {
	animation-delay: .16s;
}

.thinking-dots span:nth-child(3) {
	animation-delay: .32s;
}

.tool-group-event {
	margin: .18rem 0;
}

.tool-group-event > summary {
	display: flex;
	align-items: center;
	gap: .34rem;
	width: auto;
	padding: .04rem 0;
	color: #71717a;
	cursor: pointer;
	list-style: none;
}

.tool-group-event > summary > .tool-group-icon {
	display: grid;
	width: .92rem;
	height: .92rem;
	place-items: center;
	color: #a1a1aa;
}

.tool-group-event > summary .tool-group-icon svg {
	width: .78rem;
	height: .78rem;
	color: currentColor;
}

.tool-group-event > summary strong {
	color: #64748b;
	font-size: 11.5px;
	font-weight: 520;
	white-space: nowrap;
}

.tool-group-event > summary em {
	border: 0;
	background: transparent;
	padding: 0;
	color: #9ca3af;
	font-size: 10.5px;
	font-style: normal;
	font-weight: 500;
	white-space: nowrap;
}

.tool-group-stack {
	margin: .16rem 0 .28rem 0;
	border: 1px solid #eceff3;
	border-radius: 9px;
	background: #fff;
	padding: .54rem .62rem;
}

.tool-group-stack .tool-event {
	margin: .12rem 0;
}

.process-live .live-process-summary .tool-name,
.process-live.tool-group-event:not([open]) > summary strong,
.process-live.reasoning-card > summary > span:nth-of-type(1) {
	color: #52525b;
}

.process-live .live-process-summary .tool-preview {
	color: #9ca3af;
}

.process-live .live-process-summary .live-tool-dot,
.process-live .live-process-summary .tool-icon,
.process-live.tool-group-event:not([open]) > summary > .tool-group-icon,
.process-live.reasoning-card > summary .inline-icon {
	animation: liveIndicatorPulse 2.25s ease-in-out infinite;
	transform-origin: center;
	will-change: opacity, transform;
}

@keyframes liveIndicatorPulse {
	0%, 100% {
		opacity: .58;
		transform: scale(.92);
	}
	45% {
		opacity: .98;
		transform: scale(1.04);
	}
}

@keyframes thinkingDotBreathe {
	0%, 100% {
		opacity: .38;
		transform: translateY(.02rem) scale(.72);
		box-shadow: 0 0 0 0 rgba(161, 161, 170, .10), 0 0 6px rgba(148, 163, 184, .12);
	}
	42% {
		opacity: .96;
		transform: translateY(-.02rem) scale(1.04);
		box-shadow: 0 0 0 4px rgba(161, 161, 170, .10), 0 0 14px rgba(148, 163, 184, .32);
	}
}

@keyframes thinkingSunSweep {
	0% {
		transform: translateX(-48%);
		opacity: 0;
	}
	28% {
		opacity: .75;
	}
	58% {
		transform: translateX(50%);
		opacity: .5;
	}
	100% {
		transform: translateX(50%);
		opacity: 0;
	}
}
</style>
