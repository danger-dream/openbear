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
  win.document.activeElement = null;
  win.document.addEventListener = win.addEventListener; win.document.removeEventListener = win.removeEventListener;
  win.document.emit = win.emit;
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

// Logical-screen references, not fixed browser viewport heights. Browser chrome
// and the keyboard shrink the visible area below these dimensions on real phones.
const phoneScreens = [
  ['narrow stress case', 320, 568], ['narrow Android test size', 360, 800],
  ['iPhone SE 3', 375, 667], ['iPhone 13 mini', 375, 812],
  ['iPhone 13', 390, 844], ['iPhone 15', 393, 852],
  ['iPhone 16 Pro', 402, 874], ['wide Android test size', 412, 915],
  ['iPhone 14 Pro Max', 430, 932], ['iPhone 16 Pro Max', 440, 956],
];
for (const [label, width, height] of phoneScreens) {
  test(`viewport sizing matrix: ${label} (${width}×${height}), toolbar / keyboard / landscape`, () => {
    for (const visual of [true, false]) {
      const h = environment({visual});
      h.win.innerWidth = width; h.win.innerHeight = height - 96;
      if (h.viewport) Object.assign(h.viewport, {width, height: height - 96});
      const stop = installMobileViewport({window: h.win});
      assert.equal(h.value('height'), `${height - 96}px`);
      for (const next of [height, Math.max(180, height - 380), height - 96]) {
        h.win.innerHeight = next;
        if (h.viewport) {h.viewport.height = next; h.viewport.offsetTop = next < height - 96 ? 20 : 0; h.viewport.emit('resize');}
        h.win.emit('resize'); h.flush();
        assert.equal(h.value('height'), `${next}px`);
        assert.equal(h.value('top'), `${visual && next < height - 96 ? 20 : 0}px`);
      }
      // A wide landscape touch device stays on the mobile viewport path.
      h.win.innerWidth = height; h.win.innerHeight = width - 80;
      if (h.viewport) Object.assign(h.viewport, {width: height, height: width - 80, offsetTop: 0});
      h.win.emit('orientationchange'); h.flush();
      assert.equal(h.value('height'), `${width - 80}px`);
      stop(); assert.equal(h.attributes.size, 0); assert.equal(h.styles.size, 0);
    }
  });
}

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

test('software keyboard drops the bottom safe-area reservation only while an editor is focused and the visual viewport shrinks', () => {
  const h = environment(), attr = 'data-openbear-keyboard', editor = { tagName: 'TEXTAREA' };
  const stop = installMobileViewport({ window: h.win });
  assert.equal(h.attributes.has(attr), false);
  h.win.document.activeElement = editor; h.viewport.height = 430; h.viewport.emit('resize'); h.flush();
  assert.equal(h.attributes.has(attr), true, 'focused editor plus a large shrink is a keyboard');
  h.viewport.height = 800; h.win.document.activeElement = null; h.win.document.emit('focusout'); h.flush();
  assert.equal(h.attributes.has(attr), false, 'dismissal restores the reservation');
  h.win.document.activeElement = editor; h.viewport.height = 430; h.viewport.emit('resize'); h.flush();
  h.viewport.height = 800; h.viewport.emit('resize'); h.flush();
  assert.equal(h.attributes.has(attr), false, 'keyboard hide while focus remains also restores it');
  h.win.document.activeElement = { tagName: 'BUTTON' }; h.viewport.height = 430; h.viewport.emit('resize'); h.flush();
  assert.equal(h.attributes.has(attr), false, 'browser/toolbar-like shrink without a text editor is ignored');
  h.win.document.activeElement = { tagName: 'INPUT', type: 'text' }; h.viewport.height = 700; h.viewport.emit('resize'); h.flush();
  assert.equal(h.attributes.has(attr), false, 'small toolbar-sized changes are ignored');
  h.viewport.height = 430; h.viewport.emit('resize'); h.flush();
  assert.equal(h.attributes.has(attr), true);
  stop(); assert.equal(h.attributes.size, 0);
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
const actualBottom = source.slice(source.indexOf('async function scrollBottom('), source.indexOf('function cancelScheduledUiWork('));
const actualComposerHeight = source.slice(source.indexOf('function onComposerHeightChange('), source.indexOf('async function focusComposer('));
function anchorHarness() {
  const props = { conversationUuid: 'A' }, writes = [], frames = new Map(), media = {matches:true};
  let frameId = 0;
  let shift = 0, scrollTop = 300;
  const node = { dataset: { turnIndex: '2' }, getBoundingClientRect: () => ({ top: 80 + shift - (scrollTop - 300), bottom: 200 + shift - (scrollTop - 300) }) };
  const scroller = { get scrollTop() { return scrollTop; }, set scrollTop(value) { scrollTop = Math.min(Math.max(0,value),this.scrollHeight-this.clientHeight); writes.push(scrollTop); },
    scrollHeight: 2400, clientHeight: 500, getBoundingClientRect: () => ({ top: 100, bottom: 600 }), querySelectorAll: () => [node], querySelector: () => node };
  const ctx = vm.createContext({ props, ref, nextTick, captureTranscriptContentAnchor, transcriptContentAnchorDelta,
    scroller: ref(scroller), autoScrollLocked: ref(false), composerHeight:ref(135),
    window:{matchMedia:()=>media,requestAnimationFrame:fn=>{frames.set(++frameId,fn);return frameId;}},
    runProgrammaticScroll: fn => fn(), updateScrollerOverflow() {}, scheduleActiveTurnFromScroll() {} });
  vm.runInContext('let componentMounted = true, loadRequestGeneration = 1, scrollFrame = 0;\n' + actualAnchors + actualBottom + actualComposerHeight, ctx);
  const run = text => vm.runInContext(text, ctx);
  return { ctx, run, writes, props, media, frames,
    async flush() {const jobs=[...frames.values()];frames.clear();await Promise.all(jobs.map(fn=>fn()));},
    shift(value) { shift = value; } };
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
  a.writes.length = 0;
  h.standalone.emit('change'); h.flush(); await settle(); assert.equal(a.ctx.autoScrollLocked.value,false);
  stop();
});

test('keyboard opening and dismissal keep the latest message fully above the raised composer', async () => {
  const h=environment(),a=anchorHarness();
  a.run('autoScrollLocked.value=true;scroller.value.scrollTop=1900');
  const originalWrite=h.root.style.setProperty;
  h.root.style.setProperty=(key,value,...rest)=>{
    originalWrite(key,value,...rest);
    if(key.endsWith('height'))a.ctx.scroller.value.clientHeight=parseFloat(value)-300;
  };
  const stop=installMobileViewport({window:h.win,
    beforeChange:()=>a.run('captureMobileViewportAnchor()'),
    afterChange:snapshot=>{a.ctx.snapshot=snapshot;void a.run('restoreMobileViewportAnchor(snapshot)');},
  });
  await settle();a.writes.length=0;
  for(const height of [600,430,410,800]) {
    h.viewport.height=height;h.viewport.emit('resize');h.flush();await settle();
    const el=a.ctx.scroller.value;
    assert.equal(el.scrollTop+el.clientHeight,el.scrollHeight,`viewport ${height}: newest footer is not below the visible bottom`);
    assert.equal(a.ctx.autoScrollLocked.value,true);
  }
  assert.deepEqual(a.writes,[2100,2270,2290,1900]);stop();
});

test('late mobile composer resize follows once and stops following immediately on user unlock',async()=>{
  const a=anchorHarness();a.run('autoScrollLocked.value=true;scroller.value.clientHeight=130;scroller.value.scrollTop=2270');a.writes.length=0;
  a.run('scroller.value.clientHeight=100;onComposerHeightChange(165);onComposerHeightChange(165)');
  assert.equal(a.frames.size,1);await a.flush();assert.deepEqual(a.writes,[2300]);
  a.run('onComposerHeightChange(180);autoScrollLocked.value=false');await a.flush();assert.deepEqual(a.writes,[2300]);
  a.run('onComposerHeightChange(190)');assert.equal(a.frames.size,0,'history readers do not get scheduled tail jumps');
  a.run('autoScrollLocked.value=true');a.media.matches=false;a.run('onComposerHeightChange(200)');assert.equal(a.frames.size,0,'desktop behavior is unchanged');
  a.media.matches=true;a.run('componentMounted=false;onComposerHeightChange(210)');assert.equal(a.frames.size,0);
  a.run('onComposerHeightChange(NaN);onComposerHeightChange(0)');assert.equal(a.ctx.composerHeight.value,210);
});

test('pending keyboard tail restoration cannot cross navigation, a newer load, user unlock or unmount',async()=>{
  for(const change of ["props.conversationUuid='B'",'loadRequestGeneration++','autoScrollLocked.value=false','componentMounted=false']) {
    const a=anchorHarness();a.run('autoScrollLocked.value=true');a.ctx.snapshot=a.run('captureMobileViewportAnchor()');
    const job=a.run('restoreMobileViewportAnchor(snapshot)');a.run(change);await job;
    assert.deepEqual(a.writes,[],change);
  }
});

test('actual mobile anchor restoration ignores navigation, superseding load, relock and destruction', async () => {
  for (const change of ["props.conversationUuid = 'B'", 'loadRequestGeneration++', 'autoScrollLocked.value = true', 'componentMounted = false']) {
    const a = anchorHarness(); a.ctx.snapshot = a.run('captureMobileViewportAnchor()'); a.shift(80);
    const job = a.run('restoreMobileViewportAnchor(snapshot)'); a.run(change); await job;
    assert.deepEqual(a.writes, [], change);
  }
});
