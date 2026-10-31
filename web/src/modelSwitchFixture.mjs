// The run-config model switch's fake controller, for modelSwitch.dom.test.mjs and
// modelSwitch.browser.test.mjs. scenario: idle | source-deleted | running | failed-save.
const model = (key, protocol) => ({key, id: key.split('/')[1], provider: key.split('/')[0], label: key.split('/')[1], protocol, thinkingLevels: ['low', 'high'], defaultThinkingLevel: 'low', contextWindow: 128000});
const oldModel = model('source/gpt', 'responses'), newModel = model('target/claude', 'anthropic');
const runConfig = model => ({conversationUuid: 'original', model, contextStrategy: 'sliding_window', thinkingLevel: 'low', effectiveThinkingLevel: 'low', thinkingLevels: ['low', 'high'], defaultThinkingLevel: 'low',
  supportsThinking: true, fastMode: false, fastRequested: false, fastSupported: false, effectiveFastMode: false, agentRunConfig: {model: '', thinkLevel: '', fastMode: null, effective: {model, thinkLevel: 'low', fastMode: false}},
  contextWindow: 128000, rolloverTriggerTokens: 89600, windowTriggerRatio: 0.7});

export function createModelSwitchBackend(scenario = 'idle') {
  let selected = oldModel.key;
  const running = scenario === 'running', writes = [];
  function handle({path, method, body}) {
    let json = {ok: true, items: []}, status = 200;
    if (method !== 'GET') writes.push({path, body});
    if (path === '/api/rath/options') json = {ok: true, models: scenario === 'source-deleted' ? [newModel] : [oldModel, newModel], primaryModel: newModel.key, agents: []};
    else if (path === '/api/conversations/original/state') json = {ok: true, ...runConfig(selected), running, messages: [{role: 'user', content: '原会话的任务要求', turnUuid: 'original-turn'}, {role: 'assistant', content: '原会话已经完成的结果', turnUuid: 'original-turn'}],
      operations: [], usage: {}, frameSeq: 0, conversation: {conversationUuid: 'original', title: '保留这个会话'}};
    else if (path === '/api/conversations/original/model') {
      if (scenario === 'failed-save' && writes.length === 1) { status = 503; json = {ok: false, error: 'model_save_unavailable'}; }
      else { selected = body.model; json = {ok: true, model: selected, nextRun: running, runConfig: runConfig(selected)}; }
    }
    return {status, json};
  }
  // Every socket gets an empty bootstrap and answers pings (fake network socket or Playwright route).
  function socket(ws) {
    const send = text => (ws.receive ? ws.receive(text) : ws.send(text));
    send(JSON.stringify({type: 'bootstrap', pendingConfirmations: []}));
    const onMessage = message => { if (message === 'ping') send('pong'); };
    if (ws.onMessage) ws.onMessage(onMessage); else ws.onSend = onMessage;
  }
  return {handle, socket, writes, running};
}
