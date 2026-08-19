<script setup>
import {computed} from "vue";
import {ArrowRight, Close, MagicStick} from "@element-plus/icons-vue";
import {eventDisplayTimeMs} from "../../timelineProjection.js";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import TurnEvent from "./TurnEvent.vue";
import WorkDetailIcon from "./WorkDetailIcon.vue";

const props = defineProps({
	open: {type: Boolean, default: false},
	turn: {type: Object, default: null},
	turnIndex: {type: Number, default: 0},
	conversationUuid: {type: String, default: ""},
	autoScrollLocked: {type: Boolean, default: false},
	retryCancelPending: {type: Boolean, default: false},
	working: {type: Boolean, default: false},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	activeToolResultIndex: {type: Function, required: true},
});

const emit = defineEmits(["close", "details-toggle", "reasoning-toggle", "select-tool-result", "cancel-retry"]);

const sourceEvents = computed(() => Array.isArray(props.turn?.events) ? props.turn.events : []);
const workEntries = computed(() => {
	const rows = [];
	for (const [index, event] of sourceEvents.value.entries()) {
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
const userPreview = computed(() => String(props.turn?.user?.content || "").replace(/\s+/g, " ").trim().slice(0, 72));

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
					<p v-if="userPreview" :title="String(props.turn?.user?.content || '')">{{ userPreview }}</p>
				</div>
				<button type="button" class="work-detail-close" aria-label="关闭工作详情" @click="emit('close')"><Close/></button>
			</header>

			<div class="work-detail-body">
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
							:retry-cancel-pending="props.retryCancelPending"
							:agent-preview-only="true"
							:compact="true"
							:detail-key="props.detailKey"
							:is-detail-open="props.isDetailOpen"
							:active-tool-result-index="props.activeToolResultIndex"
							@details-toggle="relayDetailsToggle"
							@reasoning-toggle="relayReasoningToggle"
							@select-tool-result="relayToolResult"
							@cancel-retry="emit('cancel-retry', $event)"
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
	position: relative;
	flex: 0 0 0;
	width: 0;
	min-width: 0;
	height: 100%;
	overflow: hidden;
	border-left: 1px solid transparent;
	background: #fff;
	opacity: 0;
	transition: flex-basis .24s cubic-bezier(.22, 1, .36, 1), width .24s cubic-bezier(.22, 1, .36, 1), opacity .15s ease, border-color .2s ease;
}
.work-detail.open {
	flex-basis: clamp(24rem, 32vw, 32rem);
	width: clamp(24rem, 32vw, 32rem);
	border-left-color: rgba(15, 23, 42, .1);
	opacity: 1;
}
.work-detail-surface {
	display: flex;
	width: clamp(24rem, 32vw, 32rem);
	height: 100%;
	flex-direction: column;
	background: rgba(250, 250, 250, .98);
}
.work-detail-header {
	display: flex;
	min-height: 7rem;
	align-items: flex-start;
	justify-content: space-between;
	gap: 1rem;
	padding: 1rem 1rem .9rem 1.15rem;
	border-bottom: 1px solid rgba(15, 23, 42, .09);
	background: rgba(255, 255, 255, .96);
}
.work-detail-heading { min-width: 0; }
.work-detail-kicker { display: flex; align-items: center; gap: .38rem; color: #64748b; font-size: 11px; font-weight: 750; letter-spacing: .08em; }
.work-detail-kicker svg { width: .85rem; height: .85rem; }
.work-detail-kicker.working svg { color: #2563eb; animation: work-kicker-breathe 1.25s ease-in-out infinite; }
@keyframes work-kicker-breathe { 0%, 100% { opacity: .48; transform: scale(.9); } 50% { opacity: 1; transform: scale(1.06); } }
.work-detail-heading h2 { margin: .38rem 0 0; color: #18181b; font-size: 19px; font-weight: 720; letter-spacing: -.025em; }
.work-detail-heading p { max-width: 27rem; margin: .3rem 0 0; overflow: hidden; color: #71717a; font-size: 12px; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
.work-detail-close { display: grid; width: 2rem; height: 2rem; flex: 0 0 auto; place-items: center; border: 0; border-radius: .65rem; background: transparent; color: #71717a; cursor: pointer; transition: background .14s ease, color .14s ease; }
.work-detail-close:hover { background: #f4f4f5; color: #18181b; }
.work-detail-close svg { width: .92rem; height: .92rem; }
.work-detail-body { display: flex; min-height: 0; flex: 1; flex-direction: column; overflow-y: auto; padding: .85rem .9rem 1.4rem; scrollbar-width: thin; }
.work-detail-body > * { flex: 0 0 auto; }
.work-detail-running { display: flex; min-height: 2rem; flex: 0 0 auto; align-items: center; margin-top: .58rem; padding: .8rem .2rem .1rem; }
.work-running-dots { position: relative; display: inline-flex; width: max-content; min-width: 1.74rem; height: 1.05rem; align-items: center; gap: .22rem; overflow: hidden; border-radius: 999px; padding: 0 .2rem; isolation: isolate; }
.work-running-dots::after { content: ""; position: absolute; inset: -45% -70%; z-index: -1; background: linear-gradient(105deg, transparent 28%, rgba(255, 255, 255, .88) 43%, rgba(148, 163, 184, .18) 50%, transparent 66%); transform: translateX(-46%); animation: work-sun-sweep 2.05s ease-in-out infinite; }
.work-running-dots span { display: block; width: .34rem; height: .34rem; border-radius: 999px; background: #a1a1aa; box-shadow: 0 0 0 0 rgba(161, 161, 170, .20), 0 0 10px rgba(148, 163, 184, .18); animation: work-dot-breathe 1.18s ease-in-out infinite; }
.work-running-dots span:nth-child(2) { animation-delay: .16s; }
.work-running-dots span:nth-child(3) { animation-delay: .32s; }
@keyframes work-dot-breathe { 0%, 100% { opacity: .38; transform: translateY(.02rem) scale(.72); box-shadow: 0 0 0 0 rgba(161, 161, 170, .10), 0 0 6px rgba(148, 163, 184, .12); } 42% { opacity: .96; transform: translateY(-.02rem) scale(1.04); box-shadow: 0 0 0 4px rgba(161, 161, 170, .10), 0 0 14px rgba(148, 163, 184, .32); } }
@keyframes work-sun-sweep { 0% { transform: translateX(-48%); opacity: 0; } 28% { opacity: .75; } 58% { transform: translateX(50%); opacity: .5; } 100% { transform: translateX(50%); opacity: 0; } }
.work-detail-entry { display: flex; align-items: center; gap: .4rem; min-width: 0; }
.work-detail-entry + .work-detail-entry { margin-top: .6rem; }
.work-entry-time { display: inline-flex; height: 1.45rem; flex: 0 0 auto; align-self: flex-start; align-items: center; margin-top: .16rem; padding: 0; color: #a1a1aa; font-size: 10.5px; font-variant-numeric: tabular-nums; line-height: 1; white-space: nowrap; }
.work-detail-entry > .work-event, .work-detail-entry > .work-reasoning { min-width: 0; flex: 1 1 auto; }
.work-detail-empty { display: flex; min-height: 17rem; align-items: center; justify-content: center; flex-direction: column; color: #a1a1aa; text-align: center; }
.work-detail-empty > svg { width: 1.5rem; height: 1.5rem; margin-bottom: .7rem; }
.work-detail-empty strong { color: #52525b; font-size: 14px; }
.work-detail-empty p { margin: .35rem 0 0; font-size: 12px; }
.work-reasoning { margin: .16rem 0; color: #71717a; font-size: 11.5px; }
.work-reasoning summary { display: grid; grid-template-columns: auto minmax(0, auto) auto minmax(4rem, 1fr); align-items: center; gap: .34rem; min-width: 0; min-height: 1.45rem; padding: .04rem 0; color: #71717a; cursor: pointer; list-style: none; }
.work-reasoning summary::-webkit-details-marker { display: none; }
.work-reasoning-icon { display: grid; width: .92rem; height: .92rem; place-items: center; color: #a1a1aa; }
.work-reasoning-icon svg { width: .78rem; height: .78rem; }
.work-reasoning-name { min-width: 0; overflow: hidden; color: #64748b; font-weight: 520; text-overflow: ellipsis; white-space: nowrap; }
.work-disclosure { display: none; width: 1rem; height: 1rem; place-items: center; color: #64748b; }
.work-disclosure svg { width: .72rem; height: .72rem; transition: transform .14s ease; }
.work-reasoning summary:hover > .work-disclosure, .work-reasoning summary:focus-visible > .work-disclosure, .work-reasoning[open] > summary > .work-disclosure { display: grid; }
.work-reasoning[open] > summary > .work-disclosure { color: #4338ca; }
.work-reasoning[open] > summary > .work-disclosure svg { transform: rotate(90deg); }
.work-reasoning-detail { margin: .16rem 0 .28rem; border: 1px solid #eceff3; border-radius: 9px; background: #fff; padding: .54rem .62rem; }
.work-reasoning-body { max-height: min(340px, 48vh); overflow: auto; color: #52525b; font-size: 12px; line-height: 1.55; overscroll-behavior: contain; scrollbar-width: thin; }
@media (max-width: 1280px) {
	.work-detail { position: absolute; inset: 0 0 0 auto; z-index: 24; box-shadow: none; transform: translateX(100%); transition: transform .24s cubic-bezier(.22, 1, .36, 1), opacity .15s ease; }
	.work-detail.open { width: min(32rem, 88%); flex-basis: 0; transform: translateX(0); box-shadow: -20px 0 54px rgba(15, 23, 42, .16); }
	.work-detail-surface { width: min(32rem, 88vw); }
}
@media (prefers-reduced-motion: reduce) {
	.work-detail, .work-disclosure { transition: none; }
	.work-detail-kicker.working svg { animation: none; opacity: .72; }
}
</style>
