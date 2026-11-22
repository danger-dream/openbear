// Page assistant page operations in happy-dom: what the snapshot reports, which field a label
// finds in the project's form markup, and that nothing here submits or flips a switch.
import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { runPageRequest } from './pageActions.js';
import { capturePageContext, currentScope } from './pageContext.js';
import { setPageInfo, usePageAction, usePageSubject } from './pageRegistry.js';

const field = (label, input = '<input>') =>
  `<div><label class="text-xs">${label}</label><div class="el-input"><div class="el-input__wrapper">${input}</div></div></div>`;

function page(html, dialog = '') {
  document.body.innerHTML = `<main class="app-main">${html}</main>${dialog
    ? `<div class="el-overlay"><div class="el-dialog" role="dialog"><header><span class="el-dialog__title">新建凭证</span></header><div class="el-dialog__body">${dialog}</div><footer><button>取消</button><button>保存</button></footer></div></div>`
    : ''}`;
  setPageInfo(() => ({key: 'secrets', label: '凭证库'}));
}

test('fill finds a field by the label in front of its component wrapper and only types into it', async () => {
  page('', field('名称（引用用，如 github）') + field('备注') + field('序号', '<input type="number" value="30">'));
  const input = document.querySelectorAll('.el-dialog input')[1];
  let typed = 0;
  input.addEventListener('input', () => { typed += 1; });
  const result = await runPageRequest({action: 'fill', params: {label: '备注', value: '临时测试'}});
  assert.equal(result.ok, true);
  assert.equal(result.filled, '备注');
  assert.equal(input.value, '临时测试');
  assert.equal(typed, 1);
  const named = await runPageRequest({action: 'fill', params: {label: '名称', value: 'test-new'}});
  assert.equal(named.filled, '名称（引用用，如 github）');
  assert.equal(document.querySelector('.el-dialog input').value, 'test-new');
});

test('the topmost dialog is searched before the page behind it', async () => {
  page(field('备注'), field('备注'));
  await runPageRequest({action: 'fill', params: {label: '备注', value: '弹窗里'}});
  assert.equal(document.querySelector('.el-dialog input').value, '弹窗里');
  assert.equal(document.querySelector('main input').value, '');
});

test('switches, checkboxes and pickers are highlighted for the user instead of changed', async () => {
  page(`<div><span>启用</span><div class="el-switch"><input type="checkbox" class="el-switch__input"></div></div>`
    + field('分组', '<input class="el-select__input">').replace('el-input"', 'el-select"'));
  let changed = 0;
  const box = document.querySelector('input[type=checkbox]');
  box.addEventListener('change', () => { changed += 1; });
  const toggle = await runPageRequest({action: 'fill', params: {label: '启用', value: 'true'}});
  assert.equal(toggle.error, 'field_unsupported');
  assert.equal(box.checked, false);
  assert.equal(changed, 0);
  assert.equal((await runPageRequest({action: 'fill', params: {label: '分组', value: '服务器'}})).error, 'field_unsupported');
});

test('a missing field lists the labels that exist, and there is no action that presses a button', async () => {
  page('', field('名称') + field('备注'));
  const missing = await runPageRequest({action: 'fill', params: {label: '密码', value: 'x'}});
  assert.equal(missing.error, 'field_not_found');
  assert.deepEqual(missing.fields, ['名称', '备注']);
  let pressed = 0;
  document.querySelectorAll('button').forEach(button => button.addEventListener('click', () => { pressed += 1; }));
  assert.equal((await runPageRequest({action: 'click', params: {target: '保存'}})).error, 'unknown_action');
  const located = await runPageRequest({action: 'locate', params: {target: '保存'}});
  assert.equal(located.ok, true);
  assert.equal(pressed, 0);
});

test('the innermost subject on the page leads, and one in the topmost dialog leads them all', async () => {
  page('<section id="outer"><div id="inner"></div></section>');
  const outer = usePageSubject(() => ({kind: 'settings-tab', id: 'system-settings', label: '系统设置'}), {el: document.getElementById('outer')});
  const inner = usePageSubject(() => ({kind: 'settings-section', id: 'assistant', label: '页面助手'}), {el: document.getElementById('inner')});
  try {
    assert.deepEqual(capturePageContext().subjects.map(s => s.label), ['页面助手', '系统设置']);
    page('<section id="outer"><div id="inner"></div></section>', field('名称'));
    const dialog = usePageSubject(() => ({kind: 'secret-draft', label: '新建凭证（未保存）'}), {el: document.querySelector('.el-dialog')});
    try {
      const context = capturePageContext();
      assert.equal(context.subjects[0].label, '新建凭证（未保存）');
      assert.equal(context.subjects[0].scope, 'overlay');
    } finally { dialog(); }
  } finally {
    outer();
    inner();
  }
});

test('read and the snapshot report the page, the topmost dialog, its fields and buttons, subjects and actions', async () => {
  page('<h1>凭证库</h1>', field('名称'));
  const stopSubject = usePageSubject(() => ({kind: 'secret', id: 7, label: 'github', role: '正在编辑'}));
  let ran = null;
  const stopAction = usePageAction('demo.insert', {description: '插入文字', run: (args) => { ran = args; return {inserted: true}; }});
  try {
    const context = capturePageContext({visibleChars: 200});
    assert.deepEqual(context.page, {key: 'secrets', label: '凭证库', path: '/'});
    assert.deepEqual(context.overlays, [{kind: 'dialog', title: '新建凭证'}]);
    assert.equal(context.visible.of, 'overlay');
    assert.equal(context.subjects[0].ref, 'openbear://ref/secret/7');
    assert.deepEqual(context.actions.map(a => a.name), ['demo.insert']);
    assert.equal(context.label, '凭证库 › 新建凭证 › github');
    const read = await runPageRequest({action: 'read', params: {}});
    assert.deepEqual(read.fields.map(f => f.label), ['名称']);
    assert.deepEqual(read.buttons, ['取消', '保存']);
    const invoked = await runPageRequest({action: 'invoke', params: {name: 'demo.insert', args: {markdown: '下周'}}});
    assert.deepEqual([invoked.ok, invoked.inserted, ran], [true, true, {markdown: '下周'}]);
    assert.equal((await runPageRequest({action: 'invoke', params: {name: 'gone'}})).error, 'action_not_found');
  } finally {
    stopSubject();
    stopAction();
  }
});

test('the scope is the page and its main object; a dialog does not change it', async () => {
  page('<section id="detail"></section>');
  assert.deepEqual(currentScope(), {key: 'secrets', label: '凭证库', path: '/'});
  const server = usePageSubject(() => ({kind: 'server', id: 'srv-a', label: 'A', main: true}), {el: document.getElementById('detail')});
  const listed = usePageSubject(() => ({kind: 'secret', id: 3, label: 'github'}), {el: document.getElementById('detail')});
  try {
    assert.deepEqual(currentScope(), {key: 'secrets|server:srv-a', label: '凭证库 · A', path: '/', open: {kind: 'server', id: 'srv-a'}});
    page('<section id="detail"></section>', field('名称'));
    const inDialog = usePageSubject(() => ({kind: 'template', id: 9, label: '草稿', main: true}), {el: document.querySelector('.el-dialog')});
    try {
      assert.equal(currentScope().key, 'secrets', 'a main object inside a dialog is not where the user is');
    } finally { inDialog(); }
  } finally {
    server();
    listed();
  }
});

test('a request from a task of another page is refused before it touches this page', async () => {
  page('', field('备注'));
  const refused = await runPageRequest({action: 'fill', scope: {key: 'servers|server:a', label: '服务器 · A'}, params: {label: '备注', value: 'x'}});
  assert.equal(refused.error, 'page_left');
  assert.match(refused.message, /服务器 · A.*凭证库/);
  assert.equal(document.querySelector('.el-dialog input').value, '');
  const here = await runPageRequest({action: 'fill', scope: {key: 'secrets', label: '凭证库'}, params: {label: '备注', value: 'y'}});
  assert.equal(here.ok, true);
  assert.equal(document.querySelector('.el-dialog input').value, 'y');
});
