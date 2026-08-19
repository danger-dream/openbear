const ACTION_LABELS = Object.freeze({
	list: "获取记忆列表",
	search: "搜索记忆",
	get: "读取记忆",
	create: "创建记忆",
	update: "更新记忆",
	delete: "删除记忆",
	restore: "恢复记忆",
});

function parseObject(value) {
	if (value && typeof value === "object" && !Array.isArray(value)) return value;
	const raw = String(value || "").trim();
	if (!raw) return {};
	try {
		const parsed = JSON.parse(raw);
		return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

function compact(value, limit = 64) {
	const text = String(value || "").replace(/\s+/g, " ").trim();
	if (!text) return "";
	return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}

function shortMemoryUuid(value) {
	const uuid = compact(value, 96);
	if (!uuid || uuid.length <= 22) return uuid;
	return `${uuid.slice(0, 14)}…${uuid.slice(-5)}`;
}

/**
 * Build the collapsed TaskMemory card preview from the full request/result payloads.
 * The card headline identifies the action and memory; complete arguments and results
 * remain available from the expanded tool detail.
 */
export function taskMemoryToolPreview(rawArguments, rawResult = "") {
	const args = parseObject(rawArguments);
	const result = parseObject(rawResult);
	const memory = result.memory && typeof result.memory === "object" && !Array.isArray(result.memory)
		? result.memory
		: {};
	const action = compact(args.action, 24).toLowerCase();
	const actionLabel = ACTION_LABELS[action] || (action || "操作记忆");

	if (action === "list") return actionLabel;
	if (action === "search") {
		const query = compact(args.query, 56);
		return query ? `${actionLabel}： ${query}` : actionLabel;
	}

	// The caller asked for the human-readable memory subject in the headline.
	// Description is the most useful target label for the current TaskMemory UI;
	// fall back to the formal name and then the stable UUID.
	const memoryName = compact(memory.description, 72)
		|| compact(memory.name, 72)
		|| compact(args.memoryLabel, 72)
		|| compact(args.description, 72)
		|| compact(args.name, 72);
	if (memoryName) return `${actionLabel}： ${memoryName}`;

	const memoryUuid = shortMemoryUuid(memory.memoryUuid || args.memoryUuid);
	return memoryUuid ? `${actionLabel}： ${memoryUuid}` : actionLabel;
}
