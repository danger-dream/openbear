import {toolFileIdentity} from "../../artifacts/artifactFiles.js";
import {eventPrimaryToolName, toolBatchSize, toolStatus} from "./display.js";

const FILE_TOOLS = new Set(["Read", "Write", "Edit", "EditBatch"]);

function readsSeveralFiles(event) {
	const raw = event?.calls?.[0]?.arguments ?? event?.calls?.[0]?.previewArguments;
	if (raw && typeof raw === "object") return Array.isArray(raw.reads);
	try { return Array.isArray(JSON.parse(String(raw || "{}"))?.reads); } catch { return false; }
}

// Only a finished single call that the server has recorded can name its file;
// the server resolves the path from that record (a Read of reads[] names none).
export function toolFileIdentityFor(event, conversationUuid) {
	const conversation = String(conversationUuid || "").trim();
	const operationId = String(event?.operation?.opId || "").trim();
	if (!conversation || conversation.startsWith("local:") || !operationId) return null;
	if (Array.isArray(event?.events) && event.events.length) return null;
	if (!FILE_TOOLS.has(eventPrimaryToolName(event)) || toolBatchSize(event) > 1 || toolStatus(event) !== "ok") return null;
	if (readsSeveralFiles(event)) return null;
	return toolFileIdentity(conversation, operationId);
}
