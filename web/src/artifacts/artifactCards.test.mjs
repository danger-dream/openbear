import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {artifactFromUrl} from "./artifactFiles.js";

const source = readFileSync(new URL("./artifactCards.js", import.meta.url), "utf8")
	.replace(/^import .*;\n/gm, "").replace(/^export /gm, "");
const rendered = [];
const context = {
	artifactFromUrl: url => artifactFromUrl(url, "https://openbear.test"),
	ArtifactCard: "file-card", ArtifactImagePath: "image-actions",
	createVNode: (component, props) => ({component, props}),
	render: (vnode, slot) => rendered.push({vnode, slot}),
};
vm.runInNewContext(`${source}\nthis.prepare = prepareArtifactCards; this.sync = syncArtifactCards; this.unmount = unmountArtifactCards;`, context);
const href = "/api/conversations/39a541d4-4d9c-4a58-87d6-1b276779954a/artifacts/adfead18-e6d1-40df-8475-391e1245aa0d/content?preview=1";
function image(src, parent = "") {
	return {getAttribute: () => src, closest: () => parent || null, after(slot) { this.slot = slot; }};
}

test("only model artifact images gain path/download actions; uploads and linked images keep their old behavior", () => {
	rendered.length = 0;
	const model = image(href), other = image("/api/uploads/123/content"), linked = image(href, "a");
	const imgs = [model, other, linked];
	const doc = {createElement: () => ({dataset: {}, hasAttribute: name => name === "data-artifact-image-path"})};
	const root = {
		ownerDocument: doc,
		querySelectorAll(selector) {
			if (selector === "a[href]") return [];
			if (selector === "img[src]") return imgs;
			return model.slot ? [model.slot] : [];
		},
	};
	context.prepare(root);
	assert.ok(model.slot);
	assert.equal(model.slot.dataset.artifactHref, href.replace("?preview=1", ""));
	assert.equal(other.slot, undefined);
	assert.equal(linked.slot, undefined);
	context.sync(root);
	assert.equal(rendered.at(-1).vnode.component, "image-actions");
	assert.equal(rendered.at(-1).vnode.props.href, model.slot.dataset.artifactHref);
	context.unmount(root);
	assert.equal(rendered.at(-1).vnode, null);
});
