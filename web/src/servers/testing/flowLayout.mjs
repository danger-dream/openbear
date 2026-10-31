// Import after '../../domTestEnv.mjs', before the page mounts.
import {window} from '../../domTestEnv.mjs';

// Vue Flow shows a node only once it has measured it (offsetWidth / offsetHeight through a
// ResizeObserver) and draws an edge only between measured handles. happy-dom has no layout, so
// the board's container, nodes and handles get fixed sizes, and a ResizeObserver reports each
// observed element once, as a browser does. Nothing else on the page gets a size.
const FLOW_SIZES = [['vue-flow__handle', 8, 8], ['vue-flow__node', 180, 64], ['vue-flow__viewport', 1000, 700], ['vue-flow', 1000, 700]];
const sizeOf = el => FLOW_SIZES.find(([name]) => el.classList?.contains(name));
for (const [prop, i] of [['offsetWidth', 1], ['offsetHeight', 2]]) {
  const base = Object.getOwnPropertyDescriptor(window.HTMLElement.prototype, prop);
  Object.defineProperty(window.HTMLElement.prototype, prop, {configurable: true, get() { const size = sizeOf(this); return size ? size[i] : base.get.call(this); }});
}
const baseRect = window.Element.prototype.getBoundingClientRect;
window.Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
  const size = sizeOf(this);
  if (!size) return baseRect.call(this);
  return {x: 0, y: 0, left: 0, top: 0, width: size[1], height: size[2], right: size[1], bottom: size[2], toJSON() { return this; }};
};
class ResizeObserver {
  constructor(callback) { this._callback = callback; this._targets = new Set(); }
  observe(target) {
    this._targets.add(target);
    queueMicrotask(() => { if (this._targets.has(target)) this._callback([{target, contentRect: target.getBoundingClientRect()}], this); });
  }
  unobserve(target) { this._targets.delete(target); }
  disconnect() { this._targets.clear(); }
}
window.ResizeObserver = globalThis.ResizeObserver = ResizeObserver;
