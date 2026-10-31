import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import {parse, compileStyle} from '@vue/compiler-sfc';
import {themeValues, themeColor, contrast} from '../../testing/theme.mjs';

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
  ]) {
    const style = rules(file,selector);
    assert.equal(style.background,'var(--ob-chat-selected)');
    assert.equal(style.color,'var(--ob-chat-text)');
  }
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
