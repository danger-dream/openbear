import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, noOverflow, startMeshBrowser} from './testing/meshBrowser.mjs';
import {meshAccessReason} from './serverHelpers.js';

test('unsupported plain HTTP mesh address explains why without disabling data plane', () => {
  assert.match(meshAccessReason({available: false, reason: 'controller_url_requires_https'}), /数据面可用.*HTTPS/);
});

test('confirmed changes: recovery state on desktop and phone keeps draft and does not offer unsafe retry', {skip: !chromium, timeout: 90000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const [width, dark, recovery] of [[1440, false, 'restoring'], [390, true, 'restoring'], [390, false, 'restored']]) {
    const h = await setup(t, {width, dark});
    const original = h.mesh.handle.bind(h.mesh);
    h.mesh.handle = async (...args) => {
      const result = await original(...args);
      if (args[0] === 'GET' && args[1] === '') {
        result[1].draft = {dirty: true, changes: [{text: '修改组网地址'}]};
        result[1].apply = {running: false, revision: 9, recovery, progress: [{serverId: 'home', state: 'failed', error: 'mesh_change_unconfirmed'}]};
      }
      return result;
    };
    await h.page.reload();
    const bar = h.page.locator('.mesh-stage .mesh-bar');
    await bar.getByText(recovery === 'restoring' ? '正在恢复旧网络，等待节点重连确认；草稿已保留。' : '这次变更未提交，已恢复旧网络；草稿已保留，可调整后重试。').waitFor();
    assert.equal(await bar.getByRole('button', {name: '应用', exact: true}).isEnabled(), recovery === 'restored');
    assert.equal(await bar.getByRole('button', {name: '回退到上一版'}).count(), 0, 'old network already restored, do not roll back twice');
    assert.equal(h.calls.filter(c => c.method === 'POST').length, 0);
    await noOverflow(h.page);
    await h.close();
  }
});
