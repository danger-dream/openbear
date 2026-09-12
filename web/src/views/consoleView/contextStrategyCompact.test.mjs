import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {compile, createSSRApp, h} from "vue";
import {renderToString} from "vue/server-renderer";
import {parse, compileTemplate} from "@vue/compiler-sfc";
import {baseParse} from "@vue/compiler-dom";

const source = fs.readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
const {descriptor} = parse(source);
function find(nodes, predicate) {
  for (const node of nodes || []) {
    if (predicate(node)) return node;
    const match = find(Array.isArray(node.children) ? node.children : [], predicate);
    if (match) return match;
  }
}
const row = find(baseParse(descriptor.template.content).children, node => node.type === 1 && node.props.some(
  prop => prop.name === "class" && prop.value?.content === "fast-control context-strategy-control"));

test("strategy setting is one shared Fast-style row below the model controls, not a large panel above the list", () => {
  assert.ok(row);
  assert.doesNotMatch(source, /ContextStrategySwitch|run-config-context-strategy|context-strategy-heading/);
  assert.ok(source.indexOf(row.loc.source) > source.indexOf('class="thinking-segments agent-fast-segments"'));
  assert.equal((source.match(/class="fast-control context-strategy-control"/g) || []).length, 1);
  assert.doesNotMatch(row.loc.source, /<small|<p[ >]|strategy-caption/);
  assert.match(row.loc.source, /class="fast-switch"/);
  assert.match(source, /\.run-config-model-list\s*\{[^}]*min-height: 0;[^}]*flex: 1 1 auto;[^}]*overflow-y: auto;/);
  assert.deepEqual(compileTemplate({source: descriptor.template.content, filename: "ConsoleComposer.vue", id: "composer"}).errors, []);
});

for (const strategy of ["sliding_window", "model_summary"]) {
  for (const saving of [false, true]) {
    test(`compact strategy row renders and preserves switch contract: ${strategy}, saving=${saving}`, async () => {
      const props = {contextStrategy: strategy, strategySaving: saving};
      const calls = [];
      const render = compile(row.loc.source);
      let tree;
      const app = createSSRApp({render() {
        tree = render.call(this, {props, emit: (...args) => calls.push(args)}, []);
        return tree;
      }});
      app.component("ModelFeatureIcon", {props: ['name'], render: () => h("svg")});
      app.component("ElTooltip", {props: ["content", "placement", "showAfter"], render() { return this.$slots.default?.(); }});
      const html = await renderToString(app);
      const button = find([tree], node => node.type === "button");
      assert.ok(button);
      assert.equal(button.props.role, "switch");
      assert.equal(button.props["aria-checked"], strategy === "model_summary");
      assert.equal(button.props.disabled, saving);
      assert.match(html, new RegExp(`aria-live="polite">${strategy === "model_summary" ? "模型摘要" : "滑动窗口"}`));
      if (!saving) {
        button.props.onClick();
        assert.deepEqual(calls, [["select-context-strategy", strategy === "model_summary" ? "sliding_window" : "model_summary"]]);
      }
      assert.match(row.loc.source, /<el-tooltip content="主会话与 Agent 共用/);
      assert.doesNotMatch(html, /主会话与 Agent 共用/); // Hover help, not a second visible line.
    });
  }
}
