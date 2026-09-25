import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';

// Execute the real presentation composable, substituting only media and UI IO.
export function mobileAssetsRuntime({ phone = false, onBeforeUnmount = () => {}, notices = [] } = {}) {
  const isPhone = Vue.ref(phone);
  const source = fs.readFileSync(new URL('../components/useMobileAssets.js', import.meta.url), 'utf8');
  const context = vm.createContext({ ...Vue, useAdminPhone: () => isPhone, onBeforeUnmount,
    ElMessage: { error: text => notices.push(text) }, apiError: error => error?.message || String(error) });
  vm.runInContext(source.replace(/^import .*;\n/gm, '').replace('export function useMobileAssets', 'function useMobileAssets'), context);
  return { useMobileAssets: context.useMobileAssets, isPhone };
}
