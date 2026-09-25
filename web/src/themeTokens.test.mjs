import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import config from '../tailwind.config.js';
import {editorTheme} from './editorTheme.js';
import {themeValues, resolveThemeValue, themeColor, contrast} from './testHelpers/theme.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
for (const dark of [false, true]) {
  test(`${dark ? 'dark' : 'light'} root palette covers page/panel/overlay and readable controls`, () => {
    const values = themeValues(dark);
    const color = role => themeColor(`var(--ob-${role})`, values);
    const expected = dark ? [22, 22, 32, 43] : [248, 240, 255, 246];
    for (const [index, role] of ['bg', 'sidebar', 'surface', 'surface-raised'].entries()) {
      assert.deepEqual(color(role), Array(3).fill(expected[index]));
    }
    for (const value of values.values()) assert.ok(resolveThemeValue(value, values));
    for (const role of ['bg', 'surface', 'surface-raised', 'surface-soft']) {
      assert.ok(contrast(color('text'), color(role)) >= 7, `${role}: body text`);
      assert.ok(contrast(color('text-subtle'), color(role)) >= 4, `${role}: secondary controls`);
    }
    for (const role of ['blue', 'success', 'warning', 'danger', 'orange', 'violet', 'info', 'text-strong', 'text-subtle']) {
      assert.ok(contrast(color('text-inverse'), color(role)) >= 4.5, `${role}: filled control`);
    }
    const mask = resolveThemeValue(values.get('--ob-mask'), values);
    assert.equal(mask, `rgb(0 0 0 / ${dark ? '.55' : '.28'})`);
  });
  test(`${dark ? 'dark' : 'light'} process sweep is a distinct bright band, not a dark shadow`, () => {
    const values = themeValues(dark);
    const base = themeColor('var(--ob-process-sweep-soft)', values);
    const highlight = themeColor('var(--ob-process-sweep-strong)', values);
    assert.ok(highlight.every((channel, i) => channel > base[i]));
    assert.ok(contrast(base, highlight) >= 3, 'the sweep must visibly differ from the resting glyph color');
  });
  test(`${dark ? 'dark' : 'light'} Monaco chrome resolves root colors and inherits syntax/diagnostics`, () => {
    const values = themeValues(dark);
    const theme = editorTheme(dark, name => values.get(name));
    assert.equal(theme.base, dark ? 'vs-dark' : 'vs');
    assert.equal(theme.inherit, true);
    assert.deepEqual(theme.rules, []);
    assert.equal(theme.colors['editor.background'], dark ? '#202020' : '#ffffff');
    assert.equal(theme.colors['editorSuggestWidget.background'], dark ? '#2b2b2b' : '#f6f6f6');
    assert.equal(theme.colors['editor.foreground'], dark ? '#dedee3' : '#27272a');
    for (const value of Object.values(theme.colors)) assert.match(value, /^#[\da-f]{6}(?:[\da-f]{2})?$/);
    assert.ok(theme.colors['editor.selectionBackground'].length === 9);
  });
}

test('Element bridge is root-scoped for body teleports; mobile native editor and boot use the same roles', () => {
  const bridge = postcss.parse(read('./dark-theme.css'));
  const declarations = new Map();
  bridge.walkRules(rule => {
    if (rule.selectors.includes(':root')) rule.walkDecls(decl => declarations.set(decl.prop, decl.value));
  });
  assert.equal(declarations.get('--el-bg-color'), 'var(--ob-surface)');
  assert.equal(declarations.get('--el-bg-color-overlay'), 'var(--ob-surface-raised)');
  assert.equal(declarations.get('--el-overlay-color'), 'var(--ob-mask)');
  assert.equal(declarations.get('--el-text-color-primary'), 'var(--ob-text)');
  const surfaceRoles = new Map();
  bridge.walkRules(rule => {
    if (['.el-dialog', '.el-drawer', '.el-message-box'].includes(rule.selector)) {
      rule.walkDecls(decl => surfaceRoles.set(rule.selector, decl.value));
    }
  });
  for (const selector of ['.el-dialog', '.el-drawer', '.el-message-box']) assert.equal(surfaceRoles.get(selector), 'var(--ob-surface-raised)');
  for (const dark of [false, true]) {
    const values = new Map([...themeValues(dark), ...declarations]);
    for (const value of declarations.values()) assert.ok(resolveThemeValue(value, values));
  }
  assert.match(read('../index.html'), /href="\/src\/theme-tokens.css"/);
  assert.match(read('./components/AdaptiveMdEditor.vue'), /color: var\(--el-text-color-primary\)/);
  assert.match(read('./components/MdEditor.vue'), /theme: registerEditorTheme\(\)/);
  assert.match(read('./components/MdEditor.vue'), /setTheme\(registerEditorTheme\(state\?\.dark/);
  assert.match(read('./App.vue'), /background:\s*var\(--ob-mask\)/);
});

test('Tailwind semantic and legacy aliases keep opacity modifiers in both themes', async () => {
  const generated = await postcss([tailwindcss({...config, content: [{raw: '<div class="bg-ob-surface/70 bg-ob-raised text-ob-inverse border-ob-border/50 bg-macpanel/80 text-macsub/75"></div>'}]})]).process('@tailwind utilities;', {from: undefined});
  assert.match(generated.css, /rgb\(var\(--ob-surface-rgb\) \/ 0\.7\)/);
  assert.match(generated.css, /rgb\(var\(--ob-surface-rgb\) \/ 0\.8\)/);
  assert.match(generated.css, /var\(--ob-surface-raised-rgb\)/);
  assert.match(generated.css, /var\(--ob-text-inverse-rgb\)/);
  assert.match(generated.css, /var\(--ob-border-alpha\) \* 0\.5/);
  assert.match(generated.css, /rgb\(var\(--ob-text-subtle-rgb\) \/ 0\.75\)/);
});
