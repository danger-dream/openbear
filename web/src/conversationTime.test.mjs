import test from 'node:test';
import assert from 'node:assert/strict';
import {createRenderer, h, nextTick} from 'vue';
import {conversationTimeParts, groupConversationTimes, useConversationDay} from './conversationTime.js';

const local = (year, month, day, hour, minute, second) => new Date(year, month - 1, day, hour, minute, second).getTime();

test('conversation timestamps retain only the clock today and show the full local date on earlier days', () => {
  const today = local(2026, 11, 4, 0, 10, 5);
  const yesterday = local(2026, 11, 3, 23, 50, 9);
  assert.deepEqual(conversationTimeParts(today, '2026-11-04'), {date: '', time: '00:10:05', full: '2026-11-04 00:10:05'});
  assert.deepEqual(conversationTimeParts(yesterday, '2026-11-04'), {date: '2026-11-03', time: '23:50:09', full: '2026-11-03 23:50:09'});
  assert.deepEqual(conversationTimeParts(local(2025, 12, 31, 23, 59, 59), '2026-01-01'), {date: '2025-12-31', time: '23:59:59', full: '2025-12-31 23:59:59'});
});

test('Agent activity groups older dates without widening the time rail', () => {
  const a = local(2026, 11, 2, 22, 1, 2);
  const b = local(2026, 11, 2, 22, 2, 3);
  const c = local(2026, 11, 3, 9, 3, 4);
  const now = local(2026, 11, 4, 0, 4, 5);
  const lines = groupConversationTimes([
    {ts: a / 1000, timeLabel: 'old'}, {timeMs: b, timeLabel: 'old'}, {ts: c},
    {ts: now, timeLabel: 'today'}, {timeLabel: '运行中'},
  ], '2026-11-04');
  assert.deepEqual(lines.map(line => line.dateHeading), ['2026-11-02', '', '2026-11-03', '', '']);
  assert.deepEqual(lines.map(line => line.displayTime), ['22:01:02', '22:02:03', '09:03:04', '00:04:05', '运行中']);
  assert.equal(lines[1].timestampTitle, '2026-11-02 22:02:03');
});

test('the shared day clock refreshes at midnight and is cleaned up after the last view', async t => {
  const RealDate = Date;
  let now = local(2026, 11, 3, 23, 59, 59);
  class ClockDate extends RealDate {
    constructor(...args) {super(...(args.length ? args : [now]));}
    static now() {return now;}
  }
  const timers = new Map(), listeners = new Map(); let nextId = 0;
  const prior = {Date: globalThis.Date, window: globalThis.window, document: globalThis.document, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout};
  globalThis.Date = ClockDate;
  globalThis.setTimeout = (fn, ms) => {const id = ++nextId; timers.set(id, {fn, ms}); return id;};
  globalThis.clearTimeout = id => timers.delete(id);
  const events = {addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name)};
  globalThis.window = events;
  globalThis.document = {...events, visibilityState: 'visible'};
  const renderer = createRenderer({
    createElement: type => ({type, children: [], text: ''}), createText: text => ({text}), createComment: () => ({}),
    setText: (node, text) => {node.text = text;}, setElementText: (node, text) => {node.text = text;},
    patchProp() {}, insert: (node, parent) => {parent.children.push(node);}, remove() {}, parentNode: () => null, nextSibling: () => null,
  });
  const view = () => ({setup() {const day = useConversationDay(); return () => h('span', day.value);}});
  const root1 = {children: []}, root2 = {children: []};
  const app1 = renderer.createApp(view()), app2 = renderer.createApp(view());
  try {
    app1.mount(root1); app2.mount(root2); await nextTick();
    assert.equal(root1.children[0].text, '2026-11-03');
    assert.equal(timers.size, 1, 'multiple views share one midnight timer');
    now = local(2026, 11, 4, 0, 0, 1);
    const [id, timer] = [...timers][0]; timers.delete(id); timer.fn(); await nextTick();
    assert.equal(root1.children[0].text, '2026-11-04');
    assert.equal(root2.children[0].text, '2026-11-04');
    app1.unmount(); assert.equal(timers.size, 1);
    app2.unmount(); assert.equal(timers.size, 0); assert.equal(listeners.size, 0);
  } finally {
    Object.assign(globalThis, prior);
  }
});

test('a cached time becomes dated after local midnight and invalid times remain unknown', () => {
  const timestamp = local(2026, 11, 3, 23, 59, 59);
  assert.equal(conversationTimeParts(timestamp, '2026-11-03').date, '');
  assert.equal(conversationTimeParts(timestamp, '2026-11-04').date, '2026-11-03');
  for (const value of [0, null, undefined, NaN, Infinity, -1, 9e20]) {
    assert.deepEqual(conversationTimeParts(value, '2026-11-04'), {date: '', time: '', full: ''});
  }
});
