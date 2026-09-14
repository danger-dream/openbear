import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse } from "@vue/compiler-sfc";
import { compile } from "@vue/compiler-dom";
import * as Vue from "vue";
import { renderToString } from "vue/server-renderer";

const webRoot = new URL("../../", import.meta.url);
const source = readFileSync(new URL("src/components/BearLogo.vue", webRoot), "utf8");
const { descriptor } = parse(source);
const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
function asset(path) {
  return readFileSync(new URL(`public${path}`, webRoot));
}
function assertPngSize(data, size) {
  assert.deepEqual(data.subarray(0, 8), pngSignature);
  assert.equal(data.readUInt32BE(16), size);
  assert.equal(data.readUInt32BE(20), size);
}

test("shared logo renders the approved raster artwork with accessible name and uncropped sizing", async () => {
  const { code } = compile(descriptor.template.content, { mode: "function" });
  const render = new Function("Vue", code)(Vue);
  const html = await renderToString(Vue.createSSRApp({ render }));
  assert.match(html, /<img\b/);
  assert.match(html, /alt="OpenBear"/);
  assert.match(html, /draggable="false"/);
  const src = html.match(/src="([^"]+)"/)[1];
  assert.match(src, /^\/assets\/brand\/openbear-[a-f0-9]{12}\.png$/);
  assertPngSize(asset(src), 512);
  assert.deepEqual(asset(src), asset("/icons/openbear-512.png"), "web and installed app use identical artwork");
  assert.match(descriptor.styles[0].content, /object-fit:\s*contain/);
  assert.doesNotMatch(source, /<svg|🐻/);
});

test("PWA and Apple icons retain their sizes and existing application identity", () => {
  for (const [path, size] of [["/icons/openbear-192.png", 192], ["/icons/openbear-512.png", 512], ["/icons/apple-touch-icon.png", 180]]) {
    assertPngSize(asset(path), size);
  }
  const manifest = JSON.parse(asset("/manifest.webmanifest"));
  assert.deepEqual([manifest.id, manifest.start_url, manifest.scope], ["./", "./", "./"]);
  assert.deepEqual(manifest.icons.map((icon) => icon.sizes).sort(), ["192x192", "512x512"]);
});

test("favicon links use versioned assets and provide real small-size ICO frames", () => {
  const html = readFileSync(new URL("index.html", webRoot), "utf8");
  const paths = [...html.matchAll(/<link\b[^>]*rel="icon"[^>]*href="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(paths.length, 2);
  const icoPath = paths.find((path) => path.endsWith(".ico"));
  const pngPath = paths.find((path) => path.endsWith(".png"));
  assert.match(icoPath, /^\/assets\/brand\/favicon-[a-f0-9]{12}\.ico$/);
  assertPngSize(asset(pngPath), 32);
  const ico = asset(icoPath);
  assert.equal(ico.readUInt16LE(0), 0);
  assert.equal(ico.readUInt16LE(2), 1);
  const count = ico.readUInt16LE(4);
  const sizes = [];
  for (let index = 0; index < count; index += 1) {
    const entry = 6 + index * 16;
    const size = ico[entry] || 256;
    assert.equal(ico[entry + 1] || 256, size);
    const length = ico.readUInt32LE(entry + 8);
    const offset = ico.readUInt32LE(entry + 12);
    assert.ok(offset >= 6 + count * 16 && offset + length <= ico.length);
    assertPngSize(ico.subarray(offset, offset + length), size);
    sizes.push(size);
  }
  assert.deepEqual(sizes, [16, 32, 48, 64]);
});
