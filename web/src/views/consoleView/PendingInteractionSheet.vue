<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch} from "vue";
import {
	Check,
	ChevronDown,
	ChevronLeft,
	ChevronRight,
	Eye,
	EyeOff,
	FileText,
	MessageSquareText,
	ShieldAlert,
} from "@lucide/vue";
import InteractionMarkdown from "./InteractionMarkdown.vue";
import GroupProposalCard from "./GroupProposalCard.vue";
import {
	buildQuestionnaireAnswer,
	clearQuestionChoice,
	createQuestionnaireDraft,
	ensureQuestionAnswer,
	hasLongQuestionnaireBody,
	isQuestionAnswered,
	isQuestionChoiceSelected,
	questionnaireProgress,
	toggleQuestionChoice,
	validateQuestionnaire,
} from "./questionnaireState.js";
import {
	buildInteractionAnswer,
	clearInteractionSelection,
	createInteractionDraft,
	hasSubmittedInteractionText,
	interactionExpiryLabel,
	interactionPrimaryActionLabel,
	interactionRejectActionLabel,
	isInteractionExpired,
	toggleInteractionOption,
	validateInteractionDraft,
} from "./userInteractionState.js";

const props = defineProps({
	items: {type: Array, default: () => []},
	submitting: {type: Object, default: () => ({})},
	errors: {type: Object, default: () => ({})},
	phone: {type: Boolean, default: false},
	compact: {type: Boolean, default: false},
});

const emit = defineEmits(["answer"]);

const rootEl = ref(null);
const sheetEl = ref(null);
const bodyEl = ref(null);
const stepPillsEl = ref(null);
const collapsed = ref(false);
const activeId = ref("");
const nowMs = ref(Date.now());
let clockTimer = null;

// Horizontal swipe-to-switch-step state for questionnaire body on touch screens.
const swipeOffsetX = ref(0);
const swipeAnimating = ref(false);
let swipeGesture = null;

// Per-interaction drafts and UI view state (survive collapse/expand and queue switching).
const interactionDrafts = reactive({});
const questionnaireDrafts = reactive({});
const localErrors = reactive({});
const questionnaireErrors = reactive({});
const uiStates = reactive({});

// Desktop drag-resize height (px, null means CSS default).
const desktopMaxHeight = ref(null);

// Phone bottom-sheet drag state.
const sheetOffset = ref(0);
const sheetDragging = ref(false);
const sheetClosing = ref(false);
let sheetGesture = null;
let sheetCloseTimer = null;
let desktopResizeGesture = null;

const isPhoneSheet = computed(() => props.phone && !props.compact);

const activeIndex = computed(() => {
	if (!props.items.length) return -1;
	const idx = props.items.findIndex((item) => item?.confirmationId === activeId.value);
	return idx >= 0 ? idx : 0;
});

const activeItem = computed(() => (activeIndex.value >= 0 ? props.items[activeIndex.value] : null));

function actionOf(item) {
	return String(item?.action || "confirm").trim().toLowerCase();
}

function toneOf(item) {
	const raw = String(item?.type || item?.tone || "").trim().toLowerCase();
	if (raw === "danger" || raw === "warning" || raw === "success") return raw;
	if (item?.sensitive) return "warning";
	return "info";
}

function kindLabelOf(item) {
	const action = actionOf(item);
	const tone = toneOf(item);
	if (action === "group_proposal") return "小组方案";
	if (action === "questionnaire") return hasLongQuestionnaireBody(item?.body) ? "方案与问卷" : "需要决定";
	if (action === "select") return item?.requiresAuthorization ? "授权选择" : (item?.multiple ? "多项选择" : "单项选择");
	if (action === "prompt") return item?.sensitive ? "敏感输入" : "需要输入";
	if (tone === "danger") return "高风险确认";
	if (tone === "warning" || item?.requiresAuthorization) return "操作确认";
	return "请确认";
}

function optionLabel(option) {
	if (option && typeof option === "object") return String(option.label || option.text || option.value || "");
	return String(option || "");
}

function optionDescription(option) {
	return option && typeof option === "object" ? String(option.description || "") : "";
}

function questionId(question) {
	return String(question?.questionId ?? question?.id ?? "").trim();
}

function questionText(question, index) {
	return String(question?.prompt || question?.question || question?.title || question?.label || `问题 ${index + 1}`);
}

function questionsOf(item) {
	return Array.isArray(item?.questions) ? item.questions : [];
}

function optionValue(option) {
	if (option && typeof option === "object") return option.value ?? option.label ?? "";
	return option ?? "";
}

function ensureUiState(item) {
	const id = String(item?.confirmationId || "");
	if (!id) {
		return {
			tab: 0,
			listView: false,
			inlineProposalOpen: true,
			noteOpen: false,
			questionNoteOpen: {},
			overallOpen: false,
			overallText: "",
			secretVisible: false,
		};
	}
	if (!uiStates[id]) {
		const isLongQ = actionOf(item) === "questionnaire" && hasLongQuestionnaireBody(item?.body);
		uiStates[id] = {
			tab: isLongQ ? "proposal" : 0,
			listView: false,
			inlineProposalOpen: true,
			noteOpen: false,
			questionNoteOpen: {},
			overallOpen: false,
			overallText: "",
			secretVisible: false,
		};
	}
	return uiStates[id];
}

function ensureInteractionDraft(item) {
	const id = String(item?.confirmationId || "");
	if (!id) return createInteractionDraft(item);
	if (!interactionDrafts[id]) interactionDrafts[id] = createInteractionDraft(item);
	return interactionDrafts[id];
}

function ensureQuestionnaireDraft(item) {
	const id = String(item?.confirmationId || "");
	if (!id) return createQuestionnaireDraft(questionsOf(item));
	if (!questionnaireDrafts[id]) questionnaireDrafts[id] = createQuestionnaireDraft(questionsOf(item));
	return questionnaireDrafts[id];
}

function isExpired(item) {
	return isInteractionExpired(item, nowMs.value);
}

function isSubmitting(item) {
	return Boolean(props.submitting?.[item?.confirmationId]);
}

function isDisabled(item) {
	return Boolean(isSubmitting(item) || isExpired(item));
}

function expiryText(item) {
	return interactionExpiryLabel(item, nowMs.value);
}

function itemError(item) {
	const id = item?.confirmationId;
	return localErrors[id] || props.errors?.[id] || "";
}

function clearLocalError(item) {
	const id = item?.confirmationId;
	if (id && localErrors[id]) delete localErrors[id];
}

// Keep activeId valid and auto-open when fresh interactions arrive after an empty state.
// Drafts are deliberately kept across conversation switches so returning to a pending form restores unsent input.
watch(
	() => props.items.map((item) => item?.confirmationId).filter(Boolean),
	(nextIds, prevIds = []) => {
		const valid = new Set(nextIds);
		for (const map of [interactionDrafts, questionnaireDrafts, localErrors, questionnaireErrors, uiStates]) {
			const keys = Object.keys(map);
			if (keys.length > 60) {
				for (const key of keys) {
					if (!valid.has(key) && Object.keys(map).length > 40) delete map[key];
				}
			}
		}
		if (!nextIds.length) {
			activeId.value = "";
			collapsed.value = false;
			sheetOffset.value = 0;
			sheetClosing.value = false;
			return;
		}
		if (!valid.has(activeId.value)) activeId.value = nextIds[0];
		if (!prevIds.length) collapsed.value = false;
	},
	{immediate: true},
);

// Keyboard Escape collapses the sheet so user can read transcript.
function onDocumentKeydown(event) {
	if (event.key !== "Escape" || !props.items.length || collapsed.value) return;
	const target = event.target;
	if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT") && target.closest?.("[data-interaction-id]")) {
		target.blur?.();
		return;
	}
	collapsePanel();
}

onMounted(() => {
	nowMs.value = Date.now();
	clockTimer = window.setInterval(() => {
		nowMs.value = Date.now();
	}, 1000);
	document.addEventListener("keydown", onDocumentKeydown);
});

onBeforeUnmount(() => {
	if (clockTimer) window.clearInterval(clockTimer);
	if (sheetCloseTimer) window.clearTimeout(sheetCloseTimer);
	clockTimer = null;
	sheetCloseTimer = null;
	document.removeEventListener("keydown", onDocumentKeydown);
	endDesktopResize();
});

function collapsePanel() {
	if (collapsed.value) return;
	if (isPhoneSheet.value) {
		sheetClosing.value = true;
		sheetOffset.value = Math.max(sheetOffset.value, 16);
		if (sheetCloseTimer) window.clearTimeout(sheetCloseTimer);
		sheetCloseTimer = window.setTimeout(() => {
			sheetCloseTimer = null;
			sheetClosing.value = false;
			sheetOffset.value = 0;
			collapsed.value = true;
		}, 200);
	} else {
		collapsed.value = true;
	}
}

function expandPanel() {
	if (sheetCloseTimer) {
		window.clearTimeout(sheetCloseTimer);
		sheetCloseTimer = null;
	}
	sheetClosing.value = false;
	sheetOffset.value = 0;
	collapsed.value = false;
}

// ---------- Phone pull-down to collapse ----------
const phoneSheetStyle = computed(() => {
	if (!isPhoneSheet.value) return undefined;
	if (sheetClosing.value) {
		return {
			transform: "translateY(104%)",
			transition: "transform 200ms cubic-bezier(.22, .68, 0, 1)",
		};
	}
	if (sheetOffset.value > 0) {
		return {
			transform: `translateY(${sheetOffset.value}px)`,
			transition: sheetDragging.value ? "none" : "transform 220ms cubic-bezier(.22, .68, 0, 1)",
		};
	}
	return undefined;
});

const backdropStyle = computed(() => {
	if (!isPhoneSheet.value) return undefined;
	if (sheetClosing.value) return {opacity: 0, transition: "opacity 200ms ease"};
	if (sheetOffset.value > 0) {
		const ratio = Math.max(0.12, 1 - sheetOffset.value / 320);
		return {opacity: ratio, transition: sheetDragging.value ? "none" : "opacity 220ms ease"};
	}
	return undefined;
});

function onPhoneDragStart(event) {
	if (!isPhoneSheet.value || sheetClosing.value || event.button > 0) return;
	if (event.target?.closest?.("button, input, textarea, a")) return;
	sheetGesture = {
		id: event.pointerId,
		x: event.clientX,
		y: event.clientY,
		lastY: event.clientY,
		lastT: event.timeStamp,
		speed: 0,
	};
	event.currentTarget?.setPointerCapture?.(event.pointerId);
}

function onPhoneDragMove(event) {
	if (!sheetGesture || event.pointerId !== sheetGesture.id) return;
	const dx = event.clientX - sheetGesture.x;
	const dy = event.clientY - sheetGesture.y;
	if (!sheetDragging.value) {
		if (Math.hypot(dx, dy) < 4) return;
		if (dy < 0 || Math.abs(dx) > Math.abs(dy)) {
			onPhoneDragCancel();
			return;
		}
		sheetDragging.value = true;
	}
	sheetGesture.speed = (event.clientY - sheetGesture.lastY) / Math.max(1, event.timeStamp - sheetGesture.lastT);
	sheetGesture.lastY = event.clientY;
	sheetGesture.lastT = event.timeStamp;
	sheetOffset.value = Math.max(0, dy);
}

function onPhoneDragCancel() {
	sheetGesture = null;
	sheetDragging.value = false;
	sheetOffset.value = 0;
}

function onPhoneDragEnd(event) {
	if (!sheetGesture || (event && event.pointerId !== sheetGesture.id)) return;
	const shouldClose = sheetOffset.value >= 84 || (sheetOffset.value > 24 && sheetGesture.speed > 0.45);
	sheetGesture = null;
	sheetDragging.value = false;
	if (shouldClose) collapsePanel();
	else sheetOffset.value = 0;
}

// ---------- Desktop top-handle resize ----------
const desktopSheetStyle = computed(() => {
	if (isPhoneSheet.value || !desktopMaxHeight.value) return undefined;
	return {maxHeight: `${desktopMaxHeight.value}px`};
});

function onDesktopResizeStart(event) {
	if (isPhoneSheet.value || event.button > 0) return;
	const rect = sheetEl.value?.getBoundingClientRect?.();
	if (!rect) return;
	desktopResizeGesture = {startY: event.clientY, startH: rect.height};
	window.addEventListener("pointermove", onDesktopResizeMove);
	window.addEventListener("pointerup", endDesktopResize);
	window.addEventListener("pointercancel", endDesktopResize);
}

function onDesktopResizeMove(event) {
	if (!desktopResizeGesture) return;
	const dy = event.clientY - desktopResizeGesture.startY;
	const maxAllowed = Math.max(360, Math.floor((window.innerHeight || 800) * 0.78));
	desktopMaxHeight.value = Math.max(240, Math.min(maxAllowed, Math.round(desktopResizeGesture.startH - dy)));
}

function endDesktopResize() {
	if (!desktopResizeGesture) return;
	desktopResizeGesture = null;
	window.removeEventListener("pointermove", onDesktopResizeMove);
	window.removeEventListener("pointerup", endDesktopResize);
	window.removeEventListener("pointercancel", endDesktopResize);
}

// ---------- Dock summary ----------
function dockSummary(item) {
	const action = actionOf(item);
	const expiry = expiryText(item);
	if (isExpired(item)) return "已过期，等待同步状态";
	if (action === "questionnaire") {
		const ui = ensureUiState(item);
		if (ui.overallText.trim()) return "已填写总意见（免填必填项可直接退回）· 点击展开";
		const progress = questionnaireProgress(questionsOf(item), ensureQuestionnaireDraft(item));
		const prefix = hasLongQuestionnaireBody(item?.body) ? "含方案说明 · " : "";
		return `${prefix}已答 ${progress.answered}/${progress.total} 题${expiry ? ` · ${expiry}` : ""}`;
	}
	if (action === "select") {
		const draft = ensureInteractionDraft(item);
		if (draft.selectedIndexes.length) {
			const first = item.options?.[draft.selectedIndexes[0]];
			const extra = draft.selectedIndexes.length > 1 ? ` 等 ${draft.selectedIndexes.length} 项` : "";
			return `已选：${optionLabel(first)}${extra} · 点击展开提交`;
		}
		if (draft.text.trim()) return "已写自定义说明（草稿已保留）· 点击展开";
		return `等待选择${expiry ? ` · ${expiry}` : ""}`;
	}
	if (action === "prompt") {
		const draft = ensureInteractionDraft(item);
		if (draft.value.trim()) return "已输入内容（草稿已保留）· 点击展开提交";
		return `${item?.sensitive ? "仅 Web 端可见" : "等待输入"}${expiry ? ` · ${expiry}` : ""}`;
	}
	if (action === "group_proposal") return `小组提案待审批${expiry ? ` · ${expiry}` : ""}`;
	const draft = ensureInteractionDraft(item);
	if (draft.text.trim()) return "已写附言（草稿已保留）· 点击展开确认";
	return `${item?.requiresAuthorization ? "需要你的授权确认" : "等待你的确认"}${expiry ? ` · ${expiry}` : ""}`;
}

// ---------- Questionnaire step navigation & horizontal swipe ----------
function questionnaireTabs(item) {
	if (!item || actionOf(item) !== "questionnaire") return [];
	const tabs = hasLongQuestionnaireBody(item?.body) ? ["proposal"] : [];
	const count = questionsOf(item).length;
	for (let i = 0; i < count; i += 1) tabs.push(i);
	return tabs;
}

function scrollActiveStepPillIntoView() {
	nextTick(() => {
		const pill = stepPillsEl.value?.querySelector?.(".pi-step-pill.is-current");
		pill?.scrollIntoView?.({inline: "center", block: "nearest", behavior: "smooth"});
	});
}

function selectQuestionnaireTab(item, nextTab) {
	const ui = ensureUiState(item);
	ui.listView = false;
	ui.tab = nextTab;
	if (bodyEl.value) bodyEl.value.scrollTop = 0;
	scrollActiveStepPillIntoView();
}

function stepQuestionnaireTab(item, delta) {
	const ui = ensureUiState(item);
	if (ui.listView) return false;
	const tabs = questionnaireTabs(item);
	if (tabs.length <= 1) return false;
	const currentIdx = tabs.indexOf(ui.tab);
	const baseIdx = currentIdx >= 0 ? currentIdx : 0;
	const targetIdx = baseIdx + delta;
	if (targetIdx < 0 || targetIdx >= tabs.length) return false;
	selectQuestionnaireTab(item, tabs[targetIdx]);
	return true;
}

function isInsideHorizontalScroller(target, boundary) {
	let node = target;
	while (node && node !== boundary && node !== document.body) {
		const tag = String(node.tagName || "").toLowerCase();
		if (tag === "textarea" || tag === "input" || tag === "select" || tag === "table" || tag === "pre") return true;
		if (node.scrollWidth > node.clientWidth + 2) {
			const ox = window.getComputedStyle?.(node)?.overflowX || "";
			if (ox === "auto" || ox === "scroll") return true;
		}
		node = node.parentElement;
	}
	return false;
}

const bodySwipeStyle = computed(() => {
	if (!swipeOffsetX.value) return undefined;
	return {
		transform: `translateX(${swipeOffsetX.value}px)`,
		transition: swipeAnimating.value ? "transform 180ms cubic-bezier(.22, .68, 0, 1)" : "none",
	};
});

function onBodyTouchStart(event) {
	const item = activeItem.value;
	if (!item || actionOf(item) !== "questionnaire") return;
	const ui = ensureUiState(item);
	if (ui.listView || questionnaireTabs(item).length <= 1) return;
	if (!event.touches || event.touches.length !== 1) return;
	if (isInsideHorizontalScroller(event.target, bodyEl.value)) return;
	const touch = event.touches[0];
	swipeAnimating.value = false;
	swipeGesture = {
		x: touch.clientX,
		y: touch.clientY,
		lastX: touch.clientX,
		lastT: event.timeStamp || Date.now(),
		velocity: 0,
		locked: null, // "x" | "y"
	};
}

function onBodyTouchMove(event) {
	if (!swipeGesture || !event.touches || event.touches.length !== 1) return;
	const touch = event.touches[0];
	const dx = touch.clientX - swipeGesture.x;
	const dy = touch.clientY - swipeGesture.y;
	if (!swipeGesture.locked) {
		if (Math.hypot(dx, dy) < 12) return;
		if (Math.abs(dx) > Math.abs(dy) * 1.35) {
			swipeGesture.locked = "x";
		} else {
			swipeGesture.locked = "y";
			swipeGesture = null;
			return;
		}
	}
	if (swipeGesture.locked !== "x") return;
	const now = event.timeStamp || Date.now();
	swipeGesture.velocity = (touch.clientX - swipeGesture.lastX) / Math.max(1, now - swipeGesture.lastT);
	swipeGesture.lastX = touch.clientX;
	swipeGesture.lastT = now;

	const item = activeItem.value;
	const tabs = questionnaireTabs(item);
	const ui = ensureUiState(item);
	const idx = Math.max(0, tabs.indexOf(ui.tab));
	const atEdge = (dx > 0 && idx === 0) || (dx < 0 && idx === tabs.length - 1);
	const damped = atEdge ? dx * 0.22 : Math.max(-72, Math.min(72, dx * 0.45));
	swipeOffsetX.value = Math.round(damped);
}

function resetBodySwipe() {
	swipeGesture = null;
	if (!swipeOffsetX.value) return;
	swipeAnimating.value = true;
	swipeOffsetX.value = 0;
}

function onBodyTouchEnd(event) {
	if (!swipeGesture) return;
	const gesture = swipeGesture;
	swipeGesture = null;
	if (gesture.locked !== "x") return;
	const touch = event.changedTouches?.[0];
	const dx = touch ? touch.clientX - gesture.x : gesture.lastX - gesture.x;
	const speed = Math.abs(gesture.velocity);
	const triggered = Math.abs(dx) >= 52 || (Math.abs(dx) >= 28 && speed >= 0.35);
	if (triggered && activeItem.value) {
		stepQuestionnaireTab(activeItem.value, dx < 0 ? 1 : -1);
	}
	swipeAnimating.value = true;
	swipeOffsetX.value = 0;
}

// ---------- Questionnaire helpers ----------
function qProgress(item) {
	return questionnaireProgress(questionsOf(item), ensureQuestionnaireDraft(item));
}

function qAnswer(item, question) {
	return ensureQuestionAnswer(ensureQuestionnaireDraft(item), question);
}

function qSelected(item, question, option) {
	return isQuestionChoiceSelected(ensureQuestionnaireDraft(item), question, option);
}

function qAnswered(item, question) {
	return isQuestionAnswered(ensureQuestionnaireDraft(item), question);
}

function qIsRecommended(question, option) {
	const rec = Array.isArray(question?.recommendation?.values) ? question.recommendation.values : [];
	const val = optionValue(option);
	return rec.some((entry) => Object.is(entry, val));
}

function qError(item, question) {
	return questionnaireErrors[item?.confirmationId]?.[questionId(question)] || "";
}

function clearQError(item, question) {
	const map = questionnaireErrors[item?.confirmationId];
	if (map) delete map[questionId(question)];
}

function onToggleQChoice(item, question, option) {
	if (isDisabled(item)) return;
	toggleQuestionChoice(ensureQuestionnaireDraft(item), question, optionValue(option));
	clearQError(item, question);
	clearLocalError(item);
}

function onClearQChoice(item, question) {
	if (isDisabled(item)) return;
	clearQuestionChoice(ensureQuestionnaireDraft(item), question);
}

function onQTextInput(item, question, value) {
	qAnswer(item, question).text = value;
	if (String(value || "").trim()) {
		clearQError(item, question);
		clearLocalError(item);
	}
}

function openQuestionNote(item, question) {
	const ui = ensureUiState(item);
	const qid = questionId(question);
	ui.questionNoteOpen[qid] = true;
	nextTick(() => {
		const el = sheetEl.value?.querySelector?.(`[data-q-note-input="${qid}"]`);
		el?.focus?.();
	});
}

function openOverallReply(item) {
	const ui = ensureUiState(item);
	ui.overallOpen = true;
	nextTick(() => {
		sheetEl.value?.querySelector?.("[data-overall-reply-input]")?.focus?.();
	});
}

function closeOverallReply(item) {
	const ui = ensureUiState(item);
	ui.overallOpen = false;
	ui.overallText = "";
	clearLocalError(item);
}

function jumpToMissingRequired(item) {
	const questions = questionsOf(item);
	const progress = qProgress(item);
	if (!progress.missingQuestionIds.length) return;
	const firstMissingId = progress.missingQuestionIds[0];
	const idx = questions.findIndex((q) => questionId(q) === firstMissingId);
	const ui = ensureUiState(item);
	if (idx >= 0 && !ui.listView) ui.tab = idx;
	const errors = validateQuestionnaire(questions, ensureQuestionnaireDraft(item));
	questionnaireErrors[item.confirmationId] = errors;
	nextTick(() => {
		const target = sheetEl.value?.querySelector?.(`[data-question-id="${firstMissingId}"]`);
		target?.scrollIntoView?.({block: "nearest", behavior: "smooth"});
	});
}

function submitQuestionnaire(item, intent = "submit") {
	if (isDisabled(item)) return;
	const questions = questionsOf(item);
	const draft = ensureQuestionnaireDraft(item);
	const ui = ensureUiState(item);
	if (intent === "cancel") {
		clearLocalError(item);
		emit("answer", item, buildQuestionnaireAnswer(questions, draft, true));
		return;
	}
	if (intent === "feedback") {
		if (!ui.overallText.trim()) {
			localErrors[item.confirmationId] = "请先填写想反馈给模型的总意见。";
			return;
		}
		clearLocalError(item);
		emit("answer", item, buildQuestionnaireAnswer(questions, draft, false, {
			intent: "feedback",
			overallText: ui.overallText,
		}));
		return;
	}
	const errors = validateQuestionnaire(questions, draft);
	questionnaireErrors[item.confirmationId] = errors;
	if (Object.keys(errors).length) {
		jumpToMissingRequired(item);
		return;
	}
	clearLocalError(item);
	emit("answer", item, buildQuestionnaireAnswer(questions, draft, false, {
		overallText: ui.overallText,
	}));
}

// ---------- Select / Confirm / Prompt helpers ----------
function onToggleSelectOption(item, idx) {
	if (isDisabled(item)) return;
	toggleInteractionOption(item, ensureInteractionDraft(item), idx);
	clearLocalError(item);
}

function onClearSelectChoice(item) {
	if (isDisabled(item)) return;
	clearInteractionSelection(ensureInteractionDraft(item));
	clearLocalError(item);
}

function onOpenInteractionNote(item) {
	const ui = ensureUiState(item);
	ui.noteOpen = true;
	nextTick(() => {
		sheetEl.value?.querySelector?.("[data-interaction-note-input]")?.focus?.();
	});
}

function onSetInteractionText(item, value) {
	ensureInteractionDraft(item).text = value;
	if (String(value || "").trim() || ensureInteractionDraft(item).selectedIndexes.length) {
		clearLocalError(item);
	}
}

function onSetPromptValue(item, value) {
	ensureInteractionDraft(item).value = value;
	clearLocalError(item);
}

function isMultilineSensitivePrompt(item) {
	const value = ensureInteractionDraft(item).value || "";
	return value.includes("\n") || String(item?.defaultValue || "").includes("\n");
}

function answerSimpleInteraction(item, intent) {
	if (isDisabled(item)) return;
	const draft = ensureInteractionDraft(item);
	if (intent !== "cancel") {
		const error = validateInteractionDraft(item, draft, intent);
		if (error) {
			localErrors[item.confirmationId] = error;
			return;
		}
	}
	clearLocalError(item);
	emit("answer", item, buildInteractionAnswer(item, draft, intent));
}

function answerGroupProposal(item, payload) {
	if (isDisabled(item)) return;
	emit("answer", item, payload);
}

function primaryLabel(item) {
	return interactionPrimaryActionLabel(item, ensureInteractionDraft(item), isSubmitting(item));
}

function rejectLabel(item) {
	return interactionRejectActionLabel(ensureInteractionDraft(item));
}

function switchQueue(delta) {
	if (props.items.length <= 1) return;
	const nextIdx = (activeIndex.value + delta + props.items.length) % props.items.length;
	activeId.value = props.items[nextIdx]?.confirmationId || "";
}

function focusInteraction(interactionId) {
	const target = props.items.find((item) => item?.confirmationId === interactionId);
	if (!target || isExpired(target)) return false;
	activeId.value = interactionId;
	expandPanel();
	const findCard = () => {
		const scopes = [sheetEl.value, rootEl.value, typeof document !== "undefined" ? document : null].filter(Boolean);
		for (const scope of scopes) {
			if (scope.dataset?.interactionId === interactionId) return scope;
			const found = Array.from(scope.querySelectorAll?.("[data-interaction-id]") || [])
				.find((el) => el.dataset?.interactionId === interactionId);
			if (found) return found;
		}
		return null;
	};
	const card = findCard();
	if (card) {
		card.scrollIntoView?.({block: "nearest", inline: "nearest"});
		card.focus?.({preventScroll: true});
		return true;
	}
	nextTick(() => {
		const el = findCard();
		el?.scrollIntoView?.({block: "nearest", inline: "nearest"});
		el?.focus?.({preventScroll: true});
	});
	return true;
}

defineExpose({focusInteraction, collapsePanel, expandPanel});
</script>

<template>
	<div v-if="activeItem" ref="rootEl" class="pi-root" :class="{'is-compact': props.compact}">
		<!-- Collapsed dock pill (both phone and desktop) -->
		<div v-if="collapsed" class="pi-dock-pill" :data-tone="toneOf(activeItem)" :data-interaction-id="activeItem.confirmationId"
		     role="button" tabindex="0" :aria-label="`展开待处理交互：${activeItem.title || '请确认'}`"
		     @click="expandPanel" @keydown.enter.prevent="expandPanel" @keydown.space.prevent="expandPanel">
			<i class="pi-dock-pulse" :class="`tone-${toneOf(activeItem)}`" aria-hidden="true"></i>
			<div class="pi-dock-copy">
				<div class="pi-dock-title">
					<span v-if="props.items.length > 1" class="pi-dock-count">{{ activeIndex + 1 }}/{{ props.items.length }}</span>
					<span>{{ activeItem.title || kindLabelOf(activeItem) }}</span>
				</div>
				<div class="pi-dock-sub">{{ dockSummary(activeItem) }}</div>
			</div>
			<button type="button" class="pi-dock-cta" @click.stop="expandPanel">展开处理</button>
		</div>

		<!-- Expanded sheet: teleported visual-viewport overlay on phone, docked floating card on desktop/compact -->
		<Teleport to="body" :disabled="!isPhoneSheet">
			<div v-if="!collapsed" :class="isPhoneSheet ? 'pi-phone-overlay' : 'pi-desktop-host'">
				<div v-if="isPhoneSheet" class="pi-backdrop" :style="backdropStyle"
				     aria-hidden="true" @click="collapsePanel"></div>

				<section ref="sheetEl" class="pi-sheet"
				         :class="{
				           'is-phone': isPhoneSheet,
				           'is-desktop': !isPhoneSheet,
				           'is-compact': props.compact,
				           'is-expired': isExpired(activeItem),
				         }"
				         :style="isPhoneSheet ? phoneSheetStyle : desktopSheetStyle"
				         :data-tone="toneOf(activeItem)"
				         :data-interaction-id="activeItem.confirmationId"
				         :data-questionnaire-id="actionOf(activeItem) === 'questionnaire' ? activeItem.confirmationId : undefined"
				         tabindex="-1"
				         role="region"
				         :aria-label="activeItem.title || '待处理交互'">

				<!-- Handle bar: pull down on phone, resize height on desktop -->
				<div class="pi-handle-zone"
				     :title="isPhoneSheet ? '下滑收起查看对话' : '拖动调整面板高度'"
				     @pointerdown="isPhoneSheet ? onPhoneDragStart($event) : onDesktopResizeStart($event)"
				     @pointermove="isPhoneSheet ? onPhoneDragMove($event) : undefined"
				     @pointerup="isPhoneSheet ? onPhoneDragEnd($event) : undefined"
				     @pointercancel="isPhoneSheet ? onPhoneDragCancel() : undefined">
					<i class="pi-grip" aria-hidden="true"></i>
				</div>

				<!-- Header -->
				<header class="pi-head"
				        @pointerdown="isPhoneSheet ? onPhoneDragStart($event) : undefined"
				        @pointermove="isPhoneSheet ? onPhoneDragMove($event) : undefined"
				        @pointerup="isPhoneSheet ? onPhoneDragEnd($event) : undefined"
				        @pointercancel="isPhoneSheet ? onPhoneDragCancel() : undefined">
					<div class="pi-head-left">
						<span class="pi-kind-tag" :class="`tone-${toneOf(activeItem)}`">{{ kindLabelOf(activeItem) }}</span>
						<h3 class="pi-title" :title="activeItem.title || ''">{{ activeItem.title || kindLabelOf(activeItem) }}</h3>
					</div>
					<div class="pi-head-right">
						<div v-if="props.items.length > 1" class="pi-queue-nav" role="group" aria-label="切换待处理交互">
							<button type="button" class="pi-queue-btn" aria-label="上一条交互" @click="switchQueue(-1)">
								<ChevronLeft :size="14"/>
							</button>
							<span class="pi-queue-idx">{{ activeIndex + 1 }}/{{ props.items.length }}</span>
							<button type="button" class="pi-queue-btn" aria-label="下一条交互" @click="switchQueue(1)">
								<ChevronRight :size="14"/>
							</button>
						</div>
						<span v-if="expiryText(activeItem)" class="pi-timer" :class="{'is-expired': isExpired(activeItem)}">{{ expiryText(activeItem) }}</span>
						<button type="button" class="pi-fold-btn" @click="collapsePanel" aria-label="收起交互面板查看对话">
							<ChevronDown :size="14" aria-hidden="true"/>
							<span>收起看对话</span>
						</button>
					</div>
				</header>

				<div v-if="isExpired(activeItem)" class="pi-expired-bar" role="status">
					此交互已过期，等待同步最新状态。
				</div>

				<!-- Questionnaire navigation bar (proposal tab + question step pills) -->
				<div v-if="actionOf(activeItem) === 'questionnaire' && (hasLongQuestionnaireBody(activeItem.body) || questionsOf(activeItem).length > 1)"
				     class="pi-step-bar">
					<div ref="stepPillsEl" class="pi-step-pills" role="tablist" aria-label="问卷导航">
						<button v-if="hasLongQuestionnaireBody(activeItem.body)" type="button" role="tab"
						        class="pi-step-pill is-proposal"
						        :class="{'is-current': !ensureUiState(activeItem).listView && ensureUiState(activeItem).tab === 'proposal'}"
						        :aria-selected="!ensureUiState(activeItem).listView && ensureUiState(activeItem).tab === 'proposal'"
						        @click="selectQuestionnaireTab(activeItem, 'proposal')">
							<FileText :size="12" aria-hidden="true"/>
							<span>方案说明</span>
						</button>
						<button v-for="(question, idx) in questionsOf(activeItem)"
						        :key="questionId(question) || idx"
						        type="button" role="tab"
						        class="pi-step-pill"
						        :class="{
						          'is-current': !ensureUiState(activeItem).listView && ensureUiState(activeItem).tab === idx,
						          'is-done': qAnswered(activeItem, question),
						          'has-error': Boolean(qError(activeItem, question)),
						        }"
						        :aria-selected="!ensureUiState(activeItem).listView && ensureUiState(activeItem).tab === idx"
						        @click="selectQuestionnaireTab(activeItem, idx)">
							<span class="pi-step-check">
								<Check v-if="qAnswered(activeItem, question)" :size="10" :stroke-width="2.6" aria-hidden="true"/>
								<template v-else>{{ idx + 1 }}</template>
							</span>
							<span>第 {{ idx + 1 }} 题</span>
						</button>
					</div>
					<button type="button" class="pi-mode-btn"
					        @click="ensureUiState(activeItem).listView = !ensureUiState(activeItem).listView">
						{{ ensureUiState(activeItem).listView ? '分步视图' : '全部展开' }}
					</button>
				</div>

				<!-- Single scrollable body -->
				<div ref="bodyEl" class="pi-body" :style="bodySwipeStyle"
				     @touchstart.passive="onBodyTouchStart"
				     @touchmove.passive="onBodyTouchMove"
				     @touchend="onBodyTouchEnd"
				     @touchcancel="resetBodySwipe">
					<!-- 1. Group proposal -->
					<GroupProposalCard v-if="actionOf(activeItem) === 'group_proposal'"
					                   :item="activeItem"
					                   :disabled="isDisabled(activeItem)"
					                   :submitting="isSubmitting(activeItem)"
					                   :error="itemError(activeItem)"
					                   @answer="answerGroupProposal(activeItem, $event)"/>

					<!-- 2. Questionnaire -->
					<template v-else-if="actionOf(activeItem) === 'questionnaire'">
						<!-- Dedicated proposal reading tab -->
						<template v-if="!ensureUiState(activeItem).listView && hasLongQuestionnaireBody(activeItem.body) && ensureUiState(activeItem).tab === 'proposal'">
							<div class="pi-proposal-banner">
								<strong>方案正文</strong>
								<span>答题时可随时点顶部「方案说明」切回核对</span>
							</div>
							<InteractionMarkdown class="pi-markdown" :text="activeItem.body"/>
						</template>

						<!-- List view or active question step -->
						<template v-else>
							<!-- Proposal collapsible block in list view -->
							<div v-if="ensureUiState(activeItem).listView && hasLongQuestionnaireBody(activeItem.body)" class="pi-inline-proposal">
								<button type="button" class="pi-inline-proposal-head"
								        @click="ensureUiState(activeItem).inlineProposalOpen = !ensureUiState(activeItem).inlineProposalOpen">
									<span><FileText :size="13" aria-hidden="true"/> 方案说明</span>
									<span>{{ ensureUiState(activeItem).inlineProposalOpen ? '收起' : '展开' }}</span>
								</button>
								<div v-if="ensureUiState(activeItem).inlineProposalOpen" class="pi-inline-proposal-body">
									<InteractionMarkdown class="pi-markdown" :text="activeItem.body"/>
								</div>
							</div>

							<!-- Short body note -->
							<InteractionMarkdown
								v-else-if="activeItem.body && !hasLongQuestionnaireBody(activeItem.body) && (ensureUiState(activeItem).listView || ensureUiState(activeItem).tab === 0)"
								class="pi-short-body"
								:text="activeItem.body"/>

							<!-- Questions -->
							<template v-for="(question, qIdx) in questionsOf(activeItem)" :key="questionId(question) || qIdx">
								<div v-if="ensureUiState(activeItem).listView || ensureUiState(activeItem).tab === qIdx"
								     class="pi-q-block"
								     :data-question-id="questionId(question)"
								     :data-question-error="qError(activeItem, question) ? 'true' : 'false'">
									<div class="pi-q-head">
										<h4 class="pi-q-title">
											<span v-if="ensureUiState(activeItem).listView || questionsOf(activeItem).length === 1">{{ qIdx + 1 }}. </span>{{ questionText(question, qIdx) }}
										</h4>
										<span class="pi-q-badge" :class="{'is-req': question.required}">
											{{ question.required ? (question.multiple ? '多选 · 必填' : '必填') : (question.multiple ? '多选 · 选填' : '选填') }}
										</span>
									</div>
									<InteractionMarkdown v-if="question.description" class="pi-q-desc" :text="question.description"/>

									<template v-if="question.type === 'choice'">
										<div class="pi-options">
											<button v-for="(option, optIdx) in question.options || []"
											        :key="`${questionId(question)}-${optIdx}`"
											        type="button"
											        class="pi-opt"
											        :class="{'is-selected': qSelected(activeItem, question, option)}"
											        :disabled="isDisabled(activeItem)"
											        :aria-pressed="qSelected(activeItem, question, option)"
											        @click="onToggleQChoice(activeItem, question, option)">
												<span class="pi-opt-ctrl" :class="{'is-multi': question.multiple}" aria-hidden="true">
													<Check v-if="qSelected(activeItem, question, option)" :size="11" :stroke-width="2.8"/>
												</span>
												<span class="pi-opt-copy">
													<span class="pi-opt-head">
														<span class="pi-opt-label">{{ optionLabel(option) }}</span>
														<span v-if="qIsRecommended(question, option)" class="pi-rec">推荐</span>
													</span>
													<span v-if="optionDescription(option)" class="pi-opt-desc">{{ optionDescription(option) }}</span>
												</span>
											</button>
										</div>

										<div v-if="question.recommendation?.reason" class="pi-rec-reason">
											<b>推荐理由</b>{{ question.recommendation.reason }}
										</div>

										<div class="pi-q-aux-row">
											<button v-if="!ensureUiState(activeItem).questionNoteOpen[questionId(question)] && !qAnswer(activeItem, question).text.trim()"
											        type="button" class="pi-note-toggle" :disabled="isDisabled(activeItem)"
											        @click="openQuestionNote(activeItem, question)">
												＋ {{ qAnswer(activeItem, question).selectedValues.length ? '补充说明或微调本题' : '不选上面，直接写本题自定义答案' }}
											</button>
											<button v-if="!question.multiple && qAnswer(activeItem, question).selectedValues.length"
											        type="button" class="pi-clear-link" :disabled="isDisabled(activeItem)"
											        @click="onClearQChoice(activeItem, question)">
												清除选择
											</button>
										</div>

										<div v-if="ensureUiState(activeItem).questionNoteOpen[questionId(question)] || qAnswer(activeItem, question).text.trim()"
										     class="pi-note-editor">
											<textarea :data-q-note-input="questionId(question)"
											          :value="qAnswer(activeItem, question).text"
											          rows="2"
											          :disabled="isDisabled(activeItem)"
											          placeholder="补充、限制或直接写出你的方案…"
											          @input="onQTextInput(activeItem, question, $event.target.value)"></textarea>
											<span class="pi-micro">本题文字说明优先于已选项；留空则仅按已选项提交。</span>
										</div>
									</template>

									<div v-else class="pi-note-editor">
										<textarea :data-q-note-input="questionId(question)"
										          :value="qAnswer(activeItem, question).text"
										          rows="3"
										          :disabled="isDisabled(activeItem)"
										          :placeholder="question.required ? '请输入你的回答…' : '输入你的补充说明（选填，可留空）…'"
										          @input="onQTextInput(activeItem, question, $event.target.value)"></textarea>
									</div>

									<div v-if="qError(activeItem, question)" class="pi-inline-error" role="alert">
										{{ qError(activeItem, question) }}
									</div>
								</div>
							</template>
						</template>
					</template>

					<!-- 3. Select -->
					<template v-else-if="actionOf(activeItem) === 'select'">
						<InteractionMarkdown v-if="activeItem.body" class="pi-markdown" :text="activeItem.body"/>
						<div class="pi-options">
							<button v-for="(option, idx) in activeItem.options || []"
							        :key="`${activeItem.confirmationId}-${idx}`"
							        type="button"
							        class="pi-opt"
							        :class="{'is-selected': ensureInteractionDraft(activeItem).selectedIndexes.includes(idx)}"
							        :disabled="isDisabled(activeItem)"
							        :aria-pressed="ensureInteractionDraft(activeItem).selectedIndexes.includes(idx)"
							        @click="onToggleSelectOption(activeItem, idx)">
								<span class="pi-opt-ctrl" :class="{'is-multi': activeItem.multiple}" aria-hidden="true">
									<Check v-if="ensureInteractionDraft(activeItem).selectedIndexes.includes(idx)" :size="11" :stroke-width="2.8"/>
								</span>
								<span class="pi-opt-copy">
									<span class="pi-opt-head">
										<span class="pi-opt-label">{{ optionLabel(option) }}</span>
									</span>
									<span v-if="optionDescription(option)" class="pi-opt-desc">{{ optionDescription(option) }}</span>
								</span>
							</button>
						</div>
						<div class="pi-q-aux-row">
							<button v-if="!ensureUiState(activeItem).noteOpen && !ensureInteractionDraft(activeItem).text.trim()"
							        type="button" class="pi-note-toggle" :disabled="isDisabled(activeItem)"
							        @click="onOpenInteractionNote(activeItem)">
								＋ {{ ensureInteractionDraft(activeItem).selectedIndexes.length ? '补充说明、限制或退回讨论' : '不选上面，直接写意见或自定义方案' }}
							</button>
							<button v-if="!activeItem.multiple && ensureInteractionDraft(activeItem).selectedIndexes.length"
							        type="button" class="pi-clear-link" :disabled="isDisabled(activeItem)"
							        @click="onClearSelectChoice(activeItem)">
								清除选择
							</button>
						</div>
						<div v-if="ensureUiState(activeItem).noteOpen || ensureInteractionDraft(activeItem).text.trim()" class="pi-note-editor">
							<textarea data-interaction-note-input
							          :value="ensureInteractionDraft(activeItem).text"
							          rows="2"
							          :disabled="isDisabled(activeItem)"
							          placeholder="写下你的补充限制、修改条件，或直接退回讨论…"
							          @input="onSetInteractionText(activeItem, $event.target.value)"></textarea>
							<span class="pi-micro">
								{{ activeItem.requiresAuthorization ? '确认时将按补充内容授权执行；若尚未决定，请点「仅提意见」。' : '文字说明优先于选项；也可直接点「仅发意见退回」继续讨论。' }}
							</span>
						</div>
					</template>

					<!-- 4. Prompt -->
					<template v-else-if="actionOf(activeItem) === 'prompt'">
						<InteractionMarkdown v-if="activeItem.body" class="pi-markdown" :text="activeItem.body"/>
						<div class="pi-prompt-wrap">
							<div v-if="activeItem.sensitive && !isMultilineSensitivePrompt(activeItem)" class="pi-secret-row">
								<input :type="ensureUiState(activeItem).secretVisible ? 'text' : 'password'"
								       :value="ensureInteractionDraft(activeItem).value"
								       :disabled="isDisabled(activeItem)"
								       autocomplete="off"
								       placeholder="输入敏感内容…"
								       @input="onSetPromptValue(activeItem, $event.target.value)"/>
								<button type="button" class="pi-secret-eye"
								        :aria-label="ensureUiState(activeItem).secretVisible ? '隐藏内容' : '显示内容'"
								        @click="ensureUiState(activeItem).secretVisible = !ensureUiState(activeItem).secretVisible">
									<EyeOff v-if="ensureUiState(activeItem).secretVisible" :size="15"/>
									<Eye v-else :size="15"/>
									<span>{{ ensureUiState(activeItem).secretVisible ? '隐藏' : '显示' }}</span>
								</button>
							</div>
							<textarea v-else
							          :value="ensureInteractionDraft(activeItem).value"
							          rows="3"
							          :disabled="isDisabled(activeItem)"
							          :placeholder="activeItem.sensitive ? '输入敏感内容…' : '请输入你的回答…'"
							          @input="onSetPromptValue(activeItem, $event.target.value)"></textarea>
							<div v-if="activeItem.sensitive" class="pi-secret-shield">
								<ShieldAlert :size="14" aria-hidden="true"/>
								<span>仅通过 Web 端原样提交给当前任务，不发往 Telegram，公开历史自动脱敏。</span>
							</div>
						</div>
					</template>

					<!-- 5. Confirm (UserInteraction confirm / OpenBearControl restart etc.) -->
					<template v-else>
						<InteractionMarkdown v-if="activeItem.body" class="pi-markdown pi-confirm-markdown" :text="activeItem.body"/>
						<div class="pi-q-aux-row">
							<button v-if="!ensureUiState(activeItem).noteOpen && !ensureInteractionDraft(activeItem).text.trim()"
							        type="button" class="pi-note-toggle" :disabled="isDisabled(activeItem)"
							        @click="onOpenInteractionNote(activeItem)">
								＋ 附言修改或仅提意见
							</button>
						</div>
						<div v-if="ensureUiState(activeItem).noteOpen || ensureInteractionDraft(activeItem).text.trim()" class="pi-note-editor">
							<textarea data-interaction-note-input
							          :value="ensureInteractionDraft(activeItem).text"
							          rows="2"
							          :disabled="isDisabled(activeItem)"
							          placeholder="如需调整原操作条件，或想先讨论暂不执行，请在这里说明…"
							          @input="onSetInteractionText(activeItem, $event.target.value)"></textarea>
							<span class="pi-micro">点击主按钮将按补充内容授权执行；若暂不执行，请点「仅提意见」或「拒绝」。</span>
						</div>
					</template>

					<div v-if="itemError(activeItem) && actionOf(activeItem) !== 'group_proposal'" class="pi-submit-error" role="alert">
						{{ itemError(activeItem) }}
					</div>
				</div>

				<!-- Questionnaire overall reply / skip-required feedback bar -->
				<div v-if="actionOf(activeItem) === 'questionnaire'" class="pi-overall-bar">
					<button v-if="!ensureUiState(activeItem).overallOpen && !ensureUiState(activeItem).overallText.trim()"
					        type="button" class="pi-overall-trigger" :disabled="isDisabled(activeItem)"
					        @click="openOverallReply(activeItem)">
						<span class="pi-overall-trigger-text">
							<MessageSquareText :size="14" aria-hidden="true"/>
							<span>不想现在填，直接写总意见退回讨论…</span>
						</span>
						<span class="pi-overall-badge">免填必填项</span>
					</button>
					<div v-else class="pi-overall-box">
						<div class="pi-overall-head">
							<span>总回复 / 先讨论再决定</span>
							<button type="button" class="pi-clear-link" :disabled="isDisabled(activeItem)"
							        @click="closeOverallReply(activeItem)">
								{{ ensureUiState(activeItem).overallText.trim() ? '清空并收起' : '收起' }}
							</button>
						</div>
						<textarea data-overall-reply-input
						          :value="ensureUiState(activeItem).overallText"
						          rows="2"
						          :disabled="isDisabled(activeItem)"
						          placeholder="写下你的整体想法或疑问，可直接发给模型先讨论，无需被迫选完必填题…"
						          @input="ensureUiState(activeItem).overallText = $event.target.value; clearLocalError(activeItem)"></textarea>
						<div class="pi-overall-hint">
							<template v-if="ensureUiState(activeItem).overallText.trim()">
								<template v-if="qProgress(activeItem).requiredMissing > 0">
									已填写总意见：可直接点右下角<strong>「发意见并退回问卷」</strong>，无需完成必填题。
								</template>
								<template v-else>
									必填题也已完成：可选<strong>「仅发意见退回」</strong>，或<strong>「附带总说明提交」</strong>。
								</template>
							</template>
							<template v-else>
								写下疑问或修改意见后，可直接退回问卷继续讨论，不会按未完成选项执行。
							</template>
						</div>
					</div>
				</div>

				<!-- Footer actions (group_proposal renders its own footer inside GroupProposalCard) -->
				<footer v-if="actionOf(activeItem) !== 'group_proposal'" class="pi-foot">
					<!-- Questionnaire footer -->
					<template v-if="actionOf(activeItem) === 'questionnaire'">
						<div class="pi-foot-left">
							<button type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="submitQuestionnaire(activeItem, 'cancel')">
								{{ activeItem.cancelText || '暂不回答' }}
							</button>
						</div>
						<div class="pi-foot-right">
							<template v-if="ensureUiState(activeItem).overallText.trim()">
								<template v-if="qProgress(activeItem).requiredMissing === 0">
									<button type="button" class="pi-btn is-secondary" :disabled="isDisabled(activeItem)"
									        @click="submitQuestionnaire(activeItem, 'feedback')">
										仅发意见退回
									</button>
									<button type="button" class="pi-btn is-primary" :disabled="isDisabled(activeItem)"
									        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
									        @click="submitQuestionnaire(activeItem, 'submit')">
										{{ isSubmitting(activeItem) ? '提交中…' : '附带总说明提交' }}
									</button>
								</template>
								<button v-else type="button" class="pi-btn is-warning" :disabled="isDisabled(activeItem)"
								        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
								        @click="submitQuestionnaire(activeItem, 'feedback')">
									{{ isSubmitting(activeItem) ? '发送中…' : '发意见并退回问卷' }}
								</button>
							</template>

							<template v-else-if="!ensureUiState(activeItem).listView && hasLongQuestionnaireBody(activeItem.body) && ensureUiState(activeItem).tab === 'proposal'">
								<button type="button" class="pi-btn is-primary"
								        @click="selectQuestionnaireTab(activeItem, 0)">
									看完方案，开始回答（{{ questionsOf(activeItem).length }} 题）
								</button>
							</template>

							<template v-else>
								<button v-if="!ensureUiState(activeItem).listView && (ensureUiState(activeItem).tab > 0 || (ensureUiState(activeItem).tab === 0 && hasLongQuestionnaireBody(activeItem.body)))"
								        type="button" class="pi-btn is-secondary"
								        @click="stepQuestionnaireTab(activeItem, -1)">
									{{ ensureUiState(activeItem).tab === 0 ? '回看方案' : '上一题' }}
								</button>

								<button v-if="!ensureUiState(activeItem).listView && typeof ensureUiState(activeItem).tab === 'number' && ensureUiState(activeItem).tab < questionsOf(activeItem).length - 1"
								        type="button" class="pi-btn is-primary"
								        @click="stepQuestionnaireTab(activeItem, 1)">
									下一题（{{ ensureUiState(activeItem).tab + 1 }}/{{ questionsOf(activeItem).length }}）
								</button>
								<button v-else-if="qProgress(activeItem).requiredMissing > 0"
								        type="button" class="pi-btn is-secondary"
								        :disabled="isDisabled(activeItem)"
								        @click="jumpToMissingRequired(activeItem)">
									还差 {{ qProgress(activeItem).requiredMissing }} 项必填
								</button>
								<button v-else type="button" class="pi-btn is-primary"
								        :disabled="isDisabled(activeItem)"
								        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
								        @click="submitQuestionnaire(activeItem, 'submit')">
									{{ isSubmitting(activeItem) ? '提交中…' : `提交回答（${qProgress(activeItem).answered}/${qProgress(activeItem).total}）` }}
								</button>
							</template>
						</div>
					</template>

					<!-- Confirm footer -->
					<template v-else-if="actionOf(activeItem) === 'confirm'">
						<div class="pi-foot-left">
							<button type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'cancel')">
								{{ activeItem.cancelText || '暂不回答' }}
							</button>
							<button v-if="hasSubmittedInteractionText(ensureInteractionDraft(activeItem).text)"
							        type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'feedback')">
								仅提意见
							</button>
						</div>
						<div class="pi-foot-right">
							<button type="button" class="pi-btn is-secondary" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'reject')">
								{{ rejectLabel(activeItem) }}
							</button>
							<button type="button" class="pi-btn"
							        :class="toneOf(activeItem) === 'danger' ? 'is-danger' : 'is-primary'"
							        :disabled="isDisabled(activeItem)"
							        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
							        @click="answerSimpleInteraction(activeItem, 'confirm')">
								{{ primaryLabel(activeItem) }}
							</button>
						</div>
					</template>

					<!-- Select footer -->
					<template v-else-if="actionOf(activeItem) === 'select'">
						<div class="pi-foot-left">
							<button type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'cancel')">
								{{ activeItem.cancelText || '暂不回答' }}
							</button>
							<button v-if="hasSubmittedInteractionText(ensureInteractionDraft(activeItem).text) && (!activeItem.requiresAuthorization || ensureInteractionDraft(activeItem).selectedIndexes.length)"
							        type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'feedback')">
								{{ activeItem.requiresAuthorization ? '仅提意见' : '仅发意见退回' }}
							</button>
						</div>
						<div class="pi-foot-right">
							<button type="button" class="pi-btn is-primary"
							        :disabled="isDisabled(activeItem)"
							        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
							        @click="answerSimpleInteraction(activeItem, 'confirm')">
								{{ primaryLabel(activeItem) }}
							</button>
						</div>
					</template>

					<!-- Prompt footer -->
					<template v-else>
						<div class="pi-foot-left">
							<button type="button" class="pi-btn is-ghost" :disabled="isDisabled(activeItem)"
							        @click="answerSimpleInteraction(activeItem, 'cancel')">
								{{ activeItem.cancelText || '取消' }}
							</button>
						</div>
						<div class="pi-foot-right">
							<button type="button" class="pi-btn is-primary"
							        :disabled="isDisabled(activeItem)"
							        :aria-busy="isSubmitting(activeItem) ? 'true' : 'false'"
							        @click="answerSimpleInteraction(activeItem, 'confirm')">
								{{ primaryLabel(activeItem) }}
							</button>
						</div>
					</template>
				</footer>
				</section>
			</div>
		</Teleport>
	</div>
</template>

<style scoped>
@import "./userInteractionTokens.css";

.pi-root {
	margin-bottom: 0.72rem;
}

/* ---------- Collapsed dock pill ---------- */
.pi-dock-pill {
	display: flex;
	align-items: center;
	gap: 10px;
	min-height: 44px;
	padding: 7px 10px 7px 12px;
	border-radius: 13px;
	border: 1px solid var(--ob-border-strong);
	background: var(--ob-surface);
	box-shadow: var(--ob-shadow-panel);
	cursor: pointer;
	transition: border-color 0.14s ease, background-color 0.14s ease;
}
.pi-dock-pill:hover {
	background: var(--ob-surface-raised);
}
.pi-dock-pill:focus-visible {
	outline: 2px solid var(--ob-blue);
	outline-offset: 2px;
}
.pi-dock-pulse {
	width: 8px;
	height: 8px;
	border-radius: 50%;
	flex: none;
	background: var(--ob-blue);
	box-shadow: 0 0 0 3px var(--ob-blue-soft);
}
.pi-dock-pulse.tone-danger {
	background: var(--ob-danger);
	box-shadow: 0 0 0 3px var(--ob-danger-soft);
}
.pi-dock-pulse.tone-warning {
	background: var(--ob-warning);
	box-shadow: 0 0 0 3px var(--ob-warning-soft);
}
.pi-dock-pulse.tone-success {
	background: var(--ob-success);
	box-shadow: 0 0 0 3px var(--ob-success-soft);
}
.pi-dock-copy {
	flex: 1 1 auto;
	min-width: 0;
}
.pi-dock-title {
	display: flex;
	align-items: center;
	gap: 6px;
	font-size: 13px;
	font-weight: 600;
	color: var(--ob-text-strong);
	line-height: 1.3;
}
.pi-dock-title > span:last-child {
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.pi-dock-count {
	flex: none;
	padding: 0 5px;
	border-radius: 5px;
	background: var(--ob-surface-soft);
	font-size: 11px;
	font-variant-numeric: tabular-nums;
	color: var(--ob-text-subtle);
}
.pi-dock-sub {
	margin-top: 1px;
	font-size: 11.5px;
	color: var(--ob-text-subtle);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.pi-dock-cta {
	flex: none;
	height: 30px;
	padding: 0 11px;
	border: 0;
	border-radius: 8px;
	background: var(--ob-chat-button, var(--ob-text-strong));
	color: var(--ob-chat-button-text, var(--ob-text-inverse));
	font-size: 12px;
	font-weight: 600;
	cursor: pointer;
	white-space: nowrap;
}

/* ---------- Phone visual-viewport overlay & backdrop ---------- */
.pi-phone-overlay {
	position: fixed;
	left: 0;
	right: 0;
	top: var(--mobile-viewport-top, 0px);
	height: var(--mobile-viewport-height, 100dvh);
	z-index: 2100;
	display: flex;
	flex-direction: column;
	justify-content: flex-end;
	overflow: hidden;
}
.pi-backdrop {
	position: absolute;
	inset: 0;
	z-index: 0;
	background: var(--ob-mask, rgb(0 0 0 / 0.36));
	transition: opacity 0.2s ease;
}

/* ---------- Main sheet container ---------- */
.pi-sheet {
	--ob-interaction-accent: var(--ob-blue);
	position: relative;
	display: flex;
	flex-direction: column;
	background: var(--ob-surface);
	color: var(--ob-text);
	overflow: hidden;
	outline: none;
}
.pi-sheet:focus {
	outline: none;
}
.pi-sheet:focus-visible {
	box-shadow: inset 0 0 0 2px var(--ob-blue);
}
.pi-sheet.is-desktop {
	position: relative;
	border: 1px solid var(--ob-border-strong);
	border-radius: 16px;
	box-shadow: var(--ob-shadow-popover, var(--ob-shadow-panel));
	max-height: min(62dvh, 520px);
}
.pi-sheet.is-desktop.is-compact {
	border-radius: 13px;
	max-height: min(56dvh, 420px);
}
.pi-sheet.is-phone {
	position: relative;
	z-index: 1;
	width: 100%;
	border-radius: 20px 20px 0 0;
	border-top: 1px solid var(--ob-border-strong);
	box-shadow: 0 -10px 36px rgb(var(--ob-shadow-rgb, 0 0 0) / 0.22), 0 -1px 4px rgb(var(--ob-shadow-rgb, 0 0 0) / 0.10);
	max-height: calc(100% - max(16px, env(safe-area-inset-top, 0px)) - 44px);
	will-change: transform;
}
.pi-sheet.is-expired {
	opacity: 0.82;
}

/* ---------- Drag handle ---------- */
.pi-handle-zone {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: center;
	height: 18px;
	cursor: ns-resize;
	touch-action: none;
	user-select: none;
}
.pi-sheet.is-phone .pi-handle-zone {
	height: 24px;
	cursor: grab;
}
.pi-grip {
	width: 36px;
	height: 4px;
	border-radius: 99px;
	background: var(--ob-border-strong);
	transition: width 0.14s ease, background-color 0.14s ease;
}
.pi-handle-zone:hover .pi-grip {
	width: 48px;
	background: var(--ob-text-muted);
}

/* ---------- Header ---------- */
.pi-head {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 0 14px 10px;
	border-bottom: 1px solid var(--ob-border-soft);
	touch-action: none;
}
.pi-head-left {
	display: flex;
	align-items: center;
	gap: 8px;
	min-width: 0;
	flex: 1 1 auto;
}
.pi-kind-tag {
	flex: none;
	display: inline-flex;
	align-items: center;
	padding: 2px 7px;
	border-radius: 6px;
	font-size: 11px;
	font-weight: 600;
	background: var(--ob-surface-soft);
	color: var(--ob-text-subtle);
	white-space: nowrap;
}
.pi-kind-tag.tone-danger {
	background: var(--ob-danger-soft);
	color: var(--ob-danger);
}
.pi-kind-tag.tone-warning {
	background: var(--ob-warning-soft);
	color: var(--ob-warning);
}
.pi-kind-tag.tone-success {
	background: var(--ob-success-soft);
	color: var(--ob-success);
}
.pi-title {
	margin: 0;
	min-width: 0;
	font-size: 14px;
	font-weight: 650;
	line-height: 1.35;
	color: var(--ob-text-strong);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.pi-head-right {
	flex: none;
	display: flex;
	align-items: center;
	gap: 6px;
}
.pi-queue-nav {
	display: inline-flex;
	align-items: center;
	gap: 2px;
	padding: 1px;
	border-radius: 7px;
	background: var(--ob-surface-raised);
	border: 1px solid var(--ob-border);
}
.pi-queue-btn {
	display: grid;
	place-items: center;
	width: 22px;
	height: 22px;
	border: 0;
	border-radius: 5px;
	background: transparent;
	color: var(--ob-text-subtle);
	cursor: pointer;
}
.pi-queue-btn:hover {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}
.pi-queue-idx {
	padding: 0 3px;
	font-size: 11px;
	font-variant-numeric: tabular-nums;
	color: var(--ob-text-subtle);
}
.pi-timer {
	font-size: 11px;
	font-variant-numeric: tabular-nums;
	color: var(--ob-text-muted);
	white-space: nowrap;
}
.pi-timer.is-expired {
	color: var(--ob-danger);
	font-weight: 600;
}
.pi-fold-btn {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	height: 28px;
	padding: 0 9px;
	border-radius: 7px;
	border: 1px solid var(--ob-border);
	background: var(--ob-surface-raised);
	color: var(--ob-text-subtle);
	font-size: 11.5px;
	font-weight: 500;
	cursor: pointer;
	white-space: nowrap;
}
.pi-fold-btn:hover {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}
.pi-expired-bar {
	flex: none;
	padding: 6px 14px;
	background: var(--ob-danger-soft);
	color: var(--ob-danger);
	font-size: 12px;
	border-bottom: 1px solid rgb(var(--ob-danger-rgb) / 0.16);
}

/* ---------- Stepper bar ---------- */
.pi-step-bar {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 7px 14px;
	background: var(--ob-surface-raised);
	border-bottom: 1px solid var(--ob-border-soft);
}
.pi-step-pills {
	display: flex;
	align-items: center;
	gap: 5px;
	flex: 1 1 auto;
	min-width: 0;
	padding-right: 12px;
	overflow-x: auto;
	scrollbar-width: none;
	-webkit-mask-image: linear-gradient(to right, #000 calc(100% - 14px), transparent 100%);
	mask-image: linear-gradient(to right, #000 calc(100% - 14px), transparent 100%);
}
.pi-step-pills::-webkit-scrollbar {
	display: none;
}
.pi-step-pill {
	flex: none;
	display: inline-flex;
	align-items: center;
	gap: 5px;
	height: 28px;
	padding: 0 9px;
	border-radius: 7px;
	border: 1px solid transparent;
	background: transparent;
	color: var(--ob-text-subtle);
	font-size: 12px;
	font-weight: 500;
	cursor: pointer;
	white-space: nowrap;
}
.pi-step-pill:hover {
	background: var(--ob-hover);
	color: var(--ob-text);
}
.pi-step-pill.is-current {
	background: var(--ob-surface);
	border-color: var(--ob-border-strong);
	color: var(--ob-text-strong);
	font-weight: 600;
	box-shadow: 0 1px 2px rgb(var(--ob-shadow-rgb, 0 0 0) / 0.06);
}
.pi-step-pill.has-error {
	color: var(--ob-danger);
}
.pi-step-pill.is-proposal svg {
	color: var(--ob-blue);
}
.pi-step-check {
	width: 15px;
	height: 15px;
	border-radius: 50%;
	display: inline-grid;
	place-items: center;
	font-size: 10px;
	font-variant-numeric: tabular-nums;
	background: var(--ob-border-soft);
	color: var(--ob-text-subtle);
}
.pi-step-pill.is-done .pi-step-check {
	background: var(--ob-success-soft);
	color: var(--ob-success);
}
.pi-step-pill.is-current .pi-step-check {
	background: var(--ob-chat-button, var(--ob-text-strong));
	color: var(--ob-chat-button-text, var(--ob-text-inverse));
}
.pi-mode-btn {
	flex: none;
	border: 0;
	background: transparent;
	padding: 4px 7px;
	border-radius: 6px;
	font-size: 11.5px;
	color: var(--ob-text-subtle);
	cursor: pointer;
	white-space: nowrap;
}
.pi-mode-btn:hover {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}

/* ---------- Single scrollable body ---------- */
.pi-body {
	flex: 1 1 auto;
	min-height: 0;
	overflow-y: auto;
	overscroll-behavior: contain;
	padding: 12px 14px 10px;
	display: flex;
	flex-direction: column;
	gap: 8px;
	scrollbar-width: thin;
}
.pi-proposal-banner {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 4px 8px;
	padding: 7px 10px;
	border-radius: 9px;
	background: var(--ob-surface-raised);
	border: 1px solid var(--ob-border-soft);
	font-size: 11.5px;
	color: var(--ob-text-subtle);
}
.pi-proposal-banner strong {
	color: var(--ob-text-strong);
	font-weight: 600;
}
.pi-inline-proposal {
	border: 1px solid var(--ob-border);
	border-radius: 11px;
	background: var(--ob-surface-raised);
	overflow: hidden;
}
.pi-inline-proposal-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	width: 100%;
	border: 0;
	background: transparent;
	padding: 9px 12px;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--ob-text-strong);
	cursor: pointer;
}
.pi-inline-proposal-head > span:first-child {
	display: inline-flex;
	align-items: center;
	gap: 6px;
}
.pi-inline-proposal-head > span:last-child {
	font-size: 11.5px;
	font-weight: 500;
	color: var(--ob-text-subtle);
}
.pi-inline-proposal-body {
	padding: 8px 12px 12px;
	border-top: 1px solid var(--ob-border-soft);
}
.pi-markdown,
.pi-short-body {
	font-size: 13px;
	line-height: 1.62;
	color: var(--ob-text);
}
.pi-short-body {
	color: var(--ob-text-subtle);
}
.pi-confirm-markdown {
	padding: 10px 12px;
	border-radius: 10px;
	background: var(--ob-surface-raised);
	border: 1px solid var(--ob-border-soft);
}
.pi-confirm-markdown :deep(p:last-child) {
	margin-bottom: 0;
}

/* ---------- Questions & options ---------- */
.pi-q-block {
	display: flex;
	flex-direction: column;
	gap: 8px;
}
.pi-q-block + .pi-q-block {
	padding-top: 12px;
	border-top: 1px solid var(--ob-border-soft);
}
.pi-q-head {
	display: flex;
	align-items: baseline;
	justify-content: space-between;
	gap: 8px;
}
.pi-q-title {
	margin: 0;
	font-size: 13.5px;
	font-weight: 600;
	line-height: 1.48;
	color: var(--ob-text-strong);
	overflow-wrap: anywhere;
}
.pi-q-badge {
	flex: none;
	font-size: 11px;
	color: var(--ob-text-muted);
	white-space: nowrap;
}
.pi-q-badge.is-req {
	color: var(--ob-text-subtle);
}
.pi-q-desc {
	margin-top: -2px;
	font-size: 12px;
	line-height: 1.5;
	color: var(--ob-text-subtle);
}
.pi-options {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.pi-opt {
	display: flex;
	align-items: flex-start;
	gap: 10px;
	width: 100%;
	padding: 10px 12px;
	border-radius: 11px;
	border: 1px solid var(--ob-border);
	background: var(--ob-surface);
	text-align: left;
	cursor: pointer;
	transition: border-color 0.14s ease, background-color 0.14s ease;
}
.pi-opt:hover:not(:disabled) {
	background: var(--ob-hover);
	border-color: var(--ob-border-strong);
}
.pi-opt.is-selected {
	border-color: var(--ob-chat-button, var(--ob-text-strong));
	background: var(--ob-selected);
	box-shadow: inset 0 0 0 1px rgb(var(--ob-border-rgb) / 0.08);
}
.pi-opt:disabled {
	opacity: 0.56;
	cursor: not-allowed;
}
.pi-opt-ctrl {
	flex: none;
	width: 17px;
	height: 17px;
	margin-top: 2px;
	border-radius: 50%;
	border: 1.5px solid var(--ob-border-strong);
	background: var(--ob-surface);
	display: grid;
	place-items: center;
	transition: border-color 0.14s ease, background-color 0.14s ease;
}
.pi-opt-ctrl.is-multi {
	border-radius: 5px;
}
.pi-opt.is-selected .pi-opt-ctrl {
	border-color: var(--ob-chat-button, var(--ob-text-strong));
	background: var(--ob-chat-button, var(--ob-text-strong));
	color: var(--ob-chat-button-text, var(--ob-text-inverse));
}
.pi-opt-copy {
	flex: 1 1 auto;
	min-width: 0;
}
.pi-opt-head {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 6px;
}
.pi-opt-label {
	font-size: 13px;
	font-weight: 550;
	line-height: 1.4;
	color: var(--ob-text-strong);
	overflow-wrap: anywhere;
}
.pi-rec {
	font-size: 10.5px;
	font-weight: 600;
	padding: 1px 6px;
	border-radius: 5px;
	background: var(--ob-success-soft);
	color: var(--ob-success);
}
.pi-opt-desc {
	display: block;
	margin-top: 2px;
	font-size: 12px;
	line-height: 1.45;
	color: var(--ob-text-subtle);
	overflow-wrap: anywhere;
}
.pi-rec-reason {
	padding: 7px 10px;
	border-radius: 9px;
	background: var(--ob-surface-raised);
	font-size: 11.5px;
	line-height: 1.5;
	color: var(--ob-text-subtle);
	white-space: pre-wrap;
}
.pi-rec-reason b {
	color: var(--ob-text);
	font-weight: 600;
	margin-right: 5px;
}
.pi-q-aux-row {
	display: flex;
	align-items: center;
	justify-content: space-between;
	flex-wrap: wrap;
	gap: 8px;
}
.pi-note-toggle {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	border: 0;
	background: transparent;
	padding: 4px 2px;
	font-size: 12px;
	color: var(--ob-text-subtle);
	cursor: pointer;
}
.pi-note-toggle:hover:not(:disabled) {
	color: var(--ob-text-strong);
}
.pi-clear-link {
	border: 0;
	background: transparent;
	padding: 2px 4px;
	font-size: 11.5px;
	color: var(--ob-text-muted);
	cursor: pointer;
}
.pi-clear-link:hover:not(:disabled) {
	color: var(--ob-text-strong);
}
.pi-note-editor,
.pi-prompt-wrap {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.pi-note-editor textarea,
.pi-prompt-wrap textarea,
.pi-prompt-wrap input,
.pi-overall-box textarea {
	width: 100%;
	border: 1px solid var(--ob-border-strong);
	border-radius: 10px;
	background: var(--ob-surface-raised);
	padding: 8px 11px;
	font: inherit;
	font-size: 13px;
	line-height: 1.5;
	color: var(--ob-text-strong);
	outline: none;
	resize: vertical;
}
.pi-note-editor textarea:focus,
.pi-prompt-wrap textarea:focus,
.pi-prompt-wrap input:focus,
.pi-overall-box textarea:focus {
	border-color: var(--ob-chat-button, var(--ob-text-strong));
	background: var(--ob-surface);
}
.pi-micro {
	font-size: 11px;
	line-height: 1.45;
	color: var(--ob-text-muted);
}
.pi-secret-row {
	display: flex;
	align-items: center;
	gap: 6px;
}
.pi-secret-row input {
	flex: 1 1 auto;
	min-width: 0;
}
.pi-secret-eye {
	flex: none;
	display: inline-flex;
	align-items: center;
	gap: 4px;
	height: 38px;
	padding: 0 10px;
	border-radius: 10px;
	border: 1px solid var(--ob-border);
	background: var(--ob-surface-raised);
	color: var(--ob-text-subtle);
	font-size: 12px;
	cursor: pointer;
	white-space: nowrap;
}
.pi-secret-eye:hover {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}
.pi-secret-shield {
	display: flex;
	align-items: flex-start;
	gap: 6px;
	padding: 7px 10px;
	border-radius: 9px;
	background: var(--ob-surface-raised);
	font-size: 11.5px;
	line-height: 1.45;
	color: var(--ob-text-subtle);
}
.pi-secret-shield svg {
	flex: none;
	margin-top: 2px;
	color: var(--ob-warning);
}
.pi-inline-error,
.pi-submit-error {
	padding: 7px 10px;
	border-radius: 9px;
	border: 1px solid rgb(var(--ob-danger-rgb) / 0.24);
	background: var(--ob-danger-soft);
	color: var(--ob-danger);
	font-size: 12px;
	line-height: 1.45;
}

/* ---------- Questionnaire overall feedback bar ---------- */
.pi-overall-bar {
	flex: none;
	border-top: 1px solid var(--ob-border-soft);
	background: var(--ob-surface-raised);
	padding: 7px 14px;
}
.pi-overall-trigger {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	width: 100%;
	border: 0;
	background: transparent;
	padding: 3px 0;
	font-size: 12px;
	color: var(--ob-text-subtle);
	cursor: pointer;
	text-align: left;
}
.pi-overall-trigger:hover:not(:disabled) {
	color: var(--ob-text-strong);
}
.pi-overall-trigger-text {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	min-width: 0;
}
.pi-overall-badge {
	flex: none;
	font-size: 11px;
	padding: 1px 7px;
	border-radius: 99px;
	background: var(--ob-surface);
	border: 1px solid var(--ob-border);
	color: var(--ob-text-muted);
	white-space: nowrap;
}
.pi-overall-box {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.pi-overall-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	font-size: 12px;
	font-weight: 600;
	color: var(--ob-text-strong);
}
.pi-overall-hint {
	font-size: 11px;
	line-height: 1.45;
	color: var(--ob-text-subtle);
}
.pi-overall-hint strong {
	color: var(--ob-warning);
	font-weight: 600;
}

/* ---------- Footer ---------- */
.pi-foot {
	flex: none;
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 10px 14px;
	border-top: 1px solid var(--ob-border-soft);
	background: var(--ob-surface);
}
.pi-sheet.is-phone .pi-foot {
	padding-top: 10px;
	padding-bottom: max(12px, calc(env(safe-area-inset-bottom, 0px) - 8px));
}
:global(html[data-openbear-keyboard="open"]) .pi-sheet.is-phone .pi-foot {
	padding-bottom: 10px;
}
.pi-foot-left,
.pi-foot-right {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 6px;
}
.pi-foot-right {
	margin-left: auto;
}
.pi-btn {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 5px;
	min-height: 34px;
	padding: 0 13px;
	border-radius: 9px;
	border: 1px solid transparent;
	font-size: 12.5px;
	font-weight: 600;
	line-height: 1.2;
	cursor: pointer;
	white-space: nowrap;
}
.pi-btn.is-ghost {
	background: transparent;
	color: var(--ob-text-subtle);
	padding: 0 9px;
}
.pi-btn.is-ghost:hover:not(:disabled) {
	background: var(--ob-hover);
	color: var(--ob-text-strong);
}
.pi-btn.is-secondary {
	border-color: var(--ob-border-strong);
	background: var(--ob-surface);
	color: var(--ob-text-strong);
}
.pi-btn.is-secondary:hover:not(:disabled) {
	background: var(--ob-hover);
}
.pi-btn.is-primary {
	background: var(--ob-chat-button, var(--ob-text-strong));
	color: var(--ob-chat-button-text, var(--ob-text-inverse));
}
.pi-btn.is-primary:hover:not(:disabled) {
	opacity: 0.9;
}
.pi-btn.is-warning {
	background: var(--ob-warning);
	color: #fff;
}
.pi-btn.is-warning:hover:not(:disabled) {
	opacity: 0.92;
}
.pi-btn.is-danger {
	background: var(--ob-danger);
	color: #fff;
}
.pi-btn.is-danger:hover:not(:disabled) {
	opacity: 0.92;
}
.pi-btn:disabled {
	opacity: 0.48;
	cursor: not-allowed;
}

/* ---------- Mobile touch / iOS keyboard guard ---------- */
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.pi-note-editor textarea,
	.pi-prompt-wrap textarea,
	.pi-prompt-wrap input,
	.pi-overall-box textarea {
		font-size: 16px;
	}
	.pi-btn {
		min-height: 38px;
	}
	.pi-fold-btn {
		height: 32px;
	}
	.pi-step-pill {
		height: 32px;
	}
}

@media (max-width: 420px) {
	.pi-timer {
		display: none;
	}
	.pi-fold-btn span {
		font-size: 11px;
	}
}
</style>
