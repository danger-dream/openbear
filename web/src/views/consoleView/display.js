import {
	CircleCheck,
	Close,
	Compass,
	DataAnalysis,
	Document,
	EditPen,
	Finished,
	Loading,
	Monitor,
	Refresh,
	SemiSelect,
	Timer,
	Tools,
	TrendCharts,
	User,
	Warning,
} from "@element-plus/icons-vue";
import ContextCompactionIcon from "./ContextCompactionIcon.vue";
import {plainText} from "./markdown.js";
import {contextCompactionView} from "./agentPlanPresentation.js";
import {taskMemoryToolPreview} from "./taskMemoryPresentation.js";
import {toolArgumentsSummary} from "./toolArgumentsPresentation.js";

const TOOL_ARGUMENT_PREVIEW_CHARS = 5000;
const TOOL_RESULT_PREVIEW_CHARS = 8000;
const AGENT_OUTPUT_PREVIEW_CHARS = 24000;
const TOOL_META_RE = /<tool-meta>[\s\S]*?<\/tool-meta>/gi;
const PREVIEW_KEYS = ["description", "command", "pattern", "query", "title", "body", "name", "ref", "path", "old_string", "content", "action", "text", "instruction", "task"];
const LONG_AGENT_TOOLS = new Set(["Agent", "AgentMessage", "AgentStop"]);
const AGENT_ACTIVE_STATUSES = new Set(["running", "resuming", "queued", "pausing", "stopping"]);
const AGENT_TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted", "needs_openbear_control"]);
const AGENT_RECENT_LINE_LIMIT = 8;
const AGENT_RECENT_TERMINAL_CONTEXT = 2;
const agentPayloadMemo = new WeakMap();
const agentRowsMemo = new WeakMap();
const agentDisplayMemo = new WeakMap();
const agentRecentMemo = new WeakMap();
const agentArgsMemo = new WeakMap();
const agentArgumentsDisplayMemo = new WeakMap();
const toolNamesMemo = new WeakMap();
const toolPreviewMemo = new WeakMap();
const toolCallPreviewMemo = new WeakMap();
const toolResultItemsMemo = new WeakMap();
const toolResultTextMemo = new WeakMap();
const toolResultEventTextMemo = new WeakMap();
const toolStatusMemo = new WeakMap();

const TOOL_ICON = {
	Read: Document,
	Write: EditPen,
	Edit: EditPen,
	Bash: Monitor,
	Agent: User,
	AgentMessage: Finished,
	AgentStop: Close,
	ContextCompaction: ContextCompactionIcon,
};

export function modelRetryReasonLabel(retry = {}) {
	const reason = String(retry?.reason || "").trim();
	const labels = {
		rate_limit: "上游限流",
		overloaded: "上游过载",
		timeout: "请求超时",
		connection: "网络连接异常",
		server_error: "上游服务异常",
		billing: "上游余额/额度不足",
		auth: "上游鉴权失败",
		auth_permanent: "上游拒绝访问",
		format: "上游拒绝了请求格式",
	};
	const reasonText = labels[reason] || reason.replaceAll("_", " ");
	const summary = String(retry?.summary || "").trim();
	const nestedPayload = (value) => /\bHTTP\s+\d{3}\s*:\s*[\[{]/i.test(value) || /^\s*[\[{]/.test(value);
	if (summary && !nestedPayload(summary)) return summary;
	const error = String(retry?.error || "").trim();
	if (!error || error === reasonText || nestedPayload(error)) return reasonText || "上游请求失败";
	return [reasonText, error].filter(Boolean).join(" · ");
}

export function fmtNum(n) {
	return Number(n || 0).toLocaleString();
}

export function fmtCost(n) {
	return Number(n || 0) > 0 ? `$${Number(n).toFixed(5)}` : "—";
}

export function fmtTps(value) {
	const n = Number(value || 0);
	if (!n) return "—";
	return `${n.toFixed(n >= 100 ? 0 : 1)} t/s`;
}

export function fmtMs(ms) {
	const n = Number(ms || 0);
	if (!n) return "—";
	if (n < 1000) return `${Math.round(n)}ms`;
	if (n < 10000) return `${(n / 1000).toFixed(1)}s`;
	if (n < 60000) return `${Math.round(n / 1000)}s`;
	const m = Math.floor(n / 60000);
	const s = Math.round((n % 60000) / 1000);
	return `${m}m${String(s).padStart(2, "0")}s`;
}

export function fmtLiveElapsedMs(ms) {
	const n = Math.max(0, Number(ms || 0));
	if (n < 60000) return `${(Math.floor(n / 100) / 10).toFixed(1)}s`;
	const m = Math.floor(n / 60000);
	const s = Math.floor((n % 60000) / 1000);
	return `${m}m${String(s).padStart(2, "0")}s`;
}

export function fmtElapsedFromStart(startAt) {
	const ts = Number(startAt || 0);
	return ts ? fmtLiveElapsedMs(Date.now() - ts) : "0.0s";
}

function trimCompactNumber(text) {
	return String(text).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
}

export function fmtTokens(n) {
	const v = Number(n || 0);
	if (v >= 1_000_000) return `${trimCompactNumber((v / 1_000_000).toFixed(2))}M`;
	if (v >= 1000) return `${trimCompactNumber((v / 1000).toFixed(1))}K`;
	return String(Math.round(v));
}

export function fmtBytes(value) {
	const n = Math.max(0, Number(value || 0));
	if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
	if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
	return `${n} B`;
}

export function tokenPartsFromStats(stats) {
	const u = stats?.usage || {};
	const eu = stats?.expertUsage || {};
	const input = Number(u.inputTokens || 0) + Number(u.cacheReadTokens || 0) + Number(u.cacheWriteTokens || 0)
		+ Number(eu.inputTokens || 0) + Number(eu.cacheReadTokens || 0) + Number(eu.cacheWriteTokens || 0);
	const output = Number(u.outputTokens || 0) + Number(eu.outputTokens || 0);
	const cache = Number(u.cacheReadTokens || 0) + Number(u.cacheWriteTokens || 0)
		+ Number(eu.cacheReadTokens || 0) + Number(eu.cacheWriteTokens || 0);
	return {input, output, cache};
}

export function cachePct(cache, input) {
	const i = Number(input || 0);
	if (!i) return "—";
	return `${(Number(cache || 0) * 100 / i).toFixed(1)}%`;
}

export function tokenLine(parts) {
	const input = Number(parts?.input || 0);
	const output = Number(parts?.output || 0);
	const cache = Number(parts?.cache || 0);
	return `↑${fmtTokens(input)} · ↓${fmtTokens(output)} · 缓存 ${fmtTokens(cache)}（${cachePct(cache, input)}）`;
}

export function previewText(text, limit, label = "内容") {
	const raw = String(text || "");
	if (raw.length <= limit) return raw;
	const omitted = raw.length - limit;
	return `${raw.slice(0, limit)}

…（${label}前端预览已截断 ${fmtNum(omitted)} 字符，完整内容仍保留在后端记录中。）`;
}

export function shortText(text, limit = 96) {
	const s = plainText(text);
	return s.length > limit ? `${s.slice(0, Math.max(0, limit - 1))}…` : s;
}

export function modelThinkingLevels(model) {
	return Array.isArray(model?.thinkingLevels) ? model.thinkingLevels.filter(Boolean) : [];
}

export function modelDefaultThinking(model) {
	const levels = modelThinkingLevels(model);
	return model?.defaultThinkingLevel || levels[levels.length - 1] || "";
}

export function thinkingLabel(level) {
	return level ? String(level) : "无思考";
}

export function thinkingDesc(level) {
	const map = {off: "关闭", minimal: "极简", low: "轻量", medium: "标准", high: "深入", xhigh: "最强", max: "最大"};
	return map[level || ""] || "";
}

export function modelLabel(model) {
	return model?.label || model?.key || "选择模型";
}

export function modelShortLabel(model) {
	const label = modelLabel(model);
	return label.replace(/^.*?\|\s*/, "").replace(/^openai\//, "").replace(/^anthropic\//, "");
}

export function cleanToolResult(text) {
	return String(text || "").replace(TOOL_META_RE, "").trim();
}

export function parseJson(value) {
	try {
		const data = JSON.parse(String(value || ""));
		return data && typeof data === "object" ? data : null;
	} catch {
		return null;
	}
}

export function parseJsonLoose(text) {
	const raw = String(text || "").trim();
	if (!raw) return null;
	const direct = parseJson(raw);
	if (direct) return direct;
	const firstObject = raw.indexOf("{");
	if (firstObject > 0) return parseJson(raw.slice(firstObject));
	return null;
}

export function callName(call) {
	return call?.name || call?.function?.name || "Tool";
}

export function callArguments(call) {
	return call?.["arguments"] || call?.function?.["arguments"] || "";
}

export function callArgumentsDisplay(call) {
	return previewText(callArguments(call), TOOL_ARGUMENT_PREVIEW_CHARS, "工具参数");
}

export function toolIcon(name) {
	return TOOL_ICON[name] || (isAgentTool(name) ? User : Tools);
}

export function isAgentTool(name) {
	return LONG_AGENT_TOOLS.has(String(name || ""));
}

export function agentItemPreview(item) {
	if (!item || typeof item !== "object") return "Agent";
	const agent = shortText(item.workerType || item.subagent_type || item.agent || item.agentName || item.agentKey || "Agent", 24);
	const task = shortText(item.description || item.title || item.prompt || item.instruction || item.task || item.message || "", 72);
	return task ? `${agent}：${task}` : agent;
}

export function toolPreviewFromArgs(argsText) {
	const data = parseJson(argsText);
	if (!data) return shortText(argsText, 96);
	if (Array.isArray(data.items) && data.items.length) {
		return data.items.slice(0, 3).map((item) => agentItemPreview(item)).join("；") + (data.items.length > 3 ? `；另 ${data.items.length - 3} 个` : "");
	}
	for (const key of PREVIEW_KEYS) {
		const value = data[key];
		if (typeof value === "string" && value.trim()) return shortText(value, 110);
	}
	for (const [key, value] of Object.entries(data)) {
		if (typeof value === "string" && value.trim()) return `${key}=${shortText(value, 90)}`;
	}
	return "";
}

function rendererLineContent(line, toolName) {
	const text = String(line || "").trim();
	const name = String(toolName || "").trim();
	if (!name) return text;
	const marker = `${name}:`;
	const markerIndex = text.indexOf(marker);
	return markerIndex >= 0 ? text.slice(markerIndex + marker.length).trim() : text;
}

function toolPreviewFromCall(call, rawResult = "") {
	if (!call || typeof call !== "object") return "";
	const argsText = callArguments(call);
	const previewArguments = String(call.previewArguments || "").trim();
	const summaryPreview = String(call.preview || "").trim();
	const resultText = String(rawResult || "");
	const sourceArguments = argsText || previewArguments;
	const toolName = callName(call);
	const cached = toolCallPreviewMemo.get(call);
	if (
		cached
		&& cached.argsText === argsText
		&& cached.previewArguments === previewArguments
		&& cached.summaryPreview === summaryPreview
		&& cached.resultText === resultText
	) return cached.value;
	const value = toolName === "ContextCompaction" && summaryPreview
		? shortText(rendererLineContent(summaryPreview, toolName), 110)
		: sourceArguments
			? (toolName === "TaskMemory"
				? taskMemoryToolPreview(sourceArguments, resultText)
				: toolPreviewFromArgs(sourceArguments))
			: shortText(rendererLineContent(summaryPreview, toolName), 110);
	toolCallPreviewMemo.set(call, {argsText, previewArguments, summaryPreview, resultText, value});
	return value;
}

export function toolTitle(event) {
	const names = Array.isArray(event?.calls) ? event.calls.map((c) => callName(c)).filter(Boolean) : [];
	if (!names.length) return event.toolName || "Tool";
	if (names.length === 1) return names[0];
	return `${names[0]} +${names.length - 1}`;
}

function contextCompactionDisplay(event) {
	const payload = event?.operation?.payload && typeof event.operation.payload === "object"
		? event.operation.payload
		: {};
	const call = event?.calls?.[0] || null;
	const argumentsValue = call ? callArguments(call) : (payload.arguments || payload.args || "");
	return contextCompactionView({
		...payload,
		payload,
		name: call ? callName(call) : (payload.name || payload.toolName || event?.toolName || ""),
		arguments: argumentsValue,
	});
}

export function toolPreview(event) {
	if (!event || typeof event !== "object") return "";
	const compaction = contextCompactionDisplay(event);
	if (compaction.isCompaction && compaction.scope !== "agent") return compaction.cardPreview;
	const cached = toolPreviewMemo.get(event);
	if (
		cached
		&& cached.live === event.live
		&& cached.livePreview === event.livePreview
		&& cached.calls === event.calls
		&& cached.result === event.result
		&& cached.results === event.results
		&& cached.message === event.message
	) {
		return cached.value;
	}
	let value = "";
	if (event.live && event.livePreview) return shortText(event.livePreview, 110);
	if (event.calls?.length) {
		const previews = event.calls.map((call, index) => (
			toolPreviewFromCall(call, toolResultText(event, toolResultForIndex(event, index)))
		)).filter(Boolean);
		if (previews.length) value = previews.join("；");
		else value = event.calls.length > 1 ? `${event.calls.length} 个工具` : "";
	} else {
		const raw = String(event.result?.content || event.message?.content || "").trim();
		value = !raw || raw.startsWith("{") || raw.startsWith("[") ? "" : shortText(raw, 110);
	}
	toolPreviewMemo.set(event, {
		live: event.live,
		livePreview: event.livePreview,
		calls: event.calls,
		result: event.result,
		results: event.results,
		message: event.message,
		value,
	});
	return value;
}

export function toolDisplayState(event, activeIndex = 0, {includeDetail = false} = {}) {
	const toolName = toolSummaryTitle(event);
	const compaction = contextCompactionDisplay(event);
	const title = compaction.isCompaction && compaction.scope !== "agent" ? compaction.cardTitle : toolName;
	const batchSize = toolBatchSize(event);
	const state = {
		status: toolStatus(event),
		title,
		icon: toolIcon(toolName),
		preview: toolPreview(event),
		batchSize,
		isBatch: batchSize > 1,
	};
	if (!includeDetail) return state;
	const activeCall = event?.calls?.[activeIndex] || event?.calls?.[0] || null;
	const activeResult = toolResultForIndex(event, activeIndex);
	return {
		...state,
		activeCall,
		activeResult,
		activeToolName: callName(activeCall),
		activeArguments: callArguments(activeCall),
		activeArgumentsDisplay: activeCall ? callArgumentsDisplay(activeCall) : "",
		activeResultDisplay: activeResult ? toolResultDisplay(event, activeResult) : "",
		tabLabels: state.isBatch ? Array.from({length: batchSize}, (_, idx) => toolBatchLabel(event, idx)) : [],
	};
}

export function toolResultItems(event) {
	if (!event || typeof event !== "object") return [];
	const cached = toolResultItemsMemo.get(event);
	if (cached && cached.results === event.results && cached.result === event.result) return cached.value;
	const value = Array.isArray(event?.results) ? event.results.filter(Boolean) : (event?.result ? [event.result] : []);
	toolResultItemsMemo.set(event, {results: event.results, result: event.result, value});
	return value;
}

export function toolStatus(event) {
	if (!event || typeof event !== "object") return "running";
	const operation = event?.operation && typeof event.operation === "object" ? event.operation : {};
	const payload = operation?.payload && typeof operation.payload === "object" ? operation.payload : {};
	const resultState = String(payload.resultState || event?.resultState || "").trim().toLowerCase();
	const operationStatus = String(operation.status || payload.status || "").trim().toLowerCase();
	const cached = toolStatusMemo.get(event);
	if (
		cached
		&& cached.results === event.results
		&& cached.result === event.result
		&& cached.message === event.message
		&& cached.resultState === resultState
		&& cached.operationStatus === operationStatus
	) return cached.value;
	const results = toolResultItems(event);
	const hasErrorResult = results.some((item) => {
		const result = String(item?.content || "").trim();
		return result.startsWith("[错误]")
			|| result.startsWith("error:")
			|| result.includes("工具调用被中止")
			|| /^status:\s*(failed|timeout|killed)\b/im.test(result)
			|| result.includes('"exitCode": 127');
	});
	let value = "running";
	if (hasErrorResult || resultState === "error" || ["failed", "cancelled", "interrupted"].includes(operationStatus)) value = "error";
	else if (results.length || resultState === "ok" || ["completed", "partial"].includes(operationStatus)) value = "ok";
	toolStatusMemo.set(event, {
		results: event.results,
		result: event.result,
		message: event.message,
		resultState,
		operationStatus,
		value,
	});
	return value;
}

export function toolCallIdOf(item) {
	return String(item?.id || item?.toolCallId || item?.tool_call_id || "").trim();
}

export function toolResultKey(event) {
	const callIds = (event?.calls || []).map((call) => toolCallIdOf(call) || callName(call)).filter(Boolean).join("|");
	return String(event?.id || event?.operation?.opId || event?.message?.id || callIds || event?.result?.id || event?.toolName || "tool");
}

export function toolBatchSize(event) {
	return Math.max(event?.calls?.length || 0, toolResultItems(event).length);
}

export function toolResultForIndex(event, idx) {
	const results = toolResultItems(event);
	const call = event?.calls?.[idx];
	const callId = toolCallIdOf(call);
	if (callId) {
		const matched = results.find((item) => toolCallIdOf(item) === String(callId));
		if (matched) return matched;
	}
	return results[idx] || null;
}

export function toolResultText(event, activeResult = null) {
	const item = activeResult || toolResultForIndex(event, 0);
	const raw = item?.content || event?.result?.content || event?.message?.content || "";
	if (item && typeof item === "object") {
		const cached = toolResultTextMemo.get(item);
		if (cached && cached.raw === raw) return cached.value;
		const value = cleanToolResult(raw) || raw;
		toolResultTextMemo.set(item, {raw, value});
		return value;
	}
	if (event && typeof event === "object") {
		const cached = toolResultEventTextMemo.get(event);
		if (cached && cached.raw === raw && cached.result === event.result && cached.message === event.message) return cached.value;
		const value = cleanToolResult(raw) || raw;
		toolResultEventTextMemo.set(event, {raw, result: event.result, message: event.message, value});
		return value;
	}
	return cleanToolResult(raw) || raw;
}

export function toolResultDisplay(event, activeResult = null) {
	const limit = eventPrimaryToolName(event) === "ContextCompaction" ? 32000 : TOOL_RESULT_PREVIEW_CHARS;
	return previewText(toolResultText(event, activeResult), limit, "工具结果");
}

export function toolBatchLabel(event, idx) {
	const call = event?.calls?.[idx];
	const result = toolResultForIndex(event, idx);
	return callName(call) || result?.name || `工具 ${idx + 1}`;
}

export function eventPrimaryToolName(event) {
	return toolNameState(event).primary;
}

export function toolNameState(event) {
	if (!event || typeof event !== "object") return {names: [], primary: "Tool", hasAgentTool: false};
	const resultName = event?.result?.name || "";
	const cached = toolNamesMemo.get(event);
	if (cached && cached.calls === event.calls && cached.toolName === event.toolName && cached.resultName === resultName) return cached.value;
	const names = Array.isArray(event?.calls) ? event.calls.map((call) => callName(call)).filter(Boolean) : [];
	if (event?.toolName) names.push(event.toolName);
	if (resultName) names.push(resultName);
	const unique = [...new Set(names.filter(Boolean))];
	const primary = unique.find((name) => isAgentTool(name)) || unique[0] || "Tool";
	const value = {names: unique, primary, hasAgentTool: unique.some((name) => isAgentTool(name))};
	toolNamesMemo.set(event, {calls: event.calls, toolName: event.toolName, resultName, value});
	return value;
}

export function isDanglingAgentToolResult(event) {
	if (!toolNameState(event).hasAgentTool) return false;
	const text = toolResultItems(event).map((item) => cleanToolResult(item?.content || "")).join("\n") || toolResultText(event);
	return text.includes("工具调用被中止") && !agentPayload(event)?.task && !Array.isArray(agentPayload(event)?.results);
}

export function isAgentEvent(event) {
	return toolNameState(event).hasAgentTool && !isDanglingAgentToolResult(event);
}

export function toolSummaryTitle(event) {
	return isAgentEvent(event) ? eventPrimaryToolName(event) : toolTitle(event);
}

export function humanizeAgentArguments(args = {}) {
	if (!args || typeof args !== "object") return "";
	if (Array.isArray(args.items)) {
		return args.items.map((item, idx) => {
			const name = item?.workerType || item?.subagent_type || item?.agent || item?.agentName || item?.agentKey || `Agent ${idx + 1}`;
			const instruction = item?.prompt || item?.instruction || item?.task || "";
			const title = item?.description || item?.title || "";
			return [`${idx + 1}. ${name}`, title ? `标题：${title}` : "", instruction ? `任务：${instruction}` : ""].filter(Boolean).join("\n   ");
		}).join("\n");
	}
	const lines = [];
	if (args.workerType || args.subagent_type || args.agent || args.agentName || args.agentKey) lines.push(`Agent：${args.workerType || args.subagent_type || args.agent || args.agentName || args.agentKey}`);
	if (args.taskUuid || args.task_uuid) lines.push(`任务：${args.taskUuid || args.task_uuid}`);
	if (args.description || args.title) lines.push(`标题：${args.description || args.title}`);
	if (args.prompt || args.instruction || args.task) lines.push(`任务：${args.prompt || args.instruction || args.task}`);
	if (args.message || args.guidance) lines.push(`消息：${args.message || args.guidance}`);
	if (args.maxParallel) lines.push(`最大并行：${args.maxParallel}`);
	return lines.join("\n");
}

export function agentArgs(event) {
	if (!event || typeof event !== "object") return {};
	const cached = agentArgsMemo.get(event);
	if (cached && cached.calls === event.calls) return cached.value;
	const call = (event?.calls || []).find((item) => isAgentTool(callName(item))) || event?.calls?.[0];
	const value = parseJsonLoose(call?.["arguments"] || call?.function?.["arguments"] || "") || {};
	agentArgsMemo.set(event, {calls: event.calls, value});
	return value;
}

export function agentArgumentsDisplay(event) {
	if (!event || typeof event !== "object") return "";
	const cached = agentArgumentsDisplayMemo.get(event);
	if (cached && cached.calls === event.calls) return cached.value;
	const pretty = humanizeAgentArguments(agentArgs(event));
	let value;
	if (pretty) value = previewText(pretty, TOOL_ARGUMENT_PREVIEW_CHARS, "Agent 参数");
	else {
	const call = (event?.calls || []).find((item) => isAgentTool(callName(item))) || event?.calls?.[0];
		value = call ? callArgumentsDisplay(call) : "";
	}
	agentArgumentsDisplayMemo.set(event, {calls: event.calls, value});
	return value;
}

export function agentPayload(event) {
	if (!event || typeof event !== "object") return null;
	// Agent UI state is an operation fact. Do not parse tool result text as a
	// status payload; result JSON belongs to the transcript/result panel only.
	const live = event?.livePayload && typeof event.livePayload === "object" ? event.livePayload : null;
	const cached = agentPayloadMemo.get(event);
	if (cached && cached.live === live) return cached.value;
	const value = live || null;
	agentPayloadMemo.set(event, {live, value});
	return value;
}

export function agentStatusMeta(status, toolName = "Agent") {
	const base = toolName === "AgentMessage" ? "续跑" : toolName === "AgentStop" ? "停止" : "执行";
	const map = {
		completed: {icon: CircleCheck, label: `${base}完成`, cls: "ok"},
		partial: {icon: CircleCheck, label: "部分完成", cls: "ok"},
		needs_openbear_control: {icon: Compass, label: "等待裁决", cls: "pending"},
		failed: {icon: Close, label: `${base}失败`, cls: "error"},
		cancelled: {icon: SemiSelect, label: "已取消", cls: "stopped"},
		interrupted: {icon: Warning, label: "已中断", cls: "partial"},
		running: {icon: Loading, label: `${base}中`, cls: "running"},
		resuming: {icon: Refresh, label: "续跑中", cls: "running"},
		queued: {icon: Timer, label: "排队中", cls: "running"},
	};
	return map[status] || {icon: Warning, label: status || "状态未知", cls: "partial"};
}

export function payloadTask(payload) {
	return payload?.task || payload?.result?.task || {};
}

export function payloadAgentSession(payload) {
	return payload?.agentSession || payload?.result?.agentSession || {};
}

export function agentOverallStatus(event) {
	const payload = agentPayload(event);
	if (!event?.result && !payload) return "running";
	if (payload?.results && Array.isArray(payload.results)) {
		const total = payload.results.length;
		const statuses = payload.results.map((item) => String(item?.task?.status || item?.result?.task?.status || item?.status || "")).filter(Boolean);
		if (statuses.some((status) => AGENT_ACTIVE_STATUSES.has(status) && status !== "queued")) return "running";
		if (statuses.some((status) => status === "queued")) return "queued";
		if (statuses.some((status) => status === "needs_openbear_control")) return "needs_openbear_control";
		if (statuses.length && statuses.every((status) => ["cancelled", "interrupted"].includes(status))) return "interrupted";
		const ok = payload.results.filter((item) => item?.ok === true || item?.status === "completed" || item?.task?.status === "completed" || item?.result?.task?.status === "completed").length;
		if (!total) return toolStatus(event) === "error" ? "failed" : "completed";
		if (ok === total) return "completed";
		if (ok > 0) return "partial";
		return "failed";
	}
	const task = payloadTask(payload);
	if (task?.status) return String(task.status);
	const declaredStatus = String(payload?.status || "");
	if (AGENT_TERMINAL_STATUSES.has(declaredStatus) || declaredStatus === "partial") return declaredStatus;
	if (["running", "resuming", "queued"].includes(declaredStatus)) return declaredStatus;
	return toolStatus(event) === "error" ? "failed" : "completed";
}

export function taskDisplayName(base, taskUuid = "", explicit = "") {
	const given = String(explicit || "").trim();
	if (given) return given;
	const short = String(taskUuid || "").trim().slice(0, 8);
	const root = String(base || "Agent").trim() || "Agent";
	if (!short || root.endsWith(`-${short}`)) return root;
	return `${root}-${short}`;
}

export function agentItemFromArgs(item) {
	return {
		taskUuid: String(item?.taskUuid || item?.task_uuid || ""),
		status: "queued",
		name: item?.workerType || item?.subagent_type || item?.agent || item?.agentName || item?.agentKey || "Agent",
		title: item?.description || item?.title || item?.prompt || item?.instruction || item?.task || item?.message || "",
		current: "等待调度",
		error: "",
		argItem: item || null,
		resultPayload: null,
		outputFallback: "",
		hasArguments: Boolean(item && typeof item === "object" && Object.keys(item).length),
		hasOutput: false,
		metrics: {model: 0, tool: 0, context: 0, input: 0, output: 0, cache: 0, contextWindow: 0, cost: 0},
	};
}

function taskDurationMs(task = {}, fallbackMs = 0) {
	const direct = Number(task?.durationMs || task?.duration_ms || 0);
	if (direct > 0) return direct;
	const started = Number(task?.startedAtMs || task?.started_at_ms || 0);
	const finished = Number(task?.finishedAtMs || task?.finished_at_ms || 0);
	if (started > 0 && finished > started) return finished - started;
	return Math.max(0, Number(fallbackMs || 0));
}

function taskAvgTps(task = {}, fallbackMs = 0) {
	const output = Number(taskTokens(task).output || 0);
	const duration = taskDurationMs(task, fallbackMs);
	return output > 0 && duration > 0 ? output * 1000 / duration : 0;
}

export function agentRows(event) {
	const payload = agentPayload(event);
	const cached = event && typeof event === "object" ? agentRowsMemo.get(event) : null;
	if (cached && cached.payload === payload && cached.calls === event?.calls && cached.result === event?.result && cached.results === event?.results) {
		return cached.value;
	}
	let value;
	const args = agentArgs(event);
	const argItems = Array.isArray(args.items) ? args.items : [];
	if (Array.isArray(payload?.results)) {
		value = payload.results.map((item, idx) => {
			const task = item?.task || item?.result?.task || {};
			const session = item?.agentSession || item?.result?.agentSession || {};
			const result = item?.result || {};
			const resultPayload = Object.keys(result || {}).length ? result : {output: task.output};
			const outputFallback = item?.error || "";
			const status = String(task.status || item.status || (item.ok ? "completed" : "failed"));
			const tokens = taskTokens(task);
			const duration = taskDurationMs(task, item.durationMs || item?.result?.durationMs || payload?.durationMs || 0);
			const argItem = argItems[idx] || null;
			return {
				taskUuid: String(task.taskUuid || item.taskUuid || item?.result?.taskUuid || ""),
				status,
				name: taskDisplayName(session.title || session.agentKey || task.currentAgent || item.agent || "Agent", task.taskUuid || item.taskUuid || item?.result?.taskUuid, task.displayName || item.displayName || item?.result?.displayName),
				title: task.title || item.title || "",
				current: task.currentStatus || (status === "completed" ? "任务完成" : status === "failed" ? "任务失败" : status),
				error: item.error || task.error || "",
				argItem,
				resultPayload,
				outputFallback,
				hasArguments: Boolean(argItem && typeof argItem === "object" && Object.keys(argItem).length),
				hasOutput: hasAgentOutputContent(resultPayload, outputFallback),
				metrics: {
					model: Number(task.modelCalls || 0),
					tool: Number(task.toolCalls || 0),
					context: tokens.context,
					input: tokens.input,
					output: tokens.output,
					cache: tokens.cache,
					contextWindow: Number(task.contextWindow || 0),
					cost: Number(task.costUsd || 0),
					duration,
					avgTps: taskAvgTps(task, duration),
				},
			};
		});
	} else if (payload?.task || payload?.agentSession || payload?.result?.task || payload?.result?.agentSession) {
		const task = payloadTask(payload);
		const session = payloadAgentSession(payload);
		const status = String(task.status || payload.status || "completed");
		const tokens = taskTokens(task);
		const result = payload?.result || payload || {};
		const resultPayload = Object.keys(result || {}).length ? result : {output: task.output};
		const outputFallback = payload?.error || "";
		value = [{
			taskUuid: String(task.taskUuid || payload.taskUuid || payload?.result?.taskUuid || ""),
			status,
			name: taskDisplayName(session.title || session.agentKey || task.currentAgent || "Agent", task.taskUuid || payload.taskUuid || payload?.result?.taskUuid, task.displayName || payload.displayName || payload?.result?.displayName),
			title: task.title || "",
			current: task.currentStatus || (status === "completed" ? "任务完成" : status),
			error: payload.error || task.error || "",
			argItem: Object.keys(args || {}).length ? args : null,
			resultPayload,
			outputFallback,
			hasArguments: Boolean(Object.keys(args || {}).length),
			hasOutput: hasAgentOutputContent(resultPayload, outputFallback),
			metrics: {
				model: Number(task.modelCalls || 0),
				tool: Number(task.toolCalls || 0),
				context: tokens.context,
				input: tokens.input,
				output: tokens.output,
				cache: tokens.cache,
				contextWindow: Number(task.contextWindow || 0),
				cost: Number(task.costUsd || 0),
				duration: taskDurationMs(task, payload?.durationMs || 0),
				avgTps: taskAvgTps(task, payload?.durationMs || 0),
			},
		}];
	} else {
		const items = Array.isArray(args.items) ? args.items : Object.keys(args).length ? [args] : [];
		value = items.map(agentItemFromArgs);
	}
	if (event && typeof event === "object") {
		agentRowsMemo.set(event, {
			payload,
			calls: event?.calls,
			result: event?.result,
			results: event?.results,
			value,
		});
	}
	return value;
}

export function agentSummaryFromRows(event, rows = agentRows(event), toolName = eventPrimaryToolName(event)) {
	const status = agentOverallStatus(event);
	const meta = agentStatusMeta(status, toolName);
	const first = rows[0] || {};
	const countText = first.name || "Agent";
	const title = "Agent";
	const preview = first.title || first.name;
	return {toolName, statusIcon: meta.icon, label: meta.label, cls: meta.cls, title, preview, countText};
}

export function agentSummary(event) {
	const toolName = eventPrimaryToolName(event);
	return agentSummaryFromRows(event, agentRows(event), toolName);
}

export function taskTokens(task) {
	const last = task?.lastUsage || {};
	const context = Number(task?.contextTokens || 0)
		|| Number(last.inputTokens || 0) + Number(last.cacheReadTokens || 0) + Number(last.cacheWriteTokens || 0);
	const tokens = task?.tokens || {};
	const input = Number(tokens.input || 0);
	const output = Number(tokens.output || 0);
	const cache = Number(tokens.cache || 0);
	if (input || output || cache) return {context, input, output, cache};
	return {
		context,
		input: context,
		output: Number(last.outputTokens || 0),
		cache: Number(last.cacheReadTokens || 0) + Number(last.cacheWriteTokens || 0),
	};
}

export function agentTasks(event) {
	const payload = agentPayload(event);
	if (Array.isArray(payload?.results)) return payload.results.map((item) => item?.task || item?.result?.task).filter(Boolean);
	const task = payloadTask(payload);
	return Object.keys(task || {}).length ? [task] : [];
}

export function agentMetricChipList(metrics, {compact = false, showContext = true} = {}) {
	const chips = [];
	if (Number(metrics?.model || 0)) chips.push({
		key: "model",
		icon: Refresh,
		label: `${compact ? "模型" : "模型调用"} ${fmtNum(metrics.model)}`
	});
	if (Number(metrics?.tool || 0)) chips.push({
		key: "tool",
		icon: Tools,
		label: `${compact ? "工具" : "工具调用"} ${fmtNum(metrics.tool)}`
	});
	const context = Number(metrics?.context || 0);
	if (showContext && context) {
		const win = Number(metrics?.contextWindow || 0);
		const pct = win ? ` / ${fmtTokens(win)}（${(context * 100 / win).toFixed(1)}%）` : "";
		chips.push({key: "context", icon: DataAnalysis, label: `上下文 ${fmtTokens(context)}${pct}`});
	}
	if (Number(metrics?.input || 0) || Number(metrics?.output || 0) || Number(metrics?.cache || 0)) {
		chips.push({key: "tokens", icon: TrendCharts, label: `Tokens ${tokenLine(metrics)}`});
	}
	return chips;
}

function agentMetricChipsFromTasks(toolName, tasks) {
	const metrics = {model: 0, tool: 0, context: 0, input: 0, output: 0, cache: 0, contextWindow: 0, cost: 0};
	const contextWindows = new Set();
	for (const task of tasks) {
		metrics.model += Number(task.modelCalls || 0);
		metrics.tool += Number(task.toolCalls || 0);
		const tokens = taskTokens(task);
		metrics.context += tokens.context;
		metrics.input += tokens.input;
		metrics.output += tokens.output;
		metrics.cache += tokens.cache;
		const win = Number(task.contextWindow || 0);
		if (win) contextWindows.add(win);
		metrics.cost += Number(task.costUsd || 0);
	}
	if (tasks.length === 1 && contextWindows.size === 1) metrics.contextWindow = [...contextWindows][0];
	return agentMetricChipList(metrics, {showContext: false});
}

export function agentDisplayState(event) {
	if (!event || typeof event !== "object") {
		const rows = [];
		const summary = {toolName: "Tool", statusIcon: Warning, label: "状态未知", cls: "partial", title: "Agent", preview: "", countText: "Agent"};
		return {summary, rows, metricChips: [], recentLines: []};
	}
	const payload = agentPayload(event);
	const cached = agentDisplayMemo.get(event);
	if (
		cached
		&& cached.payload === payload
		 && cached.calls === event?.calls
		 && cached.result === event?.result
		 && cached.results === event?.results
		&& cached.toolName === event?.toolName
		 && cached.live === event?.live
	) {
		return cached.value;
	}
	const toolName = eventPrimaryToolName(event);
	const rows = agentRows(event);
	const summary = agentSummaryFromRows(event, rows, toolName);
	const value = {
		summary,
		rows,
		metricChips: agentMetricChipsFromTasks(toolName, agentTasks(event)),
		recentLines: agentRecentLines(event),
	};
	agentDisplayMemo.set(event, {
		payload,
		calls: event?.calls,
		result: event?.result,
		results: event?.results,
		toolName: event?.toolName,
		live: event?.live,
		value,
	});
	return value;
}

export function agentRowMetricChips(row) {
	return agentMetricChipList(row?.metrics || {}, {compact: true, showContext: true});
}

export function eventTimeMs(item) {
	const raw = item?.ts || item?.time || item?.createdAt || item?.created_at || item?.created_at_ms || item?.updatedAt || item?.updated_at || 0;
	if (!raw) return 0;
	if (typeof raw === "number") return raw > 10_000_000_000 ? raw : raw * 1000;
	const parsed = Date.parse(String(raw));
	return Number.isFinite(parsed) ? parsed : 0;
}

export function fmtEventClock(ms) {
	const value = Number(ms || 0);
	if (!value) return "—";
	const d = new Date(value);
	const hh = String(d.getHours()).padStart(2, "0");
	const mm = String(d.getMinutes()).padStart(2, "0");
	const ss = String(d.getSeconds()).padStart(2, "0");
	return `${hh}:${mm}:${ss}`;
}

export function eventAgentName(item, fallback = "") {
	const task = item?.task || {};
	const session = item?.agentSession || {};
	return taskDisplayName(
		item?.agent || item?.agentName || item?.currentAgent || task.currentAgent || session.title || session.agentKey || fallback || "",
		item?.taskUuid || item?.task_uuid || task.taskUuid || task.task_uuid,
		item?.displayName || task.displayName,
	);
}

export function recentToolArgsPreview(value, toolName = "") {
	const raw = String(value || "").trim();
	if (!raw) return "";
	const summary = toolArgumentsSummary(toolName, raw);
	if (summary) return summary;
	const data = parseJsonLoose(raw);
	if (data && typeof data === "object") {
		const parts = [];
		for (const [key, val] of Object.entries(data)) {
			if (val === undefined || val === null || val === "") continue;
			const rendered = typeof val === "string" ? val : JSON.stringify(val);
			parts.push(`${key}=${shortText(rendered, 72)}`);
			if (parts.length >= 3) break;
		}
		if (parts.length) return parts.join(" · ");
	}
	return shortText(raw, 110);
}

export function recentEventMessage(item) {
	const kind = String(item?.kind || item?.type || "");
	const detail = item?.detail && typeof item.detail === "object" ? item.detail : {};
	const name = detail.name || item?.name || "";
	if (kind === "agent_control") {
		const action = String(item?.controlAction || detail.action || "message");
		const text = String(item?.message || detail.text || "").trim();
		const summary = String(item?.summary || item?.statusText || "Agent 控制事件").trim();
		const suffix = text ? `：${shortText(text, 96)}` : "";
		if (action === "status") return `用户查询状态${suffix || (summary ? ` · ${summary}` : "")}`;
		if (action === "steer") return `用户补充/纠偏${suffix}`;
		if (action === "stop") return `用户请求停止${suffix}`;
		if (action === "needs_target") return `用户插话需要明确目标${suffix}`;
		return `${summary}${suffix}`;
	}
	if (kind === "tool_call_finished") return "";
	if (kind === "tool_call_started") {
		const args = recentToolArgsPreview(detail["arguments"] || item?.["arguments"] || "", name);
		return `调用工具 ${name || "Tool"}${args ? ` · ${args}` : ""}`;
	}
	if (kind === "model_call_started") {
		const model = detail.modelLabel || detail.model || "";
		return `模型调用开始${model ? `：${model}` : ""}`;
	}
	if (kind === "model_call_finished") {
		const duration = Number(detail.durationMs || item?.durationMs || 0);
		const tps = Number(detail.tps || 0);
		const suffix = [duration ? fmtMs(duration) : "", tps ? fmtTps(tps) : ""].filter(Boolean).join(" · ");
		return `模型调用完成${suffix ? ` · ${suffix}` : ""}`;
	}
	if (kind === "model_call_retry") {
		const duration = Number(detail.durationMs || item?.durationMs || 0);
		return `${item?.summary || "模型调用重试"}${duration ? ` · ${fmtMs(duration)}` : ""}`;
	}
	return String(item?.message || item?.summary || item?.currentStatus || item?.type || "动态");
}

function isAgentRecentActiveStatus(status) {
	return AGENT_ACTIVE_STATUSES.has(String(status || ""));
}

function agentRecentPriority(item) {
	const kind = String(item?.kind || item?.source?.kind || item?.source?.type || "");
	if (kind === "agent_control") return 0;
	if (["task_cancelled", "task_failed", "task_completed", "task_interrupted"].includes(kind)) return 2;
	return 1;
}

function compareAgentRecent(a, b) {
	const leftTime = Number(a.timeMs || 0);
	const rightTime = Number(b.timeMs || 0);
	const delta = leftTime - rightTime;
	if (Math.abs(delta) <= 1000) {
		const priority = agentRecentPriority(a) - agentRecentPriority(b);
		if (priority) return priority;
	}
	return delta || a.order - b.order;
}

function pushSortedTail(items, item, limit) {
	if (!limit) return;
	let idx = items.length;
	while (idx > 0 && compareAgentRecent(item, items[idx - 1]) < 0) idx -= 1;
	items.splice(idx, 0, item);
	if (items.length > limit) items.shift();
}

function recentSourceList(payload, toolName) {
	const sources = [];
	const push = (items, fallbackAgent = "", taskStatus = "") => {
		if (Array.isArray(items) && items.length) sources.push({items, fallbackAgent, taskStatus});
	};
	push(payload?.recentEvents, toolName, payload?.status || "");
	if (Array.isArray(payload?.results)) {
		for (const item of payload.results) {
			const task = item?.task || item?.result?.task || {};
			const session = item?.agentSession || item?.result?.agentSession || {};
			const itemStatus = String(task.status || item?.status || item?.result?.status || "");
			const fallbackAgent = taskDisplayName(session.title || session.agentKey || task.currentAgent || item?.agent || "Agent", task.taskUuid || item?.taskUuid, task.displayName || item?.displayName);
			push(item?.recentEvents, fallbackAgent, itemStatus);
			push(item?.result?.recentEvents, fallbackAgent, itemStatus);
			push(task?.recentEvents, fallbackAgent, itemStatus);
		}
	}
	const taskPayload = payloadTask(payload);
	push(taskPayload?.recentEvents, eventAgentName(payload, toolName), taskPayload?.status || payload?.status || "");
	return sources;
}


function isHiddenAgentRecentEvent(item) {
	const kind = String(item?.kind || item?.type || "");
	const summary = String(item?.summary || item?.message || item?.currentStatus || "");
	if (kind === "control_requested") return true;
	if (kind === "task_cancelled" && /清理无运行协程|运行协程不存在|Rath 任务/.test(summary)) return true;
	if (kind === "task_interrupted" && /清理无运行协程|运行协程不存在|Rath 任务/.test(summary)) return true;
	return false;
}

function agentRecentCandidate(item, fallbackAgent = "", order = 0, taskStatus = "") {
	if (!item || typeof item !== "object") return null;
	if (isHiddenAgentRecentEvent(item)) return null;
	const kind = String(item?.kind || item?.type || "event");
	if (kind === "tool_call_finished") return null;
	const taskUuid = String(item?.taskUuid || item?.task_uuid || item?.task?.taskUuid || item?.task?.task_uuid || "").trim();
	const seq = Number(item?.seq || item?.sequence || item?.eventSeq || item?.id || 0) || 0;
	const agent = eventAgentName(item, fallbackAgent);
	let message = "";
	let key = "";
	if (taskUuid && seq) {
		key = `${taskUuid}|${seq}|${kind}`;
	} else {
		message = recentEventMessage(item);
		if (!message) return null;
		const detail = item?.detail && typeof item.detail === "object" ? item.detail : {};
		const semantic = [agent, kind, detail.name || detail.modelLabel || detail.model || detail.round || "", message].filter(Boolean).join("|");
		key = `${semantic || "agent"}|${seq || order}`;
	}
	return {
		source: item,
		key,
		taskUuid,
		seq,
		kind,
		order: seq || order,
		timeMs: eventTimeMs(item),
		elapsedMs: Number(item?.elapsedMs || item?.elapsed_ms || 0),
		agent,
		message,
		taskActive: isAgentRecentActiveStatus(taskStatus),
	};
}

function renderAgentRecentCandidate(item, displaySeq) {
	const message = item.message || recentEventMessage(item.source);
	if (!message) return null;
	const elapsed = item.elapsedMs;
	const detail = item.source?.detail && typeof item.source.detail === "object" ? item.source.detail : {};
	return {
		key: item.key,
		taskUuid: item.taskUuid,
		seq: item.seq,
		kind: item.kind,
		order: item.order,
		timeMs: item.timeMs,
		elapsedMs: elapsed,
		timeLabel: item.timeMs ? fmtEventClock(item.timeMs) : (elapsed ? `+${fmtMs(elapsed)}` : "—"),
		agent: item.agent,
		detail,
		toolName: String(detail.name || item.source?.name || "Tool"),
		rawArguments: detail.arguments ?? item.source?.arguments ?? "",
		message,
		taskActive: item.taskActive,
		displaySeq,
	};
}

function selectAgentRecentCandidates(candidates) {
	const allTail = [];
	const activeTail = [];
	const terminalTail = [];
	for (const item of candidates) {
		pushSortedTail(allTail, item, AGENT_RECENT_LINE_LIMIT);
		if (item.taskActive) pushSortedTail(activeTail, item, AGENT_RECENT_LINE_LIMIT);
		else pushSortedTail(terminalTail, item, AGENT_RECENT_TERMINAL_CONTEXT);
	}
	if (!activeTail.length) return allTail;
	const selected = [...terminalTail, ...activeTail.slice(-(AGENT_RECENT_LINE_LIMIT - terminalTail.length))];
	selected.sort(compareAgentRecent);
	return selected;
}

export function agentRecentLines(event) {
	if (!event || typeof event !== "object") return [];
	const payload = agentPayload(event);
	const toolName = eventPrimaryToolName(event);
	const cached = agentRecentMemo.get(event);
	if (cached && cached.payload === payload && cached.toolName === toolName) return cached.value;
	const unique = new Map();
	let order = 0;
	for (const source of recentSourceList(payload, toolName)) {
		for (const raw of source.items) {
			order += 1;
			const item = agentRecentCandidate(raw, source.fallbackAgent, order, source.taskStatus);
			if (!item) continue;
			const existing = unique.get(item.key);
			if (!existing || compareAgentRecent(item, existing) < 0) unique.set(item.key, item);
		}
	}
	const selected = selectAgentRecentCandidates(unique.values());
	const startSeq = Math.max(1, unique.size - selected.length + 1);
	const value = selected
		.map((item, idx) => renderAgentRecentCandidate(item, item.seq || (idx + startSeq)))
		.filter(Boolean);
	agentRecentMemo.set(event, {payload, toolName, value});
	return value;
}

function agentOutputParts(result, fallback = "") {
	const output = result?.output && typeof result.output === "object" ? result.output : null;
	const taskOutput = result?.task?.output && typeof result.task.output === "object" ? result.task.output : null;
	const source = output || taskOutput || result || {};
	const firstSegment = source?.firstSegment && typeof source.firstSegment === "object" ? source.firstSegment : null;
	const parts = [];
	const summary = String(result?.summary || output?.summary || taskOutput?.summary || result?.message || fallback || "").trim();
	if (summary) parts.push(summary);
	const segmentText = String(firstSegment?.content || "").trim();
	if (segmentText && segmentText !== summary) parts.push(`【首段输出 ${Number(firstSegment.segmentIndex || 0) + 1}/${source.segmentCount || "?"}】\n${segmentText}`);
	if (!parts.length && typeof result?.output === "string") parts.push(result.output.trim());
	return {parts, source, output, taskOutput};
}

function hasAgentOutputContent(result, fallback = "") {
	const output = result?.output && typeof result.output === "object" ? result.output : null;
	const taskOutput = result?.task?.output && typeof result.task.output === "object" ? result.task.output : null;
	const source = output || taskOutput || result || {};
	const firstSegment = source?.firstSegment && typeof source.firstSegment === "object" ? source.firstSegment : null;
	if (String(result?.summary || output?.summary || taskOutput?.summary || result?.message || fallback || "").trim()) return true;
	if (String(firstSegment?.content || "").trim()) return true;
	return typeof result?.output === "string" && Boolean(result.output.trim());
}

function agentOutputSection(entry) {
	const {parts, source, output, taskOutput} = agentOutputParts(entry.result, entry.fallback);
	const text = parts.filter(Boolean).join("\n\n").trim();
	if (!text) return null;
	return {
		title: entry.title || "Agent 输出",
		text,
		artifactUuid: entry.result?.artifactUuid || output?.artifactUuid || taskOutput?.artifactUuid || "",
		segmented: Boolean(source?.segmented),
		segmentCount: Number(source?.segmentCount || 0),
		originalChars: Number(source?.originalChars || 0),
	};
}

export function agentRowArgumentsDisplay(event, row, rowIndex = 0) {
	const item = row?.argItem;
	if (item && typeof item === "object") {
		const pretty = humanizeAgentArguments(item);
		if (pretty) return previewText(pretty, TOOL_ARGUMENT_PREVIEW_CHARS, "Agent 参数");
		try { return previewText(JSON.stringify(item, null, 2), TOOL_ARGUMENT_PREVIEW_CHARS, "Agent 参数"); }
		catch { return String(item); }
	}
	const rows = agentRows(event);
	if (rows.length <= 1 || Number(rowIndex || 0) === 0) return agentArgumentsDisplay(event);
	return "";
}

export function agentRowOutputSection(row) {
	if (!row?.hasOutput) return null;
	return agentOutputSection({
		title: row.title || row.name || "Agent 输出",
		result: row.resultPayload || {},
		fallback: row.outputFallback || row.error || "",
	});
}

export function agentOutputDisplay(text) {
	return previewText(String(text || "").trim(), AGENT_OUTPUT_PREVIEW_CHARS, "Agent 输出");
}
