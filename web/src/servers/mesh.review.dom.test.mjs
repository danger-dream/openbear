// The mesh pages and the classic board in happy-dom, for what the 11-09 review of the mesh control plane found
// in everyday editing: links kept when every-pair links turn manual, a node page shown with another node's
// late answer, failures and field errors left unsaid, the access preview on part of a LAN, the way from a
// LAN to the rules, and the classic board taking keys while the servers page is put away. The fake
// controller is testing/meshBackend.mjs; helpers: testing/meshDom.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {label, memberOf, node, setup, texts, until} from './testing/meshDom.mjs';
import {member} from './testing/meshBackend.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});
const plain = list => list.map(x => x.replace(/\p{Regional_Indicator}/gu, '').trim());
const side = (o = {}) => ({all: false, allSubnets: false, members: [], tags: [], prefixes: [], subnets: [], ...o});
async function pickNode(page, name, node, group) {
  await page.locator('.m3-node-select', {has: page.getByRole('combobox', {name})}).locator('.el-select__wrapper').click();
  const pop = page.locator('.m3-node-pop:visible').locator('.el-select-group__wrap', {has: page.locator('.el-select-group__title', {hasText: group})});
  await pop.locator('.el-select-dropdown__item', {has: page.locator('.m3-ns', {hasText: new RegExp(`${node}$`)})}).click();
}
// The fake controller's answers, one kind changed: `wrap(answer, method, path)` gets the usual answer first.
function intercept(h, wrap) {
  const handle = h.mesh.handle;
  h.mesh.handle = async (method, path, body, query) => wrap(await handle(method, path, body, query), method, path);
}

test('every-pair links turned manual keep the link set by hand; a field then set on a link stays the user\'s', async t => {
  const h = await setup(t, {v3: true, init: mesh => {
    mesh.doc.members.push(member('old', 'Old-VPS', 'relay', {address: '100.96.0.4'}));
    mesh.doc.relayLinks = {mode: 'full', pairs: [{a: 'berlin', b: 'lax', transport: 'tcp', cost: 1, dialer: 'b', bias: 100}]};
  }});
  const {page} = h;
  await tab(page, '线路').click();
  await page.getByRole('radio', {name: /^手动/}).click();
  const draft = await h.saved(d => d.relayLinks.mode === 'custom', 'manual links');
  assert.deepEqual(draft.relayLinks.pairs, [{a: 'berlin', b: 'lax', transport: 'tcp', cost: 1, dialer: 'b', bias: 100},
    {a: 'berlin', b: 'old', transport: 'udp', cost: 1, dialer: 'a', auto: ['transport', 'dialer', 'bias']},
    {a: 'lax', b: 'old', transport: 'udp', cost: 1, dialer: 'a', auto: ['transport', 'dialer', 'bias']}]);
  // A protocol picked for a link the mesh works out is kept, not worked out again on saving.
  await page.locator('.m3-rows-lines .m3-row', {hasText: 'Old-VPS'}).first().click();
  await page.locator('.mesh-link-transport').getByRole('radio', {name: 'TLS'}).click();
  const picked = await h.saved(d => d.relayLinks.pairs.some(p => p.b === 'old' && p.transport === 'tls'), 'TLS picked');
  const p = picked.relayLinks.pairs.find(x => x.b === 'old' && x.transport === 'tls');
  assert.deepEqual(p.auto, ['dialer', 'bias']);
  await h.close();
});

test('a node\'s page opened from another\'s: the first node\'s late answer is not shown as this one\'s', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  let release, hold = false;
  const held = new Promise(resolve => { release = resolve; });
  intercept(h, async (answer, method, path) => { if (hold && method === 'GET' && path === '/members/home') await held; return answer; });
  await tab(page, '节点').click();
  hold = true;
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  await page.locator('.m3-node h2', {hasText: 'Home'}).waitFor();
  // Home's detail is still out; Berlin opened from its entry list answers first.
  await page.locator('#m3-sec-connect button[title="打开 Berlin"]').first().click();
  await page.locator('.m3-node h2', {hasText: 'Berlin'}).waitFor();
  const diag = page.locator('#m3-sec-diag');
  await until(page, async () => (await diag.innerText()).includes('还没有对端'), 'Berlin\'s detail');
  hold = false;
  release();
  await page.waitForTimeout(30);
  assert.ok((await diag.innerText()).includes('还没有对端'), 'Berlin\'s page keeps Berlin\'s detail');
  await h.close();
});

test('a save that fails says why with the way to save again; field errors are said at the field', async t => {
  const h = await setup(t, {v3: true, init: mesh => {
    mesh.doc.members[2].proxy = {exit: null, entry: {listen: 'local', address: '', port: 1080, default: '', strict: false}};
  }});
  const {page} = h;
  let mode = 'down';
  intercept(h, (answer, method, path) => {
    if (method !== 'PUT' || path !== '/draft' || !mode) return answer;
    if (mode === 'down') return [503, {ok: false, error: 'mesh_busy_test'}];
    return [400, {ok: false, error: 'mesh_invalid', errors: [{path: 'members[2].proxy.ports', error: 'invalid_proxy_ports', message: '一台最多 16 个代理端口'},
      {path: 'members[2].address', error: 'address_outside', message: '地址须在 100.96.0.0/16 内，且不在控制地址段 100.96.255.0/24 内'}]}];
  });
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  await page.locator('#m3-sec-net').getByRole('switch', {name: '监听 TCP'}).click();
  const alert = page.locator('.m3-failed[role=alert]');
  await alert.waitFor();
  assert.ok((await alert.innerText()).trim().length > 4);
  mode = '';
  await alert.getByRole('button', {name: '重新保存'}).click();
  await h.saved(d => memberOf(d, 'home').listen.tcp > 0, 'saved again');
  await until(page, async () => !(await alert.count()), 'the failure gone once saved');
  // Refused for two fields: each says its own reason where it is.
  mode = 'invalid';
  await page.locator('#m3-sec-net').getByRole('switch', {name: '监听 TCP'}).click();
  await until(page, async () => (await page.locator('#m3-sec-proxy .m3-fr-error').count()) > 0, 'the port\'s error');
  assert.deepEqual(await texts(page.locator('#m3-sec-proxy .m3-fr-error')), ['一台最多 16 个代理端口']);
  assert.ok((await texts(page.locator('#m3-sec-net .m3-fr-error'))).includes('地址须在 100.96.0.0/16 内，且不在控制地址段 100.96.255.0/24 内'));
  await h.close();
});

test('from a LAN on its gateway\'s page to the rules; the preview counts a rule on part of a LAN and a network holding a node', async t => {
  const h = await setup(t, {v3: true, init: mesh => {
    mesh.doc.members[0].gateway = {subnets: ['192.168.1.0/24'], masquerade: true};
    mesh.doc.rules = [{id: 'nas', src: side({members: ['home']}), dst: side({subnets: ['192.168.1.42/32']}), proto: 'tcp', ports: [[445, 445]], note: 'NAS'},
      {id: 'net', src: side({prefixes: ['100.96.0.0/30']}), dst: side({members: ['lax']}), proto: 'icmp', ports: [], note: ''}];
  }});
  const {page} = h;
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Berlin'}).click();
  await page.locator('#m3-sec-lan').getByRole('button', {name: '在访问规则里改'}).click();
  await page.locator('.m3-tabs [role=tab][aria-selected=true]', {hasText: '访问规则'}).waitFor();
  // Only the NAS of Berlin's LAN is open to Home: Home may reach it, that one address.
  await pickNode(page, '预览哪个节点或局域网', 'Berlin 的局域网', '共享局域网');
  const [, by] = await page.locator('.m3-preview-cols > div').all();
  const reachedBy = (await by.innerText()).replace(/\p{Regional_Indicator}/gu, '');
  assert.ok(reachedBy.includes('Home') && reachedBy.includes('仅其中 192.168.1.42/32') && !reachedBy.includes('谁都访问不了它'), reachedBy);
  // Home (100.96.0.3) is inside 100.96.0.0/30: both rules let it reach something.
  await pickNode(page, '预览哪个节点或局域网', 'Home', '节点');
  const [to] = await page.locator('.m3-preview-cols > div').all();
  assert.equal(await to.locator('.m3-rel').count(), 2);
  assert.ok(plain([await to.innerText()])[0].includes('LAX'));
  await h.close();
});

test('the classic board takes no Delete while the servers page is put away', async t => {
  const h = await setup(t, {lines: true, keepAlive: true});
  const {page} = h;
  await label(page, '2 · TCP').click();
  await page.locator('.mesh-inspector-col').getByText('删除连线').waitFor();
  const before = h.puts().length;
  await page.evaluate(() => window.__showServers(false));
  await page.locator('#other-page').waitFor();
  await page.keyboard.press('Delete');
  await page.keyboard.press('Backspace');
  await page.waitForTimeout(300);
  assert.equal(h.puts().length, before, 'no draft saved while the board is put away');
  // Back on screen, the same key deletes the selected link.
  await page.evaluate(() => window.__showServers(true));
  await node(page, 'home').waitFor();
  await page.keyboard.press('Delete');
  const saved = await h.saved(d => memberOf(d, 'home').entry.candidates.length === 1, 'the link deleted');
  assert.deepEqual(saved.members.find(m => m.serverId === 'home').entry.candidates.map(c => c.relay), ['berlin']);
  await h.close();
});

for (const width of [390, 1440]) test(`full-mode rules count implicit links and show their changes before applying (${width})`, async t => {
  const change = '中转线路 Berlin ⇄ LAX：协议 UDP → TCP、主动连的一端 Berlin → LAX、偏好 0 → +100ms，应用后这条线路会重连';
  const h = await setup(t, {v3: true, width, init: mesh => {
    mesh.doc.members.push(member('old', 'Old-VPS', 'relay', {address: '100.96.0.4'}));
    mesh.doc.relayLinks = {mode: 'full', pairs: []};
    mesh.doc.lineRules = [{id: 'l1', name: 'TCP 优先', on: true, applies: 'lines', a: {kind: 'any'}, b: {kind: 'any'}, order: ['tcp', 'udp'], dialer: null, bias: 100}];
    const handle = mesh.handle;
    mesh.handle = async (...args) => {
      const answer = await handle(...args);
      if (answer[1]?.auto) {
        answer[1].auto.pairs = Object.fromEntries(['berlin|lax', 'berlin|old', 'lax|old'].map(key => [key, {
          transport: {value: 'tcp', reason: '按规则「TCP 优先」'}, dialer: {value: 'b', reason: ''},
          bias: {value: 100, reason: '按规则「TCP 优先」'}, rules: ['l1'], implicit: true,
        }]));
        answer[1].draft = {dirty: true, changes: [{kind: 'lineRules', text: '修改线路规则'}, {kind: 'link.change', a: 'berlin', b: 'lax', text: change}]};
      }
      return answer;
    };
  }});
  const {page} = h;
  await tab(page, '线路').click();
  assert.match(await page.locator('.m3-list').innerText(), /作用于 3 条线路/);
  assert.equal(await page.locator('.m3-rows-lines .m3-row').count(), 3);
  for (const text of await texts(page.locator('.m3-rows-lines .m3-row'))) {
    assert.match(text, /TCP/);
    assert.match(text, /少走 \+100/);
    assert.match(text, /按规则/);
  }
  await page.locator('.mesh-bar').getByRole('button', {name: '查看全部'}).click();
  assert.ok((await texts(page.locator('.mesh-bar-list li'))).includes(change));
  await page.getByRole('button', {name: /^TCP 优先/}).click();
  assert.match(await page.locator('.m3-callout', {hasText: '现在作用于'}).innerText(), /现在作用于 3 条中转线路/);
  assert.equal(h.calls.filter(c => c.path.endsWith('/apply')).length, 0);
  await h.close();
});
