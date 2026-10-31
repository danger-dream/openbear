// Server network in Chromium, layout only: addresses on the detail page, the test form.
// Sizes, overflow, scrolling, phone and dark theme. What the page asks the backend and what it
// shows is checked without a browser in net.dom.test.mjs. Page, fake backend and helpers:
// testing/netBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ADDRESSES, GB, GEO, MESH_ADDR, SERVERS, addresses, chromium, fakeMesh, fakeNet, noOverflow, press, probe, rowTexts, run, server, shot, shots, startNetBrowser, waitFor} from './testing/netBrowser.mjs';

test('server network layout: addresses on the detail page, the test form', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startNetBrowser(t);
  await t.test('addresses on the detail page: grouped by interface, tags, the exit behind NAT, copy, refresh; offline and too old', {timeout: 90000}, async t => {
    const h = await setup(t);
    const box = addresses(h.page);
    await box.locator('.net-iface').first().waitFor();
    const groups = await box.locator('.net-iface').evaluateAll(list => list.map(el => el.innerText.replace(/\s+/g, ' ').trim()));
    // Traffic abroad leaves from a proxy: that exit too, said so.
    // Addresses come after the system and the bill; the test card after the server's other cards, across the page.
    const order = await h.page.locator('.sv-detail:visible').locator('.sv-cards > section, section.net-probe').evaluateAll(list => list.map(el => el.querySelector('h3')?.textContent || el.getAttribute('aria-label')));
    const grid = await h.page.locator('.sv-detail:visible .sv-cards').first().boundingBox();
    const card = await probe(h.page).boundingBox();
    assert.ok(Math.abs(card.width - grid.width) < 2, 'the test card spans the grid');
    // A tap copies the address (without its prefix length) and says so.
    await box.getByRole('button', {name: /10\.10\.10\.251\/24/}).click();
    await h.page.locator('.el-message', {hasText: '已复制 10.10.10.251'}).waitFor();
    const reads = h.of('GET', /\/home\/net\/addresses$/).length;
    await box.getByRole('button', {name: '刷新'}).click();
    await waitFor(() => h.of('GET', /\/home\/net\/addresses$/).length === 2, 'a refresh');
    await shot(h.page, 'desktop-light-addresses');
    await h.close();

    // Too old a node, and an offline one (not even asked).
    const old = await setup(t, {tabs: ['old', 'win'], active: 'old'});
    await addresses(old.page).getByText('节点版本过旧，升级节点后可用').waitFor();
    await old.page.locator('.sv-tab', {hasText: 'Win-Box'}).click();
    const win = old.page.locator('.sv-detail').nth(1);
    await win.locator('section.net-addresses').getByText('节点离线').waitFor();
    await win.locator('section.net-probe').getByText('节点离线，上线后才能测试。').waitFor();
    // Starting a test on a node too old for it.
    await old.page.locator('.sv-tab', {hasText: 'Old-VPS'}).click();
    await probe(old.page).getByRole('combobox', {name: '目标'}).fill('example.com');
    await probe(old.page).locator('.net-go').click();
    await probe(old.page).getByText('节点版本过旧，升级节点后可用。').waitFor();
    await old.close();
  });

  await t.test('the form: TCPing needs a port, targets checked, ranges per mode, the controller naming a field, busy', {timeout: 90000}, async t => {
    const h = await setup(t);
    const root = probe(h.page);
    await root.locator('.net-form').waitFor();
    // Defaults: Ping. The detail page is wide: mode, target and start on one line, the options out under it, nothing to unfold.
    const mid = async sel => { const b = await root.locator(sel).first().boundingBox(); return b.y + b.height / 2; };
    const line = await Promise.all(['.net-modes', '.net-target input', '.net-go'].map(mid));
    assert.ok(Math.max(...line) - Math.min(...line) <= 3, `one line: ${line}`);
    const fields = await Promise.all(['次数', '间隔（毫秒）', '超时（毫秒）'].map(name => root.getByRole('textbox', {name}).boundingBox()));
    assert.ok(fields.every(b => Math.abs(b.y - fields[0].y) <= 2), 'the options on one line');
    await root.locator('.net-go').click();
    await root.getByText('请填写目标').waitFor();
    await root.getByRole('radio', {name: 'TCPing'}).click();
    const port = root.getByRole('textbox', {name: '端口'});
    await port.waitFor();
    await root.getByRole('combobox', {name: '目标'}).fill('example.com');
    await root.locator('.net-go').click();
    await root.getByText('请填写端口').waitFor();
    await port.fill('99999');
    await root.locator('.net-go').click();
    await root.getByText('端口应为 1–65535').waitFor();
    await root.getByRole('combobox', {name: '目标'}).fill('http://exa mple.com');
    await root.locator('.net-go').click();
    await root.getByText('只填主机名或 IP，例如 example.com 或 1.1.1.1').waitFor();
    await root.getByRole('combobox', {name: '目标'}).fill('例子.中国');
    await root.locator('.net-go').click();
    await root.getByText('中文域名请换成 xn-- 开头的 Punycode 形式，或直接填 IP').waitFor();
    // The port field goes with TCPing; trace counts rounds, up to 10.
    await root.getByRole('radio', {name: '路由追踪'}).click();
    await root.getByRole('combobox', {name: '目标'}).fill('example.com');
    await root.getByRole('textbox', {name: '轮数'}).fill('11');
    await root.getByRole('textbox', {name: '间隔（毫秒）'}).fill('50');
    await root.locator('.net-go').click();
    await root.getByText('轮数应为 1–10').waitFor();
    await root.getByText('应为 200–10000 毫秒').waitFor();
    // The suggestions: the other servers' public addresses, then (home is a member) the mesh addresses, named.
    await root.getByRole('combobox', {name: '目标'}).fill('');
    if (!(await h.page.locator('.ob-suggest-list').isVisible())) await root.getByRole('combobox', {name: '目标'}).press('ArrowDown');
    const list = h.page.locator('.ob-suggest-list');
    await list.waitFor();
    const opts = await list.locator('li').evaluateAll(l => l.map(li => li.innerText.replace(/\s+/g, ' ').trim()));
    await root.getByRole('combobox', {name: '目标'}).fill('lax');
    await list.locator('li', {hasText: '45.76.0.2'}).click();
    await shot(h.page, 'desktop-light-form');
    // The controller turns a field down, or the node is busy.
    await root.getByRole('textbox', {name: '轮数'}).fill('');
    await root.getByRole('textbox', {name: '间隔（毫秒）'}).fill('');
    await root.getByRole('radio', {name: 'Ping', exact: true}).click();
    await root.getByRole('combobox', {name: '目标'}).fill('busy.example');
    await root.locator('.net-go').click();
    await root.getByText('这台服务器上已经有 4 个测试在运行，请等其中一个结束后再试。').waitFor();
    // Family against the address: caught here for a literal, by the controller for a name; shown under the choice, the options opened.
    const details = root.locator('details.net-advanced');
    if (!(await details.evaluate(el => el.open))) await details.locator('summary').click();
    await root.getByRole('radio', {name: 'IPv4'}).click();
    const famError = root.locator('.net-family-field .ob-bad');
    await root.getByRole('combobox', {name: '目标'}).fill('2001:db8::5');
    const posts = h.of('POST', /\/net\/probes$/).length;
    await root.locator('.net-go').click();
    await root.getByRole('radio', {name: '自动'}).click();
    await root.getByRole('radio', {name: 'IPv4'}).click();
    await root.getByRole('combobox', {name: '目标'}).fill('v6only.example');
    await root.locator('.net-go').click();
    await famError.waitFor();
    await h.close();
  });
});
