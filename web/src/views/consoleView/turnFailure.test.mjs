import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import pkg from "@vue/compiler-sfc";
import {compile, createSSRApp, h, ref} from "vue";
import {renderToString} from "vue/server-renderer";
import {projectOperationMessages} from "../../timelineProjection.js";

// Render the real TurnEvent SFC. Only the browser-only markdown pipeline is
// stubbed: these assertions are about which timeline row carries the failure
// affordance, not about markdown rendering.
const {parse} = pkg;
const eventUrl = new URL("./TurnEvent.vue", import.meta.url);
const {descriptor} = parse(readFileSync(eventUrl, "utf8"));
// The template calls the two markdown helpers directly, so define them in the
// script body; the setup-export collector below then exposes them by name.
const script = descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "")
  + "\nfunction hasMeaningfulAnswerText(text) { return Boolean(String(text || '').trim()); }"
  + "\nfunction answerContent(text) { return String(text || ''); }\n";
const render = compile(descriptor.template.content);
const setupNames = [...script.matchAll(/^(?:async )?function (\w+)\(/gm)].map((match) => match[1]);
const slot = {inheritAttrs: false, setup: (_, {slots}) => () => slots.default?.()};

function turnEvent(event) {
  const props = {event, turnId: "turn-1", index: 0, detailKey: () => "detail", isDetailOpen: () => false, activeToolResultIndex: () => 0};
  const context = vm.createContext({
    ref,
    defineProps: () => props,
    defineEmits: () => () => {},
    isAgentEvent: () => false,
    modelRetryReasonLabel: () => "",
    toolResultKey: () => "",
    toolStatus: () => "completed",
    answerContent: (text) => String(text || ""),
    hasMeaningfulAnswerText: (text) => Boolean(String(text || "").trim()),
    plainText: (text) => String(text || "").trim(),
    isUserInteractionEvent: () => false,
    retryStatusView: () => ({label: "", tone: "neutral"}),
    retryWaitLabel: () => "",
    reactive: (value) => value,
    computed: (getter) => ({get value() { return getter(); }}),
    watch: () => {},
    onBeforeUnmount: () => {},
    nextTick: async () => {},
    window: {clearTimeout() {}, setTimeout() { return 1; }, cancelAnimationFrame() {}, requestAnimationFrame() { return 1; }},
    performance: {now: () => 0},
    document: {addEventListener() {}, removeEventListener() {}},
    __VUE_OPTIONS_API__: true,
    __VUE_PROD_DEVTOOLS__: false,
  });
  vm.runInContext(script, context);
  const app = createSSRApp({
    render,
    setup: () => vm.runInContext(`({props, emit: () => {}, ${setupNames.join(", ")}})`, context),
  });
  for (const name of ["el-tooltip", "el-icon", "el-image", "Check", "CopyDocument", "RefreshLeft", "Link",
    "ArrowRight", "Bell", "CircleCheck", "CircleClose", "MagicStick", "Refresh", "Tools"]) {
    app.component(name, slot);
  }
  app.component("AgentEventCard", slot);
  app.component("ConsoleToolEvent", slot);
  app.component("ConsoleUserInteractionEvent", slot);
  app.component("ConsoleMarkdown", {props: ["text"], setup: (props) => () => h("p", {class: "stub-md"}, props.text)});
  app.directive("reasoning-autoscroll", {});
  return renderToString(app);
}

const banner = (html) => (html.match(/class="answer-error-banner"/g) || []).length;

test("only the failure row renders the error banner, not partial text stamped by the error cascade", async () => {
  // Mirrors the real shape: the run failed, so the rows that were still open
  // received the error text while the reporting row carries the boolean marker.
  const err = "A maximum of 4 blocks with cache_control may be provided. Found 5.";
  const projected = projectOperationMessages([
    {opId: "user", opType: "user_message", turnId: "turn-1", displaySeq: 1, createdAtMs: 1789787591000, payload: {text: "改吧"}},
    {opId: "partial-1", opType: "assistant_message", turnId: "turn-1", displaySeq: 2, status: "failed", lifecycle: "terminal",
      createdAtMs: 1789787592000, payload: {text: "我先确认接线点。", complete: true, status: "failed", error: err}},
    {opId: "assistant-error:turn-1", opType: "assistant_message", turnId: "turn-1", displaySeq: 3, status: "failed", lifecycle: "terminal",
      createdAtMs: 1789788834000, payload: {text: err, error: true, complete: true, status: "failed"}},
  ]);

  const assistant = projected.find((message) => message.role === "assistant");
  const answers = assistant.localTimeline.filter((item) => item.kind === "answer");
  assert.equal(answers.length, 2);
  assert.deepEqual(answers.map((item) => item.message.failure), [false, true]);
  assert.deepEqual(answers.map((item) => item.message.error), [true, true]);

  const html = await turnEvent(answers[0]);
  assert.equal(banner(html), 0, "partial text that was open when the run failed must not look like the failure");
  const errorHtml = await turnEvent(answers[1]);
  assert.equal(banner(errorHtml), 1, "the reporting row must render the failure affordance");
  assert.match(errorHtml, /本轮执行失败/);
});

test("a legacy error row without the boolean marker keeps rendering its text only", async () => {
  // Rows persisted before the boolean marker existed must not gain a banner.
  const html = await turnEvent({
    kind: "answer",
    id: "legacy",
    error: true,
    operation: {opId: "legacy", opType: "assistant_message", status: "failed", payload: {error: "boom"}},
    message: {content: "旧版错误行", error: true},
  });
  assert.equal(banner(html), 0);
  assert.match(html, /旧版错误行/);
});

test("a normal completed answer renders no failure affordance", async () => {
  const html = await turnEvent({kind: "answer", id: "ok", message: {content: "完成了", error: false, failure: false}});
  assert.equal(banner(html), 0);
  assert.match(html, /完成了/);
});
