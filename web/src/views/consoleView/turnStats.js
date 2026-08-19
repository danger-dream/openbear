const STABLE_AGENT_STATUSES = new Set([
	"completed",
	"failed",
	"cancelled",
	"interrupted",
	"partial",
	"needs_openbear_control",
]);

function nonNegative(value) {
	return Math.max(0, Number(value || 0));
}

function usageTotal(usage = {}) {
	return nonNegative(usage.inputTokens)
		+ nonNegative(usage.outputTokens)
		+ nonNegative(usage.cacheReadTokens)
		+ nonNegative(usage.cacheWriteTokens);
}

function publicTaskUsage(task = {}) {
	const tokens = task?.tokens && typeof task.tokens === "object" ? task.tokens : {};
	const promptTotal = nonNegative(tokens.input);
	const cache = nonNegative(tokens.cache);
	const output = nonNegative(tokens.output);
	return {
		inputTokens: Math.max(0, promptTotal - cache),
		outputTokens: output,
		cacheReadTokens: cache,
		cacheWriteTokens: 0,
		totalTokens: promptTotal + output,
	};
}

/**
 * Repair old/mid-run turn stats from the Agent cards already projected into the
 * same turn. New stats carry expertTaskUuids, making this an exact missing-task
 * merge. Legacy stats without IDs are only repaired when expertUsage is empty,
 * which avoids double counting older payloads that already embedded Agent use.
 */
export function reconcileAgentTaskUsage(stats, tasks = []) {
	if (!stats || !Array.isArray(tasks) || !tasks.length) return stats;
	const expertUsage = stats.expertUsage && typeof stats.expertUsage === "object" ? stats.expertUsage : {};
	const hasTaskIds = Array.isArray(stats.expertTaskUuids);
	if (!hasTaskIds && usageTotal(expertUsage) > 0) return stats;

	const accounted = new Set((hasTaskIds ? stats.expertTaskUuids : []).map((value) => String(value || "")).filter(Boolean));
	const seen = new Set(accounted);
	const addedUsage = {inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, totalTokens: 0};
	let added = 0;

	for (const task of tasks) {
		if (!task || typeof task !== "object") continue;
		const status = String(task.status || "");
		const taskUuid = String(task.taskUuid || task.task_uuid || "");
		if (!STABLE_AGENT_STATUSES.has(status) || !taskUuid || seen.has(taskUuid)) continue;
		seen.add(taskUuid);
		const usage = publicTaskUsage(task);
		addedUsage.inputTokens += usage.inputTokens;
		addedUsage.outputTokens += usage.outputTokens;
		addedUsage.cacheReadTokens += usage.cacheReadTokens;
		addedUsage.cacheWriteTokens += usage.cacheWriteTokens;
		addedUsage.totalTokens += usage.totalTokens;
		added += 1;
	}

	if (!added) return stats;
	const mergedUsage = {
		inputTokens: nonNegative(expertUsage.inputTokens) + addedUsage.inputTokens,
		outputTokens: nonNegative(expertUsage.outputTokens) + addedUsage.outputTokens,
		cacheReadTokens: nonNegative(expertUsage.cacheReadTokens) + addedUsage.cacheReadTokens,
		cacheWriteTokens: nonNegative(expertUsage.cacheWriteTokens) + addedUsage.cacheWriteTokens,
		totalTokens: usageTotal(expertUsage) + addedUsage.totalTokens,
	};
	return {
		...stats,
		expertUsage: mergedUsage,
		expertTaskUuids: [...seen].sort(),
		expertTasks: Math.max(nonNegative(stats.expertTasks), seen.size),
		agentUsageReconciled: true,
	};
}
