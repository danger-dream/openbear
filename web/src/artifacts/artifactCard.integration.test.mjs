import test, {after, afterEach} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {register} from "node:module";
import vm from "node:vm";
import {compileScript, compileStyle, compileTemplate, parse} from "@vue/compiler-sfc";
import {createRenderer, h, nextTick} from "vue";
import {artifactFromUrl, artifactRecord, clearArtifactCache} from "./artifactFiles.js";

const filename = "ArtifactCard.vue";
const {descriptor} = parse(readFileSync(new URL(filename, import.meta.url), "utf8"), {filename});
const source = compileScript(descriptor, {id: filename, inlineTemplate: true}).content;
const imagePathName = "ArtifactImagePath.vue";
const imagePathDescriptor = parse(readFileSync(new URL(imagePathName, import.meta.url), "utf8"), {filename: imagePathName}).descriptor;
const imagePathSource = compileScript(imagePathDescriptor, {id: imagePathName, inlineTemplate: true}).content;
register(`data:text/javascript,${encodeURIComponent(`
 let components;
 export function initialize(sources) { components = sources; }
 export function resolve(specifier, context, next) {
  if (specifier === 'element-plus') return {url:'data:text/javascript,export const ElMessage = {success(){}, error(){}};',shortCircuit:true};
  return next(specifier, context);
 }
 export function load(url, context, next) {
  for (const [name, source] of Object.entries(components)) if (url.endsWith('/' + name)) return {format:'module', source,shortCircuit:true};
  return next(url, context);
 }
`)}`, {parentURL: import.meta.url, data: {[filename]: source, [imagePathName]: imagePathSource}});
const Component = (await import("./ArtifactCard.vue")).default;
const ImagePath = (await import("./ArtifactImagePath.vue")).default;

const originals = Object.fromEntries(["location", "window", "IntersectionObserver", "navigator", "isSecureContext", "fetch"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
after(() => { for (const [key, descriptor] of Object.entries(originals)) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; } });
afterEach(clearArtifactCache);
const dispatched = [], copied = [];
let observer;
Object.defineProperty(globalThis, "location", {configurable: true, value: {origin: "https://openbear.test"}});
Object.defineProperty(globalThis, "window", {configurable: true, value: {dispatchEvent: event => { dispatched.push(event); }}});
Object.defineProperty(globalThis, "navigator", {configurable: true, value: {clipboard: {writeText: async text => { copied.push(text); }}}});
Object.defineProperty(globalThis, "isSecureContext", {configurable: true, value: true});
Object.defineProperty(globalThis, "IntersectionObserver", {configurable: true, value: class { constructor(callback) { this.callback = callback; observer = this; } observe(element) { assert.ok(element, 'never observe a missing root for a hidden image'); } disconnect() { this.disconnected = true; } }});

const node = (type, text = "") => ({type, text, props: {}, children: [], parent: null});
const renderer = createRenderer({
 createElement: type => node(type), createText: text => node("#text", text), createComment: text => node("#comment", text),
 setText(n, text) { n.text = text; }, setElementText(n, text) { n.text = text; n.children = []; },
 patchProp(n, key, old, value) { n.props[key] = value; },
 insert(n, parent, anchor = null) { if (n.parent) this.remove(n); n.parent = parent; const i = parent.children.indexOf(anchor); parent.children.splice(i < 0 ? parent.children.length : i, 0, n); },
 remove(n) { const i = n.parent?.children.indexOf(n) ?? -1; if (i >= 0) n.parent.children.splice(i, 1); n.parent = null; },
 parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
});
const walk = n => [n, ...n.children.flatMap(walk)];
const find = (n, cls) => walk(n).find(item => String(item.props.class || "").split(/\s+/).includes(cls));
const text = n => [n.type === "#comment" ? "" : n.text, ...n.children.map(text)].join("");
const href = "/api/conversations/39a541d4-4d9c-4a58-87d6-1b276779954a/artifacts/adfead18-e6d1-40df-8475-391e1245aa0d/content";
const identity = artifactFromUrl(href);
const metadata = extra => ({conversationUuid: identity.conversationUuid, artifactUuid: identity.artifactUuid, fileName: "report.md", mimeType: "text/markdown", sizeBytes: 2048, workspacePath: "workspace/artifacts/report.md", ...extra});
function mount(t, meta, summary = {title: "", excerpt: ""}, linkHref = href, inlineImage = false) {
 const record = artifactRecord(identity);
 record.metadata = meta; record.summary = summary; record.checkedAt = Date.now();
 const root = node("root"), app = renderer.createApp({render: () => h(Component, {href: linkHref, label: "测试附件", inlineImage})});
 app.mount(root); t.after(() => app.unmount()); return {root, record};
}
function click(target) { let stopped = false; target.props.onClick({currentTarget: target, preventDefault() {}, stopPropagation() { stopped = true; }}); return stopped; }

test("real attachment template and theme CSS compile", () => {
 assert.deepEqual(compileTemplate({source: descriptor.template.content, filename, id: filename}).errors, []);
 for (const style of descriptor.styles) assert.deepEqual(compileStyle({source: style.content, filename, id: filename}).errors, []);
});

test("Markdown shows actual document summary and preserves independent preview, copy and download", async t => {
 const {root} = mount(t, metadata(), {title: "真实文档标题", excerpt: "真实内容摘要"});
 assert.ok(find(root, "artifact-card--markdown")); assert.match(text(root), /真实文档标题.*report.md.*真实内容摘要.*Markdown.*2.0 KB.*阅读文档/);
 const open = find(root, "artifact-card-open"), download = find(root, "artifact-card-download");
 assert.equal(click(open), true); assert.equal(dispatched.at(-1).detail.href, href); assert.equal(dispatched.at(-1).detail.opener, open);
 assert.equal(download.props.href, `${href}?download=1`); assert.equal(download.props.download, "");
 const before = dispatched.length;
 assert.equal(click(find(root, "artifact-card-copy-path")), true); await nextTick();
 assert.equal(copied.at(-1), "workspace/artifacts/report.md"); assert.equal(dispatched.length, before);
});

test("real file-card download keeps desktop native click but routes iPhone home-screen tap without leaving the conversation", t => {
 const {root} = mount(t, metadata());
 const link = find(root, "artifact-card-download");
 const nav = globalThis.navigator;
 window.navigator = nav;
 const event = () => ({stopPropagation() {}, preventDefault() { this.prevented = true; }});
 try {
  const desktop = event(); link.props.onClick(desktop);
  assert.equal(desktop.prevented, undefined);
  nav.userAgent = "iPhone"; nav.standalone = true;
  const pwa = event(); link.props.onClick(pwa);
  assert.equal(pwa.prevented, true);
  assert.equal(dispatched.at(-1).type, "openbear:download-artifact");
  assert.equal(dispatched.at(-1).detail.href, href);
 } finally { delete nav.userAgent; delete nav.standalone; delete window.navigator; }
});

function assertImageRowAbsent(root) {
 assert.equal(find(root, "artifact-card"), undefined);
 assert.equal(find(root, "artifact-image-link"), undefined);
 assert.equal(walk(root).slice(1).some(n => ["a", "button", "img", "span", "div"].includes(n.type)), false, "only the duplicate attachment row is absent");
 assert.equal(text(root), "");
 assert.match(descriptor.styles[0].content, /\.md-artifact-slot:empty\s*\{\s*display: none;/, "comment-only slot has no leftover spacing");
}
async function assertImageCardUsable(root) {
 assert.ok(find(root, "artifact-card--image"));
 const open = find(root, "artifact-card-open"), download = find(root, "artifact-card-download");
 assert.equal(click(open), true); assert.equal(dispatched.at(-1).detail.href, href);
 assert.equal(download.props.href, `${href}?download=1`); assert.equal(download.props.download, "");
 assert.equal(click(find(root, "artifact-card-copy-path")), true); await nextTick();
 assert.equal(copied.at(-1), "workspace/artifacts/report.md");
}
for (const [fileName, mimeType] of [["photo.PNG", "image/png"], ["photo.jpg", "image/jpeg"], ["photo.webp", "image/webp"], ["photo.gif", "image/gif"], ["photo.avif", "application/octet-stream"], ["无后缀图片", "image/png"]]) {
 test(`link-only image ${fileName} retains preview, download and copy`, async t => {
  const {root} = mount(t, metadata({fileName, mimeType, contentUrl: "https://unrelated.test/file"}), undefined, `${href}?filename=${encodeURIComponent(fileName)}`);
  await assertImageCardUsable(root);
 });
 test(`confirmed image ${fileName} hides only with a matching inline image`, t => {
  const {root} = mount(t, metadata({fileName, mimeType}), undefined, href, true);
  assertImageRowAbsent(root);
 });
}

const cards = {artifactFromUrl, ArtifactCard: Component, ArtifactImagePath: ImagePath, createVNode: h, render: renderer.render};
const cardSource = readFileSync(new URL("./artifactCards.js", import.meta.url), "utf8").replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
vm.runInNewContext(`${cardSource}\nthis.prepare = prepareArtifactCards; this.sync = syncArtifactCards; this.unmount = unmountArtifactCards;`, cards);
// Model the renderer-produced message tree, before Vue mounts the card. Streaming
// morphdom preserves the slot and replaces its attributes from this fresh tree.
function preparedSlot(imageUrls = []) {
 const link = {textContent: "无后缀链接", getAttribute: () => href, closest: () => null, querySelector: () => null, hasAttribute: () => false,
  replaceWith(slot) { this.slot = slot; slot.parentElement = target; }};
 const images = imageUrls.map(src => ({getAttribute: () => src, closest: () => null, after() {}}));
 const target = {ownerDocument: {createElement(type) { const slot = node(type); slot.dataset = {}; slot.hasAttribute = () => false; return slot; }},
  querySelectorAll: selector => selector === "a[href]" ? [link] : images};
 cards.prepare(target); return link.slot;
}

test("async extensionless link stays usable; streaming adds/removes only the matching duplicate without remounting", async t => {
 const slot = preparedSlot();
 let currentSlot = slot, fetches = 0;
 const root = {querySelectorAll: () => [currentSlot]};
 cards.sync(root); t.after(() => cards.unmount(root));
 const instance = slot._vnode.component;
 assert.ok(find(slot, "artifact-card--file"), "unknown attachments retain the existing lazy placeholder");
 globalThis.fetch = async url => { fetches++; assert.equal(url, identity.metadataUrl); return Response.json({artifact: metadata({fileName: "无后缀图片", mimeType: "image/png"})}); };
 observer.callback([{isIntersecting: true}]);
 await new Promise(resolve => setImmediate(resolve)); await nextTick();
 await assertImageCardUsable(slot); assert.equal(fetches, 1, "classification fetches metadata, never image bytes");
 const vnode = slot._vnode;
 for (let i = 0; i < 5; i++) cards.sync(root);
 assert.equal(slot._vnode, vnode, "unchanged streaming paints do not rerender the slot");
 slot.dataset = preparedSlot([`${href}?preview=1`]).dataset; cards.sync(root); await nextTick();
 assertImageRowAbsent(slot); assert.equal(slot._vnode.component, instance);
 slot.dataset = preparedSlot([href.replace("adfead18", "bdfead18")]).dataset; cards.sync(root); await nextTick();
 await assertImageCardUsable(slot); assert.equal(slot._vnode.component, instance, "body A cannot hide independent attachment B");
 slot.dataset = preparedSlot().dataset; slot.dataset.artifactLabel = "流式更新后的图片链接"; cards.sync(root); await nextTick();
 await assertImageCardUsable(slot); assert.match(text(slot), /流式更新后的图片链接/);
 currentSlot = preparedSlot([href]); cards.sync(root); await nextTick();
 assertImageRowAbsent(currentSlot); assert.equal(fetches, 1, "a new duplicate slot reuses confirmed metadata");
 currentSlot.dataset = preparedSlot().dataset; cards.sync(root); await nextTick();
 await assertImageCardUsable(currentSlot); assert.equal(fetches, 1, "removing the body image restores the cached card");
});

for (const initiallyInline of [false, true]) test(`pending metadata uses latest streamed image presence (initially ${initiallyInline})`, async t => {
 const slot = preparedSlot(initiallyInline ? [href] : []), root = {querySelectorAll: () => [slot]};
 cards.sync(root); t.after(() => cards.unmount(root));
 let release;
 globalThis.fetch = () => new Promise(resolve => { release = resolve; });
 observer.callback([{isIntersecting: true}]);
 slot.dataset = preparedSlot(initiallyInline ? [] : [href]).dataset; cards.sync(root); await nextTick();
 assert.ok(find(slot, "artifact-card--file"), "an inline URL alone never suppresses unknown metadata");
 release(Response.json({artifact: metadata({fileName: "无后缀图片", mimeType: "image/png"})}));
 await new Promise(resolve => setImmediate(resolve)); await nextTick();
 if (initiallyInline) await assertImageCardUsable(slot); else assertImageRowAbsent(slot);
});

test("upper inline-image download/copy controls survive suppression of the entire lower attachment row", async t => {
 const {root} = mount(t, metadata({fileName: "photo.png", mimeType: "image/png"}), undefined, href, true);
 assertImageRowAbsent(root);
 const upper = node("upper"), app = renderer.createApp({render: () => h(ImagePath, {href})});
 app.mount(upper); t.after(() => app.unmount());
 assert.ok(find(upper, "artifact-image-actions"));
 const controls = walk(upper).filter(n => ["a", "button"].includes(n.type));
 assert.equal(controls.length, 2); assert.equal(text(controls[0]), "下载"); assert.equal(text(controls[1]), "复制路径");
 const link = controls[0]; assert.equal(link.props.href, `${href}?download=1`); assert.equal(link.props.download, "");
 assert.equal(click(controls[1]), true); await nextTick(); assert.equal(copied.at(-1), "workspace/artifacts/report.md");
 const nav = globalThis.navigator; window.navigator = nav;
 const event = () => ({stopPropagation() {}, preventDefault() { this.prevented = true; }});
 try {
  const desktop = event(); link.props.onClick(desktop); assert.equal(desktop.prevented, undefined);
  for (const userAgent of ["iPhone", "Android"]) {
   nav.userAgent = userAgent; nav.standalone = true;
   const tap = event(); link.props.onClick(tap); assert.equal(tap.prevented, true);
   assert.equal(dispatched.at(-1).type, "openbear:download-artifact"); assert.equal(dispatched.at(-1).detail.href, href);
  }
 } finally { delete nav.userAgent; delete nav.standalone; delete window.navigator; }
});

for (const [ext, category] of [["pdf", "pdf"], ["docx", "document"], ["xlsx", "spreadsheet"], ["pptx", "presentation"], ["zip", "archive"], ["mp3", "audio"], ["mp4", "video"], ["woff2", "font"], ["bin", "file"]]) test(`${ext} renders distinct type and honest download-only capability`, t => {
 const {root} = mount(t, metadata({fileName: `file.${ext}`, mimeType: "application/octet-stream", workspacePath: undefined}), undefined, href, true);
 assert.ok(find(root, `artifact-card--${category}`)); assert.equal(text(find(root, "artifact-card-extension")), ext.toUpperCase());
 assert.match(text(root), /下载查看/); assert.match(find(root, "artifact-card-open").props["aria-label"], /查看附件详情/);
 assert.equal(find(root, "artifact-card-excerpt"), undefined); assert.equal(find(root, "artifact-card-copy-path"), undefined);
 assert.equal(find(root, "artifact-card-download").props.href, `${href}?download=1`);
});

test("HTML has page/source affordance but does not embed executable content in the card", t => {
 const {root} = mount(t, metadata({fileName: "page.html", mimeType: "text/html"}));
 assert.ok(find(root, "artifact-card--html")); assert.match(text(root), /页面 \/ 源码/);
 assert.equal(walk(root).some(n => ["img", "iframe", "script"].includes(n.type)), false);
});

test("offscreen cards stay lazy; metadata arrival updates the real type without remounting", async t => {
 const {root} = mount(t, null);
 let calls = 0;
 globalThis.fetch = async url => { calls++; assert.equal(url, identity.metadataUrl); return Response.json({ok: true, artifact: metadata({fileName: "deck.pptx", mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation"})}); };
 assert.ok(find(root, "artifact-card--file")); assert.equal(calls, 0);
 observer.callback([{isIntersecting: true}]);
 await new Promise(resolve => setImmediate(resolve)); await nextTick();
 assert.equal(calls, 1); assert.ok(find(root, "artifact-card--presentation")); assert.equal(observer.disconnected, true);
});

test("failed asynchronous metadata keeps the unconfirmed attachment and its download visible", async t => {
 const {root} = mount(t, null, undefined, `${href}?filename=photo.png`, true);
 assert.ok(find(root, "artifact-card--file"));
 globalThis.fetch = async () => new Response("unavailable", {status: 404});
 observer.callback([{isIntersecting: true}]);
 await new Promise(resolve => setImmediate(resolve)); await nextTick();
 assert.ok(find(root, "artifact-card")); assert.ok(find(root, "is-unavailable"));
 assert.equal(find(root, "artifact-card-download").props.href, `${href}?download=1`);
 assert.ok(find(root, "artifact-card-open"), "a filename hint alone cannot delete an unknown attachment");
});

test("unavailable metadata still exposes the original download and details retry path", async t => {
 const {root, record} = mount(t, null); record.metadataError = "missing"; await nextTick();
 assert.ok(find(root, "is-unavailable")); assert.match(text(root), /暂时无法读取附件信息/);
 assert.ok(find(root, "artifact-card-download")); click(find(root, "artifact-card-open"));
 assert.equal(dispatched.at(-1).detail.href, href);
});
