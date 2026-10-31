import assert from "node:assert/strict";
import {register} from "node:module";
import test from "node:test";

// display.js pulls in SFC icons and highlight styles; neither matters to this decision.
register(`data:text/javascript,${encodeURIComponent(`
  export function load(url, context, nextLoad) {
    if (url.endsWith(".css") || url.endsWith(".vue")) return {format: "module", source: "export default {};", shortCircuit: true};
    return nextLoad(url, context);
  }
`)}`, import.meta.url);
const {toolFileIdentityFor} = await import("./toolFileAction.js");

const conversation = "39a541d4-4d9c-4a58-87d6-1b276779954a";
const event = (name, {opId = `tool:${name}-1`, result = "ok", calls = 1, status = "completed", events} = {}) => ({
	kind: "tool",
	operation: {opId, status, payload: {toolName: name}},
	calls: Array.from({length: calls}, (_, index) => ({id: `${name}-${index}`, name, arguments: "{\"path\": \"/tmp/a.txt\"}"})),
	results: result === null ? [] : [{toolCallId: `${name}-0`, name, content: result}],
	...(events ? {events} : {}),
});

test("finished single file-tool calls get a file identity for their own operation", () => {
	for (const name of ["Read", "Write", "Edit", "EditBatch"]) {
		const identity = toolFileIdentityFor(event(name), conversation);
		assert.equal(identity?.artifactUuid, `tool:${name}-1`, name);
		assert.equal(identity.toolFile, true);
	}
});

test("other tools, running or failed calls, batches, groups and local conversations get none", () => {
	assert.equal(toolFileIdentityFor(event("Bash"), conversation), null);
	assert.equal(toolFileIdentityFor(event("Read", {result: null, status: "running"}), conversation), null);
	assert.equal(toolFileIdentityFor(event("Write", {result: "[错误] 文件不存在", status: "failed"}), conversation), null);
	assert.equal(toolFileIdentityFor(event("Read", {calls: 2}), conversation), null);
	assert.equal(toolFileIdentityFor(event("Read", {events: [event("Read")]}), conversation), null);
	assert.equal(toolFileIdentityFor(event("Read", {opId: ""}), conversation), null);
	assert.equal(toolFileIdentityFor(event("Read"), "local:draft"), null);
	assert.equal(toolFileIdentityFor(event("Read"), ""), null);
});
