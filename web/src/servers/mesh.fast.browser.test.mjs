// Fast ports in Chromium: declared in 出口和代理 (node, port, protocol, who may use it), the channels each user keeps
// to it with their paths, the settings (relay port, path count), a node's fast relay section (its fast ports, relay port,
// capture, channels), the relay port's external mapping, nothing left of the old participation switch and fast
// addresses, and a phone at 390px in light and dark without overflow.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, memberOf, noOverflow, shot, startMeshBrowser} from './testing/meshBrowser.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});

// Home's channel to LAX: the path in use through Berlin, a direct one down, a third still tested.
const homeChannel = () => ({dest: 'lax', ready: true, using: 0, opened: Date.now() - 600000, lastUse: Date.now() - 5000, streams: 12, fallbacks: 1, paths: [
  {hops: ['berlin', 'lax'], fixed: '', state: 'ready', since: Date.now() - 300000, rttMs: 142.4, error: ''},
  {hops: ['lax'], fixed: '', state: 'down', since: Date.now() - 60000, rttMs: null, error: 'dial tcp 198.51.100.3:52003: i/o timeout'},
  {hops: ['berlin', 'lax'], fixed: 'p1', state: 'untested', since: Date.now(), rttMs: null, error: ''}]});
const homeFast = extra => ({port: 0, listenError: '', capture: {mode: 'nft', active: false, udp: false, error: 'nft not found', missing: 'nft'},
  channels: [homeChannel()], entry: {active: 2, opened: 13, failed: 0, bytesIn: 8 << 20, bytesOut: 2 << 20}, transit: {active: 0, opened: 0, failed: 0, bytesIn: 0, bytesOut: 0},
  exit: {active: 0, opened: 0, failed: 0, bytesIn: 0, bytesOut: 0}, peers: [{peer: 'berlin', target: '198.51.100.6:52003', viaMesh: false, ready: 4, rttMs: 38.5, down: false, error: ''},
    {peer: 'lax', target: '100.96.0.2:52003', viaMesh: true, ready: 2, rttMs: 141.2, down: false, error: ''}],
  headerErrors: 0, refusedEntry: 1, refusedExit: 0, ...extra});

async function addFastPort(page, h, {node = 'LAX', port = '48888'} = {}) {
  await tab(page, '出口和代理').click();
  await page.getByRole('button', {name: '＋ 新建快速端口'}).click();
  const dialog = page.locator('.mesh3-dialog, .mesh-sheet').filter({hasText: '新建快速端口'});
  await dialog.locator('.m3-node-select').first().click();
  await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: node}).click();
  await dialog.getByRole('textbox', {name: '快速端口'}).fill(port);
  await dialog.getByText(`100.96.0.2:${port}`).waitFor();
  await dialog.getByRole('button', {name: '添加'}).click();
  return h.saved(d => d.fastPorts?.[0]?.serverId === 'lax' && d.fastPorts[0].port === Number(port), 'fast port added');
}

test('mesh fast ports: declare, who uses them and how, settings, node section, phone light & dark', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);

  await t.test('desktop: a fast port declared in 出口和代理, its users and paths, settings, node sections', {timeout: 150000}, async t => {
    const h = await setup(t, {v3: true});
    const {page, mesh} = h;

    // Settings: how many paths a channel keeps, and the relay port; no fast address range any more.
    await tab(page, '设置').click();
    const paths = page.getByRole('spinbutton', {name: '快速通道路径条数'});
    assert.equal(await paths.inputValue(), '3');
    assert.equal(await page.getByRole('textbox', {name: '快速网段'}).count(), 0);
    await paths.fill('2');
    await page.getByRole('button', {name: '保存到草稿'}).click();
    await h.saved(d => d.settings?.fast?.paths === 2 && d.settings.fast.port === 52003, 'fast settings saved');

    // A fast port on LAX, open to everyone; Home has a channel to it by the time the list comes back.
    mesh.nodes.home.summary.fast = {channels: [homeChannel()]};
    const body = await addFastPort(page, h);
    assert.deepEqual(body.fastPorts[0], {serverId: 'lax', port: 48888, proto: 'any', src: {all: true, allSubnets: true, members: [], tags: [], prefixes: []}, note: ''});
    const row = page.locator('.m3-rows-fast li', {hasText: 'LAX'});
    await row.getByText('100.96.0.2:48888').waitFor();
    await row.getByText('所有成员和共享局域网').waitFor();
    await row.getByText('Home 走 Home → Berlin → LAX · 142 ms').waitFor();
    await noOverflow(page);

    // Its dialog: Home among its users, the paths unfolded with the one in use and why another is down.
    await row.locator('.m3-row').click();
    const dialog = page.locator('.mesh3-dialog').filter({hasText: '快速端口 LAX :48888'});
    await dialog.getByText('谁在用').waitFor();
    await dialog.getByRole('button', {name: /Home/}).click();
    await dialog.locator('.m3-fast-path', {hasText: '在用'}).getByText('Home → Berlin → LAX').waitFor();
    await dialog.getByText('dial tcp 198.51.100.3:52003: i/o timeout').waitFor();
    await dialog.locator('.m3-fast-path').nth(2).getByText('自建路线').waitFor();
    await dialog.getByText('走快速通道 12 次 · 回落加密组网 1 次', {exact: false}).waitFor();
    // Only Home may use it: its own rule on LAX follows.
    await dialog.getByRole('radio', {name: '只允许指定的'}).click();
    await dialog.getByRole('combobox', {name: '谁能用：添加成员或标签'}).selectOption('members:home');
    await h.saved(d => d.fastPorts[0].src.all === false && d.fastPorts[0].src.members.join() === 'home', 'fast port limited to Home');
    await dialog.getByRole('radio', {name: 'UDP', exact: true}).click();
    await h.saved(d => d.fastPorts[0].proto === 'udp', 'fast port UDP only');
    await shot(page, 'desktop-fast-port');
    await dialog.getByRole('button', {name: '完成'}).click();
    await page.locator('.m3-rows-fast li', {hasText: 'LAX'}).getByText('Home').first().waitFor();

    // Home's page: its channel, how it takes connections (no nft here), no switch or fast address of the old kind.
    mesh.extra.status = {fast: homeFast({tcpMem: {pageBytes: 4096, original: [9500, 12600, 19000], current: [9500, 12600, 19000], originalBytes: [38912000, 51609600, 77824000],
      currentBytes: [38912000, 51609600, 77824000], changed: false, limited: true, readOnly: true, container: true, pressures: 2, error: 'permission denied'}})};
    await tab(page, '节点').click();
    await page.locator('.m3-rows li', {hasText: 'Home'}).click();
    const fastSec = page.locator('#m3-sec-fast');
    await fastSec.getByText('本机没有 nft 命令，访问快速端口的连接走加密组网；装上 nftables 后自动接管').waitFor();
    await fastSec.getByText('TCP 内存偏小，速度和并发受限').waitFor();
    await fastSec.getByRole('button', {name: /到 LAX/}).click();
    await fastSec.locator('.m3-fast-path', {hasText: '在用'}).getByText('Home → Berlin → LAX').waitFor();
    await fastSec.getByText('走组网').waitFor();
    assert.equal(await fastSec.getByText('快速中转监听端口').count(), 0, 'a member with no fast port does not listen');
    assert.equal(await page.getByText('参与快速中转').count(), 0);
    assert.equal(await page.getByText('快速地址').count(), 0);
    // A fast port declared from the node's own page has the node chosen.
    await fastSec.getByRole('button', {name: '＋ 在这台声明快速端口'}).click();
    const fromNode = page.locator('.mesh3-dialog').filter({hasText: '新建快速端口'});
    await fromNode.locator('.m3-node-select').getByText('Home').waitFor();
    await fromNode.getByRole('button', {name: '取消'}).click();
    await noOverflow(page);

    // LAX listens on the relay port (a relay, and the fast port's node); its external mapping sits with the others.
    await fastSec.locator('.m3-fast-peers').getByRole('button', {name: 'LAX'}).click();
    await page.locator('#m3-sec-fast').getByText('快速中转监听端口').waitFor();
    await page.locator('#m3-sec-fast').getByText(':48888').first().waitFor();
    const netSec = page.locator('#m3-sec-net');
    await netSec.getByRole('radio', {name: /手填/}).click();
    const fastMap = netSec.getByRole('textbox', {name: '快速中转 对外端口'});
    await fastMap.fill('62013');
    await fastMap.press('Tab');
    await h.saved(d => memberOf(d, 'lax').publicPorts?.fast === 62013, 'LAX fast relay port mapped');
    await shot(page, 'desktop-fast-node');
    await h.close();
  });

  await t.test('phone 390px, light and dark: the fast port list, its sheet and a node section without overflow', {timeout: 150000}, async t => {
    for (const dark of [false, true]) {
      const h = await setup(t, {width: 390, height: 844, dark, v3: true});
      const {page, mesh} = h;
      mesh.nodes.home.summary.fast = {channels: [homeChannel()]};
      mesh.extra.status = {fast: homeFast()};

      await tab(page, '出口和代理').tap();
      await page.getByRole('button', {name: '＋ 新建快速端口'}).tap();
      const sheet = page.locator('.mesh-sheet, .mesh3-dialog').filter({hasText: '新建快速端口'});
      await sheet.locator('.m3-node-select').first().tap();
      await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: 'LAX'}).tap();
      await sheet.getByRole('textbox', {name: '快速端口'}).fill('48888');
      await sheet.getByRole('button', {name: '添加'}).tap();
      await h.saved(d => d.fastPorts?.[0]?.port === 48888, 'fast port added on a phone');
      const row = page.locator('.m3-rows-fast li', {hasText: 'LAX'});
      await row.getByText('Home 走 Home → Berlin → LAX · 142 ms').waitFor();
      await noOverflow(page);
      await row.locator('.m3-row').tap();
      const open = page.locator('.mesh-sheet, .mesh3-dialog').filter({hasText: '谁在用'});
      await open.getByRole('button', {name: /Home/}).tap();
      await open.getByText('dial tcp 198.51.100.3:52003: i/o timeout').waitFor();
      await noOverflow(page);
      await shot(page, `phone-fast-port${dark ? '-dark' : ''}`);
      await open.getByRole('button', {name: '完成'}).tap();

      await tab(page, '节点').tap();
      await page.locator('.m3-rows li', {hasText: 'Home'}).tap();
      const fastSec = page.locator('#m3-sec-fast');
      await fastSec.getByRole('button', {name: /到 LAX/}).tap();
      await fastSec.locator('.m3-fast-path').first().waitFor();
      await noOverflow(page);
      await shot(page, `phone-fast-node${dark ? '-dark' : ''}`);
      await h.close();
    }
  });
});
