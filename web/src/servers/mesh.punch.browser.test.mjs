// The mesh board in Chromium, layout only: punched direct links.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until} from './testing/meshBrowser.mjs';

test('mesh board layout: punched direct links', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('punched direct links: live state on the board and in the panel, no outside address needed, the new checks in place', {timeout: 120000}, async t => {
    const h = await setup(t, {punch: true});
    const {page} = h;
    const direct = (state) => page.locator(`.mesh-edge[data-kind=direct][data-live=${state}]`);
    const lineStyle = state => direct(state).locator('.mesh-edge-line').evaluate(el => { const s = getComputedStyle(el); return {dash: s.strokeDasharray, animation: s.animationName, width: parseFloat(s.strokeWidth)}; });
    // Three links, three states; Office–Cafe takes the worse end (Cafe is still punching while Office reports direct).
    await label(page, '直连 · 局域网 · 3.2 ms').waitFor();
    for (const state of ['direct', 'punching', 'relayed']) assert.equal(await direct(state).count(), 1, state);
    const [solid, punching, relayed] = [await lineStyle('direct'), await lineStyle('punching'), await lineStyle('relayed')];
    assert.ok(solid.dash === 'none' && solid.width >= 2.4, `direct is a solid highlight: ${JSON.stringify(solid)}`);
    assert.ok(punching.dash !== 'none' && punching.animation === 'mesh-punch', `punching is dashed and flows: ${JSON.stringify(punching)}`);
    assert.ok(relayed.dash !== 'none' && relayed.animation === 'none', `relayed is dotted: ${JSON.stringify(relayed)}`);
    // The state comes with GET /mesh: no member detail is read while nothing is selected.
    const details = () => h.calls.filter(c => c.method === 'GET' && /^\/api\/servers\/mesh\/members\/[\w-]+$/.test(c.path)).map(c => c.path.split('/').pop());
    // Hovering says why, as far as the summary knows.
    await shot(page, 'desktop-punch-light');

    // Selected relayed link: the label says why, the panel shows the live state and no dialer.
    await label(page, '走中转').click();
    // Selected: both ends' details are read for the reason, attempts and how long.
    await label(page, '走中转 · 打洞未成功，会定时重试').waitFor();
    const panel = inspector(page);
    await panel.getByText('自动打洞：两边都在防火墙后也能直连，打不通时走中转').waitFor();
    const facts = await panel.locator('.mesh-direct-live .mesh-stats').innerText();
    for (const want of [/状态\s*走中转/, /经由\s*中转/, /持续\s*5 分钟/, /尝试次数\s*4 次/, /最近错误\s*打洞超时/]) assert.match(facts, want);
    await panel.getByText('打洞未成功，会定时重试。').waitFor();
    await shot(page, 'desktop-punch-relayed');
    // The two ends disagree: the worse is shown, and both are named.
    await label(page, '打洞中').click();
    await label(page, '打洞中 · 第 3 次尝试').waitFor();
    await panel.getByText('正在打洞，第 3 次尝试。两端不一致，按较差的一端显示：Office 直连中，Cafe 正在打洞。').waitFor();
    await label(page, '直连 · 局域网 · 3.2 ms').click();
    await page.waitForFunction(() => /持续/.test(document.querySelector('.mesh-direct-live .mesh-stats')?.innerText || ''));
    // Deselected: the details stop; the board keeps the summary's state.
    await page.keyboard.press('Escape');
    const read = details().length;

    // Peers and events of a member.
    await node(page, 'home').click();
    await panel.getByRole('tab', {name: '对端'}).click();
    await panel.locator('.mesh-peer-direct[data-state=direct]', {hasText: '直连中 · 经由局域网 · 已 12 分钟'}).waitFor();
    await panel.getByRole('tab', {name: '事件'}).click();
    const events = await panel.locator('.mesh-events > li').allInnerTexts();
    await page.keyboard.press('Escape');

    // A new direct link between two members without an outside address: UDP, no complaint; NAS gets UDP listening.
    await dragPort(page, 'nas', 'home', 'top');
    await page.locator('.mesh-notice', {hasText: '已添加直连，并为 NAS 开启 UDP 监听'}).waitFor();
    let body = await h.saved(d => d.directLinks.length === 4, 'new direct link');
    assert.deepEqual(body.directLinks[3], {a: 'nas', b: 'home', transport: 'udp', dialer: 'a'});
    assert.equal(memberOf(body, 'nas').listen.udp, 51999);
    assert.deepEqual([memberOf(body, 'nas').publicEndpointAuto, memberOf(body, 'home').publicEndpointAuto], ['', '']);
    assert.equal(await title(page).innerText(), '直连');
    await panel.getByText('应用后在这里显示实时状态：直连中、正在打洞或走中转。').waitFor();
    await page.waitForTimeout(300);
    assert.equal(await panel.locator('.mesh-cand-note').count(), 0, 'nothing to fix');
    assert.equal(await label(page, '直连 · UDP').getAttribute('data-look'), null);
    assert.equal(await panel.getByRole('radio', {name: 'UDP'}).getAttribute('aria-checked'), 'true');
    // TCP keeps the dialer and needs an address from outside, as before.
    await panel.getByRole('radio', {name: 'TCP'}).click();
    await panel.getByText('主动方').waitFor();
    await panel.getByText('Home 没有对外地址，NAS 连不上它').waitFor();
    assert.equal(await panel.getByText('自动打洞').count(), 0);
    await panel.getByRole('radio', {name: 'UDP'}).click();
    await h.saved(d => d.directLinks[3].transport === 'udp' && d.directLinks.length === 4 && h.puts().length > 2, 'back to udp');
    await page.waitForTimeout(300);
    assert.equal(await panel.locator('.mesh-cand-note').count(), 0);
    await page.keyboard.press('Escape');

    // udp_not_listening: Cafe stops listening on UDP; both of its links say so in place, with the fix.
    await node(page, 'cafe').click();
    await panel.getByRole('tab', {name: '配置'}).click();
    await panel.getByRole('checkbox', {name: '监听 UDP'}).uncheck();
    await page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').nth(1).waitFor();
    assert.equal(await page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').count(), 2);
    const bad = page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').first();
    assert.equal(await bad.getAttribute('title'), '两端都要开启 UDP 监听才能直连');
    await bad.click();
    await panel.locator('.mesh-cand-note.is-error', {hasText: '两端都要开启 UDP 监听才能直连'}).waitFor();
    await panel.getByText('Cafe 没有开启 UDP 监听', {exact: true}).waitFor();
    await panel.getByRole('button', {name: '为 Cafe 开启 UDP 监听'}).click();
    await h.saved(d => memberOf(d, 'cafe').listen.udp === 51999 && d.directLinks.length === 4, 'listen fix');
    await page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').first().waitFor({state: 'detached'});
    await label(page, '走中转').waitFor();
    // punch_no_reflector: Berlin, the only relay on UDP, stops listening on it.
    await node(page, 'berlin').click();
    await panel.getByRole('tab', {name: '配置'}).click();
    await panel.getByRole('checkbox', {name: '监听 UDP'}).uncheck();
    await page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').nth(3).waitFor();
    const lost = page.locator('.mesh-edge-label[data-kind=direct][data-look=bad]').first();
    assert.equal(await lost.getAttribute('title'), '没有可用来打洞的中转（需要至少一台开启 UDP 监听的中转）');
    await lost.click();
    await panel.locator('.mesh-cand-note.is-error', {hasText: '没有可用来打洞的中转（需要至少一台开启 UDP 监听的中转）'}).waitFor();
    assert.equal(await page.locator('.mesh-edge-label[data-kind=entry][data-look=bad]').count(), 0, 'the entries use TCP');
    await h.close();

    // An older controller without the summary: the board reads the ends' details itself.
    const legacy = await setup(t, {punch: true, legacyDirect: true});
    await label(legacy.page, '走中转').waitFor();
    await label(legacy.page, '直连 · 局域网 · 3.2 ms').waitFor();
    await legacy.close();

    // Phone, dark: the same three states.
    const phone = await setup(t, {punch: true, width: 390, height: 844, dark: true});
    await label(phone.page, '直连 · 局域网 · 3.2 ms').waitFor();
    await label(phone.page, '打洞中').waitFor();
    await label(phone.page, '走中转').waitFor();
    await noOverflow(phone.page);
    // Home and Office sit side by side: their link bows out so the label covers neither node.
    const lanLabel = await label(phone.page, '直连 · 局域网 · 3.2 ms').boundingBox();
    for (const id of ['home', 'office', 'berlin', 'lax']) {
      const b = await node(phone.page, id).boundingBox();
      assert.ok(lanLabel.x + lanLabel.width <= b.x || lanLabel.x >= b.x + b.width || lanLabel.y + lanLabel.height <= b.y || lanLabel.y >= b.y + b.height, `label covers ${id}`);
    }
    for (const other of await phone.page.locator('.mesh-edge-label').all()) {
      const b = await other.boundingBox();
      const text = await other.innerText();
      if (text === '直连 · 局域网 · 3.2 ms') continue;
      assert.ok(lanLabel.x + lanLabel.width <= b.x || lanLabel.x >= b.x + b.width || lanLabel.y + lanLabel.height <= b.y || lanLabel.y >= b.y + b.height, `label covers ${text}: ${JSON.stringify([lanLabel, b])}`);
    }
    await shot(phone.page, 'phone-punch-dark');
    await phone.close();
  });
});
