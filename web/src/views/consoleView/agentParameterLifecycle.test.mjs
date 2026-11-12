import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {pathToFileURL} from "node:url";
import {compileScript, parse} from "@vue/compiler-sfc";
import {createRenderer, h, nextTick, reactive} from "vue";

// Compile both real model-row consumers. Only unrelated icon/Markdown/tool
// argument children are stubbed; there is no browser or product build output.
const temp = await mkdtemp(join(tmpdir(), "agent-parameter-component-"));
let AgentProcessActivity;
try {
  for (const name of ["AgentActivityList", "AgentProcessActivity"]) {
    const source = await readFile(new URL(`./${name}.vue`, import.meta.url), "utf8");
    const {descriptor, errors} = parse(source, {filename: `${name}.vue`});
    assert.deepEqual(errors, []);
    const compiled = compileScript(descriptor, {id: `parameter-test-${name}`, inlineTemplate: true}).content
      .replace(/from ["']vue["']/g, `from ${JSON.stringify(import.meta.resolve("vue"))}`)
      .replace(/import \{ArrowRight\} from "@element-plus\/icons-vue";/g, "const ArrowRight = {render: () => null};")
      .replace(/import (ConsoleMarkdown|ToolArgumentsView) from "[^"\n]+";/g, "const $1 = {render: () => null};")
      .replace(/from "\.\/AgentActivityList.vue"/g, 'from "./AgentActivityList.mjs"')
      .replace(/from ['"]\.\.\/\.\.\/conversationTime\.js['"]/g, `from ${JSON.stringify(new URL('../../conversationTime.js', import.meta.url).href)}`)
      .replace(/from "\.\/([^"\n]+\.js)"/g, (_, file) => `from ${JSON.stringify(new URL(file, import.meta.url).href)}`);
    await writeFile(join(temp, `${name}.mjs`), compiled);
  }
  ({default: AgentProcessActivity} = await import(pathToFileURL(join(temp, "AgentProcessActivity.mjs"))));
} finally {
  await rm(temp, {recursive: true, force: true});
}

const node = (type, text = "") => ({type, text, children: [], props: {}, parent: null});
const renderer = createRenderer({
  createElement: (type) => node(type), createText: (text) => node("text", text),
  createComment: () => node("comment"),
  setText: (el, text) => {el.text = text;},
  setElementText: (el, text) => {el.text = text; el.children = [];},
  patchProp: (el, key, _old, value) => {el.props[key] = value;},
  insert(el, parent, anchor = null) {
    if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1);
    el.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    if (index < 0) parent.children.push(el); else parent.children.splice(index, 0, el);
  },
  remove(el) {if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1); el.parent = null;},
  parentNode: (el) => el.parent,
  nextSibling: (el) => el.parent?.children[el.parent.children.indexOf(el) + 1] || null,
});
const textOf = (el) => el.text + el.children.map(textOf).join("");
const modelNodes = (el) => [
  ...(el.props.class === "activity-model-call" ? [el] : []),
  ...el.children.flatMap(modelNodes),
];
const event = (seq, kind, detail = {}) => ({key: `${seq}-${kind}`, seq, kind, detail});
const start = (seq = 1, attempt = 0) => event(seq, "model_call_started", {modelLabel: "GPT", attempt, attemptId: `a${attempt}`});
const input = (seq = 2, extra = {}) => event(seq, "model_stream_progress", {toolInput: {
  attemptId: "a0", toolNames: ["Read"], receivedBytes: 12010,
  updatedAtMs: 2000, elapsedMs: 1000, phase: "generating", ...extra,
}});

function mount(t, sourceLines) {
  let now = 2000;
  let id = 0;
  const timers = new Map();
  t.mock.method(Date, "now", () => now);
  t.mock.method(globalThis, "setInterval", (callback) => {timers.set(++id, callback); return id;});
  t.mock.method(globalThis, "clearInterval", (key) => {timers.delete(key);});
  const props = reactive({sourceLines});
  const root = node("root");
  const app = renderer.createApp({render: () => h(AgentProcessActivity, props)});
  app.mount(root);
  t.after(() => app.unmount());
  return {props, root, app, timers, text: () => textOf(root), models: () => modelNodes(root),
    async tick(value) {now = value; for (const callback of [...timers.values()]) callback(); await nextTick();}};
}

test("actual Agent model row advances elapsed time without events and stops its clock at terminal/unmount", async (t) => {
  const view = mount(t, [start(), input()]);
  assert.match(view.text(), /12.01 kB · 1秒/);
  assert.equal(view.timers.size, 1);
  await view.tick(32000);
  assert.match(view.text(), /12.01 kB · 31秒/);
  assert.equal(view.models().length, 1);
  view.props.sourceLines.push(input(3, {phase: "ready", updatedAtMs: 32000, elapsedMs: 31000}));
  await nextTick();
  await view.tick(62000);
  assert.match(view.text(), /参数已接收，等待模型结束.*1分01秒/);
  view.props.sourceLines.push(event(4, "model_call_finished", {durationMs: 61000}));
  await nextTick();
  assert.equal(view.timers.size, 0);
  assert.match(view.text(), /执行完成 √/);
  assert.doesNotMatch(view.text(), /已接收|等待模型结束/);
  const terminal = view.text();
  await view.tick(92000);
  assert.equal(view.text(), terminal);
  view.props.sourceLines.push(start(5), input(6, {updatedAtMs: 92000, elapsedMs: 0}));
  await nextTick();
  assert.equal(view.timers.size, 1);
  view.app.unmount();
  assert.equal(view.timers.size, 0);
});

for (const terminal of ["task_failed", "task_cancelled", "task_interrupted"]) {
  test(`actual Agent row clears parameter UI for ${terminal}, including legacy event sequences`, async (t) => {
    const view = mount(t, [start(), input()]);
    view.props.sourceLines.push(event(3, terminal));
    await nextTick();
    assert.equal(view.models().length, 1);
    assert.equal(view.timers.size, 0);
    assert.doesNotMatch(view.text(), /生成工具参数|已接收|等待模型结束|执行完成/);
    assert.match(view.text(), /失败|取消|中断/);
    await view.tick(92000);
    assert.equal(view.timers.size, 0);
  });
}

test("actual Agent row preserves recovery failure but never marks the ended model and tool both active", async (t) => {
  const view = mount(t, [start(), input(2, {phase: "ready"})]);
  view.props.sourceLines.push(
    event(3, "model_stream_interrupted", {status: "error", reason: "network"}),
    event(4, "model_stream_recovered_tool_calls", {toolCallCount: 1}),
    event(5, "tool_call_started", {name: "Read"}),
  );
  await nextTick();
  assert.match(view.text(), /模型流已中断，完整工具调用已恢复/);
  assert.doesNotMatch(view.text(), /等待模型结束|已接收/);
  assert.equal(view.timers.size, 0);
  view.props.sourceLines.push(event(6, "tool_call_finished", {name: "Read"}), start(7), event(8, "model_call_finished"), event(9, "task_completed"));
  await nextTick();
  assert.equal(view.models().length, 2);
  assert.match(textOf(view.models()[0]), /中断/);
  assert.match(textOf(view.models()[1]), /执行完成 √/);
  assert.equal(view.timers.size, 0);
});

test("actual Agent retry lifecycle replaces parameter state and ignores completed retry metadata", async (t) => {
  const view = mount(t, [start(), input()]);
  view.props.sourceLines.push(event(3, "model_stream_interrupted", {status: "error"}),
    event(4, "model_call_retry_wait", {retry: {active: true, attempt: 1, maxRetries: 2}}),
    event(5, "model_call_retry_resumed", {retry: {terminal: true, status: "resumed", attempt: 1, maxRetries: 2}}),
    start(6, 1));
  await nextTick();
  assert.equal(view.timers.size, 0);
  assert.doesNotMatch(view.text(), /12.01 kB/);
  view.props.sourceLines.push(input(7, {attemptId: "a1", toolNames: ["Write"], receivedBytes: 9}));
  await nextTick();
  assert.match(view.text(), /Write · 已接收 9 B/);
  assert.equal(view.timers.size, 1);
  view.props.sourceLines.push(event(8, "model_call_finished"),
    event(9, "model_call_retry_resumed", {retry: {terminal: true, status: "completed"}}), event(10, "task_completed"));
  await nextTick();
  assert.equal(view.models().length, 1);
  assert.match(view.text(), /执行完成 √/);
  assert.doesNotMatch(view.text(), /正在重试|已接收/);
  assert.equal(view.timers.size, 0);
});

for (const field of ["textChars", "reasoningChars"]) {
  test(`actual Agent parameter clock clears on ${field} progress`, async (t) => {
    const view = mount(t, [start(), input()]);
    view.props.sourceLines.push(event(3, "model_stream_progress", {[field]: 1, attemptId: "a0"}));
    await nextTick();
    assert.match(view.text(), /流式输出中/);
    assert.doesNotMatch(view.text(), /已接收|生成工具参数/);
    assert.equal(view.timers.size, 0);
  });
}
