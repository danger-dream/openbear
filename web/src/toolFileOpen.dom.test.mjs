// Opening a tool's file from its row on the real App without a browser: only file tools get the
// open button, the chevron sits after it, the preview shows the file's name, path, origin and
// "changed since" note, long source is still highlighted, opening never expands the row, and each
// open re-reads the file's metadata. Where the button sits, the full-screen phone preview and the
// decoded image stay in toolFileOpen.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from './testing/appHarness.mjs';
import {createToolFileBackend, script, uuid} from './toolFileOpenFixture.mjs';

const App = (await import('./App.vue')).default;

for (const width of [1440, 390]) test(`${width}px: a file tool row opens its file in the preview without expanding the row`, async t => {
  const backend = createToolFileBackend(), {fileRequests} = backend;
  const {page, unmount} = await openPage(t, App, {url: `/chat?id=${uuid}`, width, height: 844, handler: backend.handle, onSocket: backend.socket,
    storage: {'openbear.notifications.prompt.v1': 'dismissed'}});
  const rowOf = name => page.locator('.conversation-process').filter({has: page.locator('.process-preview', {hasText: name})});
  const readRow = rowOf('shot.png'), writeRow = rowOf('benchmark_package.py'), bashRow = rowOf('列出目录');
  // A finished turn keeps its tool rows inside the collapsed work summary.
  const work = page.getByRole('button', {name: /已工作/});
  await work.waitFor({state: 'visible'});
  await work.click();
  await readRow.waitFor({state: 'visible'});
  assert.equal(await bashRow.locator('.process-file-open').count(), 0, 'non-file tools get no button');
  assert.equal(await readRow.locator('.process-file-open').count(), 1);
  assert.equal(await writeRow.locator('.process-file-open').count(), 1);
  // The expand chevron moves behind the eye and still toggles the row.
  assert.equal(await readRow.locator('.process-row > .process-chevron').count(), 1);
  assert.equal(await readRow.locator('.process-summary .process-chevron').count(), 0);

  await readRow.locator('.process-file-open').click();
  const dialog = page.locator('.artifact-preview-dialog');
  await dialog.waitFor({state: 'visible'});
  await page.locator('.artifact-preview-image-stage img').waitFor({state: 'attached'});
  assert.equal(await dialog.locator('.artifact-preview-heading h2').textContent(), 'shot.png');
  assert.match(await dialog.locator('.artifact-preview-heading p').textContent(), /^\/tmp\/fileview\/shot\.png · /);
  assert.match(await dialog.locator('.artifact-preview-footer').textContent(), /^本机文件/);
  assert.equal(await readRow.locator('.process-summary').getAttribute('aria-expanded'), 'false', 'opening the file does not expand the row');
  await page.keyboard.press('Escape');
  await dialog.waitFor({state: 'hidden'});

  await writeRow.locator('.process-file-open').click();
  await dialog.waitFor({state: 'visible'});
  await dialog.locator('.artifact-preview-source code', {hasText: 'def main'}).waitFor();
  assert.equal(await dialog.locator('.artifact-preview-heading h2').textContent(), 'benchmark_package.py');
  assert.equal(await dialog.locator('.artifact-preview-file-changed').textContent(), '这次调用之后，文件又被改过');
  assert.ok(script.length > 8000);
  assert.ok(await dialog.locator('.artifact-preview-source code .hljs-keyword').count() > 100, 'large source files are highlighted');
  assert.equal(await dialog.locator('.artifact-preview-large-note').count(), 0);
  await page.keyboard.press('Escape');
  // Each open re-reads the file's metadata from disk.
  await writeRow.locator('.process-file-open').click();
  await dialog.locator('.artifact-preview-source code', {hasText: 'def main'}).waitFor();
  assert.equal(fileRequests.filter(item => item === 'tool:call-write').length, 2, fileRequests.join(','));
  assert.ok(!fileRequests.some(item => item.startsWith('tool:call-bash')));
  await unmount();
});
