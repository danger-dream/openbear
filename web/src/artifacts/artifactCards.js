import {createVNode, render} from "vue";
import ArtifactCard from "./ArtifactCard.vue";
import {artifactFromUrl} from "./artifactFiles.js";

const mountedRoots = new WeakMap();
const SLOT = "[data-artifact-slot]";

export function prepareArtifactCards(target) {
	const occurrences = new Map();
	for (const paragraph of Array.from(target.children)) {
		// Only standalone paragraphs become cards. Sentences, tables, lists,
		// blockquotes, code examples and linked/embedded images keep their layout.
		if (paragraph.tagName !== "P" || paragraph.querySelector("img")) continue;
		const links = paragraph.querySelectorAll("a[href]");
		if (links.length !== 1) continue;
		const link = links[0], identity = artifactFromUrl(link.getAttribute("href"));
		if (!identity) continue;
		const rest = paragraph.cloneNode(true);
		rest.querySelector("a").remove();
		for (const caret of rest.querySelectorAll(".md-live-caret")) caret.remove();
		if (!/^[\s\p{Extended_Pictographic}\uFE0F\u200D]*$/u.test(rest.textContent)) continue;
		const occurrence = occurrences.get(identity.key) || 0;
		occurrences.set(identity.key, occurrence + 1);
		const slot = target.ownerDocument.createElement("div");
		slot.className = "md-artifact-slot";
		slot.dataset.artifactSlot = `${identity.key}:${occurrence}`;
		slot.dataset.artifactHref = identity.contentUrl;
		slot.dataset.artifactLabel = link.textContent.trim();
		paragraph.replaceWith(slot);
	}
}

export function indexArtifactCards(root, keys) {
	for (const slot of root.querySelectorAll(SLOT)) keys.set(slot, `artifact:${slot.dataset.artifactSlot}`);
}
export function isArtifactSlot(node) { return node.nodeType === 1 && node.hasAttribute("data-artifact-slot"); }

export function syncArtifactCards(root, appContext) {
	const mounted = mountedRoots.get(root) || new Map();
	const slots = new Set(root.querySelectorAll(SLOT));
	for (const slot of mounted.keys()) if (!slots.has(slot)) { render(null, slot); mounted.delete(slot); }
	for (const slot of slots) {
		const href = slot.dataset.artifactHref, label = slot.dataset.artifactLabel;
		const previous = mounted.get(slot);
		if (previous?.href === href && previous?.label === label) continue;
		const vnode = createVNode(ArtifactCard, {href, label});
		if (appContext) vnode.appContext = appContext;
		render(vnode, slot);
		mounted.set(slot, {href, label});
	}
	mountedRoots.set(root, mounted);
}
export function unmountArtifactCards(root) {
	for (const slot of (mountedRoots.get(root) || new Map()).keys()) render(null, slot);
	mountedRoots.delete(root);
}
