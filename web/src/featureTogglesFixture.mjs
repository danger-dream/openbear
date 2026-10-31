// Fake /api for the 实例 / 主机 switch tests, shared by featureToggles.dom.test.mjs (happy-dom)
// and featureToggles.browser.test.mjs (Chromium). Spec text is the same as app/settings/specs.py.
// controller: 'new' has the switches, 'old' predates them (no spec, no value).
export function createFeatureState() {
  return {controller: 'new', values: {}, settingsDelayMs: 0, settingsFail: false, patches: [], settingsAnswers: 0};
}

const spec = (path, title, desc, group, effect = '下一轮生效') => ({path, title, desc, kind: 'bool', group, effect, min: null, max: null, unit: '', displayScale: 1, nullable: false, choices: [], sensitive: false, editor: 'default', variables: [], defaultValue: ''});
const FEATURE_SPECS = {
  'features.instances': spec('features.instances', '启用实例', '显示“实例”菜单，并把 InstanceControl 工具开放给模型。这个工具的说明较长，开启后每轮对话会多占用一些 token。关闭只隐藏菜单和工具，已有实例的守护与通知照常运行。', 'features'),
  'features.servers': spec('features.servers', '启用主机', '显示“主机”菜单，并把 Servers 工具和节点浏览器开放给模型。这个工具的说明较长，开启后每轮对话会多占用一些 token。关闭只隐藏菜单和工具，节点连接、组网和到期提醒照常运行。', 'features'),
};
const UI_SPEC = {'ui.showTurnStats': spec('ui.showTurnStats', '显示本轮统计', '在回答完成后显示耗时、Token、TPS 和费用等本轮统计。', 'interface')};
const UI_DOMAIN = {key: 'interface', title: '界面显示', desc: '回答内容和运行统计的默认呈现方式', sections: [{key: 'interface', title: '界面显示', paths: ['ui.showTurnStats']}]};
const FEATURE_DOMAIN = {key: 'features', title: '可选功能', desc: '实例、主机等按需开启的菜单和工具', sections: [{key: 'features', title: '按需开启', paths: ['features.instances', 'features.servers']}]};

// request: {path, method, body}; returns {status, json}.
export async function featureApi(state, {path: p, method, body}) {
  let json = {ok: true, items: []}, status = 200;
  if (p === '/api/settings' && method === 'GET') {
    if (state.settingsDelayMs) await new Promise(r => setTimeout(r, state.settingsDelayMs));
    if (state.settingsFail) { status = 500; json = {ok: false, error: 'boom'}; }
    else json = {ok: true, values: {...state.values, 'ui.showTurnStats': true}, masked: {}, usingBuiltin: {}, providerCount: 1, revision: 3};
    state.settingsAnswers++;
  }
  else if (p === '/api/settings/specs') json = state.controller === 'new'
    ? {ok: true, domains: [FEATURE_DOMAIN, UI_DOMAIN], groups: [FEATURE_DOMAIN.sections[0], UI_DOMAIN.sections[0]], specs: {...FEATURE_SPECS, ...UI_SPEC}}
    : {ok: true, domains: [UI_DOMAIN], groups: [UI_DOMAIN.sections[0]], specs: {...UI_SPEC}};
  else if (p.startsWith('/api/settings/') && method === 'PATCH') {
    const key = decodeURIComponent(p.slice('/api/settings/'.length));
    state.patches.push({key, value: body.value}); state.values[key] = body.value; json = {ok: true, path: key, value: body.value, revision: 4};
  }
  else if (p === '/api/rath/options') json = {ok: true, models: [], agents: []};
  else if (p === '/api/auth/session') json = {ok: true, authenticated: true, mode: 'controller'};
  else if (p === '/api/system/version') json = {ok: true, version: 'test'};
  else if (p === '/api/conversation-tree/bootstrap') json = {ok: true, rootFolders: [], selected: null, initialExpandedPaths: [], locatedFolders: [], running: {items: [], activityItems: [], recentItems: []}, activeCount: 1, archivedCount: 0, temporaryCount: 0};
  else if (p === '/api/conversation-tree/status') json = {ok: true, items: [], activityItems: [], recentItems: [], folderConversationCounts: {}, folderRunningCounts: {}, revision: 1};
  return {status, json};
}
