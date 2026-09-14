import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {register} from 'node:module';
import postcss from 'postcss';
import {parse, compileScript, compileStyle, compileTemplate} from '@vue/compiler-sfc';
import {createRenderer, h, nextTick} from 'vue';

const files = ['AgentEventCard.vue', 'AgentActivityList.vue', 'AgentProcessActivity.vue', 'AgentPlanWorkspace.vue', 'ToolArgumentsView.vue', 'ConsoleToolEvent.vue', 'TurnWorkDetailPanel.vue', 'TurnEvent.vue', 'TurnList.vue'];
const sources = Object.fromEntries(files.map(f => [f, fs.readFileSync(new URL(f, import.meta.url), 'utf8')]));
const descriptors = Object.fromEntries(files.map(f => [f, parse(sources[f], {filename: f}).descriptor]));
const styles = Object.fromEntries(files.map(f => [f, postcss.parse(descriptors[f].styles.filter(s => s.scoped).map(s => s.content).join('\n'))]));

// Compile and execute the real SFC templates in Vue's in-memory renderer. No
// browser, layout engine, network, service, tool or Agent execution is involved.
// Markdown's DOM-patching/artifact lifecycle is outside this package; use its
// real markdown renderer in a DOM-free leaf instead. CSS is checked separately.
// module.register is available in the supported Node 20 runtime. Compile here
// and transfer source strings to its loader thread; Vue still runs in this test
// process. The node:test per-file process owns the loader's lifetime.
const compiledSources = Object.fromEntries(files.map(name => [name, compileScript(descriptors[name], {id: name, inlineTemplate: true}).content]));
compiledSources['ConsoleMarkdown.vue'] = `import {h} from 'vue'; import {renderMarkdown} from './markdown.js'; export default {props:['text'], render(){return h('div',{class:'bear-md',innerHTML:renderMarkdown(this.text || '')})}};`;
register(`data:text/javascript,${encodeURIComponent(`
  import {fileURLToPath} from 'node:url';
  let sources;
  export function initialize(data) { sources = data; }
  export function load(url, context, nextLoad) {
    if (url.endsWith('.css')) return {format: 'module', source: 'export default {};', shortCircuit: true};
    if (url.endsWith('.vue')) {
      const name = fileURLToPath(url).split('/').at(-1);
      return {format: 'module', source: sources[name] || 'export default {render(){return null}};', shortCircuit: true};
    }
    return nextLoad(url, context);
  }
`)}`, {parentURL: import.meta.url, data: compiledSources});
const components = Object.fromEntries(await Promise.all(files.map(async f => [f, (await import(new URL(f, import.meta.url))).default])));
const {Api} = await import('../../api.js');
const originals = {...Api}, unexpectedCalls = [];
for (const key of Object.keys(Api)) if (typeof Api[key] === 'function') Api[key] = () => { unexpectedCalls.push(key); throw new Error(`Unexpected API in presentation test: ${key}`); };
after(() => {Object.assign(Api, originals); assert.deepEqual(unexpectedCalls, []);});
const {TOOL_DETAIL_CACHE_KEY, createToolDetailCache} = await import('./toolDetailCache.js');
const {agentRowArgumentsDisplay} = await import('./display.js');
const {escapeHtmlText, renderMarkdown} = await import('./markdown.js');

const node = (type, text = '') => ({type, text, props: {}, children: [], parent: null, scrollTop: 0, scrollHeight: 600, clientHeight: 100});
const renderer = createRenderer({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (n, text) => {n.text = text;}, setElementText: (n, text) => {n.text = text; n.children = [];},
  patchProp: (n, key, old, value) => {n.props[key] = value;},
  insert(n, parent, anchor = null) { if (n.parent) this.remove(n); n.parent = parent; const i = parent.children.indexOf(anchor); parent.children.splice(i < 0 ? parent.children.length : i, 0, n); },
  remove(n) {const i = n.parent?.children.indexOf(n) ?? -1; if (i >= 0) n.parent.children.splice(i, 1); n.parent = null;},
  parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
});
const walk = n => [n, ...n.children.flatMap(walk)];
const hasClass = (n, cls) => String(n.props.class || '').split(/\s+/).includes(cls);
const find = (root, cls) => walk(root).find(n => hasClass(n, cls));
const text = n => [n.text, n.props.innerHTML || '', ...n.children.map(text)].join('');
function mount(t, file, props, cache = null) {
  const root = node('root');
  const app = renderer.createApp({render: () => h(components[file], props)});
  if (cache) app.provide(TOOL_DETAIL_CACHE_KEY, cache);
  app.mount(root); t.after(() => app.unmount());
  return root;
}
const detailProps = {conversationUuid: '', turnId: 'fixture', index: 0, detailKey: (...parts) => parts.join(':'), isDetailOpen: () => true, activeToolResultIndex: () => 0, onDetailsToggle: () => {}};
const long = label => `${label}-START\n${'long payload 中文/'.repeat(3500)}\n${label}-END`;
function toolEvent(argumentsText, resultText, name = 'Bash') {return {kind: 'tool', calls: [{id: 'call-1', name, arguments: argumentsText}], result: {role: 'tool', name, toolCallId: 'call-1', content: resultText}};}

function mediaMatches(query, env) {return query.split(',').some(part => [...part.matchAll(/\(([^)]+)\)/g)].every(([, atom]) => {
  const [key, value] = atom.split(':').map(x => x.trim());
  if (key === 'max-width') return env.width <= parseFloat(value);
  if (key === 'min-width') return env.width >= parseFloat(value);
  return env[key] === value;
}));}
function css(file, selector, env) {
  const out = {};
  styles[file].walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let p = rule.parent; p; p = p.parent) {
      if (p.type === 'atrule' && p.name === 'media' && !mediaMatches(p.params, env)) return;
      if (p.type === 'atrule' && p.name === 'container' && env.cardWidth > parseFloat(p.params.match(/max-width:\s*([\d.]+)/)[1])) return;
    }
    rule.walkDecls(d => {out[d.prop] = d.value;});
  }); return out;
}
const phone = {width: 390, height: 844, mobileHeight: 844, hover: 'none', pointer: 'coarse', cardWidth: 340};
const desktop = {width: 1440, height: 900, mobileHeight: 900, hover: 'hover', pointer: 'fine', cardWidth: 850};
function px(value, env, percentBase = env.width) {
  const expr = value.replace(/var\(--mobile-viewport-height,\s*100dvh\)/g, `${env.mobileHeight}px`)
    .replace(/([\d.]+)(px|rem|dvh|vh|vw|%)/g, (_, n, unit) => Number(n) * ({px: 1, rem: 16, dvh: env.height / 100, vh: env.height / 100, vw: env.width / 100, '%': percentBase / 100}[unit]))
    .replace(/\bcalc\(/g, '(').replace(/\bmin\(/g, 'Math.min(').replace(/\bmax\(/g, 'Math.max(').replace(/\bclamp\(/g, 'clamp(');
  assert.match(expr, /^[\d\s.,()*+\-/a-zA-Z]+$/);
  return new Function('clamp', `return ${expr}`)((lo, v, hi) => Math.max(lo, Math.min(v, hi)));
}

test('all affected SFC scripts/templates and scoped CSS compile, including container queries', () => {
  for (const f of files) {
    assert.deepEqual(compileTemplate({source: descriptors[f].template.content, filename: f, id: f}).errors, [], f);
    for (const s of descriptors[f].styles) assert.deepEqual(compileStyle({source: s.content, filename: f, id: f, scoped: s.scoped}).errors, [], f);
  }
});

test('full long tool arguments/results survive both conversation TurnEvent and work detail paths', async t => {
  const args = JSON.stringify({command: long('ARGS'), description: long('DESCRIPTION')});
  const event = toolEvent(args, long('RESULT'));
  const root = mount(t, 'TurnEvent.vue', {...detailProps, event});
  assert.ok(text(root).includes('ARGS-END')); assert.ok(text(root).includes('RESULT-END'));
  const codes = walk(root).filter(n => n.type === 'code' && n.props.innerHTML);
  assert.equal(codes[0].props.innerHTML, escapeHtmlText(JSON.stringify(JSON.parse(args), null, 2)));
  assert.equal(codes[1].props.innerHTML, escapeHtmlText(long('RESULT')));
  assert.equal(walk(root).filter(n => hasClass(n, 'tool-payload-code')).length, 2);
  const work = mount(t, 'TurnWorkDetailPanel.vue', {...detailProps, open: true, turnIndex: 0, turn: {id: 'turn', events: [event]}});
  assert.ok(text(work).includes('ARGS-END')); assert.ok(text(work).includes('RESULT-END'));
  assert.ok(find(work, 'work-detail-close'));
  assert.equal(find(work, 'work-detail-body').props.tabindex, '0');
  assert.match(sources['TurnList.vue'], /<TurnEvent[\s\S]*?:event="entry\.event"/);
});

test('native disclosure, result selection, copying and close relay use the actual Vue handlers', async t => {
  let toggle, selected, closed = 0;
  const event = toolEvent('{"command":"alpha"}', 'alpha-result');
  event.calls.push({id: 'call-2', name: 'Read', arguments: '{"path":"beta"}'});
  const root = mount(t, 'ConsoleToolEvent.vue', {event, open: true, activeIndex: 0, onToggle: e => {toggle = e;}, onSelectTab: n => {selected = n;}});
  const e = {target: {open: false}};
  find(root, 'tool-event').props.onToggle(e); assert.equal(toggle, e);
  find(root, 'tool-call-selector').children.filter(n => n.type === 'button')[1].props.onClick(); assert.equal(selected, 1);
  const work = mount(t, 'TurnWorkDetailPanel.vue', {...detailProps, open: true, turn: {events: []}, onClose: () => {closed++;}});
  find(work, 'work-detail-close').props.onClick(); assert.equal(closed, 1);
  let copied = '';
  const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator'), oldWindow = globalThis.window, oldSecure = globalThis.isSecureContext;
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {clipboard: {writeText: async v => {copied = v;}}}});
  globalThis.isSecureContext = true; globalThis.window = {setTimeout: () => 0, clearTimeout: () => {}};
  try {
    await find(root, 'tool-payload-copy').props.onClick({stopPropagation() {}});
    assert.equal(JSON.parse(copied).command, 'alpha');
  } finally {
    if (oldNavigator) Object.defineProperty(globalThis, 'navigator', oldNavigator);
    else delete globalThis.navigator;
    globalThis.window = oldWindow; globalThis.isSecureContext = oldSecure;
  }
});

test('Agent process retains >12000 characters in parameters, recorded results, failures and non-collapsible model descriptions', t => {
  const raw = JSON.stringify({path: '/fixture/file', content: long('WRITE'), description: long('LOG')});
  const root = mount(t, 'AgentProcessActivity.vue', {sourceLines: [
    {key: 'a', seq: 1, kind: 'tool_call_started', detail: {name: 'Write', arguments: raw, description: long('LOG')}},
    {key: 'b', seq: 2, kind: 'tool_call_finished', detail: {name: 'Write', resultPreview: long('PREVIEW')}},
    {key: 'c', seq: 3, kind: 'model_call_retry', detail: {retry: {attempt: 1, maxRetries: 3, summary: long('RETRY')}}},
    {key: 'd', seq: 4, kind: 'tool_call_failed', detail: {name: 'Bash', error: long('ERROR')}},
  ]});
  for (const end of ['WRITE-END', 'LOG-END', 'PREVIEW-END', 'RETRY-END', 'ERROR-END']) assert.ok(text(root).includes(end), end);
  assert.ok(text(root).includes('结果（已有预览）'));
  assert.doesNotMatch(text(root), /界面预览已截断/);
});

test('Agent result tab renders every supplied character beyond the old 24000 cap, without inventing missing segments', async t => {
  const report = long('REPORT');
  const event = {kind: 'tool', calls: [{name: 'Agent', arguments: '{}'}], livePayload: {results: [{ok: true, task: {status: 'completed', title: 'fixture'}, result: {output: {summary: report, segmented: true, segmentCount: 4, originalChars: 200000}}}]}};
  const root = mount(t, 'AgentEventCard.vue', {...detailProps, event});
  const outputTab = walk(root).find(n => n.type === 'button' && text(n).startsWith('结果'));
  assert.ok(outputTab); outputTab.props.onClick(); await nextTick();
  assert.equal(find(root, 'agent-output').props.innerHTML, renderMarkdown(report));
  assert.ok(text(find(root, 'agent-output')).includes('REPORT-END'));
  assert.ok(text(root).includes('当前展示摘要和首段'), 'server-segmented limitation remains visible');
  assert.doesNotMatch(text(root), /前端预览已截断/);
});

test('compaction uses full supplied summary, never manufactures absent output', async t => {
  const args = JSON.stringify({name: 'ContextCompaction', strategy: 'model_summary', scope: 'root', compactedOutput: long('COMPACTION')});
  const event = toolEvent(args, '', 'ContextCompaction');
  const root = mount(t, 'TurnWorkDetailPanel.vue', {...detailProps, open: true, turn: {events: [event]}});
  assert.ok(text(root).includes('COMPACTION-END'));
  const missing = mount(t, 'ConsoleToolEvent.vue', {event: toolEvent('{"strategy":"model_summary","scope":"root"}', '', 'ContextCompaction'), open: true});
  assert.ok(text(missing).includes('摘要正文未记录'));
});

test('opening a referenced compaction replaces its preview with the complete loaded summary', async t => {
  const previous = Api.conversationCompaction;
  let requests = 0;
  Api.conversationCompaction = async (conversation, summary) => {
    requests++; assert.equal(conversation, 'fixture-conversation'); assert.equal(summary, '123');
    return {ok: true, compactedOutput: long('LOADED-COMPACTION')};
  };
  try {
    const event = toolEvent(JSON.stringify({strategy: 'model_summary', scope: 'root', summaryId: '123', summaryRef: 'fixture-summary', outputPreview: 'only preview'}), 'only preview', 'ContextCompaction');
    const root = mount(t, 'ConsoleToolEvent.vue', {event, open: true, conversationUuid: 'fixture-conversation'});
    await new Promise(resolve => setImmediate(resolve)); await nextTick();
    assert.equal(requests, 1);
    const code = walk(find(root, 'tool-payload-code')).find(n => n.type === 'code');
    assert.equal(code.props.innerHTML, escapeHtmlText(long('LOADED-COMPACTION')));
  } finally {Api.conversationCompaction = previous;}
});

test('launch fallback keeps full >5000-character task while the helper default still serves compact previews', async t => {
  const prompt = long('PROMPT');
  const event = {kind: 'tool', calls: [{name: 'Agent', arguments: JSON.stringify({items: [{workerType: 'fixture', prompt}]})}]};
  const root = mount(t, 'AgentEventCard.vue', {...detailProps, event});
  const tab = walk(root).find(n => n.type === 'button' && text(n).startsWith('启动信息'));
  assert.ok(tab); tab.props.onClick(); await nextTick();
  assert.ok(text(find(root, 'launch-content-frame')).includes(prompt));
  assert.doesNotMatch(text(find(root, 'launch-content-frame')), /前端预览已截断/);
  const row = {argItem: {prompt}};
  assert.match(agentRowArgumentsDisplay(event, row), /前端预览已截断/);
  assert.ok(agentRowArgumentsDisplay(event, row, 0, {full: true}).includes(prompt));
});

test('opening both paths shares the existing lazy detail cache and exposes the full loaded tail', async t => {
  const cache = createToolDetailCache();
  const previous = Api.conversationOperationDetail;
  let requests = 0;
  Api.conversationOperationDetail = async (conversation, operation) => {
    requests++; assert.equal(conversation, 'fixture-conversation'); assert.equal(operation, 'fixture-operation');
    return {ok: true, operation: {opId: operation, revision: 3, payload: {name: 'Bash', arguments: JSON.stringify({command: long('LOADED-ARGS')}), result: long('LOADED-RESULT')}}};
  };
  try {
    const event = {...toolEvent('{"command":"preview"}', 'result-preview'), operation: {opId: 'fixture-operation', revision: 3, detailAvailable: true}};
    const conversation = mount(t, 'TurnEvent.vue', {...detailProps, conversationUuid: 'fixture-conversation', event}, cache);
    const work = mount(t, 'TurnWorkDetailPanel.vue', {...detailProps, conversationUuid: 'fixture-conversation', open: true, turn: {events: [event]}}, cache);
    await new Promise(resolve => setImmediate(resolve));
    await nextTick();
    assert.equal(requests, 1);
    for (const root of [conversation, work]) {assert.ok(text(root).includes('LOADED-ARGS-END')); assert.ok(text(root).includes('LOADED-RESULT-END'));}
  } finally {Api.conversationOperationDetail = previous;}
});

test('Plan method expand and step selection keep complete existing text and final outputs', async t => {
  const plan = {title: 'Plan', objective: long('PLAN'), steps: [
    {id: 'one', title: 'First', objective: long('OBJECTIVE'), method: long('METHOD'), required: true, criteria: []},
    {id: 'two', title: 'Second', objective: 'second objective', method: long('SECOND'), required: true, criteria: []},
  ], finalOutputs: [{id: 'final', title: 'Final', description: long('FINAL')}]};
  const current = {version: 1, status: 'approved', plan};
  const root = mount(t, 'AgentPlanWorkspace.vue', {data: {task: {status: 'running'}, state: {phase: 'executing', active_plan_version: 1, current_step_id: 'one'}, current, versions: [current]}});
  await nextTick(); await nextTick();
  assert.ok(text(root).includes('METHOD-END')); assert.ok(text(root).includes('FINAL-END'));
  const expand = find(root, 'method-toggle'); assert.ok(expand); expand.props.onClick(); await nextTick();
  assert.ok(hasClass(find(root, 'method-text'), 'is-expanded'));
  walk(root).filter(n => hasClass(n, 'step-button'))[1].props.onClick(); await nextTick();
  assert.ok(text(root).includes('SECOND-END'));
  assert.equal(css('AgentPlanWorkspace.vue', '.plan-touch-details', phone).display, 'block');
  assert.equal(css('AgentPlanWorkspace.vue', '.plan-touch-details', desktop).display, 'none');
});

test('mobile category picker exposes the same tabs, overview toggle retains the exact reading node and position', async t => {
  const event = {kind: 'tool', calls: [{name: 'Agent', arguments: JSON.stringify({items: [{workerType: 'fixture', prompt: long('MOBILE-LAUNCH')}]})}]};
  const root = mount(t, 'AgentEventCard.vue', {...detailProps, event});
  const toolbar = find(root, 'agent-mobile-toolbar');
  const picker = walk(toolbar).find(n => n.type === 'select');
  const options = walk(picker).filter(n => n.type === 'option');
  const desktopTabs = walk(find(root, 'agent-tabs')).filter(n => n.type === 'button');
  assert.equal(options.length, desktopTabs.length);
  assert.ok(options.some(n => n.props.value === 'launch'));
  const toggle = walk(toolbar).find(n => n.type === 'button');
  picker.props.onChange({target: {value: 'activity'}}); await nextTick(); await nextTick();
  const scroll = find(root, 'activity-scroll'); assert.ok(scroll); scroll.scrollTop = 27;
  scroll.props.onPointerdown({currentTarget: scroll});
  scroll.props.onScrollPassive({currentTarget: scroll});
  assert.equal(toggle.props['aria-expanded'], false);
  toggle.props.onClick(); await nextTick();
  assert.equal(toggle.props['aria-expanded'], true); assert.ok(hasClass(find(root, 'agent-tool-detail'), 'mobile-meta-open'));
  assert.equal(find(root, 'activity-scroll'), scroll); assert.equal(scroll.scrollTop, 27);
  toggle.props.onClick(); await nextTick();
  assert.equal(find(root, 'activity-scroll'), scroll); assert.equal(scroll.scrollTop, 27);
  toggle.props.onClick(); await nextTick();
  picker.props.onChange({target: {value: 'launch'}}); await nextTick();
  assert.equal(toggle.props['aria-expanded'], false);
  assert.ok(hasClass(find(root, 'agent-tab-panel'), 'tab-launch'));
  assert.ok(text(find(root, 'launch-content-frame')).includes('MOBILE-LAUNCH-END'));
  assert.equal(css('AgentEventCard.vue', '.agent-mobile-toolbar', desktop).display, 'none');
  assert.equal(css('AgentEventCard.vue', '.mobile-meta-open .agent-tab-panel', desktop).display, undefined);
});

test('phone timeline gives width back to prose and only hides a full-description exact duplicate', t => {
  const root = mount(t, 'AgentActivityList.vue', {lines: [
    {key:'a',kind:'tool_call_started',toolName:'Read',toolDescription:'short display',description:long('DIFFERENT-DESCRIPTION')},
    {key:'b',kind:'tool_call_started',toolName:'Read',toolDescription:long('SAME-DESCRIPTION'),description:long('SAME-DESCRIPTION')},
  ]});
  const descriptions = walk(root).filter(n => hasClass(n, 'activity-tool-full-description'));
  assert.equal(descriptions.length, 2);
  assert.equal(hasClass(descriptions[0], 'is-summary-copy'), false);
  assert.equal(hasClass(descriptions[1], 'is-summary-copy'), true);
  assert.ok(text(root).includes('DIFFERENT-DESCRIPTION-END'));
  assert.equal(css('AgentActivityList.vue', '.activity-tool-description', phone)['white-space'], 'normal');
  assert.equal(css('AgentActivityList.vue', '.activity-tool-description', phone).overflow, 'visible');
  assert.equal(css('AgentActivityList.vue', '.activity-row', phone)['grid-template-columns'], '8px minmax(0, 1fr)');
  assert.equal(css('AgentActivityList.vue', '.activity-row time', phone)['grid-row'], '1');
  assert.equal(css('AgentActivityList.vue', '.activity-tool-call', phone)['grid-row'], '2');
  assert.equal(css('AgentActivityList.vue', '.activity-row', desktop)['grid-template-columns'], '48px 12px minmax(0, 1fr)');
});

test('whole mobile Agent/tool cards and each payload have viewport-aware bounded scroll, including keyboard and landscape', () => {
  for (const env of [phone, {...phone, width: 320, mobileHeight: 320}, {...phone, width: 430, mobileHeight: 280}, {...phone, width: 844, mobileHeight: 280}]) {
    for (const [f, sel] of [['AgentEventCard.vue', '.agent-tool-detail'], ['ConsoleToolEvent.vue', '.tool-detail']]) {
      const s = css(f, sel, env); assert.equal(s.overflow, f === 'AgentEventCard.vue' ? 'hidden' : 'auto'); assert.equal(s['box-sizing'], 'border-box');
      const height = px(s['max-height'], env); assert.ok(height <= env.mobileHeight * .65 + .01); assert.ok(height >= 180);
    }
    const tab = css('AgentEventCard.vue', '.agent-tab-panel', env);
    assert.equal(tab.height, 'auto'); assert.equal(tab.flex, '1 1 0'); assert.equal(tab['min-height'], '0');
    const frame = css('AgentEventCard.vue', '.agent-tool-detail', env);
    const stageHeight = px(frame.height, env);
    assert.ok(stageHeight <= env.mobileHeight * .65);
    assert.ok(stageHeight - 48 - stageHeight * .3 - 18 >= 60, 'short viewport still leaves a positive reading area after toolbar, bounded title and padding');
    assert.equal(css('AgentEventCard.vue', '.activity-scroll', env)['min-height'], '0');
    assert.equal(css('AgentEventCard.vue', '.activity-scroll', env)['overflow-y'], 'auto');
    assert.equal(css('AgentEventCard.vue', '.content-frame-scroll', env).overflow, 'visible');
    assert.equal(css('AgentEventCard.vue', '.instance-assignment-list', env).overflow, 'visible');
    assert.equal(css('AgentEventCard.vue', '.agent-mobile-toolbar', env).flex, '0 0 auto');
    assert.equal(css('AgentPlanWorkspace.vue', '.plan-state-empty', env).overflow, 'auto');
    for (const [f, sel] of [['ConsoleToolEvent.vue', '.tool-payload-code'], ['ToolArgumentsView.vue', '.tool-argument-block pre'], ['AgentActivityList.vue', '.activity-compaction-body']]) {
      const s = css(f, sel, env); assert.equal(s.overflow, 'auto'); assert.ok(px(s['max-height'], env) <= env.mobileHeight * .35 + .01);
    }
  }
  assert.equal(css('AgentEventCard.vue', '.agent-tab-panel', desktop).height, '450px');
  assert.equal(css('AgentEventCard.vue', '.agent-tool-detail', desktop)['max-height'], undefined);
  assert.equal(css('AgentEventCard.vue', '.agent-tool-detail', desktop).padding, '14px');
  assert.equal(css('AgentEventCard.vue', '.agent-title', desktop)['font-size'], '14px');
});

test('framed grid payloads shrink to available width; four borders remain and compaction code actually wraps', () => {
  for (const env of [phone, {...desktop, width: 820, cardWidth: 300}, desktop]) {
    const detail = css('ConsoleToolEvent.vue', '.tool-detail', env), code = css('ConsoleToolEvent.vue', '.tool-payload-code', env);
    assert.equal(detail['grid-template-columns'], 'minmax(0, 1fr)'); assert.match(detail.border, /^1px solid/);
    assert.equal(code['min-width'], '0'); assert.equal(code['max-width'], '100%'); assert.equal(code['box-sizing'], 'border-box'); assert.match(code.border, /^1px solid/);
    assert.equal(css('ConsoleToolEvent.vue', '.context-detail-section .tool-payload-code code.hljs', env)['min-width'], '0');
    assert.equal(css('ConsoleToolEvent.vue', '.context-detail-section .tool-payload-code code.hljs', env)['white-space'], 'pre-wrap');
  }
});

test('work surface follows its parent; phone covers composer, narrow desktop reserves it, close stays outside body scroll', () => {
  for (const env of [phone, {...phone, width: 320, mobileHeight: 320}, {...desktop, width: 820, cardWidth: 300}, desktop]) {
    const pane = css('TurnWorkDetailPanel.vue', '.work-detail', env), opened = css('TurnWorkDetailPanel.vue', '.work-detail.open', env), surface = css('TurnWorkDetailPanel.vue', '.work-detail-surface', env);
    const workspaceWidth = env.width > 760 ? env.width - 300 : env.width;
    const paneWidth = px(opened.width, env, workspaceWidth), surfaceWidth = px(surface.width, env, paneWidth - 1);
    assert.ok(surfaceWidth + 1 <= paneWidth + .01, 'surface including left border stays inside parent, not 88vw');
    assert.equal(pane['box-sizing'], 'border-box'); assert.equal(surface['min-height'], '0');
    if (env.width <= 760) {
      assert.equal(pane.height, 'auto'); assert.equal(pane.inset, '0'); assert.equal(opened.width, '100%');
      assert.equal(pane['z-index'], '50');
      assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-entry', env).display, 'block');
    } else if (env.width <= 1280) {assert.equal(pane.height, 'auto'); assert.equal(pane.inset, '0 0 var(--console-composer-height, 135px) auto');}
    const body = css('TurnWorkDetailPanel.vue', '.work-detail-body', env); assert.equal(body.overflow, 'auto'); assert.equal(body['min-height'], '0');
    assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-header', env).flex, '0 0 auto');
  }
  const template = descriptors['TurnWorkDetailPanel.vue'].template.content;
  assert.ok(template.indexOf('work-detail-close') < template.indexOf('work-detail-body'));
  assert.ok(template.indexOf('work-detail-turn-preview') > template.indexOf('work-detail-body'), 'phone context preview scrolls; the 44px close row does not');
  assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-turn-preview', phone).display, 'block');
  assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-turn-preview', desktop).display, 'none');
  assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-close', phone).height, '44px');
  assert.match(fs.readFileSync(new URL('./ConsoleView.vue', import.meta.url), 'utf8'), /'--console-composer-height': `\$\{composerHeight\}px`/);
  const composer = parse(fs.readFileSync(new URL('./ConsoleComposer.vue', import.meta.url), 'utf8')).descriptor;
  const composerCss = postcss.parse(composer.styles.map(s => s.content).join('\n'));
  let composerLayer = 0;
  composerCss.walkDecls('z-index', d => { if (Number.isFinite(Number(d.value))) composerLayer = Math.max(composerLayer, Number(d.value)); });
  assert.ok(composerLayer > 0 && composerLayer < Number(css('TurnWorkDetailPanel.vue', '.work-detail', phone)['z-index']), 'phone detail is above the actual composer stacking layer');
});

test('narrow Plan cards scroll as one column, while wide desktop keeps original two columns and text expansion', () => {
  assert.equal(css('AgentPlanWorkspace.vue', '.plan-overview', desktop).display, 'grid');
  assert.equal(css('AgentPlanWorkspace.vue', '.plan-overview', desktop)['grid-template-columns'], '236px minmax(0, 1fr)');
  for (const env of [phone, {...desktop, cardWidth: 320}]) {
    assert.equal(css('AgentPlanWorkspace.vue', '.plan-overview', env).display, 'block');
    assert.equal(css('AgentPlanWorkspace.vue', '.plan-overview', env).overflow, 'auto');
  }
  assert.equal(css('AgentPlanWorkspace.vue', '.method-text.is-expanded', desktop)['-webkit-line-clamp'], 'unset');
  assert.equal(css('AgentActivityList.vue', '.activity-model-description', desktop)['white-space'], 'normal');
  assert.equal(css('AgentActivityList.vue', '.activity-tool-call[open] > summary .activity-tool-description', phone)['white-space'], 'normal');
});
