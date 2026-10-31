// Server network in Chromium, layout only: the mesh member panel, 动态域名.
// Sizes, overflow, scrolling, phone and dark theme. What the page asks the backend and what it
// shows is checked without a browser in net.dom.test.mjs. Page, fake backend and helpers:
// testing/netBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ADDRESSES, GB, GEO, MESH_ADDR, SERVERS, addresses, chromium, fakeMesh, fakeNet, noOverflow, press, probe, rowTexts, run, server, shot, shots, startNetBrowser, waitFor} from './testing/netBrowser.mjs';

test('server network layout: the mesh member panel, 动态域名', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startNetBrowser(t);
  await t.test('mesh member panel behind a proxy: the exit abroad under 本机地址, then 动态域名; desktop and phone', {timeout: 120000}, async t => {
    const h = await setup(t, {tabs: [], active: 'mesh', ddns: true});
    const {page} = h;
    await page.locator('.vue-flow__node[data-id="home"]').click();
    const panel = page.locator('.mesh-inspector-col');
    const local = panel.locator('section.net-addresses');
    await local.locator('.net-exit[data-proxy]').waitFor();
    const dd = panel.locator('section.dd-card-panel');
    await dd.waitFor();
    assert.ok((await dd.boundingBox()).y > (await local.boundingBox()).y, 'after 本机地址');
    assert.equal(await dd.locator('h4').evaluate(el => getComputedStyle(el).fontSize), await local.locator('h4').evaluate(el => getComputedStyle(el).fontSize), 'headed like 本机地址');
    await page.evaluate(() => { window.__opened = []; window.addEventListener('openbear:open-ddns', e => window.__opened.push(e.detail)); });
    await dd.locator('.dd-card-row').click();
    await dd.scrollIntoViewIfNeeded();
    await shot(page, 'desktop-light-member-proxy-ddns');
    await h.close();
    const m = await setup(t, {width: 390, height: 844, tabs: [], active: 'mesh', ddns: true});
    const sheet = m.page.locator('.mesh-inspect-sheet');
    await m.page.locator('.vue-flow__node[data-id="home"]').tap();
    const card = sheet.locator('section.dd-card-panel');
    await card.waitFor();
    await card.scrollIntoViewIfNeeded();
    assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the sheet scrolls sideways');
    assert.ok((await card.locator('.dd-card-row').boundingBox()).height >= 40, 'a row big enough to tap');
    await shot(m.page, 'phone-light-member-proxy-ddns');
    await m.close();
  });

  await t.test('动态域名 is a tab of the servers page: after 组网, it fills the body, a link opens it narrowed to a machine; phone too', {timeout: 120000}, async t => {
    for (const [width, height, name] of [[1440, 900, 'desktop'], [390, 844, 'phone']]) {
      const h = await setup(t, {width, height, tabs: ['home'], active: 'probe', ddns: true});
      const {page} = h;
      const tabs = page.locator('.sv-tabs > [role="tab"]');
      await press(h, page.getByRole('tab', {name: '动态域名'}));
      const dd = page.locator('section.dd-page');
      await dd.locator('.dd-table tbody tr').first().waitFor();
      await dd.getByRole('button', {name: '添加解析'}).waitFor();
      // The tab body, not the page, holds it: nothing scrolls the page.
      const body = await page.locator('.sv-body').boundingBox();
      const box = await dd.boundingBox();
      assert.ok(Math.abs(box.height - body.height) < 2 && Math.abs(box.y - body.y) < 2, `fills the body: ${box.y},${box.height} in ${body.y},${body.height}`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1), 'the page itself does not scroll');
      await noOverflow(page);
      await shot(page, `${name}-light-ddns-tab`);
      // A link from elsewhere (a card, a reminder, /ddns): back to the tab, narrowed to the machine.
      await press(h, page.getByRole('tab', {name: '探针'}));
      await page.evaluate(() => import('/src/ddns/ddnsLink.js').then(m => m.requestDdns({serverId: 'home'})));
      await dd.locator('.dd-chip').filter({hasText: '家'}).waitFor();
      await h.close();
    }
  });
});
