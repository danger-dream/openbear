import {window} from "../../domTestEnv.mjs";
import {registerVueLoader} from "../../testing/vueLoader.mjs";
const {document} = window;
import test from "node:test";
import assert from "node:assert/strict";
import {createApp, h, nextTick, reactive} from "vue";

registerVueLoader();
const PendingInteractionSheet = (await import("./PendingInteractionSheet.vue")).default;

async function mountSheet(initialProps = {}) {
	const container = document.createElement("div");
	document.body.appendChild(container);
	const answers = [];
	const state = reactive({
		items: [],
		submitting: {},
		errors: {},
		phone: false,
		compact: false,
		...initialProps,
	});
	let exposed = null;
	const app = createApp({
		render() {
			return h(PendingInteractionSheet, {
				ref: (el) => { exposed = el; },
				items: state.items,
				submitting: state.submitting,
				errors: state.errors,
				phone: state.phone,
				compact: state.compact,
				onAnswer: (item, payload) => answers.push({item, payload}),
			});
		},
	});
	app.mount(container);
	await nextTick();
	return {
		container,
		state,
		answers,
		get exposed() { return exposed; },
		unmount() {
			app.unmount();
			container.remove();
		},
	};
}

const longProposalQuestionnaire = {
	confirmationId: "q-long",
	action: "questionnaire",
	title: "重构方案确认",
	body: "### 核心设计\n- 手机端改为跟手底部抽屉\n- 桌面端支持一键折叠和拖拽调高\n- 问卷支持免填必填项写总意见退回",
	revision: 1,
	expiresAtMs: Date.now() + 600_000,
	questions: [
		{
			id: "q1",
			type: "choice",
			required: true,
			multiple: false,
			question: "默认布局偏好？",
			options: [
				{label: "分步聚焦", value: "stepper", description: "每屏一题"},
				{label: "全部展开", value: "all", description: "单列表滚动"},
			],
			recommendation: {values: ["stepper"], reason: "手机端更紧凑"},
		},
		{
			id: "q2",
			type: "open",
			required: true,
			question: "还有哪些限制？",
		},
	],
};

test("questionnaire with long proposal shows dedicated proposal tab, folds to dock preserving draft, and submits overall feedback skipping required questions", async () => {
	const h = await mountSheet({items: [longProposalQuestionnaire]});
	try {
		// Starts on the dedicated proposal tab when body is a long proposal
		const proposalTab = document.querySelector(".pi-step-pill.is-proposal");
		assert.ok(proposalTab?.classList.contains("is-current"));
		assert.match(document.querySelector(".pi-body")?.textContent || "", /手机端改为跟手底部抽屉/);

		// Move to Q1 and select option A
		document.querySelector(".pi-foot .pi-btn.is-primary").click();
		await nextTick();
		assert.match(document.querySelector(".pi-q-title")?.textContent || "", /默认布局偏好/);
		const firstOpt = document.querySelector(".pi-opt");
		firstOpt.click();
		await nextTick();
		assert.ok(firstOpt.classList.contains("is-selected"));

		// Collapse to read conversation, then expand — draft must remain intact
		document.querySelector(".pi-fold-btn").click();
		await nextTick();
		assert.equal(document.querySelector(".pi-sheet"), null);
		const dock = document.querySelector(".pi-dock-pill");
		assert.ok(dock);
		assert.match(dock.textContent || "", /已答 1\/2 题/);
		dock.click();
		await nextTick();
		assert.ok(document.querySelector(".pi-opt")?.classList.contains("is-selected"));

		// Open overall reply bar and send feedback while q2 (required) is still empty
		document.querySelector(".pi-overall-trigger").click();
		await nextTick();
		const overallInput = document.querySelector("[data-overall-reply-input]");
		assert.ok(overallInput);
		overallInput.value = "先聊聊方案第 2 点，再决定";
		overallInput.dispatchEvent(new window.Event("input", {bubbles: true}));
		await nextTick();

		const feedbackBtn = document.querySelector(".pi-foot .pi-btn.is-warning");
		assert.ok(feedbackBtn);
		assert.match(feedbackBtn.textContent || "", /发意见并退回问卷/);
		feedbackBtn.click();
		await nextTick();

		assert.equal(h.answers.length, 1);
		assert.deepEqual(h.answers[0].payload, {
			cancelled: false,
			decision: "feedback",
			text: "先聊聊方案第 2 点，再决定",
		});
	} finally {
		h.unmount();
	}
});

test("questionnaire with all required answered plus overall text offers both feedback-only and submit-with-overall", async () => {
	const h = await mountSheet({items: [longProposalQuestionnaire]});
	try {
		// Switch to all-expanded list view and fill both required questions
		document.querySelector(".pi-mode-btn").click();
		await nextTick();
		document.querySelector(".pi-opt").click();
		const openInput = document.querySelector('[data-q-note-input="q2"]');
		openInput.value = "保持深浅主题一致";
		openInput.dispatchEvent(new window.Event("input", {bubbles: true}));
		await nextTick();

		// Add overall note
		document.querySelector(".pi-overall-trigger").click();
		await nextTick();
		const overallInput = document.querySelector("[data-overall-reply-input]");
		overallInput.value = "总体同意，优先照顾窄屏";
		overallInput.dispatchEvent(new window.Event("input", {bubbles: true}));
		await nextTick();

		const rightButtons = Array.from(document.querySelectorAll(".pi-foot-right button")).map((b) => b.textContent.trim());
		assert.deepEqual(rightButtons, ["仅发意见退回", "附带总说明提交"]);

		document.querySelector(".pi-foot-right .pi-btn.is-primary").click();
		await nextTick();
		assert.equal(h.answers.length, 1);
		assert.equal(h.answers[0].payload.text, "总体同意，优先照顾窄屏");
		assert.equal(h.answers[0].payload.answers[0].selectedValues[0], "stepper");
		assert.equal(h.answers[0].payload.answers[1].text, "保持深浅主题一致");
	} finally {
		h.unmount();
	}
});

test("phone bottom sheet teleports to body with backdrop and confirm amendment switches primary button", async () => {
	const confirmItem = {
		confirmationId: "c-restart",
		action: "confirm",
		type: "danger",
		title: "确认执行：restart",
		body: "将重启 openbear.service。",
		confirmText: "确认重启",
		cancelText: "取消",
		requiresAuthorization: true,
		revision: 1,
		expiresAtMs: Date.now() + 600_000,
	};
	const h = await mountSheet({items: [confirmItem], phone: true});
	try {
		const overlay = document.querySelector(".pi-phone-overlay");
		const sheet = document.querySelector(".pi-sheet.is-phone");
		const backdrop = document.querySelector(".pi-backdrop");
		assert.ok(overlay && sheet && backdrop);
		assert.equal(overlay.parentElement, document.body);
		assert.equal(sheet.parentElement, overlay);
		assert.equal(sheet.dataset.tone, "danger");

		// Progressive amendment input opens on demand and updates button semantics
		document.querySelector(".pi-note-toggle").click();
		await nextTick();
		const ta = document.querySelector("[data-interaction-note-input]");
		ta.value = "延迟 10 秒再重启";
		ta.dispatchEvent(new window.Event("input", {bubbles: true}));
		await nextTick();

		const primaryBtn = document.querySelector(".pi-foot-right .pi-btn.is-danger");
		assert.match(primaryBtn.textContent || "", /按补充内容确认执行/);
		primaryBtn.click();
		await nextTick();

		assert.equal(h.answers.length, 1);
		assert.equal(h.answers[0].payload.decision, "confirm_with_changes");
		assert.equal(h.answers[0].payload.text, "延迟 10 秒再重启");
	} finally {
		h.unmount();
	}
});

test("sensitive prompt masks input by default with toggle and submits exact value", async () => {
	const promptItem = {
		confirmationId: "p-secret",
		action: "prompt",
		sensitive: true,
		title: "输入 API Token",
		body: "用于更新 DDNS",
		confirmText: "提交 Token",
		cancelText: "取消",
		revision: 1,
		expiresAtMs: Date.now() + 600_000,
	};
	const h = await mountSheet({items: [promptItem]});
	try {
		const input = document.querySelector(".pi-secret-row input");
		assert.equal(input.type, "password");
		document.querySelector(".pi-secret-eye").click();
		await nextTick();
		assert.equal(document.querySelector(".pi-secret-row input").type, "text");

		input.value = "cf-secret-token";
		input.dispatchEvent(new window.Event("input", {bubbles: true}));
		await nextTick();
		document.querySelector(".pi-foot-right .pi-btn.is-primary").click();
		await nextTick();

		assert.equal(h.answers.length, 1);
		assert.deepEqual(h.answers[0].payload, {cancelled: false, value: "cf-secret-token"});
	} finally {
		h.unmount();
	}
});

test("horizontal swipe on questionnaire body switches between proposal and questions while preserving vertical scroll and input selection", async () => {
	const h = await mountSheet({items: [longProposalQuestionnaire], phone: true});
	const swipe = (target, x1, y1, x2, y2) => {
		const mk = (type, x, y, changed = false) => {
			const ev = new window.Event(type, {bubbles: true});
			ev.touches = changed ? [] : [{clientX: x, clientY: y}];
			ev.changedTouches = [{clientX: x, clientY: y}];
			return ev;
		};
		target.dispatchEvent(mk("touchstart", x1, y1));
		target.dispatchEvent(mk("touchmove", (x1 + x2) / 2, (y1 + y2) / 2));
		target.dispatchEvent(mk("touchmove", x2, y2));
		target.dispatchEvent(mk("touchend", x2, y2, true));
	};
	try {
		const body = document.querySelector(".pi-body");
		// Starts on proposal tab
		assert.ok(document.querySelector(".pi-step-pill.is-proposal")?.classList.contains("is-current"));

		// Vertical scroll does not change tab
		swipe(body, 200, 400, 190, 220);
		await nextTick();
		assert.ok(document.querySelector(".pi-step-pill.is-proposal")?.classList.contains("is-current"));

		// Swipe left -> Q1
		swipe(body, 300, 360, 180, 368);
		await nextTick();
		assert.match(document.querySelector(".pi-q-title")?.textContent || "", /默认布局偏好/);

		// Swipe left -> Q2
		swipe(body, 300, 360, 180, 362);
		await nextTick();
		assert.match(document.querySelector(".pi-q-title")?.textContent || "", /还有哪些限制/);

		// Swiping inside textarea must not hijack text cursor selection
		const textarea = document.querySelector('[data-q-note-input="q2"]');
		swipe(textarea, 120, 360, 280, 360);
		await nextTick();
		assert.match(document.querySelector(".pi-q-title")?.textContent || "", /还有哪些限制/);

		// Swipe right on body -> back to Q1
		swipe(body, 120, 360, 260, 364);
		await nextTick();
		assert.match(document.querySelector(".pi-q-title")?.textContent || "", /默认布局偏好/);

		// Swipe right -> back to proposal
		swipe(body, 120, 360, 260, 362);
		await nextTick();
		assert.ok(document.querySelector(".pi-step-pill.is-proposal")?.classList.contains("is-current"));
	} finally {
		h.unmount();
	}
});
