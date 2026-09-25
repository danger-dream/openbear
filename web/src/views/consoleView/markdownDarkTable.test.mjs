import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse, compileStyle} from '@vue/compiler-sfc';
import postcss from 'postcss';
import {themeValues, themeColor, contrast} from '../../testHelpers/theme.mjs';

const source = readFileSync(new URL('./ConsoleMarkdown.vue', import.meta.url), 'utf8');
const {descriptor} = parse(source);
const [style] = descriptor.styles;
function rules(css, selector) {
  const matched = [];
  postcss.parse(css).walkRules(rule => {
    if (rule.selectors.includes(selector)) matched.push(Object.fromEntries(rule.nodes.filter(node => node.type === 'decl').map(node => [node.prop, node.value])));
  });
  return Object.assign({}, ...matched);
}

test('both theme Markdown tables keep scoped v-html headers, body, stripes and borders distinct', () => {
  assert.equal(style.scoped, true);
  const compiled = compileStyle({source: style.content, filename: 'ConsoleMarkdown.vue', id: 'data-v-markdown', scoped: true});
  assert.deepEqual(compiled.errors, []);
  assert.match(compiled.code, /\.bear-md\[data-v-markdown\] th/);
  const header = rules(style.content, '.bear-md :deep(th)');
  const body = rules(style.content, '.bear-md :deep(td)');
  const stripe = rules(style.content, '.bear-md :deep(tr:nth-child(even) td)');
  for (const dark of [false, true]) {
    const values = themeValues(dark);
    const headerBg = themeColor(header.background, values);
    const bodyBg = themeColor(body.background, values);
    const stripeBg = themeColor(stripe.background, values);
    assert.notDeepEqual(headerBg, bodyBg);
    assert.notDeepEqual(stripeBg, bodyBg);
    assert.ok(contrast(themeColor(header.color, values), headerBg) > 7);
    assert.ok(contrast(themeColor(body.color, values), bodyBg) > 7);
  }
  assert.equal(header.border, '1px solid var(--ob-border)');
  assert.equal(body.border, '1px solid var(--ob-border)');
  assert.equal(rules(style.content, '.bear-md :deep(.md-table-scroll)')['overflow-x'], 'auto');
  assert.equal(rules(style.content, '.bear-md :deep(table)').width, 'max-content');
  assert.equal(header['white-space'], 'nowrap');
});
