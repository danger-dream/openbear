<script setup>
import {Check, CopyDocument, Link, Hide, Clock, Timer, Top, Bottom, Coin} from "@element-plus/icons-vue";
import {Undo2} from "@lucide/vue";
import {ElMessage} from "element-plus";
import {ref} from "vue";
import { contextResourceUrl } from '../../sessionBoundary.js';
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {referenceDisplayText} from "../../references/codec.js";
import {
	eventDisplayTimeMs as projectedEventDisplayTimeMs,
	eventStartedAtMs as projectedEventStartedAtMs,
	eventUpdatedAtMs as projectedEventUpdatedAtMs,
} from "../../timelineProjection.js";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import WebhookMessageCard from './WebhookMessageCard.vue';
import CronMessageCard from './CronMessageCard.vue';
import InstanceMessageCard from './InstanceMessageCard.vue';
import CollabMessageCard from './CollabMessageCard.vue';
import ReplyMarkdownShare from "./ReplyMarkdownShare.vue";
import {replyMarkdownText} from "./replyMarkdownShare.js";
import TurnEvent from "./TurnEvent.vue";
import ConversationWorkBlock from './ConversationWorkBlock.vue';
import ConversationProcessEvent from './ConversationProcessEvent.vue';
import ConversationRetryEvent from './ConversationRetryEvent.vue';
import {isInlineProcess} from './conversationWork.js';
import MessageVisibilityAction from './MessageVisibilityAction.vue';
import {useMessageVisibility, visibilitySelectionClasses, selectVisibilityRow} from './messageVisibility.js';
const visibility = useMessageVisibility();
import {conversationTimelineEntries, shouldRenderAssistantDivider} from "./conversationTimeline.js";
import {eventPrimaryToolName, isAgentEvent, tokenLine, tokenPartsFromStats, fmtTokens, cachePct} from "./display.js";
import {ListChecks} from "@lucide/vue";
import {isClosed, leaves, topNumber, turnTaskNumbers, useWorkItems} from "./workItems.js";

const props = defineProps({
	turns: {type: Array, default: () => []},
	conversationUuid: {type: String, default: ""},
	running: {type: Boolean, default: false},
	// A turn is in progress only while its controller is alive. Agents left
	// running after the controller ended (or failed) are background work: that
	// turn is finished, and new input starts the next turn.
	controllerRunning: {type: Boolean, default: false},
	// Turn the running controller writes to (a continuation block or its root); -1 = the latest.
	activeTurnIndex: {type: Number, default: -1},
	deletingTurnUuid: {type: String, default: ""},
	autoScrollLocked: {type: Boolean, default: false},
	retryActionPending: {type: Object, default: () => ({})},
	detailKey: {type: Function, required: true},
	isDetailOpen: {type: Function, required: true},
	activeToolResultIndex: {type: Function, required: true},
});
const emit = defineEmits(["details-toggle", "reasoning-toggle", "select-tool-result", "delete-suffix", "cancel-retry", "retry-now", "locate-turn"]);

function displayEvents(turn) {
	return Array.isArray(turn?.events) ? turn.events : [];
}

// Every projection hands a turn a brand-new event array, so the array identity
// is a safe memo key: while the same array is on screen its derived view cannot
// change, and any new projection invalidates it automatically. Caching this is
// what keeps a long turn from re-filtering its whole event list once per
// rendered row.
const turnViewMemo = new WeakMap();

function turnView(turn) {
	const events = displayEvents(turn);
	let view = turnViewMemo.get(events);
	if (!view) {
		view = {
			entries: conversationTimelineEntries(displayEvents(turn), eventPrimaryToolName, isAgentEvent),
			liveAnswer: events.some((event) => event?.kind === "answer" && (event?.message?.live || event?.reasoningActive)),
			liveText: activeLiveTextInfoFor(events),
		};
		turnViewMemo.set(events, view);
	}
	return view;
}

function conversationEvents(turn) {
	const view = turnView(turn);
	const hidden = visibility.hiddenIds.value;
	if (!hidden.size) return view.entries;
	if (view.hiddenIds !== hidden) {
		view.hiddenIds = hidden;
		view.visibleEntries = view.entries.filter(({event}) => !visibility.isHidden(event));
	}
	return view.visibleEntries;
}

function useProcessRow(entry) {
	return isInlineProcess(entry) && !isAgentEvent(entry.event);
}

function turnInProgress(turnIndex) {
	const active = props.activeTurnIndex >= 0 ? props.activeTurnIndex : props.turns.length - 1;
	return props.controllerRunning && turnIndex === active;
}

function continuationRootLabel(turn) {
	const info = turn?.user?.continuation || {};
	const date = new Date(Number(info.rootStartedAt || 0) * 1000);
	if (!info.rootStartedAt || Number.isNaN(date.getTime())) return "接续之前的一轮";
	const sameDay = date.toDateString() === new Date().toDateString();
	const time = date.toLocaleString("zh-CN", {hour12: false, ...(sameDay ? {} : {month: "2-digit", day: "2-digit"}), hour: "2-digit", minute: "2-digit"});
	return info.rootPreview ? `接续 ${time}「${info.rootPreview}」` : `接续 ${time} 的一轮`;
}

function turnWorking(turn, turnIndex) {
	return Boolean(turnInProgress(turnIndex) || turn?.stats?.live
		|| displayEvents(turn).some(event => event?.persistentRunIndicator) || turnView(turn).liveAnswer);
}

const workItems = useWorkItems();
function turnFocus(turn) {
	const uuid = String(turn?.user?.turnUuid || turn?.turnUuid || "");
	return uuid && workItems ? workItems.focusFor(uuid) : null;
}
function focusLabel(focus) {
	return (focus?.numbers || []).map(number => `#${number} ${workItems?.find(number)?.title || ""}`.trim()).join("、");
}
// One line per task this turn worked on, shown once the turn has finished.
function turnTaskChips(turn) {
	if (!workItems?.state.tasks.length) return [];
	const numbers = turnTaskNumbers(conversationEvents(turn).map(entry => entry.event));
	const focus = turnFocus(turn);
	for (const number of focus?.numbers || []) if (!numbers.includes(topNumber(number))) numbers.push(topNumber(number));
	return numbers.map(number => workItems.find(number)).filter(Boolean).map(task => {
		const live = (task.children?.length ? leaves(task) : [task]).filter(item => item.status !== "dropped");
		const roots = (focus?.numbers || []).filter(root => topNumber(root) === task.number);
		const left = roots.flatMap(root => { const node = workItems.find(root); return node ? leaves(node) : []; })
			.filter(item => !isClosed(item.status) && !["blocked", "ask"].includes(item.status));
		return {
			number: task.number, done: live.filter(item => item.status === "done").length, total: live.length,
			ask: live.filter(item => item.status === "ask").length,
			range: roots.map(root => `#${root}`).join("、"), left: left.length,
		};
	});
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

function insertTurnReference(turn) {
	if (!props.conversationUuid || !canDeleteTurn(turn)) return;
	const reference = {kind: 'turn', id: props.conversationUuid, itemId: turn.user.turnUuid, label: referenceDisplayText(turn.user.content || '本轮问答').replace(/\s+/g,' ').slice(0, 70), scope: 'full'};
	window.dispatchEvent(new CustomEvent('openbear:insert-reference', {detail: {reference}}));
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
	return contextResourceUrl(item?.previewUrl || item?.contentUrl || item?.downloadUrl || "");
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

function activeLiveTextInfoFor(events) {
	for (let i = events.length - 1; i >= 0; i--) {
		const event = events[i];
		if (isPersistentRunIndicator(event)) continue;
		const mode = liveTextMode(event);
		return mode ? {index: i, mode} : {index: -1, mode: ""};
	}
	return {index: -1, mode: ""};
}

function liveTextTargetForEvent(turn, idx) {
	const active = turnView(turn).liveText;
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

// A stream paint visits many row labels. Cache the exact existing formatting,
// rather than constructing thousands of locale formatters for unchanged times.
const timeLabels = new Map();
function formattedTimes(ms) {
	const value = Number(ms || 0);
	if (!value) return {short: '', full: ''};
	let labels = timeLabels.get(value);
	if (!labels) {
		const date = new Date(value);
		labels = {
			short: date.toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}),
			full: date.toLocaleString('zh-CN', {hour12: false}),
		};
		timeLabels.set(value, labels);
		if (timeLabels.size > 2048) timeLabels.delete(timeLabels.keys().next().value);
	}
	return labels;
}
function formatHoverTime(ms) { return formattedTimes(ms).short; }
function formatFullTime(ms) { return formattedTimes(ms).full; }

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
	return [time, dur].filter(Boolean).join(" · ");
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

function assistantTimeMs(turn) {
	const entries = conversationEvents(turn);
	for (let i = entries.length - 1; i >= 0; i--) {
		const time = eventTimeMs(entries[i].event);
		if (time) return time;
	}
	return 0;
}

function assistantDurationMs(turn) {
	return Number(turn?.stats?.durationMs || 0) || durationMsForEvent(conversationEvents(turn).at(-1)?.event);
}

const copiedMessageKey = ref("");
let copiedMessageTimer = 0;

function assistantTurnRawContent(turn) {
	return conversationEvents(turn)
		.filter(({event, part}) => event?.kind === "answer" && part !== 'reasoning')
		.map(({event}) => String(event?.message?.content || "").trim())
		.filter(Boolean)
		.join("\n\n");
}

function assistantTurnShareContent(turn) {
	return replyMarkdownText(conversationEvents(turn));
}

function assistantVisibilityTarget(turn) {
	const targets = visibility.assistantTargets?.(turn) || [];
	return targets.findLast(event => event.kind === 'answer' && event.message?.content) || targets.at(-1) || null;
}

function assistantMetaVisible(turn, turnIndex) {
	if (!hasAssistantContent(turn) && displayEvents(turn).some(visibility.isHidden)) return false;
	if (turnInProgress(turnIndex)) return false;
	if (turn?.stats?.live) return false;
	if (turnView(turn).liveAnswer) return false;
	// Completion statistics belong to the whole turn, even when compaction or
	// another visible operation follows the final answer (or no text was sent).
	return hasAssistantContent(turn) || hasTurnTokens(turn) || assistantDurationMs(turn) > 0;
}

function showEventTimeBadge(turn, turnIndex, conversationIndex) {
	const entries = conversationEvents(turn);
	return !(assistantMetaVisible(turn, turnIndex)
		&& conversationIndex === entries.length - 1
		&& entries[conversationIndex]?.event?.kind === "answer");
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
	<template v-for="(turn, turnIndex) in props.turns" :key="turn.id">
	<section v-if="(turn.user && !turn.user.syntheticPlaceholder && !visibility.isHidden(turn.user)) || hasAssistantContent(turn) || assistantMetaVisible(turn, turnIndex)" class="turn-block" :data-turn-index="turnIndex">
		<div v-if="turn.user?.continuation" class="turn-continuation">
			<span class="turn-continuation-label">后台任务完成后继续</span>
			<button v-if="turn.user.continuation.rootUserOpId" type="button" class="turn-continuation-root"
			        :title="`查看原提问：${turn.user.continuation.rootPreview || ''}`"
			        @click="emit('locate-turn', turn.user.continuation.rootUserOpId)">{{ continuationRootLabel(turn) }}</button>
			<span v-else class="turn-continuation-root">{{ continuationRootLabel(turn) }}</span>
		</div>
		<div v-if="turn.user && !turn.user.syntheticPlaceholder && !visibility.isHidden(turn.user)" class="timed-row timed-row-user visibility-hover-surface" :data-search-op-id="turn.user.opId || turn.user.id" :class="[visibilitySelectionClasses(turn.user, visibility), {'timed-row-event': ['webhook', 'cron', 'instance', 'collab'].includes(turn.user.source)}]" @click.capture="selectVisibilityRow($event, turn.user, visibility)">
			<div class="user-row">
				<div class="user-message-group">
					<WebhookMessageCard v-if="turn.user.source === 'webhook'" :message="turn.user"/>
					<CronMessageCard v-else-if="turn.user.source === 'cron'" :message="turn.user"/>
					<InstanceMessageCard v-else-if="turn.user.source === 'instance'" :message="turn.user"/>
					<CollabMessageCard v-else-if="turn.user.source === 'collab'" :message="turn.user"/>
					<article v-else class="message-user">
						<ConsoleMarkdown v-if="turn.user.content" :text="turn.user.content" :reference-bundle-id="turn.user.referenceBundleId || ''" :references="turn.user.references || []"/>
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
				<div v-if="!['webhook', 'cron', 'instance', 'collab'].includes(turn.user.source)" class="user-message-meta">
					<MessageVisibilityAction :target="turn.user" desktop-placement="footer" :turn="turn" mobile-selection-only/>
					<el-tooltip v-if="turn.user.content" content="复制消息" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action" aria-label="复制消息"
						        @click="copyMessage(turn.user.content, `user-${turn.user.turnUuid || turn.id}`)">
							<el-icon><Check v-if="copiedMessageKey === `user-${turn.user.turnUuid || turn.id}`"/><CopyDocument v-else/></el-icon>
						</button>
					</el-tooltip>
					<button v-if="!visibility.selecting.value && visibility.canTarget(turn.user)" type="button"
					        class="message-icon-action message-hide-action message-visibility-action" aria-label="隐藏消息" aria-haspopup="dialog"
					        :disabled="visibility.busy.value" @click.stop="visibility.openMobileMenu(turn.user, turn)"><el-icon><Hide/></el-icon></button>
					<el-tooltip v-if="canDeleteTurn(turn)" content="引用本轮问答" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action" aria-label="引用本轮问答" @click="insertTurnReference(turn)"><el-icon><Link/></el-icon></button>
					</el-tooltip>
					<el-tooltip v-if="canDeleteTurn(turn)" :content="props.running ? '请先停止当前运行' : '从此处重来'" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action restart-action"
						        :disabled="props.running || Boolean(props.deletingTurnUuid)"
						        aria-label="从此处重来"
						        @click="deleteTurnSuffix(turn)">
							<el-icon><Undo2 size="1em" :stroke-width="1.8" aria-hidden="true"/></el-icon>
						</button>
					</el-tooltip>
					<time v-if="userTimeMs(turn)" class="user-message-time" :title="formatFullTime(userTimeMs(turn))">{{ timeBadge(userTimeMs(turn)) }}</time>
				</div>
			</div>
		</div>
		
		</div>

		<div v-if="hasAssistantContent(turn) || assistantMetaVisible(turn, turnIndex)" class="assistant-row">
			<article class="assistant-card">
				<button v-if="turnFocus(turn)" type="button" class="wi-turn-focus" @click="workItems.open(turnFocus(turn).numbers[0])">
					<ListChecks :stroke-width="1.7" aria-hidden="true"/>本轮执行范围：<b>{{ focusLabel(turnFocus(turn)) }}</b>
				</button>
				<ConversationWorkBlock :turn="turn" :entries="conversationEvents(turn)" :running="turnWorking(turn, turnIndex)" :duration-ms="assistantDurationMs(turn)">
				<template #default="{entry, conversationIndex}">
					<div class="timed-row timed-row-assistant visibility-hover-surface" :data-search-op-id="(entry.part !== 'reasoning' && entry.event.kind === 'answer') || entry.event.kind === 'live_status' ? entry.event.id : undefined" :class="[visibilitySelectionClasses(entry.event, visibility), {'has-inline-retry': entry.event.kind === 'model_retry'}]" @click.capture="selectVisibilityRow($event, entry.event, visibility)">
						<span v-if="eventTimeMs(entry.event) && showEventTimeBadge(turn, turnIndex, conversationIndex)" class="time-float time-float-left" :title="formatFullTime(eventTimeMs(entry.event))">{{ timeBadge(eventTimeMs(entry.event), durationMsForEvent(entry.event)) }}</span>
						<ConversationRetryEvent v-if="entry.event.kind === 'model_retry'" :event="entry.event" :retry-action-pending="props.retryActionPending" @cancel-retry="emit('cancel-retry', $event)" @retry-now="emit('retry-now', $event)"/>
						<ConversationProcessEvent v-else-if="useProcessRow(entry)" :event="entry.event" :part="entry.part || ''" :conversation-uuid="props.conversationUuid" :active-index="props.activeToolResultIndex(entry.event)" @select-tab="emitSelectToolResult(entry.event, $event)"/>
						<TurnEvent v-else
						process-row
						:event="entry.event"
						:conversation-uuid="props.conversationUuid"
						:turn-id="turn.id"
						:index="entry.index"
						:auto-scroll-locked="props.autoScrollLocked"
						:reasoning-autoscroll="Boolean(entry.event.reasoningActive) && props.autoScrollLocked"
						:retry-action-pending="props.retryActionPending"
						:live-text-target="liveTextTargetForEvent(turn, entry.index)"
						:detail-key="props.detailKey"
						:is-detail-open="props.isDetailOpen"
						:active-tool-result-index="props.activeToolResultIndex"
						:show-reasoning="false"
						@details-toggle="emitDetailsToggle"
						@reasoning-toggle="emitReasoningToggle"
						@select-tool-result="emitSelectToolResult"
						@cancel-retry="emit('cancel-retry', $event)"
						@retry-now="emit('retry-now', $event)"
					/>
					<MessageVisibilityAction :target="entry.event" desktop-placement="gutter" :turn="turn" mobile-selection-only/>
					</div>
				</template>
				</ConversationWorkBlock>
				<div v-if="!turnWorking(turn, turnIndex) && turnTaskChips(turn).length" class="wi-turn-chips">
					<button v-for="chip in turnTaskChips(turn)" :key="chip.number" type="button" class="wi-turn-chip" @click="workItems.open(chip.number)">
						<ListChecks :stroke-width="1.7" aria-hidden="true"/><b>任务 #{{ chip.number }}</b><span>{{ chip.done }}/{{ chip.total }} 完成</span>
						<template v-if="chip.range && chip.left"><span class="wi-turn-sep">·</span><span class="wi-turn-warn">本轮范围 {{ chip.range }} 还剩 {{ chip.left }} 项没收尾</span></template>
						<template v-if="chip.ask"><span class="wi-turn-sep">·</span><span class="wi-turn-warn">{{ chip.ask }} 项等你定</span></template>
					</button>
				</div>
				<div v-if="assistantMetaVisible(turn, turnIndex)" class="assistant-message-meta">
					<span class="assistant-meta-details">
						<time v-if="assistantTimeMs(turn) || assistantDurationMs(turn)" class="assistant-message-time" :title="formatFullTime(assistantTimeMs(turn))"><span v-if="assistantTimeMs(turn)" class="footer-metric" title="回复时间"><Clock class="footer-meta-icon" aria-hidden="true"/>{{ formatHoverTime(assistantTimeMs(turn)) }}</span><span v-if="assistantTimeMs(turn) && assistantDurationMs(turn)"> · </span><span v-if="assistantDurationMs(turn)" class="footer-metric" title="耗时"><Timer class="footer-meta-icon" aria-hidden="true"/>{{ formatDuration(assistantDurationMs(turn)) }}</span></time>
						<span v-if="hasTurnTokens(turn)" class="turn-token-usage" :title="`本轮 Tokens：${turnTokenLine(turn)}`"><span v-if="assistantTimeMs(turn) || assistantDurationMs(turn)" class="assistant-meta-separator">· </span><span class="footer-metric" :aria-label="`输入 Tokens：${fmtTokens(turnTokenParts(turn).input)}`"><Top class="footer-meta-icon" aria-hidden="true"/><span class="footer-token-arrow">↑</span>{{ fmtTokens(turnTokenParts(turn).input) }}</span><span class="footer-metric-separator"> · </span><span class="footer-metric" :aria-label="`输出 Tokens：${fmtTokens(turnTokenParts(turn).output)}`"><Bottom class="footer-meta-icon" aria-hidden="true"/><span class="footer-token-arrow">↓</span>{{ fmtTokens(turnTokenParts(turn).output) }}</span><span class="footer-metric-separator"> · </span><span class="footer-metric" :aria-label="`缓存 Tokens：${fmtTokens(turnTokenParts(turn).cache)}，命中率 ${cachePct(turnTokenParts(turn).cache, turnTokenParts(turn).input)}`"><Coin class="footer-meta-icon" aria-hidden="true"/>缓存 {{ fmtTokens(turnTokenParts(turn).cache) }}（{{ cachePct(turnTokenParts(turn).cache, turnTokenParts(turn).input) }}）</span></span>
					</span>
					<span class="message-footer-actions">
					<el-tooltip v-if="assistantTurnRawContent(turn)" content="复制本轮回复" placement="bottom" :show-after="350">
						<button type="button" class="message-icon-action" aria-label="复制消息"
						        @click="copyMessage(assistantTurnRawContent(turn), `assistant-turn-${turn.turnUuid || turn.user?.turnUuid || turn.id}`)">
							<el-icon><Check v-if="copiedMessageKey === `assistant-turn-${turn.turnUuid || turn.user?.turnUuid || turn.id}`"/><CopyDocument v-else/></el-icon>
						</button>
					</el-tooltip>
					<ReplyMarkdownShare v-if="!visibility.selecting.value && assistantTurnShareContent(turn)" :content="assistantTurnShareContent(turn)"/>
					<button v-if="!visibility.selecting.value && assistantVisibilityTarget(turn)" type="button"
					        class="message-icon-action message-hide-action message-visibility-action" aria-label="隐藏回复" aria-haspopup="dialog"
					        :disabled="visibility.busy.value" @click.stop="visibility.openMobileMenu(assistantVisibilityTarget(turn), turn)"><el-icon><Hide/></el-icon></button>
					</span>
				</div>
			</article>
		</div>
	</section>
	</template>
</template>

<style scoped>
.wi-turn-focus { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 0 0 10px; padding: 0; border: 0; background: transparent; color: var(--ob-chat-text); font-size: 12px; line-height: 1.6; text-align: left; cursor: pointer; }
.wi-turn-focus svg, .wi-turn-chip svg { width: 14px; height: 14px; flex: none; color: var(--ob-chat-subtle); }
.wi-turn-focus b { font-weight: 500; }
.wi-turn-focus:hover b, .wi-turn-chip:hover b { text-decoration: underline; }
.wi-turn-chips { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; margin-top: 6px; }
.wi-turn-chip { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 0; border: 0; background: transparent; font-size: 12px; line-height: 1.6; color: var(--ob-chat-subtle); text-align: left; cursor: pointer; }
.wi-turn-chip b { color: var(--ob-chat-text); font-weight: 500; }
.wi-turn-sep { color: var(--ob-chat-muted); }
.wi-turn-warn { color: var(--ob-orange); }

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

.conversation-work .timed-row + .timed-row { margin-top: 0; }

.time-float {
	position: absolute;
	top: 0.18rem;
	z-index: 3;
	display: inline-flex;
	align-items: center;
	height: 1.35rem;
	padding: 0 .5rem;
	border: 1px solid var(--ob-border);
	border-radius: 999px;
	background: var(--ob-surface-raised);
	color: var(--ob-text-muted);
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
	box-shadow: var(--ob-shadow-popover);
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
	width: 100%;
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

.timed-row-event .user-row { justify-content: flex-start; }
.timed-row-event .user-message-group { align-items: flex-start; width: min(100%, 520px); max-width: 100%; }
.timed-row-event .user-message-meta { justify-content: flex-start; }

.user-message-meta {
	display: flex;
	min-height: 1.45rem;
	align-items: center;
	justify-content: flex-end;
	gap: .34rem;
	padding: .22rem .18rem 0;
	color: var(--ob-text-muted);
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
}

.user-message-time {
	color: var(--ob-text-muted);
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
	color: var(--ob-text-muted);
	cursor: pointer;
	transition: color .14s ease, background .14s ease;
}

.message-hide-action { display: none; }
.message-footer-actions { display: inline-flex; align-items: center; gap: .34rem; flex: none; }

.message-icon-action .el-icon {
	font-size: 12px;
}

.message-icon-action:hover:not(:disabled),
.message-icon-action:focus-visible:not(:disabled) {
	background: var(--ob-hover);
	color: var(--ob-text-disabled);
	outline: none;
}

.message-icon-action.restart-action:hover:not(:disabled),
.message-icon-action.restart-action:focus-visible:not(:disabled) {
	background: var(--ob-danger-soft);
	color: var(--ob-danger);
}

.message-icon-action:disabled {
	cursor: not-allowed;
	color: var(--ob-text-disabled);
}

.message-user {
	min-width: 0;
	max-width: 100%;
	border-radius: 13px;
	background: var(--ob-chat-bubble);
	padding: 0.85rem 1.1rem;
	color: var(--ob-chat-text);
	font-size: 13px;
	line-height: 1.85;
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
	border: 1px solid var(--ob-border);
	border-radius: 14px;
	background: rgb(var(--ob-surface-rgb) / 0.76);
	box-shadow: var(--ob-shadow-panel);
	text-decoration: none;
	color: var(--ob-text);
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

.turn-continuation {
	display: flex;
	align-items: baseline;
	gap: .45rem;
	min-width: 0;
	margin: 0 0 .45rem;
	color: var(--ob-text-muted);
	font-size: 12px;
	line-height: 1.5;
}

.turn-continuation-label { flex: none; }

.turn-continuation-root {
	min-width: 0;
	overflow: hidden;
	padding: 0;
	border: 0;
	background: none;
	color: inherit;
	font: inherit;
	text-align: left;
	text-overflow: ellipsis;
	white-space: nowrap;
}

button.turn-continuation-root {
	cursor: pointer;
	text-decoration: underline;
	text-decoration-color: var(--ob-border);
	text-underline-offset: 3px;
}

button.turn-continuation-root:hover { color: var(--ob-text); }

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
	color: var(--ob-text-muted);
	font-size: 10.5px;
	font-variant-numeric: tabular-nums;
	line-height: 1;
	white-space: nowrap;
}

.assistant-meta-details { display: contents; }
.footer-meta-icon { display: none; }

.assistant-message-time,
.turn-token-usage {
	color: var(--ob-text-muted);
}

.assistant-card {
	min-width: 0;
	flex: 1;
	max-width: 100%;
	font-size: 13px;
	line-height: 1.85;
	color: var(--ob-chat-text);
}

@media (max-width: 760px) {
	.message-user { font-size: 14px; padding: 13px 15px; border-radius: 14px; }
	.assistant-card { font-size: 14px; line-height: 1.95; }
}

/* Only the standalone separator between answer entries, not rules inside a message. */
.assistant-card > :deep(.bear-md > hr:only-child) {
	opacity: 0.4;
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

@media (min-width: 761px) {
	/* Keep hover active while the pointer crosses into the outside action gutter. */
	.visibility-hover-surface::before { content: ''; position: absolute; left: -32px; top: 0; bottom: 0; width: 32px; }
	.visibility-selectable { cursor: pointer; }
	/* Selection lives in the gutter: keep the original message surface untouched. */
	.visibility-selected::after { content: ''; position: absolute; left: -19px; top: 29px; bottom: 4px; width: 2px; border-radius: 2px; background: var(--ob-text-muted); pointer-events: none; }
	.visibility-selectable > .time-float { display: none; }
	/* Leave the message-action gutter clear of the existing hover timestamp. */
	.time-float-left { right: calc(100% + 42px); }
}
@media (min-width: 761px) and (hover: hover) and (pointer: fine) {
	.visibility-hover-surface:hover :deep(.message-visibility-action),
	.visibility-hover-surface:focus-within :deep(.message-visibility-action) { opacity: 1; pointer-events: auto; }
}

@media (max-width: 760px) {
	.message-hide-action { display: inline-grid; width: 32px; height: 32px; flex-basis: 32px; }
	.assistant-message-meta { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; gap: 2px; white-space: normal; }
	.assistant-meta-details { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 3px 8px; line-height: 1.45; }
	.assistant-message-time { white-space: nowrap; }
	.assistant-meta-separator, .footer-token-arrow, .footer-metric-separator { display: none; }
	.footer-metric { display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; vertical-align: middle; }
	.footer-meta-icon { display: block; width: 11px; height: 11px; flex: 0 0 11px; opacity: .8; }
	.turn-token-usage { display: contents; }
	.message-footer-actions { flex-wrap: nowrap; justify-self: start; align-self: start; gap: 4px; }
	.message-footer-actions .message-icon-action { width: 32px; height: 32px; flex-basis: 32px; }
	/* Identify the menu's target without changing its width or flow. */
	.visibility-menu-target { border-radius: 10px; background: rgb(var(--ob-surface-soft-rgb) / 0.08); box-shadow: 0 0 0 1px rgb(var(--ob-shadow-rgb) / 0.14); }
	/* Normal reading uses the full width; the check rail exists only in multi-select. */
	.timed-row.visibility-selectable { box-sizing: border-box; padding-left: 32px; }
	/* Compact retry summaries have their own line height: align both controls in
	   the same centered row instead of reusing the prose baseline offset. */
	.timed-row-assistant.visibility-selectable.has-inline-retry { display: flex; align-items: center; min-height: 44px; }
	.timed-row-assistant.visibility-selectable.has-inline-retry > :deep(.message-visibility-action) { top: 50%; transform: translateY(-50%); }
	.timed-row-assistant.visibility-selectable > :deep(.retry-inline-event) { width: 100%; min-width: 0; }
	.visibility-selected::after { content: ''; position: absolute; left: 9px; top: 34px; bottom: 4px; width: 2px; border-radius: 2px; background: var(--el-border-color); pointer-events: none; }
	.visibility-selectable { cursor: pointer; -webkit-tap-highlight-color: transparent; }
}

/* Touch browsers can retain :hover after a tap; never insert a time badge
   above the message on mobile. Persistent footer metadata is unchanged. */
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.user-message-group {
		max-width: 100%;
	}
	.time-float,
	.timed-row:hover > .time-float {
		display: none;
	}
}
</style>
