import assert from 'node:assert/strict';
import {createHash, randomBytes} from 'node:crypto';
import {rmSync} from 'node:fs';
import {access, cp, mkdir, mkdtemp, readdir, readFile, rename, rm, stat, utimes, writeFile} from 'node:fs/promises';
import {basename, dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createServer, version as viteVersion} from 'vite';
import vue from '@vitejs/plugin-vue';

const root = fileURLToPath(new URL('../', import.meta.url));
const temporaryRoot = fileURLToPath(new URL('../../workspace/browser-fixture-', import.meta.url));
const depsCacheRoot = fileURLToPath(new URL('../node_modules/.cache/openbear-browser-deps/', import.meta.url));

// Dependency pre-bundling repeats the same work in every test process: Vite scans
// the whole app from index.html and bundles every dependency with esbuild (about
// 2 CPU-seconds, which with eight browser files at once stretched to 10 s of scanning
// and 6 s of bundling before the first page could load). A fixture copies the last complete result for its own
// slot into its private cache before Vite starts, and Vite skips both steps when the
// copy's lockfile and config hashes still match; when they do not (a new lockfile,
// a changed plugin list, a newer Vite), Vite discards the copy and bundles from
// scratch as before. On close the fixture stores its result if it differs (first
// run, or dependencies Vite found while the pages ran), so later runs start with the
// full set and no mid-test re-optimisation reload.
// Slots are per fixture name, plugin names, Vite version and lockfile. Each stored
// result is an immutable directory named by its browserHash; `current` names the
// newest and is replaced by rename, so a reader never copies a half-written result.
// OPENBEAR_BROWSER_DEPS_CACHE=0 turns this off.
const depsCacheOn = process.env.OPENBEAR_BROWSER_DEPS_CACHE !== '0';
async function depsSlot(name, pluginNames) {
  const lock = await readFile(join(root, 'package-lock.json')).catch(() => '');
  const key = createHash('sha256').update(JSON.stringify([pluginNames, viteVersion])).update(lock).digest('hex').slice(0, 16);
  return join(depsCacheRoot, `${name.replace(/[^\w.-]/g, '_')}-${key}`);
}
// Every file the metadata names is present (a copy is never used half-made).
async function depsComplete(dir) {
  try {
    const meta = JSON.parse(await readFile(join(dir, '_metadata.json'), 'utf8'));
    const files = [...Object.values(meta.optimized || {}), ...Object.values(meta.chunks || {})].map(entry => entry.file);
    await Promise.all(files.map(file => access(join(dir, file))));
    return meta.browserHash || null;
  } catch { return null; }
}
async function restoreDeps(slot, cacheDir) {
  const current = (await readFile(join(slot, 'current'), 'utf8').catch(() => '')).trim();
  if (!current) return null;
  const deps = join(cacheDir, 'deps');
  try {
    await cp(join(slot, current), deps, {recursive: true});
    const hash = await depsComplete(deps);
    if (hash) {
      // Marks the slot as in use (see the pruning in saveDeps).
      const now = new Date();
      await utimes(join(slot, 'current'), now, now).catch(() => {});
      return hash;
    }
  } catch { /* pruned meanwhile: bundle as without a cache */ }
  await rm(deps, {recursive: true, force: true});
  return null;
}
async function saveDeps(slot, cacheDir, restored) {
  const deps = join(cacheDir, 'deps');
  const hash = await depsComplete(deps);
  if (!hash || hash === restored || !/^\w+$/.test(hash)) return;
  await mkdir(slot, {recursive: true});
  const target = join(slot, hash), temporary = join(slot, `.tmp-${process.pid}-${randomBytes(4).toString('hex')}`);
  try {
    await cp(deps, temporary, {recursive: true});
    // The same dependencies give the same browserHash. Another process may have stored
    // this result first (either copy is whole); a damaged one is replaced.
    if (!await depsComplete(target)) {
      await rm(target, {recursive: true, force: true});
      await rename(temporary, target).catch(() => {});
    }
  } finally { await rm(temporary, {recursive: true, force: true}); }
  if (!await depsComplete(target)) return;
  const pointer = join(slot, `.current-${process.pid}`);
  await writeFile(pointer, hash);
  await rename(pointer, join(slot, 'current'));
  // Older results nobody is likely still copying (a copy takes well under a second),
  // and temporaries of processes that are gone.
  const now = Date.now();
  for (const entry of await readdir(slot).catch(() => [])) {
    if (entry === hash || entry === 'current') continue;
    const owner = entry.match(/^\.(?:tmp|current)-(\d+)/);
    if (owner ? ownerAlive(Number(owner[1])) : now - (await stat(join(slot, entry)).catch(() => ({mtimeMs: now}))).mtimeMs < 600000) continue;
    await rm(join(slot, entry), {recursive: true, force: true}).catch(() => {});
  }
  // Slots of this fixture for an older lockfile, plugin list or Vite, unused for a day.
  const own = basename(slot), prefix = own.slice(0, own.lastIndexOf('-') + 1);
  for (const entry of await readdir(depsCacheRoot).catch(() => [])) {
    if (entry === own || !entry.startsWith(prefix) || !/^[0-9a-f]{16}$/.test(entry.slice(prefix.length))) continue;
    const used = (await stat(join(depsCacheRoot, entry, 'current')).catch(() => null))?.mtimeMs ?? 0;
    if (now - used > 86400000) await rm(join(depsCacheRoot, entry), {recursive: true, force: true}).catch(() => {});
  }
}

// close() normally removes a fixture cache. Caches still open when the process
// exits (an uncaught failure, Playwright's SIGINT exit) are removed on 'exit'.
// A SIGKILL (e.g. a caller's timeout killing the whole process group) runs no
// handler at all, so each cache name carries its owner's PID and the next
// fixture removes caches whose owner no longer exists.
const openCaches = new Set();
process.on('exit', () => { for (const dir of openCaches) rmSync(dir, {recursive: true, force: true}); });
const ownerAlive = pid => {
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
};
async function removeOrphanedCaches() {
  const parent = dirname(temporaryRoot), prefix = basename(temporaryRoot);
  const names = await readdir(parent).catch(() => []);
  await Promise.all(names.map(name => {
    const owner = name.startsWith(prefix) && name.slice(prefix.length).match(/^(\d+)-/);
    if (!owner || Number(owner[1]) === process.pid || ownerAlive(Number(owner[1]))) return null;
    return rm(join(parent, name), {recursive: true, force: true}).catch(() => {});
  }));
}

// Vite's dev server attaches a source map to every JavaScript response, and builds it
// again on every request: modules without one get a fresh fallback map (MagicString
// with hires boundaries over the whole file, dependency bundles included), modules
// with one are serialised and base64-encoded inline. Every new browser context asks
// for every module again, so this was about half of a test process's CPU (13 of 24 s
// in gateway.browser). `{mappings: ''}` at the end of the transform chain tells Vite
// there is no map, and the response goes out as it is. Stack traces in a page then
// point into the served code. OPENBEAR_BROWSER_SOURCEMAPS=1 keeps the maps (to step
// through a page in DevTools while debugging a test).
const noSourceMapsPlugin = {name: 'fixture-no-sourcemaps', enforce: 'post', transform: code => ({code, map: {mappings: ''}})};
const sourceMapsOn = process.env.OPENBEAR_BROWSER_SOURCEMAPS === '1';

// Each suite owns its HTTP listener AND optimizer cache. Different Vite configs
// must not replace each other's dependency hashes while a browser is using them.
// Like the real backend, write the serving process's login mode into the page.
// authMode may be a function so a fixture can switch modes between pages.
function authModePlugin(authMode) {
  return {name: 'fixture-auth-mode', transformIndexHtml: html => html.replace('"[[controller]]"', JSON.stringify(typeof authMode === 'function' ? authMode() : authMode))};
}
// warmupTimeoutMs bounds only the Vite transform of the entries (it runs before
// any page or assertion); suites that never had a separate warmup bound pass
// their own test budget.
export async function startBrowserTestServer(t, {name, plugins, entries, authMode = 'controller', warmupTimeoutMs = 30000}) {
  await removeOrphanedCaches();
  const cacheDir = await mkdtemp(`${temporaryRoot}${process.pid}-`);
  openCaches.add(cacheDir);
  const allPlugins = [vue(), authModePlugin(authMode), ...plugins, ...(sourceMapsOn ? [] : [noSourceMapsPlugin])];
  const slot = depsCacheOn ? await depsSlot(name, allPlugins.map(plugin => plugin.name)) : null;
  const restored = slot ? await restoreDeps(slot, cacheDir) : null;
  let server;
  const close = async () => {
    try {
      await server?.close();
      if (slot) await saveDeps(slot, cacheDir, restored).catch(() => {});
    } finally { await rm(cacheDir, {recursive: true, force: true}); openCaches.delete(cacheDir); }
  };
  try {
    server = await createServer({
      root, configFile: false, cacheDir, plugins: allPlugins,
      // Dependency source maps are two thirds of a stored result (40 of 60 MB) and
      // are dropped from every response anyway (above).
      optimizeDeps: {esbuildOptions: {sourcemap: sourceMapsOn}},
      // No production proxy or source-edit HMR in an isolated fixture.
      server: {host: '127.0.0.1', strictPort: true, hmr: false, watch: null},
    });
    // Vite's server.listen(0) falls back to 5173. Keep Vite's initialization
    // wrapper, but pass 0 directly to the underlying Node listener: the kernel
    // reserves a unique port, without a find-free-port/close/rebind race.
    await new Promise((resolve, reject) => {
      server.httpServer.once('error', reject);
      server.httpServer.listen(0, '127.0.0.1', () => {
        server.httpServer.off('error', reject);
        resolve();
      });
    });
    const port = server.httpServer.address().port;
    assert.ok(port > 0);
    const base = `http://127.0.0.1:${port}`;
    // Transform the actual fixture entry/lazy view graph before the UI's short
    // interaction deadlines start. This executes no application/API requests.
    let timer;
    try {
      await Promise.race([
        (async () => {
          await Promise.all(entries.map(entry => server.transformRequest(entry)));
          await server.waitForRequestsIdle();
        })(),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${name}: Vite entry warmup exceeded ${warmupTimeoutMs / 1000}s`)), warmupTimeoutMs); }),
      ]);
    } finally { clearTimeout(timer); }
    // Without a stored result the first page would wait for the dependency bundle
    // inside its own short goto deadline (several pages at once, under load, can pass
    // 15 s). Wait for it here instead, under a bound of its own; with a stored result
    // it is ready at once.
    const optimizer = server.environments?.client?.depsOptimizer;
    if (optimizer) {
      const ready = () => !Object.keys(optimizer.metadata.discovered).length && Object.keys(optimizer.metadata.optimized).length > 0;
      const until = Date.now() + 120000;
      while (!ready()) {
        if (Date.now() > until) throw new Error(`${name}: Vite dependency bundling exceeded 120s`);
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }
    t.diagnostic(`${name}: origin=${base} cache=${cacheDir} deps=${restored ? 'reused' : 'bundled'}`);
    return {server, base, close};
  } catch (error) {
    await close();
    throw error;
  }
}

// Readiness is a DOM/application condition, not networkidle (the real app keeps
// sockets/presence active). Preserve page errors and the failing URL for diagnosis.
export async function waitForBrowserReady(page, locator, errors) {
  try {
    await locator.waitFor({timeout: 15000});
  } catch (cause) {
    const text = await page.locator('body').innerText({timeout: 1000}).catch(() => 'unavailable');
    throw new Error(`Fixture not ready at ${page.url()}: ${JSON.stringify(errors)}\n${text.slice(0, 2800)}`, {cause});
  }
}
