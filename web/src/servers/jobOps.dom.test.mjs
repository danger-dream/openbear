import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h} from 'vue';
import {mountApp} from '../testing/mountApp.mjs';
import {scaleTimers, waitUntil, sleep, settle} from '../testing/domPage.mjs';
const ServerJobs = (await import('./ServerJobs.vue')).default;
const {Api} = await import('../api.js');
scaleTimers(100);
const base = {id: 'fixture01', command: 'fixture command', state: 'running', startedAtMs: Date.now(), exitCode: null};
const part = (data, offset, job = base) => ({ok: true, job, log: {data, offset, next: offset + new TextEncoder().encode(data).length, first: 0, size: offset + new TextEncoder().encode(data).length}});
const transient = () => Object.assign(new Error('temporary'), {response: {status: 409, data: {error: 'node_offline'}}});

async function jobs(t, list = [base]) {
  t.mock.method(Api, 'serverJobs', async () => ({ok: true, jobs: list}));
  const mounted = mountApp(t, {render: () => h(ServerJobs, {serverId: 'fixture', active: true, now: Date.now()})});
  await mounted.page.locator('.sv-job-open').first().waitFor();
  return mounted;
}

test('log retries temporary failure from same byte cursor, keeps visible output, then resumes', async t => {
  const offsets = [];
  let reject;
  t.mock.method(Api, 'serverJob', async (server, id, params) => {
    offsets.push(params.offset);
    if (offsets.length === 1) return part('中\n', 0);
    if (offsets.length === 2) return new Promise((resolve, fail) => { reject = fail; });
    return part('done\n', 4, {...base, state: 'exited', exitCode: 0});
  });
  const d = await jobs(t);
  await d.page.locator('.sv-job-open').click();
  await waitUntil(() => reject);
  // domPage.innerText normalizes whitespace, including PRE trailing newlines.
  // Assert the rendered text node verbatim so lost/duplicated bytes still fail.
  assert.equal(await d.page.locator('.sv-job-log').textContent(), '中\n');
  reject(transient());
  await waitUntil(() => offsets.length === 3);
  await waitUntil(() => document.querySelector('.sv-job-log')?.textContent.includes('done'));
  assert.deepEqual(offsets, [undefined, 4, 4]);
  assert.equal(await d.page.locator('.sv-job-log').textContent(), '中\ndone\n');
  assert.equal(await d.page.locator('.sv-job-actions button').count(), 0, 'new status removes stale running stop control');
  await d.unmount();
});

test('log transient retries exhaust budget, manual retry preserves the cursor', async t => {
  const offsets = [];
  let recovered = false;
  t.mock.method(Api, 'serverJob', async (server, id, params) => {
    offsets.push(params.offset);
    if (offsets.length === 1) return part('first\n', 0);
    if (!recovered) throw transient();
    return part('last\n', 6, {...base, state: 'exited', exitCode: 0});
  });
  const d = await jobs(t);
  await d.page.locator('.sv-job-open').click();
  await waitUntil(() => offsets.length === 7); // tail + failed poll + five retries
  await sleep(250);
  assert.equal(offsets.length, 7);
  assert.equal(await d.page.locator('.sv-job-log').textContent(), 'first\n');
  recovered = true;
  await d.page.getByRole('button', {name: '重试日志', exact: true}).click();
  await waitUntil(() => document.querySelector('.sv-job-log')?.textContent.includes('last'));
  assert.deepEqual(offsets, [undefined, 6, 6, 6, 6, 6, 6, 6]);
  assert.equal(await d.page.locator('.sv-job-log').textContent(), 'first\nlast\n');
  await d.unmount();
});

for (const exit of ['close', 'unmount']) {
  test(`late failed log read cannot restart polling after ${exit}`, async t => {
    let reject, calls = 0;
    t.mock.method(Api, 'serverJob', async () => {
      calls++;
      return new Promise((resolve, fail) => { reject = fail; });
    });
    const d = await jobs(t);
    await d.page.locator('.sv-job-open').click();
    await waitUntil(() => reject);
    if (exit === 'unmount') await d.unmount();
    else await d.page.locator('.el-drawer__close-btn').click();
    await settle();
    reject(transient());
    await sleep(150);
    assert.equal(calls, 1);
    await d.unmount();
  });
}

test('permanent missing-job error does not automatically retry', async t => {
  let calls = 0;
  t.mock.method(Api, 'serverJob', async () => {
    calls++;
    throw Object.assign(new Error('missing'), {response: {status: 404, data: {error: 'job_not_found'}}});
  });
  const d = await jobs(t);
  await d.page.locator('.sv-job-open').click();
  await d.page.getByRole('button', {name: '重试日志', exact: true}).waitFor();
  await sleep(150);
  assert.equal(calls, 1);
  await d.unmount();
});

test('only confirmed interrupted residue gets a stop control, and it needs explicit confirmation', async t => {
  const confirmed = {...base, id: 'orphan01', state: 'interrupted', canStop: true};
  const list = [confirmed, {...confirmed, id: 'orphan02', canStop: false}, {...base, id: 'legacy01', state: 'interrupted'}];
  let stops = 0;
  t.mock.method(Api, 'serverJob', async () => part('', 0, confirmed));
  t.mock.method(Api, 'stopServerJob', async () => { stops++; return {ok: true, stopped: true}; });
  const d = await jobs(t, list);
  assert.equal(await d.page.locator('.sv-job-stop').count(), 1);
  assert.equal(stops, 0, 'listing never stops business processes');
  await d.page.locator('.sv-job-stop').click();
  await d.page.locator('.el-message-box').waitFor();
  assert.match(await d.page.locator('.el-message-box').innerText(), /残留命令运行/);
  assert.equal(stops, 0);
  await d.page.locator('.el-message-box').getByRole('button', {name: '取消', exact: true}).click();
  assert.equal(stops, 0);
  await d.page.locator('.sv-job-open').first().click();
  await d.page.locator('.sv-job-actions').getByRole('button', {name: '停止残留进程', exact: true}).click();
  await d.page.locator('.el-message-box').getByRole('button', {name: '停止', exact: true}).click();
  await waitUntil(() => stops === 1);
  await d.unmount();
});
