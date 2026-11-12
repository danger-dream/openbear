// Extensions chosen for a conversation that exists only as a local draft. The first send
// creates the conversation with them; an untouched draft takes its folder's default.
const drafts = new Map();
const forgotten = new Set();

// A discarded draft may still be displayed under the reused local:new identity.
export function onExtensionDraftForgotten(listener) {
	forgotten.add(listener);
	return () => forgotten.delete(listener);
}

export function extensionDraft(localUuid) {
	return drafts.get(String(localUuid || "")) || null;
}

export function setExtensionDraft(localUuid, value) {
	drafts.set(String(localUuid || ""), {enabled: [...value.enabled], autoAllow: Boolean(value.autoAllow)});
}

export function forgetExtensionDraft(localUuid) {
	const key = String(localUuid || "");
	drafts.delete(key);
	for (const listener of forgotten) listener(key);
}
