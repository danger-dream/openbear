function legacyCopyText(text) {
	if (typeof document === "undefined" || !document.body) throw new Error("clipboard_unavailable");
	const activeElement = document.activeElement;
	const area = document.createElement("textarea");
	area.value = text;
	area.setAttribute("readonly", "");
	area.setAttribute("aria-hidden", "true");
	area.style.position = "fixed";
	area.style.left = "-9999px";
	area.style.top = "0";
	area.style.opacity = "0";
	document.body.appendChild(area);
	try {
		area.focus();
		area.select();
		area.setSelectionRange(0, area.value.length);
		if (!document.execCommand("copy")) throw new Error("copy_command_failed");
	} finally {
		area.remove();
		activeElement?.focus?.({preventScroll: true});
	}
}

export async function copyTextToClipboard(value) {
	const text = String(value ?? "");
	if (!text) return false;
	if (typeof navigator !== "undefined" && navigator.clipboard?.writeText && globalThis.isSecureContext) {
		try {
			await navigator.clipboard.writeText(text);
			return true;
		} catch {
			// Permission policies can reject the modern API even in a secure context.
		}
	}
	legacyCopyText(text);
	return true;
}
