import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h, ref, nextTick} from 'vue';
import 'element-plus';
import {registerVueLoader} from '../testing/vueLoader.mjs';
registerVueLoader({stubs: {
  'ironrdp-wasm': new URL('./testing/desktopOpsRdp.mjs', import.meta.url).href,
  '@novnc/novnc': new URL('./testing/desktopOpsVnc.mjs', import.meta.url).href,
}});
const {mountApp} = await import('../testing/mountApp.mjs');
const {waitUntil, settle, sleep} = await import('../testing/domPage.mjs');
const {Api} = await import('../api.js');
const DesktopPane = (await import('./DesktopPane.vue')).default;
const rdp = await import('./testing/desktopOpsRdp.mjs');
const vnc = await import('./testing/desktopOpsVnc.mjs');

async function desktop(t, platform = 'windows', phone = false) {
  t.mock.method(Api, 'serverDesktopConfig', async () => ({ok: true, password: 'fixture', username: 'fixture'}));
  const pane = ref(), active = ref(true);
  const mounted = mountApp(t, {render: () => h(DesktopPane, {ref: pane, serverId: 'fixture', platform, phone, active: active.value})});
  await waitUntil(() => document.querySelector('.dp-stage.is-open'));
  await settle();
  await sleep(30); // let initial focus/scale frames complete
  return {...mounted, pane, active, stage: document.querySelector('.dp-stage'), canvas: document.querySelector('canvas')};
}
function pointer(target, type, options = {}) {
  target.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerType: 'mouse', pointerId: 7, button: 0, clientX: 20, clientY: 20, ...options}));
}
function touch(target, type, points = []) {
  const event = new Event(type, {bubbles: true, cancelable: true});
  Object.defineProperty(event, 'touches', {value: points.map(([clientX, clientY]) => ({clientX, clientY}))});
  target.dispatchEvent(event);
}

for (const boundary of ['blur', 'inactive', 'clipboard', 'settings', 'pointercancel', 'lostpointercapture', 'windowblur']) {
  test(`RDP ${boundary} releases inputs and cancels pending frame`, async t => {
    const d = await desktop(t);
    const session = rdp.sessions.at(-1), frames = new Map();
    let sequence = 0;
    t.mock.method(globalThis, 'requestAnimationFrame', fn => { frames.set(++sequence, fn); return sequence; });
    t.mock.method(globalThis, 'cancelAnimationFrame', id => frames.delete(id));
    d.canvas.setPointerCapture = () => {};
    d.canvas.releasePointerCapture = () => {};
    d.canvas.dispatchEvent(new KeyboardEvent('keydown', {code: 'ControlLeft', bubbles: true, cancelable: true}));
    pointer(d.canvas, 'pointerdown');
    pointer(d.canvas, 'pointermove', {clientX: 50});
    d.canvas.dispatchEvent(new WheelEvent('wheel', {deltaY: 20, cancelable: true}));
    assert.ok(session.events.some(e => e.type === 'keyPressed'));
    assert.ok(session.events.some(e => e.type === 'mouseButtonPressed'));
    assert.ok(frames.size);
    const before = session.releases;
    if (boundary === 'inactive') { d.active.value = false; await nextTick(); }
    else if (boundary === 'clipboard') d.pane.value.openClipboard();
    else if (boundary === 'settings') d.pane.value.openSettings();
    else if (boundary === 'windowblur') window.dispatchEvent(new Event('blur'));
    else d.canvas.dispatchEvent(new Event(boundary));
    assert.ok(session.releases > before);
    assert.equal(frames.size, 0, 'queued move/wheel cannot replay after release');
    await d.unmount();
  });
}

test('RDP pointer released outside the canvas and touch pointers never double-send mouse input', async t => {
  const d = await desktop(t);
  const session = rdp.sessions.at(-1);
  let captured, released;
  d.canvas.setPointerCapture = id => { captured = id; };
  d.canvas.releasePointerCapture = id => { released = id; };
  pointer(d.canvas, 'pointerdown');
  pointer(window, 'pointerup', {clientX: 2000});
  assert.equal(captured, 7);
  assert.equal(released, 7);
  assert.equal(session.events.filter(e => e.type === 'mouseButtonPressed').length, 1);
  assert.equal(session.events.filter(e => e.type === 'mouseButtonReleased').length, 1);
  pointer(d.canvas, 'pointerdown', {pointerType: 'touch'});
  assert.equal(session.events.filter(e => e.type === 'mouseButtonPressed').length, 1);
  await d.unmount();
});

for (const platform of ['darwin', 'windows']) {
  test(`${platform} cancel is cleanup only; staggered two-finger end right-clicks once`, async t => {
    const d = await desktop(t, platform, true);
    const events = platform === 'darwin' ? vnc.sessions.at(-1)._sock.events : rdp.sessions.at(-1).events;
    const clicks = button => events.filter(e => platform === 'darwin' ? e.mask === (button === 2 ? 4 : 1) : e.type === 'mouseButtonPressed' && e.args[0] === button).length;
    touch(d.stage, 'touchstart', [[10, 10]]);
    touch(d.stage, 'touchcancel');
    await sleep(520);
    assert.equal(clicks(0), 0);
    assert.equal(clicks(2), 0, 'cancel must clear the long-press timer too');
    touch(d.stage, 'touchstart', [[10, 10]]);
    touch(d.stage, 'touchstart', [[10, 10], [20, 20]]);
    touch(d.stage, 'touchend', [[20, 20]]);
    touch(d.stage, 'touchend');
    assert.equal(clicks(2), 1);
    assert.equal(clicks(0), 0);
    // A fresh gesture remains usable after both fingers lift.
    touch(d.stage, 'touchstart', [[10, 10]]);
    touch(d.stage, 'touchend');
    assert.equal(clicks(0), 1);
    await d.unmount();
  });
}

test('VNC direct dragging holds the same real button mask through moves and wheel pulses', async t => {
  const d = await desktop(t, 'darwin', true);
  d.pane.value.toggleTouchMode(); // phone defaults to trackpad
  const events = vnc.sessions.at(-1)._sock.events;
  touch(d.stage, 'touchstart', [[10, 10]]);
  touch(d.stage, 'touchmove', [[30, 30]]);
  touch(d.stage, 'touchmove', [[50, 50]]);
  assert.deepEqual(events.map(e => e.mask), [1, 1, 1]);
  d.pane.value.triggerRightClick();
  assert.deepEqual(events.slice(-2).map(e => e.mask), [5, 1], 'right click cannot release held left button');
  touch(d.stage, 'touchend');
  assert.equal(events.at(-1).mask, 0);
  // A double-tap drag is also released by cancel, without an extra click.
  d.pane.value.toggleTouchMode();
  touch(d.stage, 'touchstart', [[10, 10]]);
  touch(d.stage, 'touchend');
  touch(d.stage, 'touchstart', [[10, 10]]);
  assert.equal(events.at(-1).mask, 1);
  touch(d.stage, 'touchcancel');
  assert.equal(events.at(-1).mask, 0);
  touch(d.stage, 'touchstart', [[10, 10], [20, 20]]);
  touch(d.stage, 'touchmove', [[10, 40], [20, 50]]);
  assert.deepEqual(events.slice(-2).map(e => e.mask), [8, 0]);
  touch(d.stage, 'touchend');
  await d.unmount();
});
