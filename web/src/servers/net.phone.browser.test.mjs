// Server network in Chromium, layout only: phone, light.
// Sizes, overflow, scrolling, phone and dark theme. What the page asks the backend and what it
// shows is checked without a browser in net.dom.test.mjs. Page, fake backend and helpers:
// testing/netBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ADDRESSES, GB, GEO, MESH_ADDR, SERVERS, addresses, chromium, fakeMesh, fakeNet, noOverflow, press, probe, rowTexts, run, server, shot, shots, startNetBrowser, waitFor} from './testing/netBrowser.mjs';

test('server network layout: phone, light', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startNetBrowser(t);
  for (const theme of ['light']) {
    await t.test(`phone ${theme}: cards on the detail page, inputs that do not zoom, the trace as two-line hops with no sideways scrolling, the member sheet`, {timeout: 150000}, async t => {
      const h = await setup(t, {width: 390, height: 844, dark: theme === 'dark'});
      const {page} = h;
      const root = probe(page);
      const box = addresses(page);
      await box.locator('.net-iface').first().waitFor();
      await box.scrollIntoViewIfNeeded();
      for (const height of await box.locator('.net-addr, .net-refresh').evaluateAll(l => l.map(el => el.getBoundingClientRect().height))) assert.ok(height >= 40, `address row or refresh ${height}px`);
      await noOverflow(page);
      await shot(page, `phone-${theme}-addresses`);
      // Inputs at 16px (no zoom on focus); the buttons easy to tap.
      await root.scrollIntoViewIfNeeded();
      const target = root.getByRole('combobox', {name: '目标'});
      assert.equal(await target.evaluate(el => getComputedStyle(el).fontSize), '16px');
      await press(h, root.getByRole('radio', {name: 'TCPing'}));
      assert.equal(await root.getByRole('textbox', {name: '端口'}).evaluate(el => getComputedStyle(el).fontSize), '16px');
      await press(h, root.locator('details.net-advanced > summary'));
      for (const el of await root.locator('.net-adv-grid input').all()) assert.equal(await el.evaluate(x => getComputedStyle(x).fontSize), '16px');
      for (const [what, height] of await root.locator('.net-families > button').evaluateAll(l => l.map(el => [el.textContent.trim(), el.getBoundingClientRect().height])))
        assert.ok(height < 44, `the protocol choice ${what} on one line, ${height}px`);
      for (const [what, height] of await root.locator('.net-go, details.net-advanced > summary, .net-modes > button, .net-families > button').evaluateAll(l => l.map(el => [el.textContent.trim(), el.getBoundingClientRect().height])))
        assert.ok(height >= 36, `${what} ${height}px`);
      assert.ok((await root.locator('.net-go').boundingBox()).height >= 40);
      await press(h, root.locator('details.net-advanced > summary'));
      // Focus and blur leave the page where it was (no zoom, nothing shifted sideways).
      await target.tap();
      assert.equal(await page.evaluate(() => visualViewport.scale), 1);
      await target.blur();
      await noOverflow(page);
      await run(h, {mode: 'TCPing', target: 'example.com', port: '443', count: '5'});
      await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
      await root.locator('.net-result').scrollIntoViewIfNeeded();
      await noOverflow(page);
      await shot(page, `phone-${theme}-tcping`);
      // The trace on a phone: two lines an address instead of the table, nothing to scroll sideways, the average in sight.
      await run(h, {mode: '路由追踪', target: 'example.com'});
      const list = root.locator('ol.net-hops');
      await list.locator(':scope > li').nth(3).waitFor();
      await list.locator('.net-geo-wait').first().waitFor();
      const heights = () => list.locator(':scope > li').evaluateAll(l => l.slice(0, 5).map(li => li.getBoundingClientRect().height));
      const waitingHeights = await heights();
      await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
      await list.locator(':scope > li').nth(5).getByText('AS15133 Edgecast Inc.').waitFor({timeout: 10000});
      assert.deepEqual(await heights(), waitingHeights, 'rows keep their height when the places come in');
      const hopTexts = () => list.locator(':scope > li').evaluateAll(l => l.map(li => [...li.querySelectorAll('.net-hop-line')].map(d => d.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean).join(' / ')));
      const fits = async () => {
        assert.ok(await list.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the list does not scroll sideways');
        for (const [text, right, left] of await list.locator('.net-hop-rtt, .net-hop-loss').evaluateAll(l => l.map(el => { const r = el.getBoundingClientRect(); return [el.textContent, r.right, r.left]; })))
          assert.ok(left >= 0 && right <= 390, `${text} at ${left}–${right}`);
        await noOverflow(page);
      };
      await fits();
      for (const size of await list.locator('.net-hop-line').evaluateAll(l => l.map(el => parseFloat(getComputedStyle(el).fontSize)))) assert.ok(size >= 12, `${size}px text`);
      await list.scrollIntoViewIfNeeded();
      await shot(page, `phone-${theme}-trace`);
      // Two addresses on a hop, loss, places known by operator only or by country only, a long name cut short.
      await run(h, {mode: '路由追踪', target: 'cn2.example'});
      await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
      await list.locator(':scope > li').nth(3).getByText('United States').waitFor({timeout: 10000});
      const cn2 = await hopTexts();
      const two = list.locator(':scope > li').nth(1);
      const name = list.locator(':scope > li').nth(3).locator('.net-hop-addr small');
      assert.ok(await name.evaluate(el => el.scrollWidth > el.clientWidth), 'the long name is cut short');
      await fits();
      await list.scrollIntoViewIfNeeded();
      await shot(page, `phone-${theme}-trace-multi`);
      // A hop not tried yet while it runs: the waiting placeholder, no "0%".
      await run(h, {mode: '路由追踪', target: 'gap.example'});
      await list.locator('li[data-pending] .net-geo-wait').waitFor();
      await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
      await run(h, {mode: '路由追踪', target: '100.96.0.2'});
      await root.locator('.net-mesh-path').waitFor();
      await root.locator('.net-summary').waitFor();
      await root.locator('.net-result').scrollIntoViewIfNeeded();
      await noOverflow(page);
      await shot(page, `phone-${theme}-mesh-trace`);
      await h.close();

      // The member panel in the bottom sheet.
      const m = await setup(t, {width: 390, height: 844, dark: theme === 'dark', tabs: [], active: 'mesh'});
      const sheet = m.page.locator('.mesh-inspect-sheet');
      await m.page.locator('.vue-flow__node[data-id="lax"]').tap();
      const local = sheet.locator('section.net-addresses');
      await local.locator('.net-iface').first().waitFor();
      await local.scrollIntoViewIfNeeded();
      assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the sheet scrolls sideways');
      await shot(m.page, `phone-${theme}-member-addresses`);
      await sheet.getByRole('tab', {name: '测试'}).tap();
      const pr = sheet.locator('section.net-probe');
      assert.equal(await pr.getByRole('combobox', {name: '目标'}).evaluate(el => getComputedStyle(el).fontSize), '16px');
      const pid = await run(m, {mode: '路由追踪', target: '10.10.10.1', count: '10', root: pr});
      await pr.locator('.net-mesh-path').waitFor();
      await pr.locator('.net-summary').waitFor();
      assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      assert.ok((await pr.getByRole('button', {name: '在画板上查看'}).boundingBox()).height >= 40);
      await shot(m.page, `phone-${theme}-member-test`);
      // On the board: the sheet steps aside (and closes, so the test stops) and the path shows.
      await pr.getByRole('button', {name: '在画板上查看'}).tap();
      await sheet.waitFor({state: 'hidden'});
      await m.page.locator('.mesh-path .mesh-path-hops').waitFor();
      await waitFor(() => m.stops(pid) === 1, 'a stop when the sheet closes');
      await noOverflow(m.page);
      await shot(m.page, `phone-${theme}-member-path`);
      await m.close();
    });
  }
});
