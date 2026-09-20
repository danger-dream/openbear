<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch} from "vue";
import ReferenceEditor from "../../references/ReferenceEditor.vue";
import ReferencePlainText from "../../references/ReferencePlainText.vue";
import InteractionMarkdown from "./InteractionMarkdown.vue";
import ContextCompactionIcon from "./legacy/ContextCompactionIcon.vue";
import ModelFeatureIcon from "./ModelFeatureIcon.vue";
import {
	ArrowDown,
	CircleCheck,
	Close,
	Document,
	Paperclip,
	Plus,
	Promotion,
	Search,
	SemiSelect,
	Timer,
	Warning,
} from "@element-plus/icons-vue";
import {
	fmtBytes,
	fmtTokens,
	modelDefaultThinking,
	modelLabel,
	modelShortLabel,
	thinkingLabel,
} from "./display.js";
import {
	buildQuestionnaireAnswer,
	clearQuestionChoice,
	createQuestionnaireDraft,
	ensureQuestionAnswer,
	isQuestionChoiceSelected,
	toggleQuestionChoice,
	validateQuestionnaire,
} from "./questionnaireState.js";
import {
	buildInteractionAnswer,
	clearInteractionSelection,
	createInteractionDraft,
	interactionExpiryLabel,
	interactionPrimaryActionLabel,
	interactionRejectActionLabel,
	isInteractionExpired,
	toggleInteractionOption,
	validateInteractionDraft,
} from "./userInteractionState.js";

const props = defineProps({
	draft: {type: String, default: ""},
	conversationUuid: {type: String, default: ""},
	pendingAttachments: {type: Array, default: () => []},
	attachmentPreviews: {type: Object, default: () => ({})},
	pendingConfirmations: {type: Array, default: () => []},
	confirmationSubmitting: {type: Object, default: () => ({})},
	confirmationErrors: {type: Object, default: () => ({})},
	pendingSteering: {type: Array, default: () => []},
	modelMenuOpen: {type: Boolean, default: false},
	modelGroups: {type: Array, default: () => []},
	currentModel: {type: String, default: ""},
	currentModelInfo: {type: Object, default: null},
	currentThinkLevels: {type: Array, default: () => []},
	effectiveThinking: {type: String, default: "off"},
	supportsThinking: {type: Boolean, default: false},
	currentFast: {type: Boolean, default: false},
	fastSupported: {type: Boolean, default: false},
	agentModel: {type: String, default: ""},
	agentThinkLevel: {type: String, default: ""},
	agentFastMode: {default: null},
	agentEffectiveModel: {type: String, default: ""},
	agentEffectiveThinking: {type: String, default: "off"},
	agentEffectiveFast: {type: Boolean, default: false},
	agentThinkLevels: {type: Array, default: () => []},
	agentSupportsThinking: {type: Boolean, default: false},
	agentFastSupported: {type: Boolean, default: false},
	agentDefaultThinkingLabel: {type: String, default: "模型默认"},
	running: {type: Boolean, default: false},
	canSend: {type: Boolean, default: false},
	modelQuery: {type: String, default: ""},
	contextStrategy: {type: String, default: "sliding_window"},
	strategySaving: {type: Boolean, default: false},
	canCompact: {type: Boolean, default: false},
	compacting: {type: Boolean, default: false},
	contextDisplay: {type: String, default: "—"},
	contextUsedDisplay: {type: String, default: "—"},
	contextThresholdDisplay: {type: String, default: "—"},
	contextWindowDisplay: {type: String, default: "—"},
	contextPercentDisplay: {type: String, default: "—"},
	costText: {type: String, default: "$0.0000"},
});
const emit = defineEmits([
	"update:draft",
	"update:modelQuery",
	"attachment-change",
	"remove-attachment",
	"clear-draft",
	"new-session",
	"toggle-model-menu",
	"select-model",
	"select-context-strategy",
	"compact",
	"select-thinking",
	"toggle-fast-mode",
	"select-agent-model",
	"select-agent-thinking",
	"select-agent-fast",
	"send",
	"stop",
	"answer-confirmation",
	"close-menus",
	"height-change",
]);

const fileInput = ref(null);
const composerShell = ref(null);
const composerTextarea = ref(null);
let composerResizeObserver = null;
const interactionDrafts = ref({});
const interactionErrors = ref({});
const questionnaireDrafts = ref({});
const questionnaireErrors = ref({});
const interactionNowMs = ref(Date.now());
let interactionClockTimer = null;
const runConfigTab = ref("main"); // main | agent
const DEFAULT_WINDOW_TRIGGER_RATIO = 0.7;
const modelDetailId = `model-detail-${useId()}`;
const activeModelDetail = ref(null);

function clearModelDetail() {
	activeModelDetail.value = null;
}

function showModelFeature(model, feature, event) {
	if (!props.modelMenuOpen || (event.pointerType === 'touch' && event.type !== 'click')) return;
	if (event.type === 'focus' && !event.currentTarget.matches(':focus-visible')) return;
	// The hint occupies a fixed footer slot, never a layer over model rows.
	activeModelDetail.value = {modelKey: model.key, feature};
}

watch([() => props.modelMenuOpen, runConfigTab, () => props.modelQuery, () => props.modelGroups, () => props.conversationUuid], clearModelDetail, {flush: 'sync'});
const runConfigPopoverVisible = computed({
	get: () => props.modelMenuOpen,
	set: (value) => {
		if (value && !props.modelMenuOpen) emit("toggle-model-menu");
		else if (!value && props.modelMenuOpen) {
			clearModelDetail();
			emit("close-menus");
		}
	},
});
const runConfigModelText = computed(() => props.currentModelInfo ? modelShortLabel(props.currentModelInfo) : "模型");
const runConfigStrategyText = computed(() => props.contextStrategy === 'model_summary' ? '模型摘要' : '滑动窗口');
const runConfigMetaText = computed(() => {
	const parts = [];
	if (props.supportsThinking) parts.push(thinkingLabel(props.effectiveThinking));
	if (props.currentFast) parts.push("Fast");
	if (props.contextDisplay && props.contextDisplay !== "—") parts.push(props.contextDisplay);
	return parts.join(" · ");
});
const currentDefaultThinkingLabel = computed(() => {
	const level = modelDefaultThinking(props.currentModelInfo);
	return level ? thinkingLabel(level) : "无";
});
const agentFastTriState = computed(() => {
	if (props.agentFastMode === true) return "on";
	if (props.agentFastMode === false) return "off";
	return "follow";
});
const isAgentTab = computed(() => runConfigTab.value === "agent");
const menuThinkingLevels = computed(() => isAgentTab.value ? props.agentThinkLevels : props.currentThinkLevels);
const menuSupportsThinking = computed(() => isAgentTab.value ? props.agentSupportsThinking : props.supportsThinking);
const menuThinkingLevel = computed(() => isAgentTab.value ? props.agentThinkLevel : props.effectiveThinking);
const menuDefaultThinking = computed(() => compactThinkingLabel(isAgentTab.value ? props.agentDefaultThinkingLabel : currentDefaultThinkingLabel.value));
const menuSelectedModel = computed(() => isAgentTab.value ? props.agentModel : props.currentModel);
const contextDetailText = computed(() => `已用 ${props.contextUsedDisplay} · 压缩阈值 ${props.contextThresholdDisplay} · 模型窗口 ${props.contextWindowDisplay}`);

function compactThinkingLabel(level) {
	return {off: '关闭', minimal: '极简', low: '低', medium: '中', high: '高', xhigh: '极高', max: '最高'}[level] || level || '默认';
}

function selectMenuModel(model) {
	clearModelDetail();
	if (isAgentTab.value) emit('select-agent-model', model.key);
	else emit('select-model', model);
}

function selectMenuThinking(level) {
	emit(isAgentTab.value ? 'select-agent-thinking' : 'select-thinking', level);
}

function modelTags(model) {
	const tags = [];
	if (model?.contextWindow) {
		tags.push({id: 'context', type: 'number', label: `${fmtTokens(model.contextWindow)} 上下文`});
	}
	const trigger = rolloverTriggerForModel(model);
	if (trigger > 0) {
		tags.push({id: 'compression', type: 'number', label: `${fmtTokens(trigger)} 压缩`});
	}
	if (model?.maxTokens) {
		tags.push({id: 'output', type: 'number', label: `${fmtTokens(model.maxTokens)} 输出`});
	}
	if (model?.reasoning) {
		tags.push({id: 'reasoning', type: 'feature', icon: 'brain', label: '思考'});
	}
	if (model?.supportsFast) {
		tags.push({id: 'fast', type: 'feature', icon: 'zap', label: 'Fast'});
	}
	return tags;
}

function modelFeatures(model) {
	const features = [{id: 'protocol', icon: 'protocol', label: '接口协议', value: model.protocol || '未声明'}];
	const trigger = rolloverTriggerForModel(model);
	if (trigger > 0) features.push({id: 'compression', icon: 'compression', label: '压缩阈值', value: `${fmtTokens(trigger)} tokens`});
	if (model.contextWindow) features.push({id: 'context', icon: 'context', label: '上下文窗口', value: `${fmtTokens(model.contextWindow)} tokens`});
	if (model.maxTokens) features.push({id: 'output', icon: 'output', label: '输出上限', value: `${fmtTokens(model.maxTokens)} tokens`});
	if (model.reasoning) features.push({id: 'reasoning', icon: 'brain', label: '思考能力', value: '支持推理'});
	if (model.supportsFast) features.push({id: 'fast', icon: 'zap', label: 'Fast 模式', value: '支持加速'});
	return features;
}
const contextPercentNumber = computed(() => {
	const value = Number(String(props.contextPercentDisplay || "").replace("%", ""));
	return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
});
const contextMeterStyle = computed(() => ({width: `${contextPercentNumber.value}%`}));

function rolloverTriggerForModel(model) {
	const explicit = Number(model?.rolloverTriggerTokens || 0);
	if (explicit > 0) return explicit;
	const ratio = Number(model?.windowTriggerRatio || DEFAULT_WINDOW_TRIGGER_RATIO);
	return Math.round(Number(model?.contextWindow || 0) * ratio);
}

function openFilePicker() {
	fileInput.value?.click?.();
}

function onAttachmentChange(event) {
	const files = Array.from(event.target?.files || []);
	if (files.length) emit("attachment-change", files);
	if (event.target) event.target.value = "";
}

function attachmentPreviewUrl(item) {
	return props.attachmentPreviews?.[item?.id] || "";
}

function pendingImagePreviewList() {
	return props.pendingAttachments
		.map((item) => attachmentPreviewUrl(item))
		.filter(Boolean);
}

function pendingImagePreviewIndex(item) {
	const src = attachmentPreviewUrl(item);
	return Math.max(0, pendingImagePreviewList().indexOf(src));
}

function extensionFromMime(type = "") {
	const subtype = String(type || "").split("/")[1] || "";
	if (!subtype) return "bin";
	return subtype.split(";")[0].replace(/^svg\+xml$/, "svg").replace(/[^a-z0-9.+-]/gi, "") || "bin";
}

function normalizePastedFile(file, index) {
	if (!file) return null;
	if (file.name) return file;
	const prefix = String(file.type || "").startsWith("image/") ? "pasted-image" : "pasted-file";
	const name = `${prefix}-${Date.now()}-${index + 1}.${extensionFromMime(file.type)}`;
	return new File([file], name, {type: file.type || "application/octet-stream", lastModified: file.lastModified || Date.now()});
}

function clipboardFiles(event) {
	const clipboard = event.clipboardData;
	if (!clipboard) return [];
	const fromItems = Array.from(clipboard.items || [])
		.filter((item) => item.kind === "file")
		.map((item) => item.getAsFile?.())
		.filter(Boolean)
		.map(normalizePastedFile)
		.filter(Boolean);
	if (fromItems.length) return fromItems;
	return Array.from(clipboard.files || []).map(normalizePastedFile).filter(Boolean);
}

function insertPlainTextFromPaste(event) {
	const text = event.clipboardData?.getData?.("text/plain") || "";
	if (!text) return;
	const el = composerTextarea.value;
	if (typeof el?.insertText === "function") { el.insertText(text); return; }
	const value = String(props.draft || "");
	const start = Number(el?.selectionStart ?? value.length);
	const end = Number(el?.selectionEnd ?? start);
	const next = `${value.slice(0, start)}${text}${value.slice(end)}`;
	emit("update:draft", next);
	nextTick(() => {
		try {
			el?.setSelectionRange?.(start + text.length, start + text.length);
		} catch {
		}
		adjustHeight();
	});
}

function onPaste(event) {
	const files = clipboardFiles(event);
	if (!files.length) return;
	event.preventDefault();
	if (!props.running) emit("attachment-change", files);
	insertPlainTextFromPaste(event);
}

function adjustHeight() {
	nextTick(() => {
		const el = composerTextarea.value;
		if (!el) return;
		if (typeof el.adjustHeight === "function") { el.adjustHeight(); return; }
		el.style.height = "auto";
		el.style.height = `${Math.min(el.scrollHeight, 216)}px`;
	});
}

async function focus() {
	await nextTick();
	const el = composerTextarea.value;
	if (!el) return;
	try {
		el.focus({preventScroll: true});
	} catch {
		el.focus?.();
	}
	const end = String(el.value || "").length;
	try {
		el.setSelectionRange(end, end);
	} catch {
	}
}

function onDraftInput(event) {
	emit("update:draft", event.target?.value || "");
	adjustHeight();
}

function clearDraft() {
	emit("update:draft", "");
	emit("clear-draft");
	adjustHeight();
}

function interactionAction(item) {
	return String(item?.action || "confirm").trim().toLowerCase();
}

function optionLabel(option) {
	if (option && typeof option === "object") return String(option.label || option.text || option.value || "");
	return String(option || "");
}

function ensureInteractionDraft(item) {
	const id = item?.confirmationId;
	if (!id) return createInteractionDraft(item);
	if (!interactionDrafts.value[id]) interactionDrafts.value[id] = createInteractionDraft(item);
	return interactionDrafts.value[id];
}

function optionChecked(item, idx) {
	return ensureInteractionDraft(item).selectedIndexes.includes(idx);
}

function clearInteractionError(item) {
	const id = item?.confirmationId;
	if (id && interactionErrors.value[id]) delete interactionErrors.value[id];
}

function toggleOption(item, idx) {
	toggleInteractionOption(item, ensureInteractionDraft(item), idx);
	clearInteractionError(item);
}

function clearSelectChoice(item) {
	clearInteractionSelection(ensureInteractionDraft(item));
	clearInteractionError(item);
}

function setPromptValue(item, value) {
	ensureInteractionDraft(item).value = value;
	clearInteractionError(item);
}

function setInteractionText(item, value) {
	ensureInteractionDraft(item).text = value;
	if (String(value || "").trim() || ensureInteractionDraft(item).selectedIndexes.length) clearInteractionError(item);
}

function interactionExpired(item) {
	return isInteractionExpired(item, interactionNowMs.value);
}

function interactionDisabled(item) {
	return Boolean(props.confirmationSubmitting[item?.confirmationId] || interactionExpired(item));
}

function interactionExpiry(item) {
	return interactionExpiryLabel(item, interactionNowMs.value);
}

function interactionError(item) {
	return interactionErrors.value[item?.confirmationId] || props.confirmationErrors[item?.confirmationId] || "";
}

function interactionActionName(item) {
	return {confirm: "确认", select: "选择", prompt: "输入", questionnaire: "问卷"}[interactionAction(item)] || "确认";
}

function interactionRiskLabel(item) {
	const type = String(item?.type || item?.tone || "").toLowerCase();
	if (type === "danger") return "高风险操作";
	if (type === "warning") return "请谨慎确认";
	return "";
}

function primaryActionLabel(item) {
	return interactionPrimaryActionLabel(
		item,
		ensureInteractionDraft(item),
		Boolean(props.confirmationSubmitting[item?.confirmationId]),
	);
}

function confirmRejectLabel(item) {
	return interactionRejectActionLabel(ensureInteractionDraft(item));
}

function questionnaireQuestions(item) {
	return Array.isArray(item?.questions) ? item.questions : [];
}

function questionnaireQuestionId(question) {
	return String(question?.questionId ?? question?.id ?? "").trim();
}

function questionnaireQuestionText(question, index) {
	return String(question?.prompt || question?.question || question?.title || question?.label || `问题 ${index + 1}`);
}

function questionnaireOptionValue(option) {
	if (option && typeof option === "object") return option.value ?? option.label ?? "";
	return option ?? "";
}

function questionnaireOptionDescription(option) {
	return option && typeof option === "object" ? String(option.description || "") : "";
}

function ensureQuestionnaireDraft(item) {
	const id = String(item?.confirmationId || "");
	if (!questionnaireDrafts.value[id]) questionnaireDrafts.value[id] = createQuestionnaireDraft(questionnaireQuestions(item));
	return questionnaireDrafts.value[id];
}

function questionnaireAnswer(item, question) {
	return ensureQuestionAnswer(ensureQuestionnaireDraft(item), question);
}

function questionnaireChoiceSelected(item, question, option) {
	return isQuestionChoiceSelected(ensureQuestionnaireDraft(item), question, option);
}

function toggleQuestionnaireChoice(item, question, option) {
	toggleQuestionChoice(ensureQuestionnaireDraft(item), question, questionnaireOptionValue(option));
	clearQuestionnaireError(item, question);
}

function clearQuestionnaireSelection(item, question) {
	clearQuestionChoice(ensureQuestionnaireDraft(item), question);
}

function setQuestionnaireText(item, question, value) {
	questionnaireAnswer(item, question).text = value;
	if (String(value || "").trim()) clearQuestionnaireError(item, question);
}

function questionnaireRecommendationValues(question) {
	return Array.isArray(question?.recommendation?.values) ? question.recommendation.values : [];
}

function isRecommendedOption(question, option) {
	const value = questionnaireOptionValue(option);
	return questionnaireRecommendationValues(question).some((item) => Object.is(item, value));
}

function questionnaireFieldId(item, question, index) {
	const safeConfirmation = String(item?.confirmationId || "questionnaire").replace(/[^a-zA-Z0-9_-]/g, "-");
	const safeQuestion = (questionnaireQuestionId(question) || String(index)).replace(/[^a-zA-Z0-9_-]/g, "-");
	return `questionnaire-${safeConfirmation}-${safeQuestion}`;
}

function questionnaireError(item, question) {
	return questionnaireErrors.value[item?.confirmationId]?.[questionnaireQuestionId(question)] || "";
}

function clearQuestionnaireError(item, question) {
	const errors = questionnaireErrors.value[item?.confirmationId];
	if (errors) delete errors[questionnaireQuestionId(question)];
}

function focusFirstQuestionnaireError(item) {
	nextTick(() => {
		const confirmationId = String(item?.confirmationId || "");
		const cards = Array.from(composerShell.value?.querySelectorAll?.("[data-questionnaire-id]") || []);
		const card = cards.find((element) => element.dataset.questionnaireId === confirmationId);
		const field = card?.querySelector?.("[data-question-error='true'] textarea, [data-question-error='true'] input");
		field?.focus?.();
	});
}

function submitQuestionnaire(item) {
	if (interactionDisabled(item)) return;
	const questions = questionnaireQuestions(item);
	const draft = ensureQuestionnaireDraft(item);
	const errors = validateQuestionnaire(questions, draft);
	questionnaireErrors.value[item.confirmationId] = errors;
	if (Object.keys(errors).length) {
		focusFirstQuestionnaireError(item);
		return;
	}
	emit("answer-confirmation", item, buildQuestionnaireAnswer(questions, draft));
}

function cancelQuestionnaire(item) {
	if (interactionDisabled(item)) return;
	emit("answer-confirmation", item, buildQuestionnaireAnswer(questionnaireQuestions(item), ensureQuestionnaireDraft(item), true));
}

function answerInteraction(item, intent) {
	if (interactionDisabled(item)) return;
	const draft = ensureInteractionDraft(item);
	if (intent !== "cancel") {
		const error = validateInteractionDraft(item, draft);
		if (error) {
			interactionErrors.value[item.confirmationId] = error;
			return;
		}
	}
	clearInteractionError(item);
	emit("answer-confirmation", item, buildInteractionAnswer(item, draft, intent));
}

function handleKeydown(event) {
	if (event.key !== "Enter") return;
	if (event.shiftKey) return;
	if (event.isComposing) return;
	event.preventDefault();
	if (!props.canSend) return;
	emit("send");
}

onMounted(() => {
	interactionNowMs.value = Date.now();
	interactionClockTimer = window.setInterval(() => {
		interactionNowMs.value = Date.now();
	}, 1000);
	const el = composerShell.value;
	if (!el) return;
	const notifyHeight = () => emit("height-change", Math.ceil(el.getBoundingClientRect().height));
	notifyHeight();
	if (typeof ResizeObserver !== "undefined") {
		composerResizeObserver = new ResizeObserver(notifyHeight);
		composerResizeObserver.observe(el);
	}
});

onBeforeUnmount(() => {
	if (interactionClockTimer) window.clearInterval(interactionClockTimer);
	interactionClockTimer = null;
	composerResizeObserver?.disconnect();
	composerResizeObserver = null;
});

function focusInteraction(interactionId) {
	const pending = props.pendingConfirmations.find(item => item.confirmationId === interactionId);
	if (!pending || interactionExpired(pending)) return false;
	const card = Array.from(composerShell.value?.querySelectorAll("[data-interaction-id]") || [])
		.find(element => element.dataset.interactionId === interactionId);
	if (!card) return false;
	card.scrollIntoView({block: "nearest", inline: "nearest"});
	// Focus the card, not an input: navigation must not summon the phone keyboard
	// or activate any confirmation/submit button.
	card.focus({preventScroll: true});
	return true;
}

function getReferenceOrder() { return composerTextarea.value?.getReferenceOrder?.() || []; }
defineExpose({focus, adjustHeight, openFilePicker, focusInteraction, getReferenceOrder});
</script>

<template>
	<footer ref="composerShell" class="composer-shell shrink-0 bg-gradient-to-t from-white via-white/95 to-transparent pb-4 pt-8">
		<div class="composer-content pointer-events-auto relative mx-auto">
			<div v-if="props.pendingSteering.length" class="steering-queue-card">
				<div class="steering-queue-title">
					<Timer/>
					待处理插话队列
				</div>
				<div class="steering-queue-hint">当前运行到轮次边界后，后端会取走这些内容并作为一条 user message 注入模型。</div>
				<div class="steering-queue-items">
					<div v-for="item in props.pendingSteering" :key="item.id" class="steering-queue-item"><ReferencePlainText :text="item.text" :references="item.references || []" :bundle-id="item.referenceBundleId || ''"/></div>
				</div>
			</div>

			<div v-if="props.pendingConfirmations.length" class="web-confirm-stack">
				<div v-for="item in props.pendingConfirmations" :key="item.confirmationId" class="web-confirm-card"
				     :class="{'questionnaire-card': interactionAction(item) === 'questionnaire', 'is-expired': interactionExpired(item)}"
				     :data-questionnaire-id="interactionAction(item) === 'questionnaire' ? item.confirmationId : undefined"
				     :data-interaction-id="item.confirmationId" tabindex="-1" role="region" :aria-label="item.title || '待处理交互'">
					<div class="web-confirm-title" :class="{'questionnaire-title': interactionAction(item) === 'questionnaire'}">
						<Warning/>
						<div>
							<strong>{{ item.title || (interactionAction(item) === 'questionnaire' ? '需要你补充一些信息' : '请确认') }}</strong>
							<span v-if="interactionAction(item) === 'questionnaire'" class="interaction-title-description">请按实际情况回答；选择题也可以直接填写自己的答案。</span>
							<span class="interaction-card-meta">
								<span>{{ interactionActionName(item) }}</span>
								<span v-if="interactionExpiry(item)" class="interaction-expiry" :class="{'is-expired': interactionExpired(item)}">{{ interactionExpiry(item) }}</span>
								<span v-if="interactionRiskLabel(item)" class="interaction-risk" :class="String(item.type || item.tone || '').toLowerCase()">{{ interactionRiskLabel(item) }}</span>
							</span>
						</div>
					</div>
					<div v-if="interactionExpired(item)" class="interaction-expired-notice" role="status">此交互已过期，等待同步最新状态。</div>

					<template v-if="interactionAction(item) === 'questionnaire'">
						<div v-if="item.body" class="web-interaction-content" tabindex="0" role="region" :aria-label="`${item.title || '问卷'}内容`">
							<InteractionMarkdown class="questionnaire-intro" :text="item.body"/>
						</div>
						<div class="questionnaire-questions">
							<fieldset v-for="(question, questionIndex) in questionnaireQuestions(item)"
							          :key="questionnaireQuestionId(question) || questionIndex"
							          class="questionnaire-question"
							          :class="{'has-error': questionnaireError(item, question)}"
							          :data-question-error="questionnaireError(item, question) ? 'true' : 'false'"
							          :aria-describedby="`${questionnaireFieldId(item, question, questionIndex)}-hint ${questionnaireFieldId(item, question, questionIndex)}-error`">
								<legend>
									<span class="question-number">{{ questionIndex + 1 }}</span>
									<span>{{ questionnaireQuestionText(question, questionIndex) }}</span>
									<span v-if="question.required" class="required-mark">必填</span>
									<span v-else class="optional-mark">选填</span>
								</legend>
								<InteractionMarkdown v-if="question.description" class="question-description" :text="question.description"/>
								<template v-if="question.type === 'choice'">
									<div class="question-choice-list">
										<label v-for="(option, optionIndex) in question.options || []"
										       :key="`${questionnaireQuestionId(question)}-${optionIndex}`"
										       class="question-choice-option"
										       :class="{'is-selected': questionnaireChoiceSelected(item, question, option)}">
											<input :type="question.multiple ? 'checkbox' : 'radio'"
											       :name="`question-${item.confirmationId}-${questionnaireQuestionId(question)}`"
											       :checked="questionnaireChoiceSelected(item, question, option)"
											       :disabled="interactionDisabled(item)"
											       @change="toggleQuestionnaireChoice(item, question, option)"/>
											<span class="question-choice-copy">
												<span class="question-choice-label">
													{{ optionLabel(option) }}
													<small v-if="isRecommendedOption(question, option)" class="recommendation-badge">推荐</small>
												</span>
												<small v-if="questionnaireOptionDescription(option)">{{ questionnaireOptionDescription(option) }}</small>
											</span>
										</label>
									</div>
									<div v-if="question.recommendation?.reason" class="recommendation-reason">
										<strong>推荐理由</strong>{{ question.recommendation.reason }}
									</div>
									<button v-if="!question.multiple && questionnaireAnswer(item, question).selectedValues.length"
									        type="button" class="clear-question-choice"
									        :disabled="interactionDisabled(item)"
									        @click="clearQuestionnaireSelection(item, question)">清除选择</button>
									<label class="question-free-text" :for="`${questionnaireFieldId(item, question, questionIndex)}-text`">
										<span>{{ questionnaireAnswer(item, question).selectedValues.length ? '补充、限制或修正以上选择' : '也可以不选，直接填写自己的答案' }}</span>
										<textarea :id="`${questionnaireFieldId(item, question, questionIndex)}-text`"
										          :value="questionnaireAnswer(item, question).text" rows="2"
										          :disabled="interactionDisabled(item)"
										          placeholder="写下更符合你需要的答案"
										          @input="setQuestionnaireText(item, question, $event.target.value)"></textarea>
									</label>
								</template>
								<label v-else class="question-free-text open-answer" :for="`${questionnaireFieldId(item, question, questionIndex)}-text`">
									<span>你的回答</span>
									<textarea :id="`${questionnaireFieldId(item, question, questionIndex)}-text`"
									          :value="questionnaireAnswer(item, question).text" rows="3"
									          :disabled="interactionDisabled(item)"
									          placeholder="请输入你的回答"
									          @input="setQuestionnaireText(item, question, $event.target.value)"></textarea>
								</label>
								<div :id="`${questionnaireFieldId(item, question, questionIndex)}-hint`" class="question-hint">
									{{ question.required ? (question.type === 'choice' ? '必填：至少选择一项或填写文字。' : '必填：请填写文字。') : '选填：留空也可以提交。' }}
								</div>
								<div :id="`${questionnaireFieldId(item, question, questionIndex)}-error`" class="question-error" role="alert">
									{{ questionnaireError(item, question) }}
								</div>
							</fieldset>
						</div>
						<div v-if="interactionError(item)" class="interaction-submit-error" role="alert">{{ interactionError(item) }}</div>
						<div class="web-confirm-actions questionnaire-actions">
							<button type="button" class="web-confirm-btn cancel" :disabled="interactionDisabled(item)" @click="cancelQuestionnaire(item)">暂不回答</button>
							<button type="button" class="web-confirm-btn confirm" :disabled="interactionDisabled(item)"
							        :aria-busy="props.confirmationSubmitting[item.confirmationId] ? 'true' : 'false'"
							        @click="submitQuestionnaire(item)">{{ props.confirmationSubmitting[item.confirmationId] ? '提交中…' : '提交回答' }}</button>
						</div>
					</template>

					<template v-else>
						<div v-if="item.body" class="web-interaction-content" tabindex="0" role="region" :aria-label="`${item.title || '交互'}内容`">
							<InteractionMarkdown class="web-confirm-body" :text="item.body"/>
						</div>
						<template v-if="interactionAction(item) === 'select'">
							<div class="web-interaction-options">
								<label v-for="(option, idx) in item.options || []" :key="`${item.confirmationId}-${idx}`"
								       class="web-interaction-option" :class="{'is-selected': optionChecked(item, idx)}">
									<input :type="item.multiple ? 'checkbox' : 'radio'" :name="`interaction-${item.confirmationId}`"
									       :checked="optionChecked(item, idx)" :disabled="interactionDisabled(item)" @change="toggleOption(item, idx)"/>
									<span class="web-interaction-option-copy"><strong>{{ optionLabel(option) }}</strong><small v-if="option?.description">{{ option.description }}</small></span>
								</label>
							</div>
							<button v-if="!item.multiple && ensureInteractionDraft(item).selectedIndexes.length" type="button"
							        class="clear-question-choice" :disabled="interactionDisabled(item)" @click="clearSelectChoice(item)">清除选择</button>
							<label class="web-interaction-input">
								<span>{{ ensureInteractionDraft(item).selectedIndexes.length ? '补充、限制或修正以上选择' : '也可以不选，直接填写自己的答案' }}</span>
								<textarea :value="ensureInteractionDraft(item).text" rows="2" :disabled="interactionDisabled(item)"
								          placeholder="写下更符合你需要的答案" @input="setInteractionText(item, $event.target.value)"></textarea>
								<small v-if="item.requiresAuthorization">有文字时只提交意见，不执行原操作。</small>
							</label>
						</template>
						<label v-else-if="interactionAction(item) === 'prompt'" class="web-interaction-input">
							<span>你的回答</span>
							<textarea :value="ensureInteractionDraft(item).value" :placeholder="item.sensitive ? '输入内容' : '请输入内容'"
							          rows="3" :disabled="interactionDisabled(item)" @input="setPromptValue(item, $event.target.value)"></textarea>
							<small v-if="item.sensitive">此输入会原样提交给当前任务，不会显示在公开历史中。</small>
						</label>
						<label v-else class="web-interaction-input confirm-feedback-input">
							<span>补充意见（可选）</span>
							<textarea :value="ensureInteractionDraft(item).text" rows="2" :disabled="interactionDisabled(item)"
							          placeholder="如需调整原操作，请在这里说明" @input="setInteractionText(item, $event.target.value)"></textarea>
							<small>填写任何意见后都不会执行原操作；意见将原样提交。</small>
						</label>
						<div v-if="interactionError(item)" class="interaction-submit-error" role="alert">{{ interactionError(item) }}</div>
						<div class="web-confirm-actions">
							<button type="button" class="web-confirm-btn cancel" :disabled="interactionDisabled(item)"
							        @click="answerInteraction(item, 'cancel')">{{ item.cancelText || '暂不回答' }}</button>
							<button v-if="interactionAction(item) === 'confirm'" type="button" class="web-confirm-btn reject"
							        :disabled="interactionDisabled(item)" @click="answerInteraction(item, 'reject')">{{ confirmRejectLabel(item) }}</button>
							<button type="button" class="web-confirm-btn confirm" :disabled="interactionDisabled(item)"
							        :aria-busy="props.confirmationSubmitting[item.confirmationId] ? 'true' : 'false'"
							        @click="answerInteraction(item, 'confirm')">
								{{ primaryActionLabel(item) }}
							</button>
						</div>
					</template>
				</div>
			</div>

			<div class="composer-box">
				<input ref="fileInput" type="file" multiple class="hidden" @change="onAttachmentChange"/>
				<div v-if="props.pendingAttachments.length" class="attachment-strip">
					<div v-for="item in props.pendingAttachments" :key="item.id" class="attachment-card group">
						<el-image v-if="attachmentPreviewUrl(item)" class="attachment-thumb"
						          :src="attachmentPreviewUrl(item)"
						          :alt="item.file.name"
						          fit="cover"
						          :preview-src-list="pendingImagePreviewList()"
						          :initial-index="pendingImagePreviewIndex(item)"
						          preview-teleported
						          hide-on-click-modal/>
						<div v-else class="attachment-file-tile">
							<Document class="file-preview-icon"/>
							<span>{{ item.file.name }}</span>
							<small>{{ fmtBytes(item.file.size) }}</small>
						</div>
						<el-tooltip content="移除附件" placement="top" :show-after="260">
							<button type="button" class="attachment-remove" :aria-label="`移除附件：${item.file.name}`"
							        @click.stop="emit('remove-attachment', item.id)">
								<Close/>
							</button>
						</el-tooltip>
					</div>
				</div>
				<ReferenceEditor
					ref="composerTextarea"
					:model-value="props.draft"
					:current-conversation="props.conversationUuid"
					@update:model-value="emit('update:draft', $event)"
					@send="props.canSend && emit('send')"
					@paste="onPaste"
				/>
				<div class="composer-toolbar">
					<div class="composer-actions relative flex min-w-0 flex-1 items-center gap-1.5">
						<el-tooltip content="新话题（Ctrl+N）" placement="top" :show-after="260">
							<button type="button" class="tool-btn" aria-label="新话题（Ctrl+N）" @click="emit('new-session')">
								<Plus/>
							</button>
						</el-tooltip>
						<el-tooltip content="上传图片或附件" placement="top" :show-after="260">
							<button type="button" class="tool-btn tool-btn-accent" aria-label="上传图片或附件"
							        :disabled="props.running" @click="openFilePicker">
								<Paperclip/>
							</button>
						</el-tooltip>

						<el-tooltip v-if="props.contextStrategy === 'model_summary'" :content="props.compacting ? '正在压缩上下文…' : '手动压缩上下文'" placement="top" :show-after="260">
							<button type="button" class="tool-btn" aria-label="手动压缩上下文" :disabled="!props.canCompact || props.compacting" @click="emit('compact')"><ContextCompactionIcon :class="{'context-compacting-icon': props.compacting}" /></button>
						</el-tooltip>
						<el-tooltip content="清空草稿" placement="top" :show-after="260">
							<button type="button" class="tool-btn composer-clear" aria-label="清空草稿"
							        :disabled="props.running || (!props.draft && !props.pendingAttachments.length)"
							        @click="clearDraft">
								<Close/>
							</button>
						</el-tooltip>
					</div>
					<div class="composer-status">
						<el-popover v-model:visible="runConfigPopoverVisible"
						            popper-class="composer-menu-popper run-config-menu-popper"
						            placement="top-end"
						            trigger="click"
						            :width="'min(26rem, calc(100vw - 2rem))'"
						            :show-arrow="false"
						            :hide-after="0">
							<template #reference>
								<button type="button" class="status-chip run-config-chip" aria-label="运行配置" :aria-description="`上下文压缩：${runConfigStrategyText}`"
								        :class="props.modelMenuOpen ? 'status-chip-active' : ''">
									<span class="run-config-chip-main">
										<span class="run-config-chip-model">{{ runConfigModelText }}</span>
										<span v-if="runConfigMetaText" class="run-config-chip-meta">{{ runConfigMetaText }}</span>
									</span>
									<span class="run-config-chip-strategy" :aria-label="`上下文压缩：${runConfigStrategyText}`">{{ props.contextStrategy === 'model_summary' ? '摘要压缩' : '滑窗压缩' }}</span>
									<ArrowDown class="chip-caret"/>
								</button>
							</template>
							<div class="popover-menu-content run-config-popover" @mouseleave="clearModelDetail" @scroll.capture="clearModelDetail" @keydown.esc="clearModelDetail">
								<div class="run-config-tabs" role="tablist" aria-label="运行配置切换">
									<button type="button" role="tab" :aria-selected="runConfigTab === 'main'" :class="runConfigTab === 'main' ? 'is-active' : ''" @click="runConfigTab = 'main'">主会话</button>
									<button type="button" role="tab" :aria-selected="runConfigTab === 'agent'" :class="runConfigTab === 'agent' ? 'is-active' : ''" @click="runConfigTab = 'agent'">Agent</button>
								</div>
								<el-tooltip v-if="!isAgentTab" :content="contextDetailText" placement="top" :show-after="400">
									<div class="run-config-context" :aria-label="contextDetailText">
										<div class="context-meter-row">
											<span class="config-label"><ModelFeatureIcon name="context"/>上下文</span>
											<span class="context-meter-values"><span>{{ props.contextUsedDisplay }} <span class="context-meter-limit">/ {{ props.contextThresholdDisplay }}</span></span><span class="context-percent">{{ props.contextPercentDisplay }}</span></span>
										</div>
										<div class="context-meter" aria-hidden="true"><span :style="contextMeterStyle"></span></div>
									</div>
								</el-tooltip>
								<p v-if="props.running" class="run-config-notice">{{ isAgentTab ? '模型与执行设置用于新 Agent' : '模型与执行设置在下一次调用生效' }}</p>
								<label class="model-search run-config-search">
									<Search/>
									<input :value="props.modelQuery" type="search" :placeholder="isAgentTab ? '搜索 Agent 模型' : '搜索模型'" :aria-label="isAgentTab ? '搜索 Agent 模型' : '搜索模型'" @input="emit('update:modelQuery', $event.target.value)"/>
								</label>
								<div class="run-config-model-section">
								<div class="model-list run-config-model-list">
									<div v-if="isAgentTab" class="agent-follow-group">
										<button type="button" class="model-row follow-model-row" :class="!props.agentModel ? 'is-selected' : ''" :aria-pressed="!props.agentModel" @click="emit('select-agent-model', '')">
											<div class="model-row-main">
												<div class="model-row-title-row">
													<span class="model-row-name">跟随主模型</span>
												</div>
												<div class="model-row-tags">
													<span class="model-tag model-follow-detail">{{ runConfigModelText }}</span>
												</div>
											</div>
											<span class="model-selection-mark"><CircleCheck v-if="!props.agentModel" class="model-selected-icon"/></span>
										</button>
									</div>
									<section v-for="group in props.modelGroups" :key="group.provider" class="model-group" :aria-label="group.provider">
										<div class="model-group-title">{{ group.provider }}</div>
										<div v-for="model in group.models" :key="model.key" class="model-row" :class="menuSelectedModel === model.key ? 'is-selected' : ''" @click="selectMenuModel(model)">
											<button type="button" class="model-select" :aria-pressed="menuSelectedModel === model.key" :aria-label="`${modelLabel(model)}（${model.key}）`" @click.stop="selectMenuModel(model)">
												<div class="model-row-main">
													<div class="model-row-title-row">
														<span class="model-row-name">{{ modelLabel(model) }}</span>
													</div>
													<div v-if="modelTags(model).length" class="model-row-tags">
														<span v-for="tag in modelTags(model)" :key="tag.id" class="model-tag" :class="tag.type === 'feature' ? 'model-tag-feature' : ''">
															<ModelFeatureIcon v-if="tag.icon" :name="tag.icon"/>
															<span>{{ tag.label }}</span>
														</span>
													</div>
												</div>
											</button>
											<span class="model-selection-mark"><CircleCheck v-if="menuSelectedModel === model.key" class="model-selected-icon"/></span>
										</div>
									</section>
									<div v-if="!props.modelGroups.length" class="model-empty">没有匹配模型</div>
								</div>
								</div>
								<div class="run-config-controls">
									<div class="thinking-control">
										<div class="run-config-control-head"><span class="config-label"><ModelFeatureIcon name="brain"/>思考强度</span><span class="config-hint">{{ menuSupportsThinking ? `默认 ${menuDefaultThinking}` : '未声明支持' }}</span></div>
										<div v-if="menuSupportsThinking || isAgentTab" class="thinking-segments" role="group" aria-label="思考强度">
											<el-tooltip v-if="isAgentTab" :content="`使用模型默认思考强度：${props.agentDefaultThinkingLabel}`" placement="top" :show-after="400">
												<button type="button" :class="!props.agentThinkLevel ? 'is-active' : ''" :aria-pressed="!props.agentThinkLevel" @click="selectMenuThinking('')">默认</button>
											</el-tooltip>
											<el-tooltip v-for="level in menuThinkingLevels" :key="level" :content="`思考强度：${level}`" placement="top" :show-after="400">
												<button type="button" :class="menuThinkingLevel === level ? 'is-active' : ''" :aria-pressed="menuThinkingLevel === level" @click="selectMenuThinking(level)">{{ compactThinkingLabel(level) }}</button>
											</el-tooltip>
										</div>
									</div>
									<div v-if="!isAgentTab" class="fast-control" :class="{'is-disabled': !props.fastSupported}">
										<span class="config-label"><ModelFeatureIcon name="zap"/>Fast 模式</span>
										<div class="config-value"><span v-if="!props.fastSupported" class="config-hint">不支持</span>
											<button type="button" class="fast-switch" role="switch" aria-label="Fast 模式" :aria-checked="props.currentFast" :class="props.currentFast ? 'is-on' : ''" :disabled="!props.fastSupported" @click="emit('toggle-fast-mode')"><span></span></button>
										</div>
									</div>
									<div v-else class="fast-control">
										<el-tooltip :content="props.agentFastSupported ? `跟随主会话时当前为${props.currentFast ? '开启' : '关闭'}` : '当前 Agent 模型未声明 Fast 能力'" placement="top" :show-after="400"><span class="config-label"><ModelFeatureIcon name="zap"/>Fast 模式</span></el-tooltip>
										<div class="thinking-segments agent-fast-segments" role="group" aria-label="Agent Fast 模式">
											<button type="button" :class="agentFastTriState === 'follow' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'follow'" aria-label="Fast 跟随主会话" @click="emit('select-agent-fast', null)">跟随</button>
											<button type="button" :class="agentFastTriState === 'on' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'on'" :disabled="!props.agentFastSupported" aria-label="开启 Agent Fast" @click="emit('select-agent-fast', true)">开</button>
											<button type="button" :class="agentFastTriState === 'off' ? 'is-active' : ''" :aria-pressed="agentFastTriState === 'off'" aria-label="关闭 Agent Fast" @click="emit('select-agent-fast', false)">关</button>
										</div>
									</div>
									<div class="fast-control context-strategy-control">
										<el-tooltip content="主会话与 Agent 共用；关闭使用滑动窗口，开启使用模型摘要" placement="top" :show-after="260"><span class="config-label"><ModelFeatureIcon name="compression"/>上下文压缩</span></el-tooltip>
										<div class="context-strategy-value config-value">
											<span class="config-hint" aria-live="polite">{{ props.contextStrategy === 'model_summary' ? '模型摘要' : '滑动窗口' }}</span>
											<button type="button" class="fast-switch" role="switch" aria-label="使用模型摘要（关闭为滑动窗口）" :aria-checked="props.contextStrategy === 'model_summary'" :class="props.contextStrategy === 'model_summary' ? 'is-on' : ''" :disabled="props.strategySaving" @click="emit('select-context-strategy', props.contextStrategy === 'model_summary' ? 'sliding_window' : 'model_summary')"><span></span></button>
										</div>
									</div>
								</div>
							</div>
						</el-popover>
						<el-tooltip v-if="props.running && !props.draft.trim()" content="停止生成" placement="top" :show-after="260">
							<button type="button" class="send-button stop-button" aria-label="停止生成" @click="emit('stop')">
								<SemiSelect/>
							</button>
						</el-tooltip>
						<el-tooltip v-else content="发送消息（Enter）" placement="top" :show-after="260">
							<button type="button" class="send-button" aria-label="发送消息（Enter）" :disabled="!props.canSend"
							        @click="emit('send')">
								<Promotion/>
							</button>
						</el-tooltip>
					</div>
				</div>
			</div>
			<div class="composer-hints mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-[11px] text-[#8b949e]">
				<span>Enter 发送 · Ctrl/⌘+Enter 也可发送</span>
				<span>图片/文本附件会随本轮发送</span>
			</div>
		</div>
	</footer>
</template>

<style scoped>
@import "./userInteractionTokens.css";

.context-compacting-icon { animation: context-compress-spin 1.5s linear infinite; }
@keyframes context-compress-spin { to { transform: rotate(360deg); } }

.composer-shell {
	pointer-events: none;
	padding-right: var(--console-content-gutter, 1rem);
	padding-left: var(--console-content-gutter, 1rem);
}

.composer-content {
	width: min(100%, var(--console-content-max-width, 56rem));
}

.composer-shell > div {
	pointer-events: auto;
}

.steering-queue-card {
	display: grid;
	gap: 0.55rem;
	margin-bottom: 0.75rem;
	border: 1px solid rgba(37, 99, 235, 0.22);
	border-radius: 1rem;
	background: linear-gradient(180deg, rgba(239, 246, 255, 0.98), rgba(255, 255, 255, 0.96));
	box-shadow: 0 16px 40px rgba(37, 99, 235, 0.10);
	padding: 0.85rem;
}

.steering-queue-title {
	display: flex;
	align-items: center;
	gap: 0.45rem;
	font-size: 0.82rem;
	font-weight: 800;
	color: #1d4ed8;
}

.steering-queue-title svg {
	width: 1rem;
	height: 1rem;
}

.steering-queue-hint {
	font-size: 0.73rem;
	line-height: 1.45;
	color: #64748b;
}

.steering-queue-items {
	display: grid;
	gap: 0.42rem;
}

.steering-queue-item {
	white-space: pre-wrap;
	border: 1px solid rgba(37, 99, 235, 0.16);
	border-radius: 0.78rem;
	background: rgba(255, 255, 255, 0.82);
	padding: 0.55rem 0.65rem;
	font-size: 0.8rem;
	line-height: 1.45;
	color: #1e293b;
}

.web-confirm-stack {
	display: grid;
	gap: 0.6rem;
	margin-bottom: 0.75rem;
}

/* A single card owns its content scroll; only stacked cards need an outer rail. */
.web-confirm-stack:has(.web-confirm-card + .web-confirm-card) {
	grid-auto-rows: max-content;
	max-height: min(58vh, 36rem);
	max-height: min(58dvh, 36rem);
	overflow: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
}

.web-interaction-content {
	max-height: min(24vh, 14rem);
	max-height: min(24dvh, 14rem);
	min-width: 0;
	min-height: 0;
	overflow: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
	scrollbar-color: var(--ob-interaction-border-strong) transparent;
	/* Leave room for input focus rings without moving the card edges. */
	padding: 0 0.2rem 0.2rem;
	margin: 0 -0.2rem;
}

.web-interaction-content:focus-visible {
	outline: 2px solid var(--ob-interaction-border-strong);
	outline-offset: -2px;
}

.web-confirm-card {
	display: flex;
	flex-direction: column;
	box-sizing: border-box;
	min-width: 0;
	position: relative;
	z-index: 45;
	border: 1px solid var(--ob-interaction-border);
	border-radius: 1rem;
	background: var(--ob-interaction-surface);
	box-shadow: var(--ob-interaction-shadow);
	padding: 0.85rem;
	color: var(--ob-interaction-ink);
}

.web-confirm-card:focus { outline: 2px solid var(--ob-interaction-border-strong); outline-offset: 2px; }

.web-confirm-card.is-expired {
	box-shadow: none;
	opacity: 0.82;
}

.web-confirm-title {
	flex: 0 0 auto;
	overflow-wrap: anywhere;
	display: flex;
	align-items: flex-start;
	gap: 0.52rem;
	font-size: 0.82rem;
	font-weight: 700;
	color: var(--ob-interaction-accent-strong);
}

.web-confirm-title > svg {
	width: 1.05rem;
	height: 1.05rem;
	flex: 0 0 auto;
	margin-top: 0.08rem;
}

.web-confirm-title > div {
	display: grid;
	min-width: 0;
	gap: 0.16rem;
}

.web-confirm-title strong {
	font-size: 0.9rem;
	line-height: 1.4;
}

.interaction-title-description {
	font-size: 0.75rem;
	font-weight: 400;
	line-height: 1.5;
	color: var(--ob-interaction-muted);
}

.interaction-card-meta {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 0.35rem;
	font-size: 0.67rem;
	font-weight: 650;
	line-height: 1.35;
	color: var(--ob-interaction-muted);
}

.interaction-card-meta > span + span::before {
	margin-right: 0.35rem;
	color: #b5c0cd;
	content: "·";
}

.interaction-expiry.is-expired {
	font-weight: 750;
	color: #a33f3f;
}

.interaction-risk {
	font-weight: 750;
}

.interaction-risk.warning {
	color: #8b6524;
}

.interaction-risk.danger {
	color: #a33f3f;
}

.interaction-expired-notice,
.interaction-submit-error {
	margin-top: 0.65rem;
	border: 1px solid #fecaca;
	border-radius: 0.7rem;
	background: #fef2f2;
	padding: 0.55rem 0.65rem;
	font-size: 0.74rem;
	line-height: 1.45;
	color: #991b1b;
}

.web-confirm-body {
	margin: 0.55rem 0 0;
	white-space: normal;
	font-family: inherit;
	font-size: 0.76rem;
	line-height: 1.5;
	color: var(--ob-interaction-muted);
}

.web-interaction-options {
	max-height: min(20vh, 12rem);
	max-height: min(20dvh, 12rem);
	overflow: auto;
	scrollbar-width: thin;
	display: grid;
	grid-template-columns: repeat(2, minmax(0, 1fr));
	gap: 0.45rem;
	margin-top: 0.7rem;
}

.web-interaction-option {
	display: flex;
	min-width: 0;
	align-items: flex-start;
	gap: 0.5rem;
	border: 1px solid var(--ob-interaction-border);
	border-radius: 0.72rem;
	background: var(--ob-interaction-surface-strong);
	padding: 0.55rem 0.62rem;
	font-size: 0.78rem;
	color: var(--ob-interaction-ink);
	cursor: pointer;
}

.web-interaction-option.is-selected {
	border-color: var(--ob-interaction-border-strong);
	background: var(--ob-interaction-surface-selected);
}

.web-interaction-option input {
	flex: 0 0 auto;
	margin-top: 0.17rem;
	accent-color: var(--ob-interaction-accent);
}

.web-interaction-option-copy {
	display: grid;
	min-width: 0;
	gap: 0.15rem;
	line-height: 1.4;
}

.web-interaction-option-copy small {
	font-size: 0.7rem;
	font-weight: 400;
	color: var(--ob-interaction-muted);
}

.web-interaction-input {
	display: grid;
	gap: 0.32rem;
	margin-top: 0.62rem;
	font-size: 0.72rem;
	font-weight: 650;
	color: #475569;
}

.web-interaction-input textarea {
	width: 100%;
	resize: vertical;
	border: 1px solid #ced9e5;
	border-radius: 0.68rem;
	background: var(--ob-interaction-surface-strong);
	padding: 0.58rem 0.65rem;
	font: inherit;
	font-size: 0.78rem;
	font-weight: 400;
	line-height: 1.5;
	color: var(--ob-interaction-ink);
	outline: none;
}

.web-interaction-input textarea:focus {
	border-color: var(--ob-interaction-border-strong);
	box-shadow: 0 0 0 3px var(--ob-interaction-focus);
}

.web-interaction-input small {
	font-size: 0.69rem;
	font-weight: 400;
	line-height: 1.45;
	color: var(--ob-interaction-muted);
}

.web-confirm-actions {
	flex: 0 0 auto;
	display: flex;
	flex-wrap: wrap;
	justify-content: flex-end;
	gap: 0.5rem;
	margin-top: 0.7rem;
}

.web-confirm-btn {
	min-height: 2rem;
	border: 1px solid transparent;
	border-radius: 999px;
	padding: 0.42rem 0.8rem;
	font-size: 0.76rem;
	font-weight: 700;
	line-height: 1.25;
	cursor: pointer;
}

.web-confirm-btn.cancel {
	border-color: var(--ob-interaction-border);
	background: var(--ob-interaction-surface-strong);
	color: var(--ob-interaction-muted);
}

.web-confirm-btn.reject {
	border-color: #c8d2de;
	background: #eef2f6;
	color: #475569;
}

.web-confirm-btn.confirm {
	background: var(--ob-interaction-accent);
	color: #fff;
}

.web-confirm-btn:disabled,
.web-interaction-option:has(input:disabled),
.clear-question-choice:disabled {
	opacity: 0.58;
	cursor: not-allowed;
}

.questionnaire-intro,
.question-description {
	font-size: 0.75rem;
	font-weight: 400;
	line-height: 1.5;
	color: var(--ob-interaction-muted);
	white-space: normal;
}

.questionnaire-intro {
	margin: 0.65rem 0 0;
}

.questionnaire-card > .web-interaction-content {
	max-height: min(14vh, 8rem);
	max-height: min(14dvh, 8rem);
}

.questionnaire-questions:has(.questionnaire-question + .questionnaire-question) {
	max-height: min(32vh, 20rem);
	max-height: min(32dvh, 20rem);
	overflow: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
}

.questionnaire-questions {
	display: grid;
	gap: 0.7rem;
	margin-top: 0.75rem;
}

.questionnaire-question {
	min-width: 0;
	margin: 0;
	border: 1px solid #dbe4ee;
	border-radius: 0.85rem;
	background: rgba(255, 255, 255, 0.9);
	padding: 0.72rem;
}

.questionnaire-question.has-error {
	border-color: #dc2626;
}

.questionnaire-question legend {
	display: flex;
	max-width: 100%;
	align-items: center;
	gap: 0.42rem;
	padding: 0 0.2rem;
	font-size: 0.82rem;
	font-weight: 750;
	line-height: 1.45;
	color: #1e293b;
}

.question-number {
	display: inline-grid;
	flex: 0 0 auto;
	width: 1.35rem;
	height: 1.35rem;
	place-items: center;
	border-radius: 50%;
	background: #e8eef7;
	font-size: 0.7rem;
	color: #334155;
}

.required-mark,
.optional-mark,
.recommendation-badge {
	flex: 0 0 auto;
	border-radius: 999px;
	padding: 0.1rem 0.38rem;
	font-size: 0.64rem;
	font-weight: 750;
}

.required-mark {
	background: #fee2e2;
	color: #991b1b;
}

.optional-mark {
	background: #f1f5f9;
	color: #64748b;
}

.question-description {
	max-height: min(12vh, 7rem);
	max-height: min(12dvh, 7rem);
	overflow: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
	margin: 0.25rem 0 0.55rem;
}

.question-choice-list {
	display: grid;
	grid-template-columns: repeat(2, minmax(0, 1fr));
	gap: 0.45rem;
	margin-top: 0.4rem;
}

.question-choice-option {
	display: flex;
	min-width: 0;
	align-items: flex-start;
	gap: 0.5rem;
	border: 1px solid #dbe4ee;
	border-radius: 0.72rem;
	background: #fff;
	padding: 0.55rem 0.62rem;
	cursor: pointer;
}

.question-choice-option.is-selected {
	border-color: #7896bd;
	background: #f2f6fb;
}

.question-choice-option input {
	flex: 0 0 auto;
	margin-top: 0.17rem;
	accent-color: #46678f;
}

.question-choice-copy {
	display: grid;
	min-width: 0;
	gap: 0.15rem;
	font-size: 0.78rem;
	line-height: 1.4;
	color: #263548;
}

.question-choice-copy > small {
	font-size: 0.7rem;
	font-weight: 400;
	line-height: 1.4;
	color: #64748b;
	white-space: normal;
}

.question-choice-label {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 0.3rem;
}

.recommendation-badge {
	border: 1px solid #b8c7da;
	background: #edf3fa;
	color: #345477;
}

.recommendation-reason {
	margin-top: 0.45rem;
	border-left: 2px solid #8ba4c3;
	padding-left: 0.55rem;
	font-size: 0.71rem;
	line-height: 1.45;
	color: #526274;
	white-space: pre-wrap;
}

.recommendation-reason strong {
	margin-right: 0.35rem;
	color: #345477;
}

.clear-question-choice {
	margin-top: 0.42rem;
	border: 0;
	background: transparent;
	padding: 0.15rem 0;
	font-size: 0.7rem;
	color: #526f91;
	text-decoration: underline;
	text-underline-offset: 2px;
	cursor: pointer;
}

.question-free-text {
	display: grid;
	gap: 0.32rem;
	margin-top: 0.55rem;
	font-size: 0.72rem;
	font-weight: 650;
	color: #475569;
}

.question-free-text textarea {
	width: 100%;
	resize: vertical;
	border: 1px solid #ced9e5;
	border-radius: 0.68rem;
	background: #fff;
	padding: 0.55rem 0.62rem;
	font: inherit;
	font-size: 0.78rem;
	font-weight: 400;
	line-height: 1.45;
	color: #1f2937;
	outline: none;
}

.question-free-text textarea:focus {
	border-color: #6585aa;
	box-shadow: 0 0 0 3px rgba(70, 103, 143, 0.12);
}

.question-hint {
	margin-top: 0.35rem;
	font-size: 0.68rem;
	line-height: 1.4;
	color: #64748b;
}

.question-error {
	min-height: 0;
	margin-top: 0.22rem;
	font-size: 0.7rem;
	font-weight: 650;
	color: #b91c1c;
}

.question-error:empty {
	display: none;
}

.questionnaire-actions {
	margin: 0.75rem -0.85rem -0.85rem;
	border-top: 1px solid #e2e8f0;
	background: rgba(248, 250, 252, 0.97);
	padding: 0.65rem 0.85rem;
}

/* On a short viewport, reserve space for answers rather than scrolling them away. */
@media (max-height: 600px) {
	.questionnaire-card > .web-interaction-content {
		max-height: 8vh;
		max-height: 8dvh;
	}
	.questionnaire-card .question-description {
		max-height: 6vh;
		max-height: 6dvh;
	}
}

@media (max-width: 640px) {
	.question-choice-list,
	.web-interaction-options {
		grid-template-columns: minmax(0, 1fr);
	}

	.questionnaire-question legend {
		align-items: flex-start;
		flex-wrap: wrap;
	}
}

.composer-box {
	position: relative;
	z-index: 40;
	border: 1px solid #d6d6d8;
	border-radius: 22px;
	background: #fff;
	padding: 0.5rem;
	box-shadow: 0 10px 34px rgba(0, 0, 0, .10);
}

.composer-box:focus-within {
	border-color: #b9b9bd;
	box-shadow: 0 14px 42px rgba(0, 0, 0, .12);
}

.attachment-strip {
	display: flex;
	gap: 0.62rem;
	overflow-x: auto;
	padding: 0.2rem 0.25rem 0.45rem;
	scrollbar-width: thin;
}

.attachment-card {
	position: relative;
	flex: 0 0 auto;
	width: 82px;
	height: 82px;
	overflow: hidden;
	border: 1px solid rgba(15, 23, 42, 0.10);
	border-radius: 18px;
	background: linear-gradient(180deg, #fff, #f8fafc);
	box-shadow: 0 12px 32px rgba(15, 23, 42, 0.10);
}

.attachment-thumb {
	display: block;
	width: 100%;
	height: 100%;
	cursor: zoom-in;
}

.attachment-thumb :deep(.el-image__inner) {
	width: 100%;
	height: 100%;
	object-fit: cover;
}

.attachment-file-tile {
	display: grid;
	height: 100%;
	place-items: center;
	padding: 0.45rem;
	text-align: center;
	color: #475569;
	font-size: 10px;
	line-height: 1.2;
}

.attachment-file-tile span {
	max-width: 100%;
	overflow: hidden;
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	word-break: break-all;
	font-weight: 700;
}

.attachment-file-tile small {
	color: #94a3b8;
}

.attachment-remove {
	position: absolute;
	top: 0.35rem;
	right: 0.35rem;
	display: grid;
	width: 1.35rem;
	height: 1.35rem;
	place-items: center;
	border: 0;
	border-radius: 999px;
	background: rgba(15, 23, 42, 0.76);
	color: white;
	cursor: pointer;
	opacity: 0;
	transform: scale(0.88);
	transition: opacity .16s ease, transform .16s ease, background .16s ease;
}

.attachment-card:hover .attachment-remove,
.attachment-remove:focus-visible {
	opacity: 1;
	transform: scale(1);
}

.attachment-remove:hover {
	background: rgba(220, 38, 38, 0.92);
}

.attachment-remove svg {
	width: 0.82rem;
	height: 0.82rem;
}

.file-preview-icon {
	width: 1.45rem;
	height: 1.45rem;
	color: #64748b;
}

.composer-textarea {
	display: block;
	width: 100%;
	min-height: 3.4rem;
	max-height: 13.5rem;
	resize: none;
	overflow-y: auto;
	border: 0;
	outline: 0;
	background: transparent;
	padding: 0.45rem 0.75rem;
	color: #111827;
	font-size: 14px;
	line-height: 1.65;
}

.composer-textarea::placeholder {
	color: #a1a1aa;
}

.composer-toolbar {
	display: grid;
	grid-template-columns: minmax(0, 1fr) auto;
	align-items: center;
	gap: 0.6rem;
	padding: 0.15rem 0.1rem 0;
}

.tool-btn {
	display: grid;
	width: 2rem;
	height: 2rem;
	place-items: center;
	border: 0;
	border-radius: 999px;
	background: transparent;
	color: #5f6b66;
	cursor: pointer;
}

.tool-btn svg {
	width: 1rem;
	height: 1rem;
}

.tool-btn-accent {
	color: var(--bear-accent);
}

.tool-btn:hover, .tool-btn-active {
	background: #f4f4f5;
	color: #111827;
}

.tool-btn:disabled {
	opacity: .38;
	cursor: not-allowed;
}

.composer-status {
	display: flex;
	align-items: center;
	gap: 0.35rem;
}

.status-chip {
	display: inline-flex;
	max-width: 11rem;
	height: 2rem;
	align-items: center;
	gap: 0.28rem;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	border: 0;
	border-radius: 999px;
	background: transparent;
	padding: 0 0.5rem;
	color: #52525b;
	font-size: 12px;
}

.chip-icon {
	width: 0.9rem;
	height: 0.9rem;
	flex: 0 0 auto;
}

.run-config-chip {
	max-width: min(30rem, 54vw);
	height: 1.78rem;
	gap: 0.34rem;
	background: transparent;
	padding: 0 0.34rem 0 0.48rem;
	color: #71717a;
	font-size: 11.2px;
}

.run-config-chip:hover,
.run-config-chip.status-chip-active {
	background: #f4f4f5;
	color: #27272a;
}

.run-config-chip-main {
	display: inline-flex;
	min-width: 0;
	align-items: center;
	gap: 0.28rem;
	overflow: hidden;
}

.run-config-chip-model {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-weight: 640;
}

.run-config-chip-strategy {
	flex: 0 0 auto;
	white-space: nowrap;
	color: #94a3b8;
	font-weight: 520;
}

.run-config-chip-meta {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: #94a3b8;
	font-weight: 520;
}

.run-config-chip-meta::before, .run-config-chip-strategy::before {
	content: "·";
	margin-right: 0.28rem;
	color: #d4d4d8;
}

.chip-caret {
	width: 0.72rem;
	height: 0.72rem;
	flex: 0 0 auto;
	color: #a1a1aa;
	transition: transform .14s ease, color .14s ease;
}

.run-config-chip.status-chip-active .chip-caret {
	transform: rotate(180deg);
	color: #71717a;
}

.status-chip strong {
	color: #334155;
	font-weight: 650;
}

button.status-chip {
	cursor: pointer;
}

button.status-chip:hover, .status-chip-active {
	background: #f4f4f5;
	color: #111827;
}

/* Model picker: quiet macOS surfaces, one accent, two readable type sizes. */
.popover-menu-content { overflow: hidden; border-radius: 12px; }
.run-config-popover {
	--rc-text: #2c3038;
	--rc-muted: #787e8b;
	--rc-line: #e5e7eb;
	--rc-surface: #f3f4f6;
	--rc-hover: #f5f6f8;
	--rc-selected: #edf2fc;
	--rc-control: #ffffff;
	--rc-accent: #3578df;
	--rc-track: #d8dbe2;
	--rc-detail-bg: #30343b;
	--rc-detail-text: #f8fafc;
	--rc-detail-line: #484e58;
	position: relative;
	display: flex;
	flex-direction: column;
	gap: 10px;
	width: 100%;
	max-height: min(76vh, 600px);
	max-height: min(76dvh, 600px);
	color: var(--rc-text);
	font-size: 13px;
	line-height: 1.45;
}
.run-config-popover button, .run-config-popover input { font-family: inherit; }
.run-config-tabs {
	display: flex;
	flex: 0 0 auto;
	gap: 3px;
	padding: 3px;
	border-radius: 9px;
	background: var(--rc-surface);
}
.run-config-tabs button {
	flex: 1;
	min-width: 0;
	padding: 5px 12px;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: var(--rc-muted);
	font-size: 13px;
	font-weight: 500;
	line-height: 20px;
	cursor: pointer;
	transition: background-color .15s ease, color .15s ease, box-shadow .15s ease;
}
.run-config-tabs button:hover:not(.is-active) { color: var(--rc-text); }
.run-config-tabs button.is-active {
	background: var(--rc-control);
	color: var(--rc-text);
	box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08), 0 0.5px 1px rgba(15, 23, 42, 0.04);
}
.run-config-context {
	flex: 0 0 auto;
	padding: 6px 8px;
	border-radius: 8px;
	background: var(--rc-surface);
}
.context-meter-row, .run-config-control-head, .fast-control { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.config-label { display: inline-flex; align-items: center; flex: 0 0 auto; gap: 7px; font-size: 13px; font-weight: 500; white-space: nowrap; }
.config-label svg { width: 15px; height: 15px; flex: 0 0 auto; color: var(--rc-muted); }
.context-meter-values { display: flex; align-items: baseline; gap: 8px; font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap; }
.context-meter-limit { color: var(--rc-muted); }
.context-percent { font-weight: 500; color: var(--rc-text); }
.context-meter { height: 5px; margin-top: 6px; overflow: hidden; border-radius: 5px; background: rgba(0, 0, 0, 0.06); }
html.dark .context-meter { background: rgba(255, 255, 255, 0.08); }
.context-meter span { display: block; height: 100%; border-radius: inherit; background: var(--rc-accent); transition: width .2s ease; }
.run-config-notice { margin: 0 4px; color: var(--rc-muted); font-size: 12px; }
.model-search {
	display: flex;
	align-items: center;
	flex: 0 0 auto;
	gap: 8px;
	margin: 0;
	padding: 6px 10px;
	border: 1px solid var(--rc-line);
	border-radius: 8px;
	background: var(--rc-control);
	color: var(--rc-muted);
	transition: border-color .15s ease, box-shadow .15s ease;
}
.model-search:focus-within {
	border-color: var(--rc-accent);
	box-shadow: 0 0 0 2px rgba(53, 120, 223, 0.15);
}
.model-search svg { width: 14px; height: 14px; flex: 0 0 auto; color: var(--rc-muted); }
.model-search input { min-width: 0; width: 100%; padding: 0; border: 0; outline: 0; background: transparent; color: var(--rc-text); font-size: 13px; line-height: 20px; }
.model-search input::placeholder { color: var(--rc-muted); }
.run-config-model-section { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.run-config-model-list {
	min-height: 0;
	max-height: min(42vh, 320px);
	flex: 1 1 auto;
	overflow-y: auto;
	overscroll-behavior: contain;
	scrollbar-width: thin;
	scrollbar-color: var(--rc-track) transparent;
	padding: 0 2px;
}
.model-group { margin: 0; }
.model-group + .model-group { margin-top: 10px; }
.model-group-title { padding: 4px 8px 3px; color: var(--rc-muted); font-size: 12px; font-weight: 500; letter-spacing: 0.02em; }
.model-row {
	display: flex;
	align-items: center;
	gap: 6px;
	width: 100%;
	padding: 6px 8px;
	border: 1px solid transparent;
	border-radius: 8px;
	background: transparent;
	color: var(--rc-text);
	text-align: left;
	cursor: pointer;
	transition: background-color .14s ease, border-color .14s ease;
}
.model-row:hover { background: var(--rc-hover); }
.model-row.is-selected {
	background: var(--rc-selected);
	border-color: rgba(53, 120, 223, 0.2);
}
html.dark .model-row.is-selected {
	border-color: rgba(106, 157, 241, 0.28);
}
.model-select {
	display: flex;
	align-items: center;
	flex: 1 1 auto;
	min-width: 0;
	align-self: stretch;
	padding: 0;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: inherit;
	text-align: left;
	cursor: pointer;
}
.follow-model-row { padding: 6px 8px; }
.model-row-main {
	display: flex;
	flex-direction: column;
	gap: 4px;
	flex: 1 1 auto;
	min-width: 0;
}
.model-row-title-row {
	display: flex;
	align-items: center;
	gap: 6px;
	min-width: 0;
}
.model-row-name {
	overflow: hidden;
	white-space: nowrap;
	text-overflow: ellipsis;
	font-size: 13px;
	line-height: 18px;
	font-weight: 500;
	transition: color .14s ease;
}
.model-row.is-selected .model-row-name {
	color: var(--rc-accent);
}
.model-row-tags {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 4px;
}
.model-tag {
	display: inline-flex;
	align-items: center;
	gap: 3px;
	font-size: 12px;
	line-height: 16px;
	padding: 1px 6px;
	border-radius: 4px;
	background: var(--rc-surface);
	color: var(--rc-muted);
	font-variant-numeric: tabular-nums;
	white-space: nowrap;
}
.model-tag-feature {
	color: var(--rc-text);
	background: rgba(53, 120, 223, 0.08);
}
html.dark .model-tag-feature {
	background: rgba(106, 157, 241, 0.12);
	color: var(--rc-text);
}
.model-tag .model-feature-icon {
	width: 12px;
	height: 12px;
}
.config-label .model-feature-icon { color: var(--rc-muted); }
.model-selection-mark {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 18px;
	height: 18px;
	flex: 0 0 auto;
	margin-left: 4px;
}
.model-selected-icon { width: 15px; height: 15px; flex: 0 0 auto; color: var(--rc-accent); }
.agent-follow-group { margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--rc-line); }
.model-follow-detail { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--rc-muted); font-size: 12px; }
.model-empty { padding: 22px 10px; text-align: center; color: var(--rc-muted); font-size: 13px; }
.run-config-controls {
	display: flex;
	flex: 0 0 auto;
	flex-direction: column;
	border-top: 1px solid var(--rc-line);
	padding: 2px 2px 0;
	margin-top: 2px;
}
.thinking-control { display: grid; gap: 8px; padding: 9px 0 8px; }
.config-hint { color: var(--rc-muted); font-size: 12px; font-weight: 400; white-space: nowrap; }
.thinking-segments {
	display: flex;
	flex-wrap: wrap;
	gap: 3px;
	padding: 3px;
	border-radius: 8px;
	background: var(--rc-surface);
}
.thinking-segments button {
	flex: 1;
	min-width: 34px;
	padding: 4px 7px;
	border: 0;
	border-radius: 6px;
	color: var(--rc-muted);
	background: transparent;
	font-size: 12px;
	font-weight: 500;
	line-height: 20px;
	white-space: nowrap;
	cursor: pointer;
	transition: background-color .15s ease, color .15s ease, box-shadow .15s ease;
}
.thinking-segments button:hover:not(:disabled) { color: var(--rc-text); }
.thinking-segments button.is-active {
	color: var(--rc-text);
	background: var(--rc-control);
	box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08), 0 0.5px 1px rgba(15, 23, 42, 0.04);
}
.thinking-segments button:disabled { opacity: .4; cursor: not-allowed; }
.fast-control {
	min-height: 38px;
	padding: 6px 0;
	border-top: 1px solid var(--rc-line);
}
.config-value { display: flex; align-items: center; gap: 9px; }
.agent-fast-segments { flex: 0 0 auto; }
.agent-fast-segments button { min-width: 30px; padding: 2px 8px; font-size: 12px; }
.fast-switch {
	position: relative;
	width: 36px;
	height: 22px;
	flex: 0 0 auto;
	padding: 2px;
	border: 0;
	border-radius: 11px;
	background: var(--rc-track);
	cursor: pointer;
	transition: background-color .18s cubic-bezier(0.4, 0, 0.2, 1);
}
.fast-switch span {
	display: block;
	width: 18px;
	height: 18px;
	border-radius: 50%;
	background: #fff;
	box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2), 0 0.5px 1px rgba(0, 0, 0, 0.1);
	transition: transform .18s cubic-bezier(0.4, 0, 0.2, 1);
}
.fast-switch.is-on { background: var(--rc-accent); }
.fast-switch.is-on span { transform: translateX(14px); }
.fast-switch:disabled { opacity: .45; cursor: not-allowed; }
.run-config-popover button:focus-visible { outline: 2px solid var(--rc-accent); outline-offset: -2px; }
.run-config-popover .fast-switch:focus-visible { outline-offset: 3px; }
@media (max-height: 620px) {
	.run-config-popover { overflow-y: auto; }
	.run-config-model-section { flex: 0 0 auto; }
	.run-config-model-list { flex: 0 0 auto; max-height: 180px; }
}
@media (prefers-reduced-motion: reduce) {
	.run-config-popover *, .run-config-popover *::before { transition: none; }
}

.send-button {
	width: 2rem;
	height: 2rem;
	flex: 0 0 auto;
	display: grid;
	place-items: center;
	border: 0;
	border-radius: 999px;
	background: #18181b;
	color: white;
	line-height: 1;
	cursor: pointer;
	box-shadow: 0 8px 18px rgba(15, 23, 42, .18);
}

.send-button svg {
	width: 1rem;
	height: 1rem;
}

.stop-button {
	background: #dc2626;
	box-shadow: 0 8px 18px rgba(220, 38, 38, .18);
}

.send-button:disabled {
	background: #d1d5db;
	cursor: not-allowed;
}

@media (max-width: 760px) {
	.composer-shell { padding: .5rem .75rem; }
	.composer-box { padding: .375rem; border-radius: 20px; }
	.composer-toolbar {
		display: flex;
		flex-wrap: nowrap;
		gap: 0;
		padding: 0;
	}
	.composer-actions { flex: 0 0 auto; gap: 0; }
	.composer-status {
		flex: 1 1 0;
		min-width: 0;
		justify-content: flex-end;
		flex-wrap: nowrap;
		gap: 0;
	}
	/* The hit area stays generous; the model is a quiet text link, not a pill.
	   Scope above the global dark .status-chip rule from interaction cards. */
	.composer-toolbar button.run-config-chip {
		flex: 0 1 auto;
		min-width: 0;
		max-width: 100%;
		appearance: none;
		border: 0;
		border-radius: 6px;
		background: transparent;
		box-shadow: none;
		padding-inline: .375rem;
	}
	.run-config-chip-model { font-weight: 500; }
	.composer-clear:disabled { display: none; }
	.composer-toolbar button.run-config-chip:focus-visible { outline: 2px solid var(--bear-accent); outline-offset: -2px; }
	.run-config-chip-meta { display: none; }
	.composer-hints { display: none; }
	:deep(.reference-editor-content) { min-height: min(4.5rem, calc(var(--mobile-viewport-height, 100dvh) * .22)); padding: .65rem .5rem; }
	:deep(.reference-editor-placeholder) { padding: .65rem .5rem; }
}

@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	/* In an exceptionally short landscape/keyboard viewport, keep every input
	   action reachable inside the composer rather than overflowing the page. */
	.composer-shell { max-height: 100%; overflow-y: auto; pointer-events: auto; }
	/* Larger hit areas, not larger glyphs or global control typography. */
	.tool-btn, .send-button {
		width: 44px;
		height: 44px;
		flex: 0 0 auto;
	}
	.run-config-chip { min-height: 44px; }
	.attachment-remove {
		top: 0;
		right: 0;
		width: 44px;
		height: 44px;
		opacity: 1;
		transform: none;
		/* Keep the small macOS remove badge inside its 44px tap target. */
		background: radial-gradient(circle, rgba(15, 23, 42, .8) 0 12px, transparent 13px);
	}
	:global(html.dark) .attachment-remove {
		background: radial-gradient(circle, rgba(15, 23, 42, .8) 0 12px, transparent 13px);
	}
	.attachment-remove:hover, :global(html.dark) .attachment-remove:hover {
		background: radial-gradient(circle, rgba(220, 38, 38, .92) 0 12px, transparent 13px);
	}
	:deep(.reference-editor-content) { max-height: min(13.5rem, calc(var(--mobile-viewport-height, 100dvh) * .28)); }
}
</style>

<style>
.run-config-menu-popper.el-popper {
	padding: 10px;
	border: 1px solid rgba(226, 228, 233, 0.9);
	border-radius: 16px;
	background: rgba(255, 255, 255, 0.98);
	box-shadow: 0 16px 40px -6px rgba(15, 23, 42, 0.14), 0 0 1px 1px rgba(15, 23, 42, 0.05);
	backdrop-filter: blur(24px) saturate(180%);
	-webkit-backdrop-filter: blur(24px) saturate(180%);
}
html.dark .run-config-menu-popper.el-popper {
	border-color: rgba(62, 65, 74, 0.85);
	background: rgba(25, 26, 30, 0.96);
	box-shadow: 0 20px 48px -8px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.08);
}
/* OpenBear system dark theme */
html.dark .steering-queue-card {
		border: 1px solid rgba(96, 165, 250, 0.22);
		background: linear-gradient(180deg, rgba(32, 33, 37, 0.98), rgba(29, 30, 34, 0.96));
		box-shadow: 0 16px 40px rgba(37, 99, 235, 0.1);
	}
html.dark .steering-queue-title {
		color: #60a5fa;
	}
html.dark .steering-queue-hint {
		color: #c6c6cd;
	}
html.dark .steering-queue-item {
		border: 1px solid rgba(96, 165, 250, 0.16);
		background: rgba(29, 30, 34, 0.82);
		color: #dedee1;
	}
html.dark .interaction-card-meta > span + span::before {
		color: #7b7b82;
	}
html.dark .interaction-expiry.is-expired {
		color: #fb8585;
	}
html.dark .interaction-risk.warning {
		color: #fbad66;
	}
html.dark .interaction-risk.danger {
		color: #fb8585;
	}
html.dark .interaction-expired-notice,
html.dark .interaction-submit-error {
		border: 1px solid rgba(251, 133, 133, 0.52);
		background: #1d1e22;
		color: #fb8585;
	}
html.dark .web-interaction-input {
		color: #c6c6cd;
	}
html.dark .web-interaction-input textarea {
		border: 1px solid #3d3e46;
	}
html.dark .web-confirm-btn.reject {
		border-color: #3d3e46;
		background: #202125;
		color: #c6c6cd;
	}
html.dark .web-confirm-btn.confirm {
		color: #ffffff;
	}
html.dark .questionnaire-question {
		border: 1px solid #3d3e46;
		background: rgba(29, 30, 34, 0.9);
	}
html.dark .questionnaire-question.has-error {
		border-color: rgba(251, 133, 133, 0.52);
	}
html.dark .questionnaire-question legend {
		color: #dedee1;
	}
html.dark .question-number {
		background: #202125;
		color: #dedee1;
	}
html.dark .required-mark {
		background: #202125;
		color: #fb8585;
	}
html.dark .optional-mark {
		background: #202125;
		color: #c6c6cd;
	}
html.dark .question-choice-option {
		border: 1px solid #3d3e46;
		background: #1d1e22;
	}
html.dark .question-choice-option.is-selected {
		border-color: rgba(96, 165, 250, 0.52);
		background: #202125;
	}
html.dark .question-choice-copy {
		color: #dedee1;
	}
html.dark .question-choice-copy > small {
		color: #c6c6cd;
	}
html.dark .recommendation-badge {
		border: 1px solid #3d3e46;
		background: #202125;
		color: #60a5fa;
	}
html.dark .recommendation-reason {
		border-left: 2px solid rgba(96, 165, 250, 0.52);
		color: #c6c6cd;
	}
html.dark .recommendation-reason strong {
		color: #60a5fa;
	}
html.dark .clear-question-choice {
		color: #60a5fa;
	}
html.dark .question-free-text {
		color: #c6c6cd;
	}
html.dark .question-free-text textarea {
		border: 1px solid #3d3e46;
		background: #1d1e22;
		color: #dedee1;
	}
html.dark .question-free-text textarea:focus {
		border-color: rgba(96, 165, 250, 0.52);
	}
html.dark .question-hint {
		color: #c6c6cd;
	}
html.dark .question-error {
		color: #fb8585;
	}
html.dark .questionnaire-actions {
		border-top: 1px solid #3d3e46;
		background: rgba(29, 30, 34, 0.97);
	}
html.dark .composer-box {
		border: 1px solid #3d3e46;
		background: #1d1e22;
		box-shadow: 0 10px 34px rgba(0, 0, 0, 0.16);
	}
html.dark .composer-box:focus-within {
		border-color: #3d3e46;
		box-shadow: 0 14px 42px rgba(0, 0, 0, 0.16);
	}
html.dark .attachment-card {
		border: 1px solid rgba(255, 255, 255, 0.145);
		background: linear-gradient(180deg, #1d1e22, #1d1e22);
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.16);
	}
html.dark .attachment-file-tile {
		color: #c6c6cd;
	}
html.dark .attachment-file-tile small {
		color: #a1a1a8;
	}
html.dark .attachment-remove {
		background: rgba(255, 255, 255, 0.34);
	}
html.dark .file-preview-icon {
		color: #c6c6cd;
	}
html.dark .composer-textarea {
		color: #efeff2;
	}
html.dark .composer-textarea::placeholder {
		color: #a1a1a8;
	}
html.dark .tool-btn {
		color: #c6c6cd;
	}
html.dark .tool-btn:hover,
html.dark .tool-btn-active {
		background: #202125;
		color: #efeff2;
	}
html.dark .status-chip {
		color: #c6c6cd;
	}
html.dark .run-config-chip {
		color: #c6c6cd;
	}
html.dark .run-config-chip:hover,
html.dark .run-config-chip.status-chip-active {
		background: #202125;
		color: #efeff2;
	}
html.dark .run-config-chip-meta, html.dark .run-config-chip-strategy {
		color: #a1a1a8;
	}
html.dark .run-config-chip-meta::before, html.dark .run-config-chip-strategy::before {
		color: #7b7b82;
	}
html.dark .chip-caret {
		color: #a1a1a8;
	}
html.dark .run-config-chip.status-chip-active .chip-caret {
		color: #c6c6cd;
	}
html.dark .status-chip strong {
		color: #dedee1;
	}
html.dark button.status-chip:hover,
html.dark .status-chip-active {
		background: #202125;
		color: #efeff2;
	}
html.dark .run-config-popover {
	--rc-text: #e5e6e9;
	--rc-muted: #a3a7af;
	--rc-line: #3a3d43;
	--rc-surface: #282b31;
	--rc-hover: #2e3138;
	--rc-selected: #363941;
	--rc-control: #41454e;
	--rc-accent: #6a9df1;
	--rc-track: #535963;
	--rc-detail-bg: #e5e7eb;
	--rc-detail-text: #242831;
	--rc-detail-line: #f3f4f6;
}
html.dark .send-button {
		background: #232428;
		box-shadow: 0 8px 18px rgba(0, 0, 0, 0.18);
	}
html.dark .stop-button {
		box-shadow: 0 8px 18px rgba(220, 38, 38, 0.18);
	}
html.dark .send-button:disabled {
		background: #2b2c30;
	}
</style>
