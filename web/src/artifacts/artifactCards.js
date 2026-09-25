import {createVNode, render} from "vue";
import ArtifactCard from "./ArtifactCard.vue";
import ArtifactImagePath from "./ArtifactImagePath.vue";
import {artifactFromUrl} from "./artifactFiles.js";

const mountedRoots = new WeakMap();
const SLOT = "[data-artifact-slot]";
const IMAGE_PATH_SLOT = "[data-artifact-image-path]";

// Split phrasing ancestors rather than inserting a block inside <p>/<strong>/<h2>.
// List items and blockquotes remain the card's container, preserving their order.
function liftCard(slot, target) {
	while (slot.parentElement !== target && !slot.parentElement.matches("li, blockquote, div")) {
		const parent = slot.parentElement;
		const before = parent.cloneNode(false), after = parent.cloneNode(false);
		while (parent.firstChild !== slot) before.appendChild(parent.firstChild);
		while (slot.nextSibling) after.appendChild(slot.nextSibling);
		after.removeAttribute("id");
		const meaningful = node => node.textContent.trim() || node.querySelector("img, br, input, .md-live-caret");
		parent.replaceWith(...(meaningful(before) ? [before] : []), slot, ...(meaningful(after) ? [after] : []));
	}
}

export function prepareArtifactCards(target) {
	const occurrences = new Map();
	for (const link of Array.from(target.querySelectorAll("a[href]"))) {
		// Keep tables, examples, linked images and explicit download links intact.
		if (link.closest("pre, code, table") || link.querySelector("img") || link.hasAttribute("download")) continue;
		const identity = artifactFromUrl(link.getAttribute("href"));
		if (!identity || identity.download) continue;
		const occurrence = occurrences.get(identity.key) || 0;
		occurrences.set(identity.key, occurrence + 1);
		const slot = target.ownerDocument.createElement("div");
		slot.className = "md-artifact-slot";
		slot.dataset.artifactSlot = `${identity.key}:${occurrence}`;
		slot.dataset.artifactHref = identity.contentUrl;
		slot.dataset.artifactLabel = link.textContent.trim();
		// Preserve the existing compact layout of emoji + standalone file links.
		const paragraph = link.closest("p");
		if (paragraph && !paragraph.querySelector("img") && paragraph.querySelectorAll("a").length === 1) {
			const rest = paragraph.cloneNode(true);
			rest.querySelector("a").remove();
			for (const caret of rest.querySelectorAll(".md-live-caret")) caret.remove();
			if (/^[\s\p{Extended_Pictographic}\uFE0F\u200D]*$/u.test(rest.textContent)) {
				paragraph.replaceWith(slot);
				continue;
			}
		}
		link.replaceWith(slot);
		liftCard(slot, target);
	}
	const imageOccurrences = new Map();
	for (const image of Array.from(target.querySelectorAll("img[src]"))) {
		// Linked images and tables retain their original markup and interactions.
		if (image.closest("a, pre, code, table")) continue;
		const identity = artifactFromUrl(image.getAttribute("src"));
		if (!identity) continue;
		const occurrence = imageOccurrences.get(identity.key) || 0;
		imageOccurrences.set(identity.key, occurrence + 1);
		const slot = target.ownerDocument.createElement("span");
		slot.dataset.artifactImagePath = `${identity.key}:${occurrence}`;
		slot.dataset.artifactHref = identity.contentUrl;
		image.after(slot);
	}
}

export function indexArtifactCards(root, keys) {
	for (const slot of root.querySelectorAll(SLOT)) keys.set(slot, `artifact:${slot.dataset.artifactSlot}`);
	for (const slot of root.querySelectorAll(IMAGE_PATH_SLOT)) keys.set(slot, `artifact-image:${slot.dataset.artifactImagePath}`);
}
export function isArtifactSlot(node) { return node.nodeType === 1 && (node.hasAttribute("data-artifact-slot") || node.hasAttribute("data-artifact-image-path")); }

export function syncArtifactCards(root, appContext) {
	const mounted = mountedRoots.get(root) || new Map();
	const slots = new Set(root.querySelectorAll(`${SLOT}, ${IMAGE_PATH_SLOT}`));
	for (const slot of mounted.keys()) if (!slots.has(slot)) { render(null, slot); mounted.delete(slot); }
	for (const slot of slots) {
		const href = slot.dataset.artifactHref, label = slot.dataset.artifactLabel;
		const image = slot.hasAttribute("data-artifact-image-path");
		const previous = mounted.get(slot);
		if (previous?.href === href && previous?.label === label && previous?.image === image) continue;
		const vnode = image ? createVNode(ArtifactImagePath, {href}) : createVNode(ArtifactCard, {href, label});
		if (appContext) vnode.appContext = appContext;
		render(vnode, slot);
		mounted.set(slot, {href, label, image});
	}
	mountedRoots.set(root, mounted);
}
export function unmountArtifactCards(root) {
	for (const slot of (mountedRoots.get(root) || new Map()).keys()) render(null, slot);
	mountedRoots.delete(root);
}
