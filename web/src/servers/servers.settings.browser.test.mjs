// 服务器设置 as a side drawer (a bottom sheet on a phone), and the 三网回程 it switches: compact access
// points, the reminder chips clear of what is around them, the recent reminders on their own, the
// two check switches, and the route tags and table they show or hide. In Chromium, light and dark.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, showTab} from './testing/serversBrowser.mjs';
import {ADDRESSES} from './testing/netBackend.mjs';

const hop = (ttl, ip, asn, org, country, rttMs) => ({ttl, ip, asn, org, country, rttMs, reached: false});
const trace = (line, tier, rttMs, hops = []) => ({line, tier, rttMs, reached: true, error: '', hops});
const PREMIUM_HOPS = [hop(1, '203.0.113.254', 16276, 'OVH SAS', 'SG', 0.4), hop(2, '', null, '', '', null), hop(3, '59.43.182.101', 4809, '', 'CN', 38.2),
  hop(4, '59.43.246.230', 4809, '', 'CN', 41.0), {...hop(5, '220.181.0.1', 4847, 'China Networks Inter-Exchange', 'CN', 42.6), reached: true}];
// Not reaching its target, the trace runs on to its 30th hop with nothing answering.
const QUIET_HOPS = [hop(1, '203.0.113.254', 16276, 'OVH SAS', 'SG', 0.4), hop(2, '', null, '', '', null), hop(3, '', null, '', '', null),
  hop(4, '59.43.182.101', 4809, '', 'CN', 38.2), ...Array.from({length: 26}, (_, i) => hop(5 + i, '', null, '', '', null))];
function tweak(items) {
  const bravo = items.find(i => i.id === 'srv-bravo').node;
  bravo.route = {at: Date.now() - 5 * 3600000, ip: bravo.info.ipv4, results: {
    'bj-ct': trace('CN2 GIA', 'gold', 42.6, PREMIUM_HOPS), 'sh-ct': trace('CN2 GIA', 'gold', 38.1), 'gd-ct': trace('CN2 GT', 'silver', 51.9),
    'bj-cu': trace('9929', 'gold', 47.3), 'sh-cu': trace('', '', 49.0), 'gd-cu': {...trace('', '', null), error: 'busy', reached: false},
    'bj-cm': {...trace('CMI', 'silver', 61.2, QUIET_HOPS), reached: false}, 'sh-cm': trace('CMIN2', 'gold', 44.4), 'gd-cm': trace('CMNET', 'plain', 72.5)}};
}
const withAddresses = page => page.route(/\/addresses$/, route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, ...ADDRESSES.lax})}));
const tags = card => card.locator('.sv-route-tag').evaluateAll(els => els.map(el => [el.dataset.route, el.textContent, el.dataset.tier]));
// Every tag clear of the line above and below it.
const chipsClear = dialog => dialog.locator('.sv-chips-edit').evaluateAll(rows => rows.map(row => {
  const box = row.getBoundingClientRect(), chips = [...row.querySelectorAll('.sv-chip')].map(c => c.getBoundingClientRect());
  const field = row.closest('.ob-field'), prev = field.previousElementSibling?.getBoundingClientRect(), next = field.nextElementSibling?.getBoundingClientRect();
  return chips.every(c => c.top - box.top >= 2 && box.bottom - c.bottom >= 2) && (!prev || box.top - prev.bottom >= 8) && (!next || next.top - box.bottom >= 8);
}));

// 监测地址: each part fits; a row's name and the lists it is in on one line (the address beside them, or under them on a phone);
// the directory wider than a phone scrolls within itself.
async function library(page, panel, name) {
  await panel.getByRole('tab', {name: '监测地址'}).click();
  const lib = panel.locator('[data-latency-library]');
  await lib.getByRole('button', {name: /查看全部/}).click();
  const table = lib.locator('.sv-lib-table');
  assert.ok(await table.evaluate(el => el.getBoundingClientRect().right <= el.closest('.sv-set').getBoundingClientRect().right + 1), 'the directory in the panel');
  await shot(page, `${name}-library-three`);
  await noOverflow(page);
  const phone = name.startsWith('phone');
  for (const part of ['国内', '国外']) {
    await lib.getByRole('tab', {name: part}).click();
    const rows = await lib.locator('.sv-lib-list > li:has(.sv-lib-name)').evaluateAll(els => els.map(el => {
      const box = q => el.querySelector(q)?.getBoundingClientRect();
      const [n, h, t] = [box('.sv-lib-name'), box('.sv-lib-host'), box('.sv-lib-tags')];
      return {name: el.querySelector('.sv-lib-name').textContent, height: el.getBoundingClientRect().height, tagsBeside: Math.abs((t.top + t.bottom) / 2 - (n.top + n.bottom) / 2) <= 3,
        hostBeside: Math.abs((h.top + h.bottom) / 2 - (n.top + n.bottom) / 2) <= 3, hostUnder: h.top >= n.bottom - 1};
    }));
    for (const r of rows) {
      assert.ok(r.tagsBeside, `${r.name}: its lists beside its name`);
      assert.ok(phone ? r.hostUnder : r.hostBeside, `${r.name}: the address ${phone ? 'under' : 'beside'} it`);
      assert.ok(r.height <= (phone ? 54 : 40), `${r.name}: ${r.height}px`);
    }
    await shot(page, `${name}-library-${part === '国内' ? 'cn' : 'intl'}`);
    await noOverflow(page);
  }
  await lib.getByRole('tab', {name: '默认'}).click();
  const plain = lib.locator('.sv-lib-group[data-group=plain]');
  await plain.getByRole('button', {name: '修改'}).click();
  await plain.locator('.sv-lib-pick').waitFor();
  await plain.scrollIntoViewIfNeeded();
  await shot(page, `${name}-library-defaults`);
  await noOverflow(page);
  await lib.getByRole('tab', {name: '全国三网'}).click();
}

test('the settings drawer and the route check', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('desktop', {timeout: 120000}, async t => {
    const h = await setup(t, {tweak});
    const bravo = h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'});
    assert.deepEqual(await tags(bravo), [['ct', '电信 CN2 GT', 'silver'], ['cu', '联通 9929', 'gold'], ['cm', '移动 CMNET', 'plain']],
      'the worst city of each carrier; hidden or failed ones do not count');
    assert.equal(await bravo.locator('[data-route=cu]').getAttribute('title'), '联通回程\n北京：9929 · 47 ms\n上海：未知 · 49 ms\n广东：失败（节点忙，没能开始追踪）');

    await h.page.getByRole('button', {name: '设置'}).click();
    const drawer = h.page.getByRole('dialog', {name: '服务器设置'});
    await drawer.locator('.sv-set').waitFor();
    const box = await drawer.boundingBox();
    assert.ok(box.x + box.width >= 1439 && box.width <= 522 && box.height >= 899, 'a drawer on the right, full height');
    const rows = drawer.locator('.ap-row:not(.ap-builtin)');
    const heights = await rows.evaluateAll(els => els.map(el => el.getBoundingClientRect().height));
    assert.ok(heights.every(v => v <= 68), `each access point is one line and its notes: ${heights}`);
    assert.deepEqual(await rows.first().evaluate(el => [...el.children].map(c => c.className.split(' ')[0])), ['ap-default', 'ap-name', 'mono', 'ap-acts', 'ap-meta']);
    // Grouped under tabs: 接入 shows its two blocks, the others wait behind their tabs.
    assert.deepEqual(await drawer.getByRole('tab').allInnerTexts(), ['接入', '提醒', '检测', '数据', '延迟', '监测地址']);
    assert.deepEqual(await drawer.locator('.sv-set-head > h3:visible').allInnerTexts(), ['接入点', '节点与时区']);
    const footer = drawer.locator('.instance-form-footer');
    const atBottom = async () => (await footer.boundingBox()).y + (await footer.boundingBox()).height >= box.height - 2;
    assert.ok(await atBottom(), 'save and cancel stay at the bottom');
    await shot(h.page, 'desktop-settings-top');
    await drawer.getByRole('tab', {name: '提醒'}).click();
    assert.deepEqual(await drawer.locator('.sv-set-head > h3:visible').allInnerTexts(), ['提醒', '最近提醒']);
    assert.deepEqual(await chipsClear(drawer), [true, true]);
    await drawer.locator('.sv-recent').scrollIntoViewIfNeeded();
    assert.ok(await atBottom());
    // A short tab keeps them there too.
    await drawer.getByRole('tab', {name: '检测'}).click();
    assert.ok(await atBottom(), 'buttons on the bottom edge under a short tab');
    await shot(h.page, 'desktop-settings-bottom');
    await noOverflow(h.page);
    await library(h.page, drawer, 'desktop');
    await drawer.getByRole('tab', {name: '检测'}).click();

    // Route switched off: its tags and its card go, and nothing asks for it.
    await drawer.getByLabel('三网回程', {exact: false}).uncheck();
    await drawer.getByRole('button', {name: '保存'}).click();
    await drawer.waitFor({state: 'hidden'});
    assert.equal(await h.page.locator('.pb-card .sv-route-tag').count(), 0);
    assert.deepEqual(h.backend.settings.checks, {unlock: true, route: false, ipcheck: true});
    await h.close();
  });

  await t.test('detail: the 3×3 table and a route in a popover', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak});
    await withAddresses(h.page);
    await h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
    await showTab(h.page.locator('.sv-detail:visible'), '网络');
    const card = h.page.locator('.sv-detail:visible [data-route-detail]');
    await card.waitFor();
    // Above it 延迟与丢包 finds its height once read.
    await h.page.locator('.sv-detail:visible [data-jump=latency] .lt-target').first().waitFor();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.net-iface').first().waitFor();
    // An interface's addresses run on beside it, every interface's from the same edge.
    const rows = await detail.locator('.net-iface').evaluateAll(els => els.map(el => [...el.querySelectorAll('.net-addr')].map(a => [Math.round(a.getBoundingClientRect().top), Math.round(a.getBoundingClientRect().left)])));
    assert.equal(rows[0][1][0], rows[0][0][0], 'ens3: two addresses on one line');
    assert.equal(new Set(rows.map(r => r[0][1])).size, 1, 'the addresses line up');
    assert.deepEqual(await card.locator('tbody tr').evaluateAll(rows => rows.map(r => r.innerText.replace(/\s+/g, ' ').trim())),
      ['北京 CN2 GIA 43 ms 9929 47 ms CMI 61 ms', '上海 CN2 GIA 38 ms 未知 49 ms CMIN2 44 ms', '广东 CN2 GT 52 ms 失败 — CMNET 73 ms']);
    assert.match(await card.locator('.ix-card-head').innerText(), /三网回程[\s\S]*检测于 5 小时前 · IPv4[\s\S]*重新检测/);
    // Room under the table for the route to open below it.
    await card.evaluate(el => el.scrollIntoView({block: 'start'}));
    const tall = (await card.boundingBox()).height;
    const cell = card.locator('[data-route-cell=bj-ct]');
    await cell.click();
    const hops = h.page.locator('.sv-route-popper:visible [data-route-hops]');
    await hops.waitFor();
    assert.match(await hops.locator('p').first().innerText(), /北京电信\s*CN2 GIA\s*43 ms/);
    assert.deepEqual(await hops.locator('li').evaluateAll(els => els.map(el => el.innerText.replace(/\s+/g, ' ').trim())),
      ['1 203.0.113.254 AS16276 · OVH SAS · 新加坡 0.4 ms', '2 *', '3 59.43.182.101 AS4809 · 中国 38.2 ms', '4 59.43.246.230 AS4809 · 中国 41 ms',
        '5 220.181.0.1 AS4847 · China Networks Inter-Exchange · 中国 42.6 ms']);
    assert.equal((await card.boundingBox()).height, tall, 'the route opens over the page, the card stays as it was');
    assert.ok((await hops.boundingBox()).y >= (await cell.boundingBox()).y + (await cell.boundingBox()).height - 1, 'under its cell');
    assert.equal(await cell.getAttribute('aria-pressed'), 'true');
    await shot(h.page, 'desktop-detail-route');
    await h.page.keyboard.press('Escape');
    await hops.waitFor({state: 'hidden'});
    // A cell without hops says so; a click elsewhere closes it.
    await card.locator('[data-route-cell=gd-cu]').click();
    assert.match(await hops.innerText(), /广东联通\s*失败：节点忙，没能开始追踪[\s\S]*没有逐跳记录/);
    await card.locator('.ix-card-head h3').click();
    await hops.waitFor({state: 'hidden'});
    // Runs of hops that did not answer are a row each; after the last answer the route goes quiet.
    await card.locator('[data-route-cell=bj-cm]').click();
    await hops.locator('li').first().waitFor();
    assert.deepEqual(await hops.locator('li').evaluateAll(els => els.map(el => el.innerText.replace(/\s+/g, ' ').trim())),
      ['1 203.0.113.254 AS16276 · OVH SAS · 新加坡 0.4 ms', '2–3 * 2 跳没有响应', '4 59.43.182.101 AS4809 · 中国 38.2 ms', '5–30 * 之后 26 跳都没有响应']);
    assert.match(await hops.locator('p').first().innerText(), /北京移动\s*CMI\s*61 ms\s*未到达目标/);
    await shot(h.page, 'desktop-detail-route-quiet');
    await h.page.keyboard.press('Escape');
    await hops.waitFor({state: 'hidden'});
    assert.equal(await cell.getAttribute('aria-pressed'), 'false');
    await card.getByRole('button', {name: '重新检测'}).click();
    await h.page.getByText('三网回程检测完成').waitFor();
    assert.equal(h.calls.filter(c => c.path.endsWith('/route') && c.method === 'POST').length, 1);
    await noOverflow(h.page);
    await h.close();
  });

  await t.test('phone, dark', {timeout: 120000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, touch: true, tweak});
    await h.page.getByRole('button', {name: '更多'}).click().catch(() => {});
    const settings = h.page.getByRole('menuitem', {name: '设置'}).or(h.page.getByRole('button', {name: '设置'})).first();
    await settings.click();
    const sheet = h.page.getByRole('dialog', {name: '服务器设置'});
    await sheet.locator('.sv-set').waitFor();
    assert.ok(await sheet.evaluate(el => el.classList.contains('mesh-sheet')), 'the bottom sheet');
    const row = sheet.locator('.ap-row:not(.ap-builtin)').first();
    const parts = await row.evaluate(el => Object.fromEntries(['.ap-name', '.ap-url', '.ap-acts'].map(q => { const r = el.querySelector(q).getBoundingClientRect(); return [q, r.top + r.height / 2]; })));
    assert.ok(Math.abs(parts['.ap-name'] - parts['.ap-acts']) <= 2, 'name and actions on one line');
    assert.ok(parts['.ap-url'] > parts['.ap-name'], 'the address under them');
    await shot(h.page, 'phone-dark-settings');
    await sheet.getByRole('tab', {name: '提醒'}).click();
    assert.deepEqual(await chipsClear(sheet), [true, true]);
    await sheet.locator('.sv-recent').scrollIntoViewIfNeeded();
    await shot(h.page, 'phone-dark-settings-bottom');
    await noOverflow(h.page);
    const tabs = await sheet.locator('.sv-set-tabs > .sv-ranges').evaluate(el => ({fits: el.scrollWidth <= el.clientWidth + 1,
      lines: [...el.children].map(b => Math.round(b.getBoundingClientRect().height))}));
    assert.ok(tabs.fits && new Set(tabs.lines).size === 1, `six tabs on one line each: ${JSON.stringify(tabs)}`);
    await library(h.page, sheet, 'phone-dark');
    await sheet.getByRole('button', {name: '取消'}).click();
    await sheet.waitFor({state: 'hidden'});

    const bravo = h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'});
    await bravo.scrollIntoViewIfNeeded();
    await shot(h.page, 'phone-dark-card-route');
    await bravo.click();
    await showTab(h.page.locator('.sv-detail:visible'), '网络');
    const card = h.page.locator('.sv-detail:visible [data-route-detail]');
    await card.waitFor();
    await card.scrollIntoViewIfNeeded();
    assert.ok(await card.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the table fits the phone');
    await card.locator('[data-route-cell=bj-ct]').click();
    const route = h.page.getByRole('dialog', {name: '北京电信回程'});
    await route.locator('[data-route-hops] li').first().waitFor();
    assert.equal(await route.locator('[data-route-hops] li').count(), 5);
    assert.equal(await h.page.locator('.sv-route-popper:visible').count(), 0, 'no popover on a phone');
    assert.ok(await route.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the hops fit the sheet');
    await shot(h.page, 'phone-dark-detail-route');
    await route.getByRole('button', {name: '关闭'}).click();
    await route.waitFor({state: 'hidden'});
    await noOverflow(h.page);
    await h.close();
  });
});
