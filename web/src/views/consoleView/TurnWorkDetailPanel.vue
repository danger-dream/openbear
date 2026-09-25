<script setup>
import {computed} from "vue";
import {ArrowRight, Close, MagicStick} from "@element-plus/icons-vue";
import {eventDisplayTimeMs} from "../../timelineProjection.js";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import TurnEvent from "./TurnEvent.vue";
import {useMessageVisibility} from './messageVisibility.js';
const visibility = useMessageVisibility();
import WorkDetailIcon from "./WorkDetailIcon.vue";

const props = defineProps({
	open: {type: Boolean, default: false},
	turn: {type: Object, default: null},
	turnIndex: {type: Number, default: 0},
	conversationUuid: {type: String, default: ""},
	autoScrollLocked: {type: Boolean, default: false},
	retryActionPending: {type: Object, default: () => ({})},
	working: {type: Boolean, default: false},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	activeToolResultIndex: {type: Function, required: true},
});

const emit = defineEmits(["close", "details-toggle", "reasoning-toggle", "select-tool-result", "cancel-retry", "retry-now"]);

const sourceEvents = computed(() => Array.isArray(props.turn?.events) ? props.turn.events : []);
const workEntries = computed(() => {
	const rows = [];
	for (const [index, event] of sourceEvents.value.entries()) {
		if (visibility.isHidden(event)) continue;
		if (event?.kind === "answer") {
			const reasoning = String(event?.message?.reasoning || "").trim();
			if (reasoning) rows.push({kind: "reasoning", event, index, reasoning, timeMs: eventDisplayTimeMs(event)});
			continue;
		}
		// The transient three-dot indicator communicates no durable work detail.
		if (event?.kind === "live_status" && event?.persistentRunIndicator && !event?.preview) continue;
		rows.push({kind: "event", event, index, timeMs: eventDisplayTimeMs(event)});
	}
	return rows;
});

const turnLabel = computed(() => `第 ${Math.max(1, props.turnIndex + 1)} 轮`);
const userPreview = computed(() => visibility.userContent(props.turn).replace(/\s+/g, " ").trim().slice(0, 72));

function reasoningKey(entry) {
	return props.detailKey(props.turn?.id || props.turnIndex, "work_reasoning", entry.index);
}

function relayDetailsToggle(event, key) {
	emit("details-toggle", event, key);
}

function relayReasoningToggle(event, key, active) {
	emit("reasoning-toggle", event, key, active);
}

function relayToolResult(event, index) {
	emit("select-tool-result", event, index);
}

function entryTimeLabel(timeMs) {
	const value = Number(timeMs || 0);
	if (!value) return "—";
	return new Date(value).toLocaleTimeString("zh-CN", {
		hour12: false,
		hour: "2-digit",
		minute: "2-digit",
		second: "2-digit",
	});
}

function entryTimeTitle(timeMs) {
	const value = Number(timeMs || 0);
	return value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "时间不可用";
}

function entryTimeDateTime(timeMs) {
	const value = Number(timeMs || 0);
	return value ? new Date(value).toISOString() : "";
}
</script>

<template>
	<aside class="work-detail" :class="{open: props.open}" :aria-hidden="props.open ? 'false' : 'true'">
		<div class="work-detail-surface">
			<header class="work-detail-header">
				<div class="work-detail-heading">
					<span class="work-detail-kicker" :class="{working: props.working}"><WorkDetailIcon/> 工作详情</span>
					<h2>{{ turnLabel }}</h2>
					<p v-if="userPreview" :title="visibility.userContent(props.turn)">{{ userPreview }}</p>
				</div>
				<button type="button" class="work-detail-close" aria-label="关闭工作详情" @click="emit('close')"><Close/></button>
			</header>

			<div class="work-detail-body" tabindex="0" aria-label="工作详情内容，可滚动">
				<p v-if="userPreview" class="work-detail-turn-preview" :title="visibility.userContent(props.turn)">{{ userPreview }}</p>
				<div v-if="!workEntries.length" class="work-detail-empty">
					<WorkDetailIcon/>
					<strong>这一轮没有工作详情</strong>
					<p>滚动对话后，这里会自动跟随当前可视轮次。</p>
				</div>

				<template v-for="entry in workEntries" :key="entry.event?.id || entry.event?.eventKey || `${entry.kind}-${entry.index}`">
					<div class="work-detail-entry">
						<time class="work-entry-time" :datetime="entryTimeDateTime(entry.timeMs)" :title="entryTimeTitle(entry.timeMs)">{{ entryTimeLabel(entry.timeMs) }}</time>
						<details
							v-if="entry.kind === 'reasoning'"
							class="work-reasoning"
							:open="isDetailOpen(reasoningKey(entry))"
							@toggle="emit('details-toggle', $event, reasoningKey(entry))"
						>
							<summary>
								<span class="work-reasoning-icon"><MagicStick/></span>
								<span class="work-reasoning-name">思考过程</span>
								<span class="work-disclosure"><ArrowRight/></span>
							</summary>
							<div class="work-reasoning-detail">
								<div class="work-reasoning-body"><ConsoleMarkdown :text="entry.reasoning"/></div>
							</div>
						</details>
						<TurnEvent
							v-else
							class="work-event"
							:event="entry.event"
							:conversation-uuid="props.conversationUuid"
							:turn-id="props.turn?.id || props.turnIndex"
							:index="entry.index"
							:auto-scroll-locked="props.autoScrollLocked"
							:reasoning-autoscroll="Boolean(entry.event.reasoningActive) && props.autoScrollLocked"
							:retry-action-pending="props.retryActionPending"
							:agent-preview-only="true"
							:compact="true"
							:detail-key="props.detailKey"
							:is-detail-open="props.isDetailOpen"
							:active-tool-result-index="props.activeToolResultIndex"
							@details-toggle="relayDetailsToggle"
							@reasoning-toggle="relayReasoningToggle"
							@select-tool-result="relayToolResult"
							@cancel-retry="emit('cancel-retry', $event)"
							@retry-now="emit('retry-now', $event)"
						/>
					</div>
				</template>
				<div v-if="props.working && workEntries.length" class="work-detail-running" role="status" aria-live="polite">
					<span class="work-running-dots" aria-label="正在工作"><span></span><span></span><span></span></span>
				</div>
			</div>
		</div>
	</aside>
</template>

<style scoped>
.work-detail {
	box-sizing: border-box;
	min-height: 0;
	position: relative;
	flex: 0 0 0;
	width: 0;
	min-width: 0;
	height: 100%;
	overflow: hidden;
	border-left: 1px solid transparent;
	background: var(--ob-surface);
	opacity: 0;
	transition: flex-basis .24s cubic-bezier(.22, 1, .36, 1), width .24s cubic-bezier(.22, 1, .36, 1), opacity .15s ease, border-color .2s ease;
}
.work-detail.open {
	flex-basis: clamp(24rem, 32vw, 32rem);
	width: clamp(24rem, 32vw, 32rem);
	border-left-color: var(--ob-border);
	opacity: 1;
}
.work-detail-surface {
	box-sizing: border-box;
	display: flex;
	width: 100%;
	min-width: 0;
	min-height: 0;
	height: 100%;
	flex-direction: column;
	background: var(--ob-surface);
}
.work-detail-header {
	box-sizing: border-box;
	flex: 0 0 auto;
	display: flex;
	min-height: 7rem;
	align-items: flex-start;
	justify-content: space-between;
	gap: 1rem;
	padding: 1rem 1rem .9rem 1.15rem;
	border-bottom: 1px solid var(--ob-border);
	background: var(--ob-header);
}
.work-detail-heading { min-width: 0; }
.work-detail-kicker { display: flex; align-items: center; gap: .38rem; color: var(--ob-text-subtle); font-size: 11px; font-weight: 750; letter-spacing: .08em; }
.work-detail-kicker svg { width: .85rem; height: .85rem; }
.work-detail-kicker.working svg { color: var(--ob-blue); animation: work-kicker-breathe 1.25s ease-in-out infinite; }
@keyframes work-kicker-breathe { 0%, 100% { opacity: .48; transform: scale(.9); } 50% { opacity: 1; transform: scale(1.06); } }
.work-detail-heading h2 { margin: .38rem 0 0; color: var(--ob-text-strong); font-size: 19px; font-weight: 720; letter-spacing: -.025em; }
.work-detail-heading p { max-width: 27rem; margin: .3rem 0 0; overflow: hidden; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
.work-detail-close { display: grid; width: 2rem; height: 2rem; flex: 0 0 auto; place-items: center; border: 0; border-radius: .65rem; background: transparent; color: var(--ob-text-subtle); cursor: pointer; transition: background .14s ease, color .14s ease; }
.work-detail-close:hover { background: var(--ob-hover); color: var(--ob-text-strong); }
.work-detail-close svg { width: .92rem; height: .92rem; }
.work-detail-body { box-sizing: border-box; display: flex; min-width: 0; min-height: 0; flex: 1; flex-direction: column; overflow: auto; padding: .85rem .9rem 1.4rem; overscroll-behavior-y: contain; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
.work-detail-body > * { flex: 0 0 auto; }
.work-detail-turn-preview { display: none; }
.work-detail-running { display: flex; min-height: 2rem; flex: 0 0 auto; align-items: center; margin-top: .58rem; padding: .8rem .2rem .1rem; }
.work-running-dots { position: relative; display: inline-flex; width: max-content; min-width: 1.74rem; height: 1.05rem; align-items: center; gap: .22rem; overflow: hidden; border-radius: 999px; padding: 0 .2rem; isolation: isolate; }
.work-running-dots::after { content: ""; position: absolute; inset: -45% -70%; z-index: -1; background: linear-gradient(105deg, transparent 28%, rgb(var(--ob-surface-rgb) / 0.88) 43%, rgb(var(--ob-text-muted-rgb) / 0.18) 50%, transparent 66%); transform: translateX(-46%); animation: work-sun-sweep 2.05s ease-in-out infinite; }
.work-running-dots span { display: block; width: .34rem; height: .34rem; border-radius: 999px; background: var(--ob-text-muted); box-shadow: 0 0 0 0 rgb(var(--ob-shadow-rgb) / 0.14), 0 0 10px rgb(var(--ob-shadow-rgb) / 0.14); animation: work-dot-breathe 1.18s ease-in-out infinite; }
.work-running-dots span:nth-child(2) { animation-delay: .16s; }
.work-running-dots span:nth-child(3) { animation-delay: .32s; }
@keyframes work-dot-breathe { 0%, 100% { opacity: .38; transform: translateY(.02rem) scale(.72); box-shadow: 0 0 0 0 rgb(var(--ob-shadow-rgb) / 0.1), 0 0 6px rgb(var(--ob-shadow-rgb) / 0.12); } 42% { opacity: .96; transform: translateY(-.02rem) scale(1.04); box-shadow: 0 0 0 4px rgb(var(--ob-shadow-rgb) / 0.1), 0 0 14px rgb(var(--ob-shadow-rgb) / 0.14); } }
@keyframes work-sun-sweep { 0% { transform: translateX(-48%); opacity: 0; } 28% { opacity: .75; } 58% { transform: translateX(50%); opacity: .5; } 100% { transform: translateX(50%); opacity: 0; } }
.work-detail-entry { display: flex; align-items: center; gap: .4rem; min-width: 0; }
.work-detail-entry + .work-detail-entry { margin-top: .6rem; }
.work-entry-time { display: inline-flex; height: 1.45rem; flex: 0 0 auto; align-self: flex-start; align-items: center; margin-top: .16rem; padding: 0; color: var(--ob-text-muted); font-size: 10.5px; font-variant-numeric: tabular-nums; line-height: 1; white-space: nowrap; }
.work-detail-entry > .work-event, .work-detail-entry > .work-reasoning { min-width: 0; flex: 1 1 0%; }
.work-detail-empty { display: flex; min-height: 17rem; align-items: center; justify-content: center; flex-direction: column; color: var(--ob-text-muted); text-align: center; }
.work-detail-empty > svg { width: 1.5rem; height: 1.5rem; margin-bottom: .7rem; }
.work-detail-empty strong { color: var(--ob-text); font-size: 14px; }
.work-detail-empty p { margin: .35rem 0 0; font-size: 12px; }
.work-reasoning { margin: .16rem 0; color: var(--ob-text-subtle); font-size: 11.5px; }
.work-reasoning summary { display: grid; grid-template-columns: auto minmax(0, auto) auto minmax(4rem, 1fr); align-items: center; gap: .34rem; min-width: 0; min-height: 1.45rem; padding: .04rem 0; color: var(--ob-text-subtle); cursor: pointer; list-style: none; }
.work-reasoning summary::-webkit-details-marker { display: none; }
.work-reasoning-icon { display: grid; width: .92rem; height: .92rem; place-items: center; color: var(--ob-text-muted); }
.work-reasoning-icon svg { width: .78rem; height: .78rem; }
.work-reasoning-name { min-width: 0; overflow: hidden; color: var(--ob-text-subtle); font-weight: 520; text-overflow: ellipsis; white-space: nowrap; }
.work-disclosure { display: none; width: 1rem; height: 1rem; place-items: center; color: var(--ob-text-subtle); }
.work-disclosure svg { width: .72rem; height: .72rem; transition: transform .14s ease; }
.work-reasoning summary:hover > .work-disclosure, .work-reasoning summary:focus-visible > .work-disclosure, .work-reasoning[open] > summary > .work-disclosure { display: grid; }
.work-reasoning[open] > summary > .work-disclosure { color: var(--ob-violet); }
.work-reasoning[open] > summary > .work-disclosure svg { transform: rotate(90deg); }
.work-reasoning-detail { margin: .16rem 0 .28rem; border: 1px solid var(--ob-border); border-radius: 9px; background: var(--ob-code-bg); padding: .54rem .62rem; }
.work-reasoning-body { max-height: min(340px, 48vh); overflow: auto; color: var(--ob-text); font-size: 12px; line-height: 1.55; overscroll-behavior: contain; scrollbar-width: thin; }
@media (max-width: 1280px) {
	/* This overlay shares the workspace with the composer (z-index 40).
	   End above its observed height instead of letting content scroll behind it. */
	.work-detail { position: absolute; inset: 0 0 var(--console-composer-height, 135px) auto; height: auto; z-index: 24; box-shadow: none; transform: translateX(100%); transition: transform .24s cubic-bezier(.22, 1, .36, 1), opacity .15s ease; }
	.work-detail.open { width: min(32rem, 88%); flex-basis: 0; transform: translateX(0); box-shadow: var(--ob-shadow-popover); }
}
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	/* Keep the close row touch-sized even with the keyboard open; the turn
	   preview moves into the scroll body rather than consuming fixed height. */
	.work-detail-header { min-height: 0; align-items: center; gap: .5rem; padding: .25rem .65rem; }
	.work-detail-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: .2rem .5rem; }
	.work-detail-heading h2 { margin: 0; }
	.work-detail-heading p { display: none; }
	.work-detail-turn-preview { display: block; margin: 0 0 .5rem; color: var(--ob-text-subtle); font-size: 12px; overflow-wrap: anywhere; }
	.work-detail-close { width: 44px; height: 44px; }
	.work-detail-body { padding: .5rem .75rem 1rem; }
	.work-detail-entry { display: block; }
	.work-detail-entry + .work-detail-entry { margin-top: 1rem; }
	.work-entry-time { display: flex; height: auto; margin: 0 0 .2rem; }
	.work-detail-entry > .work-event, .work-detail-entry > .work-reasoning { width: 100%; }
	.work-reasoning-detail { box-sizing: border-box; min-width: 0; max-width: 100%; }
	.work-reasoning-body { max-height: min(340px, calc(var(--mobile-viewport-height, 100dvh) * .45)); -webkit-overflow-scrolling: touch; }
}
@media (max-width: 760px) {
	/* Phone reading layer covers the composer rather than competing with it.
	   Closing restores the untouched chat/input underneath. */
	.work-detail { inset: 0; z-index: 50; }
	.work-detail.open { width: 100%; }
	.work-detail-body { padding-bottom: max(1rem, env(safe-area-inset-bottom, 0px)); }
}
@media (prefers-reduced-motion: reduce) {
	.work-detail, .work-disclosure { transition: none; }
	.work-detail-kicker.working svg { animation: none; opacity: .72; }
}
</style>
