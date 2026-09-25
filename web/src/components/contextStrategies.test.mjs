import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {compile, computed, createSSRApp, h, nextTick, onBeforeUnmount, reactive, ref, watch} from "vue";
import draggable from "vuedraggable";
import {renderToString} from "vue/server-renderer";
import {parse, compileTemplate} from "@vue/compiler-sfc";
import {normalizedRunDefaults, sparseRunDefaults, updateRunDefault, runDefaultOption} from "./folderRunDefaults.js";
import {contextCompactionView, compactAgentStepActivityLines} from "../views/consoleView/agentPlanPresentation.js";
import {scrollModelListAbove} from "./modelDragAutoScroll.js";

const read = file => fs.readFileSync(new URL(file, import.meta.url), "utf8");

function picker(t) {
  const source = parse(read("./ModelOrderPicker.vue")).descriptor.scriptSetup.content.replace(/^import .*?;\n/gm, "");
  const props = reactive({modelValue: [], disabled: false, label: "摘要模型", emptyLabel: "使用当前执行模型", footerText: "", models: [
    {key: "one/a", label: "A model", provider: "one"}, {key: "two/b", label: "B model", provider: "two"},
    {key: "one/c", label: "C model", provider: "one"},
  ]});
  const emitted = [];
  const context = vm.createContext({computed, ref, nextTick, scrollModelListAbove, onBeforeUnmount: fn => t?.after(fn), watch: (...args) => {
      const stop = watch(...args);
      t?.after(stop);
      return stop;
    },
    defineProps: () => props,
    defineEmits: () => (name, value) => {emitted.push({name, value}); props.modelValue = value;},
  });
  vm.runInContext(source, context);
  return {props, emitted, run: code => vm.runInContext(code, context)};
}

const plain = value => JSON.parse(JSON.stringify(value));

test("real picker functions multi-select, de-duplicate, drag-sort and keyboard reorder the same candidate list", t => {
  const h = picker(t);
  h.run("toggle('one/a'); toggle('two/b'); toggle('one/c')");
  assert.deepEqual(plain(h.props.modelValue), ["one/a", "two/b", "one/c"]);
  h.run("move(2, -1)");
  assert.deepEqual(plain(h.props.modelValue), ["one/a", "one/c", "two/b"]);
  h.run("selected.value = [selected.value[2], selected.value[0], selected.value[1]]");
  assert.deepEqual(plain(h.props.modelValue), ["two/b", "one/a", "one/c"]);
  h.run("toggle('one/a'); toggle('one/a')");
  assert.deepEqual(plain(h.props.modelValue), ["two/b", "one/c", "one/a"]);
  h.run("move(0, -1); move(2, 1)");
  assert.deepEqual(plain(h.props.modelValue), ["two/b", "one/c", "one/a"]);
  h.props.disabled = true;
  h.run("move(0, 1); toggle('one/c')");
  assert.deepEqual(plain(h.props.modelValue), ["two/b", "one/c", "one/a"]);
});

test("picker search preserves selected order and unavailable legacy candidates remain removable", t => {
  const h = picker(t);
  h.props.modelValue = ["removed/old", "one/c"];
  h.run("query.value = 'two'");
  assert.deepEqual(plain(h.run("groups.value.map(group => group.models.map(model => model.key))")), [["two/b"]]);
  assert.deepEqual(plain(h.run("selected.value.map(item => item.key)")), ["removed/old", "one/c"]);
  h.run("toggle('removed/old')");
  assert.deepEqual(plain(h.props.modelValue), ["one/c"]);
});

test("picker opens independently, focuses search and closes cleanly for keyboard, outside and saving", async t => {
  const h = picker(t);
  h.run("var focusedInput = 0, focusedTrigger = 0, positioned = false, preventedScroll = false; floating.value = {async position() { positioned = true }}; input.value = {focus(options) { if (!positioned) throw new Error('focused before positioning'); preventedScroll = options.preventScroll; focusedInput++ }}; trigger.value = {focus() { focusedTrigger++ }}");
  await h.run("toggleOpen()");
  assert.equal(h.run("open.value"), true);
  assert.equal(h.run("focusedInput"), 1);
  assert.equal(h.run("preventedScroll"), true);
  h.run("query.value = 'two'; closePicker(true)");
  assert.equal(h.run("open.value"), false);
  assert.equal(h.run("query.value"), "");
  assert.equal(h.run("focusedTrigger"), 1);
  await h.run("toggleOpen()");
  h.run("closePicker()");
  assert.equal(h.run("focusedTrigger"), 1, "outside close must not steal focus");
  await h.run("toggleOpen()");
  h.props.disabled = true;
  await nextTick();
  assert.equal(h.run("open.value"), false);
  await h.run("toggleOpen()");
  h.run("selected.value = [{key: 'one/a'}]");
  assert.equal(h.run("open.value"), false);
  assert.deepEqual(plain(h.props.modelValue), []);
});

test("picker trigger shows first candidate without growing selected rows in the setting", t => {
  const h = picker(t);
  assert.equal(h.run("triggerLabel.value"), "使用当前执行模型");
  h.props.modelValue = ["two/b", "one/a"];
  assert.equal(h.run("triggerLabel.value"), "B model");
  h.run("move(1, -1)");
  assert.equal(h.run("triggerLabel.value"), "A model");
  h.props.modelValue = ["gone/legacy"];
  assert.equal(h.run("triggerLabel.value"), "gone/legacy");
});

test("picker template keeps choices and reorder list in the real body-teleported floating panel", async t => {
  const {descriptor} = parse(read("./ModelOrderPicker.vue"));
  const {descriptor: floating} = parse(read("../references/FloatingPanel.vue"));
  const floatingScript = floating.scriptSetup.content.replace(/^import .*?;\n/gm, "");
  const FloatingPanel = {
    props: ["open", "anchor", "placement", "width", "label"], emits: ["close", "enter", "leave"],
    render: compile(floating.template.content),
    setup(props, {emit}) {
      const context = vm.createContext({ref, watch, nextTick, onBeforeUnmount,
        defineProps: () => props, defineEmits: () => emit, defineExpose: () => {}});
      vm.runInContext(floatingScript, context);
      return vm.runInContext("({props, panel, style, emit})", context);
    },
  };
  for (const isOpen of [false, true]) {
    const harness = picker(t);
    harness.props.modelValue = ["one/a", "two/b"];
    harness.run(`open.value = ${isOpen}`);
    const app = createSSRApp({render: compile(descriptor.template.content),
      setup: () => harness.run("({props, trigger, floating, orderScrollContainer, open, query, input, byKey, selected, triggerLabel, groups, toggle, move, closePicker, toggleOpen, startOrderScroll, finishOrderScroll})"),
      components: {FloatingPanel, draggable}});
    const context = {};
    const html = await renderToString(app, context);
    assert.match(html, /model-picker-trigger-label[^>]*>A model/);
    assert.match(html, /model-picker-count[^>]*>\+1/);
    assert.doesNotMatch(html, /model-picker-dropdown|model-order-item|model-picker-search/);
    const panel = context.teleports?.body || "";
    if (isOpen) {
      assert.match(panel, /model-picker-dropdown/);
      assert.match(panel, /model-order-item/);
      assert.match(panel, /搜索摘要模型/);
      assert.match(panel, /最后回退当前执行模型/);
    } else {
      assert.doesNotMatch(panel, /model-picker-dropdown|model-order-item/);
    }
  }
  assert.match(floating.scriptSetup.content, /strategy:'fixed'/);
  assert.match(floating.scriptSetup.content, /flip\(\).*shift\(/);
  const style = descriptor.styles[0].content;
  assert.match(style, /\.model-picker-trigger\s*\{[^}]*height: 32px/);
  assert.match(style, /\.picker-chevron\s*\{[^}]*display: block;[^}]*flex: 0 0 14px/);
  assert.match(style, /\.model-picker-content\s*\{[^}]*min-height: 0;[^}]*overflow-y: auto/);
  assert.doesNotMatch(descriptor.template.content, /⌄|model-picker-fallback/);
});

test("model picker drag scrolls its actual clipped panel content even if the pointer is over a row or header", async t => {
  const harness = picker(t);
  harness.props.modelValue = ["one/a", "two/b", "one/c"];
  harness.run("open.value = true");
  const {descriptor} = parse(read("./ModelOrderPicker.vue"));
  const options = [];
  const Probe = {
    setup(_props, {attrs}) { options.push(attrs); return () => h("div"); },
  };
  const Panel = {props: ["open", "anchor", "placement", "width", "label"], emits: ["close"],
    render() { return this.open ? this.$slots.default() : null; }};
  const render = async () => {
    const app = createSSRApp({render: compile(descriptor.template.content),
      setup: () => harness.run("({props, trigger, floating, orderScrollContainer, open, query, input, byKey, selected, triggerLabel, groups, toggle, move, closePicker, toggleOpen, startOrderScroll, finishOrderScroll})"),
      components: {FloatingPanel: Panel, draggable: Probe}});
    await renderToString(app);
    return options.at(-1);
  };
  const initial = await render();
  assert.equal(initial.scroll, true, "before the panel DOM ref mounts, Sortable can discover an ancestor");
  assert.equal(typeof initial.onStart, "function");
  assert.equal(typeof initial.onEnd, "function");
  harness.run("var scrollTarget = {scrollTop: 650, scrollHeight: 1500, clientHeight: 280}; orderScrollContainer.value = scrollTarget");
  assert.deepEqual(plain((await render()).scroll), plain(harness.run("scrollTarget")), "Sortable must target the panel's scrollable content, not the dragged list or page");
  assert.equal(initial["force-auto-scroll-fallback"], true, "run Sortable auto-scroll even in native desktop DnD");
  assert.equal(initial["bubble-scroll"], false, "do not scroll the page behind the floating panel");
  assert.ok(initial["scroll-sensitivity"] >= 60);
  assert.ok(initial["scroll-speed"] > 10);
  const style = descriptor.styles[0].content;
  assert.match(style, /\.model-picker-content\s*\{[^}]*overflow-y: auto/);
  assert.match(descriptor.template.content, /ref="orderScrollContainer" class="model-picker-content"/);
});

test("strategy inheritance is sparse and an explicit child override wins", () => {
  const base = {contextStrategy: "model_summary"};
  assert.equal(normalizedRunDefaults({fallback: base}).contextStrategy, "model_summary");
  let local = updateRunDefault({}, "contextStrategy", runDefaultOption("sliding_window"));
  assert.deepEqual(sparseRunDefaults(local), {contextStrategy: "sliding_window"});
  assert.equal(normalizedRunDefaults({local, inherited: base}).contextStrategy, "sliding_window");
  local = updateRunDefault(local, "contextStrategy", "inherit");
  assert.deepEqual(local, {});
  assert.equal(normalizedRunDefaults({local, inherited: base}).contextStrategy, "model_summary");
});

test("both custom macOS controls compile and expose keyboard, busy and readable text contracts", () => {
  for (const file of ["./ContextStrategySwitch.vue", "./ModelOrderPicker.vue"]) {
    const source = read(file);
    const {descriptor} = parse(source);
    const result = compileTemplate({source: descriptor.template.content, filename: file, id: file});
    assert.deepEqual(result.errors, []);
    assert.match(source, /:disabled=/);
    assert.match(source, /aria-/);
    assert.doesNotMatch(source, /<el-(select|option|dropdown)/);
    const sizes = [...source.matchAll(/font-size:\s*(\d+)px/g)].map(match => Number(match[1]));
    assert.ok(sizes.length && sizes.every(size => size >= 13));
  }
});

test("window and summary cards separate estimated next input from actual summary-model usage", () => {
  const window = contextCompactionView({name: "ContextCompaction", strategy: "sliding_window", scope: "root", compactionId: "c1",
    beforeEstimateTokens: 200000, afterEstimateTokens: 32000, removedBatches: 10, retainedBatches: 3, durationMs: 12});
  assert.equal(window.cardTitle, "上下文压缩");
  assert.equal(window.summaryChars, 0);
  assert.equal(window.summaryRef, "");
  assert.equal(window.cardPreview, "200k → 32k · 0.0s");
  assert.doesNotMatch(window.output, /移出|保留 .*组/);
  assert.match(window.output, /估算/);
  assert.match(window.output, /待下一次请求实测/);
  assert.doesNotMatch(window.output, /摘要正文|旧记录/);
  const summary = contextCompactionView({name: "ContextCompaction", strategy: "model_summary", scope: "root", compactionId: "c2",
    summary: "Actual saved summary", compressionModel: "provider/model", afterEstimateTokens: 21000,
    usage: {inputTokens: 100, cacheReadTokens: 50, outputTokens: 30}});
  assert.equal(summary.output, "Actual saved summary");
  assert.ok(summary.detailFacts.some(fact => fact.includes("实测：输入 150")));
  assert.ok(summary.detailFacts.some(fact => fact.includes("待下一次请求实测")));
  assert.equal(contextCompactionView({...summary, name: "ContextCompaction"}).output, summary.output);
  assert.equal(contextCompactionView({name: "OrdinaryTool", strategy: "model_summary"}).isCompaction, false);
});

test("Agent compression start and terminal update form one activity instead of two tools", () => {
  const lines = compactAgentStepActivityLines([
    {seq: 1, kind: "model_context_compaction_started", detail: {scope: "agent", strategy: "model_summary", compactionId: "a1", status: "running"}},
    {seq: 2, kind: "model_context_compaction_completed", detail: {scope: "agent", strategy: "model_summary", compactionId: "a1", status: "completed", summary: "Saved task state"}},
  ]);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].compaction.output, "Saved task state");
  assert.equal(lines[0].compaction.status, "completed");
});


test("Agent activity's actual Vue template renders strategy facts and summary only when applicable", async () => {
  const {descriptor} = parse(read("../views/consoleView/AgentActivityList.vue"));
  const render = compile(descriptor.template.content);
  for (const strategy of ["sliding_window", "model_summary"]) {
    const lines = compactAgentStepActivityLines([{seq: 1, kind: "model_context_compaction_completed", detail: {
      scope: "agent", strategy, compactionId: "c1", status: "completed", summary: "Saved summary body",
      compressionModel: "provider/model", removedBatches: 10, retainedBatches: 3,
      beforeEstimateTokens: 200000, afterEstimateTokens: 30000, durationMs: 150,
      usage: {inputTokens: 100, outputTokens: 50},
    }}]);
    const app = createSSRApp({render, setup: () => ({props: {lines}, presentedLines: lines, compact: false})});
    app.component("ArrowRight", {render: () => h("span")});
    app.component("ToolArgumentsView", {render: () => h("span")});
    app.component("ConsoleMarkdown", {props: ["text"], render() { return h("article", this.text); }});
    const html = await renderToString(app);
    assert.match(html, /估算/);
    assert.match(html, /待下一次请求实测/);
    assert.match(html, /耗时/);
    if (strategy === "sliding_window") {
      assert.match(html, /200,000 → 30,000/);
      assert.doesNotMatch(html, /移出|保留 .*组/);
      assert.doesNotMatch(html, /Saved summary body|摘要正文|未持久化压缩摘要|<article/);
    } else {
      assert.match(html, /provider\/model/);
      assert.match(html, /摘要调用实测/);
      assert.match(html, /Saved summary body/);
    }
  }
});
