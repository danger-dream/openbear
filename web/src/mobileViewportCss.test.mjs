import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import postcss from 'postcss';
import { parse } from '@vue/compiler-sfc';
const read = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8');
const css = path => path.endsWith('.vue') ? parse(read(path)).descriptor.styles.map(style => style.content).join('\n') : read(path);
const styles = Object.fromEntries(['./App.vue', './style.css', './components/ConversationTree.vue', './views/consoleView/ConsoleComposer.vue', './views/consoleView/ConsoleView.vue', './references/ReferenceEditor.vue'].map(path => [path, postcss.parse(css(path))]));
const desktop = { width: 1440, height: 900, hover: 'hover', pointer: 'fine' };
const phone = { width: 390, height: 800, hover: 'none', pointer: 'coarse' };
// This verifies parsed CSS media boundaries/declarations, not simulated browser
// layout. Geometry/keyboard state and actual event handlers are tested separately.
function matches(query, env) {
  return query.split(',').some(part => [...part.matchAll(/\(([^)]+)\)/g)].every(([, atom]) => {
    const [key, value] = atom.split(':').map(text => text.trim());
    if (key === 'max-width') return env.width <= parseFloat(value);
    if (key === 'min-width') return env.width >= parseFloat(value);
    if (key === 'max-height') return env.height <= parseFloat(value);
    if (key === 'min-height') return env.height >= parseFloat(value);
    return env[key] === value;
  }));
}
function declarations(path, selector, env) {
  const result = {};
  styles[path].walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let parent = rule.parent; parent; parent = parent.parent) if (parent.type === 'atrule' && parent.name === 'media' && !matches(parent.params, env)) return;
    rule.walkDecls(decl => { result[decl.prop] = decl.value; });
  });
  return result;
}

test('desktop shell height/layout, toolbar glyph sizes and hover-only delete styling remain unchanged', () => {
  assert.equal(declarations('./style.css', 'html', desktop).height, '100%');
  assert.deepEqual(declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell', desktop), {});
  assert.deepEqual(declarations('./App.vue', '.app-shell', desktop), {});
  assert.equal(declarations('./App.vue', '.mobile-app-bar', desktop).display, 'none');
  const composer = './views/consoleView/ConsoleComposer.vue';
  assert.equal(declarations(composer, '.tool-btn', desktop).width, '2rem');
  assert.equal(declarations(composer, '.send-button', desktop).height, '2rem');
  assert.equal(declarations(composer, '.send-button svg', desktop).width, '1rem');
  assert.equal(declarations(composer, '.attachment-remove', desktop).opacity, '0');
  assert.equal(declarations(composer, ':deep(.reference-editor-content)', desktop)['font-size'], undefined);
  assert.equal(declarations('./components/ConversationTree.vue', '.tree-touch-more', desktop).display, 'none');
});

test('phone safe top is reserved once by main/bar, bottom by shell, and fixed floats share the translated shell', () => {
  const shell = declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell', phone);
  assert.equal(shell.position, 'fixed'); assert.equal(shell.height, 'var(--mobile-viewport-height, 100dvh)');
  assert.equal(shell.transform, 'translateY(var(--mobile-viewport-top, 0px))'); assert.equal(shell['padding-top'], undefined);
  assert.equal(shell['padding-bottom'], 'env(safe-area-inset-bottom, 0px)');
  const main = declarations('./App.vue', '.app-main', phone), bar = declarations('./App.vue', '.mobile-app-bar', phone);
  assert.equal(main['padding-top'], 'calc(48px + env(safe-area-inset-top, 0px))'); assert.equal(bar.height, main['padding-top']);
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport] body', phone).overflow, 'hidden');
  const landscapeTouch = { ...phone, width: 844, height: 390 };
  assert.equal(declarations('./App.vue', '.mobile-app-bar', landscapeTouch).display, 'none');
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell', landscapeTouch)['padding-top'], 'env(safe-area-inset-top, 0px)');
});

test('touch targets are 44px with persistent remove/More visibility, original input font sizes and unchanged small icons', () => {
  for (const env of [phone, { ...phone, width: 1024 }, { ...desktop, width: 600 }]) {
    const composer = './views/consoleView/ConsoleComposer.vue';
    for (const selector of ['.tool-btn', '.send-button', '.attachment-remove']) {
      const value = declarations(composer, selector, env); assert.equal(value.width, '44px'); assert.equal(value.height, '44px');
    }
    assert.equal(declarations(composer, '.attachment-remove', env).opacity, '1');
    assert.equal(declarations(composer, '.attachment-remove svg', env).width, '0.82rem');
    for (const selector of ['.reference-editor-content', '.reference-editor-placeholder']) {
      assert.equal(declarations(composer, `:deep(${selector})`, env)['font-size'], undefined, 'mobile composer must not override accepted input typography');
      assert.equal(declarations('./references/ReferenceEditor.vue', selector, env)['font-size'], '14px', 'retain the original editor and placeholder font size');
    }
    assert.equal(declarations('./components/ConversationTree.vue', '.tree-touch-more', env).display, 'grid');
    assert.equal(declarations('./components/ConversationTree.vue', '.tree-context-menu button', env)['min-height'], '44px');
  }
});
