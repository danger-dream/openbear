import test from "node:test";
import assert from "node:assert/strict";
import {combineTimingStats, reconcileAgentTaskUsage, timingStatsFromRows} from "./turnStats.js";

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


test("timing averages use observed per-stage samples, never child counts", () => {
	const left = {modelOk: 81, expertModelCalls: 80, avgConnectMs: 10, connectSamples: 1,
		avgFirstTokenMs: 40, firstTokenSamples: 2, avgTotalMs: 100, totalTimeSamples: 3};
	const right = {modelOk: 90, avgConnectMs: null, connectSamples: 0,
		avgFirstTokenMs: 100, firstTokenSamples: 1, avgTotalMs: 500, totalTimeSamples: 1};
	assert.deepEqual(combineTimingStats(left, right), {avgConnectMs: 10, connectSamples: 1,
		avgFirstTokenMs: 60, firstTokenSamples: 3, avgTotalMs: 200, totalTimeSamples: 4});
	assert.equal(combineTimingStats({modelOk: 90, avgConnectMs: 1}, {}).avgConnectMs, null);
});

test("historical missing stages and failed requests do not dilute successful samples", () => {
	const result = timingStatsFromRows([
		{status: "ok", connect_ms: 10, first_token_ms: 40, total_time_ms: 100},
		{status: "ok", connect_ms: 0, first_token_ms: 0, total_time_ms: 300},
		{status: "error", connect_ms: 800, first_token_ms: 900, total_time_ms: 1000},
		{status: "ok", model_call_count: 10, connect_ms: 1000, first_token_ms: 5000, total_time_ms: 10000},
	]);
	assert.deepEqual(result, {avgConnectMs: 10, connectSamples: 1, avgFirstTokenMs: 40,
		firstTokenSamples: 1, avgTotalMs: 200, totalTimeSamples: 2});
	assert.equal(timingStatsFromRows([{status: "ok", connect_ms: 0}]).avgConnectMs, null);
});


test("latency stage copy describes client observations rather than provider computation", async () => {
	const {readFile} = await import("node:fs/promises");
	const source = await readFile(new URL("../StatisticsView.vue", import.meta.url), "utf8");
	for (const label of ["等待响应头", "等待可识别输出", "后续接收", "未知阶段不记为零", "不能据此区分服务端排队或纯生成耗时"]) {
		assert.ok(source.includes(label), label);
	}
	assert.ok(!source.includes('label: "建立连接"'));
	assert.ok(!source.includes('label: "生成回答"'));
});


const singleLegacyTiming = {modelCalls: 1, modelOk: 1, expertModelCalls: 0,
	avgConnectMs: 10, avgFirstTokenMs: 20, avgTotalMs: 100};
const singleCurrentTiming = {modelCalls: 1, modelOk: 1, expertModelCalls: 0,
	avgConnectMs: 30, connectSamples: 1, avgFirstTokenMs: 40, firstTokenSamples: 1,
	avgTotalMs: 300, totalTimeSamples: 1};

test("one explicitly successful legacy controller request merges with measured samples", () => {
	const expected = {connectSamples: 2, avgConnectMs: 20, firstTokenSamples: 2,
		avgFirstTokenMs: 30, totalTimeSamples: 2, avgTotalMs: 200};
	assert.deepEqual(combineTimingStats(singleLegacyTiming, singleCurrentTiming), expected);
	assert.deepEqual(combineTimingStats(singleCurrentTiming, singleLegacyTiming), expected);
});

test("two unambiguous old single-request stats retain their timing observations", () => {
	assert.deepEqual(combineTimingStats(singleLegacyTiming, singleLegacyTiming), {
		connectSamples: 2, avgConnectMs: 10, firstTokenSamples: 2,
		avgFirstTokenMs: 20, totalTimeSamples: 2, avgTotalMs: 100,
	});
});

test("legacy missing stages and uncertain populations remain unknown", () => {
	for (const uncertain of [
		{...singleLegacyTiming, modelCalls: 2, modelOk: 2},
		{...singleLegacyTiming, expertModelCalls: 1},
		{...singleLegacyTiming, expertModelCalls: undefined},
		{...singleLegacyTiming, modelCalls: undefined},
		{...singleLegacyTiming, modelOk: 0},
		{...singleLegacyTiming, modelOk: undefined},
	]) {
		assert.equal(combineTimingStats(uncertain, {}).avgTotalMs, null);
		assert.equal(combineTimingStats(uncertain, singleCurrentTiming).avgTotalMs, 300);
	}
	const missingStage = {...singleLegacyTiming, avgConnectMs: 0, avgFirstTokenMs: null};
	assert.deepEqual(combineTimingStats(missingStage, {}), {connectSamples: 0, avgConnectMs: null,
		firstTokenSamples: 0, avgFirstTokenMs: null, totalTimeSamples: 1, avgTotalMs: 100});
	// A new explicit zero sample count is authoritative, even with a stale value.
	assert.equal(combineTimingStats({...singleLegacyTiming, totalTimeSamples: 0}, {}).avgTotalMs, null);
});
