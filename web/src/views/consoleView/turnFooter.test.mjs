import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {writeFile, unlink} from "node:fs/promises";
import vm from "node:vm";
import {compile, createSSRApp, h, ref} from "vue";
import {renderToString} from "vue/server-renderer";
import {parse} from "@vue/compiler-sfc";
import {projectOperationMessages, eventDisplayTimeMs, eventStartedAtMs, eventUpdatedAtMs} from "../../timelineProjection.js";
import {conversationTimelineEntries, shouldRenderAssistantDivider} from "./conversationTimeline.js";

// Use the actual display/usage functions, omitting only browser-only icon/markdown imports.
const displayUrl = new URL(`./.turn-footer-display-${process.pid}.mjs`, import.meta.url);
let display;
try {
  const source = readFileSync(new URL("./display.js", import.meta.url), "utf8")
    .replace('import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";', "const ContextCompactionIcon = {};")
    .replace('import {plainText} from "./markdown.js";', 'const plainText = value => String(value || "");');
  await writeFile(displayUrl, source);
  display = await import(displayUrl.href);
} finally {
  await unlink(displayUrl).catch(() => {});
}
const {descriptor} = parse(readFileSync(new URL("./TurnList.vue", import.meta.url), "utf8"));
const script = descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "");
const render = compile(descriptor.template.content);
const setupNames = [...script.matchAll(/^(?:async )?function (\w+)\(/gm)].map(match => match[1]);
const slot = {inheritAttrs: false, setup: (_, {slots}) => () => slots.default?.()};
function turnList(turns, running = false) {
  const copies = [];
  const props = {turns, running, conversationUuid: "test-conversation", detailKey: () => "detail", isDetailOpen: () => false, activeToolResultIndex: () => 0};
  const context = vm.createContext({ref, ...display, conversationTimelineEntries, shouldRenderAssistantDivider,
    projectedEventDisplayTimeMs: eventDisplayTimeMs, projectedEventStartedAtMs: eventStartedAtMs, projectedEventUpdatedAtMs: eventUpdatedAtMs,
    defineProps: () => props, defineEmits: () => () => {}, referenceDisplayText: value => value,
    copyTextToClipboard: async text => copies.push(text), ElMessage: {success() {}, error() {}},
    window: {clearTimeout() {}, setTimeout() { return 1; }},
  });
  vm.runInContext(script, context);
  const app = createSSRApp({render, setup: () => vm.runInContext(`({props, emit, copiedMessageKey, ${setupNames.join(", ")}})`, context)});
  for (const name of ["el-tooltip", "el-icon", "el-image", "Check", "CopyDocument", "RefreshLeft", "Link"]) app.component(name, slot);
  app.component("ConsoleMarkdown", {props: ["text"], setup: props => () => h("p", props.text)});
  app.component("TurnEvent", {props: ["event"], setup: props => () => h("div", {"data-event-id": props.event.id}, props.event.message?.content || "上下文压缩")});
  return {props, copies, html: () => renderToString(app), run: code => vm.runInContext(code, context)};
}
const answer = (id = "answer", content = "已完成") => ({kind: "answer", id, message: {content, createdAt: 1789044000, live: false}});
const savedStats = () => ({live: false, durationMs: 128664,
  usage: {inputTokens: 496777, outputTokens: 2593, cacheReadTokens: 824448, cacheWriteTokens: 0}});
const compact = strategy => ({kind: "tool", id: `compress-${strategy}`, toolName: "ContextCompaction", operation: {
  opId: `context-${strategy}`, opType: "context_compaction", status: "completed", lifecycle: "terminal",
  createdAtMs: 1789044001000, updatedAtMs: 1789044002000,
  payload: {scope: "root", strategy, summary: "摘要不属于复制正文", durationMs: 1000},
}});
function footer(html) { return html.match(/<div class="assistant-message-meta">[\s\S]*?<\/div>/)?.[0] || ""; }

// Stats and ordering mirror the values established in the user's referenced investigation.
// Projection inputs below are deterministic fixtures, not a second production DB read.
for (const strategy of ["historical", "sliding_window", "model_summary"]) {
  test(`completed reply followed by ${strategy} compression retains one turn footer after the card`, async () => {
    const operation = strategy === "historical" ? {
      opId: "tool:context-compaction:77", opType: "tool", source: "context_compaction",
      payload: {scope: "root", toolName: "ContextCompaction", compactionId: "context-compaction:77", summaryId: 77},
    } : compact(strategy).operation;
    const projected = projectOperationMessages([
      {opId: "user", opType: "user_message", turnId: "turn-a", displaySeq: 1, createdAtMs: 1789043900000, payload: {text: "请处理"}},
      {opId: "reply", opType: "assistant_message", turnId: "turn-a", displaySeq: 2, status: "completed", lifecycle: "terminal", createdAtMs: 1789044000000, payload: {text: "已完成", complete: true}},
      {...operation, turnId: "turn-a", displaySeq: 3, status: "completed", lifecycle: "terminal", createdAtMs: 1789044001000, updatedAtMs: 1789044002000},
      {opId: "stats", opType: "stats", turnId: "turn-a", displaySeq: 4, payload: savedStats()},
    ]);
    const assistant = projected.find(message => message.role === "assistant");
    const turn = {id: "turn-a", user: projected.find(message => message.role === "user"), events: assistant.localTimeline, stats: assistant.localStats};
    assert.deepEqual(turn.events.map(event => event.kind), ["answer", "tool"]);
    const component = turnList([turn]);
    const html = await component.html();
    assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
    assert.ok(html.indexOf('class="assistant-message-meta"') > html.lastIndexOf('data-event-id='));
    assert.match(footer(html), /2m 9s/);
    assert.match(footer(html), /↑1\.32M · ↓2\.6K · 缓存 824\.4K（62\.4%）/);
    assert.match(footer(html), /aria-label="复制消息"/);
    await component.run("copyMessage(assistantTurnRawContent(props.turns[0]), 'turn-copy')");
    assert.deepEqual(component.copies, ["已完成"]);
    assert.equal(turn.stats.durationMs, 128664);
  });
}

test("multiple replies and trailing compression produce one footer and copy only answer content", async () => {
  const turn = {id: "many", events: [answer("a1", "进度"), compact("model_summary"), answer("a2", "最终结果"), compact("sliding_window")], stats: savedStats()};
  const component = turnList([turn]);
  const html = await component.html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
  assert.ok(html.indexOf('class="assistant-message-meta"') > html.indexOf('data-event-id="compress-sliding_window"'));
  assert.equal(component.run("assistantTurnRawContent(props.turns[0])"), "进度\n\n最终结果");
});

test("running, streaming, reasoning and live statistics do not expose a completed footer", async () => {
  const base = {id: "active", events: [answer(), compact("model_summary")], stats: savedStats()};
  for (const [turn, running] of [
    [base, true],
    [{...base, stats: {...base.stats, live: true}}, false],
    [{...base, events: [{...answer(), message: {...answer().message, live: true}}, compact("sliding_window")]}, false],
    [{...base, events: [{...answer(), reasoningActive: true}, compact("sliding_window")]}, false],
  ]) {
    assert.equal(footer(await turnList([turn], running).html()), "");
  }
  const prior = {...base, id: "prior"};
  const next = {id: "next", events: [answer("next-answer")], stats: {...savedStats(), live: true}};
  const html = await turnList([prior, next], true).html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1, "a later running turn must not hide prior completed stats");
});

test("stats remain visible without answer text while copy stays absent and empty turns stay empty", async () => {
  for (const events of [[compact("model_summary")], []]) {
    const html = await turnList([{id: "stats-only", events, stats: savedStats()}]).html();
    assert.match(footer(html), /2m 9s/);
    assert.match(footer(html), /↑1\.32M/);
    assert.doesNotMatch(footer(html), /aria-label="复制消息"/);
  }
  assert.equal(footer(await turnList([{id: "empty", events: []}]).html()), "");
});

test("ordinary and legacy answers retain the same footer and intermediate hover timestamps", async () => {
  const html = await turnList([{id: "legacy", events: [answer("a1", "进度"), answer("a2", "结果")]}]).html();
  assert.equal((html.match(/class="assistant-message-meta"/g) || []).length, 1);
  assert.equal((html.match(/class="time-float time-float-left"/g) || []).length, 1);
  assert.match(footer(html), /assistant-message-time/);
  assert.match(footer(html), /aria-label="复制消息"/);
  assert.doesNotMatch(footer(html), /turn-token-usage/);
});
