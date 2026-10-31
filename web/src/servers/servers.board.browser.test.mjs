// The servers page in Chromium, layout only: board, detail, background jobs, join requests, cards.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: board, detail, background jobs, join requests, cards', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('desktop board: summary filters, update notice, sparklines, list view', {timeout: 90000}, async t => {
    const h = await setup(t);
    const stats = h.page.locator('.pb-stats');
    // Bills in dollars and euros add up in CNY; the breakdown is in the title.
    const spend = stats.locator('.pb-stat').filter({hasText: '每月费用'});
    const breakdown = await spend.getAttribute('title');
    const parts = breakdown.match(/¥(\d+\.\d\d)/g).slice(0, 3).map(v => Number(v.slice(1)));
    // Every cell sits in the middle of the bar, whether it has one line or two.
    const middles = await stats.locator('.pb-stat').evaluateAll(cells => cells.map(c => {
      const kids = [...c.children].map(k => k.getBoundingClientRect()), box = c.getBoundingClientRect();
      return Math.round((Math.min(...kids.map(k => k.top)) + Math.max(...kids.map(k => k.bottom))) / 2 - (box.top + box.height / 2));
    }));
    assert.ok(middles.every(d => Math.abs(d) <= 2), JSON.stringify(middles));
    await shot(h.page, 'desktop-summary');
    await stats.getByRole('button', {name: /在线/}).click();
    await stats.getByRole('button', {name: /在线/}).click();
    await stats.getByRole('button', {name: /30 天内到期/}).click();
    await h.page.getByRole('button', {name: '显示全部'}).click();
    const papa = h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'});
    await h.page.waitForFunction(() => document.querySelectorAll('.pb-spark path.is-rx').length >= 4, null, {timeout: 15000});
    await h.page.locator('.pb-notice').getByText('2 台服务器的节点可以更新到本地构建 0.13.1').waitFor();
    await shot(h.page, 'desktop-board');
    await h.page.locator('.pb-notice').getByRole('button', {name: '全部更新'}).click();
    await h.page.locator('.pb-notice').waitFor({state: 'detached'});
    await h.page.getByRole('button', {name: '列表视图'}).click();
    const row = h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'});
    await noOverflow(h.page);
    await shot(h.page, 'desktop-list');
    await row.click();
    await h.close();
  });

  await t.test('desktop detail: gauges, live charts, renew, logs and removing with uninstall', {timeout: 90000}, async t => {
    const h = await setup(t);
    await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.sv-chart svg').first().waitFor();
    // The cards in their order, each card's actions as buttons at its head's right.
    const cards = await detail.locator('.sv-cards > section, section.net-probe').evaluateAll(list => list.map(el => el.querySelector('h3')?.textContent || el.getAttribute('aria-label')));
    for (const head of await detail.locator('.ix-card-head:has(> .ix-text-button)').all()) {
      const [title, button] = await Promise.all([head.locator('h3').boundingBox(), head.locator('> .ix-text-button').last().boundingBox()]);
      const card = await head.boundingBox();
      assert.ok(Math.abs(button.x + button.width - (card.x + card.width)) <= 1 && Math.abs(button.y + button.height / 2 - (title.y + title.height / 2)) <= 4, 'at the head\'s right');
    }
    const logs = detail.locator('.sv-logs');
    await logs.getByRole('tab', {name: '终端记录'}).click();
    await logs.getByText('192.0.2.1 · 会话已结束').waitFor();
    await detail.evaluate(el => el.closest('.sv-scroll').scrollTo(0, 0));
    await shot(h.page, 'desktop-detail');
    await detail.locator('.ix-card').filter({hasText: '账单'}).getByRole('button', {name: '续费'}).click();
    await detail.locator('.ix-more').click();
    await h.page.getByRole('menuitem', {name: '移除节点…'}).click();
    const dialog = h.page.getByRole('dialog', {name: '移除节点'});
    await dialog.getByRole('button', {name: '移除'}).click();
    await dialog.waitFor({state: 'hidden'});
    await h.close();
  });

  await t.test('background jobs: list, output panel with earlier output, stop; phone sheet and no overflow', {timeout: 120000}, async t => {
    const withJobs = items => { items.find(i => i.id === 'local').live.features = ['fs.edit', 'jobs', 'exec.shell']; };
    const h = await setup(t, {tweak: withJobs});
    await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    const card = detail.locator('.sv-jobs');
    await card.locator('.sv-job-list > li').first().waitFor();
    assert.equal(await card.locator('.sv-job-stop').count(), 1);
    const rows = await card.locator('.sv-job-list > li').evaluateAll(list => list.map(r => Math.round(r.getBoundingClientRect().height)));
    assert.ok(rows.every(height => height <= 44), JSON.stringify(rows));
    await card.getByRole('button', {name: '查看任务 mh2k3j9a1b2c 的日志'}).click();
    const panel = h.page.locator('.sv-job-drawer');
    const output = panel.locator('.sv-job-log');
    await output.waitFor();
    await panel.getByRole('button', {name: '加载更早的输出'}).click();
    await h.page.waitForFunction(() => document.querySelector('.sv-job-log')?.textContent.length > 66000);
    await shot(h.page, 'desktop-job-log');
    await panel.getByRole('button', {name: '停止任务'}).click();
    await h.page.getByRole('dialog', {name: '停止后台任务'}).getByRole('button', {name: '停止'}).click();
    await card.locator('.sv-job-state').first().filter({hasText: '已停止'}).waitFor();
    assert.ok(h.calls.some(c => c.path === '/api/servers/local/jobs/mh2k3j9a1b2c/stop' && c.method === 'POST'));
    await h.page.keyboard.press('Escape');
    await panel.waitFor({state: 'hidden'});
    // A node build without background jobs gets no such card.
    await tab(h.page, '探针').click();
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    await h.page.locator('.sv-detail:visible .sv-gauge').first().waitFor();
    await h.close();

    for (const dark of [false, true]) {
      const p = await setup(t, {width: 375, height: 812, dark, tweak: withJobs});
      await p.page.getByRole('button', {name: '列表视图'}).click();
      await p.page.locator('.pl-row').filter({hasText: 'Edge-SG.Game'}).click();
      const jobs = p.page.locator('.sv-detail:visible .sv-jobs');
      await jobs.locator('.sv-job-list > li').first().waitFor();
      await jobs.scrollIntoViewIfNeeded();
      await noOverflow(p.page);
      // A long command stays on one line; the row stays compact and Stop is a separate, finger-sized button.
      const first = jobs.locator('.sv-job-list > li').first();
      assert.ok((await first.boundingBox()).height <= 64, String((await first.boundingBox()).height));
      const stop = await first.getByRole('button', {name: '停止'}).boundingBox();
      assert.ok(stop.height >= 40 && stop.width >= 44, JSON.stringify(stop));
      assert.ok((await first.locator('.sv-job-open').boundingBox()).height >= 44);
      await shot(p.page, dark ? 'phone-dark-jobs' : 'phone-jobs');
      await first.locator('.sv-job-open').click();
      const sheet = p.page.locator('.sv-job-drawer');
      await sheet.locator('.sv-job-log').waitFor();
      assert.ok(await sheet.evaluate(el => el.getBoundingClientRect().bottom >= innerHeight - 1 && el.getBoundingClientRect().top > 40), 'a bottom sheet');
      assert.ok(await sheet.locator('.sv-job-log').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'log lines wrap');
      await noOverflow(p.page);
      await shot(p.page, dark ? 'phone-dark-job-log' : 'phone-job-log');
      await sheet.locator('.el-drawer__close-btn').click();
      await sheet.waitFor({state: 'hidden'});
      await p.close();
    }
  });

  await t.test('detail: a node past the target is not offered an update; a downgrade is asked for and named', {timeout: 60000}, async t => {
    for (const [width, touch] of [[1440, false], [390, true]]) {
      const h = await setup(t, {width, height: 844, touch, tweak: items => {
        const delta = items.find(i => i.id === 'srv-delta');
        Object.assign(delta.node, {version: '0.14.0-dev.3', updateAvailable: false,
          update: {state: 'ahead', updatable: false, downgrade: true, missing: [], protocolOk: true, withdrawn: false, target: {version: '0.13.1', source: 'github', build: 'a1b2c3d4e5f6'}}});
        delta.updateResult = {ok: true, changed: true, confirmed: true, state: 'updated', from: '0.14.0-dev.3', to: '0.13.1', build: 'a1b2c3d4e5f6', downgrade: true};
      }});
      await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
      const detail = h.page.locator('.sv-detail:visible');
      await detail.getByText('比目标 0.13.1 新，不会自动降级').waitFor();
      await detail.locator('.ix-more').click();
      await h.page.getByRole('menuitem', {name: '降级节点到 0.13.1…'}).click();
      const dialog = h.page.locator('.el-message-box');
      await dialog.getByText('这是降级：「Delta-JP」的节点会从 0.14.0-dev.3 换成更旧的 0.13.1').waitFor();
      await noOverflow(h.page);
      await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-delta/node/update')), dialog.getByRole('button', {name: '降级'}).click()]);
      await h.page.getByText('已降级到 0.13.1，节点重新连上并报告运行新构建').waitFor();
      await shot(h.page, `node-downgrade-${width}`);
      await h.close();
    }
  });

  await t.test('registration code join pending: card tag, notice, keep / take over / reject, phone without overflow', {timeout: 90000}, async t => {
    const pending = (items, id, name) => {
      const clone = JSON.parse(JSON.stringify(items.find(i => i.id === 'srv-delta')));
      Object.assign(clone, {id, name});
      clone.node.joinPending = {serverId: 'srv-delta', name: 'Delta-JP', code: '新开的 VPS', atMs: Date.now()};
      items.push(clone);
    };
    for (const [width, touch] of [[1440, false], [390, true]]) {
      const h = await setup(t, {width, height: 844, touch, tweak: items => {
        pending(items, 'srv-new1', 'delta (2)'); pending(items, 'srv-new2', 'delta (3)'); pending(items, 'srv-new3', 'delta (4) 一个很长很长的主机名用来检查通知按钮的换行');
      }});
      const card = h.page.locator('.pb-card').filter({hasText: 'delta (2)'});

      // Keep: a server of its own.
      await card.click();
      let notice = h.page.locator('.sv-detail:visible [data-join-pending]');
      await notice.getByText('机器 ID 和主机名与「Delta-JP」相同').waitFor();
      await noOverflow(h.page);
      await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new1/join')),
        notice.getByRole('button', {name: '作为新服务器保留'}).click()]);
      await notice.waitFor({state: 'detached'});

      // Take over: confirmed first, then the server it looked like opens and this one goes.
      await tab(h.page, '探针').click();
      await h.page.locator('.pb-card').filter({hasText: 'delta (3)'}).click();
      notice = h.page.locator('.sv-detail:visible [data-join-pending]');
      await notice.getByRole('button', {name: '接管「Delta-JP」'}).click();
      const dialog = h.page.locator('.el-message-box');
      await dialog.getByText('「Delta-JP」改由这台机器的节点连接').waitFor();
      await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new2/join')), dialog.getByRole('button', {name: '接管'}).click()]);
      await h.page.locator('.sv-detail:visible').getByRole('heading', {name: /Delta-JP/}).waitFor();

      // Reject, with the long name: the buttons wrap inside the notice.
      await tab(h.page, '探针').click();
      await h.page.locator('.pb-card').filter({hasText: '一个很长很长的主机名'}).click();
      notice = h.page.locator('.sv-detail:visible [data-join-pending]');
      await noOverflow(h.page);
      const fits = await notice.evaluate(el => [...el.querySelectorAll('button')].every(b => b.getBoundingClientRect().right <= el.getBoundingClientRect().right + 1));
      assert.ok(fits, 'notice buttons stay inside the notice');
      if (touch) assert.ok((await notice.getByRole('button', {name: '拒绝'}).boundingBox()).height >= 32);
      await notice.getByRole('button', {name: '拒绝'}).click();
      await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new3/join')),
        h.page.locator('.el-message-box').getByRole('button', {name: '拒绝'}).click()]);
      await shot(h.page, `join-pending-${width}`);
      await h.close();
    }
  });

  await t.test('cards: spec icons, tags without billing, no empty expiry text, long-term plans, centred summary', {timeout: 60000}, async t => {
    const h = await setup(t, {tweak: items => {
      const bero = items.find(i => i.id === 'srv-bravo');
      bero.profile.group = '欧洲';
      bero.profile.billing = {price: 0, currency: '', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false};
      items.find(i => i.id === 'srv-delta').profile.billing.expiresAt = day(73000);
      Object.assign(items.find(i => i.id === 'local').profile, {group: '', billing: {price: 0, currency: '', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}});
    }});
    const card = name => h.page.locator('.pb-card').filter({hasText: name});
    const bero = card('Bravo-FRA');
    const delta = card('Delta-JP');
    for (const stat of await h.page.locator('.pb-stat').all()) assert.equal(await stat.evaluate(el => getComputedStyle(el).alignItems), 'center');
    // A card without tags keeps its lines level with its neighbours.
    const row = ['Edge-SG.Game', 'Papa-HK.T1', 'GoldenCloud-1212'];
    const level = async selector => new Set(await Promise.all(row.map(name => card(name).locator(selector).evaluate(el => Math.round(el.getBoundingClientRect().top)))));
    assert.equal((await level('.pb-spec')).size, 1);
    assert.equal((await level('.pb-foot')).size, 1);
    await shot(h.page, 'desktop-cards');
    await h.close();
    const phone = await setup(t, {width: 390, height: 844, tweak: items => Object.assign(items.find(i => i.id === 'local').profile, {group: '', billing: {price: 0, cycle: 30, expiresAt: ''}})});
    assert.equal(await phone.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).locator('.pb-tags').evaluate(el => getComputedStyle(el).display), 'none');
    await phone.close();
  });

});
