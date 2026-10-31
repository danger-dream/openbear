// Registers the .vue loader for the servers page tests, with xterm replaced by xtermFake.mjs.
// Import after '../../domTestEnv.mjs' and before '../../testing/mountApp.mjs'.
import 'vue';
import 'element-plus';
import {registerVueLoader} from '../../testing/vueLoader.mjs';

const XTERM = new URL('./xtermFake.mjs', import.meta.url).href;
registerVueLoader({stubs: {'@xterm/xterm': XTERM, '@xterm/addon-fit': XTERM, '@xterm/addon-search': XTERM, '@xterm/addon-web-links': XTERM}});
