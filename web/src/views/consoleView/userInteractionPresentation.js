const REDACTED_TEXT = "[敏感内容已隐藏]";
const ACTIONS = new Set(["confirm", "select", "prompt", "questionnaire"]);
const TERMINAL_OPERATION_STATUSES = new Set(["completed", "cancelled", "canceled", "failed", "partial", "interrupted"]);

function object(value) {
	return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function parseInteractionObject(value) {
	if (value && typeof value === "object" && !Array.isArray(value)) return value;
	if (typeof value !== "string" || !value.trim()) return {};
	try {
		const parsed = JSON.parse(value);
		return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

function text(value, fallback = "") {
	if (value === null || value === undefined) return fallback;
	return String(value);
}

function cleanLine(value, maximum = 120) {
	const out = text(value).replace(/\s+/g, " ").trim();
	return out.length > maximum ? `${out.slice(0, maximum - 1)}…` : out;
}

function containsRedaction(value, seen = new Set()) {
	if (typeof value === "string") return value.includes(REDACTED_TEXT);
	if (!value || typeof value !== "object" || seen.has(value)) return false;
	seen.add(value);
	return Object.values(value).some((item) => containsRedaction(item, seen));
}

function optionView(option, index = 0) {
	if (option && typeof option === "object") {
		const value = option.value ?? option.label ?? String(index);
		return {
			value: text(value),
			label: text(option.label ?? option.value ?? `选项 ${index + 1}`),
			description: text(option.description),
		};
	}
	return {value: text(option), label: text(option, `选项 ${index + 1}`), description: ""};
}

function normalizeAction(...values) {
	for (const value of values) {
		const action = text(value).trim().toLowerCase();
		if (ACTIONS.has(action)) return action;
	}
	return "confirm";
}

function normalizeInteractionStatus(summary, result, operationStatus) {
	const raw = text(summary.interactionStatus || summary.status || result.interactionStatus || result.status).trim().toLowerCase();
	if (["answered", "cancelled", "timeout", "error", "pending"].includes(raw)) return raw;
	if (raw === "canceled") return "cancelled";
	if (["timed_out", "timedout", "expired"].includes(raw)) return "timeout";
	if (["failed", "errored"].includes(raw)) return "error";
	if (result.cancelled === true) return "cancelled";
	if (Object.keys(result).length) return result.error ? "error" : "answered";
	const op = text(operationStatus).trim().toLowerCase();
	if (["cancelled", "canceled", "interrupted"].includes(op)) return "cancelled";
	if (op === "failed") return "error";
	if (TERMINAL_OPERATION_STATUSES.has(op)) return "answered";
	return "pending";
}

const ACTION_META = {
	confirm: {name: "确认", fallbackTitle: "请确认"},
	select: {name: "选择", fallbackTitle: "请选择"},
	prompt: {name: "输入", fallbackTitle: "请输入信息"},
	questionnaire: {name: "问卷", fallbackTitle: "需求问卷"},
};

function confirmPolarity(result, summary) {
	for (const source of [result, summary]) {
		if (!source || typeof source !== "object") continue;
		if (source.confirmed === true || source.choice === "confirm") return true;
		if (source.confirmed === false || source.choice === "cancel") return false;
	}
	return null;
}

function outcome(action, status, result, sensitive, summary = {}) {
	if (status === "pending") return {key: "pending", label: "等待回答", tone: "waiting"};
	if (status === "cancelled") return {key: "cancelled", label: "已取消", tone: "muted"};
	if (status === "timeout") return {key: "timeout", label: "已超时", tone: "warning"};
	if (status === "error") return {key: "error", label: "出错", tone: "danger"};
	if (action === "confirm") {
		const confirmed = confirmPolarity(result, summary);
		if (confirmed === true) return {key: "confirmed", label: "已确认", tone: "success"};
		if (confirmed === false) return {key: "rejected", label: "已拒绝", tone: "muted"};
		return {key: "answered", label: "已回答", tone: "success"};
	}
	if (action === "prompt") {
		if (sensitive) return {key: "sensitive", label: "已回答", tone: "success"};
		return text(result.value).length
			? {key: "answered", label: "已回答", tone: "success"}
			: {key: "empty", label: "空回答", tone: "muted"};
	}
	return {key: "answered", label: "已回答", tone: "success"};
}

function introFor(action, outcomeValue) {
	if (outcomeValue.key === "pending") return `正在等待用户完成${ACTION_META[action].name}`;
	if (outcomeValue.key === "timeout") return `本次${ACTION_META[action].name}未在限定时间内完成`;
	if (["cancelled", "rejected"].includes(outcomeValue.key)) return `用户未继续本次${ACTION_META[action].name}`;
	if (outcomeValue.key === "error") return `本次${ACTION_META[action].name}未能完成`;
	return `用户已完成本次${ACTION_META[action].name}`;
}

function selectedValues(result) {
	const values = Array.isArray(result.selectedValues) ? result.selectedValues : [];
	return new Set(values.map((item) => text(item)));
}

function selectPresentation(args, result, sensitive) {
	if (sensitive) return [];
	const selected = selectedValues(result);
	const selectedIndexes = new Set((Array.isArray(result.selectedIndexes) ? result.selectedIndexes : []).map(Number));
	const labels = new Set((Array.isArray(result.selectedLabels) ? result.selectedLabels : []).map(text));
	return (Array.isArray(args.options) ? args.options : []).map((item, index) => {
		const option = optionView(item, index);
		return {...option, selected: selected.has(option.value) || selectedIndexes.has(index) || labels.has(option.label)};
	});
}

function questionnairePresentation(args, result, sensitive) {
	const rawAnswers = Array.isArray(result.answers) ? result.answers : [];
	const answers = new Map(rawAnswers.map((answer) => [text(answer?.questionId ?? answer?.id), object(answer)]));
	return (Array.isArray(args.questions) ? args.questions : []).map((rawQuestion, index) => {
		const question = object(rawQuestion);
		const id = text(question.id ?? question.questionId ?? index);
		const answer = answers.get(id) || {};
		const selected = sensitive ? new Set() : new Set((Array.isArray(answer.selectedValues) ? answer.selectedValues : []).map(text));
		const selectedLabels = sensitive ? new Set() : new Set((Array.isArray(answer.selectedLabels) ? answer.selectedLabels : []).map(text));
		const recommended = new Set((Array.isArray(question.recommendation?.values) ? question.recommendation.values : []).map(text));
		const options = (Array.isArray(question.options) ? question.options : []).map((item, optionIndex) => {
			const option = optionView(item, optionIndex);
			if (sensitive) return {
				key: `option-${optionIndex}`,
				label: option.label,
				description: option.description,
				selected: false,
				recommended: false,
			};
			return {
				...option,
				selected: selected.has(option.value) || selectedLabels.has(option.label),
				recommended: recommended.has(option.value),
			};
		});
		return {
			id,
			number: index + 1,
			type: question.type === "choice" ? "choice" : "open",
			question: text(question.question || question.prompt || question.title, `问题 ${index + 1}`),
			description: text(question.description),
			required: question.required !== false,
			multiple: Boolean(question.multiple),
			options,
			recommendationReason: text(question.recommendation?.reason),
			answerText: sensitive ? "" : text(answer.text),
			answered: !sensitive && (selected.size > 0 || Boolean(text(answer.text).trim())),
		};
	});
}

/** Build a DOM-safe, read-only presentation model from typed summary/detail or legacy tool data. */
export function buildUserInteractionView(input = {}) {
	const source = object(input);
	const operation = object(source.operation);
	const payload = object(operation.payload);
	const summary = {
		...object(operation.interaction),
		...object(source.interaction || payload.interaction),
	};
	if (!summary.interactionStatus && (source.interactionStatus || payload.interactionStatus)) {
		summary.interactionStatus = source.interactionStatus || payload.interactionStatus;
	}
	const args = parseInteractionObject(source.arguments ?? payload.arguments ?? payload.args);
	const result = parseInteractionObject(source.result ?? payload.resultText ?? payload.result);
	const action = normalizeAction(summary.action, args.action, result.action);
	const sensitive = Boolean(summary.sensitive || summary.secret || args.sensitive || args.secret || containsRedaction(args) || containsRedaction(result));
	const status = normalizeInteractionStatus(summary, result, operation.status || source.operationStatus);
	const outcomeValue = outcome(action, status, result, sensitive, summary);
	const title = cleanLine(summary.title || args.title || ACTION_META[action].fallbackTitle, 100);
	return {
		kind: "user_interaction",
		action,
		actionName: ACTION_META[action].name,
		title,
		body: cleanLine(args.body, 2000),
		intro: introFor(action, outcomeValue),
		status,
		statusKey: outcomeValue.key,
		statusLabel: outcomeValue.label,
		statusTone: outcomeValue.tone,
		sensitive,
		redactedText: REDACTED_TEXT,
		confirmed: !sensitive && outcomeValue.key === "confirmed",
		promptValue: action === "prompt" && !sensitive ? text(result.value) : "",
		options: action === "select" ? selectPresentation(args, result, sensitive) : [],
		questions: action === "questionnaire" ? questionnairePresentation(args, result, sensitive) : [],
		malformed: !Object.keys(args).length && !Object.keys(summary).length,
	};
}

export function isUserInteractionOperation(operation = {}) {
	const op = object(operation);
	const payload = object(op.payload);
	const opType = text(op.opType || op.op_type).trim().toLowerCase();
	const toolName = text(payload.toolName || payload.name || payload.rootToolName).trim();
	return opType === "user_interaction" || (opType === "tool" && toolName === "UserInteraction");
}

export function isUserInteractionEvent(event = {}) {
	if (event?.kind === "user_interaction") return true;
	if (isUserInteractionOperation(event?.operation)) return true;
	const calls = Array.isArray(event?.calls) ? event.calls : [];
	return text(event?.toolName || calls[0]?.name).trim() === "UserInteraction";
}

export function userInteractionEventInput(event = {}, operationOverride = null) {
	const calls = Array.isArray(event?.calls) ? event.calls : [];
	const result = event?.result || (Array.isArray(event?.results) ? event.results[0] : null);
	const operation = operationOverride || event?.operation || {};
	const payload = object(operation?.payload);
	return {
		operation,
		interaction: payload.interaction || operation?.interaction || event?.interaction
			|| (isUserInteractionOperation(operation) ? payload : undefined),
		interactionStatus: payload.interactionStatus || operation?.interactionStatus || event?.interactionStatus,
		arguments: payload.arguments ?? payload.args ?? calls[0]?.arguments,
		result: payload.resultText ?? payload.result ?? result?.content ?? result,
	};
}
