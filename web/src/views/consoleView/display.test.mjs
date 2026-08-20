import assert from "node:assert/strict";
import {readFile, unlink, writeFile} from "node:fs/promises";
import test from "node:test";

const sourceUrl = new URL("./display.js", import.meta.url);
const generatedUrl = new URL(`./.display-test-${process.pid}.mjs`, import.meta.url);

let display;
try {
	const source = await readFile(sourceUrl, "utf8");
	const nodeTestSource = source
		.replace('import ContextCompactionIcon from "./ContextCompactionIcon.vue";', "const ContextCompactionIcon = {};")
		.replace('import {plainText} from "./markdown.js";', 'const plainText = (value) => String(value || "");');
	await writeFile(generatedUrl, nodeTestSource);
	display = await import(`${generatedUrl.href}?test=${Date.now()}`);
} finally {
	await unlink(generatedUrl).catch(() => {});
}

test("model retry uses human summary and never puts nested HTTP JSON on the main line", () => {
	const raw = 'HTTP 503: {"error":{"message":"HTTP 429: too many requests"}}';
	assert.equal(display.modelRetryReasonLabel({
		reason: "rate_limit",
		summary: "当前账户请求过于频繁，请稍后再试",
		error: raw,
	}), "当前账户请求过于频繁，请稍后再试");
	assert.equal(display.modelRetryReasonLabel({reason: "rate_limit", error: raw}), "上游限流");
	assert.equal(display.modelRetryReasonLabel({reason: "billing", error: raw}), "上游余额/额度不足");
	assert.ok(!display.modelRetryReasonLabel({reason: "rate_limit", error: raw}).includes("{"));
});


test("Agent activity uses Read description before path metadata", () => {
	const message = display.recentEventMessage({
		kind: "tool_call_started",
		detail: {
			name: "Read",
			arguments: JSON.stringify({
				path: "app/tools/files.py",
				offset: 0,
				limit: 2000,
				description: "核对 Read 工具的 schema",
			}),
		},
	});
	assert.equal(message, "调用工具 Read · 核对 Read 工具的 schema");
});


test("Agent launch failure without taskUuid does not keep a queued fallback row", () => {
	const resultText = JSON.stringify({ok: false, error: "agent_preset_not_found", status: "failed"});
	const event = {
		kind: "tool",
		live: false,
		toolName: "Agent",
		calls: [{
			id: "failed-launch",
			name: "Agent",
			arguments: JSON.stringify({workerType: "missing-preset", description: "审查代码"}),
		}],
		result: {content: resultText},
		livePayload: {
			toolName: "Agent",
			status: "failed",
			taskUuid: "",
			resultText,
		},
		operation: {
			status: "failed",
			lifecycle: "terminal",
			payload: {status: "failed"},
		},
	};

	const state = display.agentDisplayState(event);
	assert.equal(state.summary.cls, "error");
	assert.equal(state.summary.label, "执行失败");
	assert.equal(state.rows.length, 1);
	assert.equal(state.rows[0].status, "failed");
	assert.equal(state.rows[0].current, "启动失败");
});
