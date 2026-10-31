import test from 'node:test';
import assert from 'node:assert/strict';
import {manualChunk, renderBuiltUrl} from '../vite.config.js';

test('shared preload runtime cannot be absorbed into lazy Monaco', () => {
  assert.equal(manualChunk('\0vite/preload-helper.js'), 'vendor');
  assert.equal(manualChunk('/project/web/node_modules/monaco-editor/esm/vs/editor/editor.api.js'), 'monaco');
  assert.equal(manualChunk('/project/web/node_modules/monaco-editor/esm/vs/editor/editor.worker.js?worker'), undefined);
});

test('existing vendor grouping remains explicit; app and lazy view modules remain automatic', () => {
  assert.equal(manualChunk('/project/web/node_modules/vue/dist/vue.runtime.esm-bundler.js'), 'vendor');
  assert.equal(manualChunk('/project/web/node_modules/element-plus/es/index.mjs'), 'vendor');
  assert.equal(manualChunk('/project/web/src/App.vue'), undefined);
  assert.equal(manualChunk('/project/web/src/views/MemoryView.vue'), undefined);
  assert.equal(manualChunk('/project/web/src/components/MdEditor.vue'), undefined);
});

test('compiled JS and CSS resolve assets relative to themselves; index.html keeps root URLs', () => {
  for (const hostType of ['js', 'css']) {
    assert.deepEqual(renderBuiltUrl('assets/KaTeX_Main-Regular-B22Nviop.woff2', {hostType, type: 'asset'}), {relative: true});
    assert.deepEqual(renderBuiltUrl('assets/brand/openbear-2eaaca574a27.png', {hostType, type: 'public'}), {relative: true});
  }
  assert.equal(renderBuiltUrl('assets/index-yI1fbXhm.js', {hostType: 'html', type: 'asset'}), undefined);
});
