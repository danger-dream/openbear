import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { parse } from '@vue/compiler-sfc';
import postcss from 'postcss';

const read = (file) => fs.readFileSync(new URL(file, import.meta.url), 'utf8');

function rules(source, selector) {
  const styles = parse(source).descriptor.styles;
  const out = {};
  for (const style of styles) {
    postcss.parse(style.content).walkRules((rule) => {
      if (rule.selectors.includes(selector)) {
        rule.walkDecls((d) => {
          out[d.prop] = d.value;
        });
      }
    });
  }
  return out;
}

test('desktop sticky note sits above work-items on the right rail and uses neutral theme tokens', () => {
  const notesSource = read('./ConversationNotes.vue');
  const selSource = read('./ConversationNotesSelection.vue');
  const consoleSource = read('./ConsoleView.vue');
  const treeSource = read('../../components/ConversationTree.vue');

  // 1. Left conversation tree remains completely untouched by notes.
  assert.doesNotMatch(treeSource, /notesSettings|userNotes|note_uuid|便签/);

  // 2. Rail entry sits at slot -4 above WorkItemsDrawer (-3), hidden on phones.
  const entryWrap = rules(notesSource, '.notes-entry-wrap');
  assert.match(entryWrap.top, /- 4 \* \(var\(--console-float-control-size/);
  assert.equal(entryWrap.position, 'fixed');

  // 3. Popover and selection overlays use OpenBear neutral surface tokens (no yellow paper tint).
  const pop = rules(notesSource, '.notes-sticky-popover');
  assert.match(pop.background, /var\(--ob-surface-rgb\)/);
  assert.equal(pop.width, '328px');

  const ctx = rules(selSource, '.notes-ctx-menu');
  assert.match(ctx.background, /var\(--ob-surface-rgb\)/);

  const toolbar = rules(selSource, '.notes-sel-toolbar');
  assert.match(toolbar.background, /var\(--ob-surface-rgb\)/);

  // 4. Mobile inputs inside sticky note sheet use >=16px to prevent iOS zoom.
  assert.match(notesSource, /\.notes-compose\.is-mobile textarea[\s\S]*?font-size:\s*16px/);

  // 5. ConsoleView mounts both selection handler and rail component, with return-to-notes support.
  assert.match(consoleSource, /<ConversationNotesSelection[^>]+:scroller="scroller"/);
  assert.match(consoleSource, /<ConversationNotes @locate="locateFromNotes"/);
  assert.match(consoleSource, /返回便签/);
});
