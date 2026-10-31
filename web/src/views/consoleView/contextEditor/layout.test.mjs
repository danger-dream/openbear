import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import selectorParser from 'postcss-selector-parser';
import {parse} from '@vue/compiler-sfc';

const styles = postcss.parse(readFileSync(new URL('./style.css', import.meta.url), 'utf8'));
const code = parse(readFileSync(new URL('./CodeEditor.vue', import.meta.url), 'utf8')).descriptor;
const editorStyles = postcss.parse(code.styles.map(s => s.content).join('\n'));
// Respect :where()/:not() specificity: merely collecting the last declaration
// missed the original width:100% reset overriding the footer's narrower input.
function specificity(selector) {
  const score = node => {
    if (node.type === 'root') return Math.max(0, ...node.nodes.map(score));
    if (node.type === 'selector') return node.nodes.reduce((sum, child) => sum + score(child), 0);
    if (node.type === 'id') return 1000000;
    if (node.type === 'class' || node.type === 'attribute') return 1000;
    if (node.type === 'tag') return 1;
    if (node.type === 'pseudo') {
      if (node.value === ':where') return 0;
      if ([':not', ':is', ':has'].includes(node.value)) return Math.max(0, ...node.nodes.map(score));
      return node.value.startsWith('::') ? 1 : 1000;
    }
    return 0;
  };
  return score(selectorParser().astSync(selector));
}
function css(matching, width = 1424, height = 1258, root = styles) {
  const result = {}, ranks = {}; matching = new Set([matching].flat());
  root.walkRules(rule => {
    for (let parent = rule.parent; parent; parent = parent.parent) {
      if (parent.type !== 'atrule' || parent.name !== 'media') continue;
      for (const match of parent.params.matchAll(/(max|min)-(width|height):\s*(\d+)px/g)) {
        const actual = match[2] === 'width' ? width : height;
        if (match[1] === 'max' ? actual > +match[3] : actual < +match[3]) return;
      }
    }
    for (const selector of rule.selectors.filter(s => matching.has(s))) {
      rule.walkDecls(decl => {
        const rank = specificity(selector) + (decl.important ? 1e9 : 0);
        if (rank >= (ranks[decl.prop] ?? -1)) { ranks[decl.prop] = rank; result[decl.prop] = decl.value; }
      });
    }
  });
  return result;
}
const inputBase = '.ce-surface :where(input:not([type=checkbox]):not([type=radio]):not([type=file]),select,textarea):not(:where(.ce-code-editor *))';
const buttonBase = '.ce-surface :where(button):not(:where(.ce-code-editor *))';
const sizes = [[1424,1258],[1440,900],[1024,768],[900,550],[390,844],[800,390]];

test('all document modes use a non-scrolling flex shell and content-sized remaining space', () => {
  for (const [w,h] of sizes) {
    const shell=css('.context-editor .ce-document-scroll',w,h);
    assert.equal(shell.display,'flex');assert.equal(shell['flex-direction'],'column');assert.equal(shell.overflow,'hidden');assert.equal(shell['min-height'],'0');
    for (const selector of ['.context-editor .ce-editor-fill','.context-editor .ce-body-section','.context-editor .ce-message-section','.context-editor .ce-body-editor','.context-editor .ce-diff-editor']) {
      const node=css(selector,w,h);assert.equal(node.flex,'1',selector);assert.equal(node['min-height'],'0',selector);assert.equal(node.overflow,'hidden',selector);assert.equal(node.height,undefined,selector);
    }
    assert.equal(css('.context-editor .ce-footer',w,h).flex,'none');
    assert.equal(css('.ce-monaco',w,h,editorStyles)['min-height'],'0');
    assert.equal(css('.ce-code-editor',w,h,editorStyles).height,'100%');
  }
});

test('tool lists and native fields own scrolling instead of growing their document parent', () => {
  for (const [w,h] of sizes) {
    const table=css(['.ce-surface .ce-table-wrap','.context-editor .ce-table-fill'],w,h);
    assert.equal(table.flex,'1');assert.equal(table['min-height'],'0');assert.equal(table.overflow,'auto');
    assert.equal(css('.ce-surface .ce-table th',w,h).position,'sticky');
    const fields=css('.context-editor .ce-field-list',w,h);assert.equal(fields.flex,'1');assert.equal(fields['min-height'],'0');assert.equal(fields.overflow,'auto');
    assert.equal(css('.context-editor .ce-preview-validation',w,h)['max-height'],'32%');
  }
});

test('form reset loses to footer grid, model selector, navigation and primary button styles', () => {
  assert.equal(specificity(inputBase),1000);assert.equal(specificity(buttonBase),1000);
  const submit=css(['.ce-surface .ce-actions','.context-editor .ce-submit']);assert.equal(submit.display,'grid');assert.equal(submit['grid-template-columns'],'auto auto minmax(100px,145px) auto auto');
  assert.equal(css([inputBase,'.context-editor .ce-footer input','.context-editor .ce-submit input']).width,'100%');
  assert.equal(css([inputBase,'.context-editor .ce-request select']).width,'auto');
  assert.equal(css([inputBase,'.ce-surface .ce-tools-toolbar input']).width,'min(330px,60%)');
  assert.equal(css([buttonBase,'.context-editor .ce-outline-link']).border,'0');
  assert.equal(css([buttonBase,'.ce-surface .ce-primary']).background,'var(--ob-chat-button)');
  assert.equal(css(['.ce-surface .ce-actions','.context-editor .ce-submit'],390,844)['grid-template-columns'],'minmax(0,1fr) minmax(0,1fr)');
});

test('edit dialogs fit the viewport once, with fixed actions and no body-level nested scrollbar', () => {
  for (const [w,h] of sizes) {
    const dialog=css(['.ce-dialog.el-dialog','.ce-edit-dialog.el-dialog'],w,h);assert.equal(dialog.height,'min(820px,calc(100dvh - 32px))');assert.equal(dialog['margin-bottom'],'16px');assert.equal(dialog.overflow,'hidden');assert.equal(dialog['box-sizing'],'border-box');
    for (const selector of ['.ce-dialog .el-dialog__body','.ce-dialog-content','.ce-dialog .ce-dialog-editor']) {
      const node=css(selector,w,h);assert.equal(node.flex,'1',selector);assert.equal(node['min-height'],'0',selector);assert.equal(node.overflow,'hidden',selector);assert.equal(node.height,undefined,selector);
    }
    assert.equal(css('.ce-dialog .el-dialog__header',w,h).flex,'none');assert.equal(css('.ce-dialog .el-dialog__footer',w,h).flex,'none');
    assert.equal(css('.ce-dialog .ce-impact',w,h)['min-height'],'0');assert.equal(css('.ce-dialog .ce-impact',w,h).overflow,'auto');
  }
});

test('difference panes share the available height rather than fixed-height cards inside another scroller', () => {
  for (const [w,h] of sizes) {
    const panes=css('.context-editor .ce-diff-panes',w,h);assert.equal(panes.flex,'1');assert.equal(panes['min-height'],'0');assert.equal(panes.overflow,'hidden');
    assert.equal(panes['grid-template-rows'],w<=760?'minmax(0,1fr) minmax(0,1fr)':'minmax(0,1fr)');
    const item=css('.context-editor .ce-diff-panes>div',w,h);assert.equal(item.display,'flex');assert.equal(item['min-height'],'0');
  }
});

test('short mobile editor removes the fixed tree from the document pane and keeps save/return outside scrolling controls', () => {
  assert.equal(css('.context-editor .ce-content').display, 'contents');
  const content = css(['.context-editor .ce-content', '.context-editor.is-compact .ce-content'],390,260);
  assert.equal(content.flex,'1');assert.equal(content['min-height'],'0');assert.equal(content.overflow,'hidden');
  const workspace = css(['.context-editor .ce-workspace','.context-editor.is-compact .ce-workspace'],390,260);
  assert.equal(workspace.display,'flex');
  assert.equal(css('.context-editor.is-compact[data-compact-panel="document"] .ce-outline',390,260).display,'none');
  assert.equal(css('.context-editor.is-compact[data-compact-panel="outline"] .ce-document',390,260).display,'none');
  assert.equal(css('.context-editor.is-compact[data-compact-panel="controls"] .ce-workspace',390,260).display,'none');
  assert.equal(css('.context-editor.is-compact .ce-compact-toolbar',390,260).flex,'none');
  assert.equal(css('.context-editor.is-compact[data-compact-panel="controls"] .ce-content',390,260)['overflow-y'],'auto');
  const source=readFileSync(new URL('../ContextEditor.vue',import.meta.url),'utf8');
  const {descriptor}=parse(source);const template=descriptor.template.content;
  const rail=template.slice(template.indexOf('class="ce-compact-toolbar"'),template.indexOf('<div class="ce-content">'));
  assert.match(rail,/@click="close"/);assert.match(rail,/@click="saveDraft"/);
  for(const name of ['outline','document','controls'])assert.ok(rail.includes(`switchCompactPanel('${name}')`));
  // The normal-height directory, original selectors/footer and drafts are not
  // destroyed when changing compact panels; CSS changes only their visibility.
  assert.match(template,/<aside class="ce-outline"/);assert.match(template,/<footer class="ce-footer">/);
});

test('ordered blocks have one content scroller without fixed-height nested editors', () => {
  for (const [w,h] of sizes) {
    const list=css('.context-editor .ce-sequence-scroll',w,h);
    assert.equal(list.flex,'1');assert.equal(list['min-height'],'0');assert.equal(list.overflow,'auto');
    assert.equal(css('.context-editor .ce-block-preview',w,h).overflow,'hidden');
    assert.equal(css('.context-editor .ce-block',w,h)['grid-template-columns'],w<=760?'22px minmax(0,1fr)':'28px minmax(0,1fr) auto');
  }
});
