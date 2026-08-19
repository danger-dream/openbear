import assert from "node:assert/strict";
import test from "node:test";
import {
	buildToolArgumentsView,
	isStructuredToolArguments,
	languageForToolPath,
	toolArgumentsRawPayload,
	toolArgumentsSummary,
} from "./toolArgumentsPresentation.js";
import {taskMemoryToolPreview} from "./taskMemoryPresentation.js";

function tag(view, label) {
	return view.tags.find((item) => item.label === label)?.value || "";
}

function block(view, label) {
	return view.blocks.find((item) => item.label === label);
}

test("Bash renders highlighted command and translated compatibility metadata", () => {
	const view = buildToolArgumentsView("Bash", JSON.stringify({
		command: "uv run pytest -q",
		description: "运行后端测试",
		cwd: "/opt/src-space/openbear",
		timeout: 120,
		run_in_background: true,
	}));
	assert.equal(view.mode, "structured");
	assert.equal(view.summary, "运行后端测试");
	assert.equal(tag(view, "超时"), "120 秒");
	assert.equal(tag(view, "请求后台"), "是（兼容参数）");
	assert.equal(tag(view, "执行方式"), "等待完成");
	assert.equal(block(view, "Shell 命令").language, "bash");
	assert.equal(block(view, "Shell 命令").content, "uv run pytest -q");
});

test("Read preserves explicit zero and false values", () => {
	const view = buildToolArgumentsView("Read", {
		path: "app/main.py",
		offset: 0,
		limit: 80,
		force: false,
		description: "查看 Read schema",
	});
	assert.equal(view.summary, "查看 Read schema");
	assert.equal(tag(view, "说明"), "查看 Read schema");
	assert.equal(view.rows[0].value, "app/main.py");
	assert.equal(tag(view, "起始行"), "0");
	assert.equal(tag(view, "读取行数"), "80 行");
	assert.equal(tag(view, "强制读取"), "否");
});

test("Write infers code language from the target path", () => {
	const view = buildToolArgumentsView("Write", {path: "app/worker.py", content: "print('ok')\n"});
	assert.equal(block(view, "写入内容").language, "python");
	assert.match(tag(view, "内容大小"), /字符$/);
	assert.equal(toolArgumentsSummary("Write", {path: "app/worker.py", content: "x"}), "app/worker.py");
});

test("Edit presents before and after blocks without losing extra fields", () => {
	const view = buildToolArgumentsView("Edit", {
		path: "web/src/App.vue",
		old_string: "const old = true",
		new_string: "const next = true",
		replace_all: true,
		custom_flag: "kept",
	});
	assert.equal(block(view, "替换前").role, "old");
	assert.equal(block(view, "替换后").role, "new");
	assert.equal(block(view, "替换后").language, "html");
	assert.equal(tag(view, "替换全部"), "是");
	assert.match(block(view, "其他参数").content, /custom_flag/);
	assert.equal(block(view, "其他参数").secondary, true);
	assert.equal(block(view, "其他参数").itemCount, 1);
});

test("Edit batch presents every before/after pair and replace-all intent", () => {
	const view = buildToolArgumentsView("Edit", {
		path: "app/example.py",
		edits: [
			{old_string: "a = 1", new_string: "a = 2"},
			{old_string: "debug = true", new_string: "debug = false", replace_all: true},
		],
	});
	assert.equal(view.summary, "批量编辑 2 段");
	assert.equal(tag(view, "编辑模式"), "批量编辑 2 段");
	assert.equal(tag(view, "第 1 段"), "单处匹配");
	assert.equal(tag(view, "第 2 段"), "全部匹配");
	assert.equal(block(view, "第 1 段 · 替换前").content, "a = 1");
	assert.equal(block(view, "第 1 段 · 替换后").content, "a = 2");
	assert.equal(block(view, "第 2 段 · 替换后").role, "new");
	assert.equal(block(view, "第 2 段 · 替换后").language, "python");
});

test("Memory secret fields are named but their values stay hidden", () => {
	const secretValue = "do-not-render-this-token";
	const view = buildToolArgumentsView("Memory", {
		resource: "secret",
		action: "set",
		name: "github",
		kvJson: JSON.stringify([{key: "token", value: secretValue}, {key: "username", value: "bear"}]),
	});
	const serialized = JSON.stringify(view);
	assert.equal(tag(view, "资源"), "凭证");
	assert.equal(tag(view, "操作"), "写入");
	assert.match(tag(view, "凭证字段"), /token、username/);
	assert.doesNotMatch(serialized, new RegExp(secretValue));
});

test("raw tool payload keeps one JSON document and still masks Memory secret values", () => {
	const readPayload = toolArgumentsRawPayload("Read", {path: "app/main.py", offset: 0, force: false});
	assert.equal(readPayload.language, "json");
	assert.deepEqual(JSON.parse(readPayload.content), {path: "app/main.py", offset: 0, force: false});

	const secretValue = "do-not-render-this-token";
	const secretPayload = toolArgumentsRawPayload("Memory", {
		resource: "secret",
		action: "set",
		kvJson: JSON.stringify([{key: "token", value: secretValue}]),
	});
	assert.equal(secretPayload.language, "json");
	assert.match(secretPayload.content, /token/);
	assert.doesNotMatch(secretPayload.content, new RegExp(secretValue));
});

test("TaskMemory collapsed preview names the action and operated memory", () => {
	const result = JSON.stringify({
		ok: true,
		memory: {
			memoryUuid: "mem_1234567890abcdef1234567890abcdef",
			name: "部署限制",
			description: "完整描述",
			body: "完整正文",
			revision: 4,
		},
	});
	for (const [action, label] of [
		["get", "读取记忆"],
		["create", "创建记忆"],
		["update", "更新记忆"],
		["delete", "删除记忆"],
		["restore", "恢复记忆"],
	]) {
		const preview = taskMemoryToolPreview(JSON.stringify({
			action,
			memoryUuid: "mem_1234567890abcdef1234567890abcdef",
			name: "部署限制",
			description: "完整描述",
			body: "完整正文",
		}), result);
		assert.equal(preview, `${label}： 完整描述`);
	}

	assert.equal(
		taskMemoryToolPreview(JSON.stringify({
			action: "get", memoryUuid: "mem_1234567890abcdef1234567890abcdef",
		})),
		"读取记忆： mem_1234567890…bcdef",
	);
	assert.equal(
		taskMemoryToolPreview(JSON.stringify({action: "create", name: "当前工作目标"})),
		"创建记忆： 当前工作目标",
	);
	assert.equal(
		toolArgumentsSummary("TaskMemory", JSON.stringify({
			action: "update", memoryUuid: "mem_1234567890abcdef1234567890abcdef",
		})),
		"更新记忆： mem_1234567890…bcdef",
	);
	assert.equal(taskMemoryToolPreview(JSON.stringify({action: "list"})), "获取记忆列表");
	assert.equal(
		taskMemoryToolPreview(JSON.stringify({action: "search", query: "部署限制"})),
		"搜索记忆： 部署限制",
	);
});

test("Memory omits default and empty optional fields", () => {
	const view = buildToolArgumentsView("Memory", {
		resource: "entry",
		action: "get",
		name: "parrot-core",
		id: 0,
		expanded: false,
		fieldsJson: "",
		body: "",
		content: "",
		summary: "",
		kvJson: "",
	});
	assert.deepEqual(view.tags.map((item) => item.label), ["操作", "资源", "名称"]);
	assert.equal(view.tags.find((item) => item.label === "操作").primary, true);
	assert.equal(view.tags.find((item) => item.label === "名称").primary, true);
	assert.equal(view.blocks.length, 0);
});

test("History translates and consumes its complete schema", () => {
	const view = buildToolArgumentsView("History", {
		action: "read_turn",
		scope: "explicit",
		conversationUuid: "conversation-1",
		turnUuid: "turn-2",
		query: "Agent 结论",
		from: "end",
		turns: 10,
		lastTurns: 5,
		before: 2,
		after: 3,
		limit: 20,
		maxChars: 20000,
		maxSnippetChars: 500,
		excludeCurrentTurn: true,
		includeNotices: false,
		includeArchived: true,
	});
	assert.equal(tag(view, "操作"), "读取指定轮次");
	assert.equal(tag(view, "范围"), "指定对话");
	assert.equal(tag(view, "读取位置"), "从末尾");
	assert.equal(tag(view, "输出上限"), "20000 字符");
	assert.equal(tag(view, "包含通知"), "否");
	assert.equal(view.rows.find((item) => item.label === "查询内容").primary, true);
	assert.equal(view.blocks.length, 0);
});

test("Context compaction highlights translated source and threshold", () => {
	const view = buildToolArgumentsView("ContextCompaction", {source: "turn_epilogue", thresholdTokens: 180000});
	assert.equal(tag(view, "触发阶段"), "本轮结束后");
	assert.equal(tag(view, "压缩阈值"), "180,000 tokens");
	assert.equal(view.tags.every((item) => item.primary), true);
	const manual = buildToolArgumentsView("ContextCompaction", {source: "manual"});
	assert.equal(tag(manual, "触发阶段"), "手动触发");
});

test("Agent Plan tools expose their workflow-critical fields", () => {
	const submit = buildToolArgumentsView("AgentPlanSubmit", {
		plan: {title: "只读核验", objective: "核对版本", steps: [{id: "s1"}], finalOutputs: [{id: "f1"}]},
		requestId: "req-1",
	});
	assert.equal(tag(submit, "计划标题"), "只读核验");
	assert.equal(tag(submit, "执行步骤"), "1 项");
	assert.equal(submit.rows.find((item) => item.label === "目标").primary, true);
	assert.ok(block(submit, "完整计划"));
	const progress = buildToolArgumentsView("AgentPlanProgress", {
		action: "complete",
		stepId: "s1",
		result: "版本已核对",
		criteria: [{id: "c1", status: "satisfied", evidence: ["ev-1"]}],
		evidence: [{type: "file", reference: "app/__init__.py", summary: "版本"}],
		finalOutputs: [],
		requestId: "req-2",
	});
	assert.equal(tag(progress, "进度动作"), "完成步骤");
	assert.equal(tag(progress, "步骤 ID"), "s1");
	assert.ok(block(progress, "完成条件"));
	assert.ok(block(progress, "验收证据"));
	assert.equal(progress.blocks.some((item) => item.label === "最终交付"), false);
	assert.equal(progress.blocks.some((item) => item.label === "其他参数"), false);
});

test("OpenBearControl gives action and reason primary hierarchy", () => {
	const view = buildToolArgumentsView("OpenBearControl", {action: "restart", reason: "应用配置", target: "gateway"});
	assert.equal(view.tags.find((item) => item.label === "操作").primary, true);
	assert.equal(view.rows.find((item) => item.label === "原因").primary, true);
});

test("Process localizes action and millisecond timeout", () => {
	const view = buildToolArgumentsView("Process", {action: "poll", session_id: "job-12", timeout: 30000});
	assert.equal(tag(view, "操作"), "查询状态");
	assert.equal(tag(view, "会话 ID"), "job-12");
	assert.equal(tag(view, "等待时间"), "30000 ms");
});

test("Web tools present query filters and URL", () => {
	const search = buildToolArgumentsView("WebSearch", {
		query: "OpenBear release",
		max_results: 8,
		content_types: "web,news",
		freshness: "week",
		zone: "intl",
	});
	assert.equal(search.rows[0].label, "搜索内容");
	assert.equal(tag(search, "时效"), "一周内");
	assert.equal(tag(search, "区域"), "国际");
	const extract = buildToolArgumentsView("WebExtract", {url: "https://example.com/docs/page"});
	assert.equal(extract.rows[0].kind, "url");
	assert.equal(extract.summary, "example.com");
	const unsafe = buildToolArgumentsView("WebExtract", {url: "javascript:alert(1)"});
	assert.equal(unsafe.rows[0].kind, "text");
});

test("unknown and malformed tool payloads keep a reliable raw fallback", () => {
	const unknown = buildToolArgumentsView("mcp__demo__lookup", {query: "bear", limit: 3});
	assert.equal(unknown.mode, "raw");
	assert.equal(block(unknown, "JSON 参数").language, "json");
	assert.match(block(unknown, "JSON 参数").content, /"query": "bear"/);
	const malformed = buildToolArgumentsView("Bash", "{not-json");
	assert.equal(malformed.mode, "raw");
	assert.equal(block(malformed, "原始参数").content, "{not-json");
});

test("tool registry and path language mapping stay deterministic", () => {
	assert.equal(isStructuredToolArguments("Bash"), true);
	assert.equal(isStructuredToolArguments("ContextCompaction"), true);
	assert.equal(isStructuredToolArguments("AgentPlanProgress"), true);
	assert.equal(isStructuredToolArguments("AgentPlanSubmit"), true);
	assert.equal(isStructuredToolArguments("mcp__demo__lookup"), false);
	assert.equal(languageForToolPath("Dockerfile"), "dockerfile");
	assert.equal(languageForToolPath("src/view.tsx"), "typescript");
	assert.equal(languageForToolPath("README.unknown"), "");
});
