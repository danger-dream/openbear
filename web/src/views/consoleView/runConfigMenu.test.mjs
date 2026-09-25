import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {themeValues, themeColor, contrast} from '../../testHelpers/theme.mjs';
import {compile, computed, createSSRApp, h, proxyRefs, ref, shallowReactive, watch} from "vue";
import {renderToString} from "vue/server-renderer";
import {parse} from "@vue/compiler-sfc";
import {baseParse} from "@vue/compiler-dom";
const displayUrl = new URL(`./.menu-display-test-${process.pid}.mjs`, import.meta.url);
let display;
try {
  const displaySource = fs.readFileSync(new URL('./display.js', import.meta.url), 'utf8')
    .replace('import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";', 'const ContextCompactionIcon = {};')
    .replace('import {plainText} from "./markdown.js";', 'const plainText = value => String(value || "");');
  fs.writeFileSync(displayUrl, displaySource);
  display = await import(displayUrl.href);
} finally { fs.rmSync(displayUrl, {force: true}); }
const {fmtTokens, modelDefaultThinking, modelLabel, modelShortLabel, thinkingLabel} = display;

const source = fs.readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
const {descriptor} = parse(source);
function walk(nodes) {
  return (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node.children) ? node.children : [])]);
}
const popup = walk(baseParse(descriptor.template.content).children).find(node => node.type === 1 && node.props.some(
  prop => prop.name === "class" && prop.value?.content === "popover-menu-content run-config-popover"));
const render = compile(popup.loc.source);
const chip = walk(baseParse(descriptor.template.content).children).find(node => node.type === 1 && node.props.some(
  prop => prop.name === 'class' && prop.value?.content === 'status-chip run-config-chip'));
const renderChip = compile(chip.loc.source);
const script = descriptor.scriptSetup.content.replace(/import[\s\S]*?from\s+["'][^"']+["'];/g, "").split("function openFilePicker()")[0];
const models = [
  {key: "OpenAI/astra", label: "GPT-6 Astra", contextWindow: 1050000, rolloverTriggerTokens: 300000, maxTokens: 128000, protocol: "responses", reasoning: true, supportsFast: true, defaultThinkingLevel: "xhigh", thinkingLevels: ["low", "medium", "high", "xhigh", "max"]},
  {key: "OpenAI/sol", label: "GPT-5.6 Sol", contextWindow: 1050000, rolloverTriggerTokens: 272000, protocol: "responses", reasoning: true, supportsFast: false},
];
function harness(overrides = {}, tab = "main") {
  const calls = [];
  const context = vm.createContext({computed, ref, watch, useId: () => 'menu-test', fmtTokens, modelDefaultThinking, modelLabel, modelShortLabel, thinkingLabel,
    defineProps: schema => shallowReactive({...Object.fromEntries(Object.entries(schema).map(([key, spec]) => [key, typeof spec.default === "function" ? spec.default() : spec.default])),
      modelMenuOpen: true, modelGroups: [{provider: "OpenAI", models}], currentModel: models[0].key, currentModelInfo: models[0],
      currentThinkLevels: models[0].thinkingLevels, effectiveThinking: "xhigh", supportsThinking: true,
      fastSupported: true, currentFast: false, contextUsedDisplay: "216K", contextThresholdDisplay: "300K", contextWindowDisplay: "1.05M", contextPercentDisplay: "72.0%",
      agentThinkLevels: ["low", "high", "xhigh"], agentSupportsThinking: true, agentFastSupported: true,
      ...overrides}),
    defineEmits: () => (...args) => calls.push(args),
  });
  vm.runInContext(script, context);
  vm.runInContext(`runConfigTab.value = ${JSON.stringify(tab)}`, context);
  const bindings = proxyRefs(vm.runInContext(`({props, emit, runConfigTab, isAgentTab, contextDetailText, contextMeterStyle,
    runConfigModelText, runConfigMetaText, runConfigStrategyText, menuSelectedModel, menuThinkingLevels, menuSupportsThinking, menuThinkingLevel, menuDefaultThinking,
    agentFastTriState, fmtTokens, modelLabel, modelTags, modelFeatures, rolloverTriggerForModel, compactThinkingLabel, selectMenuModel, selectMenuThinking,
    activeModelDetail, modelDetailId, showModelFeature, clearModelDetail, runConfigPopoverVisible})`, context));
  let trees = [];
  const tooltips = [];
  return {bindings, calls, tooltips, async renderChip() {
    const app = createSSRApp({render() { return renderChip.call(this, bindings, []); }});
    app.component('ArrowDown', {render: () => h('svg', {'data-icon': 'ArrowDown'})});
    return renderToString(app);
  }, async render() {
    trees = []; tooltips.length = 0;
    const app = createSSRApp({render() { const tree = render.call(this, bindings, []); trees.push(tree); return tree; }});
    app.component("ElTooltip", {props: ["content", "placement", "showAfter"], render() {
      tooltips.push(this.content); const children = this.$slots.default?.() || []; trees.push(...children); return children;
    }});
    for (const name of ["Search", "CircleCheck"]) app.component(name, {render: () => h("svg", {"data-icon": name})});
    app.component('ModelFeatureIcon', {props: ['name'], render() { return h('span', {'data-icon': this.name}); }});
    const html = await renderToString(app);
    return {html, nodes: walk(trees), buttons: walk(trees).filter(node => node.type === "button")};
  }};
}
const hasClass = (node, name) => String(node.props?.class || "").split(/\s+/).includes(name);

test("model rows display readable numerical attributes and feature badges, omitting protocol details", async () => {
  const h = harness();
  const {html, nodes} = await h.render();
  assert.equal((html.match(/>GPT-6 Astra</g) || []).length, 1);
  assert.equal((html.match(/72\.0%/g) || []).length, 1);
  assert.match(html, />1\.05M 上下文</);
  assert.match(html, />300K 压缩</);
  assert.match(html, />128K 输出</);
  assert.match(html, />思考</);
  assert.match(html, />Fast</);
  assert.doesNotMatch(html, /responses|protocol|未声明/);
  const rows = nodes.filter(node => node.type === "button" && hasClass(node, "model-select"));
  assert.equal(rows.length, 2);
  assert.equal(rows[0].props["aria-pressed"], true);
  assert.equal(rows[1].props["aria-pressed"], false);
  rows[1].props.onClick(uiEvent({type: 'click'}));
  assert.equal(h.calls[0][0], "select-model");
  assert.equal(h.calls[0][1], models[1]);
});

test("main thinking labels stay compact while events retain raw metadata values, Fast and search remain functional", async () => {
  const h = harness();
  const {nodes, buttons} = await h.render();
  const high = buttons.find(node => node.children === "极高");
  assert.equal(high.props["aria-pressed"], true);
  high.props.onClick();
  assert.deepEqual(h.calls.shift(), ["select-thinking", "xhigh"]);
  const fast = buttons.find(node => node.props["aria-label"] === "Fast 模式");
  assert.equal(fast.props.disabled, false);
  fast.props.onClick();
  assert.deepEqual(h.calls.shift(), ["toggle-fast-mode"]);
  nodes.find(node => node.type === "input").props.onInput({target: {value: "sol"}});
  assert.deepEqual(h.calls.shift(), ["update:modelQuery", "sol"]);
});

test("Agent tab preserves independent model selection, model-default thinking and nullable Fast inheritance", async () => {
  const h = harness({agentModel: "", agentThinkLevel: "", agentFastMode: null}, "agent");
  const {html, buttons} = await h.render();
  assert.doesNotMatch(html, /72\.0%/); // Never present Controller usage as Agent usage.
  const follow = buttons.find(node => hasClass(node, "follow-model-row"));
  assert.equal(follow.props["aria-pressed"], true);
  follow.onClick?.() || follow.props.onClick();
  assert.deepEqual(h.calls.shift(), ["select-agent-model", ""]);
  buttons.find(node => hasClass(node, "model-select")).props.onClick(uiEvent({type: 'click'}));
  assert.deepEqual(h.calls.shift(), ["select-agent-model", "OpenAI/astra"]);
  buttons.find(node => node.children === "默认").props.onClick();
  assert.deepEqual(h.calls.shift(), ["select-agent-thinking", ""]);
  buttons.find(node => node.children === "高").props.onClick();
  assert.deepEqual(h.calls.shift(), ["select-agent-thinking", "high"]);
  for (const [label, value] of [["Fast 跟随主会话", null], ["开启 Agent Fast", true], ["关闭 Agent Fast", false]]) {
    const button = buttons.find(node => node.props["aria-label"] === label);
    assert.equal(button.props["aria-pressed"], value === null);
    button.props.onClick();
    assert.deepEqual(h.calls.shift(), ["select-agent-fast", value]);
  }
});

test("unknown usage, unsupported capabilities and empty model search stay explicit", async () => {
  const h = harness({contextUsedDisplay: "待实测", contextPercentDisplay: "—", supportsThinking: false, fastSupported: false, modelGroups: []});
  const {html, buttons} = await h.render();
  assert.match(html, /待实测/);
  assert.match(html, /没有匹配模型/);
  assert.match(html, /未声明支持/);
  assert.doesNotMatch(html, /0\.0%/);
  assert.equal(buttons.find(node => node.props["aria-label"] === "Fast 模式").props.disabled, true);
  const agent = harness({agentFastSupported: false}, "agent");
  const result = await agent.render();
  assert.equal(result.buttons.find(node => node.props["aria-label"] === "开启 Agent Fast").props.disabled, true);
  assert.equal(result.buttons.find(node => node.props["aria-label"] === "关闭 Agent Fast").props.disabled, undefined);
});

function uiEvent({type = 'pointerenter', pointerType = 'mouse', focusVisible = true} = {}) {
  return {type, pointerType, stopPropagation() {}, currentTarget: {
    matches: selector => selector === ':focus-visible' && focusVisible,
  }};
}
const modelRows = result => result.buttons.filter(node => hasClass(node, 'model-select'));

test('unreported optional properties stay absent, fallback threshold stays accurate and long keys remain accessible', async () => {
  const key = 'Provider/' + 'long-model-name-'.repeat(8);
  const h = harness({modelGroups: [{provider: 'Provider', models: [{key, label: 'Long model', protocol: ''}]}]});
  const result = await h.render();
  assert.ok(modelRows(result)[0].props['aria-label'].includes(key));
  assert.equal(result.nodes.find(node => hasClass(node, 'model-row-tags')), undefined);
  const fallback = h.bindings.rolloverTriggerForModel({key: 'p/m', contextWindow: 100000, windowTriggerRatio: .6});
  assert.equal(fallback, 60000);
});

test('six property meanings use official local Lucide SVGs with their license and theme-safe rendering', async () => {
  const iconUrl = new URL('./ModelFeatureIcon.vue', import.meta.url);
  const iconSource = fs.readFileSync(iconUrl, 'utf8');
  const {descriptor: icon} = parse(iconSource);
  const imports = [...icon.scriptSetup.content.matchAll(/import (\w+) from '([^']+\.svg)'/g)];
  const paths = Object.fromEntries(imports.map(([, name, path]) => [name, path]));
  assert.deepEqual(Object.fromEntries(Object.entries(paths).map(([name, path]) => [name, path.split('/').at(-1)])), {
    brain: 'brain.svg', zap: 'zap.svg', context: 'messages-square.svg', compression: 'file-archive.svg', output: 'file-output.svg', protocol: 'webhook.svg',
  });
  const icons = Object.fromEntries(Object.entries(paths).map(([name, path]) => [name, '/assets/' + path.split('/').at(-1)]));
  const draw = compile(icon.template.content);
  for (const [name, path] of Object.entries(paths)) {
    const svg = fs.readFileSync(new URL(path, iconUrl), 'utf8');
    assert.match(svg, /viewBox="0 0 24 24"/);
    assert.match(svg, /stroke="currentColor"/);
    assert.doesNotMatch(svg, /<script|foreignObject|\son\w+=|href=/i);
    const app = createSSRApp({render() { return draw.call(this, {icons, name}, []); }});
    const html = await renderToString(app);
    assert.ok(html.includes(icons[name]));
    assert.match(html, /aria-hidden="true"/);
  }
  assert.match(iconSource, /background: currentColor/);
  assert.match(iconSource, /mask: var\(--feature-svg\)/);
  const license = fs.readFileSync(new URL('../../../public/assets/licenses/lucide.txt', import.meta.url), 'utf8');
  assert.match(license, /ISC License[\s\S]*2026 Lucide Icons and Contributors/);
  assert.match(fs.readFileSync(new URL('../../assets/model-icons/README.md', import.meta.url), 'utf8'), /5d592a96aaeb4a3a09ffd8134f8f5ed878c9a0c5/);
});

test('collapsed model button exposes the shared compression mode without losing model, thinking, Fast or tokens', async () => {
  const h = harness({modelMenuOpen: false, contextStrategy: 'sliding_window', currentFast: true, contextDisplay: '216K / 300K'});
  let html = await h.renderChip();
  assert.match(html, /class="run-config-chip-model">GPT-6 Astra</);
  assert.match(html, /class="run-config-chip-strategy" aria-label="上下文压缩：滑动窗口">滑窗压缩</);
  assert.doesNotMatch(html, /data-icon="SlidingWindowIcon"|run-config-chip-strategy-icon/);
  assert.match(html, /aria-description="上下文压缩：滑动窗口"/);
  assert.match(html, /class="run-config-chip-meta">[^<]*xhigh · Fast · 216K \/ 300K</);
  h.bindings.props.contextStrategy = 'model_summary';
  html = await h.renderChip();
  assert.match(html, /class="run-config-chip-strategy" aria-label="上下文压缩：模型摘要">摘要压缩</);
  assert.doesNotMatch(html, /data-icon="ContextCompactionIcon"|>滑窗压缩</);
  assert.doesNotMatch(html, /滑动窗口/);
  h.bindings.runConfigTab = 'agent';
  assert.match(await h.renderChip(), />摘要压缩</);
  h.bindings.props.contextStrategy = 'sliding_window';
  assert.match(await h.renderChip(), />滑窗压缩</);
  assert.deepEqual(h.calls, [], 'rendering the selected strategy never changes configuration');
});

test('collapsed button keeps quiet model typography and explicit compression metadata without a badge', async () => {
  const h = harness({currentModelInfo: null, supportsThinking: false, contextDisplay: '—'});
  const html = await h.renderChip();
  assert.match(html, /class="run-config-chip-model">模型</);
  assert.match(html, /class="run-config-chip-strategy" aria-label="上下文压缩：滑动窗口">滑窗压缩</);
  assert.doesNotMatch(html, /data-icon="SlidingWindowIcon"|run-config-chip-strategy-icon/);
  assert.doesNotMatch(html, /class="run-config-chip-meta"/);
  assert.match(source, /\.run-config-chip-main\s*\{[^}]*display: inline-flex/);
  assert.doesNotMatch(source, /\.run-config-chip-main\s*\{[^}]*flex-direction: column/);
  assert.doesNotMatch(source, /class="run-config-chip-title"|run-config-chip-strategy-icon|SlidingWindowIcon/);
  const strategyCss = source.match(/\.run-config-chip-strategy\s*\{([^}]*)\}/)[1];
  assert.match(strategyCss, /flex: 0 0 auto/);
  assert.doesNotMatch(strategyCss, /background:|border:|padding:|font-size:/);
  assert.match(source, /\.run-config-chip-model\s*\{[^}]*text-overflow: ellipsis/);
  assert.match(source, /\.run-config-chip-meta\s*\{[^}]*display: none/); // Phone toolbar shows only the model; details remain in settings.
  const css = source.slice(source.indexOf('.run-config-chip {'), source.indexOf('.chip-caret {'));
  const sizes = [...css.matchAll(/font-size:\s*([\d.]+)px/g)].map(match => Number(match[1]));
  assert.deepEqual(sizes, [11.2]); // The chip retains its restored size independently of the popup's 13/12px scale.
  assert.match(css, /height: 1\.78rem/);
  assert.match(css, /gap: 0\.34rem/);
  assert.match(css, /padding: 0 0\.34rem 0 0\.48rem/);
  assert.match(css, /\.run-config-chip-model\s*\{[^}]*font-weight: 500/);
  assert.match(css, /\.run-config-chip-meta\s*\{[^}]*color: var\(--ob-text-muted\);[^}]*font-weight: 520/);
});

for (const theme of ['light', 'dark']) {
  test(`${theme} model details contrast strongly with both their text and the model list`, () => {
    const css = source.match(/\.run-config-popover\s*\{([^}]*)\}/)[1];
    const values = themeValues(theme === 'dark');
    for (const [, name, value] of css.matchAll(/(--rc-[\w-]+):\s*([^;]+);/g)) values.set(name, value);
    const backdrop = themeColor('var(--ob-surface-raised)', values);
    const color = name => themeColor(`var(--rc-${name})`, values, backdrop);
    assert.ok(contrast(color('detail-bg'), color('detail-text')) >= 10, 'readable tooltip text');
    assert.ok(contrast(color('detail-bg'), color('hover')) >= 7, 'visually separates the floating detail from rows');
    assert.ok(contrast(color('detail-bg'), color('selected')) >= 7, 'visually separates it from selected rows too');
  });
}

test("popup typography and surfaces are unified, and short viewports keep every control reachable", () => {
  const css = source.slice(source.indexOf("/* Model picker:"), source.indexOf(".send-button {", source.indexOf("/* Model picker:")));
  const sizes = [...css.matchAll(/font-size:\s*(\d+)px/g)].map(match => Number(match[1]));
  assert.ok(sizes.length >= 8 && sizes.every(size => size >= 12 && size <= 13));
  assert.match(css, /\.model-row-name[^}]*font-size: 13px/);
  assert.match(css, /\.model-group-title[^}]*font-size: 12px/);
  assert.match(css, /\.model-tag[^}]*font-size: 12px/);
  assert.doesNotMatch(css, /font-weight:\s*[6-9]\d\d|linear-gradient|text-transform:\s*uppercase/);
  assert.match(source, /\.run-config-popover\s*\{[^}]*--rc-text: var\(--ob-text\);[^}]*--rc-accent: var\(--ob-blue\);/);
  assert.match(css, /@media \(max-height: 620px\)[\s\S]*?overflow-y: auto/);
  assert.match(css, /\.model-row-name[^}]*text-overflow: ellipsis/);
  assert.match(css, /\.thinking-segments[^}]*flex-wrap: wrap/);
});
