// The server editor's drawer in Chromium: a click on the overlay closes it, at once while nothing was
// changed and after asking while something was; on a phone the sheet's overlay, close button and drag do the same.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, settled} from './testing/serversBrowser.mjs';

test('the editor closes from its overlay, asking first while there are edits', {skip: !chromium, timeout: 200000}, async t => {
  const {setup} = await startServersBrowser(t);

  await t.test('desktop', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 1440, height: 900});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
    const ask = h.page.locator('.el-message-box').filter({hasText: '放弃修改？'});
    const outside = () => h.page.mouse.click(200, 450);
    const open = async () => { await detail.getByRole('button', {name: '编辑'}).first().click(); await editor.getByLabel('名称').waitFor(); await settled(h.page); };
    // Nothing changed: it just closes.
    await open();
    await outside();
    await editor.waitFor({state: 'hidden'});
    assert.equal(await ask.count(), 0);
    // Changed: it asks; going on keeps the edit, giving it up closes without saving.
    await open();
    await editor.getByLabel('名称').fill('Delta-JP 改名');
    await outside();
    await ask.waitFor();
    await ask.getByRole('button', {name: '继续编辑'}).click();
    await ask.waitFor({state: 'hidden'});
    assert.equal(await editor.getByLabel('名称').inputValue(), 'Delta-JP 改名');
    await h.page.keyboard.press('Escape');
    await ask.waitFor();
    await ask.getByRole('button', {name: '继续编辑'}).click();
    await editor.locator('.dd-close').click();
    await ask.waitFor();
    await ask.getByRole('button', {name: '放弃修改'}).click();
    await editor.waitFor({state: 'hidden'});
    assert.ok(!h.calls.some(c => c.method === 'PATCH' && c.path === '/api/servers/srv-delta'), 'nothing saved');
    // 取消 is the editor's own and gives the edits up as asked.
    await open();
    await editor.getByLabel('名称').fill('又改了');
    await editor.getByRole('button', {name: '取消'}).click();
    await editor.waitFor({state: 'hidden'});
    assert.equal(await ask.count(), 0);
    await h.close();
  });

  await t.test('phone, dark', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, touch: true});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    const sheet = h.page.locator('.dd-sheet:visible');
    const ask = h.page.locator('.el-message-box').filter({hasText: '放弃修改？'});
    const open = async () => {
      await detail.locator('.ix-more').click();
      await h.page.getByRole('menuitem', {name: '编辑…'}).click();
      await sheet.getByLabel('名称').waitFor();
      await settled(h.page);
    };
    await open();
    await sheet.getByLabel('名称').fill('手机上改名');
    // The overlay above the sheet: the editor nearly fills the screen, leaving a strip on top.
    const top = (await sheet.boundingBox()).y;
    assert.ok(top > 4, `sheet at ${top}`);
    await h.page.mouse.click(195, top / 2);
    await ask.waitFor();
    await ask.getByRole('button', {name: '继续编辑'}).click();
    await ask.waitFor({state: 'hidden'});
    await settled(h.page);
    assert.ok(await sheet.isVisible(), 'kept up');
    assert.equal(await sheet.getByLabel('名称').inputValue(), '手机上改名');
    await sheet.getByRole('button', {name: '关闭'}).click();
    await ask.waitFor();
    await ask.getByRole('button', {name: '放弃修改'}).click();
    await sheet.waitFor({state: 'hidden'});
    // Unchanged: the close button closes at once.
    await open();
    await sheet.getByRole('button', {name: '关闭'}).click();
    await sheet.waitFor({state: 'hidden'});
    assert.equal(await ask.count(), 0);
    await h.close();
  });
});
