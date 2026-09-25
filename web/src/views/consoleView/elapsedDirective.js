import {fmtElapsedClockMs, fmtLiveElapsedMs} from "./display.js";

const elapsedElements = new Set();
let elapsedTimer = 0;

function paintElapsed(el) {
	const state = el?._openbearElapsedState || {};
	const format = state.format === "clock" ? fmtElapsedClockMs : fmtLiveElapsedMs;
	el.textContent = state.active && state.startAt ? format(Date.now() - state.startAt) : state.fallback;
}

function ensureElapsedTicker() {
	if (elapsedTimer || !elapsedElements.size) return;
	elapsedTimer = window.setInterval(() => {
		for (const el of elapsedElements) paintElapsed(el);
	}, 250);
}

function maybeStopElapsedTicker() {
	if (elapsedElements.size || !elapsedTimer) return;
	window.clearInterval(elapsedTimer);
	elapsedTimer = 0;
}

function stopElapsed(el) {
	if (!el) return;
	elapsedElements.delete(el);
	el._openbearElapsedState = null;
	el._openbearElapsedSignature = "";
	maybeStopElapsedTicker();
}

function bindElapsed(el, options = {}) {
	const startAt = Number(options?.startAt || 0);
	const active = Boolean(options?.active);
	const fallback = String(options?.fallback || "—");
	const intervalMs = Math.max(250, Number(options?.intervalMs || 250));
	const format = options?.format === "clock" ? "clock" : "elapsed";
	const signature = `${startAt}:${active}:${fallback}:${intervalMs}:${format}`;
	if (el._openbearElapsedSignature === signature) return;
	el._openbearElapsedSignature = signature;
	elapsedElements.delete(el);
	el._openbearElapsedState = {startAt, active, fallback, intervalMs, format};
	paintElapsed(el);
	if (active && startAt) {
		elapsedElements.add(el);
		ensureElapsedTicker();
	} else {
		maybeStopElapsedTicker();
	}
}

export const vElapsed = {
	mounted(el, binding) {
		bindElapsed(el, binding.value);
	},
	updated(el, binding) {
		bindElapsed(el, binding.value);
	},
	beforeUnmount(el) {
		stopElapsed(el);
	},
};
