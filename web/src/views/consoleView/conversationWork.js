import {isUserInteractionEvent} from './userInteractionPresentation.js';
import {toolDisplayLabel} from './toolCallPresentation.js';
import {eventStartedAtMs, eventUpdatedAtMs} from '../../timelineProjection.js';

export const WORK_MOTION = Symbol('conversation-work-motion');

export function lastAnswerIndex(events = []) {
	for (let index = events.length - 1; index >= 0; index--) {
		const event = events[index];
		if (event?.kind !== 'answer' || !String(event.message?.content || '').trim()) continue;
		// A cut marks a progress segment, not a final answer. Do not hide an
		// interrupted run's only explanation behind a completed-work label.
		return event.operation?.payload?.segmentBoundary ? -1 : index;
	}
	return -1;
}

export function isInlineProcess(entry) {
	const event = entry?.event;
	if (isUserInteractionEvent(event) || event?.operation?.opType === 'agent') return false;
	return entry?.part === 'reasoning' || (event?.kind === 'answer' && !String(event.message?.content || '').trim())
		|| ['tool', 'live_tool', 'tool_group'].includes(event?.kind);
}

export function conversationWorkChunks(entries = [], resultIndex = -1) {
	const chunks = [];
	for (const entry of entries) {
		const event = entry.event;
		const isResult = entry.index === resultIndex && entry.part !== 'reasoning' && event.kind === 'answer';
		const agent = event.operation?.opType === 'agent' || ['Agent', 'AgentContinue', 'AgentMessage', 'AgentStop'].includes(event.calls?.[0]?.name || event.toolName);
		const agentStatus = event.operation?.payload?.task?.status || event.operation?.status || '';
		const activeAgent = agent && !['completed', 'partial', 'cancelled', 'interrupted', 'failed'].includes(agentStatus);
		// Preserve the position and visibility of actionable cards and stop/error
		// notices. Folding presentation is never acknowledgement of an action.
		const exposed = isResult || event.failure || event.message?.failure
			|| isUserInteractionEvent(event) || event.kind === 'model_retry'
			|| event.kind === 'live_agent' || event.kind === 'live_status' && !event.persistentRunIndicator
			|| activeAgent;
		const kind = exposed ? 'exposed' : 'work';
		let chunk = chunks.at(-1);
		if (!chunk || chunk.kind !== kind) {
			chunk = {kind, key: `${kind}:${entry.event.id || entry.event.eventKey || entry.index}:${entry.part || 'event'}`, entries: []};
			chunks.push(chunk);
		}
		chunk.entries.push(entry);
	}
	return chunks;
}

export function workDurationLabel(ms) {
	if (!Number.isFinite(Number(ms)) || Number(ms) <= 0) return '';
	const seconds = Math.max(1, Math.floor(Number(ms) / 1000));
	if (seconds < 60) return `${seconds} 秒`;
	const minutes = Math.floor(seconds / 60);
	return minutes < 60 ? `${minutes} 分 ${seconds % 60} 秒` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

export function reasoningDuration(event) {
	const start = eventStartedAtMs(event);
	const boundary = Number(event?.reasoningEndedAtMs || 0);
	if (start > 0 && boundary >= start) return boundary - start;
	const explicit = Number(event?.operation?.payload?.durationMs || event?.durationMs || 0);
	if (explicit > 0) return explicit;
	const end = eventUpdatedAtMs(event);
	return start > 0 && end > start ? end - start : 0;
}

export function latestReasoningLine(text) {
	const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
	for (let i = lines.length - 1; i >= 0; i--) if (lines[i].trim()) return {key: i, text: lines[i].trim()};
	return {key: 0, text: ''};
}

export function processToolLabel(name, fallback = '') {
	return toolDisplayLabel(name, fallback);
}

// Only current measured tokens drive the meter. An unknown reading is not 0%.
export function contextMeter({known = false, tokens = 0, window = 0, threshold = 0} = {}) {
	const used = Math.max(0, Number(tokens) || 0);
	const capacity = Math.max(0, Number(window) || 0), trigger = Math.max(0, Number(threshold) || 0);
	const percent = known && capacity ? used / capacity * 100 : null;
	const triggerPercent = known && trigger ? used / trigger * 100 : null;
	return {used, capacity, trigger, percent, triggerPercent,
		fill: percent === null ? 0 : Math.min(100, percent),
		tone: triggerPercent >= 100 ? 'danger' : triggerPercent >= 85 ? 'warning' : 'normal'};
}
