<script setup>
import {computed, ref, watch} from "vue";
import {ArrowRight, CircleCheck, EditPen, Finished, Tickets} from "@element-plus/icons-vue";
import {Api, apiError} from "../../api.js";
import {useToolDetailCache} from "./toolDetailCache.js";
import {buildUserInteractionView, userInteractionEventInput} from "./userInteractionPresentation.js";

const props = defineProps({
	event: {type: Object, required: true},
	conversationUuid: {type: String, default: ""},
	open: {type: Boolean, default: false},
	compact: {type: Boolean, default: false},
});
const emit = defineEmits(["toggle"]);
const toolDetailCache = useToolDetailCache();
const loadedDetail = ref(null);
const detailLoading = ref(false);
const detailError = ref("");

function object(value) {
	return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

const operation = computed(() => object(props.event?.operation));
const operationId = computed(() => String(operation.value.opId || "").trim());
const operationRevision = computed(() => Number(operation.value.detailRevision || operation.value.revision || 0) || 0);
const detailAvailable = computed(() => Boolean(operationId.value && operation.value.detailAvailable));
const loadedDetailMatches = computed(() => {
	const detail = object(loadedDetail.value);
	return Boolean(operationId.value
		&& String(detail.opId || "") === operationId.value
		&& Number(detail.detailRevision || detail.revision || 0) >= operationRevision.value);
});
const displayOperation = computed(() => {
	if (!loadedDetailMatches.value) return operation.value;
	const detail = object(loadedDetail.value);
	return {
		...operation.value,
		...detail,
		payload: {...object(operation.value.payload), ...object(detail.payload)},
	};
});
const view = computed(() => buildUserInteractionView(userInteractionEventInput(props.event, displayOperation.value)));
const actionIcon = computed(() => ({
	confirm: CircleCheck,
	select: Finished,
	prompt: EditPen,
	questionnaire: Tickets,
}[view.value.action] || CircleCheck));

async function loadDetail() {
	const id = operationId.value;
	const revision = operationRevision.value;
	const conversationUuid = String(props.conversationUuid || "").trim();
	if (!props.open || !detailAvailable.value || loadedDetailMatches.value || !conversationUuid
		|| conversationUuid.startsWith("local:") || detailLoading.value) return;
	detailLoading.value = true;
	detailError.value = "";
	let retry = false;
	try {
		const loader = async () => {
			const data = await Api.conversationOperationDetail(conversationUuid, id);
			if (data?.ok === false) throw new Error(data.error || "交互详情读取失败");
			return object(data?.operation);
		};
		const detail = toolDetailCache
			? await toolDetailCache.load({conversationUuid, operationId: id, revision, loader})
			: await loader();
		if (operationId.value !== id || String(detail.opId || "") !== id) return;
		if (Number(detail.detailRevision || detail.revision || 0) < operationRevision.value) {
			retry = true;
			return;
		}
		loadedDetail.value = detail;
	} catch (error) {
		detailError.value = apiError(error);
	} finally {
		detailLoading.value = false;
		if (retry && props.open) void loadDetail();
	}
}

watch(() => [operationId.value, operationRevision.value], () => {
	if (!loadedDetailMatches.value) loadedDetail.value = null;
	detailError.value = "";
});
watch(() => [props.open, operationId.value, operationRevision.value, detailAvailable.value], () => {
	void loadDetail();
}, {immediate: true});
</script>

<template>
	<details class="interaction-event" :class="[`action-${view.action}`, `tone-${view.statusTone}`, {compact: props.compact}]" :open="props.open" @toggle="emit('toggle', $event)">
		<summary>
			<span class="interaction-icon" aria-hidden="true"><component :is="actionIcon"/></span>
			<span class="interaction-heading">
				<span class="interaction-name">{{ view.actionName }}</span>
				<strong>{{ view.title }}</strong>
			</span>
			<span class="interaction-intro">{{ view.intro }}</span>
			<span class="status-chip">{{ view.statusLabel }}</span>
			<span class="disclosure-icon" aria-hidden="true"><ArrowRight/></span>
		</summary>

		<div v-if="props.open" class="interaction-detail">
			<p v-if="detailLoading" class="detail-notice">正在读取交互详情…</p>
			<p v-else-if="detailError" class="detail-notice is-error">交互详情读取失败：{{ detailError }}</p>
			<div class="readonly-card" :class="{'questionnaire-card': view.action === 'questionnaire'}">
				<header class="readonly-title">
					<span class="readonly-title-icon" aria-hidden="true"><component :is="actionIcon"/></span>
					<div><strong>{{ view.title }}</strong><span>{{ view.actionName }} · {{ view.statusLabel }}</span></div>
				</header>
				<p v-if="view.body" class="readonly-body">{{ view.body }}</p>

				<div v-if="view.sensitive" class="redacted-answer">{{ view.redactedText }}</div>

				<template v-else-if="view.action === 'confirm'">
					<div class="confirm-outcome" :class="{'is-confirmed': view.confirmed}">
						<span class="faux-indicator" aria-hidden="true">{{ view.confirmed ? '✓' : '×' }}</span>
						<span>{{ view.statusLabel }}</span>
					</div>
				</template>

				<div v-else-if="view.action === 'select'" class="readonly-options">
					<div v-for="option in view.options" :key="option.value" class="readonly-option" :class="{'is-selected': option.selected}">
						<span class="faux-choice" :class="{'is-selected': option.selected}" aria-hidden="true"><span></span></span>
						<span class="option-copy"><strong>{{ option.label }}</strong><small v-if="option.description">{{ option.description }}</small></span>
					</div>
					<p v-if="!view.options.length" class="empty-answer">没有可展示的选项</p>
				</div>

				<div v-else-if="view.action === 'prompt'" class="readonly-text-answer">
					<span>你的回答</span>
					<div>{{ view.promptValue || '（空回答）' }}</div>
				</div>

				<div v-else-if="view.action === 'questionnaire'" class="questionnaire-questions">
					<section v-for="question in view.questions" :key="question.id" class="questionnaire-question">
						<header>
							<span class="question-number">{{ question.number }}</span>
							<strong>{{ question.question }}</strong>
							<span :class="question.required ? 'required-mark' : 'optional-mark'">{{ question.required ? '必填' : '选填' }}</span>
						</header>
						<p v-if="question.description" class="question-description">{{ question.description }}</p>
						<div v-if="question.type === 'choice'" class="question-choice-list">
							<div v-for="option in question.options" :key="option.key || option.value" class="question-choice-option" :class="{'is-selected': option.selected}">
								<span class="faux-choice" :class="{'is-selected': option.selected, 'is-multiple': question.multiple}" aria-hidden="true"><span></span></span>
								<span class="option-copy">
									<span class="option-label"><strong>{{ option.label }}</strong><small v-if="option.recommended" class="recommendation-badge">当时建议</small></span>
									<small v-if="option.description">{{ option.description }}</small>
								</span>
							</div>
						</div>
						<div v-if="question.recommendationReason" class="recommendation-reason"><strong>当时建议理由</strong>{{ question.recommendationReason }}</div>
						<div v-if="question.answerText || question.type === 'open'" class="readonly-text-answer compact">
							<span>{{ question.type === 'choice' ? '补充回答' : '你的回答' }}</span>
							<div>{{ question.answerText || '（未填写）' }}</div>
						</div>
						<p v-if="!question.answered" class="question-unanswered">未回答</p>
					</section>
					<p v-if="!view.questions.length" class="empty-answer">没有可展示的问题</p>
				</div>
			</div>
		</div>
	</details>
</template>

<style scoped>
.interaction-event { container-type: inline-size; margin: .16rem 0; border: 0; color: #334155; }
.interaction-event > summary { display: flex; align-items: center; gap: .34rem; max-width: 100%; min-width: 0; min-height: 1.45rem; padding: .04rem 0; list-style: none; cursor: pointer; }
.interaction-event > summary::-webkit-details-marker { display: none; }
.interaction-icon { display: grid; width: .92rem; height: .92rem; flex: 0 0 auto; place-items: center; color: #526f91; }
.interaction-icon svg, .readonly-title-icon svg { width: 100%; height: 100%; }
.interaction-heading { display: flex; min-width: 0; flex: 0 1 auto; align-items: baseline; gap: .38rem; }
.interaction-name { flex: 0 0 auto; font-size: .72rem; font-weight: 750; color: #526f91; }
.interaction-heading strong { min-width: 0; overflow: hidden; font-size: .78rem; color: #334155; text-overflow: ellipsis; white-space: nowrap; }
.interaction-intro { min-width: 0; flex: 1 1 auto; overflow: hidden; font-size: .72rem; color: #7c8592; text-overflow: ellipsis; white-space: nowrap; }
.status-chip { display: inline-flex; height: 1.15rem; max-height: 1.15rem; flex: 0 0 auto; align-items: center; justify-content: center; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 999px; padding: 0 .38rem; background: #f8fafc; font-size: .64rem; font-weight: 700; line-height: 1; color: #64748b; white-space: nowrap; }
.interaction-event.compact .interaction-intro { display: none; }
.interaction-event.compact .interaction-heading { flex: 1 1 auto; }
.tone-success .status-chip { border-color: #bbdbc8; background: #f0f8f3; color: #35704d; }
.tone-warning .status-chip, .tone-waiting .status-chip { border-color: #ead2a6; background: #fff9ed; color: #8b6524; }
.tone-danger .status-chip { border-color: #efc3c3; background: #fff5f5; color: #a33f3f; }
.disclosure-icon { display: grid; width: 1rem; height: 1rem; flex: 0 0 auto; place-items: center; color: #94a3b8; }
.disclosure-icon svg { width: .72rem; transition: transform .14s ease; }
details[open] > summary .disclosure-icon svg { transform: rotate(90deg); }
.interaction-detail { margin: .25rem 0 .7rem 1.7rem; }
.detail-notice { margin: 0 0 .45rem; font-size: .72rem; color: #64748b; }
.detail-notice.is-error { color: #b91c1c; }
.readonly-card { border: 1px solid #dbe4ee; border-radius: .9rem; background: rgba(248,250,252,.96); padding: .85rem; box-shadow: 0 10px 28px rgba(15,23,42,.07); }
.readonly-title { display: flex; align-items: flex-start; gap: .52rem; color: #1e3a5f; }
.readonly-title-icon { display: grid; width: 1.15rem; height: 1.15rem; flex: 0 0 auto; place-items: center; margin-top: .05rem; }
.readonly-title > div { display: grid; gap: .12rem; }
.readonly-title strong { font-size: .88rem; }
.readonly-title span { font-size: .7rem; color: #64748b; }
.readonly-body { margin: .62rem 0 0; font-size: .76rem; line-height: 1.55; color: #64748b; white-space: pre-wrap; }
.redacted-answer, .empty-answer { margin: .7rem 0 0; border: 1px dashed #cbd5e1; border-radius: .7rem; background: #fff; padding: .65rem; font-size: .75rem; color: #64748b; }
.confirm-outcome { display: flex; align-items: center; gap: .5rem; margin-top: .7rem; border: 1px solid #d8e0e9; border-radius: .72rem; background: #fff; padding: .6rem; font-size: .78rem; font-weight: 700; }
.confirm-outcome.is-confirmed { border-color: #bed8c8; background: #f4faf6; color: #35704d; }
.faux-indicator { display: grid; width: 1.25rem; height: 1.25rem; place-items: center; border-radius: 50%; background: #e9eef4; color: #64748b; }
.is-confirmed .faux-indicator { background: #dcefe3; color: #2f6d48; }
.readonly-options, .question-choice-list { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: .45rem; margin-top: .7rem; }
.readonly-option, .question-choice-option { display: flex; min-width: 0; align-items: flex-start; gap: .5rem; border: 1px solid #dbe4ee; border-radius: .72rem; background: #fff; padding: .55rem .62rem; }
.readonly-option.is-selected, .question-choice-option.is-selected { border-color: #7896bd; background: #f2f6fb; }
.faux-choice { display: grid; width: .9rem; height: .9rem; flex: 0 0 auto; place-items: center; margin-top: .12rem; border: 1px solid #aab7c5; border-radius: 50%; background: #fff; }
.faux-choice.is-multiple { border-radius: .18rem; }
.faux-choice.is-selected { border-color: #526f91; }
.faux-choice.is-selected > span { width: .46rem; height: .46rem; border-radius: inherit; background: #526f91; }
.option-copy { display: grid; min-width: 0; gap: .14rem; font-size: .77rem; line-height: 1.4; color: #263548; }
.option-copy small { font-size: .69rem; font-weight: 400; color: #64748b; }
.option-label { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem; }
.readonly-text-answer { display: grid; gap: .3rem; margin-top: .65rem; font-size: .7rem; font-weight: 650; color: #475569; }
.readonly-text-answer > div { min-height: 2.4rem; border: 1px solid #ced9e5; border-radius: .68rem; background: #fff; padding: .58rem .62rem; font-size: .77rem; font-weight: 400; line-height: 1.5; color: #1f2937; white-space: pre-wrap; overflow-wrap: anywhere; }
.questionnaire-questions { display: grid; gap: .7rem; margin-top: .75rem; }
.questionnaire-question { border: 1px solid #dbe4ee; border-radius: .85rem; background: rgba(255,255,255,.9); padding: .72rem; }
.questionnaire-question > header { display: flex; align-items: center; gap: .42rem; font-size: .81rem; line-height: 1.45; color: #1e293b; }
.question-number { display: inline-grid; width: 1.35rem; height: 1.35rem; flex: 0 0 auto; place-items: center; border-radius: 50%; background: #e8eef7; font-size: .7rem; color: #334155; }
.required-mark, .optional-mark, .recommendation-badge { flex: 0 0 auto; border-radius: 999px; padding: .1rem .38rem; font-size: .63rem; font-weight: 750; }
.required-mark { background: #fee2e2; color: #991b1b; }
.optional-mark { background: #f1f5f9; color: #64748b; }
.question-description { margin: .28rem 0 0; font-size: .72rem; line-height: 1.5; color: #64748b; white-space: pre-wrap; }
.question-choice-list { margin-top: .5rem; }
.recommendation-badge { border: 1px solid #b8c7da; background: #edf3fa; color: #345477; }
.recommendation-reason { margin-top: .48rem; border-left: 2px solid #8ba4c3; padding-left: .55rem; font-size: .7rem; line-height: 1.45; color: #526274; white-space: pre-wrap; }
.recommendation-reason strong { margin-right: .35rem; color: #345477; }
.readonly-text-answer.compact { margin-top: .52rem; }
.question-unanswered { margin: .4rem 0 0; font-size: .68rem; color: #94a3b8; }
@container (max-width: 32rem) {
	.interaction-intro { display: none; }
	.interaction-heading { flex: 1 1 auto; }
}
@media (max-width: 640px) {
	.interaction-intro { display: none; }
	.interaction-heading { flex: 1 1 auto; }
	.interaction-detail { margin-left: 0; }
	.readonly-options, .question-choice-list { grid-template-columns: minmax(0,1fr); }
	.questionnaire-question > header { align-items: flex-start; flex-wrap: wrap; }
}
</style>
