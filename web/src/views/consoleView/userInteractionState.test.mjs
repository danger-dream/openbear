import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

import {
	buildInteractionAnswer,
	clearInteractionSelection,
	createInteractionDraft,
	hasSubmittedInteractionText,
	interactionExpiryLabel,
	interactionPrimaryActionLabel,
	interactionRejectActionLabel,
	interactionRevision,
	isInteractionExpired,
	isTerminalInteractionError,
	toggleInteractionOption,
	validateInteractionDraft,
} from "./userInteractionState.js";

const composerSource = readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
const consoleViewSource = readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");

const selectItem = {
	action: "select",
	options: [{label: "方案 A", value: "a"}, {label: "方案 B", value: "b"}],
	multiple: false,
};

test("select accepts text-only and preserves the exact original text", () => {
	const draft = createInteractionDraft(selectItem);
	draft.text = "  不选以上方案\n请另拟 C  ";
	assert.equal(validateInteractionDraft(selectItem, draft), "");
	assert.deepEqual(buildInteractionAnswer(selectItem, draft), {
		cancelled: false,
		selectedIndexes: [],
		selectedValues: [],
		text: "  不选以上方案\n请另拟 C  ",
	});
});

test("select submits options with original text and single choice can be cleared", () => {
	const draft = createInteractionDraft(selectItem);
	toggleInteractionOption(selectItem, draft, 0);
	draft.text = "以 A 为基础，但先不要部署。";
	assert.deepEqual(buildInteractionAnswer(selectItem, draft), {
		cancelled: false,
		selectedIndexes: [0],
		selectedValues: ["a"],
		text: "以 A 为基础，但先不要部署。",
	});
	clearInteractionSelection(draft);
	assert.deepEqual(draft.selectedIndexes, []);
	assert.match(validateInteractionDraft(selectItem, {selectedIndexes: [], text: "  "}), /请选择/);
});

test("recommendation never preselects an ordinary choice", () => {
	const draft = createInteractionDraft({...selectItem, recommendation: {values: ["a"]}});
	assert.deepEqual(draft.selectedIndexes, []);
});

test("authorization select makes its no-execution meaning explicit only when text is present", () => {
	const draft = {selectedIndexes: [0], text: "先别执行"};
	assert.equal(interactionPrimaryActionLabel({...selectItem, requiresAuthorization: true}, draft), "提交意见（不执行原操作）");
	assert.equal(interactionPrimaryActionLabel({...selectItem, requiresAuthorization: false}, draft), "提交回答");
	assert.equal(interactionPrimaryActionLabel({...selectItem, requiresAuthorization: true}, draft, true), "提交中…");
});

test("confirm text is always feedback and never authorization", () => {
	const draft = createInteractionDraft({action: "confirm"});
	draft.text = "先备份，不要立刻重启";
	assert.equal(interactionPrimaryActionLabel({action: "confirm", confirmText: "确认重启"}, draft), "提交意见（不执行原操作）");
	assert.equal(interactionRejectActionLabel(draft), "拒绝并提交说明（不执行）");
	assert.deepEqual(buildInteractionAnswer({action: "confirm"}, draft, "confirm"), {
		cancelled: false,
		decision: "feedback",
		selectedDecision: "confirm",
		text: "先备份，不要立刻重启",
		confirmed: false,
	});
	assert.equal(hasSubmittedInteractionText(" "), false, "whitespace is used only to judge emptiness");
});

test("explicit confirm/reject remain decisions only when no text is present", () => {
	assert.deepEqual(buildInteractionAnswer({action: "confirm"}, {text: ""}, "confirm"), {
		cancelled: false, decision: "confirm", selectedDecision: "confirm", text: "", confirmed: true,
	});
	assert.deepEqual(buildInteractionAnswer({action: "confirm"}, {text: ""}, "reject"), {
		cancelled: false, decision: "reject", selectedDecision: "reject", text: "", confirmed: false,
	});
});

test("cancel payload never submits select, prompt, confirm, or questionnaire-like draft content", () => {
	for (const item of [selectItem, {action: "prompt"}, {action: "confirm"}]) {
		assert.deepEqual(buildInteractionAnswer(item, {
			selectedIndexes: [0], value: "prompt secret", text: "unsubmitted draft",
		}, "cancel"), {cancelled: true});
	}
});

test("composer and parent wire shared submitting, inline error, expiry, and revision contracts", () => {
	assert.match(composerSource, /interactionDisabled\(item\)/);
	assert.match(composerSource, /interaction-submit-error/);
	assert.match(composerSource, /interactionExpiry\(item\)/);
	assert.match(consoleViewSource, /confirmationSubmitting\.value\[confirmationId\]/);
	assert.match(consoleViewSource, /revision:\s*interactionRevision\(item\)/);
	assert.match(consoleViewSource, /isTerminalInteractionError\(error\)/);
});

test("revision, expiry and terminal response compatibility are deterministic", () => {
	assert.equal(interactionRevision({}), 1);
	assert.equal(interactionRevision({revision: 7}), 7);
	assert.equal(isInteractionExpired({expiresAtMs: 2_000}, 2_000), true);
	assert.equal(interactionExpiryLabel({expiresAtMs: 62_000}, 1_000), "剩余 1 分 1 秒");
	assert.equal(interactionExpiryLabel({expiresAtMs: 1_000}, 2_000), "已过期");
	for (const [status, code] of [[409, "confirmation_already_resolved"], [409, "confirmation_expired"], [404, "not_found"], [404, "confirmation_not_found"]]) {
		assert.equal(isTerminalInteractionError({response: {status, data: {error: code}}}), true);
	}
	assert.equal(isTerminalInteractionError({response: {status: 400, data: {error: "invalid_answer"}}}), false);
});
