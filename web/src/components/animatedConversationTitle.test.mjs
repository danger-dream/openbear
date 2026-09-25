import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {effectScope, nextTick, reactive, ref, watch} from 'vue';
import {parse} from '@vue/compiler-sfc';
import {titleGraphemes} from '../conversationTitle.js';

const source = fs.readFileSync(new URL('./AnimatedConversationTitle.vue', import.meta.url), 'utf8');
const script = parse(source).descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, '');

function harness(t) {
  const props = reactive({identity: 'conversation-a', text: 'Alpha'});
  const mounted = [];
  const unmounted = [];
  const timers = new Map();
  let serial = 0;
  const window = {
    matchMedia: () => ({matches: false}),
    setTimeout(fn, delay) {timers.set(++serial, {fn, delay}); return serial;},
    clearTimeout(id) {timers.delete(id);},
  };
  const context = vm.createContext({
    ref, watch, titleGraphemes, window,
    defineProps: () => props,
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => unmounted.push(fn),
  });
  const scope = effectScope();
  scope.run(() => vm.runInContext(`${script}\nglobalThis.state={shown,blurring};`, context));
  mounted.forEach(fn => fn());
  t.after(() => {unmounted.forEach(fn => fn()); scope.stop();});
  return {
    props,
    state: context.state,
    runNext(delay) {
      const item = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(item, `missing ${delay}ms timer`);
      timers.delete(item[0]);
      item[1].fn();
    },
    timers,
  };
}

test('navigation changes title immediately while a rename of the same conversation animates', async t => {
  const h = harness(t);
  assert.equal(h.state.shown.value, 'Alpha');

  h.props.identity = 'conversation-b';
  h.props.text = 'Beta';
  await nextTick();
  assert.equal(h.state.shown.value, 'Beta');
  assert.equal(h.state.blurring.value, false);
  assert.equal(h.timers.size, 0);

  h.props.text = 'Renamed';
  await nextTick();
  assert.equal(h.state.shown.value, 'Beta');
  assert.equal(h.state.blurring.value, true);
  h.runNext(140);
  assert.equal(h.state.blurring.value, false);
  assert.equal(h.state.shown.value, 'R');
  assert.equal(h.timers.size, 1);
});
