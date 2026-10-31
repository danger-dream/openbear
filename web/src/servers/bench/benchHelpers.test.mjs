import test from 'node:test';
import assert from 'node:assert/strict';
import * as h from './benchHelpers.js';

const CATALOG = {
  categories: [{id: 'ip', name: 'IP 质量'}, {id: 'network', name: '网络质量'}],
  tests: [{id: 'ipcheck', name: 'IP.Check.Place', category: 'ip', options: [{key: 'family', type: 'choice', default: '4'}, {key: 'report', type: 'bool', default: false}],
    warnings: [{text: '装依赖'}, {text: '会上传', if: {report: [true]}}]},
  {id: 'netcheck', name: 'Net.Check.Place', category: 'network', options: [{key: 'mode', type: 'choice', default: 'low'}], warnings: [{text: '耗流量', if: {mode: ['full']}}]}],
};
const linux = (id, arch = 'amd64', extra = {}) => ({id, name: id, node: {exec: true, info: {platform: 'linux', arch}}, ...extra});

test('which servers can run tests', () => {
  assert.equal(h.eligible(linux('a')), true);
  assert.equal(h.eligible(linux('b', 'arm64')), true);
  assert.equal(h.eligible(linux('c', 'arm')), false);
  assert.equal(h.eligible({id: 'w', node: {info: {platform: 'windows', arch: 'amd64'}}}), false);
  assert.equal(h.eligible({id: 'ssh-only'}), false);
  assert.equal(h.eligible({id: 'off', node: {exec: false, info: {platform: 'linux', arch: 'amd64'}}}), false);
  // The servers page's items carry it in caps.
  assert.equal(h.eligible({id: 'page-off', node: {caps: {exec: false}, info: {platform: 'linux', arch: 'amd64'}}}), false);
  assert.equal(h.eligible({id: 'page-on', node: {caps: {exec: true}, info: {platform: 'linux', arch: 'arm64'}}}), true);
  assert.equal(h.eligible({id: 'mac', node: {caps: {exec: true}, info: {platform: 'darwin', arch: 'arm64'}}}), false);
});

test('the tree: servers with runs in order, categories and tests with runs, newest run first', () => {
  const runs = [{id: 'r1', serverId: 'sg', test: 'ipcheck', createdMs: 1, state: 'done'}, {id: 'r2', serverId: 'sg', test: 'ipcheck', createdMs: 3, state: 'running'},
    {id: 'r3', serverId: 'gone-mac', test: 'netcheck', createdMs: 2, state: 'failed'}];
  const servers = [linux('sg'), {id: 'win', node: {info: {platform: 'windows', arch: 'amd64'}}}, linux('jp'), {id: 'gone-mac', name: 'mac', node: {info: {platform: 'darwin', arch: 'arm64'}}}];
  const tree = h.buildTree(servers, runs, CATALOG);
  assert.deepEqual(tree.map(n => [n.server.id, n.eligible, n.runs, n.active]), [['sg', true, 2, 1], ['gone-mac', false, 1, 0]], 'jp can run tests but has none');
  assert.deepEqual(tree[0].categories.map(c => c.id), ['ip']);
  assert.deepEqual(tree[0].categories[0].tests[0].runs.map(r => r.id), ['r2', 'r1']);
  assert.deepEqual(h.buildTree(servers, [], CATALOG), []);
});

test('warnings follow the options', () => {
  const [ip, net] = CATALOG.tests;
  assert.deepEqual(h.warningsFor(ip, {report: false}).map(w => w.text), ['装依赖']);
  assert.deepEqual(h.warningsFor(ip, {report: true}).map(w => w.text), ['装依赖', '会上传']);
  assert.deepEqual(h.warningsFor(net, h.defaultOptions(net)), []);
  assert.deepEqual(h.defaultOptions(ip), {family: '4', report: false});
});

test('states and times', () => {
  assert.equal(h.stateText({state: 'running'}), '运行中');
  assert.equal(h.stateText({state: 'running', stopping: true}), '正在停止');
  assert.equal(h.stateTone('done'), 'ok');
  assert.equal(h.stateTone('collecting'), 'busy');
  assert.equal(h.runTime(new Date(2026, 9, 30, 23, 4).getTime()), '2026.10.30 23:04');
  assert.equal(h.took({startedMs: 1000, endedMs: 126000}), '2 分 5 秒');
  assert.equal(h.took({startedMs: 0}), '');
});

test('ANSI reports become spans with escaped text', () => {
  assert.equal(h.ansiHtml('a<b>&c'), 'a&lt;b&gt;&amp;c');
  assert.equal(h.ansiHtml('\x1b[31mred\x1b[0m plain'), '<span class="bn-fg-1">red</span> plain');
  assert.equal(h.ansiHtml('\x1b[1;32mok\x1b[22m\x1b[39m.'), '<span class="bn-fg-10 bn-b">ok</span>.');
  assert.equal(h.ansiHtml('\x1b[38;5;196mx\x1b[48;2;1;2;300my'), '<span style="color:rgb(255,0,0)">x</span><span style="color:rgb(255,0,0);background-color:rgb(1,2,255)">y</span>');
  assert.equal(h.ansiHtml('\x1b[7minv\x1b[27m'), '<span class="bn-fg-bg bn-bg-fg">inv</span>');
  assert.equal(h.ansiHtml('\x1b]0;t\x07\x1b[?25l\x1b[2Kline\rx\r\n'), 'linex\r\n');
  assert.equal(h.plainText('\x1b[1mA\x1b[0m\rB'), 'AB');
});

test('CJK takes two cells and other non-ASCII one, as in a terminal', () => {
  assert.equal(h.ansiHtml('IP质⠀│'), 'IP<span class="bn-w2">质</span><span class="bn-w1">⠀</span><span class="bn-w1">│</span>');
  assert.equal(h.ansiHtml('\x1b[32m：\x1b[0m'), '<span class="bn-fg-2"><span class="bn-w2">：</span></span>');
  assert.ok(h.wide('。'.codePointAt(0)) && h.wide('🚀'.codePointAt(0)) && !h.wide('✔'.codePointAt(0)));
});
