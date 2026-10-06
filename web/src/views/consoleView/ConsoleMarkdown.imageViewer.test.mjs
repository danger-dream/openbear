import test, {after} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {register} from "node:module";
import {compileScript, compileStyle, compileTemplate, parse} from "@vue/compiler-sfc";
import {createRenderer, h, nextTick} from "vue";

const filename = "ConsoleMarkdown.vue";
const {descriptor} = parse(readFileSync(new URL(filename, import.meta.url), "utf8"), {filename});
// Run the real compiled Markdown SFC and installed Element Plus viewer. Only
// browser-only DOM patching, focus/scroll locking and CSS animation are replaced
// in this host renderer; viewer rendering, transforms and listeners stay real.
register(`data:text/javascript,${encodeURIComponent(`
 import {readFileSync} from 'node:fs';
 let component;
 export function initialize(source) { component = source; }
 export function load(url, context, next) {
  if (url.endsWith('/ConsoleMarkdown.vue')) return {format:'module',source:component,shortCircuit:true};
  if (url.endsWith('/markdownDom.js')) return {format:'module',source:'export const vMarkdownHtml = {};',shortCircuit:true};
  if (url.endsWith('.css')) return {format:'module',source:'export {};',shortCircuit:true};
  if (url.endsWith('/use-lockscreen/index.mjs')) return {format:'module',source:'export const useLockscreen = () => {};',shortCircuit:true};
  if (url.endsWith('/focus-trap/index.mjs')) return {format:'module',source:'export default {inheritAttrs:false,setup(_, {slots}) { return () => slots.default?.(); }};',shortCircuit:true};
  if (url.endsWith('/image-viewer.vue_vue_type_script_setup_true_lang.mjs')) {
   const source = readFileSync(new URL(url), 'utf8').replace('Teleport, Transition,', 'Teleport,') + '\\nconst Transition = {inheritAttrs:false,setup(_, {slots}) { return () => slots.default?.(); }};';
   return {format:'module',source,shortCircuit:true};
  }
  return next(url, context);
 }
`)}`, {parentURL: import.meta.url, data: compileScript(descriptor, {id: filename, inlineTemplate: true}).content});
const Component = (await import("./ConsoleMarkdown.vue")).default;
const Viewer = (await import("element-plus/es/components/image-viewer/index.mjs")).ElImageViewer;
const {ZINDEX_INJECTION_KEY} = await import("element-plus/es/hooks/use-z-index/index.mjs");

const original = Object.fromEntries(["window", "document", "location"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
after(() => { for (const [key, value] of Object.entries(original)) { if (value) Object.defineProperty(globalThis, key, value); else delete globalThis[key]; } });
const win = new EventTarget();
win.navigator = {userAgent: "Macintosh Safari", standalone: false};
win.matchMedia = () => ({matches: win.navigator.standalone === true});
const doc = new EventTarget();
Object.defineProperty(globalThis, "window", {configurable: true, value: win});
Object.defineProperty(globalThis, "document", {configurable: true, value: doc});
Object.defineProperty(globalThis, "location", {configurable: true, value: {origin: "https://openbear.test"}});
const node = (type, text = "") => Object.assign(new EventTarget(), {type, text, props: {}, children: [], parent: null, complete: true});
const body = node("body");
const renderer = createRenderer({
 querySelector: target => target === "body" ? body : null,
 createElement: type => node(type), createText: text => node("#text", text), createComment: text => node("#comment", text),
 setText(n, value) { n.text = value; }, setElementText(n, value) { n.text = value; n.children = []; },
 patchProp(n, key, old, value) { n.props[key] = value; },
 insert(n, parent, anchor = null) { if (n.parent) this.remove(n); n.parent = parent; const i = parent.children.indexOf(anchor); parent.children.splice(i < 0 ? parent.children.length : i, 0, n); },
 remove(n) { const i = n.parent?.children.indexOf(n) ?? -1; if (i >= 0) n.parent.children.splice(i, 1); n.parent = null; },
 parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
});
const walk = n => [n, ...n.children.flatMap(walk)];
const find = (n, cls) => walk(n).find(item => String(item.props.class || "").split(/\s+/).includes(cls));
const tick = async () => { await nextTick(); await nextTick(); };
const path = id => `/api/conversations/39a541d4-4d9c-4a58-87d6-1b276779954a/artifacts/${id}/content`;
const first = path("adfead18-e6d1-40df-8475-391e1245aa0d");
const second = path("bdfead18-e6d1-40df-8475-391e1245aa0d");
const remote = `https://remote.test${first}`;
const urls = [first, second, remote].map(src => new URL(src, location.origin).href);
const event = (extra = {}) => ({defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extra});
function mount(t) {
 const root = node("root"), app = renderer.createApp({render: () => h(Component, {text: "正文图片", artifactCards: false})});
 app.component("ElImageViewer", Viewer); app.provide(ZINDEX_INJECTION_KEY, {current: 0});
 app.mount(root); t.after(() => { app.unmount(); win.navigator.standalone = false; });
 const markdown = find(root, "bear-md");
 const images = urls.map(src => ({src, currentSrc: src}));
 markdown.querySelectorAll = () => images;
 async function open(index) {
  const clicked = images[index];
  const e = event({target: {closest: selector => selector === "img" ? clicked : null}, currentTarget: markdown});
  markdown.props.onClick(e); await tick();
  assert.equal(e.defaultPrevented, true); assert.equal(e.stopped, true);
  find(root, "el-image-viewer__img").props.onLoad(); await tick();
 }
 return {root, markdown, open};
}
const download = root => find(root, "markdown-image-download");
const image = root => find(root, "el-image-viewer__img");
async function next(root) { find(root, "el-image-viewer__next").props.onClick(); await tick(); image(root).props.onLoad(); await tick(); }
async function key(code) { const e = new Event("keydown"); Object.defineProperty(e, "code", {value: code}); doc.dispatchEvent(e); await tick(); }

test("real template/styles compile and download has a 44px touch target with focus styling", () => {
 assert.deepEqual(compileTemplate({source: descriptor.template.content, filename, id: filename}).errors, []);
 for (const style of descriptor.styles) assert.deepEqual(compileStyle({source: style.content, filename, id: filename}).errors, []);
 assert.match(descriptor.styles[0].content, /min-height: 44px/);
 assert.match(descriptor.styles[0].content, /touch-action: manipulation/);
 assert.match(descriptor.styles[0].content, /:focus-visible/);
 assert.doesNotMatch(descriptor.styles[0].content.split(".bear-md")[0], /position:\s*absolute|bottom:\s*86px/);
});

test("toolbar layout stays within desktop and mobile viewports using actual viewer padding", () => {
 const native = readFileSync(new URL("../../../node_modules/element-plus/theme-chalk/el-image-viewer.css", import.meta.url), "utf8");
 const outer = Number(native.match(/\.el-image-viewer__actions\{[^}]*padding:0 (\d+)px/)[1]) * 2;
 const inner = Number(native.match(/\.el-image-viewer__actions__inner\{[^}]*padding:0 (\d+)px/)[1]) * 2;
 const styles = descriptor.styles[0].content;
 const caps = [...styles.matchAll(/width: min\((\d+)px, calc\(100vw - (\d+)px\)\)/g)].map(m => [Number(m[1]), Number(m[2])]);
 assert.deepEqual(caps, [[220, 74], [264, 74]]);
 for (const viewport of [280, 320, 360, 375, 390, 768, 1280]) {
  for (const [cap, reserve] of caps) {
   const total = Math.min(cap, viewport - reserve) + outer + inner;
   assert.ok(total <= viewport - 16, `toolbar ${total}px must fit ${viewport}px viewport`);
  }
 }
 assert.match(styles, /flex: 1/); assert.match(styles, /min-width: 0/);
});

test("multi-image navigation downloads current original, hides remote entry, wraps and reopens at clicked index", async t => {
 const {root, open} = mount(t);
 await open(1);
 assert.equal(image(root).props.src, urls[1]);
 assert.equal(download(root).props.href, `${second}?download=1`);
 assert.equal(download(root).props.download, "");
 assert.equal(download(root).props["aria-label"], "下载原图");
 assert.equal(walk(root).filter(n => n.type === "img").length, 1);
 const native = event(); download(root).props.onClick(native);
 assert.equal(native.stopped, true); assert.equal(native.defaultPrevented, false);
 assert.ok(image(root), "download does not close preview");
 await next(root);
 assert.equal(image(root).props.src, remote); assert.equal(download(root), undefined);
 await next(root);
 assert.equal(download(root).props.href, `${first}?download=1`);
 find(root, "el-image-viewer__close").props.onClick(); await tick();
 assert.equal(image(root), undefined);
 await open(2); assert.equal(download(root), undefined);
 find(root, "el-image-viewer__prev").props.onClick(); await tick();
 assert.equal(download(root).props.href, `${second}?download=1`);
});

test("native zoom, rotation, mode toggle, keyboard switching and Escape survive the added download", async t => {
 const {root, open} = mount(t); await open(0);
 const toolbar = find(root, "markdown-image-toolbar");
 const actions = toolbar.children.filter(n => n.type === "button");
 assert.equal(actions.length, 5, "all five native actions are wired through the toolbar slot");
 assert.ok(walk(find(root, "el-image-viewer__actions__inner")).includes(download(root)), "download belongs to the same native bottom toolbar");
 assert.equal(toolbar.props.role, "toolbar");
 assert.equal(download(root).children.filter(n => n.type === "svg").length, 1);
 assert.equal(download(root).children.some(n => n.type === "span"), false, "no floating text pill");
 assert.equal(download(root).props.title, "下载原图");
 assert.deepEqual(actions.map(n => n.props["aria-label"]), ["缩小", "放大", "切换适应/原始尺寸", "向左旋转", "向右旋转"]);
 actions[1].props.onClick(event()); await tick(); assert.match(image(root).props.style.transform, /scale\(1\.2\)/);
 actions[4].props.onClick(event()); await tick(); assert.match(image(root).props.style.transform, /rotate\(90deg\)/);
 actions[3].props.onClick(event()); await tick(); assert.match(image(root).props.style.transform, /rotate\(0deg\)/);
 actions[0].props.onClick(event()); await tick(); assert.match(image(root).props.style.transform, /scale\(1\)/);
 const originalLink = download(root).props.href;
 actions[2].props.onClick(event()); await tick(); assert.equal(image(root).props.style.maxWidth, undefined);
 assert.equal(download(root).props.href, originalLink, "transforms never substitute a rendered image for the original");
 await key("Space"); assert.equal(image(root).props.style.maxWidth, "100%", "container Space still invokes native fit/original toggle");
 // Focused controls activate once with Space, not once natively plus another
 // global mode toggle. Other keys still bubble to native navigation/Escape.
 actions[1].click = () => actions[1].props.onClick(event());
 const space = event({key: " ", target: {closest: () => actions[1]}});
 toolbar.props.onKeydown(space); await tick();
 assert.equal(space.stopped, true); assert.equal(space.defaultPrevented, true);
 assert.equal(image(root).props.style.maxWidth, "100%");
 assert.match(image(root).props.style.transform, /scale\(1\.2\)/);
 actions[0].props.onClick(event()); await tick();
 await key("ArrowUp"); assert.match(image(root).props.style.transform, /scale\(1\.2\)/);
 await key("ArrowRight"); assert.equal(download(root).props.href, `${second}?download=1`);
 await key("ArrowLeft"); assert.equal(download(root).props.href, `${first}?download=1`);
 await key("Escape"); assert.equal(image(root), undefined);
});

test("iPhone and Android standalone tap reuse artifact save flow for the currently viewed image", async t => {
 const {root, open} = mount(t); await open(0); await next(root);
 const saved = [], listener = e => saved.push(e.detail.href);
 win.addEventListener("openbear:download-artifact", listener); t.after(() => win.removeEventListener("openbear:download-artifact", listener));
 for (const userAgent of ["iPhone", "Android"]) {
  win.navigator = {userAgent, standalone: true};
  const tapped = event(); download(root).props.onClick(tapped);
  assert.equal(tapped.stopped, true); assert.equal(tapped.defaultPrevented, true);
  assert.equal(saved.at(-1), second); assert.ok(image(root));
 }
 assert.deepEqual(saved, [second, second]);
});
