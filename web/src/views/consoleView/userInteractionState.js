function actionOf(item) {
	const action = String(item?.action || "confirm").trim().toLowerCase();
	return ["confirm", "select", "prompt", "questionnaire"].includes(action) ? action : "confirm";
}

function optionLabel(option) {
	if (option && typeof option === "object") return String(option.label ?? option.text ?? option.value ?? "");
	return String(option ?? "");
}

export function interactionOptionValue(option) {
	if (option && typeof option === "object") return String(option.value ?? option.label ?? option.text ?? "");
	return String(option ?? "");
}

function normalizedDefaultIndexes(item) {
	const options = Array.isArray(item?.options) ? item.options : [];
	const explicit = Array.isArray(item?.defaultIndexes) ? item.defaultIndexes : [];
	const indexes = explicit
		.map((value) => Number(value))
		.filter((value, index, values) => Number.isInteger(value) && value >= 0 && value < options.length && values.indexOf(value) === index);
	const values = new Set(Array.isArray(item?.defaultValues) ? item.defaultValues.map(String) : []);
	if (values.size) {
		options.forEach((option, index) => {
			if (values.has(interactionOptionValue(option)) || values.has(optionLabel(option))) indexes.push(index);
		});
	}
	const unique = [...new Set(indexes)];
	return item?.multiple ? unique : unique.slice(0, 1);
}

/** Create an isolated editable draft. Recommendations are deliberately ignored. */
export function createInteractionDraft(item = {}) {
	return {
		selectedIndexes: actionOf(item) === "select" ? normalizedDefaultIndexes(item) : [],
		value: actionOf(item) === "prompt" ? String(item?.defaultValue ?? "") : "",
		text: "",
	};
}

export function toggleInteractionOption(item, draft, index) {
	const selected = Array.isArray(draft?.selectedIndexes) ? draft.selectedIndexes : [];
	if (item?.multiple) {
		draft.selectedIndexes = selected.includes(index)
			? selected.filter((value) => value !== index)
			: [...selected, index];
	} else {
		draft.selectedIndexes = [index];
	}
	return draft;
}

export function clearInteractionSelection(draft) {
	if (draft) draft.selectedIndexes = [];
	return draft;
}

export function hasMeaningfulInteractionText(value) {
	return Boolean(String(value ?? "").trim());
}

export function hasSubmittedInteractionText(value) {
	return hasMeaningfulInteractionText(value);
}

export function interactionPrimaryActionLabel(item, draft, submitting = false) {
	if (submitting) return "提交中…";
	const action = actionOf(item);
	if (action === "confirm" && hasSubmittedInteractionText(draft?.text)) return "提交意见（不执行原操作）";
	if (action === "select" && item?.requiresAuthorization && hasSubmittedInteractionText(draft?.text)) {
		return "提交意见（不执行原操作）";
	}
	if (item?.confirmText) return String(item.confirmText);
	if (action === "confirm") return "确认执行";
	return "提交回答";
}

export function interactionRejectActionLabel(draft) {
	return hasSubmittedInteractionText(draft?.text) ? "拒绝并提交说明（不执行）" : "拒绝";
}

/**
 * Validate only what the Web client can establish. Whitespace is used only to
 * determine emptiness; the draft itself is never normalized.
 */
export function validateInteractionDraft(item, draft) {
	if (actionOf(item) !== "select") return "";
	const hasSelection = Array.isArray(draft?.selectedIndexes) && draft.selectedIndexes.length > 0;
	return hasSelection || hasMeaningfulInteractionText(draft?.text)
		? ""
		: "请选择至少一项，或直接填写自己的答案。";
}

export function buildInteractionAnswer(item, draft = {}, intent = "submit") {
	if (intent === "cancel") return {cancelled: true};
	const action = actionOf(item);
	if (action === "select") {
		const options = Array.isArray(item?.options) ? item.options : [];
		const indexes = (Array.isArray(draft.selectedIndexes) ? draft.selectedIndexes : [])
			.filter((index) => Number.isInteger(index) && index >= 0 && index < options.length);
		return {
			cancelled: false,
			selectedIndexes: [...indexes],
			selectedValues: indexes.map((index) => interactionOptionValue(options[index])),
			text: String(draft.text ?? ""),
		};
	}
	if (action === "prompt") {
		return {cancelled: false, value: String(draft.value ?? "")};
	}
	if (action === "confirm") {
		const selectedDecision = intent === "reject" ? "reject" : (intent === "feedback" ? "feedback" : "confirm");
		const originalText = String(draft.text ?? "");
		// Whitespace is only used to decide emptiness; meaningful original text is
		// preserved and can never grant authority.
		const decision = hasSubmittedInteractionText(originalText) ? "feedback" : selectedDecision;
		return {
			cancelled: false,
			decision,
			selectedDecision,
			text: originalText,
			confirmed: decision === "confirm",
		};
	}
	return {cancelled: false};
}

export function interactionRevision(item) {
	const revision = Number(item?.revision);
	return Number.isFinite(revision) ? revision : 1;
}

export function interactionExpiresAtMs(item) {
	const expiresAtMs = Number(item?.expiresAtMs);
	return Number.isFinite(expiresAtMs) && expiresAtMs > 0 ? expiresAtMs : 0;
}

export function isInteractionExpired(item, nowMs = Date.now()) {
	const expiresAtMs = interactionExpiresAtMs(item);
	return Boolean(expiresAtMs && expiresAtMs <= nowMs);
}

export function interactionExpiryLabel(item, nowMs = Date.now()) {
	const expiresAtMs = interactionExpiresAtMs(item);
	if (!expiresAtMs) return "";
	const remainingSeconds = Math.ceil((expiresAtMs - nowMs) / 1000);
	if (remainingSeconds <= 0) return "已过期";
	if (remainingSeconds < 60) return `剩余 ${remainingSeconds} 秒`;
	const minutes = Math.floor(remainingSeconds / 60);
	const seconds = remainingSeconds % 60;
	if (minutes < 60) return `剩余 ${minutes} 分${seconds ? ` ${seconds} 秒` : ""}`;
	const hours = Math.floor(minutes / 60);
	return `剩余 ${hours} 小时${minutes % 60 ? ` ${minutes % 60} 分` : ""}`;
}

export function interactionErrorCode(error) {
	const data = error?.response?.data;
	const detail = data?.detail;
	return String(
		data?.error
		?? data?.code
		?? data?.message
		?? (detail && typeof detail === "object" ? (detail.error ?? detail.code ?? detail.message) : detail)
		?? "",
	).trim();
}

export function isTerminalInteractionError(error) {
	const status = Number(error?.response?.status || 0);
	const code = interactionErrorCode(error);
	if (status === 409) return ["confirmation_already_resolved", "confirmation_expired"].includes(code);
	return status === 404 && ["not_found", "confirmation_not_found"].includes(code);
}
