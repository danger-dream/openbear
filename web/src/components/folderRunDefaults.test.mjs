import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RUN_DEFAULT_INHERIT,
  hasRunDefault,
  normalizedRunDefaults,
  resolvedRunDefaults,
  runDefaultOption,
  runDefaultSelection,
  sparseRunDefaults,
  updateRunDefault,
} from './folderRunDefaults.js';

test('sparse defaults preserve false, null and empty-string as explicit local values', () => {
  const local = sparseRunDefaults({
    mainFastMode: false,
    agentModel: '',
    agentThinkLevel: '',
    agentFastMode: null,
    ignored: 'not part of the contract',
  });
  assert.deepEqual(local, {mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null});
  for (const field of Object.keys(local)) assert.equal(hasRunDefault(local, field), true);
  assert.equal(hasRunDefault({}, 'mainFastMode'), false);
});

test('each field resolves independently from local, parent, fallback and resolved compatibility data', () => {
  const value = resolvedRunDefaults(
    {mainFastMode: false, agentModel: ''},
    {mainModel: 'parent-model', mainFastMode: true, agentFastMode: null},
    {mainModel: 'server-model', mainThinkingLevel: 'medium', agentModel: 'server-agent', agentThinkLevel: '', agentFastMode: true},
    {mainThinkingLevel: 'high'},
  );
  assert.deepEqual(value, {
    mainModel: 'parent-model',
    mainThinkingLevel: 'medium',
    mainFastMode: false,
    agentModel: '',
    agentThinkLevel: '',
    agentFastMode: null,
  });
});

test('effective defaults normalize inherited values against the selected models without changing sparse input', () => {
  const local = {mainModel: 'low-only'};
  const inherited = {mainThinkingLevel: 'high', mainFastMode: true, agentModel: 'low-only', agentThinkLevel: 'high', agentFastMode: true};
  const fallback = {mainModel: 'fast', mainThinkingLevel: 'high', mainFastMode: true, agentModel: '', agentThinkLevel: '', agentFastMode: null};
  const models = [
    {key: 'fast', thinkingLevels: ['off', 'high'], defaultThinkingLevel: 'high', supportsFast: true},
    {key: 'low-only', thinkingLevels: ['low'], defaultThinkingLevel: 'low', supportsFast: false},
    {key: 'plain', thinkingLevels: [], defaultThinkingLevel: '', supportsFast: false},
  ];
  assert.deepEqual(normalizedRunDefaults({local, inherited, fallback, resolved: {}}, models), {contextStrategy: "sliding_window",
    mainModel: 'low-only', mainThinkingLevel: 'low', mainFastMode: false,
    agentModel: 'low-only', agentThinkLevel: '', agentFastMode: null,
  });
  assert.deepEqual(local, {mainModel: 'low-only'});
  assert.deepEqual(inherited, {mainThinkingLevel: 'high', mainFastMode: true, agentModel: 'low-only', agentThinkLevel: 'high', agentFastMode: true});
  assert.deepEqual(normalizedRunDefaults({local: {mainModel: 'plain'}, inherited: {}, fallback, resolved: {}}, models), {contextStrategy: "sliding_window",
    mainModel: 'plain', mainThinkingLevel: 'off', mainFastMode: false,
    agentModel: '', agentThinkLevel: '', agentFastMode: null,
  });
});

test('removed model overrides keep their stored value but effective defaults use the server fallback model', () => {
  const models = [{key: 'fallback', thinkingLevels: ['low'], defaultThinkingLevel: 'low', supportsFast: false}];
  const value = normalizedRunDefaults({
    local: {mainModel: 'removed', agentModel: 'also-removed'},
    inherited: {mainThinkingLevel: 'high', mainFastMode: true, agentThinkLevel: 'high', agentFastMode: true},
    fallback: {mainModel: 'fallback', mainThinkingLevel: 'low', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null},
    resolved: {mainModel: 'fallback', mainThinkingLevel: 'low', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null},
  }, models);
  assert.deepEqual(value, {contextStrategy: "sliding_window", mainModel: 'fallback', mainThinkingLevel: 'low', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null});
});

test('server resolved values remain the executable fallback when stored fallback models are removed', () => {
  const models = [{key: 'live', thinkingLevels: ['low'], defaultThinkingLevel: 'low', supportsFast: false}];
  assert.deepEqual(normalizedRunDefaults({
    local: {}, inherited: {},
    fallback: {mainModel: 'removed', mainThinkingLevel: 'high', mainFastMode: true, agentModel: 'removed-agent', agentThinkLevel: 'high', agentFastMode: true},
    resolved: {mainModel: 'live', mainThinkingLevel: 'low', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null},
  }, models), {contextStrategy: "sliding_window",mainModel: 'live', mainThinkingLevel: 'low', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null});
});

test('selection encoding distinguishes inherit from all explicit falsy values', () => {
  let local = {};
  for (const [field, value] of [
    ['mainFastMode', false],
    ['agentModel', ''],
    ['agentThinkLevel', ''],
    ['agentFastMode', null],
  ]) {
    local = updateRunDefault(local, field, runDefaultOption(value));
    assert.equal(hasRunDefault(local, field), true);
    assert.equal(local[field], value);
    assert.notEqual(runDefaultSelection(local, field), RUN_DEFAULT_INHERIT);
  }
  local = updateRunDefault(local, 'mainFastMode', RUN_DEFAULT_INHERIT);
  assert.equal(hasRunDefault(local, 'mainFastMode'), false);
  assert.equal(runDefaultSelection(local, 'mainFastMode'), RUN_DEFAULT_INHERIT);
  assert.deepEqual(updateRunDefault(local, 'notAField', runDefaultOption(true)), local);
});
