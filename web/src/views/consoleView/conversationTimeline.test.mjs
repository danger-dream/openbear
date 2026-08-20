import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

import {conversationTimelineEntries, isConversationTimelineEvent, shouldRenderAssistantDivider} from "./conversationTimeline.js";

const turnListSource = readFileSync(new URL("./TurnList.vue", import.meta.url), "utf8");
const workDetailPanelSource = readFileSync(new URL("./TurnWorkDetailPanel.vue", import.meta.url), "utf8");
const agentToolNames = new Set(["Agent", "AgentMessage", "AgentStop"]);
const isAgentFixture = (event) => agentToolNames.has(event?.toolName);
const canonicalCompaction = {
	opId: "manual-compact:test",
	opType: "context_compaction",
	payload: {scope: "root"},
};
const historicalCompaction = {
	opId: "tool:context-compaction:77",
	opType: "tool",
	source: "context_compaction",
	payload: {toolName: "ContextCompaction", compactionId: "context-compaction:77", summaryId: 77, scope: "root"},
};

test("main conversation keeps durable interruptions, model retries, and context compactions", () => {
	const events = [
		{kind: "answer", message: {content: "回答"}},
		{kind: "answer", message: {content: "   "}},
		{kind: "live_status", status: "处理中"},
		{kind: "live_status", persistentRunIndicator: true},
		{kind: "live_status", interruption: true, queued: true, preview: "先停一下"},
		{kind: "model_retry", retry: {active: false}},
		{kind: "tool", toolName: "Read"},
		{kind: "tool", toolName: "ContextCompaction", operation: canonicalCompaction},
	];
	const entries = conversationTimelineEntries(events, (event) => event.toolName || "");

	assert.deepEqual(entries.map(({index}) => index), [0, 3, 4, 5, 7]);
	assert.equal(entries[2].event.preview, "先停一下");
});

test("Agent events keep their source positions while ordinary tools remain filtered", () => {
	const events = [
		{kind: "answer", id: "answer-before", message: {content: "开始"}},
		{kind: "tool", id: "ordinary-read", toolName: "Read"},
		{kind: "live_agent", id: "agent-live"},
		{kind: "tool", id: "compaction", toolName: "ContextCompaction", operation: historicalCompaction},
		{kind: "tool", id: "agent-start", toolName: "Agent"},
		{kind: "model_retry", id: "retry"},
		{kind: "tool", id: "agent-message", toolName: "AgentMessage"},
		{kind: "tool", id: "ordinary-bash", toolName: "Bash"},
		{kind: "tool", id: "agent-stop", toolName: "AgentStop"},
		{kind: "answer", id: "answer-after", message: {content: "完成"}},
	];
	const entries = conversationTimelineEntries(events, (event) => event.toolName || "", isAgentFixture);

	assert.deepEqual(entries.map(({index}) => index), [0, 2, 3, 4, 5, 6, 8, 9]);
	assert.deepEqual(entries.map(({event}) => event.id), [
		"answer-before", "agent-live", "compaction", "agent-start", "retry", "agent-message", "agent-stop", "answer-after",
	]);
	assert.match(turnListSource, /conversationTimelineEntries\(displayEvents\(turn\), eventPrimaryToolName, isAgentEvent\)/);
});

test("failed Agent stays out of the main conversation but remains available to work detail", () => {
	const events = [
		{kind: "tool", id: "running", toolName: "Agent", operation: {status: "running", lifecycle: "active", payload: {status: "running"}}},
		{kind: "tool", id: "failed-task", toolName: "Agent", operation: {status: "failed", lifecycle: "terminal", payload: {task: {status: "failed"}}}},
		{kind: "tool", id: "failed-launch", toolName: "Agent", operation: {status: "failed", lifecycle: "terminal", payload: {status: "failed", taskUuid: ""}}},
		{kind: "tool", id: "completed", toolName: "Agent", operation: {status: "completed", lifecycle: "terminal", payload: {task: {status: "completed"}}}},
		{kind: "tool", id: "partial", toolName: "Agent", operation: {status: "partial", lifecycle: "terminal", payload: {status: "partial"}}},
		{kind: "tool", id: "cancelled", toolName: "Agent", operation: {status: "cancelled", lifecycle: "terminal", payload: {status: "cancelled"}}},
	];
	const entries = conversationTimelineEntries(events, (event) => event.toolName || "", isAgentFixture);

	assert.deepEqual(entries.map(({event}) => event.id), ["running", "completed", "partial", "cancelled"]);
	assert.equal(events.length, 6, "main timeline filtering must not delete source work events");
	assert.match(workDetailPanelSource, /sourceEvents = computed\(\(\) => Array\.isArray\(props\.turn\?\.events\) \? props\.turn\.events : \[\]\)/);
});

test("typed and legacy UserInteraction remain in the main timeline while ordinary tools stay filtered", () => {
	const events = [
		{kind: "user_interaction", id: "typed", operation: {opType: "user_interaction", opId: "tool:ui-1", payload: {interaction: {title: "保留卡片"}}}},
		{kind: "tool", id: "legacy", toolName: "UserInteraction", operation: {opType: "tool", opId: "tool:ui-old", payload: {name: "UserInteraction"}}},
		{kind: "tool", id: "ordinary", toolName: "Read", operation: {opType: "tool", opId: "tool:read"}},
	];
	const entries = conversationTimelineEntries(events, (event) => event.toolName || "");
	assert.deepEqual(entries.map(({event}) => event.id), ["typed", "legacy"]);
	assert.equal(isConversationTimelineEvent(events[0]), true);
	assert.equal(isConversationTimelineEvent(events[1]), true);
});

test("ordinary transient status and ordinary tools stay out of the main conversation", () => {
	assert.equal(isConversationTimelineEvent({kind: "live_status", status: "处理中"}), false);
	assert.equal(isConversationTimelineEvent({kind: "tool", toolName: "Bash", operation: {
		opId: "tool:bash-1", opType: "tool", internal: true, payload: {toolName: "Bash"},
	}}, "Bash"), false);
	assert.equal(isConversationTimelineEvent({kind: "tool", toolName: "ContextCompaction", operation: {
		opId: "tool:context-compaction:not-valid", opType: "tool", internal: true, payload: {toolName: "ContextCompaction"},
	}}, "ContextCompaction"), false);
	assert.equal(isConversationTimelineEvent({kind: "live_status", interruption: true}), true);
});

test("assistant progress adds a divider between adjacent visible answer entries", () => {
	const entries = conversationTimelineEntries([
		{kind: "answer", id: "update-1", message: {content: "进度一", live: true}},
		{kind: "tool", toolName: "Read"},
		{kind: "answer", id: "update-2", message: {content: "进度二", live: true}},
		{kind: "live_status", persistentRunIndicator: true},
		{kind: "answer", id: "update-3", message: {content: "实时输出", live: true}},
	]);

	assert.deepEqual(entries.map(({index}) => index), [0, 2, 3, 4]);
	assert.equal(entries[1].event.message.content, "进度二");
	assert.equal(shouldRenderAssistantDivider(entries, 0), false);
	assert.equal(shouldRenderAssistantDivider(entries, 1), true);
	assert.equal(shouldRenderAssistantDivider(entries, 2), false);
	assert.equal(shouldRenderAssistantDivider(entries, 3), false);
});

test("only intermediate conversation rows retain hover time and duration badges", () => {
	assert.doesNotMatch(turnListSource, /class="time-float time-float-right"/);
	assert.match(turnListSource, /v-if="eventTimeMs\(entry\.event\) && !assistantMetaVisible\(entry\.event, turnIndex, conversationIndex, turn\)" class="time-float time-float-left"/);
	assert.match(turnListSource, /class="time-float time-float-left"[^>]*>\{\{ timeBadge\(eventTimeMs\(entry\.event\), durationMsForEvent\(entry\.event\)\) \}\}/);
	assert.match(turnListSource, /\.timed-row:hover > \.time-float\s*\{/);
});
