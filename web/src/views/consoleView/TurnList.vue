<script setup>
import {Check, CopyDocument, RefreshLeft} from "@element-plus/icons-vue";
import {ElMessage} from "element-plus";
import {ref} from "vue";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {
	eventDisplayTimeMs as projectedEventDisplayTimeMs,
	eventStartedAtMs as projectedEventStartedAtMs,
	eventUpdatedAtMs as projectedEventUpdatedAtMs,
} from "../../timelineProjection.js";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import TurnEvent from "./TurnEvent.vue";
import {conversationTimelineEntries, shouldRenderAssistantDivider} from "./conversationTimeline.js";
import {eventPrimaryToolName, isAgentEvent, tokenLine, tokenPartsFromStats} from "./display.js";

const props = defineProps({
	turns: {type: Array, default: () => []},
	conversationUuid: {type: String, default: ""},
	running: {type: Boolean, default: false},
	deletingTurnUuid: {type: String, default: ""},
	autoScrollLocked: {type: Boolean, default: false},
	retryCancelPending: {type: Boolean, default: false},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	activeToolResultIndex: {type: Function, required: true},
});
const emit = defineEmits(["details-toggle", "reasoning-toggle", "select-tool-result", "delete-suffix", "cancel-retry"]);

function displayEvents(turn) {
	return Array.isArray(turn?.events) ? turn.events : [];
}

function conversationEvents(turn) {
	return conversationTimelineEntries(displayEvents(turn), eventPrimaryToolName, isAgentEvent);
}

function hasAssistantContent(turn) {
	return conversationEvents(turn).length > 0;
}

function showAssistantDivider(turn, conversationIndex) {
	return shouldRenderAssistantDivider(conversationEvents(turn), conversationIndex);
}

function canDeleteTurn(turn) {
	const turnUuid = String(turn?.user?.turnUuid || "").trim();
	const opId = String(turn?.user?.opId || turn?.user?.id || "");
	return Boolean(turnUuid) && Boolean(turn?.user?.deleteTraceable) && !opId.startsWith("local-");
}

function deleteTurnSuffix(turn) {
	if (!canDeleteTurn(turn) || props.running || props.deletingTurnUuid) return;
	emit("delete-suffix", turn);
}

function userAttachments(turn) {
	return Array.isArray(turn?.user?.attachments) ? turn.user.attachments : [];
}

function isImageAttachment(item) {
	return String(item?.kind || "").toLowerCase() === "image" || String(item?.mimeType || "").startsWith("image/");
}

function attachmentName(item) {
	return item?.fileName || item?.name || "附件";
}

function attachmentUrl(item) {
	return item?.previewUrl || item?.contentUrl || item?.downloadUrl || "";
}

function imagePreviewList(turn) {
	return userAttachments(turn)
		.filter((item) => isImageAttachment(item) && attachmentUrl(item))
		.map((item) => attachmentUrl(item));
}

function imagePreviewIndex(turn, item) {
	const url = attachmentUrl(item);
	return Math.max(0, imagePreviewList(turn).indexOf(url));
}

function emitDetailsToggle(event, key) {
	emit("details-toggle", event, key);
}

function emitReasoningToggle(event, key, active) {
	emit("reasoning-toggle", event, key, active);
}

function emitSelectToolResult(event, idx) {
	emit("select-tool-result", event, idx);
}

function isPersistentRunIndicator(event) {
	return event?.kind === "live_status" && Boolean(event?.persistentRunIndicator);
}

function liveTextMode(event) {
	if (event?.kind !== "answer") return "";
	if (event?.reasoningActive && String(event?.message?.reasoning || "").trim()) return "reasoning";
	if (event?.message?.live && String(event?.message?.content || "").trim()) return "answer";
	return "";
}

function activeLiveTextInfo(turn) {
	const events = displayEvents(turn);
	for (let i = events.length - 1; i >= 0; i--) {
		const event = events[i];
		if (isPersistentRunIndicator(event)) continue;
		const mode = liveTextMode(event);
		return mode ? {index: i, mode} : {index: -1, mode: ""};
	}
	return {index: -1, mode: ""};
}

function liveTextTargetForEvent(turn, idx) {
	const active = activeLiveTextInfo(turn);
	return active.index === idx ? active.mode : "";
}

function timeMsFromSeconds(value) {
	const n = Number(value || 0);
	if (!n) return 0;
	return n > 100000000000 ? n : n * 1000;
}

function eventStartedAtMs(event) {
	return projectedEventStartedAtMs(event);
}

function eventTimeMs(event) {
	return projectedEventDisplayTimeMs(event);
}

function userTimeMs(turn) {
	return timeMsFromSeconds(turn?.user?.createdAt || turn?.user?.created_at || turn?.startAt || turn?.startedAt);
}

function durationMsForEvent(event) {
	const direct = Number(event?.durationMs || event?.result?.durationMs || event?.operation?.payload?.durationMs || 0);
	if (direct > 0) return direct;
	const start = eventStartedAtMs(event);
	const end = projectedEventUpdatedAtMs(event);
	return start && end > start ? end - start : 0;
}

function formatHoverTime(ms) {
	const value = Number(ms || 0);
	if (!value) return "";
	const d = new Date(value);
	return d.toLocaleTimeString("zh-CN", {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function formatFullTime(ms) {
	const value = Number(ms || 0);
	if (!value) return "";
	return new Date(value).toLocaleString("zh-CN", {hour12: false});
}

function formatDuration(ms) {
	const value = Number(ms || 0);
	if (value <= 0) return "";
	if (value < 1000) return `${Math.round(value)}ms`;
	if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}s`;
	return `${Math.floor(value / 60000)}m ${Math.round((value % 60000) / 1000)}s`;
}

function timeBadge(ms, duration = 0) {
	const time = formatHoverTime(ms);
	const dur = formatDuration(duration);
	return dur ? `${time} · ${dur}` : time;
}

function turnTokenParts(turn) {
	return tokenPartsFromStats(turn?.stats);
}

function hasTurnTokens(turn) {
	const parts = turnTokenParts(turn);
	return Number(parts.input || 0) + Number(parts.output || 0) + Number(parts.cache || 0) > 0;
}

function turnTokenLine(turn) {
	return tokenLine(turnTokenParts(turn));
}

function assistantDurationMs(turn, event, conversationIndex) {
	if (conversationIndex !== conversationEvents(turn).length - 1) return 0;
	return Number(turn?.stats?.durationMs || 0) || durationMsForEvent(event);
}

const copiedMessageKey = ref("");
let copiedMessageTimer = 0;

function assistantTurnRawContent(turn) {
	return conversationEvents(turn)
		.filter(({event}) => event?.kind === "answer")
		.map(({event}) => String(event?.message?.content || "").trim())
		.filter(Boolean)
		.join("\n\n");
}

function assistantMetaVisible(event, turnIndex, conversationIndex, turn) {
	if (props.running && turnIndex === props.turns.length - 1) return false;
	if (conversationIndex !== conversationEvents(turn).length - 1) return false;
	return event?.kind === "answer" && !event?.message?.live && !event?.reasoningActive;
}

async function copyMessage(content, key) {
	const text = String(content || "");
	if (!text) return;
	try {
		await copyTextToClipboard(text);
		copiedMessageKey.value = key;
		window.clearTimeout(copiedMessageTimer);
		copiedMessageTimer = window.setTimeout(() => {
			if (copiedMessageKey.value === key) copiedMessageKey.value = "";
		}, 1400);
		ElMessage.success("消息已复制");
	} catch {
		ElMessage.error("复制失败，请手动选择文本");
	}
}
</script>

<template>
	<section v-for="(turn, turnIndex) in props.turns" :key="turn.id" class="turn-block" :data-turn-index="turnIndex">
		<div v-if="turn.user && !turn.user.syntheticPlaceholder" class="timed-row timed-row-user">
			<div class="user-row">
				<div class="user-message-group">
					<article class="message-user">
						<ConsoleMarkdown v-if="turn.user.content" :text="turn.user.content"/>
						<div v-if="userAttachments(turn).length" class="user-attachments" :class="{ 'with-text': turn.user.content }">
							<template v-for="item in userAttachments(turn)" :key="item.id || item.artifactUuid || attachmentName(item)">
							<el-image v-if="isImageAttachment(item) && attachmentUrl(item)"
							          class="user-attachment image"
							          :src="attachmentUrl(item)"
							          :alt="attachmentName(item)"
							          fit="cover"
							          :preview-src-list="imagePreviewList(turn)"
							          :initial-index="imagePreviewIndex(turn, item)"
							          preview-teleported
							          hide-on-click-modal/>
							<a v-else class="user-attachment" :href="attachmentUrl(item) || undefined" target="_blank" rel="noopener noreferrer"
							   :title="attachmentName(item)">
								<span class="file-tile">{{ attachmentName(item) }}</span>
							</a>
						</template>
					</div>
				</article>
				<div class="user-message-meta">
					<el-tooltip v-if="turn.user.content" content="复制消息" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action" aria-label="复制消息"
						        @click="copyMessage(turn.user.content, `user-${turn.user.turnUuid || turn.id}`)">
							<el-icon><Check v-if="copiedMessageKey === `user-${turn.user.turnUuid || turn.id}`"/><CopyDocument v-else/></el-icon>
						</button>
					</el-tooltip>
					<el-tooltip v-if="canDeleteTurn(turn)" :content="props.running ? '请先停止当前运行' : '从此处重来'" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action restart-action"
						        :disabled="props.running || Boolean(props.deletingTurnUuid)"
						        aria-label="从此处重来"
						        @click="deleteTurnSuffix(turn)">
							<el-icon><RefreshLeft/></el-icon>
						</button>
					</el-tooltip>
					<time v-if="userTimeMs(turn)" class="user-message-time" :title="formatFullTime(userTimeMs(turn))">{{ timeBadge(userTimeMs(turn)) }}</time>
				</div>
			</div>
		</div>
		
		</div>

		<div v-if="hasAssistantContent(turn)" class="assistant-row">
			<article class="assistant-card">
				<template v-for="(entry, conversationIndex) in conversationEvents(turn)" :key="entry.event.id || entry.event.eventKey || entry.event.message?.id || entry.event.operation?.opId || `${turn.id}-${entry.index}`">
					<ConsoleMarkdown v-if="showAssistantDivider(turn, conversationIndex)" class="assistant-update-divider" text="----"/>
					<div class="timed-row timed-row-assistant">
						<span v-if="eventTimeMs(entry.event) && !assistantMetaVisible(entry.event, turnIndex, conversationIndex, turn)" class="time-float time-float-left" :title="formatFullTime(eventTimeMs(entry.event))">{{ timeBadge(eventTimeMs(entry.event), durationMsForEvent(entry.event)) }}</span>
						<TurnEvent
						:event="entry.event"
						:conversation-uuid="props.conversationUuid"
						:turn-id="turn.id"
						:index="entry.index"
						:auto-scroll-locked="props.autoScrollLocked"
						:retry-cancel-pending="props.retryCancelPending"
						:live-text-target="liveTextTargetForEvent(turn, entry.index)"
						:detail-key="props.detailKey"
						:is-detail-open="props.isDetailOpen"
						:active-tool-result-index="props.activeToolResultIndex"
						:show-reasoning="false"
						@details-toggle="emitDetailsToggle"
						@reasoning-toggle="emitReasoningToggle"
						@select-tool-result="emitSelectToolResult"
						@cancel-retry="emit('cancel-retry', $event)"
					/>
					<div v-if="assistantMetaVisible(entry.event, turnIndex, conversationIndex, turn)" class="assistant-message-meta">
						<time v-if="eventTimeMs(entry.event)" class="assistant-message-time" :title="formatFullTime(eventTimeMs(entry.event))">{{ timeBadge(eventTimeMs(entry.event), assistantDurationMs(turn, entry.event, conversationIndex)) }}</time>
						<span v-if="hasTurnTokens(turn)" class="turn-token-usage" :title="`本轮 Tokens：${turnTokenLine(turn)}`">· {{ turnTokenLine(turn) }}</span>
						<el-tooltip v-if="assistantTurnRawContent(turn)" content="复制本轮回复" placement="bottom" :show-after="350">
							<button type="button" class="message-icon-action" aria-label="复制消息"
							        @click="copyMessage(assistantTurnRawContent(turn), `assistant-turn-${turn.turnUuid || turn.user?.turnUuid || turn.id}`)">
								<el-icon><Check v-if="copiedMessageKey === `assistant-turn-${turn.turnUuid || turn.user?.turnUuid || turn.id}`"/><CopyDocument v-else/></el-icon>
							</button>
						</el-tooltip>
					</div>
					</div>
				</template>
			</article>
		</div>
	</section>
</template>

<style scoped>
.turn-block {
	margin: 1.15rem 0 1.55rem;
}

.timed-row {
	position: relative;
	min-width: 0;
	overflow: visible;
}

.timed-row + .timed-row {
	margin-top: 0.16rem;
}

.time-float {
	position: absolute;
	top: 0.18rem;
	z-index: 3;
	display: inline-flex;
	align-items: center;
	height: 1.35rem;
	padding: 0 .5rem;
	border: 1px solid rgba(15, 23, 42, .08);
	border-radius: 999px;
	background: rgba(255, 255, 255, .92);
	color: #94a3b8;
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
	box-shadow: 0 12px 26px rgba(15, 23, 42, .08);
	opacity: 0;
	transform: translateY(2px);
	pointer-events: none;
	transition: opacity .14s ease, transform .14s ease;
}

.time-float-left {
	right: calc(100% + 12px);
}

.time-float-right {
	left: calc(100% + 12px);
}

.timed-row:hover > .time-float {
	opacity: 1;
	transform: translateY(0);
}

.user-row {
	display: flex;
	justify-content: flex-end;
	margin-bottom: 0.95rem;
}

.user-message-group {
	display: flex;
	max-width: min(78%, 640px);
	min-width: 0;
	flex-direction: column;
	align-items: flex-end;
}

.user-message-meta {
	display: flex;
	min-height: 1.45rem;
	align-items: center;
	justify-content: flex-end;
	gap: .34rem;
	padding: .22rem .18rem 0;
	color: #a1a1aa;
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
}

.user-message-time {
	color: #a1a1aa;
}

.message-icon-action {
	display: inline-grid;
	width: 22px;
	height: 22px;
	flex: 0 0 22px;
	place-items: center;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: #a1a1aa;
	cursor: pointer;
	transition: color .14s ease, background .14s ease;
}

.message-icon-action .el-icon {
	font-size: 12px;
}

.message-icon-action:hover:not(:disabled),
.message-icon-action:focus-visible:not(:disabled) {
	background: #f4f4f5;
	color: #52525b;
	outline: none;
}

.message-icon-action.restart-action:hover:not(:disabled),
.message-icon-action.restart-action:focus-visible:not(:disabled) {
	background: #fff5f4;
	color: #b42318;
}

.message-icon-action:disabled {
	cursor: not-allowed;
	color: #d4d4d8;
}

.message-user {
	max-width: 100%;
	border-radius: 18px;
	background: #f4f4f4;
	padding: 0.65rem 0.9rem;
	color: #111827;
	font-size: 14px;
	line-height: 1.72;
}

.user-attachments {
	display: grid;
	grid-template-columns: repeat(auto-fill, minmax(108px, 1fr));
	gap: 0.55rem;
	max-width: 360px;
}

.user-attachments.with-text {
	margin-top: 0.65rem;
}

.user-attachment {
	position: relative;
	display: grid;
	place-items: center;
	min-height: 88px;
	overflow: hidden;
	border: 1px solid rgba(15, 23, 42, 0.08);
	border-radius: 14px;
	background: rgba(255, 255, 255, 0.76);
	box-shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
	text-decoration: none;
	color: #334155;
}

.user-attachment.image {
	aspect-ratio: 1 / 1;
}

.user-attachment.image {
	cursor: zoom-in;
}

.user-attachment.image :deep(.el-image__inner) {
	width: 100%;
	height: 100%;
	object-fit: cover;
	display: block;
}

.file-tile {
	padding: 0.7rem;
	max-width: 100%;
	font-size: 12px;
	font-weight: 700;
	text-align: center;
	word-break: break-word;
}

.assistant-row {
	display: block;
	padding-left: 0;
	overflow: visible;
}

.assistant-message-meta {
	display: flex;
	min-height: 1.45rem;
	align-items: center;
	justify-content: flex-start;
	gap: .34rem;
	padding: .22rem .18rem 0;
	color: #a1a1aa;
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
}

.assistant-message-time,
.turn-token-usage {
	color: #a1a1aa;
}

.assistant-card {
	min-width: 0;
	flex: 1;
	max-width: 100%;
	font-size: 14px;
	line-height: 1.72;
	color: #111827;
}

.assistant-row:hover :deep(.turn-action-bar) {
	opacity: 1;
}

@media (max-width: 1120px) {
	.time-float {
		position: static;
		display: none;
		margin-bottom: .25rem;
		opacity: 0;
		transform: none;
	}
	.timed-row:hover > .time-float {
		display: inline-flex;
		opacity: 1;
	}
	.timed-row-user {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
	}
}
</style>
