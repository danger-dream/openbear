// Saving an edited context without a browser, on the real ContextEditor (Monaco replaced by
// testing/monacoStub.mjs): both destinations need a preview first, the original needs a
// confirmation that explains what stays, the save sends the edited entries with the snapshot it
// started from, a conflict keeps the editor open, and a copy is saved under its title.
// The confirm dialog's fit, sideways scroll, 16px inputs and the dark theme stay in save.browser.test.mjs.
import {window} from '../../../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h, ref} from 'vue';
import {openPage} from '../../../testing/appHarness.mjs';
import {themeValues} from '../../../testing/theme.mjs';
import {createContextSaveBackend} from './saveFixture.mjs';

const ContextEditor = (await import('../ContextEditor.vue')).default;
// The editor's Monaco theme reads the palette's RGB channels from the root's computed style; the
// stylesheet is not applied in happy-dom, so the root carries them inline.
function paletteOnRoot(dark) {
  const root = window.document.documentElement;
  for (const [key, value] of themeValues(dark)) if (key.endsWith('-rgb')) root.style.setProperty(key, value);
}

// The keyboard case (390×320 with the keyboard up, the editor switching between panes) depends on
// the editor's measured height, so it stays whole in save.browser.test.mjs.
for (const [name, width, height] of [['desktop', 1440, 900], ['phone', 390, 844], ['dark', 360, 780]]) test(name, async t => {
  const backend = createContextSaveBackend(), events = {};
  const root = {setup() { const editor = ref(null); return () => [h('button', {onClick: () => editor.value.open()}, 'Open context editor'),
    h(ContextEditor, {ref: editor, conversationUuid: 'original', onSaved: value => { events.saved = value; }, onCreated: value => { events.created = value; }})]; }};
  const {page, unmount} = await openPage(t, root, {url: '/__context-save', width, height, dark: name === 'dark', handler: backend.handle, before: () => paletteOnRoot(name === 'dark')});
  const {writes} = backend;
  await page.getByRole('button', {name: 'Open context editor', exact: true}).click();
  await page.locator('.context-editor .ce-workspace').waitFor();
  await page.getByRole('checkbox', {name: '选择 a', exact: true}).check();
  await page.getByRole('button', {name: '删除…', exact: true}).click();
  await page.getByRole('dialog').getByRole('button', {name: '确认删除', exact: true}).click();
  assert.equal(await page.getByRole('button', {name: '保存到原会话', exact: true}).isDisabled(), true);
  await page.getByRole('button', {name: '校验并预览', exact: true}).click();
  await page.locator('.ce-help[role="status"]').filter({hasText: '自动处理 25 组'}).waitFor();
  assert.equal(await page.locator('.ce-help[role="status"]').count(), 1);
  assert.equal(await page.getByRole('button', {name: '保存副本', exact: true}).isDisabled(), false);
  await page.getByRole('button', {name: '保存到原会话', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: '保存到原会话', exact: true});
  await dialog.waitFor(); assert.equal(writes.length, 0);
  assert.match(await dialog.innerText(), /原始聊天和工具执行记录不变/);
  await dialog.getByRole('button', {name: '保存到原会话', exact: true}).click();
  await page.waitForFunction(() => events.saved?.conversationUuid === 'original');
  assert.equal(writes[0].body.working.entries.length, 1); assert.equal(writes[0].body.revision, 0); assert.equal(writes[0].body.snapshotToken, 'initial');
  assert.match(await page.locator('.ce-status').innerText(), /已保存到原会话/);
  assert.equal(await page.locator('.context-editor').count(), 1);
  // A server-side conflict leaves the current editor available instead of navigating away.
  await page.getByRole('button', {name: '校验并预览', exact: true}).click(); backend.conflict();
  await page.getByRole('button', {name: '保存到原会话', exact: true}).click();
  await page.getByRole('dialog', {name: '保存到原会话', exact: true}).getByRole('button', {name: '保存到原会话', exact: true}).click();
  await page.getByRole('alert').filter({hasText: '409'}).waitFor();
  assert.equal(await page.locator('.context-editor').count(), 1);
  await page.getByRole('textbox', {name: '副本标题', exact: true}).fill('Context copy');
  await page.getByRole('button', {name: '保存副本', exact: true}).click();
  await page.waitForFunction(() => events.created?.conversationUuid === 'copy');
  assert.equal(writes.at(-1).method, 'branch'); assert.equal(writes.at(-1).body.title, 'Context copy');
  await unmount();
});
