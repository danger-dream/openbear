// Terminal OSC 8 links without a browser: the tip names the real address and calls out a host the
// text pretends to be, a click or a tap always asks first, 打开 opens the real address and 取消
// nothing — in happy-dom with the xterm stand-in (testing/xtermFake.mjs renders OSC 8 links as
// spans and calls the pane's link handler). Pointing at the rendered text, where the tip and the
// box sit and their sizes on a phone stay in terminalLinks.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from './testing/pageDom.mjs';
import {network} from '../testing/fakeNetwork.mjs';
import {OUTPUT} from './testing/terminalLinksFixture.mjs';

async function open(t, {width, height, dark = false}) {
  const opened = [];
  const h = await mountPage(t, {width, height, dark, load: n => import(`./TerminalPane.vue?fresh=${n}`),
    root: (Pane, h) => ({render: () => h('div', {class: 'servers-ui instance-ui', style: 'position: fixed; inset: 0'}, [h('div', {class: 'tw-panes', style: 'position: absolute; inset: 0'}, [h(Pane, {serverId: 'srv', active: true})])])}),
    handle: async () => [404, {ok: false, error: 'not_found'}],
    before: () => {
      window.open = (...args) => { opened.push(args); return null; };
      network.onSocket = socket => { if (/\/api\/servers\/srv\/terminal/.test(socket.url)) { socket.receive(JSON.stringify({type: 'status', state: 'open'})); socket.receive(Buffer.from(OUTPUT)); } };
    }});
  await h.page.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent.includes('使用说明'));
  const link = text => h.page.locator('.xterm-rows .xterm-link', {hasText: text});
  return {...h, opened, link};
}

test('desktop: hover shows the real address and the mismatch; leaving hides it; the click asks first', async t => {
  const {page, opened, link, close} = await open(t, {width: 1280, height: 800});
  const tip = page.locator('.tw-link-tip');
  await link('paypal.com').hover();
  await tip.waitFor();
  assert.equal(await tip.locator('.tw-link-warn').innerText(), '显示的是 paypal.com，实际打开 evil.example');
  assert.equal(await tip.locator('.tw-link-address').innerText(), 'https://evil.example/login?next=%2F');
  assert.equal(await tip.locator('.tw-link-address b').innerText(), 'evil.example', 'the host stands out');
  await page.mouse.move(5, 790);
  await tip.waitFor({state: 'detached'});

  // A text that names no host: the address, no warning.
  await link('使用说明').hover();
  await tip.waitFor();
  assert.equal(await tip.locator('.tw-link-warn').count(), 0);
  assert.equal(await tip.locator('.tw-link-address').innerText(), 'https://docs.example.com/guide');
  assert.equal(await tip.locator('.tw-link-hint').innerText(), '点击后确认再打开');

  // The click still asks; nothing opens until 打开.
  await link('paypal.com').hover();
  await link('paypal.com').click();
  const dialog = page.locator('.tw-link-box');
  await dialog.waitFor();
  assert.equal(await tip.count(), 0, 'the tip goes when the confirmation opens');
  assert.match(await dialog.locator('.el-message-box__title').innerText(), /链接文字和实际地址不一致/);
  assert.match(await dialog.locator('.tw-link-warn').innerText(), /显示的文字指向 paypal\.com，实际会打开 evil\.example/);
  assert.equal(await dialog.locator('.tw-link-address').innerText(), 'https://evil.example/login?next=%2F');
  assert.deepEqual(opened, []);
  await dialog.getByRole('button', {name: '打开'}).click();
  await dialog.waitFor({state: 'detached'});
  assert.deepEqual(opened, [['https://evil.example/login?next=%2F', '_blank', 'noopener,noreferrer']]);
  await close();
});

test('phone (dark): a tap shows the real address and the warning before anything opens', async t => {
  const {page, opened, link, close} = await open(t, {width: 390, height: 760, dark: true});
  await link('paypal.com').tap();
  const dialog = page.locator('.tw-link-box');
  await dialog.waitFor();
  assert.match(await dialog.locator('.tw-link-warn').innerText(), /实际会打开 evil\.example/);
  assert.equal(await dialog.locator('.tw-link-address').innerText(), 'https://evil.example/login?next=%2F');
  await dialog.getByRole('button', {name: '取消'}).tap();
  await dialog.waitFor({state: 'detached'});
  assert.deepEqual(opened, [], 'cancel opens nothing');
  assert.equal(await page.locator('.tw-link-tip').count(), 0, 'no tip left behind after the tap');
  await close();
});
