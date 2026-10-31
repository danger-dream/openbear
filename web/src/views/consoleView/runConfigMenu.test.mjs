import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {themeValues, themeColor, contrast} from '../../testing/theme.mjs';
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
const {fmtTokens, modelDefaultThinking, modelThinkingLevels, modelLabel, modelShortLabel, thinkingLabel} = display;

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
  const context = vm.createContext({computed, ref, watch, scheduleRunConfigPosition() {}, fmtTokens, modelDefaultThinking, modelLabel, modelShortLabel, thinkingLabel,
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
  const bindings = proxyRefs(vm.runInContext(`({props, emit, runConfigTab, isAgentTab, contextPercentNumber, runConfigContextRingClass,
    runConfigModelText, runConfigMetaText, runConfigMetaParts, runConfigStrategyText, runConfigThinkingBadge, runConfigStatusLabel, menuSelectedModel, menuThinkingLevels, menuSupportsThinking, menuThinkingLevel, menuDefaultThinking,
    agentFastTriState, fmtTokens, modelLabel, modelTags, rolloverTriggerForModel, compactThinkingLabel, selectMenuModel, selectMenuThinking,
    runConfigPopoverVisible,
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

test('desktop context adds the existing-style ring before its numbers without changing other chip content', async () => {
  for (const [percent, fill, tone] of [['58.8%', '58.8', ''], ['90.0%', '90', 'is-warning'], ['120.0%', '100', 'is-danger'], ['—', '0', 'is-unknown']]) {
    const label = `160K / 272K（${percent}）`;
    const h = harness({currentFast: true, contextDisplay: label, contextPercentDisplay: percent});
    const html = await h.renderChip();
    assert.equal((html.match(/class="run-config-context-ring(?:\s|")/g) || []).length, 1);
    assert.match(html, new RegExp(`stroke-dasharray="${fill} 100"`));
    if (tone) assert.ok(html.includes(`run-config-context-ring ${tone}`));
    assert.ok(html.indexOf('data-icon="zap"') < html.indexOf('class="run-config-context-ring'));
    assert.ok(html.indexOf('class="run-config-context-ring') < html.indexOf(label));
    assert.ok(html.includes('GPT-6 Astra') && html.includes('极高') && html.includes('滑窗压缩'));
    assert.ok(!(await h.render()).html.includes('run-config-context-ring'), 'the picker popover is unchanged');
  }
  assert.match(source, /\.run-config-context-ring\s*\{[^}]*display:\s*inline-block\s*;/,
    'the inline usage ring must override Tailwind SVG display:block or it splits the chip into clipped lines');
  assert.match(source, /@media \(max-width: 760px\)[\s\S]*?\.run-config-chip-meta \{ display: none; \}/);
});

test("model rows display readable numerical attributes and feature badges, omitting protocol details", async () => {
  const h = harness();
  const {html, nodes} = await h.render();
  assert.equal((html.match(/>GPT-6 Astra</g) || []).length, 1);
  assert.doesNotMatch(html, /压缩阈值占用|context-meter|72\.0%|216K/);
  assert.doesNotMatch(html, />1\.05M 上下文</);
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

test('model names occupy their own row and all original tags remain together below in both tabs', async () => {
  for (const tab of ['main', 'agent']) {
    const result = await harness({}, tab).render();
    const rows = result.nodes.filter(node => node.type === 'button' && hasClass(node, 'model-select'));
    for (const [index, row] of rows.entries()) {
      const content = walk(row.children);
      const title = content.find(node => hasClass(node, 'model-row-title-row'));
      assert.equal(walk(title.children).filter(node => hasClass(node, 'model-row-name')).length, 1);
      assert.equal(walk(title.children).some(node => hasClass(node, 'model-tag')), false);
      const details = content.find(node => hasClass(node, 'model-row-tags'));
      const tags = walk(details.children).filter(node => hasClass(node, 'model-tag'));
      const labels = tags.map(tag => walk(tag.children).find(child => child.type === 'span' && typeof child.children === 'string').children);
      assert.deepEqual(labels, index === 0
        ? ['300K 压缩', '128K 输出', '思考', 'Fast']
        : ['272K 压缩', '思考']);
      assert.ok(content.indexOf(title) < content.indexOf(details));
    }
  }
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

test("removed usage stays absent while unsupported capabilities and empty model search stay explicit", async () => {
  const h = harness({contextUsedDisplay: "待实测", contextPercentDisplay: "—", supportsThinking: false, fastSupported: false, modelGroups: []});
  const {html, buttons} = await h.render();
  assert.doesNotMatch(html, /待实测|压缩阈值占用/);
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

test('model property icons use official local Lucide SVGs with their license and theme-safe rendering', async () => {
  const iconUrl = new URL('./ModelFeatureIcon.vue', import.meta.url);
  const iconSource = fs.readFileSync(iconUrl, 'utf8');
  const {descriptor: icon} = parse(iconSource);
  const imports = [...icon.scriptSetup.content.matchAll(/import (\w+) from '([^']+\.svg)'/g)];
  const paths = Object.fromEntries(imports.map(([, name, path]) => [name, path]));
  assert.deepEqual(Object.fromEntries(Object.entries(paths).map(([name, path]) => [name, path.split('/').at(-1)])), {
    brain: 'brain.svg', zap: 'zap.svg', compression: 'file-archive.svg',
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
  const withoutRing = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<svg class="run-config-context-ring[^"]*"[\s\S]*?<\/svg>/, '');
  assert.match(withoutRing, /class="run-config-chip-meta"><span class="run-config-meta-part">极高<\/span><span class="run-config-meta-part run-config-meta-fast" title="Fast 模式"><span data-icon="zap"><\/span><\/span><span class="run-config-meta-part">216K \/ 300K<\/span><\/span>/);
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
    assert.doesNotMatch(result.html, /压缩阈值占用|上下文 216K|72\.0%/);
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
  assert.match(css, /\.model-search\s*\{[^}]*background: var\(--ob-chat-hover\);/, 'search uses the same quiet gray surface as the conversation tree');
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
  assert.match(html, /跟随：低/);
  assert.doesNotMatch(html, /<button[^>]*>默认<\/button>/);
  assert.ok(tooltips.includes('跟随主会话思考强度，当前为低'));
  assert.match(h.bindings.runConfigSettingsSummary, /^思考 跟随/);
});

test('Agent manual thinking hint says it is not following the main conversation', async () => {
  const h = harness({agentThinkLevel: 'low', agentEffectiveThinking: 'low'}, 'agent');
  const {html} = await h.render();
  assert.match(html, /未跟随主会话/);
  assert.doesNotMatch(html, /跟随：/);
});

test('main tab keeps model-default wording for main thinking', async () => {
  const h = harness({}, 'main');
  const {html} = await h.render();
  assert.match(html, /默认 极高/);
  assert.doesNotMatch(html, /跟随主会话思考强度/);
});

const viewSource = fs.readFileSync(new URL('./ConsoleView.vue', import.meta.url), 'utf8');
function viewSection(start, end) {
  const from = viewSource.indexOf(start);
  const to = viewSource.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `ConsoleView section: ${start}`);
  return viewSource.slice(from, to);
}

// Exercise the real local defaults -> state rebuild -> computed props -> Composer path.
// The stale state deliberately differs from local refs until reset assigns its new object.
function localConfigHarness({mainLevels = ['off', 'low', 'medium', 'high'], agentLevels = ['low', 'medium']} = {}) {
  const context = vm.createContext({
    computed, ref, modelDefaultThinking, modelThinkingLevels, thinkingLabel,
    props: {conversationUuid: 'local:new'}, isLocalConversation: ref(true),
    chatState: ref({model: 'old/model', effectiveThinkingLevel: 'xhigh'}),
    localModel: ref(''), localThinking: ref(''), localFast: ref(false),
    localAgentModel: ref(''), localAgentThinking: ref(''), localAgentFast: ref(null),
    localContextStrategy: ref('sliding_window'), DEFAULT_NEW_CONVERSATION_THINKING: 'high',
    currentPrimaryModelKey: ref('test/main'), primaryModelKey: ref('test/main'),
    modelOptions: ref([
      {key: 'test/main', thinkingLevels: mainLevels, defaultThinkingLevel: mainLevels.length ? 'high' : ''},
      {key: 'test/agent', thinkingLevels: agentLevels, defaultThinkingLevel: agentLevels.length ? 'low' : ''},
    ]),
    pinnedActiveTurnIndex: null, readingAnchor: null, activeTurnIndex: ref(0),
    messages: ref([]), lastStats: ref(null), running: ref(false), foregroundRunning: ref(false),
    rootTurnRunning: ref(false), runStartedAt: ref(0), status: ref(''),
    closeWs() {}, clearUiCaches() {}, resetOperationStore() {}, clearActiveRun() {},
    normalizeLedgerUsageBaseline: value => value,
  });
  vm.runInContext([
    'const displayedRunConfig = computed(() => chatState.value);',
    viewSection('const currentModel = computed(', 'const agentFastSupported = computed('),
    viewSection('function primaryModelInfo()', 'async function loadLocalRunDefaults('),
    viewSection('function resetLocalConversationState(', 'function detailKey('),
    viewSection('function buildLocalAgentRunConfig(', 'async function saveAgentRunConfig('),
  ].join('\n'), context);
  return vm.runInContext(`({chatState, localThinking, effectiveThinking, agentModel, agentThinkLevel,
    agentThinkLevels, agentSupportsThinking, agentEffectiveThinking, agentDefaultThinkingLabel,
    applyLocalRunDefaults, resetLocalConversationState, buildLocalAgentRunConfig, completeLocalRunConfig})`, context);
}

function localAgentMenu(local) {
  return harness({
    agentModel: local.agentModel.value, agentThinkLevel: local.agentThinkLevel.value,
    agentThinkLevels: local.agentThinkLevels.value, agentSupportsThinking: local.agentSupportsThinking.value,
    agentEffectiveThinking: local.agentEffectiveThinking.value,
    agentDefaultThinkingLabel: local.agentDefaultThinkingLabel.value,
    effectiveThinking: local.effectiveThinking.value,
  }, 'agent');
}

for (const [name, defaults, capabilities, expected, source, label] of [
  ['same model follows low instead of high default', {mainThinkingLevel: 'low'}, {}, 'low', 'main', '低'],
  ['different model follows supported medium', {mainThinkingLevel: 'medium', agentModel: 'test/agent'}, {}, 'medium', 'main', '中'],
  ['off remains a valid inherited value', {mainThinkingLevel: 'off'}, {}, 'off', 'main', '关闭'],
  ['unsupported high falls back to Agent model default', {mainThinkingLevel: 'high', agentModel: 'test/agent'}, {}, 'low', 'model_default', '低'],
  ['unsupported off also falls back to Agent model default', {mainThinkingLevel: 'off', agentModel: 'test/agent'}, {}, 'low', 'model_default', '低'],
  ['Agent with no thinking capability uses off', {mainThinkingLevel: 'high', agentModel: 'test/agent'}, {agentLevels: []}, 'off', 'model_default', '关闭'],
  ['main with no thinking capability supplies off', {mainThinkingLevel: 'high', agentModel: 'test/agent'}, {mainLevels: [], agentLevels: ['off', 'low']}, 'off', 'main', '关闭'],
  ['unset main thinking uses its effective model default', {agentModel: 'test/agent'}, {agentLevels: ['low', 'high']}, 'high', 'main', '高'],
]) {
  test(`local Agent preview: ${name}`, async () => {
    const local = localConfigHarness(capabilities);
    local.applyLocalRunDefaults(defaults);
    assert.equal(local.effectiveThinking.value, 'xhigh', 'chatState has not been rebuilt yet');
    local.resetLocalConversationState();
    const config = local.chatState.value.agentRunConfig;
    assert.equal(config.thinkLevel, '');
    assert.equal(config.effective.thinkLevel, expected);
    assert.equal(config.effective.source.thinkLevel, source);
    assert.equal(local.completeLocalRunConfig().agentThinkLevel, '', 'preview never persists a resolved override');
    const menu = localAgentMenu(local);
    const {html} = await menu.render();
    if (config.effective.supportsThinking) assert.ok(html.includes(`跟随：${label}`));
    else assert.match(html, /未声明支持/);
    assert.ok(menu.tooltips.includes(`跟随主会话思考强度，当前为${label}`));
  });
}

test('local Agent preview preserves explicit thinking, including off', async () => {
  for (const explicit of ['high', 'off']) {
    const local = localConfigHarness();
    local.applyLocalRunDefaults({mainThinkingLevel: 'low', agentThinkLevel: explicit});
    local.resetLocalConversationState();
    const config = local.chatState.value.agentRunConfig;
    assert.equal(config.thinkLevel, explicit);
    assert.equal(config.effective.thinkLevel, explicit);
    assert.equal(config.effective.source.thinkLevel, 'conversation');
    assert.match((await localAgentMenu(local).render()).html, /未跟随主会话/);
  }
});

test('local Agent follows newly edited refs before chatState catches up, including a model switch', () => {
  const local = localConfigHarness();
  local.applyLocalRunDefaults({mainThinkingLevel: 'low'});
  local.resetLocalConversationState();
  local.applyLocalRunDefaults({mainThinkingLevel: 'off'});
  assert.equal(local.effectiveThinking.value, 'low');
  assert.equal(local.buildLocalAgentRunConfig().effective.thinkLevel, 'off');
  local.resetLocalConversationState();
  assert.equal(local.agentEffectiveThinking.value, 'off');
  local.applyLocalRunDefaults({mainModel: 'test/agent', mainThinkingLevel: 'medium'});
  assert.equal(local.effectiveThinking.value, 'off');
  local.resetLocalConversationState();
  assert.equal(local.agentEffectiveThinking.value, 'medium');
  assert.equal(local.chatState.value.agentRunConfig.effective.model, 'test/agent');
});

for (const running of [false, true]) {
  test(`selecting Agent follow confirms main conversation${running ? ' for next new Agent' : ''}`, async () => {
    const notices = [];
    const patches = [];
    const context = vm.createContext({
      running: ref(running), activeConversationUuid: ref('conversation'), isLocalConversation: ref(false),
      runConfigSaves: {enqueue: async (_uuid, save) => {await save(); return {applied: true};}},
      Api: {conversationSetAgentRunConfig: async (_uuid, patch) => patches.push(patch)},
      isRunConfigInteractionCurrent: () => true,
      ElMessage: {success: text => notices.push(text), error: error => {throw error;}},
      apiError: error => error,
    });
    vm.runInContext(viewSection('async function saveAgentRunConfig(', 'async function selectAgentFast('), context);
    await vm.runInContext('selectAgentThinking("")', context);
    assert.equal(patches[0].thinkLevel, '');
    assert.deepEqual(notices, [`Agent 思考已跟随主会话${running ? '，下一次新 Agent 生效' : ''}`]);
  });
}
