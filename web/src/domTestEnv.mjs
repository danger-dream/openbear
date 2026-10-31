// A happy-dom window installed as the globals Vue, Element Plus and @vue/test-utils expect,
// for component tests that need a DOM but no real browser (no layout: sizes are all 0).
// Import this before vue, element-plus or @vue/test-utils: Vue reads `document` when it loads.
import {Window} from 'happy-dom';

export const window = new Window({url: 'http://localhost/', width: 1280, height: 800});

const NAMES = [
  'document', 'navigator', 'location', 'history', 'localStorage', 'sessionStorage',
  'Node', 'Element', 'HTMLElement', 'SVGElement', 'Text', 'Comment', 'DocumentFragment', 'HTMLInputElement',
  'HTMLTextAreaElement', 'HTMLSelectElement', 'HTMLButtonElement', 'HTMLAnchorElement', 'ShadowRoot',
  'Event', 'CustomEvent', 'MouseEvent', 'PointerEvent', 'KeyboardEvent', 'InputEvent', 'FocusEvent',
  'WheelEvent', 'TouchEvent', 'DragEvent', 'ClipboardEvent', 'ErrorEvent',
  'MutationObserver', 'ResizeObserver', 'IntersectionObserver', 'DOMParser', 'XMLSerializer',
  'getComputedStyle', 'matchMedia', 'requestAnimationFrame', 'cancelAnimationFrame', 'scrollTo',
  'CSS', 'CSSStyleSheet', 'Range', 'Selection', 'NodeFilter', 'DOMRect', 'FileReader', 'Blob', 'File',
  'Document',
  'innerWidth', 'innerHeight', 'visualViewport', 'devicePixelRatio',
];
Object.defineProperty(globalThis, 'window', {value: window, configurable: true, writable: true});
for (const name of NAMES) {
  if (window[name] === undefined) continue;
  const value = typeof window[name] === 'function' && !/^[A-Z]/.test(name) ? window[name].bind(window) : window[name];
  Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
}

export function resize(width, height) {
  window.happyDOM.setViewport({width, height});
}
