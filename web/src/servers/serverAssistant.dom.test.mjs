// What the page assistant learns from the servers page, in happy-dom: an open server detail names
// its server as a reference the backend can resolve, and the process list hands over the rows the
// user is looking at, so "这个进程" and "这台" need no further description.
import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h} from 'vue';
import {setup} from './testing/serversDom.mjs';
import {mountApp} from '../testing/mountApp.mjs';
import {Api} from '../api.js';
import {capturePageContext, currentScope} from '../assistant/pageContext.js';

const ProcessTop = (await import('./ProcessTop.vue')).default;

test('an open server detail is the subject, as a server reference', async t => {
  const hd = await setup(t);
  await hd.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  await hd.page.locator('.sv-detail:visible .dt-tabs').waitFor();
  const subject = capturePageContext().subjects.find(item => item.kind === 'server');
  assert.equal(subject.ref, 'openbear://ref/server/srv-delta');
  assert.equal(subject.label, 'Delta-JP');
  assert.equal(subject.role, '服务器详情');
  // Each server's detail keeps its own assistant conversation.
  assert.match(currentScope().key, /\|server:srv-delta$/);
  assert.deepEqual(currentScope().open, {kind: 'server', id: 'srv-delta'});
  await hd.close();
});

test('the process list hands over its rows with the server they run on', async t => {
  t.mock.method(Api, 'serverProcesses', async (sid, sort) => ({sort, count: 2, processes: [
    {pid: 812, identity: 'epoch:812', name: 'dockerd', user: 'root', command: '/usr/bin/dockerd -H fd://', cpu: 2.1, rss: 90},
    {pid: 1, identity: 'epoch:1', name: 'systemd', user: 'root', command: '/sbin/init', cpu: 0, rss: 12},
  ]}));
  const server = {id: 'srv-delta', name: 'Delta-JP', node: {info: {platform: 'linux'}, caps: {exec: true}}};
  const {page} = mountApp(t, {render: () => h(ProcessTop, {server})});
  await page.getByText('dockerd').first().waitFor();
  const subject = capturePageContext().subjects.find(item => item.kind === 'processes');
  assert.equal(subject.role, 'Delta-JP');
  assert.equal(subject.details.serverId, 'srv-delta');
  assert.deepEqual(subject.details.processes.map(p => [p.pid, p.command]), [[812, '/usr/bin/dockerd -H fd://'], [1, '/sbin/init']]);
});
