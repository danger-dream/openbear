// Fake endpoints for the gateway update notice/dialog, shared by gatewayUpdate.dom.test.mjs and
// gatewayUpdate.browser.test.mjs. An update request is held until finishUpdate() releases it,
// so a test can look at the page while the update runs.
export const OLD = 'c'.repeat(64), NEW = '5beda9c64bd1fa3d2f85cce7c65a8b082393884285a605f1bdc8c8b2161dd414';
const instance = {id: 'child-a', name: '实例甲', status: 'ready', healthStatus: 'healthy', serverId: 'local', port: 18970, managementUrl: 'http://127.0.0.1:18970', workspacePath: '/opt/openbear/instances/a',
  models: {mode: 'later'}, account: {username: 'alpha'}, gateway: {status: 'synced', revision: 1, appliedRevision: 1}, notifications: {loginSuccess: false, loginFailure: true, health: true}, authMode: 'gateway'};

export function createGatewayUpdateBackend({gatewayPatch = {}, failUpdate = ''} = {}) {
  const calls = [];
  let gateway = {ok: true, enabled: true, connected: true, status: 'connected', publicUrl: 'https://bear.example.com', serverId: 'local', port: 18962, clientIp: {source: 'auto', trustedProxies: []}, observed: null,
    build: OLD, updateAvailable: true, updateReasons: ['build_outdated'], autoUpdate: true, lastUpdate: null, updating: false, ...gatewayPatch};
  let release = null, waiting = null;
  const held = new Promise(resolve => { waiting = resolve; });
  async function handle({path, method, body}) {
    calls.push({path, method, body});
    let json = {ok: true, items: []}, status = 200;
    if (path === '/api/instance-gateway') json = gateway;
    else if (path === '/api/instance-gateway/update') {
      await new Promise(resolve => { release = resolve; waiting(); });
      if (failUpdate) { status = 502; json = {ok: false, error: failUpdate}; gateway = {...gateway, lastUpdate: {atMs: Date.now(), ok: false, auto: false, error: failUpdate, build: gateway.build}}; }
      else { gateway = {...gateway, build: NEW, updateAvailable: false, updateReasons: [], lastUpdate: {atMs: Date.now(), ok: true, auto: false, error: '', build: NEW}}; json = gateway; }
    }
    else if (path === '/api/instance-gateway/auto-update') { gateway = {...gateway, autoUpdate: body.autoUpdate}; json = gateway; }
    else if (path === '/api/instance-servers') json = {ok: true, items: [{id: 'local', name: '本机', local: true, publicIp: '203.0.113.10', status: 'ok', usedBy: ['访问网关']}]};
    else if (path === '/api/servers') json = {ok: true, items: [{id: 'local', name: 'Edge-SG', local: true, region: 'SG'}]};
    else if (path === '/api/instances' && method === 'GET') json = {ok: true, items: [instance]};
    return {status, json};
  }
  // Waits for the page's update request (not a timed poll), then lets it answer.
  const finishUpdate = async () => { await held; release(); };
  return {calls, handle, finishUpdate};
}
