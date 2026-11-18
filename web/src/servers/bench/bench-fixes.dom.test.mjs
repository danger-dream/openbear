import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from '../testing/pageDom.mjs';
import {reactive} from 'vue';

for (const width of [390, 1440]) {
  test(`BENCH-B1/B3 pending stop and readable failed report (${width}px)`, async t => {
    const run = reactive({id: 'pending', serverId: 'fake', state: 'interrupted', name: 'IP.Check.Place', createdMs: 1,
      stopPending: true, error: 'node_offline', detail: '停止尚未确认', sections: [{label: 'IPv4', ansi: 'original report'}]});
    let deleted = 0;
    const h = await mountPage(t, {width, dark: width === 390,
      load: n => import(`./BenchReport.vue?fresh=${n}`),
      root: (View, render) => ({render: () => render(View, {run, onDelete: () => deleted++})}),
      handle: async () => [200, {ok: true}]});
    await h.page.getByText('待确认停止', {exact: true}).waitFor();
    const button = h.page.getByRole('button', {name: '删除', exact: true});
    assert.notEqual(await button.getAttribute('disabled'), null);
    assert.match(await h.page.locator('.bn-wait').innerText(), /确认前不能删除/);
    Object.assign(run, {stopPending: false, state: 'failed', error: 'bench_json_invalid', detail: '结构化报告缺失或无效，原始终端报告仍可查看'});
    await h.page.getByText('结构化报告缺失或无效，原始终端报告仍可查看', {exact: true}).waitFor();
    assert.equal(await h.page.locator('.bn-term').innerText(), 'original report');
    assert.equal(await button.getAttribute('disabled'), null);
    await button.click();
    assert.equal(deleted, 1);
    assert.deepEqual(h.errors, []);
    await h.close();
  });

  test(`BENCH-B3/B4 quality refresh never displays stale scores or clean failed DNSBL (${width}px)`, async t => {
    const report = {runId: 'old', atMs: 1, family: 'IPv4', ip: '203.0.113.1', location: {}, asn: '64500', org: 'Test', kind: '',
      usage: {}, company: {}, scores: [], risk: {value: null, level: null, sources: 0}, factors: [], media: [], scenes: [],
      purity: {value: null, label: '无法评估'}, mail: {checked: 3, listed: 0, marked: 0, answered: 0, failed: 2, unknown: 1}};
    let response = {ok: true, report, changes: [], pending: {runId: 'new', state: 'running'}};
    const h = await mountPage(t, {width, dark: width === 390,
      load: n => import(`../detail/IpQuality.vue?fresh=${n}`),
      root: (View, render) => ({render: () => render(View, {server: {id: 'fake', node: {info: {ipv4: '203.0.113.1'}}}, online: true, active: true, now: 100})}),
      handle: async (method, path) => [200, path.endsWith('/ip-quality') ? response : {ok: true, ip: '203.0.113.1', network: {}}]});
    await h.page.getByText('覆盖不完整', {exact: true}).waitFor();
    assert.match(await h.page.locator('.ipq').innerText(), /有效查询 0\/3 · 失败 2 · 未知 1/);
    assert.equal(await h.page.getByText('未上榜', {exact: true}).count(), 0);
    response = {ok: true, report: {...report, mail: {checked: 3, listed: 0, marked: 0}}, changes: [], pending: {runId: 'new', state: 'running'}};
    await h.page.getByText('共查 3 个 · 旧报告未记录失败覆盖率', {exact: true}).waitFor({timeout: 15000});
    response = {ok: true, report: {...report, mail: {...report.mail, answered: 3, failed: 0, unknown: 0}}, changes: [], pending: {runId: 'new', state: 'running'}};
    await h.page.getByText('未上榜', {exact: true}).waitFor({timeout: 15000});
    response = {ok: true, report: null, changes: [], pending: null, error: 'bench_json_invalid', failedRunId: 'new'};
    await h.page.getByText('本次结构化报告缺失或无效，未沿用旧质量结果。可到评测页查看原始报告或重新检测。', {exact: true}).waitFor({timeout: 15000});
    assert.equal(await h.page.locator('.ipq-hero').count(), 0);
    assert.equal(await h.page.getByText('未上榜', {exact: true}).count(), 0);
    assert.deepEqual(h.errors, []);
    await h.close();
  });
}
