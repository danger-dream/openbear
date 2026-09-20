import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref} from 'vue';
import {chooseActiveTurnIndex} from './activeTurn.js';
import {captureTranscriptContentAnchor, transcriptContentAnchorDelta} from './transcriptContentAnchor.js';

// Run the component's real anchor, reflow and active-turn functions against a
// fake scroller. No browser, layout engine, network, service or Agent run is
// involved; only the rendered scroll geometry is a seam.
const source = fs.readFileSync(process.env.SCROLL_TEST_SOURCE || new URL('./ConsoleView.vue', import.meta.url), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `missing ${start}`);
  return source.slice(a, b);
};
const SCROLLER_CODE = section('// Read every turn\'s viewport box once;', 'function scrollToTurnIndex(');

// A transcript that re-wraps with the available width. Narrowing the column
// makes an earlier turn taller and pushes every later turn down the page, which
// is what opening the work-detail panel does. scrollTop is clamped to the
// physical range, including when the available height shrinks during reflow.
function scroller(width, turns) {
  const el = {
    clientWidth: width,
    clientHeight: 700,
    getBoundingClientRect: () => ({top: 0, bottom: el.clientHeight, height: el.clientHeight, left: 0, right: el.clientWidth}),
  };
  const laidOut = () => {
    let cursor = 0;
    return turns.map(({index, height}) => {
      const box = {index, top: cursor, bottom: cursor + height(el.clientWidth)};
      cursor = box.bottom;
      return box;
    });
  };
  const visible = () => laidOut().map(box => ({...box, top: box.top - el.scrollTop, bottom: box.bottom - el.scrollTop}));
  Object.defineProperty(el, 'scrollHeight', {get: () => laidOut().at(-1).bottom});
  let scrollTop = 0;
  Object.defineProperty(el, 'scrollTop', {
    get: () => (scrollTop = Math.max(0, Math.min(scrollTop, el.scrollHeight - el.clientHeight))),
    set: value => {scrollTop = Math.max(0, Math.min(value, el.scrollHeight - el.clientHeight));},
  });
  const nodes = turns.map(turn => {
    const node = {dataset: {turnIndex: String(turn.index)}, textNodes: [],
      getBoundingClientRect: () => visible().find(box => box.index === turn.index),
      contains: text => node.textNodes.includes(text),
    };
    if (turn.paragraph) node.textNodes.push({textContent: 'x'.repeat(turn.paragraph.length), parent: node, paragraph: turn.paragraph});
    return node;
  });
  // Only text measurement is simulated: production TreeWalker/Range selection
  // and binary search locate a real character in the width-dependent lines.
  const doc = {
    createTreeWalker: node => {let index = 0; return {nextNode: () => node.textNodes[index++] || null};},
    createRange: () => {
      let text, start = 0, end = 1;
      const rect = offset => {
        const top = text.parent.getBoundingClientRect().top + Math.floor(offset / text.paragraph.columns(el.clientWidth)) * 20;
        return {top, bottom: top + 20, height: 20, width: 10};
      };
      return {
        selectNodeContents: node => {text = node;},
        setStart: (node, offset) => {text = node; start = offset;}, setEnd: (_node, offset) => {end = offset;},
        getBoundingClientRect: () => {const first = rect(start), last = rect(end - 1); return {...first, bottom: last.bottom, height: last.bottom - first.top};},
        getClientRects: () => {
          const columns = text.paragraph.columns(el.clientWidth);
          return Array.from({length: Math.ceil(text.textContent.length / columns)}, (_, index) => rect(index * columns));
        },
      };
    },
  };
  for (const node of nodes) {node.ownerDocument = doc; for (const text of node.textNodes) text.ownerDocument = doc;}
  el.querySelectorAll = () => nodes;
  el.querySelector = selector => nodes.find(node => Number(node.dataset.turnIndex) === Number(/data-turn-index="(\d+)"/.exec(selector)?.[1])) || null;
  return el;
}

function harness(el, {locked = false} = {}) {
  const frames = [], observers = [];
  class FakeResizeObserver {
    constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
    observe(target) { this.targets.push(target); this.callback([{target}]); }
    disconnect() { this.targets = []; }
  }
  const context = vm.createContext({
    ref, el, chooseActiveTurnIndex, ResizeObserver: FakeResizeObserver,
    captureTranscriptContentAnchor, transcriptContentAnchorDelta,
    scroller: null,
    autoScrollLocked: ref(locked),
    activeTurnIndex: ref(0),
    performance: {now: () => 0},
    window: {
      requestAnimationFrame: callback => {frames.push(callback); return frames.length;},
      cancelAnimationFrame: () => {},
      setTimeout: () => 1,
      clearTimeout: () => {},
    },
    updateScrollerOverflow: () => {},
    scrollerAtBottom: () => el.scrollHeight - el.scrollTop - el.clientHeight <= 80,
    runProgrammaticScroll: fn => fn(),
    ACTIVE_TURN_SCROLL_UPDATE_MS: 140,
    SCROLL_BOTTOM_THRESHOLD: 80,
  });
  context.scroller = ref(el);
  vm.runInContext(`let pinnedActiveTurnIndex = null, readingAnchor = null, lastActiveTurnScrollUpdateAt = 0, activeTurnScrollFrame = 0, activeTurnScrollTimer = 0, observedScrollerWidth = 0, scrollerReflowFrame = 0;
${SCROLLER_CODE}`, context);
  return {context, frames, observers, run: code => vm.runInContext(code, context), flush: () => frames.splice(0).forEach(cb => cb())};
}

// Turn 1 is long and re-wraps 400 -> 900, so turn 2's document offset moves
// 700 -> 1200. A reader sitting at the top of turn 2 is the reported case.
const turns = [
  {index: 0, height: () => 300},
  {index: 1, height: w => (w > 1100 ? 400 : 900)},
  {index: 2, height: () => 1200}, // enough content below the anchor; no impossible scrollTop
];

test('the turn the reader is on survives the panel narrowing the transcript', () => {
  const el = scroller(1440, turns);
  const h = harness(el);
  el.scrollTop = 700;                       // turn 2 starts exactly at the viewport top
  h.run('updateActiveTurnFromScroll()');
  assert.equal(h.run('activeTurnIndex.value'), 2);
  assert.equal(h.run('readingAnchor.index'), 2);

  el.clientWidth = 990;                     // the work-detail panel opened
  h.run('compensateTranscriptReflow()');
  assert.equal(el.querySelector('[data-turn-index="2"]').getBoundingClientRect().top, 0, 'turn 2 stayed at the viewport top');
  assert.equal(el.scrollTop, 1200, 'the scroll offset followed the re-wrapped content');
  assert.equal(h.run('activeTurnIndex.value'), 2, 'the panel still describes the turn on screen, not the previous one');
});

test('without the compensation the same reflow makes the panel describe the previous turn', () => {
  const el = scroller(1440, turns);
  const h = harness(el);
  el.scrollTop = 700;
  h.run('updateActiveTurnFromScroll()');
  assert.equal(h.run('activeTurnIndex.value'), 2);

  el.clientWidth = 990;
  h.run('updateActiveTurnFromScroll()');    // what the stale-layout render does today
  assert.equal(el.scrollTop, 700, 'the stale pixel offset is left in place');
  assert.equal(h.run('activeTurnIndex.value'), 1, 'the reported symptom: the previous turn takes over');
});

test('a locked reader stays pinned to the newest turn instead of drifting up', () => {
  const el = scroller(1440, turns);
  const h = harness(el, {locked: true});
  el.scrollTop = 400;
  h.run('compensateTranscriptReflow()');
  assert.equal(el.scrollTop, el.scrollHeight - el.clientHeight, 'locked scroll follows the clamped bottom');
  assert.equal(h.frames.length, 0, 'the locked path re-derives its state immediately rather than waiting for a frame');
});

test('the compensation leaves the transcript alone without an observed reading position', () => {
  const el = scroller(1440, turns);
  const h = harness(el);
  el.scrollTop = 700;                       // updateActiveTurnFromScroll() never ran
  h.run('compensateTranscriptReflow()');
  assert.equal(el.scrollTop, 700);
  assert.deepEqual(h.frames, [], 'nothing is written without evidence of where the reader was');
});

test('a locked reader never restores a mid-transcript position', () => {
  const el = scroller(1440, turns);
  const h = harness(el, {locked: true});
  el.scrollTop = 700;
  h.run('updateActiveTurnFromScroll()');
  el.clientWidth = 990;
  h.run('compensateTranscriptReflow()');
  assert.notEqual(el.scrollTop, 1200, 'locked state must not treat history as the reading position');
});

function longTranscript() {
  const paragraph = {length: 10000, columns: w => Math.max(10, Math.floor(w / 14))};
  return scroller(1440, [
    {index: 0, height: () => 200},
    {index: 1, paragraph, height: w => Math.ceil(paragraph.length / paragraph.columns(w)) * 20},
    {index: 2, height: () => 1000},
  ]);
}
function characterTop(el, offset) {
  const turn = el.querySelector('[data-turn-index="1"]');
  const text = turn.textNodes[0];
  return turn.getBoundingClientRect().top + Math.floor(offset / text.paragraph.columns(el.clientWidth)) * 20;
}

test('C06: the same character inside a long paragraph stays on screen through narrowing and widening', () => {
  const el = longTranscript(), h = harness(el);
  el.scrollTop = 1200;
  h.run('updateActiveTurnFromScroll()');
  assert.equal(h.run('activeTurnIndex.value'), 1);
  // Independently identify the currently visible character. The old component
  // has no content anchor, but must still fail on visible drift, not a missing field.
  const offset = captureTranscriptContentAnchor(el.querySelector('[data-turn-index="1"]'), el.getBoundingClientRect()).offset;
  const top = characterTop(el, offset);
  for (const width of [1360, 1280, 1100, 990, 1050, 1200, 1440]) {
    el.clientWidth = width;
    h.run('compensateTranscriptReflow()');
    assert.equal(characterTop(el, offset), top, `original reading character at width ${width}`);
    assert.equal(h.run('readingAnchor.content.offset'), offset, 'multi-frame animation must not walk to a new line start');
    assert.equal(h.run('activeTurnIndex.value'), 1);
    assert.ok(el.scrollTop >= 0 && el.scrollTop <= el.scrollHeight - el.clientHeight);
  }
});

test('the observer only reacts to width and coalesces one reflow into a single frame', () => {
  const el = scroller(1440, turns);
  const h = harness(el);
  el.scrollTop = 700;
  h.run('updateActiveTurnFromScroll()');
  h.run('observeScrollerReflow()');
  assert.equal(h.observers.length, 1, 'the scroller is observed exactly once');
  assert.equal(h.frames.length, 0, 'the first callback only establishes the baseline');

  el.clientHeight = 400;                    // composer growth / keyboard: height only
  h.observers[0].callback([{target: el}]);
  assert.equal(h.frames.length, 0, 'a height-only change is left to its own restore paths');

  el.clientWidth = 990;
  h.observers[0].callback([{target: el}]);  // the 240 ms panel animation emits many of these
  h.observers[0].callback([{target: el}]);
  h.observers[0].callback([{target: el}]);
  assert.equal(h.frames.length, 1, 'the animation burst becomes one compensation');
  h.flush();
  assert.equal(el.scrollTop, 1200);
  assert.equal(h.run('activeTurnIndex.value'), 2);
});
test('C06: fixture clamps negative, bottom and short-transcript scroll positions', () => {
  const el = scroller(1440, turns);
  el.scrollTop = -100; assert.equal(el.scrollTop, 0);
  el.scrollTop = el.scrollHeight; assert.equal(el.scrollTop, el.scrollHeight - el.clientHeight);
  const short = scroller(1440, [{index: 0, height: () => 100}]);
  short.scrollTop = 900; assert.equal(short.scrollTop, 0);
});

test('C06: explicit user scrolling captures a new line, while locked reflow follows bottom', () => {
  const el = longTranscript(), h = harness(el);
  el.scrollTop = 1200; h.run('updateActiveTurnFromScroll()');
  const old = h.run('readingAnchor.content.offset');
  el.scrollTop += 400; h.run('updateActiveTurnFromScroll()');
  const next = h.run('readingAnchor.content.offset'), top = characterTop(el, next);
  assert.notEqual(next, old);
  el.clientWidth = 990; h.run('compensateTranscriptReflow()');
  assert.equal(characterTop(el, next), top);
  h.context.autoScrollLocked.value = true;
  el.clientWidth = 1440; h.run('compensateTranscriptReflow()');
  assert.equal(el.scrollTop, el.scrollHeight - el.clientHeight);
});

test('C06: detached/replaced text is not reused as a content anchor', () => {
  const el = longTranscript(), h = harness(el);
  el.scrollTop = 1200; h.run('updateActiveTurnFromScroll()');
  const turn = el.querySelector('[data-turn-index="1"]');
  const previous = h.run('readingAnchor.content');
  turn.textNodes = [];
  assert.equal(transcriptContentAnchorDelta(previous, turn, el.getBoundingClientRect()), null);
  el.clientWidth = 990; h.run('compensateTranscriptReflow()');
  assert.equal(el.scrollTop, 1200, 'existing turn-top fallback remains available after replacement');
  assert.equal(h.run('readingAnchor.content'), null);
});

test('C06: a cleared conversation anchor never scrolls the next transcript', () => {
  const el = longTranscript(), h = harness(el);
  el.scrollTop = 1200; h.run('updateActiveTurnFromScroll()');
  h.run('readingAnchor = null'); // the synchronous switch/reset boundary
  const next = longTranscript(); next.scrollTop = 300; next.clientWidth = 990;
  h.context.scroller.value = next;
  h.run('compensateTranscriptReflow()');
  assert.equal(next.scrollTop, 300);
});
