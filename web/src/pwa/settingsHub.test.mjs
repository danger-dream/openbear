import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import * as Vue from "vue";
import { parse, compileTemplate } from "@vue/compiler-sfc";

// Execute the real SettingsHub setup and shared lazy helper. Only the dynamic
// module transport is replaced: Node cannot import a .vue page as the browser can.
test("SettingsHub keeps section order/default/events and loads only the selected page", async () => {
  const descriptor = parse(readFileSync(new URL("../views/SettingsHubView.vue", import.meta.url), "utf8")).descriptor;
  const helper = readFileSync(new URL("../lazyView.js", import.meta.url), "utf8");
  const props = Vue.reactive({ section: "channels" });
  const loads = [], emitted = [];
  const context = vm.createContext({
    ...Vue,
    onBeforeUnmount() {},
    LazyViewState: { name: "loading-state" },
    defineProps: () => props,
    defineEmits: () => (...args) => emitted.push(args),
    loadView: async (path) => { loads.push(path); return { default: { name: path } }; },
  });
  vm.runInContext(helper.replace(/^import .*;\n/gm, "").replace("export function ", "function "), context);
  const scope = Vue.effectScope();
  scope.run(() => vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm, "").replace(/\bimport\(/g, "loadView("), context));
  const run = (code) => vm.runInContext(code, context);
  const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await Vue.nextTick(); };
  try {
    assert.equal(loads.length, 0, "definitions alone must not request modules");
    assert.deepEqual(Array.from(run("sections.map(item => item.key)")), ["channels", "templates", "agents", "system-settings", "logs", "install-app"]);
    const first = run("activeComponent.value");
    const view = first.setup({}, { attrs: {}, slots: {} });
    await flush();
    assert.deepEqual(loads, ["./ChannelsView.vue"]);
    assert.equal(view().type.name, "./ChannelsView.vue");
    run("selectSection('install-app')");
    await Vue.nextTick();
    assert.deepEqual(emitted.at(-1), ["section-changed", "install-app"]);
    const install = run("activeComponent.value");
    assert.notEqual(install, first);
    install.setup({}, { attrs: {}, slots: {} });
    await flush();
    assert.deepEqual(loads, ["./ChannelsView.vue", "./InstallAppView.vue"]);
    run("selectSection('channels')");
    assert.equal(run("activeComponent.value"), first, "component identity is stable");
    first.setup({}, { attrs: {}, slots: {} });
    await flush();
    assert.equal(loads.length, 2, "successful module stays cached");
    props.section = "logs";
    await Vue.nextTick();
    assert.equal(run("activeSection.value"), "logs");
    props.section = "unknown";
    await Vue.nextTick();
    assert.equal(run("activeSection.value"), "channels");
    run("selectSection('unknown')");
    assert.equal(run("activeSection.value"), "channels");
    const template = compileTemplate({ source: descriptor.template.content, filename: "SettingsHubView.vue", id: "pwa-settings-test" });
    assert.deepEqual(template.errors, []);
  } finally { scope.stop(); }
});
