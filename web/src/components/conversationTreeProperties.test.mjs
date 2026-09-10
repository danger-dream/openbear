import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import postcss from 'postcss';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const style = fs.readFileSync(new URL('./conversationTreeProperties.css', import.meta.url), 'utf8');

test('folder prompt editor is not wrapped in a native label that activates Monaco hidden IME input', () => {
  const labels = [...source.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)];
  assert.ok(labels.length > 0);
  assert.ok(labels.every(([label]) => !label.includes('<MdEditor')));
  assert.match(source, /class="property-field" role="group" aria-labelledby="folder-prompt-label"/);
  assert.match(source, /<span id="folder-prompt-label">/);
});

test('folder properties use scoped readable typography and compact inherited previews', () => {
  assert.match(style, /\.folder-properties-dialog \.effective-disclosure pre \{[^}]*font-family:inherit;[^}]*font-size:13px;/);
  assert.match(style, /\.folder-properties-dialog \.el-button \{[^}]*font-size:14px;/);
  assert.match(style, /\.folder-properties-dialog \.run-choice-value \{[^}]*font-size:14px;/);
  assert.match(style, /grid-template-columns:70px minmax\(0,1fr\) minmax\(0,1fr\)/);
  assert.match(style, /\.folder-properties-dialog \.run-field-hint \{[^}]*font-size:13px;/);
  assert.doesNotMatch(source, /class="run-default-group"|class="run-defaults-head"|class="property-note run-default-note"/);
  assert.match(source, /class="run-choice-inherit">\{\{ propertiesForm\.temporary \? '默认' : '继承'/);
  assert.match(source, /<h2>\{\{ propertiesForm\.temporary \? '临时会话属性' : '目录属性' \}\}<\/h2>/);
  assert.doesNotMatch(style, /font(?:-size)?:\s*(?:9|10|11|12)px/);
  postcss.parse(style).walkRules(rule => {
    assert.ok(rule.selectors.every(selector => /^(html\.dark )?\.folder-properties-(dialog|popover)/.test(selector)), rule.selector);
  });
  assert.match(style, /\.folder-properties-dialog \.el-dialog__body \{[^}]*min-height:0;[^}]*overflow:auto;/);
  assert.match(style, /\.folder-properties-dialog \.el-dialog__footer \{ flex:none;/);
  assert.match(source, /<details v-if="!propertiesForm\.temporary" class="effective-disclosure">[\s\S]*查看当前继承提示词/);
  assert.match(source, /class="property-segmented" role="tablist"/);
  assert.match(source, /aria-controls="folder-properties-panel-context"/);
  assert.match(source, /aria-controls="folder-properties-panel-defaults"/);
  assert.doesNotMatch(source, /<el-tabs|<el-tab-pane/);
});

test('property sheet owns its macOS styling without changing other Element Plus controls', () => {
  assert.match(source, /import "\.\/conversationTreeProperties\.css"/);
  assert.match(style, /--fp-surface:#f5f5f7/);
  assert.match(style, /property-segmented button\[aria-selected="true"\] \{ background:var\(--fp-selected\)/);
  assert.match(style, /folder-properties-popover\.el-popper/);
  assert.equal((source.match(/popper-class="folder-properties-popover" :show-arrow="false"/g) || []).length, 6);
  assert.match(source, /function navigatePropertiesTab\(event\)/);
  assert.match(style, /prefers-reduced-motion/);
});

test('temporary properties are separated at the bottom of the system menu', () => {
  const menu = source.slice(source.indexOf(`<template v-else-if="['system', 'root'].includes(menu.row?.kind)">`), source.indexOf('<template v-else>', source.indexOf(`<template v-else-if="['system', 'root'].includes(menu.row?.kind)">`)));
  assert.ok(menu.indexOf("runMenuAction('new-conversation')") < menu.indexOf("runMenuAction('new-folder')"));
  assert.match(menu, /新建根级目录[\s\S]*<template v-if="menu\.row\?\.systemNode === 'temporary'">\s*<hr \/>\s*<button[^>]+runMenuAction\('properties'\)/);
});

test('ordinary workspace input keeps its label and a neutral public example', () => {
  assert.match(source, /<label\b[^>]*>\s*<span id="folder-workspace-label">本节点工作目录[\s\S]*?<el-input[^>]+placeholder="例如 \/home\/user\/projects\/my-project"[^>]*\/>\s*<\/label>/);
});

test('six stable run-default fields expose inheritance and explicit follow values', () => {
  for (const field of ['mainModel', 'mainThinkingLevel', 'mainFastMode', 'agentModel', 'agentThinkLevel', 'agentFastMode']) {
    assert.match(source, new RegExp(`data-run-default-field="${field}"`));
    assert.match(source, new RegExp(`runDefaultSelection\\(propertiesForm\\.runDefaults, '${field}'\\)`));
  }
  assert.match(source, /跟随主模型（明确设置）[^\n]+runDefaultOption\(''\)/);
  assert.match(source, /跟随模型默认[^\n]+runDefaultOption\(''\)/);
  assert.match(source, /跟随主会话 Fast（明确设置）[^\n]+runDefaultOption\(null\)/);
  assert.match(source, /label="关闭" :value="runDefaultOption\(false\)"/);
  assert.match(source, /saveRunDefaults \? \{ runDefaults: sparseRunDefaults\(propertiesForm\.runDefaults\) \} : \{\}/);
  assert.match(source, /openbear:folder-properties-changed/);
});

test('capability choices and summaries follow backend normalization without densifying sparse values', () => {
  assert.match(source, /return levels\.length \? levels : \["off"\]/);
  assert.doesNotMatch(source, /levels\.includes\("off"\) \? levels : \["off", \.\.\.levels\]/);
  assert.match(source, /normalizedRunDefaults\(\{/);
  assert.match(source, /selected, applied/);
  assert.match(source, /→ 实际/);
  assert.match(source, /if \(!propertyModelsLoaded\.value\) return "";/);
});

test('context-only saves omit unchanged defaults while explicit changes remain validated', () => {
  assert.match(source, /propertiesRunDefaultsBaseline\.value = sparseRunDefaults\(data\.runDefaults\?\.local\)/);
  assert.match(source, /JSON\.stringify\(sparseRunDefaults\(propertiesForm\.runDefaults\)\)\s*!== JSON\.stringify\(propertiesRunDefaultsBaseline\.value\)/);
  assert.match(source, /const saveRunDefaults = propertyModelsLoaded\.value && runDefaultsChanged\.value/);
  assert.match(source, /saveRunDefaults \? runDefaultsValidationError\(\) : ""/);
  assert.match(source, /function resetPropertiesForm\(row\) \{\s*propertiesRunDefaultsBaseline\.value = \{\}/);
});

test('folder loading and saving are generation guarded against stale dialogs', () => {
  assert.match(source, /const request = \+\+propertiesRequestGeneration;/);
  assert.match(source, /request !== propertiesRequestGeneration \|\| !propertiesDialog\.value/);
  assert.match(source, /const folderId = propertiesForm\.folderId;/);
  assert.match(source, /conversationFolderPropertiesImpact\(folderId, payload\)/);
  assert.match(source, /updateConversationFolderProperties\(folderId, \{ \.\.\.payload, updateSnapshots:/);
});
