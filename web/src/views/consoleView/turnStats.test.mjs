import test from "node:test";
import assert from "node:assert/strict";
import {reconcileAgentTaskUsage} from "./turnStats.js";

const taskA = {
	taskUuid: "agent-a",
	status: "completed",
	tokens: {input: 708981, output: 7458, cache: 653056},
};

const taskB = {
	taskUuid: "agent-b",
	status: "failed",
	tokens: {input: 2315288, output: 12851, cache: 2217216},
};

test("missing Agent usage is merged from terminal cards with prompt/cache semantics", () => {
	const stats = {
		usage: {inputTokens: 100, outputTokens: 20, cacheReadTokens: 900, cacheWriteTokens: 0},
		expertUsage: {inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, totalTokens: 0},
		expertTaskUuids: [],
	};
	const result = reconcileAgentTaskUsage(stats, [taskA, taskB]);
	assert.deepEqual(result.expertUsage, {
		inputTokens: 153997,
		outputTokens: 20309,
		cacheReadTokens: 2870272,
		cacheWriteTokens: 0,
		totalTokens: 3044578,
	});
	assert.deepEqual(result.expertTaskUuids, ["agent-a", "agent-b"]);
	assert.equal(result.expertTasks, 2);
});

test("duplicate cards and task UUIDs already present in stats are never counted twice", () => {
	const stats = {
		expertUsage: {inputTokens: 55925, outputTokens: 7458, cacheReadTokens: 653056, cacheWriteTokens: 0, totalTokens: 716439},
		expertTaskUuids: ["agent-a"],
	};
	const result = reconcileAgentTaskUsage(stats, [taskA, taskA, taskB]);
	assert.deepEqual(result.expertTaskUuids, ["agent-a", "agent-b"]);
	assert.deepEqual(result.expertUsage, {
		inputTokens: 153997,
		outputTokens: 20309,
		cacheReadTokens: 2870272,
		cacheWriteTokens: 0,
		totalTokens: 3044578,
	});
});

test("legacy stats with non-zero Agent usage are preserved when task IDs are unavailable", () => {
	const stats = {
		expertUsage: {inputTokens: 10, outputTokens: 2, cacheReadTokens: 90, cacheWriteTokens: 0, totalTokens: 102},
	};
	assert.equal(reconcileAgentTaskUsage(stats, [taskA]), stats);
});

test("running Agent cards are excluded until they reach a stable accounting boundary", () => {
	const stats = {expertUsage: {}, expertTaskUuids: []};
	const result = reconcileAgentTaskUsage(stats, [{...taskA, status: "running"}]);
	assert.equal(result, stats);
});
