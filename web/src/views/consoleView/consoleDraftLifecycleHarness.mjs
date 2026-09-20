import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {build} from 'esbuild';
import {parse, compileScript} from '@vue/compiler-sfc';
import {createMemoryAttachmentDraftDriver} from './attachmentDraftMemoryDriver.mjs';

const require = createRequire(import.meta.url);
const vue = require('vue');
const directory = path.dirname(fileURLToPath(import.meta.url));
const consoleFile = path.join(directory, 'ConsoleView.vue');
let compiledSetup;

// Compile the entire production setup, including its actual watchers and lifecycle
// callbacks. Only browser UI, transport and the storage driver are seams. Keep the
// bundle in memory: running this regression never writes generated source files.
async function consoleSetupCode() {
  if (compiledSetup) return compiledSetup;
  compiledSetup = build({
    entryPoints: [consoleFile], bundle: true, write: false, platform: 'node', format: 'cjs', logLevel: 'silent',
    plugins: [{name: 'draft-lifecycle-seams', setup(b) {
      b.onResolve({filter: /^vue$/}, () => ({path: 'vue', external: true}));
      b.onResolve({filter: /^element-plus$/}, () => ({path: 'messages', namespace: 'seam'}));
      b.onResolve({filter: /api\.js$/}, () => ({path: 'api', namespace: 'seam'}));
      b.onResolve({filter: /^@element-plus\/icons-vue$/}, () => ({path: 'icons', namespace: 'seam'}));
      b.onResolve({filter: /attachmentDraftStorage\.js$/}, a => a.importer === consoleFile
        ? {path: 'storage', namespace: 'seam'} : undefined);
      b.onLoad({filter: /.*/, namespace: 'seam'}, ({path: name}) => {
        const contents = {
          messages: 'export const ElMessage=__env.messages; export const ElMessageBox=__env.confirm;',
          api: 'export const Api=__env.api; export const apiError=e=>e.message||String(e); export const conversationWsUrl=id=>`ws://test/${id}`;',
          icons: Object.keys(require('@element-plus/icons-vue')).map(key => `export const ${key}={};`).join('\n'),
          storage: `import {createAttachmentDraftStorage as actual} from ${JSON.stringify(path.join(directory, 'attachmentDraftStorage.js'))}; export const createAttachmentDraftStorage=()=>actual({driver:__env.driver});`,
        }[name];
        return {contents, resolveDir: directory};
      });
      b.onLoad({filter: /\.css$/}, () => ({contents: '', loader: 'js'}));
      b.onLoad({filter: /\.vue$/}, ({path: filename}) => {
        if (filename !== consoleFile) return {contents: 'export default {};'};
        const {descriptor} = parse(fs.readFileSync(filename, 'utf8'));
        const script = compileScript(descriptor, {id: 'draft-lifecycle', genDefaultAs: '__component'});
        return {contents: `${script.content}\n__component.render=()=>null; export default __component;`, resolveDir: directory};
      });
    }}],
  }).then(result => result.outputFiles[0].text);
  return compiledSetup;
}

const renderer = vue.createRenderer({
  createComment: text => ({text}), createText: text => ({text}), createElement: type => ({type}),
  insert(node, parent) {node.parent = parent; parent.child = node;},
  remove(node) {if (node.parent) node.parent.child = null;},
  parentNode: node => node.parent, nextSibling: () => null,
  setText() {}, setElementText() {}, patchProp() {},
});
const target = extra => Object.assign(new EventTarget(), extra);
function storage() {
  const values = new Map();
  return {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value)};
}
export function draftEnvironment() {
  const timers = new Set();
  const frames = new Map();
  let frameId = 0;
  const schedule = (fn, delay) => {
    const id = setTimeout(() => {timers.delete(id); fn();}, delay);
    timers.add(id); return id;
  };
  const cancel = id => {timers.delete(id); clearTimeout(id);};
  const window = target({localStorage: storage(), sessionStorage: storage(), setTimeout: schedule, clearTimeout: cancel,
    requestAnimationFrame: fn => {frames.set(++frameId, fn); return frameId;}, cancelAnimationFrame: id => frames.delete(id),
    matchMedia: () => ({matches: false})});
  const document = target({compatMode: 'CSS1Compat', visibilityState: 'visible', hasFocus: () => true});
  const driver = createMemoryAttachmentDraftDriver();
  const snapshot = uuid => ({conversationUuid: uuid, operations: [], messages: [], frameSeq: 0, running: false, usage: {}});
  const api = {
    rathOptions: async () => ({models: [{key: 'test-model'}], primaryModel: 'test-model'}),
    conversationDefaults: async () => ({defaults: {mainModel: 'test-model'}}),
    conversationState: async uuid => snapshot(uuid), readConversationActivity: async () => ({}),
  };
  class Socket extends EventTarget {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor() {super(); this.readyState = 1;}
    close() {this.readyState = 3;}
  }
  const errors = [];
  const messages = Object.fromEntries(['error', 'warning', 'success', 'info'].map(level => [level, value => errors.push([level, value])]));
  return {window, document, driver, api, snapshot, errors, messages, confirm: {confirm: async () => true},
    globals: {window, document, WebSocket: Socket, setTimeout: schedule, clearTimeout: cancel},
    cleanup() {for (const timer of timers) clearTimeout(timer); frames.clear();}};
}

export async function mountDraftConsole(env, uuid) {
  const module = {exports: {}};
  const context = vm.createContext({module, exports: module.exports, require, __env: env, ...env.globals,
    console, performance, AbortController, File, Blob, URL, structuredClone, crypto: globalThis.crypto});
  vm.runInContext(await consoleSetupCode(), context, {filename: consoleFile});
  const component = module.exports.default;
  const container = {};
  let vnode;
  const update = conversationUuid => {
    vnode = vue.h(component, {conversationUuid, folderId: ''});
    renderer.render(vnode, container);
  };
  update(uuid);
  // Deliberately do not await any HTTP or nextTick here: the first editable
  // render must already contain the old draft.
  return {get state() {return vnode.component.setupState;}, update, unmount: () => renderer.render(null, container)};
}
export async function settleDraftConsole() {for (let i = 0; i < 40; i++) await vue.nextTick();}
export function deferred() {
  let resolve;
  const promise = new Promise(yes => {resolve = yes;});
  return {promise, resolve};
}
export const draftFile = name => new File(['draft file'], name, {type: 'text/plain'});
export const draftNames = h => Array.from(h.state.pendingAttachments, item => item.file.name);
