import test from "node:test";
import assert from "node:assert/strict";
import {readFile, unlink, writeFile} from "node:fs/promises";
import {compileScript, parse} from "@vue/compiler-sfc";
import {createRenderer, reactive} from "vue";

const sharedUrl = new URL("./AgentProcessActivity.vue", import.meta.url);
const generatedUrl = new URL(`./.AgentProcessActivity.test-${process.pid}.mjs`, import.meta.url);

const sharedSource = await readFile(sharedUrl, "utf8");

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
  let bindings;
  const app = createRenderer({
    createComment: () => ({}), insert() {}, remove() {},
    parentNode: () => null, nextSibling: () => null,
  }).createApp({setup() {
    bindings = AgentProcessActivity.setup(props, {expose: () => {}});
    return () => null;
  }});
  app.mount({});
  t.after(() => app.unmount());

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
