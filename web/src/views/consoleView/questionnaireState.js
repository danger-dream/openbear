function questionId(question) {
	return String(question?.questionId ?? question?.id ?? "").trim();
}

function optionValue(option) {
	if (option && typeof option === "object") return option.value ?? option.label ?? "";
	return option ?? "";
}

export function createQuestionnaireDraft(questions = []) {
	const answers = {};
	for (const question of Array.isArray(questions) ? questions : []) {
		const id = questionId(question);
		if (!id) continue;
		answers[id] = {selectedValues: [], text: ""};
	}
	return {answers};
}

export function ensureQuestionAnswer(draft, question) {
	const id = questionId(question);
	if (!draft.answers) draft.answers = {};
	if (!draft.answers[id]) draft.answers[id] = {selectedValues: [], text: ""};
	return draft.answers[id];
}

export function toggleQuestionChoice(draft, question, value) {
	const answer = ensureQuestionAnswer(draft, question);
	const selected = Array.isArray(answer.selectedValues) ? answer.selectedValues : [];
	if (question?.multiple) {
		answer.selectedValues = selected.some((item) => Object.is(item, value))
			? selected.filter((item) => !Object.is(item, value))
			: [...selected, value];
	} else {
		answer.selectedValues = [value];
	}
	return answer;
}

export function clearQuestionChoice(draft, question) {
	ensureQuestionAnswer(draft, question).selectedValues = [];
}

export function isQuestionChoiceSelected(draft, question, option) {
	const value = optionValue(option);
	return ensureQuestionAnswer(draft, question).selectedValues.some((item) => Object.is(item, value));
}

export function validateQuestionnaire(questions = [], draft) {
	const errors = {};
	for (const question of Array.isArray(questions) ? questions : []) {
		if (!question?.required) continue;
		const id = questionId(question);
		if (!id) continue;
		const answer = ensureQuestionAnswer(draft, question);
		const hasText = Boolean(String(answer.text || "").trim());
		const hasSelection = Array.isArray(answer.selectedValues) && answer.selectedValues.length > 0;
		if (String(question?.type || "open") === "choice") {
			if (!hasSelection && !hasText) errors[id] = "请选择至少一项，或直接填写你的答案。";
		} else if (!hasText) {
			errors[id] = "请填写这一题。";
		}
	}
	return errors;
}

export function hasLongQuestionnaireBody(body) {
	const raw = String(body || "").trim();
	if (!raw) return false;
	if (raw.length >= 140) return true;
	const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
	if (lines.length >= 3) return true;
	return lines.some((line) => {
		const trimmed = line.trimStart();
		return trimmed.startsWith("#") || trimmed.startsWith("- ") || trimmed.startsWith("* ") || trimmed.startsWith("+ ")
			|| trimmed.startsWith("```") || trimmed.startsWith("|") || /^\d+\.\s/.test(trimmed);
	});
}

export function isQuestionAnswered(draft, question) {
	const answer = ensureQuestionAnswer(draft, question);
	const hasText = Boolean(String(answer.text || "").trim());
	if (String(question?.type || "open") === "choice") {
		return hasText || (Array.isArray(answer.selectedValues) && answer.selectedValues.length > 0);
	}
	return hasText;
}

export function questionnaireProgress(questions = [], draft) {
	const list = Array.isArray(questions) ? questions : [];
	let answered = 0;
	const missingQuestionIds = [];
	for (const question of list) {
		const id = questionId(question);
		const done = isQuestionAnswered(draft, question);
		if (done) answered += 1;
		else if (question?.required && id) missingQuestionIds.push(id);
	}
	return {
		total: list.length,
		answered,
		requiredMissing: missingQuestionIds.length,
		missingQuestionIds,
	};
}

export function buildQuestionnaireAnswer(questions = [], draft, cancelled = false, options = {}) {
	if (cancelled) return {cancelled: true};
	const overallText = String(options?.overallText ?? draft?.overallText ?? "");
	if (options?.intent === "feedback") {
		return {
			cancelled: false,
			decision: "feedback",
			text: overallText,
		};
	}
	const payload = {
		cancelled: false,
		answers: (Array.isArray(questions) ? questions : []).map((question) => {
			const answer = ensureQuestionAnswer(draft, question);
			return {
				questionId: questionId(question),
				selectedValues: String(question?.type || "open") === "choice" && Array.isArray(answer.selectedValues)
					? [...answer.selectedValues]
					: [],
				text: String(answer.text || ""),
			};
		}),
	};
	if (overallText.trim()) payload.text = overallText;
	return payload;
}
