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
register(`data:text/javascript,${encodeURIComponent(`
 let component;
 export function initialize(source) { component = source; }
 export function resolve(specifier, context, next) {
  if (specifier === 'element-plus') return {url:'data:text/javascript,export const ElMessage = {success(){}, error(){}};',shortCircuit:true};
  return next(specifier, context);
 }
 export function load(url, context, next) {
  if (url.endsWith('/ArtifactCard.vue')) return {format:'module', source:component,shortCircuit:true};
  return next(url, context);
 }
`)}`, {parentURL: import.meta.url, data: source});
const Component = (await import("./ArtifactCard.vue")).default;

const originals = Object.fromEntries(["location", "window", "IntersectionObserver", "navigator", "isSecureContext", "fetch"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
after(() => { for (const [key, descriptor] of Object.entries(originals)) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; } });
afterEach(clearArtifactCache);
const dispatched = [], copied = [];
let observer;
Object.defineProperty(globalThis, "location", {configurable: true, value: {origin: "https://openbear.test"}});
Object.defineProperty(globalThis, "window", {configurable: true, value: {dispatchEvent: event => { dispatched.push(event); }}});
Object.defineProperty(globalThis, "navigator", {configurable: true, value: {clipboard: {writeText: async text => { copied.push(text); }}}});
Object.defineProperty(globalThis, "isSecureContext", {configurable: true, value: true});
Object.defineProperty(globalThis, "IntersectionObserver", {configurable: true, value: class { constructor(callback) { this.callback = callback; observer = this; } observe() {} disconnect() { this.disconnected = true; } }});

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
function mount(t, meta, summary = {title: "", excerpt: ""}, linkHref = href) {
 const record = artifactRecord(identity);
 record.metadata = meta; record.summary = summary; record.checkedAt = Date.now();
 const root = node("root"), app = renderer.createApp({render: () => h(Component, {href: linkHref, label: "测试附件"})});
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

function assertCompactImage(root) {
 assert.ok(find(root, "artifact-image-link"));
 assert.equal(find(root, "artifact-card"), undefined, "all images lose the card, not just duplicates");
 assert.equal(walk(root).some(n => n.type === "img"), false, "a file link never creates another inline image");
 assert.equal(find(root, "artifact-image-open").props.href, href);
 assert.equal(find(root, "artifact-image-download").props.href, `${href}?download=1`);
 assert.equal(find(root, "artifact-image-download").props.download, "");
}
for (const [fileName, mimeType] of [["photo.PNG", "image/png"], ["photo.jpg", "image/jpeg"], ["photo.webp", "image/webp"], ["photo.gif", "image/gif"], ["photo.avif", "application/octet-stream"], ["无后缀图片", "image/png"]]) test(`image attachment ${fileName} is a compact link, never a thumbnail card`, async t => {
 const {root} = mount(t, metadata({fileName, mimeType, contentUrl: "https://unrelated.test/file"}), undefined, `${href}?filename=${encodeURIComponent(fileName)}`);
 assertCompactImage(root);
 const before = dispatched.length; assert.equal(click(find(root, "artifact-image-open")), true);
 assert.equal(dispatched.length, before + 1); assert.equal(dispatched.at(-1).detail.href, href);
 assert.equal(click(walk(root).find(n => n.type === "button")), true); await nextTick();
 assert.equal(copied.at(-1), "workspace/artifacts/report.md");
});

test("asynchronous extensionless image metadata and streaming slot refresh never bring image cards back", async t => {
 const slot = node("div"); slot.dataset = {artifactSlot: `${identity.key}:0`, artifactHref: href, artifactLabel: "无后缀链接"};
 slot.hasAttribute = () => false;
 let currentSlot = slot, renders = 0, fetches = 0;
 const root = {querySelectorAll: () => [currentSlot]};
 const context = {ArtifactCard: Component, ArtifactImagePath: {}, createVNode: h, render(vnode, container) { renders++; renderer.render(vnode, container); }};
 const source = readFileSync(new URL("./artifactCards.js", import.meta.url), "utf8").replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
 vm.runInNewContext(`${source}\nthis.sync = syncArtifactCards; this.unmount = unmountArtifactCards;`, context);
 context.sync(root); t.after(() => context.unmount(root));
 const instance = slot._vnode.component;
 assert.ok(find(slot, "artifact-card--file"), "unknown attachments retain the existing lazy placeholder");
 globalThis.fetch = async url => { fetches++; assert.equal(url, identity.metadataUrl); return Response.json({artifact: metadata({fileName: "无后缀图片", mimeType: "image/png"})}); };
 observer.callback([{isIntersecting: true}]);
 await new Promise(resolve => setImmediate(resolve)); await nextTick();
 assertCompactImage(slot); assert.equal(fetches, 1, "classification fetches metadata, never image bytes");
 const link = find(slot, "artifact-image-open");
 for (let i = 0; i < 5; i++) context.sync(root);
 assert.equal(renders, 1); assert.equal(slot._vnode.component, instance); assert.equal(find(slot, "artifact-image-open"), link);
 slot.dataset.artifactLabel = "流式更新后的图片链接"; context.sync(root); await nextTick();
 assertCompactImage(slot); assert.equal(slot._vnode.component, instance);
 currentSlot = node("div"); currentSlot.dataset = {...slot.dataset}; currentSlot.hasAttribute = () => false;
 context.sync(root); await nextTick();
 assertCompactImage(currentSlot); assert.equal(fetches, 1, "even a new slot reuses metadata and stays compact");
});

test("compact image download keeps native desktop behavior and the mobile original-file save flow", t => {
 const {root} = mount(t, metadata({fileName: "photo.png", mimeType: "image/png", workspacePath: undefined}));
 const link = find(root, "artifact-image-download");
 assertCompactImage(root); assert.equal(walk(root).some(n => n.type === "button"), false);
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
 const {root} = mount(t, metadata({fileName: `file.${ext}`, mimeType: "application/octet-stream", workspacePath: undefined}));
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

test("unavailable metadata still exposes the original download and details retry path", async t => {
 const {root, record} = mount(t, null); record.metadataError = "missing"; await nextTick();
 assert.ok(find(root, "is-unavailable")); assert.match(text(root), /暂时无法读取附件信息/);
 assert.ok(find(root, "artifact-card-download")); click(find(root, "artifact-card-open"));
 assert.equal(dispatched.at(-1).detail.href, href);
});
