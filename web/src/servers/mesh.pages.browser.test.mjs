// The mesh pages in Chromium, where sizes are real: a node's page, its diagnostics tools.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, label, noOverflow, settled, shot, startMeshBrowser, texts, until} from './testing/meshBrowser.mjs';
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

      // Own routes, the node page's cards: each pair as wide as the section, every route's state at its
      // right edge, beside its chain where there is room, a short pair's as a long one's.
      h.mesh.doc.paths = [
        {id: 'p1', on: true, from: 'home', to: 'lax', via: ['berlin', 'r1', 'r2'], _states: {to: 'pinned', back: 'pinned'}},
        {id: 'p2', on: true, from: 'home', to: 'lax', via: ['berlin'], _states: {to: 'standby', back: 'standby'}},
        {id: 'p3', on: true, from: 'home', to: 'berlin', via: ['lax'], _states: {to: 'pinned', back: 'pinned'}}];
      await page.evaluate(async () => { const {refresh} = await import('/src/servers/mesh/meshStore.js'); await refresh(true); });
      const own = page.getByRole('region', {name: '自建路线'});
      await own.locator('.m3-rt-state').nth(2).waitFor();
      await own.scrollIntoViewIfNeeded();
      const list = await own.locator('.m3-rt-own').boundingBox();
      for (const card of await own.locator('.m3-rt-pair').all()) {
        const box = await card.boundingBox();
        assert.ok(Math.abs(box.width - list.width) <= 1, `a pair's card is ${box.width}px of ${list.width}px`);
      }
      const rights = [];
      for (const row of await own.locator('.m3-rt-list > li').all()) {
        const [chain, state] = await Promise.all([row.locator('.m3-rt-chain').boundingBox(), row.locator('.m3-rt-state').boundingBox()]);
        rights.push(state.x + state.width);
        if (width > 760) assert.ok(Math.abs(state.y + state.height / 2 - (chain.y + chain.height / 2)) < 6, 'a state beside its chain');
      }
      assert.ok(Math.max(...rights) - Math.min(...rights) <= 1, `states end at ${rights.map(Math.round)}`);
      // Every hop with its flag, the pair's names too.
      assert.equal(await own.locator('.m3-rt-pair').first().locator('.m3-rt-pair-h .m3-node-name .sv-flag').count(), 2);
      const chain = own.locator('.m3-rt-list > li').first().locator('.m3-rt-chain');
      assert.equal(await chain.locator('.m3-node-name').count(), 5);
      assert.equal(await chain.locator('.m3-node-name .sv-flag').count(), 5);
      await shot(page, `${name}-own`);
      await noOverflow(page);
      await h.close();
    });
  }
});

test('mesh settings: the notifications laid out on a desktop and a phone', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const [name, width, height] of [['desktop', 1440, 900], ['phone', 390, 844]]) {
    for (const dark of [false, true]) {
      await t.test(`${name}${dark ? ', dark' : ''}: every switch in reach beside its label, no overflow`, {timeout: 120000}, async t => {
        const h = await setup(t, {v3: true, width, height, dark});
        const {page} = h;
        await page.locator('.m3-tabs').getByRole('tab', {name: /^设置/}).click();
        const alerts = page.getByRole('region', {name: '通知'});
        await alerts.scrollIntoViewIfNeeded();
        const switches = alerts.getByRole('switch');
        assert.equal(await switches.count(), 10);
        for (let i = 0; i < 10; i++) {
          const box = await switches.nth(i).boundingBox();
          assert.ok(box.x >= 0 && box.x + box.width <= width && box.height >= 28, `switch ${i}: ${JSON.stringify(box)}`);
        }
        await shot(page, `${name}-${dark ? 'dark' : 'light'}-settings-alerts`);
        await noOverflow(page);
        await h.close();
      });
    }
  }
});

test('mesh pages: a node\'s earlier events on a desktop and a phone', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const [name, size] of [['desktop', {}], ['phone 390px', {width: 390, height: 844}]]) {
    await t.test(`${name}: the stored history loads page by page, the events held stay, no overflow`, {timeout: 120000}, async t => {
      const h = await setup(t, {v3: true, ...size});
      const {page} = h;
      await page.locator('.m3-tabs').getByRole('tab', {name: /^节点/}).click();
      await page.locator('.m3-row', {hasText: 'Home'}).click();
      await page.locator('.m3-node-nav button', {hasText: '诊断'}).click();
      const diag = page.locator('#m3-sec-diag');
      await diag.getByRole('tab', {name: '事件'}).click();
      const rows = diag.locator('.m3-nitems > li');
      await rows.first().waitFor();
      const held = await texts(rows);
      const total = async () => Number((await diag.locator('.el-pagination__total').textContent().catch(() => '')).match(/\d+/)?.[0] || await rows.count());
      const load = diag.getByRole('button', {name: '加载更早的事件'});
      // A finger's reach on the phone.
      if (size.width) assert.ok((await load.boundingBox()).height >= 24, `the button is ${(await load.boundingBox()).height}px tall`);
      await load.scrollIntoViewIfNeeded();
      await shot(page, `events-${name.split(' ')[0]}-before`);
      const asked = () => h.calls.filter(c => c.path === '/api/servers/mesh/members/home/events').map(c => Object.fromEntries(new URLSearchParams(c.query)));

      await load.click();
      await until(page, async () => asked().length === 1 && await total() > held.length, 'the first page of the history');
      assert.deepEqual(asked(), [{limit: '300'}]);
      // Those held, the history lacking them, are still on the first page, newest first among the stored.
      const first = await texts(rows);
      for (const text of held) assert.ok(first.includes(text), `${text} is gone after loading: ${first.join(' | ')}`);
      const afterFirst = await total();
      await diag.scrollIntoViewIfNeeded();
      await shot(page, `events-${name.split(' ')[0]}-loaded`);
      await noOverflow(page);

      // On from the last stored one: the 20 left, and no more after them.
      await load.click();
      await diag.getByText('没有更早的了（保留 30 天）').waitFor();
      const second = asked()[1];
      assert.equal(second.limit, '200');
      assert.equal(second.beforeSeq, String(1000 - 297));
      assert.ok(await total() > afterFirst, `${await total()} rows after the second page, ${afterFirst} before`);
      assert.equal(await load.count(), 0);
      await shot(page, `events-${name.split(' ')[0]}-all`);
      await noOverflow(page);
      await h.close();
    });
  }
});

test('mesh pages: why a route stays, the trend\'s flips, the routing settings and a link\'s cost on a desktop and a phone', {skip: !chromium, timeout: 400000}, async t => {
  const {setup} = await startMeshBrowser(t);
  const inView = async (locator, width, what) => {
    const box = await locator.boundingBox();
    assert.ok(box && box.x >= 0 && box.x + box.width <= width + 1, `${what}: ${JSON.stringify(box)}`);
  };
  for (const [name, width, height] of [['desktop', 1440, 900], ['phone', 390, 844]]) {
    for (const dark of [false, true]) {
      await t.test(`${name}${dark ? ', dark' : ''}: wrapped in place, no overflow`, {timeout: 120000}, async t => {
        const h = await setup(t, {v3: true, width, height, dark});
        const {page} = h, shotName = `${name}-${dark ? 'dark' : 'light'}`;
        h.mesh.setPath((src, dst) => ({hops: [{node: src, via: ''}, {node: 'berlin', via: 'entry', rttMs: 12}, {node: dst, via: 'relay', rttMs: 140}], rttMs: 152, stale: false,
          decision: {node: 'berlin', dest: dst, cost: 190.4, smoothed: 231.7, best: 'sg', bestCost: 160.2, bestSmoothed: 214.9, held: 'settling'}}));
        h.mesh.doc.members.push({...JSON.parse(JSON.stringify(h.mesh.doc.members[0])), serverId: 'sg', name: 'SG', address: '100.96.0.9'});
        await page.evaluate(async () => { const {refresh} = await import('/src/servers/mesh/meshStore.js'); await refresh(true); });
        await page.locator('.m3-tabs').getByRole('tab', {name: /^节点/}).click();
        await page.locator('.m3-row', {hasText: 'Home'}).click();
        // Why the relay keeps a route though another is cheaper, its smoothed costs beside the real ones.
        await page.locator('.m3-rt-why').waitFor();
        await page.locator('.m3-node-select', {has: page.getByRole('combobox', {name: '路线另一端'})}).locator('.el-select__wrapper').click();
        await settled(page);
        await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: 'LAX'}).first().click();
        await settled(page);
        const why = page.locator('.m3-rt-why');
        await until(page, async () => /便宜 30\.2 ms/.test(await why.innerText().catch(() => '')), 'the route to LAX');
        assert.match(await why.innerText(), /代价 190\.4（平滑 231\.7）；改走 SG 代价 160\.2（平滑 214\.9），便宜 30\.2 ms，没换：更好的路还在观察/);
        await why.scrollIntoViewIfNeeded();
        await inView(why, width, 'why the route stays');
        await shot(page, `routing-${shotName}-why`);
        await noOverflow(page);
        // The trend's title counts the routes that came back within five minutes.
        await page.locator('.m3-node-nav button', {hasText: '诊断'}).click();
        const flips = page.locator('#m3-sec-diag').getByText(/来回 1 次/);
        await flips.waitFor();
        await flips.scrollIntoViewIfNeeded();
        await settled(page);
        await inView(flips, width, 'the trend title');
        await shot(page, `routing-${shotName}-trend`);
        // The routing settings: the seven parameters, each box in reach. SG out of the draft again, no banner over them.
        h.mesh.doc.members.pop();
        await page.evaluate(async () => { const {refresh} = await import('/src/servers/mesh/meshStore.js'); await refresh(true); });
        await page.locator('.m3-tabs').getByRole('tab', {name: /^设置/}).click();
        await page.getByText('有 1 项更改未应用').waitFor({state: 'hidden'});
        const routing = page.getByRole('region', {name: '选路'});
        await routing.waitFor();
        const boxes = routing.getByRole('spinbutton');
        assert.equal(await boxes.count(), 7);
        for (let i = 0; i < 7; i++) {
          await boxes.nth(i).scrollIntoViewIfNeeded();
          await inView(boxes.nth(i), width, `routing box ${i}`);
        }
        await routing.scrollIntoViewIfNeeded();
        await shot(page, `routing-${shotName}-settings`);
        await noOverflow(page);
        await h.close();
      });
    }
    // The board's link panel: the measured cost worked out, routes going by the advertised one.
    await t.test(`${name}: a link's cost worked out on the board`, {timeout: 120000}, async t => {
      const h = await setup(t, {reach: true, width, height});
      const {page} = h;
      await label(page, '拨号 · 79 ms').click();
      const math = page.locator('.mesh-cost-math');
      await math.waitFor();
      assert.match(await math.innerText(), /^实测代价 = 延迟 79\.4 \+ 抖动 1\.2 × 2 \+ 丢包率 0% × 1000 \+ 每跳 2 ≈ 82。/);
      await math.scrollIntoViewIfNeeded();
      await inView(math, width, 'the cost worked out');
      await shot(page, `routing-${name}-link`);
      await noOverflow(page);
      await h.close();
    });
  }
});
