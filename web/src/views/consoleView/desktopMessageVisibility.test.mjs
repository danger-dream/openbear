import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';
import {parse, compileStyle} from '@vue/compiler-sfc';
import {ref} from 'vue';
import {selectVisibilityRow, visibilitySelectionClasses} from './messageVisibility.js';

const files = ['MessageVisibilityAction.vue', 'MessageVisibilityBar.vue', 'ConsoleView.vue', 'ConsoleHeader.vue', 'TurnList.vue', 'TurnWorkDetailPanel.vue'];
const descriptors = Object.fromEntries(files.map(file => [file, parse(fs.readFileSync(new URL(file, import.meta.url), 'utf8')).descriptor]));
const styles = Object.fromEntries(files.map(file => [file, postcss.parse(descriptors[file].styles.map(s => s.content).join('\n'))]));
function css(file, selector, width = 1440) {
  const env = {width, hover: width > 760 ? 'hover' : 'none', pointer: width > 760 ? 'fine' : 'coarse'}, out = {};
  function matches(query) { return query.split(',').some(part => [...part.matchAll(/\(([^)]+)\)/g)].every(([, atom]) => {
    const [key, value] = atom.split(':').map(v => v.trim());
    if (key === 'min-width') return env.width >= parseFloat(value);
    if (key === 'max-width') return env.width <= parseFloat(value);
    return env[key] === value;
  })); }
  styles[file].walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let parent = rule.parent; parent; parent = parent.parent) if (parent.type === 'atrule' && parent.name === 'media' && !matches(parent.params)) return;
    rule.walkDecls(d => { out[d.prop] = d.value; });
  });
  return out;
}

test('desktop message menu and checkboxes are docked beside the first line, not in a new footer', () => {
  const file = 'MessageVisibilityAction.vue';
  for (const selector of ['.message-visibility-action.placement-gutter', '.message-visibility-action.placement-footer.is-selecting']) {
    const value = css(file, selector);
    assert.equal(value.position, 'absolute'); assert.equal(value.top, '0'); assert.equal(value.left, '-30px');
  }
  assert.equal(css(file, '.message-visibility-action').opacity, '0');
  assert.equal(css(file, '.message-visibility-action.is-open').opacity, '1');
  assert.equal(css(file, '.message-visibility-action:focus-within').opacity, '1');
  assert.equal(css(file, '.message-visibility-action', 390).opacity, undefined);
  assert.equal(css(file, '.visibility-more', 390).height, '44px');
  assert.equal(css('TurnList.vue', '.visibility-hover-surface::before').width, '32px', 'pointer can cross the gap to the outside menu');
  assert.match(descriptors['TurnList.vue'].template.content, /:target="entry.event" desktop-placement="gutter"/);
  const compiled = compileStyle({source: descriptors['TurnList.vue'].styles[0].content, filename: 'TurnList.vue', id: 'data-v-test', scoped: true});
  assert.deepEqual(compiled.errors, []);
  assert.match(compiled.code, /visibility-hover-surface\[data-v-test\]:hover\s+\.message-visibility-action/);
});

test('desktop selection uses a custom round check and gutter marker without painting the message', () => {
  const action = 'MessageVisibilityAction.vue';
  assert.equal(css(action, '.visibility-select input').opacity, '0');
  assert.equal(css(action, '.visibility-check')['border-radius'], '50%');
  assert.equal(css(action, '.visibility-select input:checked + .visibility-check').background, 'var(--ob-text-subtle)');
  assert.equal(css(action, '.visibility-select input:focus-visible + .visibility-check')['outline-offset'], '3px');
  assert.equal(css(action, '.visibility-action-menu .visibility-hide-turn').display, 'flex');
  assert.equal(css(action, '.visibility-action-menu .visibility-hide-turn', 390).display, 'none');
  for (const file of ['TurnList.vue']) {
    assert.equal(css(file, '.visibility-selected').background, undefined);
    assert.equal(css(file, '.visibility-selected')['box-shadow'], undefined);
    assert.equal(css(file, '.visibility-selected::after').width, '2px');
  }
  assert.match(descriptors['TurnList.vue'].template.content, /:target="entry.event" desktop-placement="gutter" :turn="turn"/);
  assert.doesNotMatch(descriptors['TurnWorkDetailPanel.vue'].template.content, /MessageVisibilityAction|visibilitySelectionClasses|selectVisibilityRow/);
  assert.equal(css('TurnWorkDetailPanel.vue', '.work-detail-entry').display, 'flex');
  assert.equal(css('TurnWorkDetailPanel.vue', '.visibility-selected::after').width, undefined);
});

test('desktop batch bar is compact and removed from flex sizing; restore entry leaves the header', () => {
  const bar = css('MessageVisibilityBar.vue', '.message-visibility-bar');
  assert.equal(bar.position, 'absolute'); assert.equal(bar.width, 'max-content'); assert.equal(bar.left, '50%');
  assert.equal(bar.bottom, 'calc(var(--console-composer-height, 135px) + 14px)');
  assert.doesNotMatch(descriptors['ConsoleHeader.vue'].template.content, /desktop-actions|hidden-content/);
  const template = descriptors['ConsoleView.vue'].template.content;
  const controls = template.slice(template.indexOf('<aside class="console-controls"'), template.indexOf('</aside>'));
  assert.match(controls, /hidden-content-toggle/);
  assert.equal(css('ConsoleView.vue', '.hidden-content-toggle').position, 'fixed');
  assert.equal(css('ConsoleView.vue', '.console-controls', 390).display, 'none');
});

test('desktop row click selects once, protects links, and leaves checkbox clicks / text selection unchanged', () => {
  let toggles = 0, prevented = 0, stopped = 0;
  const value = {opId: 'a'}, visibility = {selecting: ref(true), busy: ref(false), selected: ref(new Set(['a'])), canTarget: () => true, toggle: () => toggles++};
  const event = {target: {closest: () => null}, preventDefault: () => prevented++, stopPropagation: () => stopped++};
  const desktop = {matchMedia: () => ({matches: true}), getSelection: () => ''};
  selectVisibilityRow(event, value, visibility, desktop);
  assert.deepEqual([toggles, prevented, stopped], [1, 1, 1]);
  assert.deepEqual(visibilitySelectionClasses(value, visibility), {'visibility-selectable': true, 'visibility-selected': true});
  selectVisibilityRow({...event, target: {closest: () => ({})}}, value, visibility, desktop);
  selectVisibilityRow(event, value, visibility, {...desktop, getSelection: () => 'selected text'});
  assert.equal(toggles, 1);
  visibility.busy.value = true;
  selectVisibilityRow(event, value, visibility, desktop);
  assert.equal(toggles, 1);
  visibility.selecting.value = false;
  assert.equal(visibilitySelectionClasses(value, visibility)['visibility-selected'], false);
});

test('Escape exits desktop selection unless a dialog or in-flight save owns the interaction', () => {
  let cancelled = 0, prevented = 0;
  const visibility = {selecting: ref(true), busy: ref(false), managing: ref(false), selected: ref(new Set()), undoIds: ref([]), cancelSelection: () => cancelled++};
  const context = vm.createContext({defineProps: () => ({workDetailOpen: false}), useMessageVisibility: () => visibility, watch() {}, onMounted() {}, onBeforeUnmount() {},
    window: {matchMedia: () => ({matches: true})}, event: {key: 'Escape', preventDefault: () => prevented++}});
  vm.runInContext(descriptors['MessageVisibilityBar.vue'].scriptSetup.content.replace(/^import .*;\n/gm, ''), context);
  vm.runInContext('onKeydown(event)', context);
  assert.deepEqual([cancelled, prevented], [1, 1]);
  visibility.managing.value = true;
  vm.runInContext('onKeydown(event)', context);
  assert.equal(cancelled, 1);
});
