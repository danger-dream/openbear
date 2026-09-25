import test from "node:test";
import assert from "node:assert/strict";
import {scrollModelListAbove} from "./modelDragAutoScroll.js";

test("desktop drag held at viewport top continues scrolling the model list to the first row, then cleans up", t => {
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval, originalClearInterval = globalThis.clearInterval;
  let dragover, interval, removed = false, cleared = false;
  globalThis.window = {matchMedia: () => ({matches:true})};
  globalThis.document = {
    addEventListener(type, fn, capture) {assert.equal(type, "dragover"); assert.equal(capture, true); dragover = fn;},
    removeEventListener(type, fn, capture) {assert.equal(type, "dragover"); assert.equal(fn, dragover); assert.equal(capture, true); removed = true;},
  };
  globalThis.setInterval = (fn, delay) => {assert.equal(delay, 24); interval = fn; return 1;};
  globalThis.clearInterval = id => {assert.equal(id, 1); cleared = true;};
  t.after(() => {
    globalThis.window = originalWindow; globalThis.document = originalDocument;
    globalThis.setInterval = originalSetInterval; globalThis.clearInterval = originalClearInterval;
  });
  const list = {scrollTop: 500, getBoundingClientRect: () => ({top:350, left:200, right:900})};
  const stop = scrollModelListAbove(list);
  dragover({clientX:400, clientY:1}); // viewport top is far above the 80px Sortable edge zone
  for (let i = 0; i < 30; i++) interval();
  assert.equal(list.scrollTop, 0, "held drag reaches the first item without another mouse move");
  list.scrollTop = 500;
  dragover({clientX:400, clientY:500}); // back within the list: Sortable owns scrolling
  interval();
  assert.equal(list.scrollTop, 500);
  dragover({clientX:100, clientY:1}); // not in the model list column
  interval();
  assert.equal(list.scrollTop, 500);
  stop();
  assert.ok(removed && cleared);
});

test("mobile sorting is unchanged by the desktop-only viewport-top adapter", t => {
  const originalWindow = globalThis.window;
  globalThis.window = {matchMedia: () => ({matches:false})};
  t.after(() => {globalThis.window = originalWindow;});
  assert.doesNotThrow(() => scrollModelListAbove({})());
});
