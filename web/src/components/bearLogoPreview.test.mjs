import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { compileScript, parse } from "@vue/compiler-sfc";
import { compile } from "@vue/compiler-dom";
import * as Vue from "vue";
import { assetUrl } from "../assetBase.js";

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
  const sfc = new Function("nextTick", "ref", "ElImageViewer", "BearLogo", "assetUrl", setupCode)(Vue.nextTick, Vue.ref, Viewer, Logo, assetUrl);
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
  assert.deepEqual(viewer.props["url-list"], ["/assets/brand/openbear-original-2eaaca574a27.png"]);
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
  const image = readFileSync(new URL("public/assets/brand/openbear-original-2eaaca574a27.png", root));
  assert.equal(image.readUInt32BE(16), 1024);
  assert.equal(image.readUInt32BE(20), 1024);
  assert.equal(image[25], 6, "the full-resolution artwork is transparent too");
  assert.equal(createHash("sha256").update(image).digest("hex"), "2eaaca574a272148538fda5f24815e58d44cac85ae7ebe27058f0f1c03f6dcf7");
});
