import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, inspector, node, noOverflow, startMeshBrowser} from './testing/meshBrowser.mjs';

test('mesh resource profile: pending resources shown without interrupting role change, desktop and phone', {skip: !chromium, timeout: 90000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const [width, dark, pending] of [[1440, false, true], [390, true, true], [390, false, false]]) {
    const h = await setup(t, {width, height: 900, dark});
    const original = h.mesh.handle.bind(h.mesh);
    h.mesh.handle = async (...args) => {
      const result = await original(...args);
      if (args[0] === 'GET' && args[1] === '/members/home' && result[1]?.status) {
        result[1].status.resources = {profile: 'member', ...(pending ? {pendingProfile: 'relay'} : {})};
      }
      return result;
    };
    if (width < 760) await node(h.page, 'home').tap();
    else await node(h.page, 'home').click();
    const panel = width < 760 ? h.page.locator('.mesh-inspect-sheet') : inspector(h.page);
    await panel.locator('.mesh-stats dt', {hasText: '配置版本'}).waitFor();
    if (pending) {
      await panel.getByText('角色已生效，收发缓冲和工作线程等固定资源在组网服务下次重启或升级时加载。').waitFor();
      assert.match(await panel.innerText(), /资源档位\s*待生效/);
    } else {
      assert.equal(await panel.locator('.mesh-stats dt', {hasText: '资源档位'}).count(), 0);
    }
    await noOverflow(h.page);
    assert.equal(h.calls.filter(c => c.method === 'POST').length, 0, 'viewing pending resources does not restart or upgrade');
    await h.close();
  }
});
