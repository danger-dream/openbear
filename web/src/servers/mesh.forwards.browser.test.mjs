// Port forwards in Chromium: made in 端口转发 (entry and port, protocol, target and the address it dials, line, PROXY
// protocol, a whitelist of networks and provinces), how one runs, a port another forward on the entry holds, the
// switches that change it, both ends' node sections, and a phone at 390px in light and dark without overflow.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, noOverflow, shot, startMeshBrowser} from './testing/meshBrowser.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});
const editor = (page, title) => page.locator('.mesh3-dialog, .mesh-sheet').filter({hasText: title});
const running = extra => ({forwards: [{id: 'fw1', listen: ['tcp [::]:443', 'udp [::]:443'], errors: [], active: 3, sessions: 1, opened: 10, refused: 2, failed: 1,
  bytesIn: 8 << 20, bytesOut: 64 << 20, lastError: '', lastErrorAt: 0, ready: 2, down: false, tunnelError: '', rttMs: 31.4, ...extra}]});

async function pick(page, dialog, nth, name, tap = false) {
  const select = dialog.locator('.m3-node-select').nth(nth);
  await (tap ? select.tap() : select.click());
  const item = page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: name});
  await (tap ? item.tap() : item.click());
  // The list fades out: the next picker's must not be taken for it.
  while (await page.locator('.m3-node-pop:visible').count()) await page.waitForTimeout(50);
}

test('mesh port forwards: made, run, changed, node sections, phone light & dark', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);

  await t.test('desktop: a forward from Home to LAX with a whitelist, its state, its switches and both node pages', {timeout: 150000}, async t => {
    const h = await setup(t, {v3: true});
    const {page, mesh} = h;

    await tab(page, '端口转发').click();
    await page.getByText('还没有端口转发').waitFor();
    await page.getByRole('button', {name: '＋ 新建端口转发'}).click();
    const dialog = editor(page, '新建端口转发');
    await pick(page, dialog, 0, 'Home');
    await dialog.getByRole('textbox', {name: '监听端口'}).fill('443');
    await pick(page, dialog, 1, 'LAX');
    assert.equal(await dialog.getByRole('textbox', {name: '目标地址'}).inputValue(), '127.0.0.1');
    await dialog.getByRole('textbox', {name: '目标端口'}).fill('8443');
    await dialog.getByRole('radio', {name: '快速通道'}).click();
    await dialog.getByText('快速通道：沿组网选出的最快路径逐跳直连').waitFor();
    await dialog.getByRole('radio', {name: 'v2'}).click();
    await dialog.getByRole('radio', {name: '只允许白名单'}).click();
    await dialog.getByText('还没填白名单：现在仍允许所有来源。').waitFor();
    const nets = dialog.getByRole('textbox', {name: '允许的 IP 或网段'});
    await nets.fill('1.2.3.4\nnot-an-ip');
    await nets.press('Tab');
    await dialog.getByText('这些不是 IP 或网段：not-an-ip').waitFor();
    assert.equal(await dialog.getByRole('button', {name: '添加'}).isDisabled(), true);
    await nets.fill('1.2.3.4\n112.115.147.0/24');
    await nets.press('Tab');
    await dialog.locator('.m3-fwd-sub .el-select').click();
    await page.locator('.el-select-dropdown__item:visible', {hasText: '广东省'}).click();
    await dialog.getByText('省份网段来自 ip2region').click();
    await dialog.getByText('IP、网段和省份任一匹配就放行').waitFor();
    await shot(page, 'desktop-forward-new');
    await dialog.getByRole('button', {name: '添加'}).click();
    const body = await h.saved(d => d.forwards?.length === 1, 'forward added');
    assert.deepEqual(body.forwards[0], {entry: 'home', listen: 443, proto: 'any', target: 'lax', host: '127.0.0.1', port: 8443, line: 'fast', proxyProtocol: 2,
      allow: {prefixes: ['1.2.3.4', '112.115.147.0/24'], provinces: ['广东省']}, enabled: true, note: ''});

    const row = page.locator('.m3-rows-forwards li', {hasText: 'Home'});
    await row.getByText(':443').waitFor();
    await row.getByText('LAX 本机 :8443').waitFor();
    await row.getByText('快速通道 · PROXY v2').waitFor();
    await row.getByText('2 个 IP 或网段 · 广东省').waitFor();
    await row.getByText('未应用').waitFor();
    // Running on Home: its connections and UDP clients in the list.
    mesh.nodes.home.summary.forward = running();
    await row.getByText('3 个连接 · 1 个 UDP 客户端').waitFor();
    await noOverflow(page);
    await shot(page, 'desktop-forwards');

    // Its dialog: how it runs; UDP alone takes PROXY protocol off; turned off it stays in the list.
    await row.locator('.m3-row').click();
    const open = editor(page, '端口转发 Home :443');
    await open.getByText('监听：tcp [::]:443、udp [::]:443').waitFor();
    await open.getByText('白名单拦下 2 次', {exact: false}).waitFor();
    await open.getByText('预备 2 条', {exact: false}).waitFor();
    // A fast forward says which line new connections take: the fast relay's path, or the mesh meanwhile.
    mesh.nodes.home.summary.forward = running({via: 'fast', rttMs: 6.2});
    await open.getByText('走快速通道 · 往返 6.2', {exact: false}).waitFor();
    mesh.nodes.home.summary.forward = running({via: 'mesh'});
    await open.getByText('快速通道还没通，新连接暂走组网').waitFor();
    await row.getByText('3 个连接 · 1 个 UDP 客户端 · 快速通道还没通，暂走组网').waitFor();
    mesh.nodes.home.summary.forward = running();
    await open.getByRole('radio', {name: 'UDP', exact: true}).click();
    await h.saved(d => d.forwards[0].proto === 'udp' && d.forwards[0].proxyProtocol === 0, 'UDP only, no PROXY protocol');
    assert.equal(await open.getByRole('radio', {name: 'v1'}).isDisabled(), true);
    await open.getByRole('switch', {name: '启用'}).click();
    await h.saved(d => d.forwards[0].enabled === false, 'forward turned off');
    await shot(page, 'desktop-forward-open');
    await open.getByRole('button', {name: '完成'}).click();
    await row.getByText('草稿停用 · 上报仍在监听').waitFor();
    await row.locator('.m3-row').click();
    await editor(page, '端口转发 Home :443').getByRole('switch', {name: '启用'}).click();
    await h.saved(d => d.forwards[0].enabled === true, 'forward on again');
    await editor(page, '端口转发 Home :443').getByRole('button', {name: '完成'}).click();

    // Another forward on Home's 443: refused as typed.
    await page.getByRole('button', {name: '＋ 新建端口转发'}).click();
    const second = editor(page, '新建端口转发');
    await pick(page, second, 0, 'Home');
    await second.getByRole('textbox', {name: '监听端口'}).fill('443');
    await second.getByRole('radio', {name: 'UDP', exact: true}).click();
    await second.getByText('Home 上已经有转发在用 443').waitFor();
    await second.getByRole('button', {name: '取消'}).click();

    // Home's page: the forward out of it, a port it could not take; LAX's: the forward into it.
    mesh.extra.status = {forward: running({errors: ['udp: listen udp :443: bind: address already in use'], listen: ['tcp [::]:443']})};
    await tab(page, '节点').click();
    await page.locator('.m3-rows li', {hasText: 'Home'}).click();
    const sec = page.locator('#m3-sec-forward');
    await sec.getByRole('button', {name: /:443 · UDP → LAX 本机 :8443/}).waitFor();
    await page.getByText('端口转发没有监听上：udp: listen udp :443: bind: address already in use').first().waitFor();
    await sec.getByRole('button', {name: '＋ 新建端口转发'}).click();
    await editor(page, '新建端口转发').locator('.m3-node-select').first().getByText('Home').waitFor();
    await editor(page, '新建端口转发').getByRole('button', {name: '取消'}).click();
    await noOverflow(page);
    await shot(page, 'desktop-forward-node');
    await page.getByRole('button', {name: '返回节点列表'}).click();
    await page.getByRole('button', {name: '打开 LAX 的详情'}).click();
    await page.locator('#m3-sec-forward').getByRole('button', {name: /Home :443 → 本机 :8443/}).waitFor();
    await h.close();
  });

  await t.test('phone 390px, light and dark: the list, the sheet and a node section without overflow', {timeout: 150000}, async t => {
    for (const dark of [false, true]) {
      const h = await setup(t, {width: 390, height: 844, dark, v3: true});
      const {page, mesh} = h;
      await tab(page, '端口转发').tap();
      await page.getByRole('button', {name: '＋ 新建端口转发'}).tap();
      const sheet = editor(page, '新建端口转发');
      await pick(page, sheet, 0, 'Home', true);
      await sheet.getByRole('textbox', {name: '监听端口'}).fill('443');
      await pick(page, sheet, 1, 'LAX', true);
      await sheet.getByRole('textbox', {name: '目标端口'}).fill('8443');
      await sheet.getByRole('radio', {name: '只允许白名单'}).tap();
      await sheet.getByRole('textbox', {name: '允许的 IP 或网段'}).fill('1.2.3.4');
      await sheet.getByRole('textbox', {name: '允许的 IP 或网段'}).blur();
      await noOverflow(page);
      await shot(page, `phone-forward-new${dark ? '-dark' : ''}`);
      await sheet.getByRole('button', {name: '添加'}).tap();
      await h.saved(d => d.forwards?.[0]?.listen === 443, 'forward added on a phone');
      mesh.nodes.home.summary.forward = running();
      const row = page.locator('.m3-rows-forwards li', {hasText: 'Home'});
      await row.getByText('3 个连接 · 1 个 UDP 客户端').waitFor();
      await noOverflow(page);
      await shot(page, `phone-forwards${dark ? '-dark' : ''}`);
      await row.locator('.m3-row').tap();
      const open = editor(page, '端口转发 Home :443');
      await open.getByText('监听：tcp [::]:443、udp [::]:443').waitFor();
      await noOverflow(page);
      await open.getByRole('button', {name: '完成'}).tap();

      await tab(page, '节点').tap();
      await page.locator('.m3-rows li', {hasText: 'Home'}).tap();
      await page.locator('#m3-sec-forward').getByRole('button', {name: /:443/}).waitFor();
      await noOverflow(page);
      await shot(page, `phone-forward-node${dark ? '-dark' : ''}`);
      await h.close();
    }
  });
});
