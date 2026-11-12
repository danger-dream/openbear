import test from 'node:test';
import assert from 'node:assert/strict';
import {manualChunk, renderBuiltUrl, splitResourceKeys} from '../vite.config.js';

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

test('a quoted resource-like object key is split so 0.15.0 updaters do not look for it as a file', () => {
  // scripts/release_validation.py as installed by 0.15.0.
  const old = /(['"])((?:\/assets\/|\.\/|\.\.\/)[^'"\\\s]+\.(?:js|mjs|css|wasm|json|ttf|otf|eot|woff|woff2|svg|png|jpe?g|gif|webp|ico|map)(?:[?#][^'"\\\s]*)?)\1/gi;
  const glue = 'function e(){return{__proto__:null,"./rdp_client_bg.js":{__proto__:null,f:1},\'../x.wasm\' : 2}}';
  const out = splitResourceKeys(glue);
  assert.equal(out, 'function e(){return{__proto__:null,["./rdp_client_bg"+".js"]:{__proto__:null,f:1},[\'../x\'+\'.wasm\'] : 2}}');
  assert.deepEqual(out.match(old), null);
  const value = new Function(`${out}; return e();`)();
  assert.deepEqual(Object.keys(value), ['./rdp_client_bg.js', '../x.wasm']);
});

test('resource strings that are not object keys stay as they are', () => {
  for (const code of ['import("./a.js")', 'x?"./a.js":"./b.js"', 'new URL("./c.wasm",import.meta.url)', 'switch(x){case"./a.js":break}']) {
    assert.equal(splitResourceKeys(code), code);
  }
});

test('compiled JS and CSS resolve assets relative to themselves; index.html keeps root URLs', () => {
  for (const hostType of ['js', 'css']) {
    assert.deepEqual(renderBuiltUrl('assets/KaTeX_Main-Regular-B22Nviop.woff2', {hostType, type: 'asset'}), {relative: true});
    assert.deepEqual(renderBuiltUrl('assets/brand/openbear-2eaaca574a27.png', {hostType, type: 'public'}), {relative: true});
  }
  assert.equal(renderBuiltUrl('assets/index-yI1fbXhm.js', {hostType: 'html', type: 'asset'}), undefined);
});
