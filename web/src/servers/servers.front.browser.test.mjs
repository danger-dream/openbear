// 前置机 (fronts) on the servers page in Chromium, layout only: the add form's tabs and exit rows, and the server editor
// (a drawer on a desktop, the bottom sheet on a phone) with its tag box, price and date columns and exit rows,
// on a desktop and a phone, light and dark. What they send is checked in servers.front.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, settled, showTab} from './testing/serversBrowser.mjs';

const box = locator => locator.evaluate(el => el.getBoundingClientRect().toJSON());
const inside = async (dialog, locator, label) => {
  const outer = await box(dialog);
  for (const b of await locator.evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()))) {
    assert.ok(b.left >= outer.left - 1 && b.right <= outer.right + 1, `${label} leaves the dialog: ${JSON.stringify(b)} in ${JSON.stringify(outer)}`);
  }
};
const near = (a, b, label) => assert.ok(Math.abs(a - b) <= 1.5, `${label}: ${a} vs ${b}`);
async function pickAddress(scope, label, ip) {
  const select = scope.getByLabel(label, {exact: true});
  await select.locator(`option[value="${ip}"]`).waitFor({state: 'attached'});
  await select.selectOption(ip);
}

test('fronts: add form and editor fit desktop and phone, light and dark', {skip: !chromium, timeout: 240000}, async t => {
  const {setup} = await startServersBrowser(t);
  for (const [width, dark] of [[1440, false], [390, false], [390, true]]) {
    const h = await setup(t, {width, height: width < 500 ? 844 : 900, dark, touch: width < 500});
    await h.page.getByRole('button', {name: '添加服务器'}).click();
    const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
    await dialog.getByRole('tab', {name: '前置机'}).click();
    await dialog.getByLabel('出口', {exact: true}).selectOption('srv-papa');
    await pickAddress(dialog, '出口地址', '10.0.10.246');
    await dialog.getByRole('button', {name: '加一个备用出口'}).click();
    await dialog.getByLabel('备用出口', {exact: true}).selectOption('srv-delta');
    await pickAddress(dialog, '备用出口地址', '10.77.17.46');
    // Five ways to add a server, each tab whole; every exit control inside the form.
    await inside(dialog, dialog.getByRole('tab'), 'a tab');
    await inside(dialog, dialog.locator('.sv-front-exit > *'), 'an exit control');
    // The two rows keep the same columns, and an address does not squeeze to nothing.
    const [primary, backup] = [await box(dialog.getByLabel('出口地址', {exact: true})), await box(dialog.getByLabel('备用出口地址', {exact: true}))];
    near(primary.left, backup.left, 'address columns'); near(primary.width, backup.width, 'address widths');
    assert.ok(primary.width >= 120, `出口地址 is ${primary.width}px wide`);
    await noOverflow(h.page);
    await shot(h.page, `front-add-${width}${dark ? '-dark' : ''}`);
    await dialog.getByRole('button', {name: '取消'}).click();

    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    await showTab(h.page.locator('.sv-detail:visible'), '系统');
    await h.page.locator('.sv-detail:visible [data-part=billing]').getByRole('button', {name: '编辑'}).click();
    const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
    await editor.getByLabel(/这是前置机/).check();
    await editor.getByLabel('出口', {exact: true}).selectOption('srv-papa');
    await pickAddress(editor, '出口地址', '10.0.10.246');
    await editor.getByRole('button', {name: '加一个备用出口'}).click();
    await editor.getByLabel('备用出口', {exact: true}).selectOption('srv-bravo');
    await inside(editor, editor.locator('.sv-front-exit > *'), 'an exit control');
    near((await box(editor.getByLabel('出口地址', {exact: true}))).left, (await box(editor.getByLabel('备用出口地址', {exact: true}))).left, 'editor address columns');
    // The tag box is as tall as the name field and spans the same width as the group.
    const [name, tags, group] = [await box(editor.getByLabel('服务器名称')), await box(editor.locator('.sv-tags-edit')), await box(editor.getByLabel('分组'))];
    near(tags.height, name.height, 'tag box height'); near(tags.left, group.left, 'tag box left'); near(tags.right, group.right, 'tag box right');
    // Price and dates in the same two columns: the price ends where the start date does, the cycle sits over the expiry.
    const [price, cycle, start, expiry, until] = await Promise.all([editor.getByLabel('价格', {exact: true}), editor.getByLabel('计费周期'),
      editor.getByLabel('开始日期'), editor.getByLabel('到期日期'), editor.locator('.sv-dates > label').nth(1)].map(box));
    near(cycle.top, price.top, 'cycle and price on one line'); near(price.right, start.right, 'price and start right edges'); near(cycle.left, until.left, 'cycle over the expiry'); near(cycle.right, expiry.right, 'cycle and expiry right edges');
    // +1月 … +1年 under the expiry; on a phone across the row, all four on one line.
    const plus = await editor.getByRole('group', {name: '延长到期日'}).getByRole('button').evaluateAll(els => els.map(el => el.getBoundingClientRect().toJSON()));
    if (width < 500) {
      for (const b of plus) near(b.top, plus[0].top, 'one line of +1月 … +1年');
      near(plus[0].left, (await box(editor.locator('.sv-dates > label').first())).left, 'across the row'); near(plus.at(-1).right, expiry.right, 'to the end of the row');
    }
    else near(plus[0].left, until.left, '+1月 under the expiry');
    await noOverflow(h.page);
    await shot(h.page, `front-editor-${width}${dark ? '-dark' : ''}`);
    await editor.getByRole('button', {name: '+1月'}).evaluate(el => el.scrollIntoView({block: 'center'}));
    await shot(h.page, `front-editor-billing-${width}${dark ? '-dark' : ''}`);
    await h.close();
  }
});

test('editor: on a short laptop screen a full-height drawer, its title and buttons kept in view', {skip: !chromium, timeout: 120000}, async t => {
  const {setup} = await startServersBrowser(t);
  const h = await setup(t, {width: 1366, height: 680});
  await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
  await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Papa-HK.T1」'});
  await editor.getByLabel('服务器名称').waitFor();
  await settled(h.page);
  const panel = await box(editor);
  near(panel.top, 0, 'drawer top'); near(panel.bottom, 680, 'drawer bottom'); near(panel.right, 1366, 'drawer right edge');
  // Scrolled to the end of the form: the title stays at the top and 保存 at the bottom, both on screen.
  await editor.getByLabel('备注').scrollIntoViewIfNeeded();
  const [title, save] = [await box(editor.getByRole('heading', {name: '编辑「Papa-HK.T1」'})), await box(editor.getByRole('button', {name: '保存', exact: true}))];
  assert.ok(title.top >= 0 && title.bottom <= 680, `title at ${JSON.stringify(title)}`);
  assert.ok(save.top >= 0 && save.bottom <= 680, `保存 at ${JSON.stringify(save)}`);
  // A click beside the drawer asks before it drops an edit; going on keeps it.
  await editor.getByLabel('备注').fill('改了备注');
  await h.page.mouse.click(200, 300);
  const ask = h.page.locator('.el-message-box').filter({hasText: '放弃修改？'});
  await ask.getByRole('button', {name: '继续编辑'}).click();
  await ask.waitFor({state: 'hidden'});
  await settled(h.page);
  assert.ok(await editor.isVisible());
  await shot(h.page, 'editor-drawer-1366x680');
  await editor.getByRole('button', {name: '取消'}).click();
  await editor.waitFor({state: 'hidden'});
  await h.close();
});
