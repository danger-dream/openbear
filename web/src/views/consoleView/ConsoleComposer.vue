<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref} from "vue";
import {
	ArrowDown,
	CircleCheck,
	Close,
	Cpu,
	DataAnalysis,
	Document,
	Lightning,
	MagicStick,
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
	thinkingDesc,
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

const props = defineProps({
	draft: {type: String, default: ""},
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
	canCompact: {type: Boolean, default: false},
	compacting: {type: Boolean, default: false},
	modelQuery: {type: String, default: ""},
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
	"select-thinking",
	"toggle-fast-mode",
	"select-agent-model",
	"select-agent-thinking",
	"select-agent-fast",
	"send",
	"compact",
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
const questionnaireDrafts = ref({});
const questionnaireErrors = ref({});
const runConfigTab = ref("main"); // main | agent
const DEFAULT_COMPACT_RATIO = 0.7;
const runConfigPopoverVisible = computed({
	get: () => props.modelMenuOpen,
	set: (value) => {
		if (value && !props.modelMenuOpen) emit("toggle-model-menu");
		else if (!value && props.modelMenuOpen) emit("close-menus");
	},
});
const runConfigModelText = computed(() => props.currentModelInfo ? modelShortLabel(props.currentModelInfo) : "模型");
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
const agentModelInfo = computed(() => {
	const key = props.agentModel || props.agentEffectiveModel || props.currentModel || "";
	for (const group of props.modelGroups || []) {
		const hit = (group.models || []).find((m) => m.key === key);
		if (hit) return hit;
	}
	return null;
});
const agentFastTriState = computed(() => {
	if (props.agentFastMode === true) return "on";
	if (props.agentFastMode === false) return "off";
	return "follow";
});
const isAgentTab = computed(() => runConfigTab.value === "agent");
const headTitleText = computed(() => isAgentTab.value ? "Agent 默认" : "运行配置");
const headModelText = computed(() => {
	if (!isAgentTab.value) {
		return props.currentModelInfo ? modelShortLabel(props.currentModelInfo) : "选择模型";
	}
	if (!props.agentModel) return "跟随主模型";
	return agentModelInfo.value ? modelShortLabel(agentModelInfo.value) : (props.agentModel || "选择模型");
});
const headModelKey = computed(() => {
	if (!isAgentTab.value) return props.currentModelInfo?.key || props.currentModel || "未选择模型";
	if (!props.agentModel) return `跟随 ${props.currentModel || "主模型"}`;
	return props.agentModel;
});
const headMetaText = computed(() => {
	if (props.running) {
		return isAgentTab.value ? "运行中修改将在下一次新 Agent 生效" : "运行中修改将在下一次调用生效";
	}
	if (!isAgentTab.value) return `默认思考 ${currentDefaultThinkingLabel.value}`;
	const think = props.agentSupportsThinking
		? (props.agentThinkLevel ? thinkingLabel(props.agentThinkLevel) : `默认 ${props.agentDefaultThinkingLabel}`)
		: "无思考档";
	const fast = agentFastTriState.value === "follow"
		? `Fast 跟随主会话(${props.currentFast ? "开" : "关"})`
		: (agentFastTriState.value === "on" ? "Fast 开" : "Fast 关");
	return `${think} · ${fast}`;
});
const contextPercentNumber = computed(() => {
	const value = Number(String(props.contextPercentDisplay || "").replace("%", ""));
	return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
});
const contextMeterStyle = computed(() => ({width: `${contextPercentNumber.value}%`}));

function compactTriggerForModel(model) {
	const explicit = Number(model?.compactTriggerTokens || 0);
	if (explicit > 0) return explicit;
	const ratio = Number(model?.compactRatio || DEFAULT_COMPACT_RATIO);
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
	return String(item?.action || "confirm");
}

function optionLabel(option) {
	if (option && typeof option === "object") return String(option.label || option.text || option.value || "");
	return String(option || "");
}

function optionValue(option) {
	if (option && typeof option === "object") return String(option.value ?? option.label ?? option.text ?? "");
	return String(option || "");
}

function ensureInteractionDraft(item) {
	const id = item?.confirmationId;
	if (!id) return {selectedIndexes: [], value: ""};
	if (!interactionDrafts.value[id]) {
		const defaults = Array.isArray(item.defaultIndexes) ? item.defaultIndexes.map((x) => Number(x)).filter(Number.isFinite) : [];
		const defaultValues = new Set(Array.isArray(item.defaultValues) ? item.defaultValues.map((x) => String(x)) : []);
		const selectedIndexes = defaults.length ? defaults : [];
		if (!selectedIndexes.length && defaultValues.size && Array.isArray(item.options)) {
			item.options.forEach((option, idx) => {
				if (defaultValues.has(optionValue(option)) || defaultValues.has(optionLabel(option))) selectedIndexes.push(idx);
			});
		}
		interactionDrafts.value[id] = {
			selectedIndexes: item.multiple ? selectedIndexes : selectedIndexes.slice(0, 1),
			value: String(item.defaultValue || ""),
		};
	}
	return interactionDrafts.value[id];
}

function optionChecked(item, idx) {
	return ensureInteractionDraft(item).selectedIndexes.includes(idx);
}

function toggleOption(item, idx) {
	const draft = ensureInteractionDraft(item);
	if (item.multiple) {
		draft.selectedIndexes = draft.selectedIndexes.includes(idx)
			? draft.selectedIndexes.filter((x) => x !== idx)
			: [...draft.selectedIndexes, idx];
	} else {
		draft.selectedIndexes = [idx];
	}
}

function setPromptValue(item, value) {
	ensureInteractionDraft(item).value = value;
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
	if (props.confirmationSubmitting[item?.confirmationId]) return;
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
	if (props.confirmationSubmitting[item?.confirmationId]) return;
	emit("answer-confirmation", item, buildQuestionnaireAnswer(questionnaireQuestions(item), ensureQuestionnaireDraft(item), true));
}

function answerInteraction(item, intent) {
	const action = interactionAction(item);
	if (action === "select") {
		const draft = ensureInteractionDraft(item);
		const indexes = intent === "cancel" ? [] : draft.selectedIndexes;
		emit("answer-confirmation", item, {
			cancelled: intent === "cancel",
			selectedIndexes: indexes,
			selectedValues: indexes.map((idx) => optionValue(item.options?.[idx])),
		});
		return;
	}
	if (action === "prompt") {
		const draft = ensureInteractionDraft(item);
		emit("answer-confirmation", item, {
			cancelled: intent === "cancel",
			value: intent === "cancel" ? "" : draft.value,
		});
		return;
	}
	emit("answer-confirmation", item, {confirmed: intent === "confirm", cancelled: intent === "cancel"});
}

function handleKeydown(event) {
	if (event.key !== "Enter") return;
	if (event.shiftKey) return;
	if (event.isComposing) return;
	event.preventDefault();
	emit("send");
}

onMounted(() => {
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
	composerResizeObserver?.disconnect();
	composerResizeObserver = null;
});

defineExpose({focus, adjustHeight, openFilePicker});
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
					<div v-for="item in props.pendingSteering" :key="item.id" class="steering-queue-item">{{ item.text }}</div>
				</div>
			</div>

			<div v-if="props.pendingConfirmations.length" class="web-confirm-stack">
				<div v-for="item in props.pendingConfirmations" :key="item.confirmationId" class="web-confirm-card"
				     :class="{'questionnaire-card': interactionAction(item) === 'questionnaire'}"
				     :data-questionnaire-id="interactionAction(item) === 'questionnaire' ? item.confirmationId : undefined">
					<template v-if="interactionAction(item) === 'questionnaire'">
						<div class="web-confirm-title questionnaire-title">
							<Warning/>
							<div>
								<strong>{{ item.title || '需要你补充一些信息' }}</strong>
								<span>请按实际情况回答；选择题也可以直接填写自己的答案。</span>
							</div>
						</div>
						<p v-if="item.body" class="questionnaire-intro">{{ item.body }}</p>
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
								<p v-if="question.description" class="question-description">{{ question.description }}</p>
								<template v-if="question.type === 'choice'">
									<div class="question-choice-list">
										<label v-for="(option, optionIndex) in question.options || []"
										       :key="`${questionnaireQuestionId(question)}-${optionIndex}`"
										       class="question-choice-option"
										       :class="{'is-selected': questionnaireChoiceSelected(item, question, option)}">
											<input :type="question.multiple ? 'checkbox' : 'radio'"
											       :name="`question-${item.confirmationId}-${questionnaireQuestionId(question)}`"
											       :checked="questionnaireChoiceSelected(item, question, option)"
											       :disabled="props.confirmationSubmitting[item.confirmationId]"
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
									        :disabled="props.confirmationSubmitting[item.confirmationId]"
									        @click="clearQuestionnaireSelection(item, question)">清除选择</button>
									<label class="question-free-text" :for="`${questionnaireFieldId(item, question, questionIndex)}-text`">
										<span>{{ questionnaireAnswer(item, question).selectedValues.length ? '补充、限制或修正以上选择' : '也可以不选，直接填写自己的答案' }}</span>
										<textarea :id="`${questionnaireFieldId(item, question, questionIndex)}-text`"
										          :value="questionnaireAnswer(item, question).text" rows="2"
										          :disabled="props.confirmationSubmitting[item.confirmationId]"
										          placeholder="写下更符合你需要的答案"
										          @input="setQuestionnaireText(item, question, $event.target.value)"></textarea>
									</label>
								</template>
								<label v-else class="question-free-text open-answer" :for="`${questionnaireFieldId(item, question, questionIndex)}-text`">
									<span>你的回答</span>
									<textarea :id="`${questionnaireFieldId(item, question, questionIndex)}-text`"
									          :value="questionnaireAnswer(item, question).text" rows="3"
									          :disabled="props.confirmationSubmitting[item.confirmationId]"
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
						<div v-if="props.confirmationErrors[item.confirmationId]" class="questionnaire-submit-error" role="alert">
							{{ props.confirmationErrors[item.confirmationId] }}
						</div>
						<div class="web-confirm-actions questionnaire-actions">
							<button type="button" class="web-confirm-btn cancel"
							        :disabled="props.confirmationSubmitting[item.confirmationId]"
							        @click="cancelQuestionnaire(item)">暂不回答</button>
							<button type="button" class="web-confirm-btn confirm"
							        :disabled="props.confirmationSubmitting[item.confirmationId]"
							        :aria-busy="props.confirmationSubmitting[item.confirmationId] ? 'true' : 'false'"
							        @click="submitQuestionnaire(item)">{{ props.confirmationSubmitting[item.confirmationId] ? '提交中…' : '提交回答' }}</button>
						</div>
					</template>
					<template v-else>
						<div class="web-confirm-title">
							<Warning/>
							{{ item.title || '请确认' }}
						</div>
						<pre class="web-confirm-body">{{ item.body }}</pre>
						<div v-if="interactionAction(item) === 'select'" class="web-interaction-options">
							<label v-for="(option, idx) in item.options || []" :key="`${item.confirmationId}-${idx}`" class="web-interaction-option">
								<input :type="item.multiple ? 'checkbox' : 'radio'" :name="`interaction-${item.confirmationId}`"
								       :checked="optionChecked(item, idx)" @change="toggleOption(item, idx)"/>
								<span>{{ optionLabel(option) }}</span>
							</label>
						</div>
						<div v-else-if="interactionAction(item) === 'prompt'" class="web-interaction-prompt">
							<textarea :value="ensureInteractionDraft(item).value" :placeholder="item.sensitive ? '输入内容' : '请输入内容'"
							          rows="3" @input="setPromptValue(item, $event.target.value)"></textarea>
							<div v-if="item.sensitive" class="web-interaction-hint">此输入会原样提交给当前任务。</div>
						</div>
						<div class="web-confirm-actions">
							<button type="button" class="web-confirm-btn cancel" @click="answerInteraction(item, 'cancel')">{{ item.cancelText || '取消' }}</button>
							<button type="button" class="web-confirm-btn confirm" @click="answerInteraction(item, 'confirm')">{{ item.confirmText || (interactionAction(item) === 'select' ? '确认选择' : '确认') }}</button>
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
							<button type="button" class="attachment-remove" aria-label="移除附件"
							        @click.stop="emit('remove-attachment', item.id)">
								<Close/>
							</button>
						</el-tooltip>
					</div>
				</div>
				<textarea
					ref="composerTextarea"
					:value="props.draft"
					rows="1"
					class="composer-textarea"
					placeholder="在这里输入消息，按 Enter 发送"
					@input="onDraftInput"
					@keydown="handleKeydown"
					@paste="onPaste"
				></textarea>
				<div class="composer-toolbar">
					<div class="relative flex min-w-0 flex-1 items-center gap-1.5">
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
						<el-tooltip content="手动压缩上下文" placement="top" :show-after="260">
							<button
								type="button"
								class="tool-btn compact-tool-btn"
								:class="{'is-compacting': props.compacting}"
								:aria-label="props.compacting ? '正在压缩上下文' : '手动压缩上下文'"
								:aria-busy="props.compacting ? 'true' : 'false'"
								:disabled="!props.canCompact || props.compacting"
								@click="emit('compact')"
							>
								<svg class="compact-context-icon" viewBox="0 0 1024 1024" aria-hidden="true" focusable="false">
									<path d="M489.18 342.76c0.98 0.9 2.22 1.35 3.28 2.14 5.34 4.17 11.78 7.08 19.25 7.08 9.87 0 18.4-4.89 24.34-12.05l126.19-126.19 0.51-0.51c12.28-12.29 11.77-32.77-0.51-45.06-12.29-12.29-32.77-12.29-45.06 0.51l-73.21 73.57V95.97c0-17.41-14.34-31.74-31.75-31.74-17.92 0-32.25 13.82-32.25 31.74v147.1l-73.73-73.36c-12.29-12.29-32.26-12.29-44.55 0-12.29 12.28-12.8 32.25-0.51 45.05l128 128zM95.96 447.72h831.5c17.92 0 32.26-14.34 32.26-31.75s-13.82-31.74-31.75-31.74H95.96c-17.41 0-31.74 14.34-31.74 31.74s13.82 31.75 31.74 31.75zM927.97 576.23H95.96c-17.41 0-31.74 14.34-31.74 31.75s13.82 31.74 31.74 31.74h831.5c17.92 0 32.26-14.34 32.26-31.74 0-17.41-13.83-31.75-31.75-31.75zM536.21 684.19c-5.75-7.25-14.27-12.21-24.5-12.21-8.83 0-16.82 3.59-22.59 9.43-0.13 0.13-0.32 0.16-0.45 0.3l-128 128.01c-5.63 6.14-9.22 13.82-9.22 22.52 0 17.41 14.34 31.75 32.26 31.75 8.71 0 16.39-3.58 22.53-9.22l73.73-74.08v147.3c0 17.41 13.83 31.74 31.75 31.74s32.26-14.34 31.74-31.74V780.83l73.21 73.93c12.29 12.29 32.26 12.29 44.55 0s12.8-32.26 0.51-45.05L536.21 684.19z"/>
								</svg>
								<span v-if="props.compacting" class="compact-loading-indicator" aria-hidden="true"></span>
							</button>
						</el-tooltip>
						<el-tooltip content="清空草稿" placement="top" :show-after="260">
							<button type="button" class="tool-btn" aria-label="清空草稿"
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
						            :width="'min(24rem, calc(100vw - 2rem))'"
						            :show-arrow="false"
						            :hide-after="0">
							<template #reference>
								<button type="button" class="status-chip run-config-chip" aria-label="运行配置"
								        :class="props.modelMenuOpen ? 'status-chip-active' : ''">
									<span class="run-config-chip-main">
										<span class="run-config-chip-model">{{ runConfigModelText }}</span>
										<span v-if="runConfigMetaText" class="run-config-chip-meta">{{ runConfigMetaText }}</span>
									</span>
									<ArrowDown class="chip-caret"/>
								</button>
							</template>
							<div class="popover-menu-content run-config-popover">
								<div class="run-config-tabs" role="tablist" aria-label="运行配置切换">
									<button type="button" role="tab"
									        :aria-selected="runConfigTab === 'main'"
									        :class="runConfigTab === 'main' ? 'is-active' : ''"
									        @click="runConfigTab = 'main'">主会话</button>
									<button type="button" role="tab"
									        :aria-selected="runConfigTab === 'agent'"
									        :class="runConfigTab === 'agent' ? 'is-active' : ''"
									        @click="runConfigTab = 'agent'">Agent</button>
								</div>
								<div class="run-config-head" :class="isAgentTab ? 'is-agent' : ''">
									<div class="run-config-title-row">
										<span class="run-config-title"><Cpu/>{{ headTitleText }}</span>
										<strong>{{ headModelText }}</strong>
									</div>
									<div class="run-config-subtitle">
										<span>{{ headModelKey }}</span>
										<span>{{ headMetaText }}</span>
									</div>
									<template v-if="!isAgentTab">
										<div class="context-meter-row">
											<span><DataAnalysis/>上下文 {{ props.contextDisplay }}</span>
											<strong>{{ props.contextPercentDisplay }}</strong>
										</div>
										<div class="context-meter"><span :style="contextMeterStyle"></span></div>
										<div class="run-config-context-detail">
											<span>已用 {{ props.contextUsedDisplay }}</span>
											<span>压缩阈值 {{ props.contextThresholdDisplay }}</span>
											<span>模型窗口 {{ props.contextWindowDisplay }}</span>
										</div>
									</template>
								</div>
								<template v-if="runConfigTab === 'main'">
									<label class="model-search run-config-search">
										<Search/>
										<input :value="props.modelQuery" type="search" placeholder="搜索模型"
										       @input="emit('update:modelQuery', $event.target.value)"/>
									</label>
									<div class="model-list run-config-model-list">
										<div v-for="group in props.modelGroups" :key="group.provider" class="model-group">
											<div class="model-group-title">{{ group.provider }}</div>
											<button v-for="model in group.models" :key="model.key" type="button" class="menu-row model-row"
											        :class="props.currentModel === model.key ? 'menu-row-active' : ''"
											        @click="emit('select-model', model)">
												<span class="menu-icon"><Cpu/></span>
												<span class="min-w-0 flex-1">
													<span class="block truncate font-medium">{{ modelLabel(model) }}</span>
													<span class="model-meta">
														<span>{{ model.protocol || '—' }}</span>
														<span>{{ fmtTokens(compactTriggerForModel(model)) }} trigger</span>
														<span>{{ fmtTokens(model.contextWindow || 0) }} window</span>
														<span v-if="model.reasoning">reasoning</span>
														<span v-if="model.supportsFast">fast</span>
														<span v-if="model.maxTokens">{{ fmtTokens(model.maxTokens) }} out</span>
													</span>
												</span>
												<CircleCheck v-if="props.currentModel === model.key" class="menu-check"/>
											</button>
										</div>
										<div v-if="!props.modelGroups.length" class="model-empty">没有匹配模型</div>
									</div>
									<div class="run-config-controls">
										<div class="run-config-control-head">
											<span><MagicStick/>思考强度</span>
											<small>来自当前模型元数据 · 默认 {{ currentDefaultThinkingLabel }}</small>
										</div>
										<div v-if="props.supportsThinking" class="thinking-segments">
											<button v-for="level in props.currentThinkLevels" :key="level" type="button"
											        :class="props.effectiveThinking === level ? 'is-active' : ''"
											        @click="emit('select-thinking', level)">
												<span>{{ thinkingLabel(level) }}</span>
												<small>{{ thinkingDesc(level) || '可用' }}</small>
											</button>
										</div>
										<div v-else class="control-unavailable">当前模型元数据未声明可用思考强度</div>
										<div class="fast-control" :class="{ 'is-on': props.currentFast, 'is-disabled': !props.fastSupported }">
											<div class="fast-copy">
												<span><Lightning/>Fast 模式</span>
												<small>{{ props.fastSupported ? '当前模型元数据支持 Fast' : '当前模型未标记 Fast 能力' }}</small>
											</div>
											<button type="button" class="fast-switch"
											        :class="props.currentFast ? 'is-on' : ''"
											        :disabled="!props.fastSupported"
											        @click="emit('toggle-fast-mode')">
												<span></span>
											</button>
										</div>
									</div>
								</template>
								<template v-else>
									<label class="model-search run-config-search">
										<Search/>
										<input :value="props.modelQuery" type="search" placeholder="搜索 Agent 模型"
										       @input="emit('update:modelQuery', $event.target.value)"/>
									</label>
									<div class="model-list run-config-model-list">
										<div class="model-group agent-follow-group">
												<button type="button" class="menu-row model-row"
												        :class="!props.agentModel ? 'menu-row-active' : ''"
												        @click="emit('select-agent-model', '')">
													<span class="menu-icon"><Cpu/></span>
													<span class="min-w-0 flex-1">
														<span class="block truncate font-medium">跟随主模型</span>
														<span class="model-meta">
															<span>{{ props.currentModel || '主模型' }}</span>
															<span>随主会话切换</span>
														</span>
													</span>
													<CircleCheck v-if="!props.agentModel" class="menu-check"/>
												</button>
											</div>
										<div v-for="group in props.modelGroups" :key="`agent-${group.provider}`" class="model-group">
											<div class="model-group-title">{{ group.provider }}</div>
											<button v-for="model in group.models" :key="`agent-${model.key}`" type="button" class="menu-row model-row"
											        :class="props.agentModel === model.key ? 'menu-row-active' : ''"
											        @click="emit('select-agent-model', model.key)">
												<span class="menu-icon"><Cpu/></span>
												<span class="min-w-0 flex-1">
													<span class="block truncate font-medium">{{ modelLabel(model) }}</span>
													<span class="model-meta">
														<span>{{ model.protocol || '—' }}</span>
														<span>{{ fmtTokens(compactTriggerForModel(model)) }} trigger</span>
														<span>{{ fmtTokens(model.contextWindow || 0) }} window</span>
														<span v-if="model.reasoning">reasoning</span>
														<span v-if="model.supportsFast">fast</span>
														<span v-if="model.maxTokens">{{ fmtTokens(model.maxTokens) }} out</span>
													</span>
												</span>
												<CircleCheck v-if="props.agentModel === model.key" class="menu-check"/>
											</button>
										</div>
										<div v-if="!props.modelGroups.length" class="model-empty">没有匹配模型</div>
									</div>
									<div class="run-config-controls">
										<div class="run-config-control-head">
											<span><MagicStick/>Agent 思考</span>
											<small>默认 {{ props.agentDefaultThinkingLabel }}</small>
										</div>
										<div class="thinking-segments">
											<button type="button"
											        :class="!props.agentThinkLevel ? 'is-active' : ''"
											        @click="emit('select-agent-thinking', '')">
												<span>模型默认</span>
												<small>{{ props.agentDefaultThinkingLabel }}</small>
											</button>
											<button v-for="level in props.agentThinkLevels" :key="`agent-think-${level}`" type="button"
											        :class="props.agentThinkLevel === level ? 'is-active' : ''"
											        @click="emit('select-agent-thinking', level)">
												<span>{{ thinkingLabel(level) }}</span>
												<small>{{ thinkingDesc(level) || '可用' }}</small>
											</button>
										</div>
										<div v-if="!props.agentSupportsThinking && !props.agentThinkLevels.length" class="control-unavailable">当前 Agent 生效模型未声明思考强度</div>
										<div class="run-config-control-head agent-fast-head">
											<span><Lightning/>Agent Fast</span>
											<small>{{ props.agentFastSupported ? '可独立于主会话' : '生效模型未标记 Fast' }}</small>
										</div>
										<div class="thinking-segments agent-fast-segments">
											<button type="button" :class="agentFastTriState === 'follow' ? 'is-active' : ''" @click="emit('select-agent-fast', null)">
												<span>跟随主会话</span>
												<small>{{ props.currentFast ? '当前开' : '当前关' }}</small>
											</button>
											<button type="button" :class="agentFastTriState === 'on' ? 'is-active' : ''" :disabled="!props.agentFastSupported" @click="emit('select-agent-fast', true)">
												<span>开启</span>
												<small>强制 Fast</small>
											</button>
											<button type="button" :class="agentFastTriState === 'off' ? 'is-active' : ''" @click="emit('select-agent-fast', false)">
												<span>关闭</span>
												<small>强制普通</small>
											</button>
										</div>
									</div>
								</template>
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
			<div class="mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-[11px] text-[#8b949e]">
				<span>Enter 发送 · Ctrl/⌘+Enter 也可发送</span>
				<span>图片/文本附件会随本轮发送</span>
			</div>
		</div>
	</footer>
</template>

<style scoped>
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

.web-confirm-card {
	position: relative;
	z-index: 45;
	border: 1px solid rgba(245, 158, 11, 0.38);
	background: rgba(255, 251, 235, 0.98);
	box-shadow: 0 14px 42px rgba(120, 53, 15, 0.12);
	border-radius: 1rem;
	padding: 0.85rem;
}

.web-confirm-title {
	display: flex;
	align-items: center;
	gap: 0.45rem;
	font-size: 0.82rem;
	font-weight: 700;
	color: #92400e;
}

.web-confirm-title svg {
	width: 1rem;
	height: 1rem;
}

.web-confirm-body {
	margin: 0.55rem 0 0;
	max-height: 12rem;
	overflow: auto;
	white-space: pre-wrap;
	font-size: 0.76rem;
	line-height: 1.45;
	color: #3f3f46;
	font-family: inherit;
}

.web-interaction-options {
	display: grid;
	gap: 0.45rem;
	margin-top: 0.7rem;
}

.web-interaction-option {
	display: flex;
	align-items: center;
	gap: 0.5rem;
	border: 1px solid rgba(217, 119, 6, 0.18);
	border-radius: 0.75rem;
	background: rgba(255, 255, 255, 0.72);
	padding: 0.5rem 0.65rem;
	font-size: 0.8rem;
	color: #3f3f46;
	cursor: pointer;
}

.web-interaction-option input {
	accent-color: #d97706;
}

.web-interaction-prompt {
	margin-top: 0.7rem;
}

.web-interaction-prompt textarea {
	width: 100%;
	resize: vertical;
	border: 1px solid rgba(217, 119, 6, 0.24);
	border-radius: 0.75rem;
	background: rgba(255, 255, 255, 0.78);
	padding: 0.6rem 0.7rem;
	font-size: 0.82rem;
	line-height: 1.45;
	color: #27272a;
	outline: none;
}

.web-interaction-prompt textarea:focus {
	border-color: rgba(217, 119, 6, 0.55);
	box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.12);
}

.web-interaction-hint {
	margin-top: 0.35rem;
	font-size: 0.72rem;
	color: #92400e;
}

.web-confirm-actions {
	display: flex;
	justify-content: flex-end;
	gap: 0.5rem;
	margin-top: 0.7rem;
}

.web-confirm-btn {
	border: 0;
	border-radius: 999px;
	padding: 0.42rem 0.8rem;
	font-size: 0.78rem;
	font-weight: 700;
	cursor: pointer;
}

.web-confirm-btn.cancel {
	background: #f4f4f5;
	color: #52525b;
}

.web-confirm-btn.confirm {
	background: #d97706;
	color: white;
}

.web-confirm-btn:disabled {
	opacity: 0.58;
	cursor: not-allowed;
}

.questionnaire-card {
	max-height: min(70vh, 46rem);
	overflow: auto;
	border-color: rgba(37, 99, 235, 0.28);
	background: rgba(248, 250, 252, 0.98);
	box-shadow: 0 18px 48px rgba(15, 23, 42, 0.14);
}

.questionnaire-title {
	align-items: flex-start;
	color: #1e3a5f;
}

.questionnaire-title > div {
	display: grid;
	gap: 0.18rem;
}

.questionnaire-title strong {
	font-size: 0.9rem;
}

.questionnaire-title span,
.questionnaire-intro,
.question-description {
	font-size: 0.75rem;
	font-weight: 400;
	line-height: 1.5;
	color: #64748b;
	white-space: pre-wrap;
}

.questionnaire-intro {
	margin: 0.65rem 0 0;
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

.questionnaire-submit-error {
	margin-top: 0.65rem;
	border: 1px solid #fecaca;
	border-radius: 0.7rem;
	background: #fef2f2;
	padding: 0.55rem 0.65rem;
	font-size: 0.74rem;
	line-height: 1.45;
	color: #991b1b;
}

.questionnaire-actions {
	position: sticky;
	bottom: -0.85rem;
	margin: 0.75rem -0.85rem -0.85rem;
	border-top: 1px solid #e2e8f0;
	background: rgba(248, 250, 252, 0.97);
	padding: 0.65rem 0.85rem;
}

.questionnaire-actions .confirm {
	background: #46678f;
}

@media (max-width: 640px) {
	.questionnaire-card {
		max-height: 62vh;
	}

	.question-choice-list {
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

.compact-tool-btn {
	width: 2.58rem;
	color: #b26a00;
}

.compact-tool-btn.is-compacting {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: .28rem;
}

.tool-btn.compact-tool-btn:hover {
	background: #fff7e6;
	color: #8a4f00;
}

.compact-context-icon {
	display: block;
	width: 1rem;
	height: 1rem;
	flex: 0 0 auto;
	fill: currentColor;
}

.compact-loading-indicator {
	box-sizing: border-box;
	display: block;
	width: .68rem;
	height: .68rem;
	flex: 0 0 auto;
	border: 1.5px solid rgba(178, 106, 0, .24);
	border-top-color: currentColor;
	border-right-color: rgba(178, 106, 0, .54);
	border-radius: 999px;
	animation: compact-loading-spin .9s linear infinite;
}

@keyframes compact-loading-spin {
	to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
	.compact-loading-indicator { animation: none; }
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

.run-config-chip-meta {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: #94a3b8;
	font-weight: 520;
}

.run-config-chip-meta::before {
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

.popover-menu-content {
	overflow: hidden;
	border-radius: 12px;
}

.run-config-popover {
	display: flex;
	width: 100%;
	max-height: min(72vh, 36rem);
	flex-direction: column;
	gap: 0.42rem;
}

.run-config-head {
	display: grid;
	gap: 0.42rem;
	border: 1px solid rgba(37, 99, 235, 0.12);
	border-radius: 12px;
	background: linear-gradient(180deg, #f8fbff, #ffffff);
	padding: 0.65rem;
}

.run-config-head.is-agent {
	border-color: rgba(124, 58, 237, 0.14);
	background: linear-gradient(180deg, #faf8ff, #ffffff);
}

.run-config-tabs {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 0.2rem;
	padding: 0.16rem;
	border: 1px solid #e2e8f0;
	border-radius: 10px;
	background: #f8fafc;
}

.run-config-tabs button {
	border: 0;
	border-radius: 8px;
	background: transparent;
	padding: 0.38rem 0.45rem;
	color: #64748b;
	font-size: 11.5px;
	font-weight: 750;
	line-height: 1.2;
	cursor: pointer;
	transition: background .12s ease, color .12s ease, box-shadow .12s ease;
}

.run-config-tabs button:hover {
	color: #334155;
}

.run-config-tabs button.is-active {
	background: #ffffff;
	color: #0f172a;
	box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08), inset 0 0 0 1px #e2e8f0;
}



.agent-fast-head {
	margin-top: 0.1rem;
}

.agent-fast-segments button:disabled {
	opacity: 0.45;
	cursor: not-allowed;
}

.run-config-title-row,
.context-meter-row,
.run-config-context-detail,
.run-config-control-head,
.fast-control {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 0.7rem;
}

.run-config-title,
.context-meter-row span,
.run-config-control-head span,
.fast-copy span {
	display: inline-flex;
	align-items: center;
	gap: 0.34rem;
}

.run-config-title-row strong {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: #111827;
	font-size: 13px;
	font-weight: 800;
}

.run-config-title,
.context-meter-row {
	color: #334155;
	font-size: 11.5px;
	font-weight: 800;
}

.run-config-title svg,
.context-meter-row svg,
.run-config-control-head svg,
.fast-copy svg {
	width: 0.86rem;
	height: 0.86rem;
	color: #475569;
}

.run-config-subtitle,
.run-config-context-detail {
	min-width: 0;
	flex-wrap: wrap;
	color: #94a3b8;
	font-size: 10.5px;
	line-height: 1.35;
}

.run-config-subtitle {
	display: flex;
	gap: 0.5rem;
}

.run-config-subtitle span:first-child {
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.context-meter-row strong {
	color: #2563eb;
	font-size: 12px;
	font-weight: 850;
}

.context-meter {
	height: 6px;
	overflow: hidden;
	border-radius: 999px;
	background: #e2e8f0;
}

.context-meter span {
	display: block;
	height: 100%;
	border-radius: inherit;
	background: linear-gradient(90deg, #60a5fa, #2563eb);
	transition: width .18s ease;
}

.model-search {
	display: flex;
	align-items: center;
	gap: .38rem;
	flex: 0 0 auto;
	margin: .02rem .12rem .26rem;
	border: 1px solid #e2e8f0;
	border-radius: 8px;
	background: #f8fafc;
	padding: .26rem .44rem;
	color: #94a3b8;
}

.run-config-search {
	margin: 0;
}

.model-search svg {
	width: .76rem;
	height: .76rem;
}

.model-search input {
	min-width: 0;
	flex: 1;
	border: 0;
	outline: 0;
	background: transparent;
	color: #334155;
	font-size: 10.5px;
}

.model-list {
	min-height: 0;
	flex: 1 1 auto;
	overflow-y: auto;
	padding-right: .08rem;
}

.run-config-model-list {
	max-height: min(34vh, 15rem);
	border: 1px solid #f1f5f9;
	border-radius: 12px;
	padding: 0.25rem;
}

.agent-follow-group {
	margin-bottom: 0.18rem;
	padding-bottom: 0.18rem;
	border-bottom: 1px solid #f1f5f9;
}


.model-group + .model-group {
	margin-top: .3rem;
	padding-top: .22rem;
	border-top: 1px solid #f1f5f9;
}

.model-group-title {
	padding: .18rem .42rem;
	color: #94a3b8;
	font-size: 9.5px;
	font-weight: 800;
	letter-spacing: .12em;
	text-transform: uppercase;
}

.model-empty {
	padding: .9rem .4rem;
	text-align: center;
	color: #94a3b8;
	font-size: 12px;
}

.menu-row {
	display: flex;
	width: 100%;
	align-items: center;
	gap: 0.48rem;
	border: 0;
	border-radius: 8px;
	background: transparent;
	padding: 0.36rem 0.42rem;
	text-align: left;
	color: #27272a;
	cursor: pointer;
}

.compact-row {
	min-height: 1.82rem;
}

.menu-row:hover {
	background: #f8fafc;
}

.menu-row-active {
	background: #f1f5f9;
	box-shadow: inset 0 0 0 1px #e2e8f0;
}

.menu-icon {
	display: grid;
	width: 1.18rem;
	height: 1.18rem;
	place-items: center;
	border-radius: 6px;
	background: #f1f5f9;
	color: #64748b;
}

.menu-icon svg {
	width: 0.72rem;
	height: 0.72rem;
}

.menu-row-active .menu-icon {
	background: #e2e8f0;
	color: #334155;
}

.menu-check {
	width: 0.86rem;
	height: 0.86rem;
	color: #334155;
}

.model-row {
	padding: 0.34rem 0.42rem;
}

.model-row .font-medium {
	font-size: 12px;
}

.model-meta {
	display: flex;
	flex-wrap: wrap;
	gap: 0.18rem;
	margin-top: 0.01rem;
	color: #94a3b8;
	font-size: 9.2px;
}

.model-meta span {
	border-radius: 4px;
	background: #f8fafc;
	padding: 0.01rem 0.24rem;
}

.run-config-controls {
	display: grid;
	gap: 0.6rem;
	border: 1px solid #f1f5f9;
	border-radius: 12px;
	background: #fcfcfd;
	padding: 0.62rem;
}

.run-config-control-head small,
.fast-copy small {
	color: #94a3b8;
	font-size: 10px;
	line-height: 1.35;
}

.thinking-segments {
	display: flex;
	flex-wrap: wrap;
	gap: 0.35rem;
}

.thinking-segments button {
	display: grid;
	gap: 0.02rem;
	min-width: 4rem;
	border: 1px solid #e2e8f0;
	border-radius: 10px;
	background: #fff;
	padding: 0.38rem 0.46rem;
	color: #475569;
	font-size: 11px;
	font-weight: 750;
	text-align: left;
	cursor: pointer;
}

.thinking-segments button small {
	color: #94a3b8;
	font-size: 9.5px;
	font-weight: 600;
}

.thinking-segments button:hover:not(:disabled) {
	border-color: #bfdbfe;
	background: #eff6ff;
	color: #1d4ed8;
}

.thinking-segments button.is-active {
	border-color: rgba(37, 99, 235, 0.35);
	background: #eff6ff;
	color: #1d4ed8;
	box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.08);
}

.control-unavailable {
	border-radius: 10px;
	background: #f8fafc;
	padding: 0.55rem;
	color: #94a3b8;
	font-size: 11px;
}

.fast-control {
	border-top: 1px solid #f1f5f9;
	padding-top: 0.56rem;
}

.fast-control.is-disabled {
	opacity: 0.62;
}

.fast-copy {
	display: grid;
	gap: 0.08rem;
}

.fast-copy span {
	color: #334155;
	font-size: 11.5px;
	font-weight: 800;
}

.fast-switch {
	position: relative;
	width: 2.42rem;
	height: 1.36rem;
	flex: 0 0 auto;
	border: 0;
	border-radius: 999px;
	background: #cbd5e1;
	padding: 0.15rem;
	cursor: pointer;
	transition: background .16s ease, opacity .16s ease;
}

.fast-switch span {
	display: block;
	width: 1.06rem;
	height: 1.06rem;
	border-radius: 999px;
	background: white;
	box-shadow: 0 2px 6px rgba(15, 23, 42, 0.18);
	transition: transform .16s ease;
}

.fast-switch.is-on {
	background: #2563eb;
}

.fast-switch.is-on span {
	transform: translateX(1.06rem);
}

.fast-switch:disabled {
	cursor: not-allowed;
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
	.composer-toolbar {
		grid-template-columns: 1fr;
	}

	.composer-status {
		justify-content: flex-end;
		flex-wrap: wrap;
	}

	.tool-btn {
		width: 1.85rem;
	}

	.run-config-chip {
		max-width: min(100%, 82vw);
	}

	.run-config-chip-meta {
		display: none;
	}

	.run-config-title-row,
	.context-meter-row,
	.run-config-control-head,
	.fast-control {
		align-items: flex-start;
		flex-direction: column;
		gap: 0.35rem;
	}

	.thinking-segments button {
		min-width: 0;
		flex: 1 1 5rem;
	}
}
</style>
