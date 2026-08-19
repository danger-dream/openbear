import {contextCompactionSourceLabel} from "./agentPlanPresentation.js";
import {taskMemoryToolPreview} from "./taskMemoryPresentation.js";

const HAS = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key);

const LANGUAGE_BY_EXTENSION = {
	bash: "bash",
	c: "c",
	cc: "cpp",
	cpp: "cpp",
	cs: "csharp",
	css: "css",
	go: "go",
	h: "c",
	hpp: "cpp",
	html: "html",
	ini: "ini",
	java: "java",
	js: "javascript",
	json: "json",
	jsx: "javascript",
	kt: "kotlin",
	less: "less",
	lua: "lua",
	md: "markdown",
	mjs: "javascript",
	php: "php",
	py: "python",
	rb: "ruby",
	rs: "rust",
	scala: "scala",
	scss: "scss",
	sh: "bash",
	sql: "sql",
	svelte: "html",
	toml: "ini",
	ts: "typescript",
	tsx: "typescript",
	vue: "html",
	xml: "xml",
	yaml: "yaml",
	yml: "yaml",
};

const ACTION_LABELS = {
	approve: "批准",
	block: "标记阻塞",
	cancel: "取消",
	complete: "完成步骤",
	confirm: "确认",
	del: "删除",
	delete: "删除",
	edit: "编辑",
	finalize: "最终确认",
	get: "读取",
	kill: "终止",
	list: "列出",
	log: "查看日志",
	message: "发送消息",
	models: "查看模型",
	mcp_status: "MCP 状态",
	poll: "查询状态",
	prompt: "输入",
	read: "读取",
	read_turn: "读取指定轮次",
	remove: "移除",
	request_replan: "请求重新规划",
	restart: "重启",
	resume: "恢复",
	revise: "要求修改",
	search: "搜索",
	select: "选择",
	set: "写入",
	skills_reload: "重载 Skills",
	skills_status: "Skills 状态",
	start: "开始步骤",
	status: "查看状态",
	steer: "补充/纠偏",
	stop: "停止",
	update: "更新进度",
	write: "写入",
};

const RESOURCE_LABELS = {
	doc: "文档",
	entry: "记忆",
	identity: "身份",
	secret: "凭证",
};

const FRESHNESS_LABELS = {
	day: "一天内",
	month: "一月内",
	week: "一周内",
	year: "一年内",
};

const ZONE_LABELS = {cn: "中国", intl: "国际"};
const HISTORY_SCOPE_LABELS = {current: "当前对话", explicit: "指定对话", conversation: "指定对话"};
const HISTORY_FROM_LABELS = {start: "从开头", end: "从末尾"};
function asText(value) {
	if (value === undefined || value === null) return "";
	if (typeof value === "string") return value;
	if (typeof value === "boolean") return value ? "是" : "否";
	if (typeof value === "number") return String(value);
	try {
		return JSON.stringify(value);
	} catch {
		return String(value);
	}
}

function prettyJson(value) {
	if (typeof value === "string") {
		const trimmed = value.trim();
		if (!trimmed) return "";
		try {
			return JSON.stringify(JSON.parse(trimmed), null, 2);
		} catch {
			return value;
		}
	}
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return asText(value);
	}
}

function parseArguments(rawArguments) {
	if (rawArguments && typeof rawArguments === "object") {
		return {data: rawArguments, raw: prettyJson(rawArguments), parsed: true};
	}
	const raw = String(rawArguments || "").trim();
	if (!raw) return {data: null, raw: "", parsed: false};
	try {
		return {data: JSON.parse(raw), raw, parsed: true};
	} catch {
		const objectStart = raw.indexOf("{");
		if (objectStart > 0) {
			try {
				return {data: JSON.parse(raw.slice(objectStart)), raw, parsed: true};
			} catch {
				// Keep the original payload as the reliable fallback.
			}
		}
		return {data: null, raw, parsed: false};
	}
}

function redactedMemorySecretArguments(value) {
	if (String(value?.resource || "").trim().toLowerCase() !== "secret" || !HAS(value, "kvJson")) return value;
	const fields = secretFieldNames(value.kvJson);
	return {
		...value,
		kvJson: fields.length
			? `[已隐藏：${fields.join("、")}（${fields.length} 项凭证字段）]`
			: "[已隐藏凭证值]",
	};
}

// The detailed tool card deliberately shows one faithful payload document instead
// of reconstructing it into tool-specific tags and cards. Keep the existing
// Memory-secret masking rule so a simpler presentation never widens disclosure.
export function toolArgumentsRawPayload(toolName, rawArguments) {
	const parsed = parseArguments(rawArguments);
	if (!parsed.raw && !parsed.data) return {content: "", language: ""};
	if (!parsed.parsed || !parsed.data || typeof parsed.data !== "object" || Array.isArray(parsed.data)) {
		return {content: parsed.raw || prettyJson(parsed.data), language: ""};
	}
	const data = String(toolName || "").trim().toLowerCase() === "memory"
		? redactedMemorySecretArguments(parsed.data)
		: parsed.data;
	return {content: prettyJson(data), language: "json"};
}

function firstLine(value) {
	return String(value || "").trim().split(/\r?\n/, 1)[0] || "";
}

function compact(value, limit = 96) {
	const text = String(value || "").replace(/\s+/g, " ").trim();
	return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}

function boolLabel(value) {
	return Boolean(value) ? "是" : "否";
}

function hasDisplayContent(value) {
	if (value === undefined || value === null || value === "") return false;
	if (Array.isArray(value)) return value.length > 0;
	if (typeof value === "object") return Object.keys(value).length > 0;
	return true;
}

function actionLabel(value) {
	const raw = String(value || "").trim();
	return ACTION_LABELS[raw] || raw;
}

export function languageForToolPath(path) {
	const value = String(path || "").trim();
	const basename = value.split(/[\\/]/).pop()?.toLowerCase() || "";
	if (["dockerfile", "containerfile"].includes(basename)) return "dockerfile";
	if (["makefile", "gnumakefile"].includes(basename)) return "makefile";
	const dot = basename.lastIndexOf(".");
	if (dot < 0) return "";
	return LANGUAGE_BY_EXTENSION[basename.slice(dot + 1)] || "";
}

class ViewBuilder {
	constructor(toolName, data) {
		this.toolName = toolName;
		this.data = data;
		this.consumed = new Set();
		this.tags = [];
		this.rows = [];
		this.blocks = [];
	}

	has(key) {
		return HAS(this.data, key);
	}

	take(key) {
		if (!this.has(key)) return undefined;
		this.consumed.add(key);
		return this.data[key];
	}

	takeFirst(keys) {
		let selected;
		for (const key of keys) {
			if (!this.has(key)) continue;
			const value = this.take(key);
			if (selected === undefined || selected === null || selected === "") selected = value;
		}
		return selected;
	}

	addTag(label, value, options = {}) {
		if (value === undefined || value === null || value === "") return;
		this.tags.push({
			label,
			value: asText(value),
			mono: Boolean(options.mono),
			wide: Boolean(options.wide),
			primary: Boolean(options.primary),
		});
	}

	addRow(label, value, options = {}) {
		if (value === undefined || value === null || value === "") return;
		this.rows.push({
			label,
			value: asText(value),
			kind: options.kind || "text",
			mono: Boolean(options.mono || ["path", "url"].includes(options.kind)),
			copyable: options.copyable !== false,
			primary: Boolean(options.primary),
		});
	}

	addBlock(label, value, language = "", options = {}) {
		if ((value === undefined || value === null) || (value === "" && !options.allowEmpty)) return;
		this.blocks.push({
			label,
			content: asText(value),
			language,
			role: options.role || "",
			secondary: Boolean(options.secondary),
			itemCount: Number(options.itemCount || 0),
		});
	}

	finish(summary = "") {
		const extras = {};
		for (const [key, value] of Object.entries(this.data)) {
			if (!this.consumed.has(key)) extras[key] = value;
		}
		const extraCount = Object.keys(extras).length;
		if (extraCount) this.addBlock("其他参数", prettyJson(extras), "json", {secondary: true, itemCount: extraCount});
		return {
			mode: "structured",
			toolName: this.toolName,
			summary: compact(summary),
			tags: this.tags,
			rows: this.rows,
			blocks: this.blocks,
		};
	}
}

function bashView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const command = view.take("command");
	const description = view.take("description");
	const cwd = view.take("cwd");
	const timeout = view.take("timeout");
	const background = view.takeFirst(["background", "run_in_background"]);
	view.addTag("说明", description, {wide: true, primary: true});
	view.addTag("工作目录", cwd, {mono: true, wide: true});
	if (timeout !== undefined && timeout !== null && timeout !== "") view.addTag("超时", `${timeout} 秒`);
	if (background !== undefined) {
		view.addTag("请求后台", `${boolLabel(background)}（兼容参数）`);
		view.addTag("执行方式", "等待完成");
	}
	view.addBlock("Shell 命令", command, "bash", {allowEmpty: true});
	return view.finish(description || firstLine(command));
}

function readView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const path = view.take("path");
	const description = view.take("description");
	view.addTag("说明", description, {wide: true, primary: true});
	view.addRow("文件路径", path, {kind: "path", primary: true});
	for (const [key, label, suffix] of [["offset", "起始行", ""], ["limit", "读取行数", " 行"]]) {
		const value = view.take(key);
		if (value !== undefined && value !== null && value !== "") view.addTag(label, `${value}${suffix}`);
	}
	const force = view.take("force");
	if (force !== undefined) view.addTag("强制读取", boolLabel(force));
	return view.finish(description || path);
}

function writeView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const path = view.take("path");
	const content = view.take("content");
	view.addRow("文件路径", path, {kind: "path", primary: true});
	if (content !== undefined && content !== null) view.addTag("内容大小", `${String(content).length.toLocaleString()} 字符`);
	view.addBlock("写入内容", content, languageForToolPath(path), {allowEmpty: true});
	return view.finish(path);
}

function editView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const path = view.take("path");
	const edits = view.take("edits");
	const language = languageForToolPath(path);
	view.addRow("文件路径", path, {kind: "path", primary: true});
	if (Array.isArray(edits)) {
		const count = edits.length;
		view.addTag("编辑模式", `批量编辑 ${count} 段`, {primary: true});
		edits.forEach((item, index) => {
			const edit = item && typeof item === "object" ? item : {};
			const number = index + 1;
			view.addTag(`第 ${number} 段`, edit.replace_all === true ? "全部匹配" : "单处匹配");
			view.addBlock(`第 ${number} 段 · 替换前`, edit.old_string, language, {role: "old", allowEmpty: true});
			view.addBlock(`第 ${number} 段 · 替换后`, edit.new_string, language, {role: "new", allowEmpty: true});
		});
		return view.finish(`批量编辑 ${count} 段`);
	}
	const before = view.takeFirst(["old_string", "oldString"]);
	const after = view.takeFirst(["new_string", "newString"]);
	const replaceAll = view.takeFirst(["replace_all", "replaceAll"]);
	if (replaceAll !== undefined) view.addTag("替换全部", boolLabel(replaceAll));
	view.addBlock("替换前", before, language, {role: "old", allowEmpty: true});
	view.addBlock("替换后", after, language, {role: "new", allowEmpty: true});
	return view.finish(path);
}

function secretFieldNames(value) {
	let parsed = value;
	if (typeof value === "string") {
		try { parsed = JSON.parse(value); } catch { return []; }
	}
	if (Array.isArray(parsed)) {
		return parsed.map((item) => item && typeof item === "object" ? item.key : "").filter(Boolean).map(String);
	}
	if (parsed && typeof parsed === "object") return Object.keys(parsed);
	return [];
}

function memoryView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const resource = view.takeFirst(["resource", "kind"]);
	const action = view.take("action");
	view.addTag("操作", actionLabel(action), {primary: true});
	view.addTag("资源", RESOURCE_LABELS[String(resource || "")] || resource);
	for (const [key, label, mono, primary] of [
		["ref", "引用", true, true], ["name", "名称", true, true], ["category", "分类", false, false],
		["project", "项目", false, false], ["tags", "标签", false, false], ["availableTo", "授权范围", false, false],
	]) {
		const value = view.take(key);
		view.addTag(label, value, {mono, wide: true, primary});
	}
	const id = view.take("id");
	if (Number(id || 0) > 0) view.addTag("ID", id, {mono: true, primary: true});
	const expanded = view.take("expanded");
	if (expanded === true) view.addTag("全量注入", "已启用");
	const note = view.take("note");
	view.addRow("备注", note, {copyable: true});
	for (const [key, label, language] of [
		["fieldsJson", "结构化字段", "json"], ["body", "记忆正文", "markdown"], ["content", "文档内容", "markdown"], ["summary", "摘要", "markdown"],
	]) {
		const value = view.take(key);
		if (value !== undefined && value !== null && value !== "") view.addBlock(label, language === "json" ? prettyJson(value) : value, language);
	}
	const kvJson = view.take("kvJson");
	if (kvJson !== undefined && kvJson !== null && kvJson !== "") {
		if (String(resource || "") === "secret") {
			const fields = secretFieldNames(kvJson);
			view.addTag("凭证字段", fields.length ? `${fields.join("、")}（${fields.length} 项，值已隐藏）` : "值已隐藏", {wide: true, primary: true});
		} else {
			view.addBlock("键值字段", prettyJson(kvJson), "json", {allowEmpty: true});
		}
	}
	return view.finish([actionLabel(action), RESOURCE_LABELS[String(resource || "")] || resource].filter(Boolean).join(" · "));
}

function processView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const action = view.take("action");
	const sessionId = view.takeFirst(["sessionId", "session_id", "jobId", "job_id"]);
	view.addTag("操作", actionLabel(action), {primary: true});
	view.addTag("会话 ID", sessionId, {mono: true, wide: true, primary: true});
	for (const [key, label, suffix] of [["offset", "起始行", ""], ["limit", "返回行数", " 行"], ["timeout", "等待时间", " ms"]]) {
		const value = view.take(key);
		if (value !== undefined && value !== null && value !== "") view.addTag(label, `${value}${suffix}`);
	}
	return view.finish([actionLabel(action), sessionId].filter(Boolean).join(" · "));
}

function webSearchView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const query = view.take("query");
	view.addRow("搜索内容", query, {copyable: true, primary: true});
	const maxResults = view.takeFirst(["max_results", "maxResults"]);
	if (maxResults !== undefined) view.addTag("结果数", maxResults);
	const contentTypes = view.takeFirst(["content_types", "contentTypes"]);
	view.addTag("内容类型", contentTypes, {wide: true});
	const freshness = view.take("freshness");
	view.addTag("时效", FRESHNESS_LABELS[String(freshness || "")] || freshness);
	const zone = view.take("zone");
	view.addTag("区域", ZONE_LABELS[String(zone || "")] || zone);
	return view.finish(query);
}

function webExtractView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const url = view.take("url");
	const safeHttpUrl = /^https?:\/\//i.test(String(url || "").trim());
	view.addRow("网页地址", url, {kind: safeHttpUrl ? "url" : "text", mono: true, primary: true});
	let summary = url;
	if (safeHttpUrl) {
		try { summary = new URL(String(url || "")).hostname || url; } catch { /* use the raw URL */ }
	}
	return view.finish(summary);
}

function historyView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const action = view.take("action");
	view.addTag("操作", actionLabel(action), {primary: true});
	const scope = view.take("scope");
	view.addTag("范围", HISTORY_SCOPE_LABELS[String(scope || "")] || scope);
	const conversationUuid = view.takeFirst(["conversationUuid", "conversation_uuid"]);
	const turnUuid = view.takeFirst(["turnUuid", "turn_uuid"]);
	view.addTag("对话 ID", conversationUuid, {mono: true, wide: true, primary: true});
	view.addTag("轮次 ID", turnUuid, {mono: true, wide: true, primary: true});
	const query = view.takeFirst(["query", "text"]);
	view.addRow("查询内容", query, {copyable: true, primary: true});
	const from = view.take("from");
	view.addTag("读取位置", HISTORY_FROM_LABELS[String(from || "")] || from);
	for (const [key, label, suffix] of [
		["turns", "读取轮数", " 轮"], ["lastTurns", "末尾轮数", " 轮"], ["before", "目标前", " 轮"], ["after", "目标后", " 轮"],
		["limit", "返回数量", " 条"], ["maxChars", "输出上限", " 字符"], ["maxSnippetChars", "摘要上限", " 字符"],
	]) {
		const value = view.take(key);
		if (value !== undefined && value !== null && value !== "") view.addTag(label, `${value}${suffix}`);
	}
	for (const [key, label] of [
		["excludeCurrentTurn", "排除当前轮"], ["includeNotices", "包含通知"], ["includeArchived", "包含归档"],
	]) {
		const value = view.take(key);
		if (value !== undefined) view.addTag(label, boolLabel(value));
	}
	return view.finish(query || actionLabel(action));
}

function controlView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const action = view.take("action");
	view.addTag("操作", actionLabel(action), {primary: true});
	const reason = view.take("reason");
	view.addRow("原因", reason, {primary: true});
	for (const [key, label] of [["target", "目标"], ["name", "名称"], ["query", "查询"]]) {
		const value = view.take(key);
		view.addTag(label, value, {wide: true});
	}
	for (const [key, label] of [["message", "消息"], ["instruction", "指令"], ["text", "内容"]]) {
		const value = view.take(key);
		view.addBlock(label, value, "markdown");
	}
	return view.finish(actionLabel(action));
}

function interactionView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const action = view.take("action");
	const title = view.take("title");
	view.addTag("交互类型", actionLabel(action), {primary: true});
	view.addTag("标题", title, {wide: true, primary: true});
	for (const [key, label, suffix] of [
		["type", "提示类型", ""], ["tone", "语气", ""], ["default", "默认确认", ""], ["multiple", "允许多选", ""],
		["sensitive", "敏感输入", ""], ["confirmText", "确认按钮", ""], ["cancelText", "取消按钮", ""], ["timeoutSeconds", "等待时间", " 秒"],
	]) {
		const value = view.take(key);
		if (value === undefined || value === null || value === "") continue;
		view.addTag(label, typeof value === "boolean" ? boolLabel(value) : `${value}${suffix}`, {wide: true});
	}
	const body = view.take("body");
	view.addBlock("正文", body, "markdown", {allowEmpty: true});
	const defaultValue = view.take("defaultValue");
	if (defaultValue !== undefined) view.addBlock("默认输入", defaultValue, "");
	return view.finish(title || actionLabel(action));
}

function agentWaitView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	for (const [key, label, suffix] of [
		["taskUuid", "任务 ID", ""], ["task_uuid", "任务 ID", ""], ["timeout", "等待时间", " ms"], ["timeoutSeconds", "等待时间", " 秒"],
	]) {
		const value = view.take(key);
		if (value !== undefined && value !== null && value !== "") view.addTag(label, `${value}${suffix}`, {mono: key.includes("Uuid") || key.includes("uuid"), wide: true, primary: key.includes("Uuid") || key.includes("uuid")});
	}
	return view.finish(view.tags.map((item) => item.value).join(" · "));
}

function contextCompactionView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const source = view.take("source");
	const thresholdTokens = view.take("thresholdTokens");
	view.addTag("触发阶段", contextCompactionSourceLabel(source), {primary: true});
	if (thresholdTokens !== undefined && thresholdTokens !== null && thresholdTokens !== "") {
		view.addTag("压缩阈值", `${Number(thresholdTokens).toLocaleString()} tokens`, {primary: true});
	}
	return view.finish(contextCompactionSourceLabel(source));
}

function planSubmitView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const rawPlan = view.take("plan");
	let plan = rawPlan;
	if (typeof rawPlan === "string") {
		try { plan = JSON.parse(rawPlan); } catch { plan = null; }
	}
	const title = plan && typeof plan === "object" ? plan.title : "";
	const objective = plan && typeof plan === "object" ? plan.objective : "";
	const steps = plan && typeof plan === "object" && Array.isArray(plan.steps) ? plan.steps : [];
	const finalOutputs = plan && typeof plan === "object" && Array.isArray(plan.finalOutputs) ? plan.finalOutputs : [];
	view.addTag("计划标题", title, {primary: true, wide: true});
	view.addRow("目标", objective, {primary: true});
	if (steps.length) view.addTag("执行步骤", `${steps.length} 项`);
	if (finalOutputs.length) view.addTag("最终交付", `${finalOutputs.length} 项`);
	const requestId = view.take("requestId");
	view.addTag("请求 ID", requestId, {mono: true, wide: true});
	view.addBlock("完整计划", prettyJson(rawPlan), "json");
	return view.finish(title || objective || "提交执行计划");
}

function planProgressView(toolName, data) {
	const view = new ViewBuilder(toolName, data);
	const action = view.take("action");
	const stepId = view.take("stepId");
	view.addTag("进度动作", actionLabel(action), {primary: true});
	view.addTag("步骤 ID", stepId, {mono: true, wide: true, primary: true});
	const result = view.take("result");
	view.addRow("步骤结果", result, {primary: true});
	const blocker = view.take("blocker");
	if (typeof blocker === "string") view.addRow("阻塞原因", blocker, {primary: true});
	else if (blocker !== undefined && blocker !== null) view.addBlock("阻塞信息", prettyJson(blocker), "json");
	for (const [key, label] of [["criteria", "完成条件"], ["evidence", "验收证据"], ["finalOutputs", "最终交付"]]) {
		const value = view.take(key);
		if (hasDisplayContent(value)) view.addBlock(label, prettyJson(value), "json");
	}
	const requestId = view.take("requestId");
	view.addTag("请求 ID", requestId, {mono: true, wide: true});
	return view.finish([actionLabel(action), stepId].filter(Boolean).join(" · "));
}

const PRESENTERS = {
	AgentPlanProgress: planProgressView,
	AgentPlanSubmit: planSubmitView,
	AgentWait: agentWaitView,
	Bash: bashView,
	ContextCompaction: contextCompactionView,
	Edit: editView,
	History: historyView,
	Memory: memoryView,
	OpenBearControl: controlView,
	Process: processView,
	Read: readView,
	UserInteraction: interactionView,
	WebExtract: webExtractView,
	WebSearch: webSearchView,
	Write: writeView,
};

export function isStructuredToolArguments(toolName) {
	return Boolean(PRESENTERS[String(toolName || "")]);
}

export function buildToolArgumentsView(toolName, rawArguments) {
	const name = String(toolName || "Tool");
	const parsed = parseArguments(rawArguments);
	if (!parsed.raw && !parsed.data) {
		return {mode: "empty", toolName: name, summary: "", tags: [], rows: [], blocks: []};
	}
	if (!parsed.parsed || !parsed.data || typeof parsed.data !== "object" || Array.isArray(parsed.data)) {
		return {
			mode: "raw",
			toolName: name,
			summary: "",
			tags: [],
			rows: [],
			blocks: [{label: "原始参数", content: parsed.raw || prettyJson(parsed.data), language: parsed.parsed ? "json" : "", role: ""}],
		};
	}
	const presenter = PRESENTERS[name];
	if (presenter) return presenter(name, parsed.data);
	return {
		mode: "raw",
		toolName: name,
		summary: "",
		tags: [],
		rows: [],
		blocks: [{label: "JSON 参数", content: prettyJson(parsed.data), language: "json", role: ""}],
	};
}

export function toolArgumentsSummary(toolName, rawArguments) {
	if (String(toolName || "") === "TaskMemory") return taskMemoryToolPreview(rawArguments);
	return buildToolArgumentsView(toolName, rawArguments).summary;
}
