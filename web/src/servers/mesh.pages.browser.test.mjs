// The mesh pages in Chromium, where sizes are real: a node's page, its diagnostics tools.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, noOverflow, settled, shot, startMeshBrowser} from './testing/meshBrowser.mjs';
import {linesAtWork} from './testing/meshLinesFixture.mjs';

test('mesh pages: a node\'s page laid out', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const dark of [false, true]) {
    await t.test(`tools: the options out and the target as tall as the buttons beside it${dark ? ', dark' : ''}`, {timeout: 120000}, async t => {
      const h = await setup(t, {v3: true, dark});
      const {page} = h;
      await page.locator('.m3-tabs').getByRole('tab', {name: /^节点/}).click();
      await page.locator('.m3-row', {hasText: 'Home'}).click();
      await shot(page, `node-${dark ? 'dark' : 'light'}-top`);
      await page.locator('#m3-sec-lan').scrollIntoViewIfNeeded();
      await shot(page, `node-${dark ? 'dark' : 'light'}-lan`);
      await page.locator('.m3-node-nav button', {hasText: '诊断'}).click();
      const diag = page.locator('#m3-sec-diag');
      await diag.getByRole('tab', {name: '工具'}).click();
      await diag.locator('.net-target input').waitFor();
      assert.equal(await diag.locator('.net-advanced > summary').isVisible(), false);
      const height = async sel => (await diag.locator(sel).first().boundingBox()).height;
      const [modes, target, go] = [await height('.net-modes'), await height('.net-target input'), await height('.net-go')];
      assert.ok(Math.abs(target - go) <= 2 && Math.abs(target - modes) <= 2, `modes ${modes}, target ${target}, go ${go}`);
      await diag.scrollIntoViewIfNeeded();
      await shot(page, `node-${dark ? 'dark' : 'light'}-tools`);
      await noOverflow(page);
      // Lines: a member's relays on one line with their flags, the wide column theirs.
      await page.getByRole('button', {name: '返回节点列表'}).click();
      await page.locator('.m3-tabs').getByRole('tab', {name: /^线路/}).click();
      const entry = page.locator('.m3-rows-entries .m3-row', {hasText: 'Home'});
      await entry.waitFor();
      assert.ok((await entry.boundingBox()).height < 50, `the row is ${(await entry.boundingBox()).height}px tall`);
      assert.equal(await entry.locator('.m3-row-conn .sv-flag').count(), 2);
      await entry.scrollIntoViewIfNeeded();
      await shot(page, `lines-${dark ? 'dark' : 'light'}-entries`);
      // Connection limit dialog on desktop.
      await page.getByRole('button', {name: '＋ 新建限制'}).click();
      await page.locator('.mesh3-dialog').waitFor();
      await shot(page, `desktop-${dark ? 'dark' : 'light'}-limit-dialog`);
      await page.locator('.mesh3-dialog').getByRole('button', {name: '完成'}).click();
      await shot(page, `desktop-${dark ? 'dark' : 'light'}-lines`);
      await noOverflow(page);
      await h.close();
    });
    await t.test(`phone 390px: connection limits sheet, upstreams and auto links without overflow${dark ? ', dark' : ''}`, {timeout: 120000}, async t => {
      const h = await setup(t, {v3: true, lines: true, width: 390, height: 844, dark});
      const {page} = h;
      await page.getByRole('button', {name: '全部展开'}).click();
      await page.locator('.m3-chip', {hasText: 'Home'}).waitFor();
      await shot(page, `phone-${dark ? 'dark' : 'light'}-overview`);
      await noOverflow(page);

      // Node detail on phone: upstreams, direct switch, auto tag & reason.
      await page.locator('.m3-tabs').getByRole('tab', {name: /^节点/}).click();
      await page.locator('.m3-row', {hasText: 'Home'}).click();
      await page.locator('.m3-node-nav button', {hasText: '怎么连进组网'}).click();
      await page.locator('#m3-sec-connect').scrollIntoViewIfNeeded();
      await shot(page, `phone-${dark ? 'dark' : 'light'}-node-connect`);
      await noOverflow(page);

      // Lines & connection limit sheet on phone: input font-size >= 16px, no horizontal overflow.
      await page.locator('.m3-tabs').getByRole('tab', {name: /^线路/}).click();
      await page.getByRole('button', {name: '＋ 新建限制'}).click();
      const sheet = page.getByRole('dialog', {name: '连接限制'});
      await sheet.waitFor();
      const fontSize = await sheet.getByRole('textbox', {name: '限制名称'}).evaluate(el => parseFloat(getComputedStyle(el).fontSize));
      assert.ok(fontSize >= 16, `phone input font-size ${fontSize}px < 16px`);
      await shot(page, `phone-${dark ? 'dark' : 'light'}-limit-sheet`);
      await noOverflow(page);
      await sheet.getByRole('button', {name: '完成'}).click();
      await sheet.waitFor({state: 'hidden'});
      await shot(page, `phone-${dark ? 'dark' : 'light'}-lines`);
      await noOverflow(page);
      await h.close();
    });
    await t.test(`own routes desktop & phone 390px: the board's route, the editor, a node's routes and the best ten, the lines page${dark ? ', dark' : ''}`, {timeout: 180000}, async t => {
      const routes = () => [
        {id: 'p1', on: true, from: 'home', to: 'lax', via: ['berlin'], _states: {to: 'pinned', back: 'pinned'}},
        {id: 'p2', on: true, from: 'home', to: 'berlin', via: ['lax'], _states: {to: 'waiting', back: 'waiting'}},
      ];
      const best = () => ({
        routes: [{nodes: ['home', 'berlin', 'lax'], segments: [{a: 'home', b: 'berlin', exists: true, rttMs: 12.4}, {a: 'berlin', b: 'lax', exists: true, rttMs: 141.8}], rttMs: 154.2, own: 1},
          {nodes: ['home', 'lax'], segments: [{a: 'home', b: 'lax', exists: true, rttMs: 158.3, loss: 0.033}], rttMs: 158.3, own: null}],
        own: [{id: 'p1', rank: 1, on: true, nodes: ['home', 'berlin', 'lax'], segments: [], rttMs: 154.2, place: 1}]});
      const pinned = (src, dst) => ({hops: [{node: src, via: ''}, {node: 'berlin', via: 'entry', rttMs: 12}, {node: dst, via: 'relay', rttMs: 140}], rttMs: 152, stale: false,
        fixed: {id: 'p1', pathId: 'p1', back: false, rank: 1, nodes: [src, 'berlin', dst], state: 'pinned', since: Date.now() - 60000}});
      const unpinned = (src, dst) => ({hops: [{node: src, via: ''}, {node: 'sg', via: 'entry', rttMs: 38}, {node: dst, via: 'relay', rttMs: 160}], rttMs: 198, stale: false});
      for (const [form, width, height] of [['desktop', 1440, 900], ['phone', 390, 844]]) {
        const h = await setup(t, {v3: true, lines: true, width, height, dark});
        h.mesh.doc.relayLinks = {mode: 'custom', pairs: [{a: 'berlin', b: 'lax', transport: 'udp', dialer: 'a'}]};
        h.mesh.doc.paths = routes();
        // SG, a relay linked to nothing yet: a route through it lacks two links.
        h.mesh.doc.members.push({...JSON.parse(JSON.stringify(h.mesh.doc.members[0])), serverId: 'sg', name: 'SG', address: '100.96.0.9'});
        h.mesh.setBest(best);
        const {page} = h, name = `routes-${form}-${dark ? 'dark' : 'light'}`;
        await page.evaluate(async () => { const {refresh} = await import('/src/servers/mesh/meshStore.js'); await refresh(true); });
        const pick = async (label, node) => {
          await page.locator('.m3-node-select', {has: page.getByRole('combobox', {name: label})}).locator('.el-select__wrapper').click();
          await settled(page);
          await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: node}).first().click();
          await settled(page);
        };
        // The board's route: on an own route, then not, with the button to make it one.
        h.mesh.setPath(pinned);
        await pick('起点', 'Home');
        await pick('终点', 'LAX');
        await page.locator('.m3-ppanel', {hasText: '按自建路线第 1 条'}).waitFor();
        await shot(page, `${name}-board`);
        h.mesh.setPath(unpinned);
        await page.getByRole('button', {name: '交换起点和终点'}).click();
        await page.getByRole('button', {name: '交换起点和终点'}).click();
        await page.locator('.m3-ppanel').getByRole('button', {name: '加为自建路线'}).click();
        // The editor on Home's page, laid out as the route goes.
        const editor = page.getByRole('dialog', {name: '自己搭一条路线'});
        await editor.waitFor();
        await settled(page);
        await shot(page, `${name}-editor`);
        await noOverflow(page);
        await editor.getByRole('button', {name: '取消'}).click();
        await editor.waitFor({state: 'hidden'});
        // Home's routes: two pairs, then the best ten to LAX.
        const own = page.locator('.m3-rt-own');
        await own.scrollIntoViewIfNeeded();
        await pick('路线另一端', 'LAX');
        await page.getByRole('button', {name: '计算前 10 条'}).click();
        await page.locator('.m3-rt-best').waitFor();
        await page.locator('.m3-rt-pair').first().getByRole('button', {name: '编辑'}).click();
        await settled(page);
        await own.scrollIntoViewIfNeeded();
        await shot(page, `${name}-node`);
        await page.locator('.m3-rt-best').scrollIntoViewIfNeeded();
        await shot(page, `${name}-best`);
        await noOverflow(page);
        // The lines page lists every pair's, read only.
        await page.locator('.m3-tabs').getByRole('tab', {name: /^线路/}).click();
        const region = page.getByRole('region', {name: '自建路线'});
        await region.waitFor();
        await region.scrollIntoViewIfNeeded();
        await shot(page, `${name}-lines`);
        await noOverflow(page);
        await h.close();
      }
    });
  }
});

test('mesh pages: the lines page at work, its scrollbar at the window\'s edge', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const [width, height] of [[1440, 900], [390, 844]]) for (const dark of [false, true]) {
    const name = `lines-${width > 760 ? 'desktop' : 'phone'}-${dark ? 'dark' : 'light'}`;
    await t.test(name, {timeout: 120000}, async t => {
      const h = await setup(t, {v3: true, lines: true, width, height, dark});
      const {page} = h;
      linesAtWork(h.mesh);
      await page.reload();
      await page.locator('.m3-tabs').getByRole('tab', {name: /^线路/}).click();
      await page.locator('.m3-auto-log').first().waitFor();
      // The page scrolls, and its scrollbar is the window's right edge, not inset by the view's padding.
      const scroller = page.locator('.m3-page.is-scroll');
      const edge = await scroller.evaluate(el => ({right: el.getBoundingClientRect().right, scrolls: el.scrollHeight > el.clientHeight, bar: el.offsetWidth - el.clientWidth}));
      assert.ok(edge.scrolls, 'the lines page does not scroll');
      assert.ok(Math.abs(edge.right - width) <= 1, `the scroller ends at ${edge.right}px of ${width}px`);
      // Empty limits, own routes and rules take a line each.
      for (const label of ['连接限制', '自建路线', '线路规则']) {
        const box = await page.getByRole('region', {name: label}).boundingBox();
        assert.ok(box.height < (width > 760 ? 50 : 90), `${label} is ${box.height}px tall`);
      }
      await shot(page, `${name}-top`);
      await noOverflow(page);
      // The folded rest opens from its head, the word right after its title; a measured line's note keeps to one line.
      const act = await page.locator('.m3-rows-group-act').boundingBox();
      assert.ok(act.x < width / 2, `展开 is at ${act.x}px of ${width}px`);
      const note = await page.locator('.m3-rows-lines > li[data-group="auto"] .m3-line-note > span').boundingBox();
      assert.ok(note.height < 24, `the note is ${note.height}px tall`);
      await page.locator('.m3-auto-log', {hasText: '最近一次调整'}).getByRole('button', {name: '查看 2 对'}).click();
      await page.locator('.m3-rows-lines > li[data-group="auto"]:not(.m3-rows-group)').getByRole('button', {name: '查看 2 对', exact: true}).click();
      await page.locator('.m3-rows-lines').scrollIntoViewIfNeeded();
      await shot(page, `${name}-lines`);
      await noOverflow(page);
      await h.close();
    });
  }
});
