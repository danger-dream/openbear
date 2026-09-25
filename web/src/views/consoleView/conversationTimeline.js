import {isContextCompactionOperation} from "../../timelineProjection.js";
import {isUserInteractionEvent} from "./userInteractionPresentation.js";

function agentEventStatus(event = {}) {
	const operation = event?.operation && typeof event.operation === "object" ? event.operation : {};
	const operationPayload = operation?.payload && typeof operation.payload === "object" ? operation.payload : {};
	const livePayload = event?.livePayload && typeof event.livePayload === "object" ? event.livePayload : {};
	const task = operationPayload?.task && typeof operationPayload.task === "object"
		? operationPayload.task
		: livePayload?.task && typeof livePayload.task === "object" ? livePayload.task : {};
	return String(task.status || operation.status || operationPayload.status || livePayload.status || event?.status || "").trim();
}

function isFailedAgentEvent(event = {}) {
	return agentEventStatus(event) === "failed";
}

export function isConversationTimelineEvent(event, _primaryToolName = "", agentEvent = false) {
	if (event?.kind === "answer") return Boolean(String(event?.message?.content || "").trim() || String(event?.message?.reasoning || "").trim());
	if (event?.kind === "live_status") return Boolean(event?.persistentRunIndicator || event?.interruption);
	if (event?.kind === "model_retry") return true;
	if (event?.kind === "live_agent") return !isFailedAgentEvent(event);
	if (isUserInteractionEvent(event)) return true;
	if (event?.operation?.internal) return isContextCompactionOperation(event.operation);
	if (event?.kind === 'live_tool' || event?.kind === 'tool_group') return true;
	return event?.kind === "tool" && (!agentEvent || !isFailedAgentEvent(event));
}

function isTextAnswer(event) {
	return event?.kind === "answer" && Boolean(String(event?.message?.content || "").trim());
}

export function shouldRenderAssistantDivider(entries = [], index = 0) {
	const currentIndex = Number(index);
	if (!Number.isInteger(currentIndex) || currentIndex <= 0) return false;
	return isTextAnswer(entries[currentIndex - 1]?.event) && isTextAnswer(entries[currentIndex]?.event);
}

export function conversationTimelineEntries(events = [], primaryToolNameForEvent = () => "", isAgentEventForEvent = () => false) {
	return (Array.isArray(events) ? events : [])
		.flatMap((event, index) => {
			if (event?.kind !== 'answer' || !String(event.message?.reasoning || '').trim()) return [{event, index}];
			const reasoning = {event, index, part: 'reasoning'};
			return String(event.message?.content || '').trim() ? [reasoning, {event, index, part: 'answer'}] : [reasoning];
		})
		.filter(({event}) => isConversationTimelineEvent(event, primaryToolNameForEvent(event), isAgentEventForEvent(event)));
}
