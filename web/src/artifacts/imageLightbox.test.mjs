import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {register} from "node:module";
import {compileScript, parse} from "@vue/compiler-sfc";
import "../domTestEnv.mjs";
import {mount} from "@vue/test-utils";

const filename = new URL("./ImageLightbox.vue", import.meta.url);
const code = compileScript(parse(readFileSync(filename, "utf8"), {filename: "ImageLightbox.vue"}).descriptor, {id: "ImageLightbox.vue", inlineTemplate: true}).content;
register(`data:text/javascript,${encodeURIComponent(`
 let src;
 export function initialize(v) { src = v; }
 export function load(url, context, next) {
  if (url.endsWith('/ImageLightbox.vue')) return {format: 'module', source: src, shortCircuit: true};
  return next(url, context);
 }
`)}`, import.meta.url, {data: code});

const {default: ImageLightbox} = await import("./ImageLightbox.vue");

test("ImageLightbox keeps raw filename when URL contains an unescaped percent sign", () => {
	const wrapper = mount(ImageLightbox, {
		props: {
			urlList: ["https://example.com/uploads/100%coverage.png?token=1"],
			teleported: false,
		},
	});
	const link = wrapper.find("a[download]");
	assert.equal(link.exists(), true);
	assert.equal(link.attributes("download"), "100%coverage.png");
	wrapper.unmount();
});

// 11-12 review: lifting a pinch's fingers one at a time left the last as a plain touch, so the
// image stayed small, or dragged past its edge, instead of springing back.
test("ImageLightbox springs back when a pinch ends one finger at a time", async () => {
	const wrapper = mount(ImageLightbox, {props: {urlList: ["https://example.com/a.png"], teleported: false}});
	await wrapper.find("img").trigger("load");
	const stage = wrapper.find(".image-lightbox-stage");
	const at = (x, y) => ({clientX: x, clientY: y});
	const scaleOf = () => Number(/scale\(([\d.]+)\)/.exec(wrapper.find("img").attributes("style"))[1]);
	const pinch = async (from, to, lift) => {
		await stage.trigger("touchstart", {touches: [at(200 - from, 400), at(200 + from, 400)]});
		await stage.trigger("touchmove", {touches: [at(200 - to, 400), at(200 + to, 400)]});
		await stage.trigger("touchend", {touches: [at(200 + to, 400)], changedTouches: [at(200 - to, 400)]});
		await stage.trigger("touchmove", {touches: [at(200 + to + lift, 400 + lift)]});
		await stage.trigger("touchend", {touches: [], changedTouches: [at(200 + to + lift, 400 + lift)]});
	};
	await pinch(100, 40, 0);
	assert.equal(scaleOf(), 1, "pinched small, lifted without moving: back to 1x, not taken for a tap");
	await pinch(100, 40, 120);
	assert.equal(scaleOf(), 1, "pinched small, the last finger dragged: back to 1x, no swipe or dismiss");
	assert.equal(wrapper.emitted("close"), undefined);
	await pinch(40, 120, 0);
	assert.ok(scaleOf() > 2.5, "pinched up stays zoomed");
	wrapper.unmount();
});
