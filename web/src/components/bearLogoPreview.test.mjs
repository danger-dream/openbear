import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { compileScript, parse } from "@vue/compiler-sfc";
import { compile } from "@vue/compiler-dom";
import * as Vue from "vue";

const root = new URL("../../", import.meta.url);
const source = readFileSync(new URL("src/components/BearLogoPreview.vue", root), "utf8");
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "bear-logo-preview-test" });
const setupCode = script.content.replace(/^import .*;\n/gm, "").replace("export default", "return");
const { code } = compile(descriptor.template.content, {
  mode: "function", prefixIdentifiers: true, bindingMetadata: script.bindings,
});
const render = new Function("Vue", code)(Vue);
const Viewer = { name: "ElImageViewer" };
const Logo = { name: "BearLogo" };
function component() {
  const sfc = new Function("nextTick", "ref", "ElImageViewer", "BearLogo", setupCode)(Vue.nextTick, Vue.ref, Viewer, Logo);
  const state = sfc.setup({}, { expose() {} });
  return { state, tree: () => render({}, [], {}, Vue.proxyRefs(state)) };
}
function find(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const child of Array.isArray(node.children) ? node.children.flat(Infinity) : []) {
    const found = find(child, predicate);
    if (found) return found;
  }
  return null;
}

test("native button opens the original only on activation, with viewport-safe modal options", async () => {
  const view = component();
  assert.equal(view.state.previewOpen.value, false);
  assert.equal(find(view.tree(), (node) => node.type === Viewer), null, "no viewer or original image request before activation");
  const button = find(view.tree(), (node) => node.type === "button");
  assert.equal(button.props.type, "button");
  assert.equal(button.props["aria-label"], "查看 OpenBear 大图");
  assert.equal(button.props["aria-haspopup"], "dialog");
  assert.equal(button.props["aria-expanded"], false);
  let stopped = false;
  button.props.onClick({ stopPropagation() { stopped = true; } });
  assert.equal(stopped, true, "opening the image must not activate surrounding navigation");
  assert.equal(view.state.previewOpen.value, true);
  const viewer = find(view.tree(), (node) => node.type === Viewer);
  assert.ok(viewer);
  assert.deepEqual(viewer.props["url-list"], ["/assets/brand/openbear-original-d32cdfb09c17.png"]);
  assert.equal(viewer.props["close-on-press-escape"], true);
  assert.ok(Object.hasOwn(viewer.props, "hide-on-click-modal"));
  assert.ok(Object.hasOwn(viewer.props, "teleported"), "escape transformed or clipped mobile sidebar ancestors");
  assert.equal(viewer.props.infinite, false);
  assert.equal(find(view.tree(), (node) => node.type === "button").props["aria-expanded"], true);

  let focusOptions;
  view.state.trigger.value = { focus(options) { focusOptions = options; } };
  await viewer.props.onClose();
  assert.equal(view.state.previewOpen.value, false);
  assert.equal(find(view.tree(), (node) => node.type === Viewer), null);
  assert.deepEqual(focusOptions, { preventScroll: true });
  // A disappearing mobile trigger must not make close fail.
  view.state.trigger.value = null;
  await view.state.closePreview();
});

test("preview uses the exact approved full-resolution artwork, not the reduced icon", () => {
  const image = readFileSync(new URL("public/assets/brand/openbear-original-d32cdfb09c17.png", root));
  assert.equal(image.readUInt32BE(16), 1254);
  assert.equal(image.readUInt32BE(20), 1254);
  assert.equal(createHash("sha256").update(image).digest("hex"), "d32cdfb09c17a0e9ab6ff4a806df0b53c690fa4a4dfc27ebe097445a37811c56");
});

test("only desktop/sidebar and mobile top-left brand entries opt into the preview", () => {
  const app = readFileSync(new URL("src/App.vue", root), "utf8");
  assert.equal((app.match(/<BearLogoPreview\s*\/>/g) || []).length, 2);
  assert.match(app, /mobile-brand-logo"><BearLogoPreview\s*\/>/);
  assert.match(app, /sidebar-brand-logo[^>]*>\s*<BearLogoPreview\s*\/>/);
  for (const path of ["src/views/LoginView.vue", "src/views/InstallAppView.vue"]) {
    assert.doesNotMatch(readFileSync(new URL(path, root), "utf8"), /BearLogoPreview/);
  }
  assert.match(descriptor.styles[0].content, /:focus-visible/);
  assert.match(descriptor.styles[0].content, /touch-action:\s*manipulation/);
});
