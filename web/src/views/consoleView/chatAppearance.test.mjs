import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import {parse, compileStyle} from '@vue/compiler-sfc';
import {themeValues, themeColor, contrast} from '../../testHelpers/theme.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
function rules(file, selector) {
  const descriptor = parse(read(file)).descriptor, result = {};
  for (const style of descriptor.styles) {
    const compiled = compileStyle({source:style.content,filename:file,id:'data-v-appearance',scoped:style.scoped});
    assert.deepEqual(compiled.errors, []);
    postcss.parse(style.content).walkRules(rule => {
      if (rule.parent.type === 'root' && rule.selectors.includes(selector)) rule.walkDecls(d => {result[d.prop] = d.value;});
    });
  }
  return result;
}

for (const dark of [false, true]) test(`${dark ? 'dark' : 'light'} approved chat palette keeps readable text on neutral layered surfaces`, () => {
  const values = themeValues(dark), color = role => themeColor(`var(--ob-chat-${role})`, values);
  assert.deepEqual(color('bg'), dark ? [28,28,30] : [248,248,248]);
  assert.deepEqual(color('sidebar'), dark ? [22,22,24] : [240,240,240]);
  for (const surface of ['bg','sidebar','panel','bubble']) {
    assert.ok(contrast(color('text'),color(surface)) >= 7, `${surface}: primary text`);
    assert.ok(contrast(color('subtle'),color(surface)) >= 3.8, `${surface}: secondary controls`);
  }
  assert.ok(contrast(color('button-text'),color('button')) >= 7);
  const selected = values.get('--ob-chat-selected');
  assert.match(selected, dark ? /255 255 255/ : /20 20 24/);
});

test('recent and tree selection use the same neutral background and readable foreground', () => {
  for (const [file,selector] of [
    ['../../components/ConversationTree.vue','.tree-node-main.is-chat-active'],
    ['../../components/ConversationActivityFolder.vue','.activity-row.is-selected'],
  ]) {
    const style = rules(file,selector);
    assert.equal(style.background,'var(--ob-chat-selected)');
    assert.equal(style.color,'var(--ob-chat-text)');
  }
});

test('completed recent conversations use a quiet neutral dot while actionable states retain their colors', () => {
  const file = '../../components/ConversationActivityFolder.vue';
  const completed = rules(file, '.activity-state-dot.is-completed');
  assert.equal(completed.background, 'var(--ob-chat-muted)');
  assert.equal(completed['border-color'], 'var(--ob-chat-muted)');
  assert.equal(rules(file, '.activity-state-dot.is-running')['border-top-color'], 'var(--ob-blue)');
  assert.equal(rules(file, '.activity-state-dot.is-waiting').background, 'var(--ob-warning)');
  assert.equal(rules(file, '.activity-state-dot.is-failed').background, 'var(--ob-danger)');
});

test('desktop controls are quiet rounded squares, keeping active state distinct from neutral resting state', () => {
  for (const [file,selector] of [
    ['./ConsoleView.vue','.scroll-lock-toggle'],
    ['./ConsoleView.vue','.hidden-content-toggle'],
    ['./TaskMemoryDrawer.vue','.task-memory-entry'],
  ]) {
    const style = rules(file,selector);
    assert.equal(style['border-radius'],'7px');
    assert.equal(style.background,'var(--ob-chat-bg)');
    assert.equal(style.color,'var(--ob-chat-subtle)');
  }
  for (const [file,selector] of [
    ['./ConsoleView.vue','.scroll-lock-toggle.locked'],
    ['./TaskMemoryDrawer.vue','.task-memory-entry.active'],
  ]) assert.equal(rules(file,selector).background,'var(--ob-chat-selected)');
});

test('turn navigation rail blends into the transcript without a rounded floating surface', () => {
  const style = rules('./TurnMinimap.vue', '.turn-minimap-rail');
  assert.equal(style['border-radius'], '0');
  assert.equal(style.background, 'transparent');
  assert.equal(style['backdrop-filter'], 'none');
  assert.equal(style['box-shadow'], 'none');
});

test('desktop hides the duplicate usage trigger while phones retain their existing meter', () => {
  const source = parse(read('./ConsoleComposer.vue')).descriptor.styles.map(style => style.content).join('\n');
  const css = postcss.parse(source);
  const hidden = [];
  css.walkRules(rule => {
    if (!rule.selector.includes('.context-usage-trigger')) return;
    rule.walkDecls('display', decl => {
      if (decl.value === 'none') hidden.push({selector: rule.selector, media: rule.parent.params});
    });
  });
  assert.deepEqual(hidden, [{selector: '.composer-status :deep(.context-usage-trigger)', media: '(min-width: 761px)'}]);
  assert.equal(rules('./ContextUsageMeter.vue', '.context-usage-trigger').display, 'inline-flex');
  assert.notEqual(rules('./ConsoleComposer.vue', '.run-config-chip-meta').display, 'none');
  let phonePlacement;
  css.walkRules(rule => {
    if (rule.selector === '.composer-toolbar :deep(.context-usage-trigger)' && rule.parent.params === '(max-width: 760px)') phonePlacement = rule.toString();
  });
  assert.match(phonePlacement, /position: absolute; left: 50%; bottom: -34px/);
});

test('compact title retains path and numeric statistics, with one live context owner in composer', () => {
  const header = rules('./ConsoleHeader.vue','.header-title');
  assert.equal(header['font-size'],'13px');
  assert.equal(header['font-weight'],'500');
  const composer = read('./ConsoleComposer.vue');
  assert.equal((composer.match(/<ContextUsageMeter\b/g)||[]).length,1);
  assert.match(read('./ContextUsageMeter.vue'),/class="context-inline-value">\{\{ usedLabel \}\} \/ \{\{ triggerLabel \}\}/);
  const tools = read('./MobileConversationTools.vue');
  assert.match(tools,/<el-drawer v-model="menuOpen" direction="btt" size="auto"/);
  assert.equal(rules('./MobileConversationTools.vue','.mobile-conversation-menu .el-drawer__body')['overflow-y'],'auto');
});
