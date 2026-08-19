import {isContextCompactionOperation} from "../../timelineProjection.js";
import {isUserInteractionEvent} from "./userInteractionPresentation.js";

export function isConversationTimelineEvent(event, _primaryToolName = "", agentEvent = false) {
	if (event?.kind === "answer") return Boolean(String(event?.message?.content || "").trim());
	if (event?.kind === "live_status") return Boolean(event?.persistentRunIndicator || event?.interruption);
	if (event?.kind === "model_retry") return true;
	if (event?.kind === "live_agent") return true;
	if (isUserInteractionEvent(event)) return true;
	return event?.kind === "tool" && (agentEvent || isContextCompactionOperation(event?.operation));
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
		.map((event, index) => ({event, index}))
		.filter(({event}) => isConversationTimelineEvent(event, primaryToolNameForEvent(event), isAgentEventForEvent(event)));
}
