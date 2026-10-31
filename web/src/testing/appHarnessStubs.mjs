// Registers testing/vueLoader.mjs with the stubs the appHarness pages need, before
// testing/mountApp.mjs registers it without any (the first registration wins).
import 'vue';
import 'element-plus';
import {registerVueLoader} from './vueLoader.mjs';
registerVueLoader({stubs: {'monaco-editor': new URL('./monacoStub.mjs', import.meta.url).href}});
