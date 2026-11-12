// Registers the .vue loader for the servers page tests, with xterm replaced by xtermFake.mjs and Leaflet by
// leafletFake.mjs.
// Import after '../../domTestEnv.mjs' and before '../../testing/mountApp.mjs'.
import 'vue';
import 'element-plus';
import {registerVueLoader} from '../../testing/vueLoader.mjs';

// happy-dom's IntersectionObserver never calls back: here every section observed is on screen at once, so the
// detail page's sections load as they would once scrolled to. The browser tests check the real scrolling.
class ShownObserver {
  constructor(callback) { this.callback = callback; }
  observe(target) { queueMicrotask(() => this.callback([{target, isIntersecting: true}], this)); }
  unobserve() {}
  disconnect() {}
  takeRecords() { return []; }
}
globalThis.IntersectionObserver = ShownObserver;
if (globalThis.window) globalThis.window.IntersectionObserver = ShownObserver;

const XTERM = new URL('./xtermFake.mjs', import.meta.url).href;
registerVueLoader({stubs: {'@xterm/xterm': XTERM, '@xterm/addon-fit': XTERM, '@xterm/addon-search': XTERM, '@xterm/addon-web-links': XTERM,
  leaflet: new URL('./leafletFake.mjs', import.meta.url).href}});
