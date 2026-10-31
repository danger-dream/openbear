// The mesh board in Chromium, layout only: instances on a mesh address and host firewalls.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until} from './testing/meshBrowser.mjs';

test('mesh board layout: instances on a mesh address and host firewalls', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('instances on a mesh address and host firewalls: read-only automatic rules after the user\'s; a Linux member\'s firewalls and what they may not let through; phone light and dark', {timeout: 150000}, async t => {
    const h = await setup(t, {firewall: true});
    const panel = inspector(h.page);
    const auto = panel.getByRole('list', {name: '自动放行'});
    await auto.waitFor();
    // Read-only: no editing, no deleting, not dragged; the user's rules and their buttons as before.
    await panel.getByText('还没有规则：除了下面的自动放行，成员之间互相不能访问。').waitFor();
    await panel.getByRole('button', {name: '允许所有成员互相 ping'}).click();
    await h.saved(d => d.rules.length === 1, 'a rule of the user\'s');
    await h.page.keyboard.press('Escape');
    await panel.getByRole('button', {name: '编辑第 1 条规则'}).waitFor();
    // The automatic row lines up with the user's rule text.
    const [userX, autoX] = await Promise.all([panel.locator('.mesh-rule-list').first().locator('.mesh-rule-text').boundingBox(), auto.locator('.mesh-rule-text').boundingBox()]);
    assert.ok(Math.abs(userX.x - autoX.x) <= 1, `aligned: ${userX.x} ${autoX.x}`);
    await shot(h.page, 'desktop-auto-rules');
    // The Linux member: its firewalls as a status row, one hint per thing they may not let through.
    await node(h.page, 'home').click();
    const row = panel.locator('.mesh-stats > div', {has: h.page.locator('dt', {hasText: /^主机防火墙$/})});
    await row.waitFor();
    const hints = panel.locator('.mesh-hints summary');
    await hints.filter({hasText: 'work 区域'}).click();
    await panel.getByText(/obmesh0 是在这台机器上手动指定到 work 区域的，组网没有改动它/).waitFor();
    await shot(h.page, 'desktop-host-firewall');
    await noOverflow(h.page);
    await h.close();
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark, firewall: true});
      await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
      const overview = p.page.locator('.mesh-overview-sheet');
      const list = overview.getByRole('list', {name: '自动放行'});
      await list.waitFor();
      await list.scrollIntoViewIfNeeded();
      const box = await list.locator('li').boundingBox();
      assert.ok(box.height <= 64, `an automatic rule stays compact on a phone: ${box.height}`);
      assert.ok(await overview.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'fits the width');
      await shot(p.page, `phone-auto-rules-${dark ? 'dark' : 'light'}`);
      await noOverflow(p.page);
      await p.close();
      const q = await setup(t, {width: 390, height: 844, dark, firewall: true});
      await node(q.page, 'home').tap();
      const sheet = q.page.locator('.mesh-inspect-sheet');
      await sheet.locator('.mesh-stats dt', {hasText: '主机防火墙'}).waitFor();
      await sheet.locator('.mesh-hints summary', {hasText: 'work 区域'}).tap();
      await sheet.getByText(/obmesh0 是在这台机器上手动指定到 work 区域的/).waitFor();
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the hints fit the width');
      await shot(q.page, `phone-host-firewall-${dark ? 'dark' : 'light'}`);
      await noOverflow(q.page);
      await q.close();
    }
  });
});
