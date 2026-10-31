import {toolFileIdentity} from "../../artifacts/artifactFiles.js";
import {eventPrimaryToolName, toolBatchSize, toolStatus} from "./display.js";

const FILE_TOOLS = new Set(["Read", "Write", "Edit", "EditBatch"]);

// Only a finished single call that the server has recorded can name its file;
// the server resolves the path from that record.
export function toolFileIdentityFor(event, conversationUuid) {
	const conversation = String(conversationUuid || "").trim();
	const operationId = String(event?.operation?.opId || "").trim();
	if (!conversation || conversation.startsWith("local:") || !operationId) return null;
	if (Array.isArray(event?.events) && event.events.length) return null;
	if (!FILE_TOOLS.has(eventPrimaryToolName(event)) || toolBatchSize(event) > 1 || toolStatus(event) !== "ok") return null;
	return toolFileIdentity(conversation, operationId);
}
