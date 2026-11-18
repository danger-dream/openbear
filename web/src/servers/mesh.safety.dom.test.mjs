import test from 'node:test';
import assert from 'node:assert/strict';
import {memberOf, setup, until} from './testing/meshDom.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});
const side = (o = {}) => ({all: false, allSubnets: false, members: [], tags: [], prefixes: [], subnets: [], ...o});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
async function home(page) {
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  await page.locator('.m3-node h2', {hasText: 'Home'}).waitFor();
}

for (const inFlight of [false, true]) test(`old action keeps an edit whose save is ${inFlight ? 'in flight' : 'finished'}`, async t => {
  const h = await setup(t, {v3: true});
  const store = await import(`./mesh/meshStore.js?fresh=${h.fresh}`);
  await home(h.page);
  const old = structuredClone((await h.mesh.handle('GET', ''))[1]);
  const actionReply = deferred(), saveReply = deferred();
  const handle = h.mesh.handle;
  h.mesh.handle = async (...args) => {
    const answer = await handle(...args);
    if (inFlight && args[0] === 'PUT' && args[1] === '/draft') await saveReply.promise;
    return answer;
  };
  const action = store.act(() => actionReply.promise);
  const input = h.page.getByRole('textbox', {name: '添加标签'});
  await input.fill('new-tag'); await input.press('Enter');
  await h.saved(d => memberOf(d, 'home').tags.includes('new-tag'));
  if (!inFlight) await until(h.page, () => !store.mesh.saving, 'save returned');
  actionReply.resolve(old);
  await action;
  assert.ok(memberOf(store.mesh.work, 'home').tags.includes('new-tag'));
  saveReply.resolve();
  await until(h.page, () => !store.mesh.saving, 'save finished');
  assert.equal(store.unsaved(), false);
  // A later unrelated save cannot send the old action's tags back to the server.
  store.mesh.work.settings.mtu = 1300;
  await store.save();
  assert.ok(memberOf(h.mesh.doc, 'home').tags.includes('new-tag'));
  assert.equal(h.mesh.doc.settings.mtu, 1300);
  await h.close();
});

for (const previous of ['loaded', 'pending', 'error']) test(`diagnostic routes isolate nodes and generations (${previous})`, async t => {
  // This test supplies /routes itself. The shared routes:true fixture replaces
  // Berlin with SG and makes Home a relay, so keep the Home -> Berlin entry here.
  const h = await setup(t, {v3: true});
  assert.ok(memberOf(h.mesh.doc, 'home').entry.candidates.some(c => c.relay === 'berlin'));
  const a = deferred(), b = deferred();
  let askedA = false, askedB = false;
  const handle = h.mesh.handle;
  h.mesh.handle = async (method, path, body, query) => {
    if (method === 'GET' && path === '/routes') {
      const src = new URLSearchParams(query).get('src');
      const answer = [200, {ok: true, routes: [{dst: 'lax', hops: [{node: src, via: ''}, {node: 'lax', via: 'relay', rttMs: src === 'home' ? 111 : 222}], rttMs: src === 'home' ? 111 : 222}], subnets: []}];
      if (src === 'home') {
        askedA = true;
        if (previous !== 'loaded') await a.promise;
        return previous === 'error' ? [404, {ok: false, error: 'not_found'}] : answer;
      }
      if (src === 'berlin') { askedB = true; await b.promise; return answer; }
    }
    return handle(method, path, body, query);
  };
  await home(h.page);
  const diag = h.page.locator('#m3-sec-diag');
  await diag.getByRole('tab', {name: '路由', exact: true}).click();
  await until(h.page, () => askedA, 'A route requested');
  if (previous === 'loaded') await until(h.page, async () => (await diag.innerText()).includes('111 ms'), 'A route visible');
  await h.page.locator('#m3-sec-connect button[title="打开 Berlin"]').first().click();
  await h.page.locator('.m3-node h2', {hasText: 'Berlin'}).waitFor();
  await until(h.page, () => askedB, 'switch requests B without changing the tab');
  assert.ok(!(await diag.innerText()).includes('111 ms'));
  assert.equal(await diag.getByRole('button', {name: '图上看'}).count(), 0);
  a.resolve();
  await h.page.waitForTimeout(30);
  assert.ok(!(await diag.innerText()).includes('111 ms'));
  assert.ok(!(await diag.innerText()).includes('主控还不支持查看路由'));
  b.resolve();
  await until(h.page, async () => (await diag.innerText()).includes('222 ms'), 'B route visible');
  await h.close();
});

for (const width of [390, 1440]) test(`LAN summary identifies IPv4/IPv6 partial ACLs (${width})`, async t => {
  const h = await setup(t, {v3: true, width, init: m => {
    m.doc.members[0].gateway = {name: 'LAN', subnets: ['192.168.1.0/24', 'fd12:3456::/64'], masquerade: true};
    m.doc.rules = [
      {id: 'v4', src: side({members: ['home']}), dst: side({subnets: ['192.168.1.42/32']}), proto: 'tcp', ports: [[445, 445]], note: ''},
      {id: 'v6', src: side({members: ['lax']}), dst: side({subnets: ['fd12:3456::42/128']}), proto: 'udp', ports: [[53, 53]], note: ''},
    ];
  }});
  await tab(h.page, '共享局域网').click();
  const row = h.page.locator('.m3-rows-lans .m3-row', {hasText: 'LAN'});
  const text = await row.innerText();
  for (const value of ['Home', 'LAX', '仅 192.168.1.42/32', '仅 fd12:3456::42/128', '445', '53']) assert.ok(text.includes(value), text);
  assert.ok(!text.includes('谁都访问不了'));
  await h.close();
});

test('CAS conflict keeps local edits, never auto-retries with a newer poll token, and offers explicit reload', async t => {
  const h = await setup(t, {v3: true, width: 390, init: m => {
    m.doc.rules = [{id: 'old-acl', src: side({members: ['home']}), dst: side({members: ['lax']}), proto: 'icmp', ports: [], note: ''}];
  }});
  const store = await import(`./mesh/meshStore.js?fresh=${h.fresh}`);
  await home(h.page);
  store.mesh.work.settings.mtu = 1300; // still-local edit before another client revokes the ACL
  h.mesh.doc.rules = [];
  await store.refresh();
  const input = h.page.getByRole('textbox', {name: '添加标签'});
  await input.fill('local-tag'); await input.press('Enter');
  await until(h.page, () => store.mesh.conflict, 'conflict returned');
  assert.equal(store.mesh.work.rules.length, 1);
  assert.ok(memberOf(store.mesh.work, 'home').tags.includes('local-tag'));
  assert.equal(h.mesh.doc.rules.length, 0);
  assert.equal(store.unsaved(), true);
  const sent = h.puts().length;
  await store.refresh(); await store.save();
  assert.equal(h.puts().length, sent, 'no silent retry using another client\'s version');
  const alert = h.page.locator('.m3-failed[role=alert]');
  assert.ok((await alert.innerText()).includes('本地编辑已保留'));
  await alert.getByRole('button', {name: '载入最新草稿'}).click();
  await h.page.locator('.el-message-box').getByRole('button', {name: '载入最新草稿'}).click();
  await until(h.page, () => !store.mesh.conflict, 'reload accepted');
  assert.equal(store.mesh.work.rules.length, 0);
  assert.equal(store.unsaved(), false);
  await h.close();
});

test('toggling noTransit back to baseline clears conflict/unsaved despite key-order changes, hides expanded bar summary, and lists node link changes', async t => {
  const h = await setup(t, {v3: true});
  const store = await import(`./mesh/meshStore.js?fresh=${h.fresh}`);
  await tab(h.page, '节点').click();
  await h.page.locator('.m3-row', {hasText: 'Berlin'}).click();
  await h.page.locator('.m3-node h2', {hasText: 'Berlin'}).waitFor();

  const sw = h.page.getByRole('switch', {name: /只承载自己的流量/});
  await sw.click();
  await h.saved(d => memberOf(d, 'berlin').noTransit === true);
  await until(h.page, () => !store.mesh.saving && !store.unsaved(), 'noTransit=true saved');

  // Simulate another writer changing draftVersion right before user unchecks noTransit:
  h.mesh.doc.settings.mtu = 1360;
  await sw.click();
  await until(h.page, () => store.mesh.conflict, 'conflict triggered');
  // Conflict alert has no close trap button:
  assert.equal(await h.page.locator('.m3-failed[role=alert] button[aria-label="关闭提示"]').count(), 0);

  // Re-checking noTransit restores the exact baseline despite delete+re-add changing object key order:
  await sw.click();
  await until(h.page, () => !store.mesh.conflict && !store.unsaved() && store.mesh.work.settings.mtu === 1360, 'reverted to baseline and synced');

  // Inject a link.change involving berlin and check node right-column changes + expanded bar summary dedup:
  const handle = h.mesh.handle;
  const linkChange = '中转线路 Berlin ⇄ LAX：协议 TCP → TLS，应用后这条线路会重连';
  h.mesh.handle = async (...args) => {
    const ans = await handle(...args);
    if (args[0] === 'GET' && args[1] === '') {
      ans[1].draft = {dirty: true, changes: [{kind: 'link.change', a: 'berlin', b: 'lax', text: linkChange}]};
    }
    return ans;
  };
  await store.refresh(true);
  await until(h.page, () => h.page.locator('.mesh-bar .mesh-bar-summary').count().then(n => n === 1), 'summary shown when collapsed');
  assert.ok((await h.page.locator('.m3-node-side').innerText()).includes(linkChange));
  await h.page.locator('.mesh-bar').getByRole('button', {name: '查看全部'}).click();
  assert.equal(await h.page.locator('.mesh-bar .mesh-bar-summary').count(), 0, 'duplicate summary hidden when expanded');
  assert.ok((await h.page.locator('.mesh-bar-list').innerText()).includes(linkChange));
  await h.close();
});
