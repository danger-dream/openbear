// The mesh board in Chromium, layout only: rules, events, security.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until} from './testing/meshBrowser.mjs';

test('mesh board layout: rules, events, security', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('rules layer: hidden by default, a rule drawn with the rule tool, edited in the panel, picked from the board', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    const toggle = page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'});
    assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
    await page.locator('.mesh-tool', {hasText: '访问规则'}).click();
    await dragBody(page, 'home', 'berlin');
    let body = await h.saved(d => d.rules.length === 1, 'new rule');
    assert.deepEqual([body.rules[0].src.members, body.rules[0].dst.members, body.rules[0].proto], [['home'], ['berlin'], 'any']);
    assert.equal(await toggle.getAttribute('aria-pressed'), 'true', 'the layer turns on for a new rule');
    assert.equal(await title(page).innerText(), '访问规则 1');
    await label(page, '全部').waitFor();
    const panel = inspector(page);
    await panel.getByRole('radio', {name: 'TCP'}).check();
    await panel.getByRole('textbox', {name: '端口'}).fill('22, 8000-8100');
    await panel.getByRole('textbox', {name: '端口'}).press('Tab');
    body = await h.saved(d => d.rules[0].proto === 'tcp' && d.rules[0].ports.length === 2, 'rule ports');
    assert.deepEqual(body.rules[0].ports, [[22, 22], [8000, 8100]]);
    await label(page, 'TCP 22, 8000-8100').waitFor();
    await page.locator('.mesh-summary').click();
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.mesh-tool[aria-checked=true]').innerText(), '选择');
    // The panel lists rules as sentences; ping for everyone draws both ways between every pair.
    await panel.getByText('允许 Home 访问 Berlin 的 TCP 22, 8000-8100').waitFor();
    await panel.getByRole('button', {name: '允许所有成员互相 ping'}).click();
    body = await h.saved(d => d.rules.length === 2 && d.rules[1].proto === 'icmp', 'ping rule');
    assert.deepEqual([body.rules[1].src.all, body.rules[1].dst.all, body.rules[1].src.members, body.rules[1].dst.members], [true, true, [], []]);
    await page.keyboard.press('Escape');
    // Ping for everyone draws no arrows: it is listed in the corner legend.
    assert.equal(await label(page, 'ping').count(), 0);
    assert.equal(await page.locator('.mesh-edge[data-kind=rule]').count(), 1);
    const legend = page.locator('.mesh-legend');
    assert.deepEqual(await legend.getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label'))), ['访问规则 2：所有成员 → 所有成员 · ping']);
    // Hovering a rule highlights its members; picking one keeps the others faded.
    await legend.getByRole('button').hover();
    assert.deepEqual(await dimmed(page), [], 'everyone is part of the ping rule');
    await label(page, 'TCP 22, 8000-8100').click();
    assert.equal(await title(page).innerText(), '访问规则 1');
    await page.mouse.move(5, 5);
    assert.deepEqual(await dimmed(page), ['lax']);
    assert.ok(await page.locator('.mesh-edge[data-kind=entry]').first().getAttribute('data-dim'), 'other links fade');
    await shot(page, 'desktop-rules');
    await page.keyboard.press('Escape');
    assert.deepEqual(await dimmed(page), []);
    // Hovering a rule in the panel's list highlights it too.
    await inspector(page).getByRole('button', {name: '编辑第 1 条规则'}).hover();
    assert.deepEqual(await dimmed(page), ['lax']);
    await page.mouse.move(5, 5);
    // A second rule on the same pair merges into one arrow; picking it lists both.
    await page.locator('.mesh-tool', {hasText: '访问规则'}).click();
    await dragBody(page, 'berlin', 'home');
    await h.saved(d => d.rules.length === 3, 'second rule');
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    await label(page, '2 条规则').click();
    assert.equal(await title(page).innerText(), 'Berlin 和 Home 之间的规则');
    assert.equal(await inspector(page).locator('.mesh-pair-rules .mesh-rule').count(), 2);
    assert.equal(await page.locator('.mesh-edge[data-kind=rule]').count(), 1);
    await inspector(page).getByRole('button', {name: '编辑第 3 条规则'}).click();
    assert.equal(await title(page).innerText(), '访问规则 3');
    await page.keyboard.press('Escape');
    await toggle.click();
    assert.equal(await page.locator('.mesh-edge[data-kind=rule]').count(), 0);
    // The legend keeps only what the quiet lines are.
    assert.equal(await legend.getByRole('button').count(), 0);
    assert.equal(await legend.locator('.mesh-legend-note').count(), 1);
    await h.close();
  });

  await t.test('rules for all members: the ping button, the "all members" choice on either side, summaries and the controller\'s change text', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    const panel = inspector(page);
    await panel.getByRole('button', {name: '允许所有成员互相 ping'}).click();
    await h.saved(d => d.rules.length === 1 && d.rules[0].src.all && d.rules[0].dst.all, 'ping for all');
    // The new rule opens in the editor: both sides say all members; no member picker, networks can still be added.
    await panel.getByText('各成员自己的组网地址，以后加入的也算；不含网关后面的局域网').waitFor();
    await shot(page, 'desktop-rule-all');
    // Back in the list: the sentence, the board legend, and the change as the controller words it.
    await page.keyboard.press('Escape');
    await panel.getByText('允许 所有成员 访问 所有成员 的 ICMP（ping）').waitFor();
    await panel.locator('.mesh-changes li', {hasText: '新增访问规则：所有成员 → 所有成员 · ICMP'}).waitFor();
    const layer = page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'});
    if (await layer.getAttribute('aria-pressed') === 'false') await layer.click();
    await shot(page, 'desktop-rule-all-list');
    await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
    // Untick the source: nothing chosen covers nobody, and the editor says so; then pick one member.
    await panel.getByRole('checkbox', {name: '来源：所有成员'}).uncheck();
    await h.saved(d => d.rules[0].src.all === false && d.rules[0].dst.all === true, 'source not all');
    await panel.getByText('还没有选，这条规则不会生效').waitFor();
    await panel.getByRole('combobox', {name: '来源：添加成员或标签'}).selectOption({label: 'Home'});
    await h.saved(d => d.rules[0].src.members.join() === 'home' && d.rules[0].dst.all, 'source home');
    await label(page, 'ping').count().then(n => assert.equal(n, 0, 'one member to all members stays in the legend'));
    // Ticking all members again drops the chosen members.
    await panel.getByRole('checkbox', {name: '来源：所有成员'}).check();
    await h.saved(d => d.rules[0].src.all && !d.rules[0].src.members.length, 'source all again');
    await noOverflow(page);
    await h.close();
    // Phone, dark: the same editor in the bottom sheet, wide enough, nothing sideways.
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark});
      await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
      const overview = p.page.locator('.mesh-overview-sheet');
      await overview.getByRole('button', {name: '允许所有成员互相 ping'}).tap();
      await p.saved(d => d.rules.length === 1 && d.rules[0].src.all, 'phone ping for all');
      const box = p.page.getByRole('checkbox', {name: '目标：所有成员'});
      await box.waitFor();
      const sheet = p.page.locator('.mesh-sheet', {has: box});
      const row = await sheet.locator('.mesh-side-all').first().boundingBox();
      assert.ok(row.height >= 36, `all members row ${row.height}px tall`);
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'rule editor fits the width');
      await shot(p.page, `phone-rule-all-${dark ? 'dark' : 'light'}`);
      await noOverflow(p.page);
      await p.close();
    }
  });

  await t.test('events: repeats with when they began, a relay\'s flapping route as one row, raw tunnel errors in Chinese with the original aside; desktop and phone', {timeout: 150000}, async t => {
    const h = await setup(t, {noisy: true});
    const {page} = h;
    const texts = async locator => (await locator.allInnerTexts()).map(x => x.replace(/\s+/g, ' ').trim());
    const panel = inspector(page);
    await node(page, 'home').click();
    await panel.getByRole('tab', {name: '事件'}).click();
    const rows = panel.locator('.mesh-events > li');
    await rows.first().waitFor();
    let list = await texts(rows);
    // The tunnel error in Chinese, with the server's name; the original is folded under "原文".
    const tunnel = rows.filter({hasText: '隧道报错'});
    await tunnel.locator('.mesh-event-raw > summary').click();
    // A repeat merged by the controller: how many times and since when.
    // The standby's trial and the relay's confirmation are routine: folded, counted with their repeats.
    const note = panel.locator('.mesh-routine-note');
    await note.getByRole('button', {name: '显示'}).click();
    // Ids, keys and sequence numbers stay out of what is read; only the opened original has them.
    for (const e of await texts(rows.locator('.mesh-event-source, .sv-log-source, .mesh-event-detail, .mesh-event-repeat'))) assert.doesNotMatch(e, /\bc1\b|seq=|peer\(|E0pT|[0-9a-f]{8}-[0-9a-f]{4}/, e);
    await shot(page, 'desktop-events-member');
    // On a relay: four flips to Berlin read as one row; it opens to the changes.
    await node(page, 'lax').click();
    await panel.getByRole('tab', {name: '事件'}).click();
    const run = rows.filter({hasText: '换路'});
    await run.waitFor();
    await run.getByRole('button', {name: '展开 4 条'}).click();
    await rows.filter({hasText: '网段没走组网'}).filter({hasText: '网关网段 10.10.10.0/24 和本机所在的网络重叠，本机不经组网访问它们'}).waitFor();
    await shot(page, 'desktop-events-relay');
    await run.getByRole('button', {name: '收起'}).click();
    await h.close();
    // Phone, dark and light: the events tab in the bottom sheet; the original and the run open by tap.
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark, noisy: true});
      await node(p.page, 'home').tap();
      const sheet = p.page.locator('.mesh-inspect-sheet');
      await sheet.getByRole('tab', {name: '事件'}).tap();
      const raw = sheet.locator('.mesh-events > li', {hasText: '隧道报错'}).locator('.mesh-event-raw');
      await raw.locator('summary').tap();
      await raw.locator('code').waitFor();
      assert.ok((await raw.locator('summary').boundingBox()).height >= 32);
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'events fit the width');
      const sizes = await sheet.locator('.mesh-events').evaluate(el => [...el.querySelectorAll('span, small, time, summary')].filter(x => x.textContent.trim()).map(x => parseFloat(getComputedStyle(x).fontSize)));
      assert.ok(Math.min(...sizes) >= 12, `smallest text ${Math.min(...sizes)}px`);
      await shot(p.page, `phone-events-member-${dark ? 'dark' : 'light'}`);
      await sheet.getByRole('button', {name: '关闭'}).tap();
      await sheet.waitFor({state: 'hidden'});
      await p.page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
      await node(p.page, 'lax').tap();
      await sheet.getByRole('tab', {name: '事件'}).tap();
      const run = sheet.locator('.mesh-events > li', {hasText: '换路'});
      await run.getByRole('button', {name: '展开 4 条'}).tap();
      assert.ok((await run.getByRole('button', {name: '收起'}).boundingBox()).height >= 32);
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'relay events fit the width');
      await shot(p.page, `phone-events-relay-${dark ? 'dark' : 'light'}`);
      await noOverflow(p.page);
      await p.close();
    }
  });

  await t.test('security: a member’s end-to-end sessions, rejected signatures and latest security event as status rows; phone light and dark', {timeout: 120000}, async t => {
    const rowsOf = scope => label => scope.locator('.mesh-stats > div', {has: scope.page().locator('dt', {hasText: new RegExp(`^${label}$`)})});
    const check = async row => {
      // The newest of them, in Chinese, with what to do and when.
    };
    const h = await setup(t, {secure: true});
    await node(h.page, 'home').click();
    await inspector(h.page).locator('.mesh-stats dt', {hasText: '端到端会话'}).waitFor();
    await check(rowsOf(inspector(h.page)));
    // The events tab reads them in Chinese too.
    await inspector(h.page).getByRole('tab', {name: '事件'}).click();
    await shot(h.page, 'desktop-security-events');
    await inspector(h.page).getByRole('tab', {name: '状态'}).click();
    await shot(h.page, 'desktop-security-status');
    await h.close();
    // A member whose node reports none of it: no such rows.
    const plain = await setup(t);
    await node(plain.page, 'home').click();
    await inspector(plain.page).locator('.mesh-stats dt', {hasText: 'CPU'}).waitFor();
    await plain.close();
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark, secure: true});
      await node(p.page, 'home').tap();
      const sheet = p.page.locator('.mesh-inspect-sheet');
      await sheet.locator('.mesh-stats dt', {hasText: '最近安全事件'}).waitFor();
      await check(rowsOf(sheet));
      await sheet.locator('.mesh-stats').scrollIntoViewIfNeeded();
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'status rows fit the width');
      // Sized as the rows above them: values at least 12px, labels and notes as theirs.
      const sizes = await sheet.locator('.mesh-stats').evaluate(el => [...el.children].map(row => ['dt', 'dd > span', 'dd > small'].map(q => row.querySelector(q)?.textContent.trim() ? parseFloat(getComputedStyle(row.querySelector(q)).fontSize) : null)));
      const platform = sizes[0];
      for (const [dt, value, note] of sizes.slice(-3)) assert.ok(dt === platform[0] && value >= 12 && (note === null || note === dt), JSON.stringify(sizes));
      // The warning reads against the sheet in either theme.
      const [fg, bg] = await sheet.evaluate(el => [getComputedStyle(el.querySelector('.mesh-stats small.ob-warn')).color, getComputedStyle(el).backgroundColor]);
      assert.notEqual(fg, bg);
      await shot(p.page, `phone-security-status-${dark ? 'dark' : 'light'}`);
      await noOverflow(p.page);
      await p.close();
    }
  });
});
