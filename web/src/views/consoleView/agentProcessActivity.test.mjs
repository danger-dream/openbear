import test from "node:test";
import assert from "node:assert/strict";
import {readFile, unlink, writeFile} from "node:fs/promises";
import {compileScript, parse} from "@vue/compiler-sfc";
import {effectScope, reactive} from "vue";

const sharedUrl = new URL("./AgentProcessActivity.vue", import.meta.url);
const activityListUrl = new URL("./AgentActivityList.vue", import.meta.url);
const eventCardUrl = new URL("./AgentEventCard.vue", import.meta.url);
const workspaceUrl = new URL("./AgentPlanWorkspace.vue", import.meta.url);
const generatedUrl = new URL(`./.AgentProcessActivity.test-${process.pid}.mjs`, import.meta.url);

const [sharedSource, activityListSource, eventCardSource, workspaceSource] = await Promise.all([
  readFile(sharedUrl, "utf8"),
  readFile(activityListUrl, "utf8"),
  readFile(eventCardUrl, "utf8"),
  readFile(workspaceUrl, "utf8"),
]);

let AgentProcessActivity;
try {
  const {descriptor, errors} = parse(sharedSource, {filename: "AgentProcessActivity.vue"});
  assert.deepEqual(errors, []);
  const compiled = compileScript(descriptor, {id: "agent-process-activity-test"}).content
    .replace(/^import AgentActivityList from "\.\/AgentActivityList\.vue";$/m, "const AgentActivityList = {};");
  await writeFile(generatedUrl, compiled);
  ({default: AgentProcessActivity} = await import(`${generatedUrl.href}?test=${Date.now()}`));
} finally {
  await unlink(generatedUrl).catch(() => {});
}

test("shared Agent process activity compacts source lifecycles once and limits logical rows", (t) => {
  const props = reactive({
    sourceLines: [
      {key: "t1", seq: 1, kind: "tool_call_started", timeLabel: "10:00:01", toolName: "Bash", rawArguments: "{\"command\":\"pwd\",\"description\":\"确认当前目录\"}", toolDescription: "确认当前目录", detail: {name: "Bash", round: 1}},
      {key: "t2", seq: 2, kind: "tool_call_finished", timeLabel: "10:00:02", detail: {name: "Bash", round: 1, resultPreview: "status: ok"}},
      {key: "x1", seq: 3, kind: "artifact_created", timeLabel: "10:00:03", message: "未知事件仍保留"},
      {key: "m1", seq: 4, kind: "model_call_started", timeLabel: "10:00:04", detail: {modelLabel: "GPT", thinkLevel: "xhigh"}},
      {key: "m2", seq: 5, kind: "model_stream_progress", timeLabel: "10:00:05", detail: {textChars: 24}},
      {key: "m3", seq: 6, kind: "model_call_finished", timeLabel: "10:00:06", detail: {modelLabel: "GPT", thinkLevel: "xhigh", durationMs: 1200}},
      {key: "c1", seq: 7, kind: "context_compaction_compact", timeLabel: "10:00:07", message: "上下文压缩完成", compaction: {summaryId: "summary-1"}, compactedOutput: "压缩摘要"},
      {key: "x2", seq: 8, kind: "artifact_created", timeLabel: "10:00:08", message: "最终未知事件"},
    ],
    modelLabel: "fallback-model",
    thinkLevel: "high",
    fastMode: true,
    limit: 0,
    emptyText: "empty",
  });
  const scope = effectScope();
  const bindings = scope.run(() => AgentProcessActivity.setup(props, {expose: () => {}}));
  t.after(() => scope.stop());

  assert.deepEqual(bindings.displayLines.value.map((line) => line.message), [
    "调用工具 Bash √",
    "未知事件仍保留",
    "模型调用：GPT · 1.2s √",
    "上下文压缩完成",
    "最终未知事件",
  ]);
  assert.equal(bindings.displayLines.value[0].kind, "tool_call_started");
  assert.equal(bindings.displayLines.value[0].rawArguments, "{\"command\":\"pwd\",\"description\":\"确认当前目录\"}");
  assert.equal(bindings.displayLines.value[0].toolDescription, "确认当前目录");
  assert.equal(bindings.displayLines.value[0].processStatus, "success");
  assert.equal(bindings.displayLines.value[2].processType, "model");
  assert.equal(bindings.displayLines.value[2].modelLabel, "GPT");
  assert.equal(bindings.displayLines.value[2].modelThinkLevel, "xhigh");
  assert.equal(bindings.displayLines.value[2].modelFastMode, true);
  assert.equal(bindings.displayLines.value[2].modelDescription, "1.2s");
  assert.equal(bindings.displayLines.value[2].modelStatus, "success");
  assert.equal(bindings.displayLines.value[2].modelStatusText, "执行完成 √");

  props.limit = 3;
  assert.deepEqual(bindings.displayLines.value.map((line) => line.message), [
    "模型调用：GPT · 1.2s √",
    "上下文压缩完成",
    "最终未知事件",
  ]);
});

test("top-level activity and managed step activity share the source-line presentation component", () => {
  assert.equal((sharedSource.match(/compactAgentStepActivityLines\(/g) || []).length, 1);
  assert.match(sharedSource, /<AgentActivityList[^>]*:lines="displayLines"[^>]*compact\/>/);

  assert.match(eventCardSource, /<AgentProcessActivity[\s\S]*?:source-lines="activityDisplayLines"[\s\S]*?:model-label="activityModelLabel"[\s\S]*?:think-level="activityThinkLevel"[\s\S]*?:fast-mode="activityFastMode"[\s\S]*?\/>/);
  assert.doesNotMatch(eventCardSource, /import AgentActivityList/);
  assert.doesNotMatch(eventCardSource, /compactAgentStepActivityLines/);
  assert.match(eventCardSource, /\.filter\(\(item\) => !isActivityExcludedEventKind\(item\.kind\)\)/);
  assert.match(eventCardSource, /agentCompactionActivityView\(item\)/);
  assert.match(eventCardSource, /toolArgumentsSummary\(toolName, rawArguments\)/);
  assert.match(eventCardSource, /toolDescription: summary/);

  assert.match(workspaceSource, /selectedStepActivitySource = computed\(\(\) => agentStepActivityLines\(/);
  assert.match(workspaceSource, /<AgentProcessActivity[\s\S]*?:source-lines="selectedStepActivitySource"[\s\S]*?:think-level="launchInfo\.thinkLevel[^"]*"[\s\S]*?:fast-mode="launchInfo\.fastMode"[\s\S]*?:limit="5"[\s\S]*?\/>/);
  assert.doesNotMatch(workspaceSource, /import AgentActivityList/);
  assert.doesNotMatch(workspaceSource, /compactAgentStepActivityLines/);
});

test("shared tool and model rows use the original timeline dot with status-only tones", () => {
  assert.match(activityListSource, /import \{ArrowRight\} from "@element-plus\/icons-vue"/);
  assert.doesNotMatch(activityListSource, /\bRefresh\b|toolIcon\(|activity-process-icon|activity-model-icon|activity-tool-icon/);
  assert.match(activityListSource, /<time>\{\{ line\.timeLabel \}\}<\/time>\s*<span class="activity-dot"><\/span>/);
  assert.match(activityListSource, /toolArgumentsSummary\(name, rawArguments\)/);

  const modelRowStart = activityListSource.indexOf('<div v-else-if="line.processModel"');
  const modelRowEnd = activityListSource.indexOf('</div>', modelRowStart);
  const modelRow = activityListSource.slice(modelRowStart, modelRowEnd);
  assert.match(modelRow, /activity-model-name">\{\{ line\.processModel\.label \}\}<\/strong>/);
  assert.doesNotMatch(modelRow, />模型调用<|activity-model-label/);
  assert.ok(modelRow.indexOf('line.processModel.thinkLevel') < modelRow.indexOf('line.processModel.fastMode'));
  assert.ok(modelRow.indexOf('line.processModel.fastMode') < modelRow.indexOf('line.processModel.description'));
  assert.ok(modelRow.indexOf('line.processModel.description') < modelRow.indexOf('activity-process-status'));
  assert.match(modelRow, /<span class="activity-model-meta">Fast<\/span>/);
  assert.match(modelRow, /:class="`tone-\$\{line\.processModel\.statusTone\}`"/);

  const toolRowStart = activityListSource.indexOf('<details v-else-if="line.processTool"');
  const toolRowEnd = activityListSource.indexOf('</details>', toolRowStart);
  const toolRow = activityListSource.slice(toolRowStart, toolRowEnd);
  const nameIndex = toolRow.indexOf('activity-process-name activity-tool-name');
  const descriptionIndex = toolRow.indexOf('class="activity-tool-description"');
  const statusIndex = toolRow.indexOf('activity-process-status activity-tool-status');
  const arrowIndex = toolRow.indexOf('class="activity-tool-arrow"');
  assert.ok(nameIndex < descriptionIndex);
  assert.ok(descriptionIndex < statusIndex);
  assert.ok(statusIndex < arrowIndex);
  assert.match(toolRow, /:class="`tone-\$\{line\.processTool\.statusTone\}`"/);

  assert.match(activityListSource, /running: "执行中"/);
  assert.match(activityListSource, /success: "执行完成 √"/);
  assert.match(activityListSource, /failed: "执行失败 ×"/);
  assert.match(activityListSource, /denied: "已拒绝 ×"/);
  assert.match(activityListSource, /running: "调用中"/);
  assert.match(activityListSource, /failed: "调用失败 ×"/);
  assert.match(activityListSource, /\.activity-process-status\.tone-active \{ color: #2563eb; \}/);
  assert.match(activityListSource, /\.activity-process-status\.tone-success \{ color: #357047; \}/);
  assert.match(activityListSource, /\.activity-process-status\.tone-danger \{ color: #b42318; \}/);

  assert.doesNotMatch(activityListSource, /\.activity-row\.tone-(?:active|success|danger) \.activity-tool-call > summary/);
  assert.match(activityListSource, /\.activity-tool-call > summary, \.activity-model-call \{[^}]*color: #52525b/);
  assert.match(activityListSource, /\.activity-tool-call, \.activity-model-call \{[^}]*grid-column: 3/);
  assert.match(activityListSource, /\.activity-tool-description, \.activity-model-description \{[^}]*flex: 0 1 auto;[^}]*text-overflow: ellipsis/);
  assert.match(activityListSource, /\.activity-process-status \{[^}]*flex: 0 0 auto;[^}]*white-space: nowrap/);
  assert.doesNotMatch(activityListSource, /justify-self:\s*end|margin-left:\s*auto|justify-content:\s*space-between|text-align:\s*right/);
  assert.match(activityListSource, /<ToolArgumentsView :tool-name="line\.processTool\.name" :raw-arguments="line\.processTool\.rawArguments" compact\/>/);
});
