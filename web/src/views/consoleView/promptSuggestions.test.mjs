import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {parse} from "@vue/compiler-sfc";
import {compile} from "@vue/compiler-dom";
import * as Vue from "vue";
import {renderToString} from "vue/server-renderer";

const read = path => readFileSync(new URL(path, import.meta.url), "utf8");
const {descriptor} = parse(read("./PromptSuggestions.vue"));
const script = descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, "");
const {code} = compile(descriptor.template.content, {
  mode: "function", prefixIdentifiers: true,
  bindingMetadata: {props: "setup-const", emit: "setup-const", expanded: "setup-ref", Sparkles: "setup-const", X: "setup-const", ChevronDown: "setup-const"},
});
const render = new Function("Vue", code)(Vue);
const icon = {render: () => Vue.h("svg")};
const items = ["继续", "换一个更长的方案，把桌面和手机都检查一遍", "先别改"];

function view(collapsed = false) {
  const emitted = [];
  const props = Vue.reactive({items, collapsed});
  const context = vm.createContext({ref: Vue.ref, watch: Vue.watch, defineProps: () => props, defineEmits: () => (...args) => emitted.push(args)});
  const bindings = vm.runInContext(`${script}\n;({props, emit, expanded})`, context);
  const setup = Vue.proxyRefs({...bindings, Sparkles: icon, X: icon, ChevronDown: icon});
  const tree = () => render({}, [], {}, setup);
  return {props, emitted, tree, html: () => renderToString(Vue.createSSRApp({render: tree}))};
}

function buttons(node, out = []) {
  if (!node || typeof node !== "object") return out;
  if (Array.isArray(node)) {
    node.forEach(child => buttons(child, out));
    return out;
  }
  if (node.type === "button") out.push(node);
  buttons(node.children, out);
  return out;
}

test("an empty editor shows every suggestion; picking fills and closing dismisses", async () => {
  const component = view();
  const html = await component.html();
  assert.match(html, /aria-label="推荐的下一句"/);
  assert.match(html, /接着说/);
  assert.match(html, /aria-expanded="true"/);
  assert.doesNotMatch(html, /\d+ 条|disabled|猜你想说/);
  const [, close, first] = buttons(component.tree());
  assert.equal(close.props["aria-label"], "关闭推荐");
  first.props.onClick();
  close.props.onClick();
  assert.deepEqual(component.emitted, [["pick", "继续"], ["dismiss"]]);
});

test("a draft folds the list to one line that still opens and picks", async () => {
  const component = view(true);
  let html = await component.html();
  assert.match(html, /class="prompt-suggestions is-collapsed"/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /3 条/);
  assert.doesNotMatch(html, /填入：/);
  buttons(component.tree())[0].props.onClick();
  html = await component.html();
  assert.match(html, /aria-expanded="true"/);
  assert.doesNotMatch(html, /disabled/);
  buttons(component.tree())[3].props.onClick();
  assert.deepEqual(component.emitted, [["pick", "换一个更长的方案，把桌面和手机都检查一遍"]], "another pick replaces the draft");
});

test("typing folds and clearing the draft unfolds automatically", async () => {
  const component = view();
  component.props.collapsed = true;
  await Vue.nextTick();
  assert.match(await component.html(), /is-collapsed/);
  component.props.collapsed = false;
  await Vue.nextTick();
  assert.doesNotMatch(await component.html(), /is-collapsed/);
});

test("long suggestions wrap without a card frame and keep touch-sized targets", () => {
  const css = descriptor.styles[0].content;
  assert.match(css, /\.prompt-suggestions-list\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(css, /\.prompt-suggestion\s*\{[^}]*max-width:\s*100%[^}]*overflow-wrap:\s*anywhere/);
  assert.match(css, /@media \(max-width: 760px\), \(hover: none\) and \(pointer: coarse\)\s*\{\s*\.prompt-suggestion\s*\{\s*min-height:\s*38px/);
  assert.doesNotMatch(css, /box-shadow|opacity:\s*\.\d/);
});

test("the composer no longer hosts suggestions; the transcript receives them", () => {
  const composer = read("./ConsoleComposer.vue");
  assert.doesNotMatch(composer, /猜你想说|prompt-suggestion|promptSuggestions/);
  const consoleView = read("./ConsoleView.vue");
  const turnList = consoleView.match(/<TurnList[\s\S]*?\/>/)[0];
  assert.match(turnList, /:prompt-suggestions="transcriptPromptSuggestions"/);
  assert.match(turnList, /:prompt-suggestions-collapsed="Boolean\(String\(draft \|\| ''\)\.trim\(\)\)"/);
  assert.match(turnList, /@pick-suggestion="pickPromptSuggestion"/);
  assert.match(turnList, /@dismiss-suggestions="dismissPromptSuggestions"/);
  const composerTag = consoleView.match(/<ConsoleComposer[\s\S]*?\/>/)[0];
  assert.doesNotMatch(composerTag, /suggestion/);
  assert.match(consoleView, /running\.value \|\| pendingConfirmations\.value\.length \|\| pendingSteering\.value\.length/);
});
