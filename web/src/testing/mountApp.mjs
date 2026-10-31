// Mounts a real page component with Element Plus into the happy-dom document, for the
// *.dom.test.mjs files. Import '../domTestEnv.mjs' and './fakeNetwork.mjs' first.
import assert from 'node:assert/strict';
import {window, resize} from '../domTestEnv.mjs';
import {createApp} from 'vue';
import ElementPlus from 'element-plus';
import * as Icons from '@element-plus/icons-vue';
import {registerVueLoader} from './vueLoader.mjs';
import {DomPage, settle} from './domPage.mjs';
import {addStyles, viewportChanged} from './cssIndex.mjs';
import {readFileSync} from 'node:fs';

// The big CommonJS packages load before the hooks are registered: after that every require()
// inside them makes a round trip to the hooks thread (element-plus alone goes from ~1 s to ~6 s).
registerVueLoader();

// happy-dom has no canvas; the flag-emoji check and text measuring only need something that answers.
const context2d = new Proxy({}, {get: (target, key) => key === 'measureText' ? () => ({width: 0, actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0})
  : key === 'getImageData' ? (x, y, w = 1, h = 1) => ({data: new Uint8ClampedArray(Math.max(1, w * h) * 4)}) : key === 'canvas' ? null : key in target ? target[key] : () => {},
set: (target, key, value) => { target[key] = value; return true; }});
window.HTMLCanvasElement.prototype.getContext = function getContext() { return context2d; };
window.HTMLCanvasElement.prototype.toDataURL = () => 'data:,';
if (!window.HTMLElement.prototype.scrollIntoView) window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
globalThis.HTMLCanvasElement = window.HTMLCanvasElement;
// happy-dom checks a number's step with a float remainder (37.79 % 0.01 !== 0) and so blocks forms
// a browser submits. Check it as the HTML spec does: (value - base) / step is a whole number.
const Validity = window.document.createElement('input').validity.constructor;
Object.defineProperty(Validity.prototype, 'stepMismatch', {configurable: true, get() {
  const el = this.element ?? Object.values(this).find(v => v?.tagName === 'INPUT');
  if (!el || el.tagName !== 'INPUT' || !['number', 'range'].includes(el.type) || el.value === '') return false;
  const step = el.hasAttribute('step') ? el.getAttribute('step') : '1';
  if (step === 'any') return false;
  const base = el.hasAttribute('min') ? Number(el.getAttribute('min')) : 0, n = (Number(el.value) - base) / Number(step);
  return Math.abs(n - Math.round(n)) > 1e-7;
}});

// Popper (Element Plus tooltips, selects, menus) calls node.toString() to tell a window from an
// element; happy-dom's Element#toString returns outerHTML, serialising the whole subtree on every
// position update (seconds per page). A browser gives "[object HTMLDivElement]".
window.Element.prototype.toString = function toString() { return `[object ${this.constructor?.name || 'Element'}]`; };

// happy-dom (21.0.4) answers a media query list "a, b" with a AND b; a browser with a OR b
// (MOBILE_VIEWPORT_QUERY is such a list). Each part is asked separately and the answers ORed.
const baseMatchMedia = window.matchMedia.bind(window);
function splitList(query) {
  const parts = []; let depth = 0, start = 0;
  for (let i = 0; i < query.length; i++) {
    if (query[i] === '(') depth++; else if (query[i] === ')') depth--;
    else if (query[i] === ',' && depth === 0) { parts.push(query.slice(start, i).trim()); start = i + 1; }
  }
  parts.push(query.slice(start).trim());
  return parts.filter(Boolean);
}
// A touch device (mountApp's `touch`, like Playwright's hasTouch + isMobile) answers the
// hover / pointer features as a phone does: no hover, a coarse pointer.
let touchDevice = false;
const TOUCH_FEATURES = {'hover': 'none', 'any-hover': 'none', 'pointer': 'coarse', 'any-pointer': 'coarse'};
const asTouch = query => query.replace(/\(\s*(any-hover|hover|any-pointer|pointer)\s*(?::\s*([a-z-]+)\s*)?\)/g, (m, feature, value) =>
  (value ? TOUCH_FEATURES[feature] === value : TOUCH_FEATURES[feature] !== 'none') ? '(min-width: 0px)' : '(min-width: 100000px)');
window.matchMedia = function matchMedia(query) {
  if (touchDevice) query = asTouch(String(query));
  const parts = splitList(String(query));
  if (parts.length < 2) return baseMatchMedia(query);
  const lists = parts.map(baseMatchMedia), listeners = new Map();
  const any = () => lists.some(l => l.matches);
  const mql = {
    media: String(query), onchange: null,
    get matches() { return any(); },
    addEventListener(type, fn) {
      if (type !== 'change' || listeners.has(fn)) return;
      let last = any();
      const relay = () => { const now = any(); if (now !== last) { last = now; fn.call(mql, {matches: now, media: mql.media}); } };
      listeners.set(fn, relay);
      for (const l of lists) l.addEventListener('change', relay);
    },
    removeEventListener(type, fn) { const relay = listeners.get(fn); if (!relay) return; listeners.delete(fn); for (const l of lists) l.removeEventListener('change', relay); },
    addListener(fn) { mql.addEventListener('change', fn); },
    removeListener(fn) { mql.removeEventListener('change', fn); },
    dispatchEvent() { return true; },
  };
  return mql;
};
globalThis.matchMedia = window.matchMedia;

// Element Plus's own display rules, so its components break lines and hide like in a browser.
addStyles(readFileSync(new URL('../../node_modules/element-plus/dist/index.css', import.meta.url), 'utf8'), 'element-plus/dist/index.css');

export {window, resize, settle};

// Mount `root` (a component, or a function of {h} returning one) on a fresh #app.
// touch: answer media queries as a phone's touch screen does (hover: none, pointer: coarse).
// Returns {page, app, errors, unmount}; unmount() also checks that nothing threw.
export function mountApp(t, root, {width = 1440, height = 900, dark = false, network = null, touch = false} = {}) {
  touchDevice = touch;
  resize(width, height);
  viewportChanged();
  // domTestEnv copies innerWidth / innerHeight once; keep the copies in step with the viewport.
  for (const name of ['innerWidth', 'innerHeight']) Object.defineProperty(globalThis, name, {value: window[name], configurable: true, writable: true});
  window.document.documentElement.classList.toggle('dark', dark);
  window.document.body.innerHTML = '<div id="app"></div>';
  const errors = [];
  const app = createApp(root);
  app.use(ElementPlus);
  for (const [key, icon] of Object.entries(Icons)) if (key !== 'default') app.component(key, icon);
  app.config.errorHandler = error => { errors.push(error?.stack || String(error)); };
  app.config.warnHandler = () => {};
  const onError = event => errors.push(event?.error?.stack || event?.message || String(event));
  window.addEventListener('error', onError);
  const onRejection = reason => errors.push(`unhandled rejection: ${reason?.stack || reason}`);
  process.on('unhandledRejection', onRejection);
  app.mount('#app');
  let mounted = true;
  const unmount = async ({check = true} = {}) => {
    if (!mounted) return;
    mounted = false;
    await settle();
    app.unmount();
    window.removeEventListener('error', onError);
    process.off('unhandledRejection', onRejection);
    window.document.body.innerHTML = '';
    if (check) assert.deepEqual(errors, []);
  };
  t.after(() => unmount({check: false}));
  return {page: new DomPage(network), app, errors, unmount};
}
