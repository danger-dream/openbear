// Pure presentation helpers for work items; no Vue or network imports, so tool
// summaries and tests can use them directly.
export const STATUS_LABELS = {
	discussing: "讨论中", decided: "已定", in_progress: "进行中", done: "已完成", abandoned: "已放弃",
	todo: "待做", doing: "进行中", blocked: "受阻", ask: "等你定", dropped: "按决定不做",
};
const CLOSED = new Set(["done", "dropped", "abandoned"]);
// Shown next to an item because the user may need to act on it.
export const ATTENTION = new Set(["ask", "blocked"]);

export function isClosed(status) {
	return CLOSED.has(String(status || ""));
}

export function* walk(nodes) {
	for (const node of nodes || []) {
		yield node;
		yield* walk(node.children);
	}
}

export function leaves(node) {
	if (!node?.children?.length) return [node];
	return node.children.flatMap(leaves);
}

export function statusOf(node) {
	return String(node?.effectiveStatus || node?.status || "");
}

export function topNumber(number) {
	return String(number || "").split(".")[0];
}

export function inSubtree(number, root) {
	const value = String(number || ""), base = String(root || "");
	return Boolean(base) && (value === base || value.startsWith(`${base}.`));
}

export function formatTime(ms) {
	const value = Number(ms || 0);
	if (!value) return "";
	const date = new Date(value), now = new Date();
	const pad = n => String(n).padStart(2, "0");
	const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
	if (date.toDateString() === now.toDateString()) return time;
	const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
	if (date.toDateString() === yesterday.toDateString()) return `昨天 ${time}`;
	return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

function parse(raw) {
	if (raw && typeof raw === "object") return raw;
	try {
		const value = JSON.parse(String(raw || "{}"));
		return value && typeof value === "object" ? value : {};
	} catch { return {}; }
}

const ACTION_LABELS = {
	create: "建任务", add: "加子项", update: "修改", note: "记录", decide: "记决策", correct: "记纠正", focus: "执行范围",
	phase: "阶段", start: "开始", complete: "完成", block: "受阻", ask: "等你定", drop: "不做", reopen: "重新打开",
	delete: "删除", get: "查看", list: "查看任务",
};
const NOTE_LABELS = {investigation: "调查", proposal: "方案", note: "记录"};

function numbersOf(args) {
	if (Array.isArray(args.entries) && args.entries.length) return args.entries.map(entry => String(entry?.number || "")).filter(Boolean);
	if (Array.isArray(args.items) && args.items.length) return args.items.map(String);
	if (Array.isArray(args.decisions)) return args.decisions.flatMap(entry => entry?.items || []).map(String);
	return args.number ? [String(args.number)] : [];
}

// "#4.3 → 已完成", "执行范围 #4.4", "建任务 “修复安全问题”"; never claims an outcome the result lacks.
export function workItemToolPreview(rawArguments, rawResult = "") {
	const args = parse(rawArguments), result = parse(rawResult);
	const action = String(args.action || "list");
	const numbers = numbersOf(args).map(number => `#${number.replace(/^#/, "")}`);
	const list = numbers.length > 3 ? `${numbers.slice(0, 3).join("、")} 等 ${numbers.length} 项` : numbers.join("、");
	if (result.ok === false) return [ACTION_LABELS[action] || action, list, String(result.message || result.error || "未成功").slice(0, 60)].filter(Boolean).join(" · ");
	if (action === "create") return `建任务 “${String(args.title || "").slice(0, 60)}”${result.number ? ` #${result.number}` : ""}`;
	if (action === "focus") return `执行范围 ${list}`;
	if (action === "note") return `${NOTE_LABELS[args.kind] || "记录"} ${list}`.trim();
	if (["start", "complete", "block", "ask", "drop", "reopen"].includes(action)) {
		const items = Array.isArray(result.items) ? result.items : [];
		const failed = Array.isArray(result.errors) ? result.errors.length : 0;
		const done = items.length === 1 ? `#${items[0].number} → ${items[0].status}` : items.length ? `${items.length} 项 → ${items[0].status}` : "";
		return [done || `${ACTION_LABELS[action]} ${list}`, failed ? `${failed} 项未成功` : ""].filter(Boolean).join(" · ");
	}
	if (action === "phase") return `${list} → ${STATUS_LABELS[args.phase] || args.phase || ""}`;
	if (action === "list") return "查看任务";
	return `${ACTION_LABELS[action] || action} ${list}`.trim();
}

// The item a WorkItem call is about, for opening the panel at it.
export function workItemCallNumber(rawArguments, rawResult = "") {
	const args = parse(rawArguments), result = parse(rawResult);
	if (result.number) return String(result.number);
	const numbers = numbersOf(args);
	return numbers.length ? numbers[0].replace(/^#/, "") : "";
}

// Top-level tasks a turn's WorkItem calls touched, in first-seen order.
export function turnTaskNumbers(events, out = []) {
	for (const event of events || []) {
		if (Array.isArray(event?.events)) turnTaskNumbers(event.events, out);
		for (const call of event?.calls || []) {
			if (call?.name !== "WorkItem") continue;
			const args = parse(call.arguments || call.previewArguments);
			const number = workItemCallNumber(args, event?.result?.content || "");
			const top = topNumber(number);
			if (top && !out.includes(top)) out.push(top);
		}
	}
	return out;
}
