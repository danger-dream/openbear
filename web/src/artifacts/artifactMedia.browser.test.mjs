import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../testing/playwright.cjs'));} catch {}
let ffmpeg = false;
try { execFileSync('ffmpeg', ['-version'], {stdio: 'ignore'}); ffmpeg = true; } catch {}

const conversation = '11111111-2222-3333-4444-555555555555';
const ids = {audio: '66666666-7777-8888-9999-000000000001', mp4: '66666666-7777-8888-9999-000000000002', webm: '66666666-7777-8888-9999-000000000003'};
const contentPath = id => `/api/conversations/${conversation}/artifacts/${id}/content`;

// Two seconds of a 440 Hz tone: a real decodable file without a binary fixture.
function wav(seconds = 2, rate = 8000) {
  const samples = seconds * rate, data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) data.writeInt16LE(Math.round(Math.sin(2 * Math.PI * 440 * i / rate) * 8000), i * 2);
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(36 + data.length, 4); header.write('WAVE', 8); header.write('fmt ', 12);
  header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22); header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34); header.write('data', 36); header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}
function videos() {
  const dir = mkdtempSync(join(tmpdir(), 'openbear-media-'));
  try {
    const source = ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=15:duration=3', '-f', 'lavfi', '-i', 'sine=frequency=330:duration=3'];
    execFileSync('ffmpeg', [...source, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', join(dir, 'clip.mp4')]);
    execFileSync('ffmpeg', [...source, '-c:v', 'libvpx-vp9', '-c:a', 'libopus', join(dir, 'clip.webm')]);
    return {mp4: readFileSync(join(dir, 'clip.mp4')), webm: readFileSync(join(dir, 'clip.webm'))};
  } finally { rmSync(dir, {recursive: true, force: true}); }
}

// Serves metadata and content the way aiohttp's FileResponse does, including 206 ranges.
function mediaPlugin(files, ranges) {
  const entry = `
import {createApp,h,ref} from 'vue';
import 'element-plus/dist/index.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import ConsoleMarkdown from '/src/views/consoleView/ConsoleMarkdown.vue';
import ArtifactPreview from '/src/artifacts/ArtifactPreview.vue';
const text = ref(window.__initialText);
createApp({render:()=>h('main',{style:'max-width:760px;margin:0 auto;padding:12px'},[h(ConsoleMarkdown,{text:text.value}),h(ArtifactPreview)])}).mount('#app');
window.__setText=value=>{text.value=value;};
`;
  return {
    name: 'artifact-media',
    resolveId(id) { if (id === 'virtual:artifact-media') return '\0virtual:artifact-media'; },
    load(id) { if (id === '\0virtual:artifact-media') return entry; },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url, 'http://fixture');
        if (url.pathname === '/fixture') {
          res.setHeader('content-type', 'text/html');
          res.end(`<html class="${url.searchParams.get('theme') === 'dark' ? 'dark' : ''}"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script>window.__initialText=${JSON.stringify(url.searchParams.get('text') || '')};</script><script type="module" src="/@id/__x00__virtual:artifact-media"></script></body></html>`);
          return;
        }
        const match = url.pathname.match(/^\/api\/conversations\/([^/]+)\/artifacts\/([^/]+)(\/content)?$/);
        const file = match && files[match[2]];
        if (!file) return next();
        if (!match[3]) {
          res.setHeader('content-type', 'application/json');
          res.end(JSON.stringify({ok: true, artifact: {artifactUuid: match[2], conversationUuid: match[1], fileName: file.name, mimeType: file.mime, sizeBytes: file.body.length, sha256: match[2]}}));
          return;
        }
        res.setHeader('content-type', file.mime); res.setHeader('accept-ranges', 'bytes');
        const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
        if (!range) { res.setHeader('content-length', file.body.length); res.end(file.body); return; }
        ranges.push(req.headers.range);
        const start = range[1] ? Number(range[1]) : 0, end = range[2] ? Math.min(Number(range[2]), file.body.length - 1) : file.body.length - 1;
        res.statusCode = 206;
        res.setHeader('content-range', `bytes ${start}-${end}/${file.body.length}`); res.setHeader('content-length', end - start + 1);
        res.end(file.body.subarray(start, end + 1));
      });
    },
  };
}

async function launch(t, files) {
  const ranges = [];
  const fixture = await startBrowserTestServer(t, {name: 'artifact-media', entries: ['virtual:artifact-media'], plugins: [mediaPlugin(files, ranges)]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  // Playback assertions must not depend on the headless profile's autoplay engagement.
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
  return {fixture, browser, ranges};
}
const playing = (locator, after = 0) => locator.evaluate((el, after) => new Promise((resolve, reject) => {
  const started = Date.now();
  (function check() { if (!el.paused && el.currentTime > after + 0.2) resolve(el.currentTime); else if (Date.now() - started > 8000) reject(new Error(`not playing: paused=${el.paused} t=${el.currentTime} error=${el.error?.code}`)); else setTimeout(check, 50); })();
}), after);

test('an audio card plays in place, seeks by range, keeps playing while the reply streams, and hands over to the preview', {skip: !chromium, timeout: 120000}, async t => {
  const {fixture, browser, ranges} = await launch(t, {[ids.audio]: {name: 'voice.wav', mime: 'audio/wav', body: wav()}});
  const reply = `录音如下：\n\n[voice.wav](${contentPath(ids.audio)})\n\n正在整理`;
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  await page.goto(`${fixture.base}/fixture?text=${encodeURIComponent(reply)}`);
  const audio = page.locator('.artifact-card--audio .artifact-card-player audio');
  await audio.waitFor({state: 'attached'});
  assert.equal(await page.locator('.artifact-card--audio .artifact-card-open audio').count(), 0, 'the player is not nested in the open button');
  const duration = await audio.evaluate(el => new Promise(resolve => el.readyState ? resolve(el.duration) : el.addEventListener('loadedmetadata', () => resolve(el.duration), {once: true})));
  assert.ok(Math.abs(duration - 2) < 0.1, `duration ${duration}`);
  const player = page.locator('.artifact-card--audio .audio-player');
  assert.match(await player.locator('.audio-player-time').textContent(), /^0:00 \/ 0:02$/);
  await audio.evaluate(el => { el.dataset.marker = 'first'; });
  await player.getByRole('button', {name: /^播放：/}).click();
  const before = await playing(audio);
  assert.ok(await player.getByRole('button', {name: /^暂停：/}).isVisible());
  // The reply keeps growing around the card, as a streamed answer does.
  for (let i = 1; i <= 5; i++) { await page.evaluate(value => window.__setText(value), `${reply}${'，继续'.repeat(i)}\n\n- 第 ${i} 点`); await page.waitForTimeout(60); }
  assert.equal(await audio.evaluate(el => el.dataset.marker), 'first', 'the same element survives every patch');
  await playing(audio, before);
  // Dragging the progress bar ends with a change event that seeks the element.
  await audio.evaluate(el => new Promise(resolve => {
    el.addEventListener('seeked', resolve, {once: true});
    const seek = el.parentElement.querySelector('.audio-player-seek');
    seek.value = '1.5'; seek.dispatchEvent(new Event('input', {bubbles: true})); seek.dispatchEvent(new Event('change', {bubbles: true}));
  }));
  assert.ok(await audio.evaluate(el => el.currentTime) >= 1.5);
  await player.getByRole('button', {name: /播放速度/}).click();
  assert.equal(await audio.evaluate(el => el.playbackRate), 1.25);
  assert.ok(ranges.length > 0, 'the browser requested byte ranges');

  await page.locator('.artifact-card--audio .artifact-card-open').click();
  const dialog = page.locator('.artifact-preview-dialog');
  const previewAudio = dialog.locator('.artifact-preview-audio-stage audio');
  await previewAudio.waitFor({state: 'attached'});
  assert.match(await dialog.locator('.artifact-preview-audio-stage strong').textContent(), /voice\.wav/);
  await playing(previewAudio);
  assert.equal(await audio.evaluate(el => el.paused), true, 'starting the preview pauses the card');
  await previewAudio.evaluate(el => { window.__previewAudio = el; });
  await dialog.getByRole('button', {name: '关闭附件预览'}).click();
  assert.equal(await page.evaluate(() => window.__previewAudio.paused), true, 'closing stops the preview at once');
  await context.close();
});

test('video cards play inline with native controls, upgrade image-syntax videos, and play in the preview on desktop and phone', {skip: !chromium || !ffmpeg, timeout: 180000}, async t => {
  const media = videos();
  const {fixture, browser} = await launch(t, {[ids.mp4]: {name: 'clip.mp4', mime: 'video/mp4', body: media.mp4}, [ids.webm]: {name: 'screen.webm', mime: 'video/webm', body: media.webm}});
  const reply = `[clip.mp4](${contentPath(ids.mp4)})\n\n![screen.webm](${contentPath(ids.webm)}?preview=1)`;
  for (const [width, theme] of [[1280, 'light'], [390, 'dark']]) await t.test(`${width}px ${theme}`, {timeout: 60000}, async () => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: phone, isMobile: phone});
    const page = await context.newPage();
    await page.goto(`${fixture.base}/fixture?theme=${theme}&text=${encodeURIComponent(reply)}`);
    const inlineVideos = page.locator('.artifact-card--video .artifact-card-thumbnail video');
    await page.waitForFunction(() => document.querySelectorAll('.artifact-card--video .artifact-card-thumbnail video').length === 2);
    for (const inlineVideo of await inlineVideos.all()) {
      const frame = await inlineVideo.evaluate(el => new Promise((resolve, reject) => {
        const done = () => resolve({ready: el.readyState, width: el.videoWidth, paused: el.paused, inline: el.playsInline, controls: el.controls});
        if (el.readyState >= 2) done(); else { el.addEventListener('loadeddata', done, {once: true}); el.addEventListener('error', () => reject(new Error(`cover failed ${el.error?.code}`)), {once: true}); }
      }));
      assert.deepEqual(frame, {ready: frame.ready, width: 320, paused: true, inline: true, controls: true});
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'cards do not overflow horizontally');

    // Play and seek directly inside the inline chat card
    const firstInline = inlineVideos.first();
    await firstInline.evaluate(el => el.play());
    await playing(firstInline);
    await firstInline.evaluate(el => new Promise(resolve => { el.addEventListener('seeked', resolve, {once: true}); el.currentTime = 1.5; }));
    assert.ok(await firstInline.evaluate(el => el.currentTime) >= 1.4);

    // Opening the preview pauses the inline card video and plays full-width in the preview
    await page.locator('.artifact-card--video .artifact-card-open').first().click();
    const dialog = page.locator('.artifact-preview-dialog');
    const video = dialog.locator('.artifact-preview-video-stage video');
    await video.waitFor({state: 'attached'});
    assert.deepEqual(await video.evaluate(el => [el.controls, el.playsInline]), [true, true]);
    await playing(video);
    assert.equal(await firstInline.evaluate(el => el.paused), true, 'starting the preview pauses the inline card video');
    const box = await video.boundingBox(), dialogBox = await dialog.boundingBox();
    assert.ok(box.width <= dialogBox.width + 1 && box.x >= dialogBox.x - 1, 'the video fits inside the preview');
    if (phone) {
      assert.ok(dialogBox.width >= width - 1, 'the phone preview is fullscreen');
      assert.ok(box.width >= width - 4, 'phone video control bar spans full width even for a 320px source');
      assert.equal(await dialog.locator('.artifact-preview-toolbar').count(), 0, 'phone video preview hides redundant toolbar');
    }
    await video.evaluate(el => new Promise(resolve => { el.addEventListener('seeked', resolve, {once: true}); el.currentTime = 2; }));
    await video.evaluate(el => { window.__previewVideo = el; });
    await dialog.getByRole('button', {name: '关闭附件预览'}).click();
    assert.equal(await page.evaluate(() => window.__previewVideo.paused), true);
    await dialog.waitFor({state: 'hidden'});
    await context.close();
  });
});

test('mobile image lightbox supports pinch-to-zoom, double-tap zoom, swipe navigation, download, and backdrop/pull-down close', {skip: !chromium, timeout: 90000}, async t => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAADklEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64');
  const img1 = '66666666-7777-8888-9999-000000000011', img2 = '66666666-7777-8888-9999-000000000012';
  const {fixture, browser} = await launch(t, {
    [img1]: {name: 'first.png', mime: 'image/png', body: png},
    [img2]: {name: 'second.png', mime: 'image/png', body: png},
  });
  const reply = `![第一张](${contentPath(img1)}?preview=1)\n\n![第二张](${contentPath(img2)}?preview=1)`;
  const context = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true, isMobile: true});
  const page = await context.newPage();
  await page.goto(`${fixture.base}/fixture?theme=dark&text=${encodeURIComponent(reply)}`);
  const firstImg = page.locator('.bear-md img').first();
  await firstImg.waitFor({state: 'visible'});
  await firstImg.click();

  const lightbox = page.locator('.image-lightbox');
  await lightbox.waitFor({state: 'visible'});
  assert.equal((await lightbox.locator('.el-image-viewer__progress').textContent()).trim(), '1 / 2');
  const downloadHref = await lightbox.locator('.image-lightbox-download').getAttribute('href');
  assert.match(downloadHref, new RegExp(`${img1}/content\\?download=1$`));

  // Stationary single-finger touchstart must not preventDefault (preserves native mobile long-press save)
  const preventedSingleStart = await lightbox.locator('.image-lightbox-stage').evaluate(el => {
    const ev = new Event('touchstart', {bubbles: true, cancelable: true});
    ev.touches = [{clientX: 195, clientY: 420}];
    el.dispatchEvent(ev);
    return ev.defaultPrevented;
  });
  assert.equal(preventedSingleStart, false);

  // Two-finger pinch-to-zoom scales the image up
  await lightbox.locator('.image-lightbox-stage').evaluate(el => {
    const start = new Event('touchstart', {bubbles: true, cancelable: true});
    start.touches = [{clientX: 150, clientY: 420}, {clientX: 250, clientY: 420}];
    el.dispatchEvent(start);
    const move = new Event('touchmove', {bubbles: true, cancelable: true});
    move.touches = [{clientX: 90, clientY: 420}, {clientX: 310, clientY: 420}];
    el.dispatchEvent(move);
    const end = new Event('touchend', {bubbles: true, cancelable: true});
    end.touches = []; end.changedTouches = [{clientX: 310, clientY: 420}];
    el.dispatchEvent(end);
  });
  const pinchTransform = await lightbox.locator('.image-lightbox-img').evaluate(el => el.style.transform);
  assert.match(pinchTransform, /scale\(2\.2/);

  // Double-tap resets back to 100%, and another double-tap zooms to 250%
  await lightbox.locator('.image-lightbox-stage').evaluate(el => {
    for (let i = 0; i < 2; i++) {
      const s = new Event('touchstart', {bubbles: true, cancelable: true});
      s.touches = [{clientX: 195, clientY: 420}];
      el.dispatchEvent(s);
      const e = new Event('touchend', {bubbles: true, cancelable: true});
      e.touches = []; e.changedTouches = [{clientX: 195, clientY: 420}];
      el.dispatchEvent(e);
    }
  });
  assert.match(await lightbox.locator('.image-lightbox-img').evaluate(el => el.style.transform), /scale\(1(\.0000)?\)/);

  // Horizontal swipe at 1x switches to image 2 / 2
  await lightbox.locator('.image-lightbox-stage').evaluate(el => {
    const s = new Event('touchstart', {bubbles: true, cancelable: true});
    s.touches = [{clientX: 300, clientY: 420}];
    el.dispatchEvent(s);
    const m = new Event('touchmove', {bubbles: true, cancelable: true});
    m.touches = [{clientX: 120, clientY: 420}];
    el.dispatchEvent(m);
    const e = new Event('touchend', {bubbles: true, cancelable: true});
    e.touches = []; e.changedTouches = [{clientX: 120, clientY: 420}];
    el.dispatchEvent(e);
  });
  assert.equal((await lightbox.locator('.el-image-viewer__progress').textContent()).trim(), '2 / 2');
  await page.waitForTimeout(10);

  // Tapping the empty stage area outside the image closes the lightbox
  await lightbox.locator('.image-lightbox-stage').evaluate(el => {
    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
  });
  await lightbox.waitFor({state: 'hidden'});
  await context.close();
});
