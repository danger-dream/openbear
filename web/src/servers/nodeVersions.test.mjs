import test from 'node:test';
import assert from 'node:assert/strict';
import { incompatibleText, nodeBuildLine, nodeStateTag, nodeUpdateText, updateAllText, updateResultText, updateSummary } from './nodeVersions.js';

const TARGET = {version: '1.2.0', source: 'github', build: 'a1b2c3d4e5f6'};
const node = (state, extra = {}) => ({version: '1.1.0', build: '0123456789ab', source: 'release', updateAvailable: ['required', 'available', 'mismatch'].includes(state),
  caps: {exec: true}, update: {state, missing: [], target: TARGET, ...extra}});

test('list tags: only states that ask for something, required in warning tone with the missing features', () => {
  assert.deepEqual(nodeStateTag(node('required', {missing: ['mesh', 'net']})),
    {state: 'required', text: '需更新', tone: 'warn', title: '更新后才能使用组网、网络测试'});
  assert.equal(nodeStateTag(node('available')).text, '可更新');
  assert.equal(nodeStateTag(node('mismatch')).text, '构建不一致');
  assert.equal(nodeStateTag(node('current')), null);
  assert.equal(nodeStateTag(node('ahead')), null);
  assert.deepEqual(nodeStateTag(node('ahead', {downgrade: true}), {all: true}), {state: 'ahead', text: '比目标新', tone: '', title: '节点比主控选定的版本新，不会自动降级'});
  assert.equal(nodeStateTag(node('')), null);
  assert.equal(nodeStateTag(null), null);
  // An older controller without update states.
  assert.equal(nodeStateTag({updateAvailable: true}).text, '可更新');
  assert.equal(nodeStateTag({updateAvailable: false}), null);
});

test('detail: build line with its source, and what the update would do', () => {
  assert.equal(nodeBuildLine(node('current')), '0123456789ab · 正式版');
  assert.equal(nodeBuildLine({source: 'dev', build: ''}), '开发构建 · 未上报构建');
  assert.equal(nodeBuildLine(null), '');
  assert.equal(nodeUpdateText(node('available')), '节点 1.1.0 可以更新到 1.2.0（a1b2c3d4e5f6）');
  assert.equal(nodeUpdateText(node('required', {missing: ['mesh']})), '节点 1.1.0 需要更新才能使用组网，可以更新到 1.2.0（a1b2c3d4e5f6）');
  assert.equal(nodeUpdateText(node('required', {withdrawn: true})), '节点 1.1.0 所跑的版本已撤回，需要更新到 1.2.0（a1b2c3d4e5f6）');
  // Off a withdrawn version onto an older target: named a downgrade.
  assert.equal(nodeUpdateText({...node('required', {withdrawn: true, downgrade: true}), version: '1.3.0'}), '节点 1.3.0 所跑的版本已撤回，需要降级到 1.2.0（a1b2c3d4e5f6）');
  assert.equal(nodeUpdateText({...node('ahead', {downgrade: true}), version: '1.3.0-dev.4'}), '');
  assert.equal(nodeUpdateText(node('required', {protocolOk: false})), '节点 1.1.0 与主控的通信协议不兼容，需要更新到 1.2.0（a1b2c3d4e5f6）');
  assert.match(nodeUpdateText({...node('mismatch'), version: '1.2.0'}), /^节点运行的构建 0123456789ab 不是主控选定的 a1b2c3d4e5f6/);
  assert.equal(nodeUpdateText(node('available', {target: {...TARGET, source: 'local', version: '1.3.0-dev.2'}})), '节点 1.1.0 可以更新到本地构建 1.3.0-dev.2（a1b2c3d4e5f6）');
  assert.equal(nodeUpdateText(node('current')), '');
});

test('summary counts nodes that can update, must update, and only reinstall', () => {
  const items = [{node: node('required', {missing: ['mesh']})}, {node: node('available')}, {node: {...node('available'), caps: {exec: false}}}, {node: node('current')}, {node: null}];
  assert.deepEqual(updateSummary(items), {due: 3, direct: 2, required: 1, blocked: 1, features: '组网'});
});

test('update results: confirmed only once the node runs the new build', () => {
  assert.deepEqual(updateResultText({changed: false}), {tone: 'success', text: '已经是主控选定的构建'});
  assert.equal(updateResultText({changed: true, confirmed: true, from: '1.1.0', to: '1.2.0'}).text, '已更新到 1.2.0，节点重新连上并报告运行新构建');
  assert.equal(updateResultText({changed: true, confirmed: false}).tone, 'warning');
  assert.equal(updateResultText({changed: true, confirmed: true, from: '1.3.0-dev.4', to: '1.2.0', downgrade: true}).text, '已降级到 1.2.0，节点重新连上并报告运行新构建');
  assert.deepEqual(updateAllText([{ok: true, changed: true, confirmed: true}, {ok: true, changed: true, confirmed: false}, {ok: false, error: 'node_offline'},
    {ok: false, error: 'node_update_needs_exec'}]), {tone: 'warning', text: '已更新 1 台，1 台文件已替换但未确认运行，1 台失败', failed: [{ok: false, error: 'node_offline'}]});
  assert.equal(updateAllText([{ok: true, changed: false}]).text, '节点都已是主控选定的构建');
});

test('a node that cannot work with this controller says which side to update', () => {
  assert.equal(incompatibleText({incompatible: '', protocol: {min: 1, max: 1}}), '');
  assert.equal(incompatibleText(null), '');
  assert.match(incompatibleText({incompatible: 'node_protocol_outdated', protocol: {min: 1, max: 1, used: null}}), /^节点的通信协议（版本 1）比主控支持的旧.*更新节点后恢复$/);
  assert.match(incompatibleText({incompatible: 'controller_protocol_outdated', protocol: {min: 2, max: 3, used: null}}), /（版本 2–3）比主控支持的新.*需要先更新主控$/);
});
