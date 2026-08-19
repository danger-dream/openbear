import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

import {
	buildUserInteractionView,
	isUserInteractionEvent,
	isUserInteractionOperation,
	userInteractionEventInput,
} from "./userInteractionPresentation.js";

const componentSource = readFileSync(new URL("./ConsoleUserInteractionEvent.vue", import.meta.url), "utf8");

function view(action, status = "answered", extraArgs = {}, extraResult = {}) {
	return buildUserInteractionView({
		operation: {status: status === "pending" ? "running" : "completed", payload: {
			interaction: {action, title: `当时的${action}标题`, interactionStatus: status},
			arguments: JSON.stringify({action, title: `当时的${action}标题`, body: "只描述问题，不复述回答", ...extraArgs}),
			result: JSON.stringify({status, ...extraResult}),
		}},
	});
}

test("typed and exact legacy UserInteraction are recognized without misclassifying ordinary tools", () => {
	assert.equal(isUserInteractionOperation({opType: "user_interaction", payload: {}}), true);
	assert.equal(isUserInteractionOperation({opType: "tool", payload: {name: "UserInteraction"}}), true);
	assert.equal(isUserInteractionOperation({opType: "tool", payload: {name: "Read"}}), false);
	assert.equal(isUserInteractionOperation({opType: "agent_control", payload: {name: "UserInteraction"}}), false);
	assert.equal(isUserInteractionEvent({kind: "tool", calls: [{name: "UserInteraction"}]}), true);
	assert.equal(userInteractionEventInput({calls: [{arguments: '{"action":"confirm"}'}]}).arguments, '{"action":"confirm"}');
});

test("typed lazy summary preserves root questionnaire presentation after refresh without affecting ordinary tools", () => {
	const operation = {
		opType: "user_interaction",
		payload: {
			action: "questionnaire",
			title: "验收问卷标题",
			status: "completed",
			interactionStatus: "answered",
			sensitive: false,
		},
	};
	const input = userInteractionEventInput({operation});
	const model = buildUserInteractionView(input);
	assert.equal(input.interaction, operation.payload);
	assert.equal(model.action, "questionnaire");
	assert.equal(model.title, "验收问卷标题");
	assert.equal(model.status, "answered");
	assert.equal(model.statusLabel, "已回答");
	assert.equal(model.intro, "用户已完成本次问卷");

	const ordinaryInput = userInteractionEventInput({operation: {
		opType: "tool",
		payload: {name: "Read", action: "questionnaire", title: "不得识别为问卷", interactionStatus: "answered"},
	}});
	assert.equal(ordinaryInput.interaction, undefined);
	const ordinaryModel = buildUserInteractionView(ordinaryInput);
	assert.equal(ordinaryModel.action, "confirm");
	assert.equal(ordinaryModel.title, "请确认");
});

test("confirm/select/prompt terminal status matrix has stable labels and answer-free intro", () => {
	const statuses = {
		pending: "等待回答", cancelled: "已取消", timeout: "已超时", error: "出错",
	};
	const names = {confirm: "确认", select: "选择", prompt: "输入"};
	const intros = {
		pending: (name) => `正在等待用户完成${name}`,
		cancelled: (name) => `用户未继续本次${name}`,
		timeout: (name) => `本次${name}未在限定时间内完成`,
		error: (name) => `本次${name}未能完成`,
	};
	for (const action of ["confirm", "select", "prompt"]) {
		for (const [status, label] of Object.entries(statuses)) {
			const model = view(action, status);
			assert.equal(model.action, action);
			assert.equal(model.statusLabel, label);
			assert.equal(model.body, "只描述问题，不复述回答");
			assert.equal(model.intro, intros[status](names[action]));
		}
	}
	assert.equal(view("confirm", "answered", {}, {confirmed: true}).statusLabel, "已确认");
	assert.equal(view("confirm", "answered", {}, {confirmed: false}).statusLabel, "已拒绝");
	assert.equal(view("confirm", "answered").statusLabel, "已回答");
	assert.equal(view("confirm", "answered").intro, "用户已完成本次确认");
	assert.equal(view("prompt", "answered", {}, {value: "逐字回答"}).promptValue, "逐字回答");
});

test("loading interaction detail body does not rewrite the collapsed intro", () => {
	const summary = buildUserInteractionView(userInteractionEventInput({
		operation: {
			opType: "user_interaction",
			payload: {
				action: "confirm",
				title: "处理 Docker Action 重复构建",
				status: "completed",
				interactionStatus: "cancelled",
			},
		},
	}));
	const detailed = buildUserInteractionView(userInteractionEventInput({
		operation: {
			opType: "user_interaction",
			payload: {
				action: "confirm",
				title: "处理 Docker Action 重复构建",
				status: "completed",
				interactionStatus: "cancelled",
				arguments: JSON.stringify({
					action: "confirm",
					title: "处理 Docker Action 重复构建",
					body: "我建议现在：① 重跑失败的 main 镜像任务，使 :main 更新到...",
				}),
			},
		},
	}));
	assert.equal(summary.intro, "用户未继续本次确认");
	assert.equal(detailed.intro, summary.intro);
	assert.equal(detailed.body.startsWith("我建议现在"), true);
});

test("lazy confirm summary uses confirmed polarity and does not treat a missing result as rejection", () => {
	const confirmed = buildUserInteractionView(userInteractionEventInput({
		operation: {
			opType: "user_interaction",
			payload: {
				action: "confirm",
				title: "确认 Demo4 的现实验收边界",
				status: "completed",
				interactionStatus: "answered",
				sensitive: false,
				confirmed: true,
			},
		},
	}));
	assert.equal(confirmed.statusLabel, "已确认");
	assert.equal(confirmed.statusKey, "confirmed");
	assert.equal(confirmed.intro, "用户已完成本次确认");

	const rejected = buildUserInteractionView(userInteractionEventInput({
		operation: {
			opType: "user_interaction",
			payload: {
				action: "confirm",
				title: "确认 Demo4 的现实验收边界",
				status: "completed",
				interactionStatus: "answered",
				confirmed: false,
			},
		},
	}));
	assert.equal(rejected.statusLabel, "已拒绝");
	assert.equal(rejected.intro, "用户未继续本次确认");

	const unknown = buildUserInteractionView(userInteractionEventInput({
		operation: {
			opType: "user_interaction",
			payload: {
				action: "confirm",
				title: "确认 Demo4 的现实验收边界",
				status: "completed",
				interactionStatus: "answered",
			},
		},
	}));
	assert.equal(unknown.statusLabel, "已回答");
	assert.equal(unknown.intro, "用户已完成本次确认");
});

test("questionnaire preserves question order, selected plus text, and never treats recommendation as an answer", () => {
	const model = view("questionnaire", "answered", {questions: [
		{id: "q1", type: "choice", question: "第一题", required: true, multiple: true,
			options: [{label: "甲", value: "a"}, {label: "乙", value: "b"}],
			recommendation: {values: ["a"], reason: "当时建议理由"}},
		{id: "q2", type: "open", question: "第二题", required: false},
		{id: "q3", type: "choice", question: "第三题", options: [{label: "推荐项", value: "r"}], recommendation: {values: ["r"]}},
	]}, {answers: [
		{questionId: "q2", text: "开放文本"},
		{questionId: "q1", selectedValues: ["b"], text: "补充文本"},
	]});
	assert.deepEqual(model.questions.map((q) => q.question), ["第一题", "第二题", "第三题"]);
	assert.equal(model.questions[0].options[0].recommended, true);
	assert.equal(model.questions[0].options[0].selected, false);
	assert.equal(model.questions[0].options[1].selected, true);
	assert.equal(model.questions[0].answerText, "补充文本");
	assert.equal(model.questions[1].answerText, "开放文本");
	assert.equal(model.questions[2].options[0].recommended, true);
	assert.equal(model.questions[2].options[0].selected, false);
	assert.equal(model.questions[2].answered, false);
});

test("sensitive and redacted payloads are fail-closed in presentation", () => {
	const secret = "UI-SECRET-DO-NOT-RENDER";
	for (const args of [
		{action: "prompt", title: "密码", body: "请输入密码", sensitive: true, defaultValue: secret},
		{action: "prompt", title: "密码", body: "[敏感内容已隐藏]", defaultValue: secret},
	]) {
		const model = buildUserInteractionView({arguments: JSON.stringify(args), result: JSON.stringify({status: "answered", value: secret})});
		assert.equal(model.sensitive, true);
		assert.equal(model.promptValue, "");
		assert.equal(JSON.stringify(model).includes(secret), false);
		assert.equal(model.redactedText, "[敏感内容已隐藏]");
	}
});

test("component contract is read-only and retains collapsed and questionnaire structure", () => {
	assert.match(componentSource, /interaction-icon/);
	assert.match(componentSource, /interaction-name/);
	assert.match(componentSource, /interaction-intro/);
	assert.match(componentSource, /status-chip/);
	assert.match(componentSource, /\.interaction-event > summary \{ display: flex;/);
	assert.doesNotMatch(componentSource, /\.interaction-event > summary \{[^}]*grid-template-columns/);
	assert.match(componentSource, /white-space:\s*nowrap/);
	assert.match(componentSource, /compact:\s*\{type:\s*Boolean/);
	assert.match(componentSource, /disclosure-icon/);
	assert.match(componentSource, /question-number/);
	assert.match(componentSource, /required-mark/);
	assert.match(componentSource, /optional-mark/);
	assert.match(componentSource, /recommendation-reason/);
	assert.match(componentSource, /readonly-text-answer/);
	assert.doesNotMatch(componentSource, /<(?:input|textarea|select|button)\b/i);
	assert.doesNotMatch(componentSource, /提交|取消|清除|复制|JSON\.stringify/);
});
