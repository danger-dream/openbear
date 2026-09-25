import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parse, compileScript, compileTemplate, compileStyle} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import postcss from 'postcss';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {ElEmpty} from 'element-plus';
import {observeAdminPhone, ADMIN_PHONE_QUERY} from '../adminViewport.js';

const names = ['Memory', 'Secrets', 'Docs', 'Template', 'RathAgents', 'Settings', 'Logs', 'Skills', 'Mcp'];
const sfcs = Object.fromEntries(names.map(name => [name, parse(fs.readFileSync(new URL(`${name}View.vue`, import.meta.url), 'utf8')).descriptor]));
const shared = postcss.parse(fs.readFileSync(new URL('../admin-mobile.css', import.meta.url), 'utf8'));
const walk = nodes => (nodes || []).flatMap(n => [n, ...walk(Array.isArray(n.children) ? n.children : [])]);
const nodes = name => walk(baseParse(sfcs[name].template.content).children);
const attr = (node, name) => node.props?.find(p => p.type === 6 && p.name === name)?.value?.content;
const hasClass = (node, name) => attr(node, 'class')?.split(/\s+/).includes(name);
const findClass = (name, cls) => nodes(name).find(n => hasClass(n, cls));
function css(selector, width = 390, root = shared) {
  const result = {}, priorities = {};
  root.walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let p = rule.parent; p; p = p.parent) if (p.type === 'atrule' && p.name === 'media') {
      const max = p.params.match(/max-width:\s*(\d+)px/), min = p.params.match(/min-width:\s*(\d+)px/);
      if ((max && width > Number(max[1])) || (min && width < Number(min[1]))) return;
    }
    rule.walkDecls(d => {
      if (!priorities[d.prop] || d.important) { result[d.prop] = d.value; priorities[d.prop] = d.important; }
    });
  });
  return result;
}
async function render(markup, scope) {
  let tree;
  const compiled = Vue.compile(markup);
  const app = Vue.createSSRApp({render() { tree = compiled.call(this, scope, []); return tree; }});
  app.config.warnHandler = message => assert.fail(message);
  app.directive('loading', {getSSRProps: () => ({})});
  app.component('ElEmpty', ElEmpty);
  const html = await renderToString(app);
  return {html, nodes: walk([tree])};
}

test('all nine management SFC scripts, templates and scoped styles compile', () => {
  for (const name of names) {
    const descriptor = sfcs[name];
    const script = compileScript(descriptor, {id: `test-${name}`});
    const result = compileTemplate({source: descriptor.template.content, filename: `${name}View.vue`, id: `test-${name}`, compilerOptions: {bindingMetadata: script.bindings}});
    assert.deepEqual(result.errors, [], name);
    for (const style of descriptor.styles) assert.deepEqual(compileStyle({source: style.content, filename: name, id: `test-${name}`, scoped: style.scoped}).errors, []);
  }
});

test('phone geometry is opt-in and new CSS leaves desktop typography/layout unchanged', () => {
  shared.walkRules(rule => {
    if (rule.selector === '.admin-mobile-only') return;
    assert.equal(rule.parent.type, 'atrule', rule.selector);
    assert.equal(rule.parent.name, 'media');
    assert.ok(['(max-width: 760px)', '(min-width: 761px)'].includes(rule.parent.params));
  });
  for (const name of names.filter(name => name !== 'Settings')) assert.ok(findClass(name, 'admin-page'), name);
  for (const width of [761, 1024, 1440]) {
    for (const selector of ['.admin-page', '.admin-page .admin-list', '.admin-dialog.el-dialog', '.admin-drawer.el-drawer', '.template-page .template-editor-pane', '.admin-page .doc-card']) assert.deepEqual(css(selector, width), {});
    assert.equal(css('.admin-mobile-only', width).display, 'none');
  }
  shared.walkDecls(d => assert.ok(!['zoom', 'transform', 'font-family'].includes(d.prop)));
});

test('320–760px controls stay outside a bounded list scroller; summary tiles are replaced by one compact line', () => {
  for (const width of [320, 360, 390, 430, 640, 720, 760]) {
    assert.equal(css('.admin-page', width).display, 'flex');
    assert.equal(css('.admin-page', width).overflow, 'hidden');
    assert.equal(css('.admin-page .admin-list', width)['overflow-y'], 'auto');
    assert.equal(css('.admin-page .admin-list', width).flex, '1 1 0%');
    assert.equal(css('.admin-page > header:not(.admin-page-header)', width).flex, 'none');
    assert.equal(css('.admin-summary > summary', width)['min-height'], '28px');
    assert.equal(css('.admin-page .admin-batch', width)['flex-wrap'], 'nowrap');
    assert.equal(css('.admin-page .admin-filters', width).flex, 'none');
    assert.equal(css('.admin-page .admin-heading', width).display, 'none');
    assert.equal(css('.admin-page .admin-card-actions', width).width, '100%');
  }
  for (const name of ['RathAgents', 'Logs', 'Skills', 'Mcp']) {
    assert.ok(hasClass(findClass(name, 'admin-stats'), 'admin-desktop-only'));
    assert.ok(nodes(name).some(n => n.tag === 'MobileAdminSummary'));
  }
  for (const name of names.filter(n => !['Settings', 'Template'].includes(n))) {
    const root = findClass(name, 'admin-page'), list = findClass(name, 'admin-list');
    assert.ok(root.children.some(n => hasClass(n, 'admin-list')), name);
    for (const control of nodes(name).filter(n => ['header', 'AdminPageHeader'].includes(n.tag) || hasClass(n, 'admin-filters') || hasClass(n, 'memory-categories'))) assert.ok(!walk(list.children).some(n => n.loc?.start.offset === control.loc.start.offset), `${name} control must not scroll with the list`);
  }
});

test('every management dialog/drawer is explicitly bounded, including teleported settings preview', () => {
  for (const name of names) for (const node of nodes(name).filter(n => ['el-dialog', 'el-drawer'].includes(n.tag))) {
    assert.ok(hasClass(node, node.tag === 'el-dialog' ? 'admin-dialog' : 'admin-drawer'), name);
    assert.ok(node.props.some(p => p.name === 'append-to-body'), `${name}: viewport offsets must not be doubled inside the translated app shell`);
  }
  for (const width of [320, 390, 760]) {
    assert.equal(css('.admin-dialog.el-dialog', width).width, '100%');
    assert.match(css('.admin-dialog.el-dialog', width)['max-height'], /--mobile-viewport-height/);
    assert.match(css('.admin-drawer.el-drawer', width).height, /--mobile-viewport-height/);
    assert.equal(css('.admin-drawer.el-drawer', width).width, '100%');
    assert.equal(css('.admin-dialog .el-dialog__body', width)['overflow-y'], 'auto');
    assert.equal(css('.admin-dialog .el-dialog__footer', width).flex, 'none');
    assert.equal(css('.admin-dialog .asset-edit-body', width).height, 'auto');
    assert.equal(css('.admin-dialog .asset-editor', width)['min-height'], '0');
    assert.equal(css('.admin-dialog .asset-editor', width).height, 'auto');
    assert.equal(css('.admin-dialog .asset-form-grid', width)['grid-template-columns'], 'repeat(2, minmax(0, 1fr))');
    assert.equal(css('.admin-dialog .el-dialog__footer', width)['flex-wrap'], 'nowrap');
    assert.equal(css('.admin-dialog .asset-footer > div:last-child', width)['flex-wrap'], 'nowrap');
    assert.equal(css('.admin-dialog .el-dialog__footer .asset-footer-time', width).display, 'none');
    assert.equal(css('.admin-dialog .el-dialog__footer .el-button', width).flex, 'none');
    assert.equal(css('.secret-kv-edit > :nth-child(2)', width)['grid-area'], '2 / 1 / 3 / -1');
  }
});

test('asset drag always scrolls the actual list, never the now-fixed outer page', () => {
  for (const name of ['Memory', 'Secrets', 'Docs']) {
    assert.equal(attr(findClass(name, 'admin-list'), 'ref'), 'scrollContainer');
    for (const node of nodes(name).filter(n => n.tag === 'draggable')) {
      const expression = node.props.find(p => p.name === 'bind' && p.arg?.content === 'scroll').exp.content;
      assert.equal(expression, 'scrollContainer');
      assert.ok(['.group-handle', '.drag-handle'].includes(attr(node, 'handle')));
    }
  }
  assert.equal(css('.memory-card > .drag-handle')['grid-area'], '1 / 3');
  assert.equal(css('.memory-card .memory-copy')['grid-area'], '1 / 2');
  assert.equal(css('.doc-card > .drag-handle')['grid-area'], '1 / 4');
  assert.equal(css('.doc-card .doc-copy')['grid-area'], '1 / 3');
  assert.equal(css('.admin-page .drag-handle')['touch-action'], 'none');
});

test('viewport observer handles rotation and legacy media listeners and cleans up', () => {
  for (const legacy of [false, true]) {
    const seen = [], listeners = new Set();
    const media = {matches: true};
    if (legacy) { media.addListener = fn => listeners.add(fn); media.removeListener = fn => listeners.delete(fn); }
    else { media.addEventListener = (name, fn) => {assert.equal(name, 'change'); listeners.add(fn);}; media.removeEventListener = (_, fn) => listeners.delete(fn); }
    const stop = observeAdminPhone(value => seen.push(value), {matchMedia(query) { assert.equal(query, ADMIN_PHONE_QUERY); return media; }});
    media.matches = false; listeners.forEach(fn => fn());
    media.matches = true; listeners.forEach(fn => fn());
    stop(); assert.equal(listeners.size, 0); assert.deepEqual(seen, [true, false, true]);
  }
});

test('template mobile switch preserves draft/panes and uses ordinary button state, not activation APIs', async () => {
  const markup = findClass('Template', 'template-pane-switch').loc.source;
  const scope = Vue.reactive({editing: {content: 'unsaved draft'}, mobilePane: 'edit'});
  let view = await render(markup, scope);
  const buttons = view.nodes.filter(n => n.type === 'button');
  assert.equal(buttons[0].props['aria-pressed'], true);
  buttons[1].props.onClick();
  assert.equal(scope.mobilePane, 'preview');
  assert.equal(scope.editing.content, 'unsaved draft');
  view = await render(markup, scope);
  assert.equal(view.nodes.filter(n => n.type === 'button')[1].props['aria-pressed'], true);
  view.nodes.filter(n => n.type === 'button')[0].props.onClick();
  assert.equal(scope.mobilePane, 'edit');
  for (const cls of ['template-editor-pane', 'template-preview-pane']) {
    assert.ok(!findClass('Template', cls).props.some(p => p.name === 'if' || p.name === 'show'));
  }
  assert.equal(css('.template-page .mobile-pane-edit .template-preview-pane').display, 'none');
  assert.equal(css('.template-page .mobile-pane-preview .template-editor-pane').display, 'none');
});

test('system domain picker retains all domains and existing query reset behavior', async () => {
  const scope = Vue.reactive({domains: [{key:'runtime', title:'运行'}, {key:'tools', title:'工具'}], activeDomain:'runtime', query:'filter', domainSettingCount: () => 3});
  const view = await render(findClass('Settings', 'settings-domain-picker').loc.source, scope);
  const select = view.nodes.find(n => n.type === 'select');
  assert.equal(select.props['aria-label'], '切换设置领域');
  assert.deepEqual(view.nodes.filter(n => n.type === 'option').map(n => n.props.value), ['runtime', 'tools']);
  select.props.onChange({target: {value:'tools'}});
  assert.equal(scope.activeDomain, 'tools'); assert.equal(scope.query, '');
});

test('render log cards open the same row and audit details retain full text without executing APIs', async () => {
  const logs = [{id:1, ts:123, source:'web', client_ip:'test-ip', template_name:'test-template', output_len:88, ms:5, params_json:'{"test":true}'}];
  const calls = [];
  const scope = {logs, loading:false, fmtTime: String, formatNum: String, viewDetail: row => calls.push(row)};
  const markup = findClass('Logs', 'log-cards').loc.source;
  const view = await render(markup, scope);
  view.nodes.find(n => n.type === 'button').props.onClick();
  assert.equal(calls[0], logs[0]);
  for (const value of ['test-ip', 'test-template', '88', '5ms']) assert.ok(view.html.includes(value));
  const audit = nodes('Logs').filter(n => hasClass(n, 'log-cards'))[1];
  const details = await render(audit.loc.source, {loading:false, auditLogs:[{id:2, kind:'fixture', created_at:456, detail:{text:'full-detail'}}], fmtTime: String, prettyJson: JSON.stringify});
  assert.ok(details.nodes.some(n => n.type === 'details'));
  assert.ok(details.html.includes('full-detail'));
  const size = nodes('Logs').find(n => attr(n, 'aria-label') === '每页日志数量');
  const sizes = await render(size.loc.source, {pageSize:20, onSizeChange: value => calls.push(value)});
  sizes.nodes.find(n => n.type === 'select').props.onChange({target: {value:'50'}});
  assert.equal(calls.at(-1), 50);
});

test('compact search rows, template identity and editor occupy bounded tracks rather than wrapping or using vh heights', () => {
  for (const width of [320, 360, 390, 430, 760]) {
    assert.equal(css('.skills-page .admin-filters > div', width)['grid-template-columns'], 'minmax(0, 1fr) 80px auto');
    assert.equal(css('.mcp-page .admin-filters > div', width)['flex-direction'], 'row');
    assert.equal(css('.template-meta > div:first-child', width)['grid-template-columns'], 'minmax(0, 1fr) minmax(0, 1fr)');
    assert.equal(css('.template-page .template-workspace', width).flex, '1 1 0%');
    assert.equal(css('.template-page .template-workspace', width).overflow, 'hidden');
    assert.equal(css('.template-page .template-editor-pane', width)['min-height'], '0');
    assert.equal(css('.template-page .template-editor-pane', width).height, '100%');
    assert.equal(css('.admin-page .el-button', width).height, '30px');
  }
  const settingsCss = postcss.parse(sfcs.Settings.styles.filter(s => s.scoped).map(s => s.content).join('\n'));
  assert.equal(css('.settings-shell', 390, settingsCss).overflow, 'hidden');
  assert.equal(css('.settings-layout', 390, settingsCss).overflow, 'hidden');
  assert.equal(css('.settings-content', 390, settingsCss)['overflow-y'], 'auto');
  assert.equal(css('.settings-content', 390, settingsCss).flex, '1 1 0%');
  for (const name of ['Memory', 'Docs', 'Settings']) {
    const editor = nodes(name).find(n => n.tag === 'MdEditor');
    assert.ok(editor.props.some(p => p.name === 'mobile-flow'), name);
    assert.match(sfcs[name].scriptSetup.content, /AdaptiveMdEditor/);
  }
});

test('mobile template toolbar retains all operations with existing handlers and disabled states', async () => {
  const called = [], scope = {editing:{is_active:false,is_agent_active:false}, dirty:()=>true, showHelp:false, saving:false, changingTemplate:false};
  for (const name of ['newTpl','save','activate','activateAgent','openBuiltinImport','removeCurrent']) scope[name]=()=>called.push(name);
  const view = await render(findClass('Template', 'template-mobile-actions').loc.source, scope);
  for (const button of view.nodes.filter(n => n.type === 'button')) button.props.onClick();
  assert.deepEqual(called, ['newTpl','save','activate','activateAgent','openBuiltinImport','removeCurrent']);
  assert.equal(scope.showHelp,true);
  for (const busy of ['saving', 'changingTemplate']) {
    scope[busy] = true;
    const pending = await render(findClass('Template', 'template-mobile-actions').loc.source, scope);
    const pendingButtons = pending.nodes.filter(n => n.type === 'button');
    for (const index of [0, 1, 2, 3, 6]) assert.equal(pendingButtons[index].props.disabled, true, `${busy}:${index}`);
    scope[busy] = false;
  }
  scope.editing=null; scope.dirty=()=>false;
  const empty=await render(findClass('Template', 'template-mobile-actions').loc.source, scope);
  const buttons=empty.nodes.filter(n=>n.type==='button');
  assert.equal(buttons[1].props.disabled,true);
  assert.equal(buttons[2].props.disabled,true);
  assert.equal(buttons[3].props.disabled,true);
  assert.equal(buttons.at(-1).props.disabled,true);
});
