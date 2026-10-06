import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse, compileTemplate} from '@vue/compiler-sfc';
import postcss from 'postcss';
import {themeValues, themeColor} from './testHelpers/theme.mjs';

const source = readFileSync(new URL('./views/SettingsView.vue', import.meta.url), 'utf8');
const {descriptor, errors} = parse(source);
const rules = new Map();
for (const style of descriptor.styles) {
  postcss.parse(style.content).walkRules(rule => {
    const declarations = new Map();
    rule.walkDecls(decl => declarations.set(decl.prop, decl.value));
    rules.set(rule.selector, declarations);
  });
}
const toggle = rules.get('.mac-toggle');
const knob = rules.get('.mac-toggle__knob');

test('toggle preserves its 46x26 box and centers a full-height 20px knob without inline line boxes', () => {
  assert.deepEqual(errors, []);
  assert.equal(toggle.get('display'), 'inline-flex');
  assert.equal(toggle.get('align-items'), 'center');
  assert.equal(toggle.get('box-sizing'), 'border-box');
  assert.equal(toggle.get('line-height'), '1');
  assert.equal(toggle.get('flex-shrink'), '0');
  assert.equal(knob.get('flex-shrink'), '0');
  assert.equal(knob.get('display'), 'grid');
  assert.equal(knob.get('place-items'), 'center');
  assert.equal(toggle.get('width'), '46px');
  assert.equal(toggle.get('height'), '26px');
  assert.equal(toggle.get('padding'), '2px');
  assert.equal(toggle.get('border'), '1px solid var(--ob-border)');
  assert.equal(knob.get('width'), '20px');
  assert.equal(knob.get('height'), '20px');
  const innerHeight = parseFloat(toggle.get('height')) - 2 * (parseFloat(toggle.get('padding')) + parseFloat(toggle.get('border')));
  assert.equal(innerHeight, parseFloat(knob.get('height')));
  const travel = parseFloat(toggle.get('width')) - 2 * (parseFloat(toggle.get('padding')) + parseFloat(toggle.get('border'))) - parseFloat(knob.get('width'));
  assert.equal(rules.get('.mac-toggle.is-on .mac-toggle__knob').get('transform'), `translateX(${travel}px)`);
});

for (const dark of [false, true]) {
  test(`${dark ? 'dark' : 'light'} off track uses solid theme gray and on track stays green`, () => {
    const off = dark ? rules.get('html.dark .mac-toggle') : toggle;
    assert.equal(off.get('background'), 'var(--ob-surface-soft)');
    assert.deepEqual(themeColor(off.get('background'), themeValues(dark)), dark ? [48, 48, 48] : [241, 241, 242]);
    const on = rules.get(dark ? 'html.dark .mac-toggle.is-on' : '.mac-toggle.is-on');
    assert.equal(on.get('background'), 'linear-gradient(180deg, var(--ob-success), var(--ob-success))');
  });
}

test('settings template still compiles and retains toggle state, saving indicator and accessible label', () => {
  const compiled = compileTemplate({source: descriptor.template.content, filename: 'SettingsView.vue', id: 'settings-toggle-test'});
  assert.deepEqual(compiled.errors, []);
  const button = descriptor.template.content.match(/<button\s+type="button"\s+class="mac-toggle"[\s\S]*?<\/button>/)?.[0];
  assert.ok(button);
  for (const binding of [':class="draft[spec.path] ? \'is-on\' : \'\'"', ':disabled="saving[spec.path]"', ':aria-pressed="Boolean(draft[spec.path])"', '@click="toggleBool(spec)"']) assert.ok(button.includes(binding));
  assert.ok(button.includes("{{ saving[spec.path] ? '…' : '' }}"));
  assert.ok(button.includes('<span class="sr-only">切换 {{ spec.title }}</span>'));
  assert.equal(rules.get('.mac-toggle:disabled').get('opacity'), '0.7');
});
