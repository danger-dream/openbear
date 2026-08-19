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
