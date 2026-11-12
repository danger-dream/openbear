// The two network lines on the server cards and 服务解锁 in the detail, in Chromium: the
// ASN line, the services' icons (grey where not served, a corner mark where served as another
// country, CN in red), a lookup of an older address left out, 重新检测, and on a phone in the dark
// theme nothing cut or scrolling sideways.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, tab, showTab} from './testing/serversBrowser.mjs';

const SERVICES = ['google', 'youtube', 'openai', 'anthropic', 'gemini', 'netflix', 'spotify', 'reddit', 'tiktok'];
const xtom = {country: 'DE', countryName: 'Germany', asn: 3214, org: 'xTom GmbH'};
// GeoIP puts Bravo in the Emirates; the user set Germany, which counts.
const all = (region, changes = {}) => Object.fromEntries(SERVICES.map(s => [s, {state: 'ok', region, note: '', ...changes[s]}]));
function tweak(items) {
  const bravo = items.find(i => i.id === 'srv-bravo').node;
  bravo.info = {...bravo.info, ipv6: '2a0e:6901:110::1'};
  bravo.network = {ips: [bravo.info.ipv4, bravo.info.ipv6], v4: {...xtom, country: 'AE'}, v6: {...xtom, country: 'AE'}};
  bravo.unlock = {at: Date.now() - 2 * 3600000, ips: [bravo.info.ipv4, bravo.info.ipv6], v4: all('DE', {netflix: {region: 'US'}}),
    v6: all('DE', {reddit: {state: 'blocked', region: '', note: 'datacenter'}})};
  const papa = items.find(i => i.id === 'srv-papa').node;
  papa.network = {ips: [papa.info.ipv4, ''], v4: {country: 'HK', countryName: 'Hong Kong', asn: 4134, org: 'China Telecom Global Limited, a very long operator name'}, v6: null};
  // 送中: Google serves it as China, so YouTube and Gemini refuse it.
  papa.unlock = {at: Date.now() - 600000, ips: [papa.info.ipv4, ''], v4: all('HK', {
    google: {region: 'CN'}, youtube: {state: 'blocked', region: 'CN'}, gemini: {state: 'blocked', region: 'CN'},
    anthropic: {state: 'error', region: '', note: 'challenge'}, netflix: {state: 'limited', note: 'originals'}, reddit: {state: 'blocked', region: '', note: 'datacenter'}})};
  // Looked up while it had another address.
  const delta = items.find(i => i.id === 'srv-delta').node;
  delta.network = {ips: ['192.0.2.1', ''], v4: xtom, v6: null};
  // A server in mainland China: checked once, but neither check is for it.
  const golf = items.find(i => i.id === 'srv-golf');
  Object.assign(golf, {region: 'CN', live: items.find(i => i.id === 'srv-delta').live});
  golf.profile.region = 'CN';
  golf.node = {...golf.node, online: true, unlock: {at: Date.now(), ips: [golf.node.info.ipv4, ''], v4: all('CN')},
    route: {at: Date.now(), ip: golf.node.info.ipv4, results: {'bj-ct': {line: '163', tier: 'plain', rttMs: 30, reached: true, error: '', hops: []}}}};
}
const card = (page, name) => page.locator('.pb-card').filter({hasText: name});
const icons = line => line.locator('.sv-svc').evaluateAll(els => els.map(el => [el.dataset.service, el.dataset.state, el.querySelector('b')?.textContent || '']));
// Both lines stay one line each, inside the card; the operator gives way first.
const fits = c => c.evaluate(el => {
  const box = el.getBoundingClientRect(), asn = el.querySelector('.pb-asn'), row = el.querySelector('.pb-unlock');
  const inside = [...el.querySelectorAll('.pb-unlock .sv-svc')].every(i => { const r = i.getBoundingClientRect(); return r.left >= box.left && r.right <= box.right; });
  return {asn: asn.getBoundingClientRect().height < 24, row: row.getBoundingClientRect().height < 24, inside, cut: asn.querySelector('.pb-asn-org').scrollWidth > asn.querySelector('.pb-asn-org').clientWidth};
});
const colour = (page, name) => page.evaluate(variable => {
  const probe = document.body.appendChild(Object.assign(document.createElement('i'), {style: `color: var(${variable})`}));
  const color = getComputedStyle(probe).color;
  probe.remove();
  return color;
}, name);
const danger = page => colour(page, '--ob-danger');

test('the ASN line, the unlock icons and 重新检测', {skip: !chromium, timeout: 240000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('desktop', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak});
    const bravo = card(h.page, 'Bravo-FRA');
    assert.equal(await bravo.locator('.pb-asn-org').innerText(), 'AS3214 xTom GmbH');
    assert.equal(await bravo.locator('.pb-asn').getAttribute('title'), 'IPv4 198.51.100.9 · AS3214 xTom GmbH\nIPv6 2a0e:6901:110::1 · AS3214 xTom GmbH\n点开看 BGP 上游');
    const line = bravo.locator('.pb-unlock');
    assert.deepEqual(await icons(line), SERVICES.map(s => [s, 'ok', s === 'netflix' ? 'US' : '']), 'served as the US from Germany: marked');
    assert.equal(await line.locator('[data-service=netflix]').getAttribute('title'),
      'Netflix\nIPv4：可用 · 解锁地区 美国（US），与 IP 所在地德国不同\nIPv6：可用 · 解锁地区 德国（DE）\n检测于 2 小时前');
    assert.match(await line.locator('[data-service=reddit]').getAttribute('title'), /IPv6：不可用（拒绝机房 IP）/);
    assert.equal(await line.locator('[data-service=netflix] b').evaluate(el => el.dataset.tone), 'warn');

    const papa = card(h.page, 'Papa-HK.T1');
    assert.deepEqual(await icons(papa.locator('.pb-unlock')), [['google', 'ok', 'CN'], ['youtube', 'blocked', 'CN'], ['openai', 'ok', ''], ['anthropic', 'error', ''],
      ['gemini', 'blocked', 'CN'], ['netflix', 'limited', ''], ['spotify', 'ok', ''], ['reddit', 'blocked', ''], ['tiktok', 'ok', '']]);
    const cn = papa.locator('.pb-unlock [data-service=google] b');
    assert.equal(await cn.evaluate(el => getComputedStyle(el).backgroundColor), await danger(h.page), 'CN in red');
    const grey = await papa.locator('[data-service=youtube] svg').evaluate(el => [getComputedStyle(el).fill, getComputedStyle(el).opacity]);
    const brand = await papa.locator('[data-service=tiktok] svg').evaluate(el => getComputedStyle(el).fill);
    assert.equal(grey[1], '0.4');
    assert.notEqual(grey[0], brand);
    assert.match(await papa.locator('[data-service=anthropic]').getAttribute('title'), /检测失败（被 Cloudflare 人机验证拦下）/);
    assert.deepEqual(await fits(papa), {asn: true, row: true, inside: true, cut: true});
    assert.equal(await card(h.page, 'Delta-JP').locator('.pb-asn').count(), 0, 'a lookup of another address is not shown');
    assert.equal(await card(h.page, 'Delta-JP').locator('.pb-unlock').count(), 0, 'not checked yet: no icons');
    const golf = card(h.page, 'GoldenCloud');
    assert.equal(await golf.locator('.pb-unlock').count() + await golf.locator('.sv-route-tag').count(), 0, 'nothing for a server in China');
    await shot(h.page, 'desktop-cards-unlock');

    await papa.click();
    const detail = h.page.locator('.sv-detail:visible');
    await showTab(detail, '网络');
    const block = detail.locator('[data-unlock-detail]');
    // Read once scrolled to.
    await block.scrollIntoViewIfNeeded();
    await block.locator('.su-grid li').first().waitFor();
    // The full check's list, the cards' nine with the cards' verdicts; Anthropic's could not tell, so the full check's stands.
    const nine = ['google', 'youtube', 'openai', 'anthropic', 'gemini', 'netflix', 'spotify', 'reddit', 'tiktok'];
    assert.deepEqual(await block.locator('li').evaluateAll((els, ids) => els.filter(el => ids.includes(el.dataset.service)).map(el => [el.dataset.service, el.querySelector('strong').textContent]), nine),
      [['youtube', '不可用'], ['netflix', '受限'], ['spotify', 'HK'], ['tiktok', 'HK'], ['openai', 'HK'], ['anthropic', 'ES'], ['gemini', '不可用'], ['google', 'CN'], ['reddit', '不可用']]);
    assert.equal(await block.locator('li').count(), 58);
    assert.match(await block.locator('.ix-card-head').innerText(), /服务解锁[\s\S]*检测于 \d 小时前 · 出口 西班牙[\s\S]*重新检测/);
    assert.equal(await detail.locator('.sv-ip[data-family=IPv4] [data-tone=bad]').innerText(), 'Google 判定 中国（送中）');
    await block.scrollIntoViewIfNeeded();
    await shot(h.page, 'desktop-detail-unlock');
    await block.getByRole('button', {name: '重新检测'}).click();
    await h.page.getByText('解锁检测完成').waitFor();
    assert.equal(h.calls.filter(c => c.path.endsWith('/unlock') && c.method === 'POST').length, 1);
    // The full check follows: under way, then its new list.
    await block.getByText('完整检测中…').waitFor();
    assert.equal(await block.getByRole('button', {name: '重新检测'}).isDisabled(), true);
    await block.locator('.ix-card-head', {hasText: '检测于 刚刚'}).waitFor({timeout: 15000});
    assert.equal(await block.getByText('完整检测中…').count(), 0);
    await noOverflow(h.page);

    // Bravo: the region the user set, not GeoIP's, is what the services are compared with.
    await tab(h.page, '探针').click();
    await card(h.page, 'Bravo-FRA').click();
    const berlin = h.page.locator('.sv-detail:visible');
    await berlin.locator('[data-unlock-detail]').scrollIntoViewIfNeeded();
    await berlin.locator('[data-unlock-detail] .su-grid li').first().waitFor();
    assert.deepEqual(await berlin.locator('.sv-ip[data-family=IPv4] .sv-ip-net > span').allInnerTexts(), ['AS3214 xTom GmbH · 德国', 'Google 判定 德国（DE）']);
    assert.deepEqual(await berlin.locator('[data-unlock-detail] strong[data-tone]').allInnerTexts(), ['US'], 'only Netflix, served as the US, is marked');
    // Its node runs no commands: the nine, and why.
    assert.equal(await berlin.locator('[data-unlock-detail] li').count(), 9);
    assert.match(await berlin.locator('[data-unlock-detail] .su-notice').innerText(), /关闭了命令执行，跑不了完整检测/);

    // A server in China: neither check shows, not even to start one.
    await tab(h.page, '探针').click();
    await card(h.page, 'GoldenCloud').click();
    const china = h.page.locator('.sv-detail:visible');
    await china.locator('[data-jump=latency]').waitFor();
    assert.equal(await china.locator('[data-unlock-detail], [data-route-detail]').count(), 0);
    await h.close();
  });

  await t.test('phone, dark', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, touch: true, tweak});
    const papa = card(h.page, 'Papa-HK.T1');
    assert.deepEqual(await fits(papa), {asn: true, row: true, inside: true, cut: true});
    assert.deepEqual(await fits(card(h.page, 'Bravo-FRA')), {asn: true, row: true, inside: true, cut: false});
    // Black brands turn light on the dark background.
    const tiktok = await papa.locator('[data-service=tiktok] svg').evaluate(el => getComputedStyle(el).fill);
    assert.equal(tiktok, 'rgb(236, 236, 236)');
    for (const name of ['youtube', 'gemini', 'reddit', 'anthropic']) {
      assert.equal(await papa.locator(`[data-service=${name}] svg`).evaluate(el => getComputedStyle(el).fill), await colour(h.page, '--ob-chat-subtle'), `${name} grey in the dark too`);
    }
    await papa.scrollIntoViewIfNeeded();
    await shot(h.page, 'phone-dark-cards-unlock');
    await noOverflow(h.page);

    await papa.click();
    await showTab(h.page.locator('.sv-detail:visible'), '网络');
    const block = h.page.locator('.sv-detail:visible [data-unlock-detail]');
    await block.scrollIntoViewIfNeeded();
    await block.locator('.su-grid li').first().waitFor();
    // Two to a row; the long group folded until opened.
    assert.equal(await block.locator('li').count(), 8 + 8 + 11);
    await block.getByRole('button', {name: '展开其余 31 项'}).click();
    assert.equal(await block.locator('li').count(), 58);
    const rows = await block.locator('[data-group=ai] li').evaluateAll(els => new Set(els.map(el => Math.round(el.getBoundingClientRect().top))).size);
    assert.equal(rows, 4, 'eight in four rows');
    await block.scrollIntoViewIfNeeded();
    await shot(h.page, 'phone-dark-detail-unlock');
    await noOverflow(h.page);
    await h.close();
  });
});
