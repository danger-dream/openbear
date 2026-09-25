import test from 'node:test';
import assert from 'node:assert/strict';
import {effectScope, ref} from 'vue';
import {createMessageVisibility, visibilityOperationId, visibilitySelectionClasses, visibilityTargetPreview} from './messageVisibility.js';

function harness(api = {}, options = {}) {
  const scope = effectScope(), conversationUuid = ref('A');
  const operations = ref(new Map(['u', 'a', 't'].map((id, i) => [id, {opId: id, opType: ['user_message', 'assistant_message', 'tool'][i]}])));
  const errors = [], anchors = [];
  const value = scope.run(() => createMessageVisibility({conversationUuid, operations, api, onError: e => errors.push(e), beforeChange: () => 'position', afterChange: a => anchors.push(a), ...options}));
  const state = (revision, hiddenIds = [], conversation = 'A') => ({conversationUuid: conversation, revision, hiddenIds, items: hiddenIds.map(opId => ({opId, type: 'assistant_message'}))});
  return {value, scope, conversationUuid, operations, errors, anchors, state};
}

test('long-press preview and highlight identify one message, then clear with the menu', () => {
  const h = harness(), v = h.value, target = {id: 'a', message: {content: '这里是第二条回复。\n请保留第一条。'}};
  try {
    v.openMobileMenu(target, {events: [target]});
    assert.deepEqual(v.mobileMenu.value.preview, {label: '模型回复', text: '这里是第二条回复。 请保留第一条。'});
    assert.equal(visibilitySelectionClasses({id: 'a'}, v)['visibility-menu-target'], true);
    assert.equal(visibilitySelectionClasses({id: 'u'}, v)['visibility-menu-target'], undefined);
    v.mobileMenu.value = null;
    assert.equal(visibilitySelectionClasses(target, v)['visibility-menu-target'], undefined);
    assert.deepEqual(visibilityTargetPreview({attachments: [{fileName: '截图.png'}]}, {opType: 'user_message'}), {label: '你的消息', text: '附件：截图.png'});
    assert.deepEqual(visibilityTargetPreview({retry: {attempt: 2}}, {opType: 'model_retry'}), {label: '模型重试', text: '第 2 次重试'});
  } finally { h.scope.stop(); }
});

test('mobile selection captures before width reflow and restores on entry and exit, but not toggles or desktop', () => {
  const trace = [];
  const h = harness({}, {
    preserveSelectionPosition: () => true,
    beforeChange: () => { const anchor = {selecting: h.value.selecting.value}; trace.push(['capture', anchor.selecting]); return anchor; },
    afterChange: anchor => trace.push(['restore', anchor.selecting, h.value.selecting.value]),
  });
  try {
    h.value.startSelection({id: 'a'});
    h.value.toggle({id: 'u'});
    h.value.startSelection({id: 'u'});
    h.value.cancelSelection();
    h.value.cancelSelection();
    assert.deepEqual(trace, [['capture', false], ['restore', false, true], ['capture', true], ['restore', true, false]]);
  } finally { h.scope.stop(); }
  const desktop = harness();
  try { desktop.value.startSelection({id: 'a'}); desktop.value.cancelSelection(); assert.equal(desktop.anchors.length, 0); }
  finally { desktop.scope.stop(); }
});

test('stable IDs survive streaming, stale snapshots and cross-conversation events do not reveal content', () => {
  const h = harness(), {value: v} = h;
  try {
    assert.equal(v.apply(h.state(2, ['u', 'a'])), true);
    assert.equal(v.userContent({user: {opId: 'u', content: 'private'}}), '');
    assert.equal(v.isHidden({eventKey: 'a', message: {content: 'new streamed text'}}), true);
    assert.equal(v.apply(h.state(1)), false);
    assert.equal(v.apply(h.state(99, [], 'B')), false);
    assert.equal(v.isHidden({operation: {opId: 'a'}}), true);
    assert.equal(v.canTarget({id: 'local-1'}), false);
    assert.equal(v.canTarget({id: 't'}), true);
    assert.deepEqual(h.anchors, ['position']);
    assert.equal(visibilityOperationId({id: 'transient', operation: {opId: 'a'}}), 'a');
  } finally { h.scope.stop(); }
});

test('batch hide, undo and restore all retain original operations', async () => {
  let revision = 0, hidden = [], calls = [];
  const h = harness({updateMessageVisibility: async (uuid, data) => {
    calls.push(data);
    hidden = data.restoreAll ? [] : data.hidden ? [...new Set([...hidden, ...data.opIds])] : hidden.filter(id => !data.opIds.includes(id));
    return {visibility: h.state(++revision, hidden, uuid)};
  }});
  try {
    const v = h.value, before = JSON.stringify([...h.operations.value]);
    v.startSelection({opId: 'u'}); v.toggle({eventKey: 'a'});
    assert.equal(v.selected.value.size, 2);
    assert.equal(await v.hideSelected(), true);
    assert.equal(v.selecting.value, false);
    assert.deepEqual(v.undoIds.value, ['u', 'a']);
    await v.undo();
    assert.equal(v.hiddenIds.value.size, 0);
    await v.hide([{opId: 't'}]);
    await v.restoreAll();
    assert.equal(v.hiddenIds.value.size, 0);
    assert.equal(JSON.stringify([...h.operations.value]), before);
    assert.equal(calls.length, 4);
  } finally { h.scope.stop(); }
});

test('hide a whole assistant round, preserve user interruptions and other rounds, and undo only newly hidden items', async () => {
  const calls = [];
  let revision = 1, hidden = ['already'];
  const h = harness({updateMessageVisibility: async (uuid, data) => {
    calls.push(data);
    hidden = data.hidden ? [...new Set([...hidden, ...data.opIds])] : hidden.filter(id => !data.opIds.includes(id));
    return {visibility: h.state(++revision, hidden, uuid)};
  }});
  try {
    for (const [opId, opType, extra] of [
      ['r', 'reasoning'], ['a2', 'assistant_message'], ['interrupt', 'user_message'],
      ['agent', 'agent'], ['retry', 'model_retry'], ['compact', 'context_compaction'],
      ['interaction', 'user_interaction'], ['already', 'tool'], ['other-turn', 'assistant_message'],
      ['internal', 'tool', {internal: true}], ['payload-internal', 'reasoning', {payload: {internal: true}}],
      ['not-visible', 'tool', {payload: {hidden: true}}], ['stats', 'stats'],
    ]) h.operations.value.set(opId, {opId, opType, ...extra});
    const v = h.value;
    v.apply(h.state(revision, hidden));
    const before = JSON.stringify([...h.operations.value]);
    const turn = {user: {opId: 'u'}, events: [
      {operation: {opId: 'r'}}, {eventKey: 'a'}, {id: 'interrupt'}, {opId: 't'},
      ...['a2', 'agent', 'retry', 'compact', 'interaction', 'already', 'internal', 'payload-internal', 'not-visible', 'stats', 'live-indicator'].map(id => ({id})),
      {opId: 'a'},
    ]};
    assert.equal(await v.hideAssistantTurn(turn), true);
    assert.deepEqual(calls[0], {opIds: ['r', 'a', 't', 'a2', 'agent', 'retry', 'compact', 'interaction'], hidden: true});
    assert.equal(v.isHidden(turn.user), false);
    assert.equal(v.isHidden({id: 'interrupt'}), false);
    assert.equal(v.isHidden({id: 'other-turn'}), false);
    assert.equal(v.assistantTargets(turn).length, 0);
    assert.equal(await v.hideAssistantTurn(turn), false, 'no empty API request');
    assert.equal(await v.hideAssistantTurn(null), false);
    assert.equal(calls.length, 1);
    await v.undo();
    assert.deepEqual([...v.hiddenIds.value], ['already']);
    assert.equal(JSON.stringify([...h.operations.value]), before);
  } finally { h.scope.stop(); }
});

test('failed save keeps visible state; late save cannot affect a different conversation', async () => {
  let resolve;
  const h = harness({updateMessageVisibility: () => new Promise(r => {resolve = r;})});
  try {
    const pending = h.value.hide([{opId: 'u'}]);
    assert.equal(h.value.busy.value, true);
    h.conversationUuid.value = 'B';
    resolve({visibility: h.state(1, ['u'])});
    assert.equal(await pending, false);
    assert.equal(h.value.hiddenIds.value.size, 0);
    assert.equal(h.value.busy.value, false);
  } finally { h.scope.stop(); }
  const failed = harness({updateMessageVisibility: async () => {throw new Error('network');}});
  try {
    assert.equal(await failed.value.hide([{opId: 'u'}]), false);
    assert.equal(failed.value.hiddenIds.value.size, 0);
    assert.equal(failed.errors.length, 1);
  } finally { failed.scope.stop(); }
});
