import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import postcss from 'postcss';
import { parse } from '@vue/compiler-sfc';
const read = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8');
const css = path => path.endsWith('.vue') ? parse(read(path)).descriptor.styles.map(style => style.content).join('\n') : read(path);
const styles = Object.fromEntries(['./App.vue', './style.css', './mobile-inputs.css', './components/ConversationTree.vue', './components/ConversationActivityFolder.vue', './views/consoleView/ConsoleComposer.vue', './views/consoleView/ConsoleView.vue', './references/ReferenceEditor.vue'].map(path => [path, postcss.parse(css(path))]));
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
  const chatShell = declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell.is-console', phone);
  assert.equal(chatShell.background, 'var(--ob-chat-bg)');
  assert.equal(chatShell['padding-bottom'], undefined, 'chat retains the shell safe-area padding');
  assert.deepEqual(declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell.is-console', desktop), {});
  const main = declarations('./App.vue', '.app-main', phone), bar = declarations('./App.vue', '.mobile-app-bar', phone);
  assert.equal(main['padding-top'], 'calc(48px + env(safe-area-inset-top, 0px))'); assert.equal(bar.height, main['padding-top']);
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport] body', phone).overflow, 'hidden');
  const landscapeTouch = { ...phone, width: 844, height: 390 };
  assert.equal(declarations('./App.vue', '.mobile-app-bar', landscapeTouch).display, 'none');
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell', landscapeTouch)['padding-top'], 'env(safe-area-inset-top, 0px)');
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport] .app-shell.is-console', landscapeTouch).background, 'var(--ob-chat-bg)');
});

test('keyboard footer keeps browser geometry unshifted and limits the 4px offset to standalone', () => {
  const prefix = 'html[data-openbear-mobile-viewport][data-openbear-keyboard] .app-shell.is-console ';
  for (const width of [320,360,390,402,430,760]) {
    const env = {...phone,width};
    assert.deepEqual(declarations('./style.css', prefix + '.composer-shell', {...env,'display-mode':'browser'}), {'padding-bottom':'0'}, 'browser keyboard must not push the footer below the viewport');
    assert.deepEqual(declarations('./style.css', prefix + '.composer-shell', {...env,'display-mode':'standalone'}), {'padding-bottom':'0',transform:'translateY(4px)'}, 'only standalone retains the requested visual trial');
    assert.deepEqual(declarations('./style.css', prefix + '.composer-usage-summary', env), {'min-height':'24px','padding-top':'0'});
    assert.deepEqual(declarations('./style.css', prefix + '.context-usage-trigger', env), {height:'24px'});
    assert.equal(declarations('./views/consoleView/ConsoleComposer.vue', '.composer-shell', env).padding, '.5rem .75rem 8px', 'normal footer still keeps its original padding');
    assert.equal(declarations('./views/consoleView/ConsoleComposer.vue', '.composer-usage-summary', env)['flex-wrap'], 'wrap');
  }
  for (const selector of ['.composer-shell','.composer-usage-summary','.context-usage-trigger']) {
    assert.deepEqual(declarations('./style.css', prefix + selector, desktop), {});
    assert.deepEqual(declarations('./style.css', prefix + selector, {...phone,width:844}), {});
  }
});

test('standalone chat with keyboard hidden uses only 16px of the bottom safe area without changing browser or keyboard rules', () => {
  const selector = 'html[data-openbear-mobile-viewport]:not([data-openbear-keyboard]) .app-shell.is-console';
  for (const width of [320,360,390,402,430,760]) {
    assert.deepEqual(declarations('./style.css', selector, {...phone,width,'display-mode':'standalone'}), {'padding-bottom':'max(0px, calc(env(safe-area-inset-bottom, 0px) - 16px))'});
    assert.deepEqual(declarations('./style.css', selector, {...phone,width,'display-mode':'browser'}), {});
  }
  assert.deepEqual(declarations('./style.css', selector, {...desktop,'display-mode':'standalone'}), {});
  assert.equal(declarations('./style.css', 'html[data-openbear-mobile-viewport][data-openbear-keyboard] .app-shell', phone)['padding-bottom'], '0');
});

test('mobile recents keep title and status on one 44px row and reserve more height for the tree without changing desktop', () => {
  const path = './components/ConversationActivityFolder.vue';
  for (const env of [phone, {...phone, width: 320}, {...phone, width: 844, height: 390}]) {
    const row = declarations(path, '.activity-row-open', env);
    assert.equal(row.height, '44px'); assert.equal(row['min-height'], '44px'); assert.equal(row.padding, '0 6px');
    assert.equal(declarations(path, '.activity-row-copy', env)['flex-direction'], 'row');
    const title = declarations(path, '.activity-row-title', env);
    assert.equal(title['white-space'], 'nowrap'); assert.equal(title['text-overflow'], 'ellipsis');
    assert.equal(title.display, 'block'); assert.equal(title['-webkit-line-clamp'], undefined);
    const status = declarations(path, '.activity-row-status-touch', env);
    assert.equal(status.display, 'block'); assert.equal(status['white-space'], 'nowrap');
    assert.equal(declarations(path, '.activity-folder', env)['max-height'], '40%');
    const content = declarations(path, '.activity-folder-content', env);
    assert.equal(content['max-height'], 'min(28dvh, 220px)'); assert.equal(content['overflow-y'], 'auto');
    for (const selector of ['.activity-row-read', '.activity-row-more']) {
      const action = declarations(path, selector, env);
      assert.equal(action.width, '44px'); assert.equal(action.height, '44px');
    }
  }
  assert.equal(declarations(path, '.activity-row-open', desktop).height, '29px');
  assert.equal(declarations(path, '.activity-row-copy', desktop).display, 'contents');
  assert.equal(declarations(path, '.activity-row-status-touch', desktop).display, 'none');
  assert.equal(declarations(path, '.activity-folder-content', desktop)['max-height'], 'min(32vh, 260px)');
});

test('touch targets are 44px with persistent remove/More visibility, zoom-safe input sizes and unchanged small icons', () => {
  for (const env of [phone, { ...phone, width: 1024 }, { ...desktop, width: 600 }]) {
    const composer = './views/consoleView/ConsoleComposer.vue';
    for (const selector of ['.tool-btn', '.attachment-remove']) {
      const value = declarations(composer, selector, env); assert.equal(value.width, '44px'); assert.equal(value.height, '44px');
    }
    const send = declarations(composer, '.send-button', env); assert.equal(send.width, '38px'); assert.equal(send.height, '38px');
    assert.equal(declarations(composer, '.attachment-remove', env).opacity, '1');
    assert.equal(declarations(composer, '.attachment-remove svg', env).width, '0.82rem');
    for (const selector of ['.reference-editor-content', '.reference-editor-placeholder']) {
      assert.equal(declarations(composer, `:deep(${selector})`, env)['font-size'], undefined, 'the shared input policy owns mobile typography');
      assert.equal(declarations('./references/ReferenceEditor.vue', selector, desktop)['font-size'], '14px', 'desktop retains its original editor and placeholder size');
      const mobileSelector = selector === '.reference-editor-content' ? '[contenteditable]:not([contenteditable="false"])' : selector;
      assert.equal(declarations('./mobile-inputs.css', mobileSelector, env)['font-size'], '16px', 'actual editable node and placeholder use the mobile font floor');
    }
    assert.equal(declarations('./components/ConversationTree.vue', '.tree-touch-more', env).display, 'grid');
    assert.equal(declarations('./components/ConversationTree.vue', '.tree-context-menu button', env)['min-height'], '44px');
  }
});
