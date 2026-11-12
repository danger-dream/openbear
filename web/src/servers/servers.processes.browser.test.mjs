// The busiest processes behind a card and the machine in detail, in Chromium: the popover on a desktop,
// the bottom sheet on a phone, ending a process in its row, what macOS/Windows and a node without
// command execution offer, and that nothing scrolls sideways.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, settled, showTab} from './testing/serversBrowser.mjs';

const fits = locator => locator.evaluate(el => [...el.querySelectorAll('*')].filter(n => n.scrollWidth > n.clientWidth + 1 && getComputedStyle(n).overflowX !== 'visible'
  && !n.matches('.pt-name, .pt-who small')).map(n => n.className));

test('processes behind the live cards, ending one, and the machine in detail', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('desktop: a popover per card, every order, ending a stubborn process, details', {timeout: 90000}, async t => {
    const h = await setup(t);
    await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.ov-grid').waitFor();
    assert.equal(await detail.locator('.sv-cpu-model').innerText(), 'AMD EPYC 7B13', 'the CPU model has its own line, uncut');
    assert.equal(await detail.locator('.ov-card.is-action').count(), 4, 'on Linux every card opens processes: CPU, memory, reads and writes, network');
    await detail.getByRole('button', {name: /^处理器 64%/}).click();
    const pop = h.page.locator('.sv-top-popper:visible');
    await pop.locator('.pt-row').first().waitFor();
    assert.match(await pop.locator('.pt-head').innerText(), /CPU 占用最高的进程[\s\S]*共 214 个进程/);
    const first = () => pop.locator('.pt-row .pt-name').first().innerText();
    assert.equal(await first(), 'python');
    assert.match(await pop.locator('.pt-row').first().innerText(), /PID 1355 · root[\s\S]*gateway\/server\.py[\s\S]*23\.4%[\s\S]*1\.5 GB/);
    assert.deepEqual(await pop.locator('.pt-sorts [role=tab]').allInnerTexts(), ['CPU', '内存', '读写', '连接', '网络']);
    await shot(h.page, 'desktop-processes-cpu');
    assert.deepEqual(await fits(pop), []);

    await pop.getByRole('tab', {name: '内存'}).click();
    await pop.locator('.pt-head', {hasText: '内存占用最高的进程'}).waitFor();
    await h.page.waitForFunction(() => document.querySelector('.sv-top-popper .pt-row .pt-name')?.textContent === 'clickhouse-server');
    await pop.getByRole('tab', {name: '读写'}).click();
    await pop.locator('.pt-head', {hasText: '读写最多的进程'}).waitFor();
    await h.page.waitForFunction(() => document.querySelector('.sv-top-popper .pt-row .pt-name')?.textContent === 'clickhouse-server');
    assert.match(await pop.locator('.pt-row').first().innerText(), /读[\s\S]*\/s[\s\S]*写[\s\S]*\/s/);
    await pop.getByRole('tab', {name: '网络'}).click();
    await pop.locator('.pt-section', {hasText: '容器'}).waitFor();
    assert.match(await pop.innerText(), /eth0[\s\S]*docker0[\s\S]*另有 1 个网卡现在没有流量[\s\S]*immich_server · 3 个进程[\s\S]*按 TCP 连接数/);
    assert.equal(await first(), 'xray');
    await shot(h.page, 'desktop-processes-net');

    // Ending, confirmed in the row: the popover stays open throughout.
    await pop.getByRole('tab', {name: 'CPU'}).click();
    const stubborn = pop.locator('.pt-row', {hasText: 'clickhouse-server'});
    await stubborn.getByRole('button', {name: '结束 clickhouse-server（PID 4242）'}).click();
    await stubborn.getByRole('button', {name: '确认结束'}).click();
    await stubborn.getByRole('button', {name: '强制结束'}).waitFor();
    assert.match(await pop.locator('.pt-note').innerText(), /没有在 10 秒内退出，可以强制结束/);
    await shot(h.page, 'desktop-processes-stubborn');
    await stubborn.getByRole('button', {name: '强制结束'}).click();
    await pop.locator('.pt-note', {hasText: '已强制结束 clickhouse-server（PID 4242）'}).waitFor();
    await h.page.waitForFunction(() => ![...document.querySelectorAll('.sv-top-popper .pt-name')].some(n => n.textContent === 'clickhouse-server'));
    const kills = h.calls.filter(c => c.path.endsWith('/processes/kill')).map(c => c.body);
    assert.deepEqual(kills, [{pid: 4242, name: 'clickhouse-server', force: false}, {pid: 4242, name: 'clickhouse-server', force: true}]);

    // Clicking elsewhere closes it, and it stops asking the node.
    await detail.locator('.ix-card-head h3', {hasText: '走势'}).click();
    await pop.waitFor({state: 'hidden'});
    await h.page.waitForFunction(() => !document.querySelector('.pt'));
    const asked = h.calls.filter(c => c.path.endsWith('/processes')).length;
    await h.page.waitForTimeout(3500);
    assert.equal(h.calls.filter(c => c.path.endsWith('/processes')).length, asked);

    await showTab(detail, '网络');
    await detail.getByRole('button', {name: 'TCP 连接，查看连接最多的进程'}).click();
    await h.page.locator('.sv-top-popper:visible .pt-head', {hasText: '连接最多的进程'}).waitFor();
    await h.page.keyboard.press('Escape');

    await showTab(detail, '系统');
    await detail.getByRole('button', {name: '详细信息'}).click();
    const drawer = h.page.locator('.dd-drawer:visible');
    await drawer.getByText('AMD EPYC 9654 96-Core Processor').waitFor();
    assert.match(await drawer.innerText(), /1 路|8 核 · 16 线程[\s\S]*2\.40 GHz · 最高 3\.70 GHz[\s\S]*L3 32M/);
    assert.deepEqual(await drawer.locator('.hd-tags .pb-tag').allInnerTexts(), ['AES', 'AVX', 'AVX2', 'AVX-512', 'SSE4.2', 'AMD-V']);
    assert.match(await drawer.innerText(), /vda[\s\S]*虚拟磁盘[\s\S]*\/data\/a\/very\/long\/mount\/point\/for\/backups[\s\S]*eth0[\s\S]*10 Gbit\/s[\s\S]*203\.0\.113\.7\/24/);
    assert.deepEqual(await fits(drawer), []);
    await shot(h.page, 'desktop-details');
    await noOverflow(h.page);
    await h.close();
  });

  await t.test('phone, dark: the bottom sheet, and what other platforms and consent offer', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, touch: true, tweak: items => {
      Object.assign(items.find(i => i.id === 'srv-delta').node.info, {platform: 'windows', os: 'Windows Server 2022', cpu: 'Intel Xeon'});
    }});
    await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.getByRole('button', {name: /^处理器 64%/}).click();
    const sheet = h.page.locator('.dd-sheet:visible');
    await sheet.locator('.pt-row').first().waitFor();
    assert.equal(await h.page.locator('.sv-top-popper:visible').count(), 0, 'no popover on a phone');
    const end = sheet.locator('.pt-row').first().getByRole('button', {name: /^结束 /});
    assert.ok((await end.boundingBox()).height >= 32, 'a touch-sized button');
    assert.deepEqual(await fits(sheet), []);
    await noOverflow(h.page);
    await shot(h.page, 'phone-processes-dark');
    await sheet.getByRole('button', {name: '关闭'}).click().catch(() => h.page.keyboard.press('Escape'));
    await settled(h.page);
    await h.close();

    // Windows lists CPU and memory only; the network card opens nothing there.
    const w = await setup(t, {tweak: items => {
      Object.assign(items.find(i => i.id === 'srv-delta').node.info, {platform: 'windows', os: 'Windows Server 2022', cpu: 'Intel Xeon'});
    }});
    await w.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const win = w.page.locator('.sv-detail:visible');
    await win.locator('.ov-grid').waitFor();
    assert.equal(await win.locator('.ov-card.is-action[data-kind=net]').count(), 0);
    await win.getByRole('button', {name: /^内存 /}).click();
    const pop = w.page.locator('.sv-top-popper:visible');
    await pop.locator('.pt-row').first().waitFor();
    assert.deepEqual(await pop.locator('.pt-sorts [role=tab]').allInnerTexts(), ['CPU', '内存']);
    await w.close();
    // A node with command execution off lists processes but cannot end them.
    const b = await setup(t);
    await b.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
    const bravo = b.page.locator('.sv-detail:visible');
    await bravo.getByRole('button', {name: /^处理器 \d+%/}).click();
    const listed = b.page.locator('.sv-top-popper:visible');
    await listed.locator('.pt-row').first().waitFor();
    assert.equal(await listed.getByRole('button', {name: /^结束 /}).count(), 0);
    await b.close();
  });
});
