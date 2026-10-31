// The instances page, login, settings and account pages without a browser: the requests and
// their bodies, texts, counts, states and navigation of instances.browser.test.mjs, checked on
// the real components in happy-dom. Sizes, overflow, 16px inputs and touch-target heights of
// the same screens stay in instances.browser.test.mjs.
import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage, storageDump} from '../testing/appHarness.mjs';
import {createInstancesBackend} from './instancesFixture.mjs';

const {h} = await import('vue');
const {authMode} = await import('../authMode.js');
const {resetFeatureTogglesForTest} = await import('../featureToggles.js');
const VIEWS = {
  login: () => import('../views/LoginView.vue'), app: () => import('../App.vue'), settings: () => import('../views/SettingsHubView.vue'),
  sessions: () => import('../views/SessionsView.vue'), instances: () => import('../views/InstancesView.vue'),
};

async function setup(t, {view = 'instances', mode = 'controller', width = 390, ...options} = {}) {
  const backend = createInstancesBackend({mode, ...options});
  const component = (await VIEWS[view]()).default;
  const opened = [], folders = [];
  // As the browser entry: every view gets the same props.
  const root = {render: () => h(component, {section: 'system-settings', 'onOpen-conversation': id => opened.push(id), 'onOpen-folder': id => folders.push(id)})};
  let mounted;
  const goto = async ({keepStorage = false} = {}) => {
    mounted = await openPage(t, root, {url: view === 'login' ? '/login' : view === 'app' ? '/instances' : '/__test', width, height: 844, keepStorage,
      handler: ({path, method, body, query}) => backend.handle({path, method, body, query}),
      before: () => { authMode.value = mode; window.__OPENBEAR_AUTH_MODE__ = mode; resetFeatureTogglesForTest(); }});
    await mounted.page.locator('#app > *').first().waitFor({state: 'attached'});
    return mounted.page;
  };
  const self = {calls: backend.calls, opened, folders, goto, failMaintain: backend.failMaintain,
    get page() { return mounted.page; }, close: () => mounted.unmount()};
  return self;
}
const click = (page, name) => page.getByRole('button', {name, exact: true}).click();
async function pickServer(page, name) {
  await page.getByRole('button', {name: /^运行位置：/}).click();
  await page.locator('.ss-option').filter({has: page.locator('.ss-name', {hasText: name})}).click();
}
const oneLine = s => s.replace(/\s+/g, ' ').trim();

test('managed password form uses only password endpoint; errors do not echo secrets', async t => {
  const h = await setup(t, {view: 'login', mode: 'managed', width: 320}); await h.goto();
  await h.page.getByLabel('账号', {exact: true}).fill('owner'); await h.page.getByLabel('密码', {exact: true}).fill('fixture-password');
  assert.ok(!await h.page.getByText('Telegram', {exact: false}).count());
  assert.equal(await h.page.getByLabel('密码', {exact: true}).getAttribute('type'), 'password');
  await click(h.page, '登录'); await h.page.getByRole('alert').waitFor();
  assert.equal(await h.page.getByLabel('密码', {exact: true}).inputValue(), '');
  assert.ok(!await h.page.getByText('DO-NOT-ECHO-password-fixture', {exact: false}).count());
  assert.deepEqual(h.calls.filter(c => c.method === 'POST').map(c => [c.path, c.body]), [['/api/auth/password/login', {username: 'owner', password: 'fixture-password'}]]);
  assert.deepEqual(storageDump(), {});
  await h.close();
});

test('controller login page shows Telegram at once, without a mode request', async t => {
  const h = await setup(t, {view: 'login'}); await h.goto();
  await h.page.getByText('输入 Web Secret Key 后，需要在 Telegram 中二次确认。').waitFor();
  assert.equal(await h.page.getByText('正在确认登录方式', {exact: false}).count(), 0);
  assert.ok(!h.calls.some(c => c.path === '/api/auth/mode')); assert.equal(h.calls.filter(c => c.method === 'POST').length, 0); await h.close();
});

test('real App role routing hides only instances on managed and retains controller navigation', async t => {
  for (const mode of ['managed', 'controller']) {
    const h = await setup(t, {view: 'app', mode, width: 1280}); await h.goto();
    await h.page.locator('.sidebar-desktop-nav').waitFor();
    if (mode === 'managed') {
      await h.page.waitForFunction(() => window.location.pathname.startsWith('/chat'));
      assert.equal(await h.page.locator('.sidebar-desktop-nav').getByText('实例', {exact: true}).count(), 0);
      assert.equal(h.calls.filter(c => c.path === '/api/instances').length, 0);
    } else {
      await h.page.locator('.instances-page').waitFor();
      assert.equal(await h.page.locator('.sidebar-desktop-nav').getByText('实例', {exact: true}).count(), 1);
    }
    const dock = h.page.locator('.sidebar-desktop-nav');
    if (mode === 'managed') {
      for (const name of ['记忆', '凭证', '文档', '定时', 'Skills', 'MCP']) assert.equal(await dock.getByText(name, {exact: true}).count(), 1);
      assert.equal(await dock.getByText('更多').count(), 0);
    } else {
      // More than six destinations: five pinned, the rest under More.
      assert.deepEqual(await dock.locator('.sidebar-resource-label').allInnerTexts(), ['记忆', '凭证', '文档', '实例', '主机', '更多']);
      await dock.getByRole('button', {name: '更多入口'}).click();
      const menu = h.page.getByRole('region', {name: '更多入口'});
      await menu.locator('.sidebar-more-go').first().waitFor();
      const names = pressed => menu.locator(`.sidebar-more-row:has(.sidebar-more-pin[aria-pressed="${pressed}"]) .sidebar-more-go`).allInnerTexts();
      assert.deepEqual(await names(false), ['定时任务', 'Skills', 'MCP 管理']);
      assert.deepEqual(await names(true), ['记忆管理', '凭证库', '文档库', '实例管理', '服务器']);
      assert.match(await menu.locator('.sidebar-more-caption').nth(1).innerText(), /已固定 5\/5 · 已满/);
      assert.ok(await menu.getByRole('button', {name: '固定 定时任务'}).isDisabled(), 'five pinned is the limit');
      // What was unpinned can be pinned again from the same row.
      await menu.getByRole('button', {name: '取消固定 实例管理'}).click();
      assert.deepEqual(await names(false), ['定时任务', 'Skills', 'MCP 管理', '实例管理']);
      assert.ok(await menu.getByRole('button', {name: '固定 实例管理'}).isEnabled());
      await menu.getByRole('button', {name: '固定 实例管理'}).click();
      assert.deepEqual(await dock.locator('.sidebar-resource-label').allInnerTexts(), ['记忆', '凭证', '文档', '实例', '主机', '更多']);
      await menu.getByRole('button', {name: '取消固定 实例管理'}).click();
      await menu.getByRole('button', {name: '固定 定时任务'}).click();
      assert.deepEqual(await dock.locator('.sidebar-resource-label').allInnerTexts(), ['记忆', '凭证', '文档', '定时', '主机', '更多']);
      await menu.getByRole('button', {name: '实例管理', exact: true}).click();
      await menu.waitFor({state: 'hidden'});
      await h.page.locator('.instances-page').waitFor();
      assert.equal(await dock.getByRole('button', {name: '更多入口'}).getAttribute('aria-current'), 'page');
      // A reload keeps the pins.
      await h.goto({keepStorage: true}); await h.page.locator('.sidebar-desktop-nav').waitFor();
      const again = h.page.locator('.sidebar-desktop-nav');
      assert.deepEqual(await again.locator('.sidebar-resource-label').allInnerTexts(), ['记忆', '凭证', '文档', '定时', '主机', '更多']);
      await again.getByRole('button', {name: '更多入口'}).click();
      const reopened = h.page.getByRole('region', {name: '更多入口'});
      await reopened.waitFor();
      await h.page.keyboard.press('Escape'); await reopened.waitFor({state: 'hidden'});
    }
    await h.close();
  }
});

test('shared settings keep business controls but hide managed restart, TG configuration and updater; password change is usable', async t => {
  for (const mode of ['managed', 'controller']) {
    const h = await setup(t, {view: 'settings', mode, width: 390}); await h.goto(); await h.page.getByLabel('搜索全部设置').waitFor();
    assert.equal(await h.page.getByRole('button', {name: '重启 OpenBear', exact: true}).count(), mode === 'controller' ? 1 : 0);
    await h.page.getByText('最大轮次', {exact: true}).waitFor();
    await h.page.getByLabel('搜索全部设置').fill('Telegram');
    assert.equal(await h.page.getByRole('button', {name: '发送 Telegram 测试通知', exact: true}).count(), mode === 'controller' ? 1 : 0);
    assert.equal(await h.page.getByText('Telegram 长任务通知', {exact: true}).count(), mode === 'controller' ? 1 : 0);
    await h.page.getByLabel('搜索全部设置').fill('web.port'); assert.equal(await h.page.locator('h4').filter({hasText: /^\s*监听端口\s*$/}).count(), mode === 'controller' ? 1 : 0);
    await h.page.getByLabel('搜索全部设置').fill('agent.maxTurns'); await h.page.getByText('最大轮次', {exact: true}).waitFor();
    assert.ok(!h.calls.some(c => c.method === 'POST' && (/restart|notifications\/test|system\/update/.test(c.path)))); await h.close();
    const app = await setup(t, {view: 'app', mode, width: 1280}); await app.goto(); await app.page.locator('.sidebar-version').first().waitFor();
    const refresh = app.page.getByRole('button', {name: '保留当前编辑', exact: true}); if (await refresh.count()) await refresh.click();
    const dismiss = app.page.getByRole('button', {name: '暂不开启', exact: true}); if (await dismiss.count()) await dismiss.click();
    await app.page.locator('.sidebar-version').first().click(); await app.page.getByText('版本与更新', {exact: true}).waitFor();
    assert.equal(await app.page.getByRole('button', {name: '更新到 vnext', exact: true}).count(), mode === 'controller' ? 1 : 0);
    if (mode === 'managed') await app.page.getByText('此受管实例不能自行升级或重启，请在主控实例的维护会话中操作容器，保留 /workspace 数据卷。').waitFor();
    assert.ok(!app.calls.some(c => c.method === 'POST' && c.path.includes('/system/update'))); await app.close();
    const account = await setup(t, {view: 'sessions', mode, width: 320}); await account.goto(); await account.page.locator('.sessions-content').waitFor();
    if (mode === 'controller') assert.equal(await account.page.getByText('修改账号与密码', {exact: true}).count(), 0);
    else {
      await account.page.getByText('修改账号与密码', {exact: true}).click(); await account.page.getByLabel('当前密码', {exact: true}).fill('current-fixture-password'); await account.page.getByLabel('新密码', {exact: true}).fill('new-fixture-password'); await account.page.getByLabel('确认新密码', {exact: true}).fill('different-fixture-password'); await click(account.page, '修改并退出所有设备'); await account.page.getByText('两次输入的新密码不一致。').waitFor(); assert.equal(account.calls.filter(c => c.path === '/api/auth/password/change').length, 0);
      await account.page.getByLabel('确认新密码', {exact: true}).fill('new-fixture-password'); assert.deepEqual(storageDump(), {});
      await click(account.page, '修改并退出所有设备'); await account.page.waitForFunction(() => window.location.pathname === '/login');
      assert.deepEqual(account.calls.find(c => c.path === '/api/auth/password/change').body, {currentPassword: 'current-fixture-password', password: 'new-fixture-password'});
    }
    await account.close();
  }
  const denied = await setup(t, {view: 'sessions', mode: 'managed', width: 390, changeStatus: 403}); await denied.goto(); await denied.page.getByText('修改账号与密码', {exact: true}).click(); for (const name of ['当前密码', '新密码', '确认新密码']) await denied.page.getByLabel(name, {exact: true}).fill('fixture-password-long'); await click(denied.page, '修改并退出所有设备'); await denied.page.getByText('当前密码未通过验证，请重新输入。').waitFor(); assert.equal(await denied.page.getByLabel('当前密码', {exact: true}).inputValue(), ''); assert.equal(await denied.page.getByText('DO-NOT-ECHO-password-fixture', {exact: false}).count(), 0); await denied.close();
});

test('no gateway: empty state; enabling checks the port, defaults the address and warns about mapping', async t => {
  const h = await setup(t, {gatewayOn: false, instances: false, portUsed: true, width: 390}); await h.goto();
  await h.page.getByText('还没有启用访问网关', {exact: true}).waitFor();
  assert.equal(await h.page.getByRole('button', {name: '创建实例', exact: true}).isDisabled(), true);
  await click(h.page, '启用访问网关');
  await h.page.getByText('✗ 已被 nginx 占用', {exact: true}).waitFor();
  assert.equal(await h.page.getByRole('button', {name: '启用', exact: true}).isDisabled(), true);
  await h.page.getByLabel('网关端口').fill('18963');
  await h.page.getByText('✓ 本机上可用', {exact: true}).waitFor();
  assert.equal(await h.page.getByLabel('访问地址').inputValue(), 'http://203.0.113.10:18963');
  await h.page.getByLabel('访问地址').fill('https://bear.example.com');
  await h.page.getByText('请自行把 bear.example.com 映射到 203.0.113.10:18963', {exact: false}).waitFor();
  await click(h.page, '启用'); await h.page.getByText('访问网关', {exact: true}).first().waitFor();
  assert.deepEqual(h.calls.find(c => c.path === '/api/instance-gateway/enable').body, {serverId: 'local', port: 18963, publicUrl: 'https://bear.example.com'});
  await h.page.getByRole('link', {name: 'https://bear.example.com'}).waitFor();
  assert.equal(await h.page.getByRole('button', {name: '创建实例', exact: true}).isDisabled(), false);
  await h.close();
});

test('create: generated password, copied models, automatic port/data dir; credentials shown once', async t => {
  const h = await setup(t, {instances: false, width: 320}); await h.goto(); await click(h.page, '创建实例');
  const password = await h.page.getByLabel('登录密码').inputValue();
  // A short password only shows advice; it can still be submitted.
  await h.page.getByLabel('登录密码').fill('abc'); await h.page.getByText('密码较短，容易被猜到；建议至少 8 位。', {exact: true}).waitFor();
  assert.equal(await h.page.getByLabel('登录密码').evaluate(el => el.validity.valid), true);
  await h.page.getByLabel('登录密码').fill(password);
  assert.match(password, /^[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}$/);
  await h.page.getByLabel('实例名称').fill('实例乙'); await h.page.getByLabel('登录账号').fill('beta');
  await pickServer(h.page, '云服务器');
  assert.equal(await h.page.getByLabel('默认模型').inputValue(), 'parent/model');
  await h.page.getByText('勾选的渠道（含 API Key）会复制到这个实例；默认只复制默认模型所在的渠道。', {exact: true}).waitFor();
  // Only the primary model's provider is copied by default; it cannot be unticked.
  const channels = h.page.getByRole('group', {name: '复制的渠道'});
  assert.equal(await channels.getByLabel('parent').isChecked(), true); assert.equal(await channels.getByLabel('parent').isDisabled(), true);
  assert.equal(await channels.getByLabel('second').isChecked(), false);
  await h.page.getByText('高级（端口、数据目录、镜像、内存、实例地址）').click();
  assert.equal(await h.page.getByLabel('内存上限').getAttribute('placeholder'), '不限（单位 MB，例如 2048）');
  assert.equal(await h.page.getByLabel('数据目录').getAttribute('placeholder'), '自动：/opt/openbear/instances/beta（重名时加后缀）');
  await click(h.page, '创建并部署');
  await h.page.getByText('已创建「实例乙」，正在部署', {exact: true}).waitFor();
  const body = h.calls.find(c => c.path === '/api/instances' && c.method === 'POST').body;
  assert.deepEqual(body, {name: '实例乙', serverId: 'srv-1', account: {username: 'beta', password}, models: {mode: 'copy', primary: 'parent/model', providers: ['parent']}});
  await h.page.getByText(password, {exact: true}).waitFor();
  await h.page.locator('.ob-checks li').filter({hasText: '账号已写入访问网关'}).waitFor();
  await click(h.page, '查看部署会话'); assert.deepEqual(h.opened, ['conv-new']);
  assert.equal(await h.page.getByText(password, {exact: true}).count(), 0);
  assert.deepEqual(storageDump(), {});
  await h.close();
});

test('adding a server from the create form keeps the form; vault keys are grouped and the passphrase field is suggested', async t => {
  const h = await setup(t, {width: 1280}); await h.goto(); await click(h.page, '创建实例');
  await h.page.getByLabel('实例名称').fill('保留我');
  await pickServer(h.page, '添加服务器…');
  const server = h.page.getByRole('dialog', {name: '添加服务器'}); await server.waitFor();
  assert.equal(await server.locator('.ob-radios label').count(), 3);
  const select = server.getByLabel('密钥库中的 SSH 私钥');
  assert.deepEqual(await select.locator('optgroup').evaluateAll(groups => groups.map(g => g.label)), ['deploy', '可能不是 SSH 私钥', '无法使用']);
  assert.equal(await select.locator('option[disabled]').filter({hasText: 'PuTTY'}).count(), 1);
  await server.getByText('✓ 这个字段能解开私钥', {exact: true}).waitFor();
  await server.getByLabel('服务器名称').fill('新服务器'); await server.getByLabel('服务器地址').fill('192.0.2.20');
  await server.getByRole('button', {name: '测试连接', exact: true}).click(); await server.getByText('未安装 Docker', {exact: false}).waitFor();
  await server.getByRole('button', {name: '保存', exact: true}).click(); await server.waitFor({state: 'hidden'});
  assert.deepEqual(h.calls.find(c => c.path === '/api/instance-servers' && c.method === 'POST').body, {name: '新服务器', host: '192.0.2.20', port: 22, username: 'root', auth: 'key', keyRef: {secret: 'deploy', field: '主密钥', passphraseField: '口令'}});
  assert.equal(await h.page.getByLabel('实例名称').inputValue(), '保留我');
  await h.page.getByRole('button', {name: '运行位置：新服务器 · 192.0.2.20', exact: true}).waitFor();
  await h.close();
});

for (const width of [1280, 390]) {
  test(`${width}px server picker: flags, host and in-use marks first, unavailable nodes greyed, search`, async t => {
    const h = await setup(t, {width}); await h.goto(); await click(h.page, '创建实例');
    const trigger = h.page.getByRole('button', {name: /^运行位置：/});
    // The host shows under its own name, marked as the controller and as running the gateway.
    assert.equal(oneLine(await trigger.innerText()), '🇸🇬 Edge-SG 主控 网关');
    await trigger.click();
    const panel = h.page.locator('.ss-panel'); await panel.waitFor();
    assert.deepEqual(await panel.locator('.ss-caption').allInnerTexts(), ['已在使用', '其他服务器']);
    const rows = await panel.locator('.ss-option').evaluateAll(els => els.map(el => [el, el.getAttribute('aria-disabled') === 'true']));
    const texts = await panel.locator('.ss-option').allInnerTexts();
    assert.deepEqual(texts.map((text, i) => [oneLine(text), rows[i][1]]), [['🇸🇬 Edge-SG 主控 网关', false], ['🇯🇵 云服务器 1 个实例 SSH', false], ['🇺🇸 洛杉矶节点机 节点', false], ['🇩🇪 柏林节点机 节点离线', true], ['＋ 添加服务器…', false]]);
    assert.equal(await panel.locator('.ss-tag').filter({hasText: '1 个实例'}).getAttribute('title'), '实例甲');
    if (width < 761) {
      await panel.locator('.ss-option').filter({hasText: '云服务器'}).evaluate(el => {
        el.dispatchEvent(new window.PointerEvent('pointerdown', {bubbles:true,pointerType:'touch',pointerId:1,clientY:200}));
        el.dispatchEvent(new window.PointerEvent('pointermove', {bubbles:true,pointerType:'touch',pointerId:1,clientY:120}));
        el.dispatchEvent(new window.PointerEvent('pointercancel', {bubbles:true,pointerType:'touch',pointerId:1}));
      });
      assert.equal(await panel.isVisible(), true);
      assert.equal(oneLine(await trigger.innerText()), '🇸🇬 Edge-SG 主控 网关');
    }
    // An unavailable server cannot be chosen; the list stays open.
    await panel.locator('.ss-option').filter({hasText: '柏林'}).click({force: true});
    assert.equal(await panel.isVisible(), true);
    await h.page.getByLabel('搜索服务器').fill('日本');
    assert.deepEqual(await panel.locator('.ss-option .ss-name').allInnerTexts(), ['云服务器', '添加服务器…']);
    await h.page.getByLabel('搜索服务器').press('Enter');
    await panel.waitFor({state: 'hidden'});
    assert.equal(oneLine(await trigger.innerText()), '🇯🇵 云服务器 1 个实例 SSH');
    await pickServer(h.page, '洛杉矶节点机');
    await h.page.getByText('高级（端口、数据目录、镜像、内存、实例地址）').click();
    // An online node carries the controller's requests: the instance listens on loopback only.
    assert.equal(await h.page.getByLabel('实例地址').getAttribute('placeholder'), '自动：只在服务器本机监听，主控经节点访问');
    await h.close();
  });
}

test('detail: overview, notifications toggle immediately, sessions link the folder, maintenance opens its session', async t => {
  const h = await setup(t, {width: 1280}); await h.goto();
  const row = h.page.locator('.instance-row'); await row.getByText('alpha · 云服务器', {exact: true}).waitFor(); await row.getByText('最近登录 1 小时前', {exact: true}).waitFor();
  await row.click();
  await h.page.getByText('http://203.0.113.10:18962', {exact: false}).first().waitFor();
  await h.page.getByText('🇯🇵云服务器 · 端口 18970', {exact: true}).waitFor();
  await click(h.page, '登录与通知'); await h.page.getByText('198.51.100.23', {exact: true}).first().waitFor();
  await h.page.getByLabel('登录成功').check();
  // Was a 20 × 50 ms poll: wait for the PATCH itself.
  await h.page.waitForFunction(() => h.calls.some(c => c.method === 'PATCH' && c.body?.notifications?.loginSuccess === true));
  assert.deepEqual(h.calls.find(c => c.method === 'PATCH').body, {notifications: {loginSuccess: true, loginFailure: true, health: true}});
  await click(h.page, '部署记录'); await click(h.page, '打开目录 →'); await h.page.waitForFunction(() => h.folders.length === 1); assert.deepEqual(h.folders, ['deploy-folder']);
  await click(h.page, '维护…');
  const dialog = h.page.getByRole('dialog', {name: '维护「实例甲」'}); await dialog.waitFor();
  assert.equal(await dialog.getByRole('button', {name: '开始维护', exact: true}).isDisabled(), true);
  await dialog.getByLabel('维护内容').fill('安装 LibreOffice'); await dialog.getByLabel('执行模型').selectOption('second/big');
  await dialog.getByRole('button', {name: '开始维护', exact: true}).click(); await dialog.waitFor({state: 'hidden'}); await h.page.waitForFunction(() => h.opened.length === 1);
  assert.deepEqual(h.calls.find(c => c.path.endsWith('/maintenance')).body, {instructions: '安装 LibreOffice', model: 'second/big'});
  assert.deepEqual(h.opened, ['conv-maint']);
  await h.close();
});

test('reset password and a pending gateway sync are explicit; check shows problems', async t => {
  const h = await setup(t, {width: 390, syncFails: true, verifyItemStatus: 'needs_verification'}); await h.goto(); await h.page.locator('.instance-row').click();
  await h.page.getByRole('button', {name: '重置密码', exact: true}).click();
  const box = h.page.locator('.el-message-box'); await box.waitFor();
  const text = await box.innerText(); const password = text.match(/新密码：(\S+)/)[1];
  await box.getByRole('button', {name: '确认并复制', exact: true}).click();
  await h.page.getByText('新密码尚未同步到访问网关，系统会自动重试。', {exact: true}).waitFor();
  assert.deepEqual(h.calls.find(c => c.method === 'PATCH').body, {account: {password}});
  await h.page.getByText('账号变更尚未同步到访问网关', {exact: false}).waitFor();
  await click(h.page, '立即重试'); await h.page.getByText('账号变更尚未同步到访问网关', {exact: false}).waitFor({state: 'hidden'});
  await click(h.page, '立即检查'); await h.page.getByText('检查完成：存在问题，请查看概况', {exact: true}).waitFor();
  assert.equal(await h.page.locator('.instance-detail-header .ob-dot').innerText(), '待检查');
  await h.close();
});

for (const width of [1280, 390]) {
  test(`${width}px detail: key rotation and network migration sit under More, confirm what they do and report their state`, async t => {
    const h = await setup(t, {width, itemPatch: {authMode: 'gateway', networkMigration: {pending: false, blockers: []}}}); await h.goto(); await h.page.locator('.instance-row').click();
    const note = h.page.getByRole('note');
    // (the note itself holds the text: Playwright's getByText matches the scope element, testing/domPage does not)
    await note.filter({hasText: '实例端口仍对外开放，主控和网关经服务器地址明文访问它。可在「更多」中选择「迁移网络」改为加密访问。'}).waitFor();
    const more = h.page.getByRole('button', {name: '更多操作', exact: true});
    await more.click();
    const menu = h.page.locator('.ix-more-menu'); await menu.getByRole('menuitem', {name: '迁移网络…'}).waitFor();
    assert.deepEqual((await menu.getByRole('menuitem').allInnerTexts()).map(s => s.trim()), ['重置密码…', '重新部署…', '更换密钥…', '迁移网络…', '归档…']);
    await menu.getByRole('menuitem', {name: '更换密钥…'}).click();
    let box = h.page.locator('.el-message-box'); await box.waitFor();
    const rotateText = await box.innerText();
    assert.match(rotateText, /该实例现有的登录会话会全部失效/); assert.match(rotateText, /实例当前不在线时，会在它恢复后自动完成更换/);
    await box.getByRole('button', {name: '更换', exact: true}).click();
    await h.page.getByText('实例暂时连不上，恢复后会自动完成更换', {exact: true}).waitFor();
    await h.page.getByText('密钥更换尚未完成：实例恢复连接后会自动完成。', {exact: true}).waitFor();
    await more.click(); await menu.getByRole('menuitem', {name: '迁移网络…'}).click();
    box = h.page.locator('.el-message-box'); await box.getByText('迁移网络', {exact: true}).waitFor();
    const migrateText = await box.innerText();
    assert.match(migrateText, /重建容器/); assert.match(migrateText, /数据目录不受影响/);
    await box.getByRole('button', {name: '迁移', exact: true}).click();
    await h.page.getByText('网络已迁移', {exact: true}).waitFor();
    await note.waitFor({state: 'hidden'});
    assert.deepEqual(h.calls.filter(c => c.method === 'POST' && /rotate-keys|migrate-network/.test(c.path)).map(c => c.path), ['/api/instances/child-a/rotate-keys', '/api/instances/child-a/migrate-network']);
    await more.click(); await menu.getByRole('menuitem', {name: '更换密钥…'}).waitFor();
    assert.equal(await menu.getByRole('menuitem', {name: '迁移网络…'}).count(), 0);
    await h.close();
  });
}

test('a migration that cannot run yet says why and offers no button', async t => {
  // As found online: the gateway here, the instance on a server outside the mesh whose node is too old.
  const h = await setup(t, {width: 320, itemPatch: {authMode: 'gateway', networkMigration: {pending: false, blockers: ['node_upgrade_required', 'server_mesh_required']}}}); await h.goto(); await h.page.locator('.instance-row').click();
  const note = h.page.getByRole('note');
  assert.equal(await note.innerText(), '实例端口仍对外开放，主控和网关经服务器地址明文访问它。需要先升级该服务器的节点，才能改为加密访问。网关和实例不在同一台服务器，需要先把该服务器加入组网，才能改为加密访问。');
  await h.page.getByRole('button', {name: '更多操作', exact: true}).click();
  const menu = h.page.locator('.ix-more-menu'); await menu.getByRole('menuitem', {name: '更换密钥…'}).waitFor();
  assert.equal(await menu.getByRole('menuitem', {name: '迁移网络…'}).count(), 0);
  assert.ok(!h.calls.some(c => c.path.endsWith('/migrate-network')));
  await h.close();
});

test('gateway settings show automatic IP detection; servers page lists usage and blocks deleting used servers', async t => {
  const h = await setup(t, {width: 390}); await h.goto();
  await h.page.locator('.ob-strip').getByRole('button', {name: '设置', exact: true}).click();
  const dialog = h.page.getByRole('dialog', {name: '访问网关'}); await dialog.waitFor();
  await dialog.getByText('自动识别 · 经本机反向代理访问', {exact: true}).waitFor();
  await dialog.getByText('已从 X-Forwarded-For 读取真实 IP', {exact: false}).waitFor();
  await dialog.getByText('识别不对？手动指定代理', {exact: true}).click();
  await dialog.getByLabel('代理地址').fill('192.0.2.10'); await dialog.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.getByText('已信任的代理', {exact: true}).waitFor();
  assert.deepEqual(h.calls.find(c => c.path === '/api/instance-gateway/client-ip').body, {trustedProxies: ['192.0.2.10']});
  // Rotation says up front that every login session ends.
  await dialog.getByRole('button', {name: '更换密钥…', exact: true}).click();
  const confirm = h.page.getByRole('dialog', {name: '更换密钥'});
  await confirm.getByText('所有使用者的登录会话都会失效', {exact: false}).waitFor();
  await confirm.getByRole('button', {name: '更换', exact: true}).click();
  await h.page.getByText('已更换，1 个实例暂时连不上，恢复后自动更换', {exact: true}).waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/instance-gateway/rotate-keys' && c.method === 'POST').length, 1);
  await dialog.getByRole('button', {name: '关闭', exact: true}).last().click(); await dialog.waitFor({state: 'hidden'});
  // Servers moved to their own page; the instance page keeps only the gateway strip.
  assert.equal(await h.page.locator('.ob-pages').count(), 0);
  await h.close();
});

test('a private-network proxy is trusted only after the owner confirms it', async t => {
  const h = await setup(t, {width: 320, observed: {peer: '10.0.0.5', header: 'X-Forwarded-For', atMs: Date.now() - 60000, trusted: false}}); await h.goto();
  await h.page.locator('.ob-strip').getByRole('button', {name: '设置', exact: true}).click();
  const dialog = h.page.getByRole('dialog', {name: '访问网关'}); await dialog.waitFor();
  await dialog.getByText('发现未信任的代理', {exact: true}).waitFor();
  await dialog.getByText('最近一次请求经 10.0.0.5 转发', {exact: false}).waitFor();
  const trust = dialog.getByRole('button', {name: '信任这个代理', exact: true});
  await trust.click();
  await dialog.getByText('已信任的代理', {exact: true}).waitFor();
  assert.deepEqual(h.calls.find(c => c.path === '/api/instance-gateway/client-ip').body, {trustedProxies: ['10.0.0.5']});
  assert.equal(await trust.count(), 0);
  await dialog.getByText('识别不对？手动指定代理', {exact: true}).click();
  assert.equal(await dialog.getByLabel('代理地址').inputValue(), '10.0.0.5');
  await h.close();
});
