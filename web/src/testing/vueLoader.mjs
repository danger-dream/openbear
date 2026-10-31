// Test-only Node module hooks: component tests import the real .vue files, and the modules they
// use, without Vite (Node >= 20.6, module.register; works on Node 20 and 24).
//
// Usage — register before the first component import (static imports are hoisted, so import
// components dynamically after this call):
//   import '../domTestEnv.mjs';                       // first: Vue reads `document` when it loads
//   import {registerVueLoader} from '../testing/vueLoader.mjs';
//   registerVueLoader({stubs: {'monaco-editor': new URL('./monacoStub.mjs', import.meta.url).href}});
//   const View = (await import('../views/ServersView.vue')).default;
// testing/mountApp.mjs calls registerVueLoader() (no stubs) when it is imported.
// Import big CommonJS packages (vue, element-plus) before registering, as mountApp.mjs does:
// once hooks are registered every require() inside them goes through the hooks thread, which
// makes loading element-plus about five times slower.
//
// Stubs: registerVueLoader({stubs}) registers once per process and later calls do nothing
// (the hooks run in their own thread), so pass every stub on the first call — to use stubs
// with mountApp.mjs, call registerVueLoader({stubs}) in a module imported before it.
// stubs maps an import specifier, exactly as written in the importing source (for example
// 'monaco-editor', or './CodeEditor.vue'), to the URL of the module that replaces it, e.g.
//   registerVueLoader({stubs: {'monaco-editor': new URL('./monacoStub.mjs', import.meta.url).href}});
//
// What the hooks do:
// - *.vue is compiled with @vue/compiler-sfc (script, template, and <style> blocks);
// - the app's own CSS (CSS files under src/ and the <style> blocks) goes to testing/cssIndex.mjs,
//   so tests know which elements are hidden and which are block-level (no layout: sizes stay 0);
//   CSS from node_modules becomes an empty module;
// - `?raw` imports return the file text, `?url` and assets a path string, `?worker` a stub class;
// - extensionless relative imports resolve like Vite: .js, .vue, .mjs, /index.js in that order;
// - compiled SFCs are cached on disk by content hash (compiler version + these hooks + file +
//   source), shared by parallel test processes: compiling a page's SFCs takes longer than its tests;
// - a fresh copy of the app: importing a file under src/ with `?fresh=N` gives it, and every app
//   module it imports (src/ outside src/testing/, no CSS), new instances for that N, so module
//   state (stores, caches) starts over as on a new page:
//     const View = (await import(`../views/ServersView.vue?fresh=${n}`)).default;
//   Packages (vue, element-plus) and the test helpers stay shared.
import {register} from 'node:module';
import {createHash} from 'node:crypto';
import {mkdir, readFile, rename, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

let registered = false;
export function registerVueLoader({stubs = {}} = {}) {
  if (registered) return;
  registered = true;
  register(import.meta.url, {parentURL: import.meta.url, data: {stubs}});
}

// ---- hooks (run in the loader thread) ----
let stubMap = {};
export function initialize(data = {}) { stubMap = data.stubs || {}; }

const STYLE = /\.(css|scss|sass|less|styl)$/;
const ASSET = /\.(png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|mp3|wav)$/;
const CSS_INDEX = new URL('./cssIndex.mjs', import.meta.url).href;
const dataModule = source => `data:text/javascript,${encodeURIComponent(source)}`;
const cssModule = (css, file) => `import {addStyles} from ${JSON.stringify(CSS_INDEX)}; addStyles(${JSON.stringify(css)}, ${JSON.stringify(file)}); export default '';`;

async function tryResolve(specifier, context, next) {
  try { return await next(specifier, context); } catch (error) {
    const relative = specifier.startsWith('.') || specifier.startsWith('/') || specifier.startsWith('file:');
    if (!relative || !['ERR_MODULE_NOT_FOUND', 'ERR_UNSUPPORTED_DIR_IMPORT'].includes(error.code)) throw error;
    for (const suffix of ['.js', '.vue', '.mjs', '/index.js']) {
      try { return await next(specifier + suffix, context); } catch { /* next candidate */ }
    }
    throw error;
  }
}

const SRC = new URL('../', import.meta.url).href, TESTING = new URL('./', import.meta.url).href;
export async function resolve(specifier, context, next) {
  const fresh = /[?&]fresh=(\d+)/.exec(context.parentURL || '')?.[1];
  if (!fresh) return resolveOne(specifier, context, next);
  const result = await resolveOne(specifier, context, next);
  const {url} = result;
  if (url.startsWith(SRC) && !url.startsWith(TESTING) && !url.includes('/node_modules/') && !url.includes('?') && !STYLE.test(url)) return {...result, url: `${url}?fresh=${fresh}`};
  return result;
}

async function resolveOne(specifier, context, next) {
  if (Object.hasOwn(stubMap, specifier)) return {url: stubMap[specifier], shortCircuit: true};
  const [path, query = ''] = specifier.split('?');
  if (query === 'worker' || query.startsWith('worker&')) return {url: dataModule('export default class TestWorker { postMessage() {} terminate() {} addEventListener() {} removeEventListener() {} }'), shortCircuit: true};
  if (query === 'url' || (ASSET.test(path) && !query)) return {url: dataModule(`export default ${JSON.stringify(path)};`), shortCircuit: true};
  if (query === 'raw') {
    const resolved = await tryResolve(path, context, next);
    return {url: `${resolved.url}?raw`, shortCircuit: true};
  }
  if (STYLE.test(path)) {
    // The app's CSS is read in load(); CSS a package ships is not needed.
    const resolved = await tryResolve(path, context, next).catch(() => null);
    if (!resolved || !resolved.url.startsWith('file:') || resolved.url.includes('/node_modules/') || !resolved.url.endsWith('.css')) return {url: dataModule('export default "";'), shortCircuit: true};
    return {url: resolved.url, shortCircuit: true};
  }
  return tryResolve(specifier, context, next);
}

let sfc, version, hooksHash;
async function compileVue(source, filename) {
  const {parse, compileScript, compileTemplate, compileStyle} = sfc;
  const {descriptor, errors} = parse(source, {filename});
  if (errors.length) throw new Error(`${filename}: ${errors[0].message || errors[0]}`);
  const id = createHash('sha256').update(filename).digest('hex').slice(0, 8);
  const scoped = descriptor.styles.some(s => s.scoped);
  let code;
  if (descriptor.script || descriptor.scriptSetup) {
    code = compileScript(descriptor, {id, inlineTemplate: Boolean(descriptor.scriptSetup), genDefaultAs: '_sfc_main'}).content;
  } else code = 'const _sfc_main = {};';
  if (descriptor.template && !descriptor.scriptSetup) {
    const template = compileTemplate({source: descriptor.template.content, filename, id, scoped, compilerOptions: {scopeId: scoped ? `data-v-${id}` : undefined}});
    if (template.errors.length) throw new Error(`${filename}: ${template.errors[0].message || template.errors[0]}`);
    code += `\n${template.code.replace(/\nexport function render/, '\nfunction _sfc_render').replace(/^export function render/, 'function _sfc_render')}\n_sfc_main.render = _sfc_render;`;
  }
  if (scoped) code += `\n_sfc_main.__scopeId = 'data-v-${id}';`;
  const styles = descriptor.styles.map(style => compileStyle({source: style.content, filename, id: `data-v-${id}`, scoped: style.scoped}).code).join('\n');
  if (styles.trim()) code = `import {addStyles as _addStyles} from ${JSON.stringify(CSS_INDEX)}; _addStyles(${JSON.stringify(styles)}, ${JSON.stringify(filename)});\n${code}`;
  return `${code}\n_sfc_main.__file = ${JSON.stringify(filename)};\nexport default _sfc_main;`;
}

const CACHE = fileURLToPath(new URL('../../../.cache/tests/vue/', import.meta.url));
export async function load(url, context, next) {
  const [path, rawQuery = ''] = url.split('?');
  const query = rawQuery.replace(/(^|&)fresh=\d+/, '').replace(/^&/, '');
  if (query === 'raw' && path.startsWith('file:')) return {format: 'module', source: `export default ${JSON.stringify(await readFile(fileURLToPath(path), 'utf8'))};`, shortCircuit: true};
  if (path.startsWith('file:') && path.endsWith('.css') && !path.includes('/node_modules/')) {
    const file = fileURLToPath(path);
    return {format: 'module', source: cssModule(await readFile(file, 'utf8'), file), shortCircuit: true};
  }
  if (path.startsWith('file:') && path.endsWith('.vue')) {
    sfc ||= await import('@vue/compiler-sfc');
    version ||= JSON.parse(await readFile(fileURLToPath(new URL('../../node_modules/@vue/compiler-sfc/package.json', import.meta.url)), 'utf8').catch(() => '{"version":"?"}')).version;
    hooksHash ||= createHash('sha256').update(await readFile(fileURLToPath(import.meta.url))).digest('hex').slice(0, 12);
    const filename = fileURLToPath(path), source = await readFile(filename, 'utf8');
    const cached = join(CACHE, createHash('sha256').update(`${version}\0${hooksHash}\0${filename}\0${source}`).digest('hex') + '.mjs');
    let code = await readFile(cached, 'utf8').catch(() => null);
    if (code === null) {
      code = await compileVue(source, filename);
      // Write then rename, so a parallel process never reads half a file.
      const partial = `${cached}.${process.pid}.${Math.random().toString(36).slice(2)}`;
      await mkdir(CACHE, {recursive: true});
      await writeFile(partial, code);
      await rename(partial, cached).catch(() => {});
    }
    return {format: 'module', source: code, shortCircuit: true};
  }
  return next(url, context);
}
