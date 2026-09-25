import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { parse, compileTemplate, compileStyle } from '@vue/compiler-sfc';
import postcss from 'postcss';

const dir = dirname(fileURLToPath(import.meta.url));
const views = 'MemoryView SecretsView DocsView SkillsView McpView SettingsHubView SettingsView ChannelsView TemplateView RathAgentsView SessionsView StatisticsView LogsView LoginView InstallAppView'.split(' ');
const roles = new Set('bg sidebar header surface surface-raised surface-soft text text-strong text-subtle text-muted text-disabled text-inverse border border-soft border-strong hover selected focus mask blue success warning danger info violet orange blue-soft success-soft warning-soft danger-soft info-soft violet-soft orange-soft shadow-panel shadow-popover shadow-dialog shadow-inset scrollbar scrollbar-hover code-bg code-text'.split(' '));
const channels = new Set('bg sidebar header surface surface-raised surface-soft text text-strong text-subtle text-muted text-disabled text-inverse border blue success warning danger info violet orange'.split(' '));
const literal = /#[\da-f]{3,8}\b|rgba?\(\s*\d+\s*,\s*\d+/i;
const neutralUtility = /(?<![\w-])(?:bg|text|border|ring|outline|placeholder|divide)-(?:white|black|zinc|slate|gray|neutral)(?:-\d+)?(?:\/[^\s"']+)?/;

test('all 15 owned view templates and styles compile with contract-backed colors', () => {
  for (const name of views) {
    const filename = join(dir, `${name}.vue`);
    const source = readFileSync(filename, 'utf8');
    const { descriptor, errors } = parse(source, { filename });
    assert.deepEqual(errors, [], `${name}: invalid Vue SFC`);
    assert.ok(descriptor.template, `${name}: template retained`);
    const template = compileTemplate({ source: descriptor.template.content, filename, id: name });
    assert.deepEqual(template.errors, [], `${name}: template compile`);
    assert.doesNotMatch(descriptor.template.content, neutralUtility, `${name}: literal neutral utility`);
    assert.doesNotMatch(source, /--ob---ob-/, `${name}: broken palette reference`);
    assert.doesNotMatch(source, /\b(?:bg|text|border)-\[#[\da-f]+\]/i, `${name}: literal utility color`);
    for (const [index, style] of descriptor.styles.entries()) {
      const result = compileStyle({ source: style.content, filename, id: `data-v-${name}`, scoped: style.scoped });
      assert.deepEqual(result.errors, [], `${name}: style ${index} compile`);
      const css = postcss.parse(result.code, { from: filename });
      css.walkDecls(decl => {
        if (name !== 'StatisticsView' || decl.prop !== '--stat-cyan') {
          assert.doesNotMatch(decl.value, literal, `${name}: fixed CSS color in ${decl.prop}`);
        }
      });
    }
    for (const token of source.matchAll(/--ob-([\w-]+)/g)) {
      const role = token[1];
      assert.ok(role === 'shadow-rgb' || roles.has(role) || (role.endsWith('-rgb') && channels.has(role.slice(0, -4))), `${name}: undefined palette role ${token[0]}`);
    }
  }
});

test('statistics keeps differentiated chart series and semantic outcome colors', () => {
  const stats = readFileSync(join(dir, 'StatisticsView.vue'), 'utf8');
  assert.match(stats, /const COLORS = \["#087af5", "#7656d6", "#16a36a", "#df8b0b", "#0e98a7", "#e5484d"/);
  assert.match(stats, /--stat-green:var\(--ob-success\)/);
  assert.match(stats, /--stat-red:var\(--ob-danger\)/);
  assert.match(stats, /--stat-bg:var\(--ob-bg\)/);
  assert.doesNotMatch(stats, /html\.dark \.statistics-view/);
  const channels = readFileSync(join(dir, 'ChannelsView.vue'), 'utf8');
  assert.match(channels, /\.mac-primary-button \{\s*background: var\(--ob-blue\) !important;\s*color: var\(--ob-text-inverse\)/);
  assert.match(channels, /html\.dark \.mobile-channel-pill\.is-active \{\s*background: var\(--ob-selected\);\s*border-color: var\(--ob-blue\);\s*color: var\(--ob-blue\)/);
});
