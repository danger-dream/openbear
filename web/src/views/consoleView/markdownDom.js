import morphdom from "morphdom";
import {prepareArtifactCards, indexArtifactCards, isArtifactSlot, syncArtifactCards, unmountArtifactCards} from "../../artifacts/artifactCards.js";

function indexImages(root, keys) {
	const occurrences = new Map();
	for (const image of root.querySelectorAll("img")) {
		// Resource + occurrence is stable while text/markup around an image grows.
		// Repeated uses of the same URL must still be separate DOM nodes.
		const resource = JSON.stringify([image.getAttribute("src"), image.getAttribute("srcset")]);
		const occurrence = occurrences.get(resource) || 0;
		occurrences.set(resource, occurrence + 1);
		keys.set(image, `image:${resource}:${occurrence}`);
	}
}

/** Patch only renderer-produced HTML; this is not an HTML sanitizer. */
export function patchMarkdownDom(root, html, {artifactCards = false, appContext} = {}) {
	// A detached div in the active document can still start image requests.
	// Parse and assemble the comparison tree in template's inert document instead.
	const template = root.ownerDocument.createElement("template");
	template.innerHTML = String(html || "");
	const target = template.content.ownerDocument.createElement("div");
	target.appendChild(template.content);
	if (artifactCards) prepareArtifactCards(target);

	const keys = new WeakMap();
	indexImages(root, keys);
	indexImages(target, keys);
	indexArtifactCards(root, keys);
	indexArtifactCards(target, keys);
	morphdom(root, target, {
		childrenOnly: true, // Vue owns the root's tag, attributes and event handlers.
		getNodeKey: (node) => keys.get(node),
		onBeforeElUpdated: (from, to) => !from.isEqualNode(to),
		// Vue owns each mounted card's contents; streaming only updates its slot.
		onBeforeElChildrenUpdated: (from) => !isArtifactSlot(from),
	});
	syncArtifactCards(root, appContext);
}

// v-html replaces all children. This directive leaves unchanged nodes (including
// in-flight images, code blocks and table scroll containers) mounted in place.
function bindingOptions(binding) {
	const value = typeof binding.value === "string" ? {html: binding.value} : (binding.value || {});
	return {...value, appContext: binding.instance?.$?.appContext};
}
export const vMarkdownHtml = {
	beforeMount(el, binding) {
		const options = bindingOptions(binding);
		patchMarkdownDom(el, options.html, options);
	},
	updated(el, binding) {
		const options = bindingOptions(binding);
		const old = typeof binding.oldValue === "string" ? {html: binding.oldValue} : (binding.oldValue || {});
		if (options.html !== old.html || options.artifactCards !== old.artifactCards) patchMarkdownDom(el, options.html, options);
	},
	beforeUnmount: unmountArtifactCards,
};
