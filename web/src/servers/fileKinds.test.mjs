import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { imageType, imageUrl, kind, readLimited } from './fileKinds.js';

test('an SVG previews as an image from a data: URL, never from a blob: URL of this origin', () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script><text>中文</text></svg>';
  const buffer = new TextEncoder().encode(svg).buffer;
  assert.equal(kind({type: 'file', name: 'logo.SVG'}), 'image');
  const url = imageUrl(buffer, imageType('logo.SVG'));
  assert.ok(url.startsWith('data:image/svg+xml;base64,'));
  assert.equal(Buffer.from(url.split(',')[1], 'base64').toString('utf8'), svg);
  const large = new Uint8Array(200_000).fill(65);
  assert.equal(Buffer.from(imageUrl(large.buffer, 'image/svg+xml').split(',')[1], 'base64').length, large.length, 'longer than one chunk');
  const png = imageUrl(new Uint8Array([137, 80, 78, 71]).buffer, imageType('a.png'));
  assert.ok(png.startsWith('blob:'));
  URL.revokeObjectURL(png);
});

test('a preview stops reading once more bytes arrive than it allows, whatever size was listed', async () => {
  let pulled = 0;
  const endless = new Response(new ReadableStream({pull(controller) { pulled += 1; controller.enqueue(new Uint8Array(1000).fill(97)); }}));
  assert.equal(await readLimited(endless, 4500), null);
  assert.ok(pulled <= 6, 'the stream is cancelled, not drained');
  const exact = await readLimited(new Response(new Uint8Array(4500).fill(98)), 4500);
  assert.equal(exact.byteLength, 4500);
  assert.equal(new Uint8Array(exact)[4499], 98);
  assert.equal((await readLimited(new Response(''), 10)).byteLength, 0);
  const viewer = fs.readFileSync(new URL('./FileViewer.vue', import.meta.url), 'utf8');
  assert.match(viewer, /const buffer = await readLimited\(response, limit\);\n\s*if \(buffer === null\) \{ error\.value = tooLarge; return; \}/);
});
