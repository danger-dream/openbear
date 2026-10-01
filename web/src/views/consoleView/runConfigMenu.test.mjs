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
  const context = vm.createContext({computed, ref, watch, scheduleRunConfigPosition() {}, useId: () => 'menu-test', fmtTokens, modelDefaultThinking, modelLabel, modelShortLabel, thinkingLabel,
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
    runConfigModelText, runConfigMetaText, runConfigMetaParts, runConfigStrategyText, runConfigThinkingBadge, runConfigStatusLabel, menuSelectedModel, menuThinkingLevels, menuSupportsThinking, menuThinkingLevel, menuDefaultThinking,
    agentFastTriState, fmtTokens, modelLabel, modelTags, modelFeatures, rolloverTriggerForModel, compactThinkingLabel, selectMenuModel, selectMenuThinking,
    activeModelDetail, modelDetailId, showModelFeature, clearModelDetail, runConfigPopoverVisible,
    runConfigContent, runConfigSearchInput, runConfigCompact, runConfigSettingsOpen, runConfigSettingsSummary, toggleRunConfigSettings, finishRunConfigSearch})`, context));
  let trees = [];
  const tooltips = [];
  return {bindings, calls, tooltips, async renderChip() {
    const app = createSSRApp({render() { return renderChip.call(this, bindings, []); }});
    app.component('ArrowDown', {render: () => h('svg', {'data-icon': 'ArrowDown'})});
    app.component('ModelFeatureIcon', {props: ['name'], render() { return h('span', {'data-icon': this.name}); }});
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

test("Agent tab preserves independent model selection, main-following thinking and nullable Fast inheritance", async () => {
  const h = harness({agentModel: "", agentThinkLevel: "", agentFastMode: null}, "agent");
  const {html, buttons} = await h.render();
  assert.doesNotMatch(html, /72\.0%/); // Never present Controller usage as Agent usage.
  const follow = buttons.find(node => hasClass(node, "follow-model-row"));
  assert.equal(follow.props["aria-pressed"], true);
  follow.onClick?.() || follow.props.onClick();
  assert.deepEqual(h.calls.shift(), ["select-agent-model", ""]);
  buttons.find(node => hasClass(node, "model-select")).props.onClick(uiEvent({type: 'click'}));
  assert.deepEqual(h.calls.shift(), ["select-agent-model", "OpenAI/astra"]);
  buttons.find(node => node.children === "跟随" && !node.props["aria-label"]).props.onClick();
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
  const button = html.match(/^<button[^>]*>/)[0];
  assert.match(button, /aria-label="运行配置"/);
  assert.match(button, /aria-description="上下文压缩：滑动窗口，思考强度：极高，Fast 模式已开启"/);
  assert.match(html, /class="run-config-chip-meta"><!--\[--><span class="run-config-meta-part"><!--\[-->极高<!--\]--><\/span><span class="run-config-meta-part run-config-meta-fast" title="Fast 模式"><span data-icon="zap"><\/span><\/span><span class="run-config-meta-part"><!--\[-->216K \/ 300K<!--\]--><\/span><!--\]--><\/span>/);
  assert.equal((html.match(/class="run-config-chip-meta"[\s\S]*?<\/span><\/span>/)?.[0].match(/Fast(?! 模式)/g) || []).length, 0, 'desktop metadata uses the icon instead of Fast text');
  assert.equal(h.bindings.runConfigMetaText, '极高 · Fast · 216K / 300K');
  assert.match(html, /class="run-config-chip-status" aria-hidden="true"/);
  assert.doesNotMatch(html, /run-config-chip-status" role="img"/);
  assert.match(html, /class="run-config-status-thinking">极高</, 'thinking shows only the level, without an icon');
  assert.match(html, /class="run-config-status-fast"><span data-icon="zap"><\/span><\/span>/, 'Fast is an icon only');
  assert.doesNotMatch(html, /data-icon="brain"/);
  const inline = html.indexOf('run-config-chip-model') < html.indexOf('run-config-chip-status')
    && html.indexOf('run-config-chip-status') < html.indexOf('run-config-chip-strategy');
  assert.ok(inline, 'model, then thinking · Fast, then compression, on one line');
  h.bindings.props.contextStrategy = 'model_summary';
  html = await h.renderChip();
  assert.match(html.match(/^<button[^>]*>/)[0], /aria-description="上下文压缩：模型摘要，思考强度：极高，Fast 模式已开启"/);
  assert.match(html, /class="run-config-chip-strategy" aria-label="上下文压缩：模型摘要">摘要压缩</);
  assert.doesNotMatch(html, /data-icon="ContextCompactionIcon"|>滑窗压缩</);
  assert.doesNotMatch(html, /滑动窗口/);
  h.bindings.runConfigTab = 'agent';
  assert.match(await h.renderChip(), />摘要压缩</);
  h.bindings.props.contextStrategy = 'sliding_window';
  assert.match(await h.renderChip(), />滑窗压缩</);
  assert.deepEqual(h.calls, [], 'rendering the selected strategy never changes configuration');
});

test('collapsed status row follows effective thinking and Fast, omitting off states', async () => {
  const h = harness({modelMenuOpen: false, currentFast: false, effectiveThinking: 'low'});
  let html = await h.renderChip();
  assert.match(html.match(/^<button[^>]*>/)[0], /aria-description="上下文压缩：滑动窗口，思考强度：低"/);
  assert.match(html, /class="run-config-status-thinking">低</);
  assert.doesNotMatch(html, /run-config-status-fast/);
  h.bindings.props.effectiveThinking = 'off'; h.bindings.props.currentFast = true;
  html = await h.renderChip();
  assert.match(html.match(/^<button[^>]*>/)[0], /aria-description="上下文压缩：滑动窗口，Fast 模式已开启"/);
  assert.doesNotMatch(html, /run-config-status-thinking/);
  h.bindings.props.currentFast = false;
  html = await h.renderChip();
  assert.doesNotMatch(html, /run-config-chip-status/);
  assert.match(html.match(/^<button[^>]*>/)[0], /aria-description="上下文压缩：滑动窗口"/);
  assert.deepEqual(h.calls, [], 'status display never changes configuration');
});

test('collapsed status maps every supported level and describes only effective main settings, not the Agent tab', async () => {
  for (const [level, label] of Object.entries({minimal: '极简', low: '低', medium: '中', high: '高', xhigh: '极高', max: '最高'})) {
    const h = harness({effectiveThinking: level, currentFast: false, agentEffectiveThinking: 'max', agentEffectiveFast: true}, 'agent');
    const html = await h.renderChip();
    assert.equal(h.bindings.runConfigThinkingBadge, label);
    assert.ok(html.match(/^<button[^>]*>/)[0].includes(`aria-description="上下文压缩：滑动窗口，思考强度：${label}"`));
    assert.match(html, /run-config-chip-status" aria-hidden="true"/);
    assert.doesNotMatch(html, /run-config-status-fast/);
    assert.deepEqual(h.calls, []);
  }
  for (const effectiveThinking of ['high', 'off', '']) {
    const h = harness({supportsThinking: false, effectiveThinking, currentFast: false});
    const html = await h.renderChip();
    assert.doesNotMatch(html, /run-config-chip-status/);
    assert.match(html.match(/^<button[^>]*>/)[0], /aria-description="上下文压缩：滑动窗口"/);
  }
});

test('collapsed button keeps quiet model typography and explicit compression metadata without a badge', async () => {
  const h = harness({currentModelInfo: null, supportsThinking: false, contextDisplay: '—'});
  const html = await h.renderChip();
  assert.match(html, /class="run-config-chip-model">模型</);
  assert.match(html, /class="run-config-chip-strategy" aria-label="上下文压缩：滑动窗口">滑窗压缩</);
  assert.doesNotMatch(html, /data-icon="SlidingWindowIcon"|run-config-chip-strategy-icon/);
  assert.doesNotMatch(html, /class="run-config-chip-meta"/);
  assert.doesNotMatch(html, /class="run-config-chip-status"/, 'no empty status row without thinking or Fast');
  assert.match(source, /\.run-config-chip-main\s*\{[^}]*display: inline-flex/);
  assert.doesNotMatch(source, /\.run-config-chip-main\s*\{[^}]*flex-direction: column/);
  assert.doesNotMatch(source, /run-config-chip-title/, 'status is inline beside the model, not a second row');
  assert.match(source, /\.run-config-chip-status::before,\s*\.run-config-chip-status > span \+ span::before\s*\{\s*content: "·"/);
  assert.doesNotMatch(source, /run-config-chip-strategy-icon|SlidingWindowIcon/);
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

for (const tab of ['main', 'agent']) {
  test(`${tab} compact search retains query and configuration, and settings replace rather than wrap the results`, async () => {
    const r = harness({modelQuery: 'astra'}, tab);
    let blurred = 0;
    r.bindings.runConfigSearchInput = {blur() {blurred++;}};
    r.bindings.runConfigCompact = true;
    const shown = (nodes, name) => nodes.find(node => hasClass(node, name)).dirs.find(dir => typeof dir.value === 'boolean').value;
    let result = await r.render();
    assert.equal(shown(result.nodes, 'run-config-model-section'), true);
    assert.equal(shown(result.nodes, 'run-config-search'), true);
    assert.equal(shown(result.nodes, 'run-config-controls'), false);
    assert.match(result.html, /Fast/); assert.match(result.html, /滑动窗口/);
    if (tab === 'main') assert.match(result.html, /上下文 216K \/ 300K · 72\.0%/);
    const settings = result.buttons.find(button => button.props?.onClick === r.bindings.toggleRunConfigSettings);
    settings.props.onClick(); assert.equal(blurred, 1);
    result = await r.render();
    assert.equal(shown(result.nodes, 'run-config-model-section'), false);
    assert.equal(shown(result.nodes, 'run-config-search'), false);
    assert.equal(shown(result.nodes, 'run-config-controls'), true);
    assert.match(result.html, /返回模型/); assert.match(result.html, /思考强度/); assert.match(result.html, /上下文压缩/);
    settings.props.onClick(); result = await r.render();
    assert.equal(shown(result.nodes, 'run-config-model-section'), true);
    assert.equal(r.bindings.props.modelQuery, 'astra');
    assert.equal(r.bindings.props.currentFast, false); assert.equal(r.bindings.props.effectiveThinking, 'xhigh');
    assert.equal(r.calls.length, 0, 'switching panes changes no configuration or business state');
    const done = result.buttons.find(button => button.props?.onClick === r.bindings.finishRunConfigSearch);
    done.props.onClick(); assert.deepEqual(r.calls, [['close-menus']]); assert.equal(blurred, 3);
    assert.equal(r.bindings.props.modelQuery, 'astra');
  });
}

test("popup typography and surfaces are unified, and short viewports keep every control reachable", () => {
  const css = source.slice(source.indexOf("/* Model picker:"), source.indexOf(".send-button {", source.indexOf("/* Model picker:")));
  const sizes = [...css.matchAll(/font-size:\s*(\d+)px/g)].map(match => Number(match[1]));
  assert.ok(sizes.length >= 8 && sizes.every(size => (size >= 12 && size <= 13) || size === 16));
  assert.match(css, /\.run-config-search input\s*\{\s*font-size: 16px;\s*\}/, 'only the mobile search input gets the iOS-safe size');
  assert.match(css, /\.model-row-name[^}]*font-size: 13px/);
  assert.match(css, /\.model-group-title[^}]*font-size: 12px/);
  assert.match(css, /\.model-tag[^}]*font-size: 12px/);
  assert.doesNotMatch(css, /font-weight:\s*[6-9]\d\d|linear-gradient|text-transform:\s*uppercase/);
  assert.match(source, /\.run-config-popover\s*\{[^}]*--rc-text: var\(--ob-text\);[^}]*--rc-accent: var\(--ob-blue\);/);
  assert.match(css, /@media \(max-height: 620px\)[\s\S]*?overflow-y: auto/);
  assert.match(css, /\.model-row-name[^}]*text-overflow: ellipsis/);
  assert.match(css, /\.thinking-segments[^}]*flex-wrap: wrap/);
});

test('Agent unset thinking is presented as following the main conversation, not the model default', async () => {
  const h = harness({agentThinkLevel: '', agentEffectiveThinking: 'low', agentDefaultThinkingLabel: 'xhigh'}, 'agent');
  const {html} = await h.render();
  const tooltips = h.tooltips;
  assert.match(html, />跟随</);
  assert.match(html, /跟随 · 低/);
  assert.doesNotMatch(html, /<button[^>]*>默认<\/button>/);
  assert.ok(tooltips.includes('跟随主会话思考强度，当前为低'));
  assert.match(h.bindings.runConfigSettingsSummary, /^思考 跟随/);
});

test('main tab keeps model-default wording for main thinking', async () => {
  const h = harness({}, 'main');
  const {html} = await h.render();
  assert.match(html, /默认 极高/);
  assert.doesNotMatch(html, /跟随主会话思考强度/);
});
