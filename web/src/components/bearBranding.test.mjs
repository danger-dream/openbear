import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";
import { parse } from "@vue/compiler-sfc";
import { compile } from "@vue/compiler-dom";
import * as Vue from "vue";
import { renderToString } from "vue/server-renderer";

const webRoot = new URL("../../", import.meta.url);
const source = readFileSync(new URL("src/components/BearLogo.vue", webRoot), "utf8");
const { descriptor } = parse(source);
const {code} = compile(descriptor.template.content, {mode: 'function'});
const render = new Function('Vue', code)(Vue);
const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
function asset(path) {
  return readFileSync(new URL(`public${path}`, webRoot));
}
function assertPngSize(data, size) {
  assert.deepEqual(data.subarray(0, 8), pngSignature);
  assert.equal(data.readUInt32BE(16), size);
  assert.equal(data.readUInt32BE(20), size);
}
// Alpha of one pixel from an 8-bit RGBA, non-interlaced PNG.
function pngAlpha(data, x, y) {
  assert.equal(data[25], 6, "artwork keeps an alpha channel");
  const width = data.readUInt32BE(16), stride = width * 4, idat = [];
  for (let offset = 8; offset < data.length;) {
    const length = data.readUInt32BE(offset);
    if (data.toString("ascii", offset + 4, offset + 8) === "IDAT") idat.push(data.subarray(offset + 8, offset + 8 + length));
    offset += length + 12;
  }
  const raw = inflateSync(Buffer.concat(idat));
  let previous = Buffer.alloc(stride), row;
  for (let line = 0; line <= y; line += 1) {
    const filter = raw[line * (stride + 1)];
    row = Buffer.from(raw.subarray(line * (stride + 1) + 1, (line + 1) * (stride + 1)));
    for (let i = 0; i < stride; i += 1) {
      const left = i >= 4 ? row[i - 4] : 0, up = previous[i], upLeft = i >= 4 ? previous[i - 4] : 0;
      const p = left + up - upLeft, pa = Math.abs(p - left), pb = Math.abs(p - up), pc = Math.abs(p - upLeft);
      const predictor = [0, left, up, (left + up) >> 1, pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft][filter];
      row[i] = (row[i] + predictor) & 255;
    }
    previous = row;
  }
  return row[x * 4 + 3];
}

test("shared logo renders one transparent raster artwork for both themes with accessible name and uncropped sizing", async () => {
  const html = await renderToString(Vue.createSSRApp({render}));
  assert.match(html, /<img\b/);
  assert.match(html, /alt="OpenBear"/);
  assert.match(html, /draggable="false"/);
  const src = html.match(/src="([^"]+)"/)[1];
  assert.match(src, /^\/assets\/brand\/openbear-[a-f0-9]{12}\.png$/);
  assertPngSize(asset(src), 512);
  assert.equal(pngAlpha(asset(src), 0, 0), 0, "the artwork has no backdrop");
  assert.equal(pngAlpha(asset(src), 511, 0), 0);
  assert.equal(pngAlpha(asset(src), 256, 384), 255);
  assert.deepEqual(asset(src), asset("/icons/openbear-512.png"), "UI and installed-app icons use the same approved artwork");
  assert.doesNotMatch(source, /<script|theme/i, "a transparent logo needs no per-theme variant");
  assert.match(descriptor.styles[0].content, /border-radius:\s*22%/);
  assert.match(descriptor.styles[0].content, /object-fit:\s*contain/);
  assert.doesNotMatch(source, /<svg|🐻/);
});

test("PWA and Apple icons are transparent so the platform supplies the backdrop", () => {
  for (const [path, size] of [["/icons/openbear-192.png", 192], ["/icons/openbear-512.png", 512], ["/icons/apple-touch-icon.png", 180]]) {
    assertPngSize(asset(path), size);
    assert.equal(pngAlpha(asset(path), 0, 0), 0);
    assert.equal(pngAlpha(asset(path), size - 1, 0), 0);
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
    assert.equal(pngAlpha(ico.subarray(offset, offset + length), size / 2, Math.floor(size / 10)), 0, "no backdrop between the horns");
    sizes.push(size);
  }
  assert.deepEqual(sizes, [16, 32, 48, 64]);
});
