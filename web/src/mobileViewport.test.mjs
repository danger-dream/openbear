import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { nextTick, ref } from 'vue';
import { installMobileViewport, MOBILE_VIEWPORT_QUERY } from './mobileViewport.js';
import {captureTranscriptContentAnchor, transcriptContentAnchorDelta} from './views/consoleView/transcriptContentAnchor.js';

function target(extra = {}) {
  const listeners = new Map();
  return Object.assign(extra, {
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    emit(type) { for (const fn of listeners.get(type) || []) fn({ type }); },
    count() { return [...listeners.values()].reduce((n, set) => n + set.size, 0); },
  });
}
function environment({ mobile = true, visual = true } = {}) {
  const writes = [], styles = new Map(), attributes = new Map(), frames = new Map();
  let frameId = 0;
  const root = {
    style: {
      getPropertyValue: key => styles.get(key)?.[0] || '', getPropertyPriority: key => styles.get(key)?.[1] || '',
      setProperty(key, value, priority = '') { writes.push([key, value]); styles.set(key, [value, priority]); },
      removeProperty(key) { writes.push([key, null]); styles.delete(key); },
    },
    getAttribute: key => attributes.get(key) ?? null,
    setAttribute(key, value) { writes.push([key, value]); attributes.set(key, value); },
    removeAttribute(key) { writes.push([key, null]); attributes.delete(key); },
  };
  const media = target({ matches: mobile }), standalone = target({ matches: false });
  const viewport = visual ? target({ height: 800, width: 390, offsetTop: 0, offsetLeft: 0, scale: 1 }) : null;
  const win = target({
    innerHeight: 800, innerWidth: 390, visualViewport: viewport, document: { documentElement: root },
    matchMedia: query => query === MOBILE_VIEWPORT_QUERY ? media : standalone,
    requestAnimationFrame(fn) { const id = ++frameId; frames.set(id, fn); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
    scrollTo() { assert.fail('must not force document scroll'); },
  });
  return { win, root, media, standalone, viewport, writes, styles, attributes, frames,
    flush() { const batch = [...frames.values()]; frames.clear(); batch.forEach(fn => fn()); },
    value: key => root.style.getPropertyValue(`--mobile-viewport-${key}`),
  };
}

test('wide fine-pointer desktop never receives viewport attributes/styles even through resize/scroll/cleanup', () => {
  for (const visual of [true, false]) {
    const h = environment({ mobile: false, visual });
    const stop = installMobileViewport({ window: h.win, beforeChange() { assert.fail('desktop callback'); } });
    h.win.emit('resize'); h.viewport?.emit('scroll'); h.flush(); stop();
    assert.deepEqual(h.writes, []); assert.equal(h.win.count(), 0); assert.equal(h.media.count(), 0);
  }
});

test('phone keyboard resize, viewport offset/scroll, keyboard dismissal and rotation track one visual height', () => {
  const h = environment(), calls = [];
  const stop = installMobileViewport({ window: h.win, beforeChange: () => { calls.push('before'); return 'anchor'; }, afterChange: anchor => calls.push(anchor) });
  assert.equal(h.value('height'), '800px');
  h.viewport.height = 430; h.viewport.offsetTop = 35;
  h.viewport.emit('resize'); h.viewport.emit('scroll'); h.win.emit('resize');
  assert.equal(h.frames.size, 1); h.flush();
  assert.equal(h.value('height'), '430px'); assert.equal(h.value('top'), '35px');
  h.viewport.offsetTop = 60; h.viewport.emit('scroll'); h.flush(); assert.equal(h.value('top'), '60px');
  h.viewport.height = 800; h.viewport.offsetTop = 0; h.viewport.emit('resize'); h.flush();
  assert.equal(h.value('height'), '800px'); assert.equal(h.value('top'), '0px');
  h.win.innerWidth = 844; h.viewport.width = 844; h.viewport.height = 390;
  h.win.emit('orientationchange'); h.flush(); assert.equal(h.value('height'), '390px');
  h.standalone.emit('change'); h.flush(); h.win.emit('pageshow'); h.flush();
  assert.deepEqual(calls, Array.from({ length: 7 }, () => ['before', 'anchor']).flat());
  stop(); assert.equal(h.attributes.size, 0); assert.equal(h.styles.size, 0);
});

test('fallback without VisualViewport, desktop transition and teardown restore previous values/priorities exactly', () => {
  const h = environment({ visual: false });
  h.root.style.setProperty('--mobile-viewport-height', '99px', 'important');
  h.root.setAttribute('data-openbear-mobile-viewport', 'previous-owner');
  const stop = installMobileViewport({ window: h.win });
  h.win.innerHeight = 410; h.win.emit('resize'); h.flush(); assert.equal(h.value('height'), '410px');
  h.media.matches = false; h.media.emit('change'); h.flush();
  assert.deepEqual(h.styles.get('--mobile-viewport-height'), ['99px', 'important']);
  assert.equal(h.value('top'), ''); assert.equal(h.attributes.get('data-openbear-mobile-viewport'), 'previous-owner');
  h.media.matches = true; h.media.emit('change'); h.flush();
  h.win.emit('resize'); assert.equal(h.frames.size, 1); stop(); stop();
  assert.equal(h.frames.size, 0); assert.equal(h.win.count() + h.media.count() + h.standalone.count(), 0);
  assert.deepEqual(h.styles.get('--mobile-viewport-height'), ['99px', 'important']);
});

test('pinch zoom and zoom pan remain native; layout resumes only when scale returns to one', () => {
  const h = environment(); const stop = installMobileViewport({ window: h.win }); const before = h.writes.length;
  h.viewport.scale = 2; h.viewport.height = 220; h.viewport.offsetTop = 140;
  h.viewport.emit('resize'); h.viewport.emit('scroll'); h.flush();
  assert.equal(h.writes.length, before); assert.equal(h.value('height'), '800px');
  h.viewport.scale = 1; h.viewport.height = 440; h.viewport.offsetTop = 20;
  h.viewport.emit('resize'); h.flush(); assert.equal(h.value('height'), '440px');
  stop(); assert.equal(h.viewport.count(), 0);
});

const settle = async () => { for (let i = 0; i < 8; i++) await nextTick(); };
const source = fs.readFileSync(new URL('./views/consoleView/ConsoleView.vue', import.meta.url), 'utf8');
const actualAnchors = source.slice(source.indexOf('function captureMobileViewportAnchor()'), source.indexOf('function updateActiveTurnFromScroll()'));
function anchorHarness() {
  const props = { conversationUuid: 'A' }, writes = [];
  let shift = 0, scrollTop = 300;
  const node = { dataset: { turnIndex: '2' }, getBoundingClientRect: () => ({ top: 80 + shift - (scrollTop - 300), bottom: 200 + shift - (scrollTop - 300) }) };
  const scroller = { get scrollTop() { return scrollTop; }, set scrollTop(value) { scrollTop = value; writes.push(value); },
    scrollHeight: 2400, clientHeight: 500, getBoundingClientRect: () => ({ top: 100, bottom: 600 }), querySelectorAll: () => [node], querySelector: () => node };
  const ctx = vm.createContext({ props, ref, nextTick, captureTranscriptContentAnchor, transcriptContentAnchorDelta,
    scroller: ref(scroller), autoScrollLocked: ref(false),
    runProgrammaticScroll: fn => fn(), updateScrollerOverflow() {}, scheduleActiveTurnFromScroll() {} });
  vm.runInContext('let componentMounted = true, loadRequestGeneration = 1;\n' + actualAnchors, ctx);
  const run = text => vm.runInContext(text, ctx);
  return { ctx, run, writes, props, shift(value) { shift = value; } };
}

test('actual ConsoleView anchor survives mobile safe-area/keyboard change without a new scroll runtime or forced bottom', async () => {
  const h = environment(), a = anchorHarness();
  const originalWrite = h.root.style.setProperty;
  h.root.style.setProperty = (...args) => { originalWrite(...args); a.shift(args[1] === '430px' ? 40 : 0); };
  const stop = installMobileViewport({ window: h.win,
    beforeChange: () => a.run('captureMobileViewportAnchor()'),
    afterChange: snapshot => { a.ctx.snapshot = snapshot; void a.run('restoreMobileViewportAnchor(snapshot)'); },
  });
  await settle(); a.writes.length = 0;
  // One layout shift, regardless of event coalescing. Use the height write as a
  // deterministic stand-in for layout, not a duplicate of the anchor algorithm.
  h.root.style.setProperty = (...args) => { originalWrite(...args); if (args[0].endsWith('height')) a.shift(40); };
  h.viewport.height = 430; h.viewport.emit('resize'); h.flush(); await settle();
  assert.deepEqual(a.writes, [340]);
  a.run('autoScrollLocked.value = true'); a.writes.length = 0;
  h.standalone.emit('change'); h.flush(); await settle(); assert.deepEqual(a.writes, []);
  stop();
});

test('actual mobile anchor restoration ignores navigation, superseding load, relock and destruction', async () => {
  for (const change of ["props.conversationUuid = 'B'", 'loadRequestGeneration++', 'autoScrollLocked.value = true', 'componentMounted = false']) {
    const a = anchorHarness(); a.ctx.snapshot = a.run('captureMobileViewportAnchor()'); a.shift(80);
    const job = a.run('restoreMobileViewportAnchor(snapshot)'); a.run(change); await job;
    assert.deepEqual(a.writes, [], change);
  }
});
