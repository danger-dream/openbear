<script setup>
import {computed, nextTick, onBeforeUnmount, ref, watch} from "vue";
import {ElTooltip} from "element-plus";
import {Check, Clock, Loading, Refresh, Warning} from "@element-plus/icons-vue";
import AgentProcessActivity from "./AgentProcessActivity.vue";
import {agentStepActivityLines, buildAgentPlanView, initialAgentLaunch, preferredAgentPlanVersion} from "./agentPlanPresentation.js";

const props = defineProps({
	data: {type: Object, default: null},
	loading: {type: Boolean, default: false},
	error: {type: String, default: ""},
	activityLines: {type: Array, default: () => []},
	activityLoading: {type: Boolean, default: false},
	activityError: {type: String, default: ""},
});
const emit = defineEmits(["refresh"]);

const selectedStepId = ref("");
const selectedPlanVersion = ref(0);
const planVersionPinned = ref(false);
const methodTextEl = ref(null);
const methodExpanded = ref(false);
const methodOverflow = ref(false);
let methodResizeObserver = null;
const view = computed(() => buildAgentPlanView(props.data || {}, {planVersion: selectedPlanVersion.value}));
const launchInfo = computed(() => initialAgentLaunch(props.data || {}));
const selectedStep = computed(() => view.value.steps.find((step) => step.id === selectedStepId.value) || view.value.steps[0] || null);
const selectedStepActivitySource = computed(() => agentStepActivityLines(
	props.activityLines,
	view.value.currentVersion,
	selectedStep.value?.id,
	{startedAt: selectedStep.value?.startedAt, completedAt: selectedStep.value?.completedAt},
));
const progressPct = computed(() => {
	const total = view.value.counts.requiredSteps;
	return total ? Math.round(view.value.counts.completedSteps * 100 / total) : 0;
});
const latestDecision = computed(() => [...view.value.versionDecisions].reverse().find((item) => item.action) || null);
const hasPlan = computed(() => view.value.hasPlan);

watch(
	() => props.data,
	(data) => {
		const preferred = preferredAgentPlanVersion(data || {});
		const versions = new Set((data?.versions || []).map((item) => Number(item?.version || 0)).filter(Boolean));
		if (!planVersionPinned.value || !versions.has(selectedPlanVersion.value)) {
			selectedPlanVersion.value = preferred;
			planVersionPinned.value = false;
		}
	},
	{immediate: true},
);

watch(
	() => [selectedStep.value?.id, selectedStep.value?.method],
	() => {
		methodExpanded.value = false;
		methodOverflow.value = false;
		void nextTick(measureMethodOverflow);
	},
	{immediate: true},
);

watch(methodTextEl, (element, previous) => {
	if (previous && methodResizeObserver) methodResizeObserver.unobserve(previous);
	if (!methodResizeObserver && typeof ResizeObserver !== "undefined") {
		methodResizeObserver = new ResizeObserver(() => {
			if (!methodExpanded.value) measureMethodOverflow();
		});
	}
	if (element && methodResizeObserver) methodResizeObserver.observe(element);
	void nextTick(measureMethodOverflow);
});

watch(
	() => view.value.defaultStepId,
	(value) => {
		if (!selectedStepId.value || !view.value.steps.some((step) => step.id === selectedStepId.value)) selectedStepId.value = value;
	},
	{immediate: true},
);

function measureMethodOverflow() {
	const element = methodTextEl.value;
	if (!element || methodExpanded.value) return;
	methodOverflow.value = element.scrollHeight > element.clientHeight + 1;
}

function toggleMethod() {
	methodExpanded.value = !methodExpanded.value;
}

onBeforeUnmount(() => methodResizeObserver?.disconnect());

function formatTime(value) {
	const number = Number(value || 0);
	if (!number) return "—";
	return new Date(number * (number < 10_000_000_000 ? 1000 : 1)).toLocaleString();
}

function selectPlanVersion(version) {
	const nextVersion = Number(version || 0);
	if (!nextVersion || nextVersion === selectedPlanVersion.value) return;
	selectedStepId.value = "";
	selectedPlanVersion.value = nextVersion;
	planVersionPinned.value = nextVersion !== preferredAgentPlanVersion(props.data || {});
}

function selectRelevantStep(check) {
	if (check.done) return;
	const target = view.value.steps.find((step) => {
		if (check.key === "steps") return step.required && step.status !== "completed";
		if (check.key === "criteria") return step.criteria.some((criterion) => criterion.required && !criterion.satisfied);
		if (check.key === "evidence") return step.criteria.some((criterion) => criterion.required && !criterion.evidence.length);
		return false;
	});
	if (target) selectedStepId.value = target.id;
}
</script>

<template>
	<div class="plan-workspace">
		<div v-if="loading && !data" class="plan-state-empty">
			<Loading class="spin"/>
			<div><strong>正在读取执行计划</strong><span>从持久化任务记录中恢复步骤和验收依据…</span></div>
		</div>
		<div v-else-if="error && !data" class="plan-state-empty is-error">
			<Warning/>
			<div><strong>执行计划读取失败</strong><span>{{ error }}</span></div>
			<button type="button" @click="emit('refresh')"><Refresh/>重新读取</button>
		</div>
		<div v-else-if="!hasPlan" class="plan-state-empty">
			<Clock/>
			<div><strong>{{ view.phaseMeta.label }}</strong><span>{{ view.phaseMeta.description }}</span></div>
			<button type="button" :disabled="loading" @click="emit('refresh')"><Refresh/>刷新状态</button>
		</div>

		<div v-else class="plan-overview">
			<aside class="plan-sidebar" tabindex="0" aria-label="计划步骤与版本，可滚动">
				<div class="plan-caption">
					<div class="plan-caption-head">
						<strong>{{ view.phaseMeta.label }} · {{ view.counts.completedSteps }}/{{ view.counts.requiredSteps }}</strong>
						<button type="button" :disabled="loading" title="重新读取最新状态" @click="emit('refresh')"><Refresh :class="{spin: loading}"/></button>
					</div>
					<p>{{ view.phaseMeta.description }}</p>
				</div>

				<div v-if="view.isHistoricalVersion" class="plan-attention is-history">
					<Clock/><span>正在查看历史计划 v{{ view.currentVersion }}；当前执行版本是 v{{ view.activeVersion }}。</span>
				</div>
				<div v-else-if="view.phase === 'needs_user_decision'" class="plan-attention is-warning">
					<Warning/><span>{{ view.state.last_controller_guidance || '自动计划调整已到上限，需要你的决定。' }}</span>
				</div>
				<div v-else-if="view.phase === 'blocked_control'" class="plan-attention is-danger">
					<Warning/><span>{{ selectedStep?.blocker?.reason || view.state.last_controller_guidance || '主控正在处理当前阻塞。' }}</span>
				</div>
				<div v-else-if="view.taskTerminal" class="plan-attention" :class="{'is-danger': view.taskStatus === 'failed'}">
					<Warning/><span>{{ view.phaseMeta.description }}</span>
				</div>

				<div class="progress-line" :title="`必做步骤完成 ${progressPct}%`"><span :style="{width: `${progressPct}%`}"></span></div>

				<div class="step-list">
					<button
						v-for="step in view.steps"
						:key="step.id"
						type="button"
						class="step-button"
						:class="[{active: selectedStepId === step.id, 'is-current': step.current, 'is-running': step.status === 'running'}, `tone-${step.statusMeta.tone}`]"
						:aria-current="step.current ? 'step' : undefined"
						:title="step.current ? `当前执行：${step.title}` : `${step.title} · ${step.statusMeta.label}`"
						@click="selectedStepId = step.id"
					>
						<span class="step-number">
							<Check v-if="step.status === 'completed'"/>
							<Loading v-else-if="step.status === 'running'" class="step-running-icon"/>
							<Warning v-else-if="['blocked', 'failed', 'cancelled', 'interrupted'].includes(step.status)"/>
							<Clock v-else-if="step.status === 'pending'"/>
							<template v-else>{{ step.index }}</template>
						</span>
						<span class="step-text">
							<strong>{{ step.title }}</strong>
							<span class="step-meta">
								<span>{{ step.criteria.filter((item) => item.satisfied).length }}/{{ step.criteria.length }} 条件</span>
								<span class="step-status" :class="`tone-${step.statusMeta.tone}`"><i v-if="step.status === 'running'"></i>{{ step.statusMeta.label }}</span>
							</span>
						</span>
						<span class="step-arrow">›</span>
					</button>
				</div>

				<div v-if="view.versions.length > 1" class="version-switcher">
					<label>计划版本</label>
					<select aria-label="选择计划版本" :value="view.currentVersion" @change="selectPlanVersion($event.target.value)">
						<option v-for="version in [...view.versions].reverse()" :key="version.version" :value="version.version">
							v{{ version.version }} · {{ version.typeLabel }} · {{ version.statusLabel }}{{ version.active ? '（当前执行）' : version.pending ? '（等待确认）' : '' }}
						</option>
					</select>
					<small v-if="view.current.diff?.changedFields?.length">本版变更：{{ view.current.diff.changedFields.join('、') }}</small>
					<small v-else>共 {{ view.versions.length }} 个版本，可查看历次计划与执行记录。</small>
				</div>
			</aside>

			<main class="plan-inspector" tabindex="0" aria-label="计划步骤详情，可滚动">
				<div v-if="selectedStep" class="inspector-content">
					<div class="inspector-kicker">步骤 {{ selectedStep.index }} · {{ selectedStep.id }}</div>
					<div class="inspector-title-row">
						<h2>{{ selectedStep.title }}</h2>
						<span class="done-label" :class="`tone-${selectedStep.statusMeta.tone}`"><Check v-if="selectedStep.status === 'completed'"/>{{ selectedStep.statusMeta.label }}</span>
					</div>
					<p class="inspector-summary">{{ selectedStep.objective || view.plan.objective }}</p>

					<div class="step-detail-stack">
						<div class="info-cell method-cell">
							<span>执行方法</span>
							<p ref="methodTextEl" class="method-text" :class="{'is-expanded': methodExpanded}">{{ selectedStep.method || '等待 Agent 记录执行方法。' }}</p>
							<button v-if="methodOverflow" type="button" class="method-toggle" :aria-expanded="methodExpanded" @click="toggleMethod">
								{{ methodExpanded ? '收起' : '展开全部' }}<i :class="{'is-expanded': methodExpanded}">⌄</i>
							</button>
						</div>

						<section class="step-activity-card">
							<header>
								<div><strong>步骤过程</strong><span>仅显示最近 5 条，完整内容请查看“过程记录”</span></div>
								<em><Loading v-if="activityLoading" class="spin"/>最近 5 条</em>
							</header>
							<div v-if="activityError" class="step-activity-error">{{ activityError }}</div>
							<div v-if="activityLoading && !selectedStepActivitySource.length" class="step-activity-loading"><Loading class="spin"/>正在归集步骤事件…</div>
							<AgentProcessActivity
								v-else
								:source-lines="selectedStepActivitySource"
								empty-text="这个步骤暂时没有可显示的过程记录。"
								:model-label="launchInfo.model === '—' ? '' : launchInfo.model"
								:think-level="launchInfo.thinkLevel === '—' ? '' : launchInfo.thinkLevel"
								:fast-mode="launchInfo.fastMode"
								:limit="5"
							/>
						</section>

						<div v-if="selectedStep.result" class="info-cell result-cell"><span>执行结果</span><p>{{ selectedStep.result }}</p></div>
					</div>

					<div v-if="selectedStep.blocker?.reason" class="blocker-note"><Warning/><div><strong>阻塞原因</strong><p>{{ selectedStep.blocker.reason }}</p></div></div>

					<div class="section-label"><span>完成条件</span><span>{{ selectedStep.criteria.filter((item) => item.satisfied).length }}/{{ selectedStep.criteria.length }} 已满足</span></div>
					<div v-if="!selectedStep.criteria.length" class="inline-empty">这个步骤没有声明完成条件。</div>
					<div v-for="criterion in selectedStep.criteria" :key="criterion.id" class="criterion">
						<div class="criterion-head">
							<span class="criterion-check" :class="`tone-${criterion.statusMeta.tone}`"><Check v-if="criterion.satisfied"/><Clock v-else/></span>
							<div class="criterion-copy"><strong>{{ criterion.description }}</strong><p>{{ criterion.note ? `判断说明：${criterion.note}` : `${criterion.required ? '必需条件' : '可选条件'} · ${criterion.statusMeta.label}` }}</p></div>
						</div>
						<div v-if="criterion.evidence.length" class="evidence-list">
							<article v-for="evidence in criterion.evidence" :key="evidence.uuid" class="evidence">
								<div class="evidence-title-row">
									<span class="evidence-kind">{{ evidence.typeLabel }}</span>
									<ElTooltip placement="top" effect="light" :show-after="250">
										<template #content><div class="evidence-tooltip"><strong>{{ evidence.typeLabel }}</strong><p>{{ evidence.summary }}</p><code>记录 ID：{{ evidence.uuid }}</code></div></template>
										<span class="evidence-info">依据详情</span>
									</ElTooltip>
								</div>
								<p class="evidence-summary">{{ evidence.summary }}</p>
								<details class="plan-touch-details"><summary>依据详情</summary><code>记录 ID：{{ evidence.uuid }}</code></details>
							</article>
						</div>
						<div v-else class="evidence-missing"><Warning/>尚未绑定验收依据</div>
					</div>

					<div v-if="selectedStep.startedAt || selectedStep.completedAt" class="step-times"><span>开始 {{ formatTime(selectedStep.startedAt) }}</span><span>完成 {{ formatTime(selectedStep.completedAt) }}</span></div>

					<div class="section-label"><span>完成检查</span><span>{{ view.completionChecks.filter((item) => item.done).length }}/{{ view.completionChecks.length }} 通过</span></div>
					<div class="completion-list">
						<button v-for="check in view.completionChecks" :key="check.key" type="button" class="completion-item" :class="{done: check.done}" @click="selectRelevantStep(check)">
							<span><Check v-if="check.done"/><Clock v-else/></span><span><strong>{{ check.label }}</strong><small>{{ check.detail }}</small></span>
						</button>
					</div>

					<div v-if="latestDecision" class="decision-note"><Check/><div><strong>最近一次计划确认：{{ latestDecision.action === 'approve' ? '已批准' : latestDecision.action }}</strong><p>{{ latestDecision.reason || '计划决定已记录' }}</p></div></div>

					<template v-if="view.finalOutputs.length">
						<div class="section-label"><span>最终交付</span><span>{{ view.counts.completedOutputs }}/{{ view.counts.finalOutputs }} 已形成</span></div>
						<div class="deliverable-list">
							<article v-for="output in view.finalOutputs" :key="output.id" class="deliverable" :class="{done: output.completed}">
								<span class="deliverable-check"><Check v-if="output.completed"/><Clock v-else/></span>
								<div>
									<strong>{{ output.title }}</strong><p>{{ output.summary || output.description }}</p>
									<div v-if="output.sources.length" class="source-list">
										<ElTooltip v-for="source in output.sources" :key="source.raw" placement="top" effect="light" :show-after="250">
											<template #content><div class="evidence-tooltip"><strong>{{ source.typeLabel }}</strong><p>{{ source.summary }}</p><code v-if="source.uuid">记录 ID：{{ source.uuid }}</code></div></template>
											<span class="source-chip">{{ source.label }}</span>
										</ElTooltip>
									</div>
									<details v-if="output.sources.length" class="plan-touch-details">
										<summary>来源详情</summary>
										<div v-for="source in output.sources" :key="source.raw"><strong>{{ source.typeLabel }}</strong><p>{{ source.summary }}</p><code v-if="source.uuid">记录 ID：{{ source.uuid }}</code></div>
									</details>
								</div>
							</article>
						</div>
					</template>
				</div>
				<div v-else class="inline-empty">当前计划没有可展示的步骤。</div>
			</main>
		</div>
	</div>
</template>

<style scoped>
.plan-workspace {
	--ink: var(--ob-text);
	--secondary: var(--ob-text-subtle);
	--tertiary: var(--ob-text-muted);
	--line: var(--ob-border);
	--line-strong: var(--ob-border-strong);
	--blue: var(--ob-blue);
	--blue-soft: var(--ob-blue-soft);
	--green: var(--ob-success);
	--green-soft: var(--ob-success-soft);
	--orange: var(--ob-orange);
	--red: var(--ob-danger);
	box-sizing: border-box;
	width: 100%;
	max-width: 100%;
	height: 100%;
	min-width: 0;
	min-height: 0;
	overflow: hidden;
	color: var(--ink);
}
.plan-workspace, .plan-workspace * { box-sizing: border-box; }

.plan-state-empty {
	display: flex;
	height: 100%;
	min-height: 0;
	align-items: center;
	justify-content: center;
	gap: 12px;
	border: 1px dashed var(--line-strong);
	border-radius: 13px;
	background: rgb(var(--ob-surface-rgb) / 0.72);
	padding: 24px;
	color: var(--secondary);
}
.plan-state-empty > svg { width: 22px; height: 22px; }
.plan-state-empty > div { display: grid; gap: 3px; }
.plan-state-empty strong { color: var(--ink); font-size: 13px; }
.plan-state-empty span { font-size: 12px; }
.plan-state-empty button { display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--ob-surface); padding: 6px 9px; color: var(--secondary); font-size: 11px; cursor: pointer; }
.plan-state-empty button svg { width: 12px; }
.plan-state-empty.is-error { border-color: rgb(var(--ob-danger-rgb) / 0.25); background: var(--ob-danger-soft); color: var(--red); }

.plan-overview { display: grid; grid-template-columns: 236px minmax(0, 1fr); width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0; overflow: hidden; }
.plan-sidebar { min-width: 0; min-height: 0; overflow-x: hidden; overflow-y: auto; border-right: 1px solid var(--line); background: rgb(var(--ob-surface-soft-rgb) / 0.72); padding: 14px 10px 16px; scrollbar-color: var(--ob-scrollbar) transparent; scrollbar-width: thin; }
.plan-caption { padding: 0 7px 10px; }
.plan-caption-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.plan-caption strong { display: block; font-size: 12px; font-weight: 650; }
.plan-caption p { margin: 4px 0 0; color: var(--tertiary); font-size: 10.5px; line-height: 1.45; }
.plan-caption button { display: grid; width: 23px; height: 23px; flex: 0 0 auto; place-items: center; border: 0; border-radius: 6px; background: transparent; color: var(--tertiary); cursor: pointer; }
.plan-caption button:hover { background: var(--ob-hover); color: var(--secondary); }
.plan-caption button:disabled { cursor: wait; opacity: .55; }
.plan-caption button svg { width: 12px; }

.plan-attention { display: flex; gap: 7px; align-items: flex-start; margin: 0 7px 10px; border: 1px solid rgb(var(--ob-orange-rgb) / 0.18); border-radius: 8px; background: var(--ob-orange-soft); padding: 7px 8px; color: var(--orange); font-size: 10px; line-height: 1.45; }
.plan-attention.is-danger { border-color: rgb(var(--ob-danger-rgb) / 0.18); background: var(--ob-danger-soft); color: var(--red); }
.plan-attention.is-history { border-color: rgb(var(--ob-violet-rgb) / 0.16); background: rgb(var(--ob-violet-rgb) / 0.06); color: var(--ob-violet); }
.plan-attention svg { width: 12px; flex: 0 0 auto; }
.progress-line { height: 3px; margin: 3px 7px 12px; overflow: hidden; border-radius: 999px; background: rgb(var(--ob-text-muted-rgb) / 0.15); }
.progress-line > span { display: block; height: 100%; border-radius: inherit; background: var(--blue); transition: width .2s ease; }

.step-list { display: grid; gap: 4px; }
.step-button { position: relative; display: grid; grid-template-columns: 28px minmax(0, 1fr) auto; gap: 8px; align-items: center; width: 100%; overflow: hidden; border: 0; border-radius: 10px; background: transparent; padding: 8px; color: inherit; text-align: left; cursor: pointer; transition: background .18s ease, box-shadow .18s ease, transform .18s ease; }
.step-button:hover { background: var(--ob-hover); }
.step-button.active:not(.is-current) { background: rgb(var(--ob-surface-rgb) / 0.82); box-shadow: inset 0 0 0 1px rgb(var(--ob-blue-rgb) / 0.2); }
.step-button.is-current { background: linear-gradient(90deg, rgb(var(--ob-blue-rgb) / 0.15), rgb(var(--ob-blue-rgb) / 0.055)); box-shadow: inset 3px 0 0 var(--blue), inset 0 0 0 1px rgb(var(--ob-blue-rgb) / 0.15), 0 3px 10px rgb(var(--ob-blue-rgb) / 0.07); animation: current-step-breathe 2.4s ease-in-out infinite; }
.step-button.is-current:hover { background: linear-gradient(90deg, rgb(var(--ob-blue-rgb) / 0.18), rgb(var(--ob-blue-rgb) / 0.075)); }
.step-number { display: grid; width: 25px; height: 25px; place-items: center; border: 1px solid var(--line-strong); border-radius: 50%; background: var(--ob-surface); color: var(--ob-text-subtle); font-size: 10px; transition: border-color .18s ease, background .18s ease, color .18s ease, box-shadow .18s ease; }
.step-number svg { width: 12px; }
.step-button.tone-success .step-number { border-color: rgb(var(--ob-success-rgb) / 0.2); background: var(--green-soft); color: var(--green); }
.step-button.tone-danger .step-number { border-color: rgb(var(--ob-danger-rgb) / 0.18); background: var(--ob-danger-soft); color: var(--red); }
.step-button.tone-warning .step-number { border-color: rgb(var(--ob-orange-rgb) / 0.2); background: var(--ob-orange-soft); color: var(--orange); }
.step-button.is-current .step-number { border-color: rgb(var(--ob-blue-rgb) / 0.32); background: var(--ob-surface); color: var(--blue); box-shadow: 0 0 0 3px rgb(var(--ob-blue-rgb) / 0.09); }
.step-running-icon { animation: step-icon-spin 1s linear infinite; }
.step-text { min-width: 0; }
.step-text strong { display: block; overflow: hidden; color: var(--ink); font-size: 11.5px; font-weight: 620; text-overflow: ellipsis; white-space: nowrap; }
.step-button.is-current .step-text strong { color: var(--ob-blue); font-weight: 700; }
.step-meta { display: flex; min-width: 0; align-items: center; gap: 5px; margin-top: 3px; color: var(--tertiary); font-size: 9.5px; }
.step-meta > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-status { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 4px; border: 1px solid var(--ob-border); border-radius: 999px; background: rgb(var(--ob-surface-soft-rgb) / 0.07); padding: 1px 5px; color: var(--ob-text-subtle); font-size: 9px; font-weight: 650; line-height: 1.35; }
.step-status.tone-success { border-color: rgb(var(--ob-success-rgb) / 0.15); background: rgb(var(--ob-success-rgb) / 0.08); color: var(--green); }
.step-status.tone-active { border-color: rgb(var(--ob-blue-rgb) / 0.2); background: rgb(var(--ob-blue-rgb) / 0.11); color: var(--ob-blue); }
.step-status.tone-danger { border-color: rgb(var(--ob-danger-rgb) / 0.16); background: rgb(var(--ob-danger-rgb) / 0.07); color: var(--red); }
.step-status.tone-warning { border-color: rgb(var(--ob-orange-rgb) / 0.16); background: rgb(var(--ob-orange-rgb) / 0.07); color: var(--orange); }
.step-status i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 0 rgb(var(--ob-blue-rgb) / 0.35); animation: step-status-pulse 1.35s ease-out infinite; }
.step-arrow { color: var(--ob-text-muted); font-size: 13px; transition: color .18s ease, transform .18s ease; }
.step-button.active .step-arrow, .step-button.is-current .step-arrow { color: var(--blue); transform: translateX(1px); }

@keyframes step-icon-spin { to { transform: rotate(360deg); } }
@keyframes step-status-pulse { 70%, 100% { box-shadow: 0 0 0 5px transparent; } }
@keyframes current-step-breathe { 0%, 100% { box-shadow: inset 3px 0 0 var(--blue), inset 0 0 0 1px rgb(var(--ob-blue-rgb) / 0.14), 0 3px 10px rgb(var(--ob-blue-rgb) / 0.05); } 50% { box-shadow: inset 3px 0 0 var(--blue), inset 0 0 0 1px rgb(var(--ob-blue-rgb) / 0.24), 0 4px 14px rgb(var(--ob-blue-rgb) / 0.12); } }

@media (prefers-reduced-motion: reduce) {
	.step-button, .step-number, .step-arrow { transition: none; }
	.step-button.is-current, .step-running-icon, .step-status i { animation: none; }
}

.version-switcher { display: grid; gap: 5px; margin: 13px 7px 0; border-top: 1px solid var(--line); padding-top: 10px; }
.version-switcher label { color: var(--secondary); font-size: 10px; font-weight: 650; }
.version-switcher select { width: 100%; min-width: 0; border: 1px solid var(--line-strong); border-radius: 7px; outline: none; background: var(--ob-surface); padding: 6px 24px 6px 7px; color: var(--ink); font: inherit; font-size: 10px; cursor: pointer; }
.version-switcher select:focus { border-color: rgb(var(--ob-blue-rgb) / 0.55); box-shadow: 0 0 0 2px rgb(var(--ob-blue-rgb) / 0.1); }
.version-switcher small { overflow: hidden; color: var(--tertiary); font-size: 9px; line-height: 1.4; text-overflow: ellipsis; }

.plan-inspector { width: 100%; max-width: 100%; min-width: 0; min-height: 0; overflow-x: hidden; overflow-y: auto; padding: 18px 20px 22px; scrollbar-color: var(--ob-scrollbar) transparent; scrollbar-width: thin; }
.inspector-content, .criterion, .criterion-copy, .evidence-list, .deliverable, .deliverable > div { max-width: 100%; min-width: 0; }
.inspector-kicker { color: var(--blue); font-size: 10px; font-weight: 680; letter-spacing: .06em; text-transform: uppercase; }
.inspector-title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-top: 4px; }
.inspector-title-row h2 { min-width: 0; overflow-wrap: anywhere; margin: 0; color: var(--ink); font-size: 19px; font-weight: 700; letter-spacing: -.025em; }
.done-label { display: inline-flex; align-items: center; gap: 4px; color: var(--secondary); font-size: 11px; font-weight: 620; white-space: nowrap; }
.done-label svg { width: 12px; }
.done-label.tone-success { color: var(--green); }
.done-label.tone-active { color: var(--blue); }
.done-label.tone-danger { color: var(--red); }
.inspector-summary { overflow-wrap: anywhere; margin: 7px 0 16px; color: var(--secondary); font-size: 12.5px; line-height: 1.58; }

.step-detail-stack { display: grid; grid-template-columns: minmax(0, 1fr); gap: 9px; margin-bottom: 16px; }
.info-cell { min-width: 0; border: 1px solid var(--line); border-radius: 10px; background: rgb(var(--ob-surface-rgb) / 0.72); padding: 9px 10px; }
.info-cell span { display: block; color: var(--tertiary); font-size: 10px; font-weight: 620; }
.info-cell p { margin: 4px 0 0; color: var(--ob-text); font-size: 11.5px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.method-text { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.method-text.is-expanded { display: block; overflow: visible; -webkit-line-clamp: unset; }
.method-toggle { display: flex; align-items: center; gap: 3px; margin: 6px auto -3px; border: 0; border-radius: 6px; background: transparent; padding: 2px 7px; color: var(--ob-blue); font-size: 9.5px; cursor: pointer; }
.method-toggle:hover, .method-toggle:focus-visible { background: rgb(var(--ob-blue-rgb) / 0.07); color: var(--ob-blue); }
.method-toggle:focus-visible { outline: 2px solid rgb(var(--ob-blue-rgb) / 0.16); outline-offset: 1px; }
.method-toggle i { font-size: 13px; font-style: normal; line-height: .7; transition: transform .16s ease; }
.method-toggle i.is-expanded { transform: rotate(180deg); }
.step-activity-card { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: 10px; background: rgb(var(--ob-surface-rgb) / 0.72); padding: 9px 10px 10px; }
.step-activity-card > header { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 5px; }
.step-activity-card > header > div { display: grid; min-width: 0; gap: 2px; }
.step-activity-card > header strong { color: var(--ob-text); font-size: 10px; font-weight: 650; }
.step-activity-card > header span { overflow: hidden; color: var(--tertiary); font-size: 9.5px; text-overflow: ellipsis; white-space: nowrap; }
.step-activity-card > header em { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 4px; color: var(--tertiary); font-size: 9.5px; font-style: normal; }
.step-activity-card > header em svg { width: 11px; }
.step-activity-error { margin: 6px 0; border-radius: 7px; background: var(--ob-danger-soft); padding: 6px 8px; color: var(--red); font-size: 10px; overflow-wrap: anywhere; }
.step-activity-loading { display: flex; min-height: 44px; align-items: center; justify-content: center; gap: 6px; color: var(--tertiary); font-size: 10.5px; }
.step-activity-loading svg { width: 13px; }
.blocker-note { display: flex; gap: 8px; margin-bottom: 14px; border: 1px solid rgb(var(--ob-danger-rgb) / 0.16); border-radius: 9px; background: var(--ob-danger-soft); padding: 8px 10px; color: var(--red); }
.blocker-note > svg { width: 14px; flex: 0 0 auto; }
.blocker-note strong { font-size: 11px; }
.blocker-note p { margin: 2px 0 0; font-size: 10.5px; line-height: 1.45; }

.section-label { display: flex; align-items: center; justify-content: space-between; margin: 16px 0 7px; color: var(--ob-text); font-size: 11.5px; font-weight: 650; }
.section-label span:last-child { color: var(--tertiary); font-size: 10px; font-weight: 500; }
.criterion { border-top: 1px solid var(--line); padding: 10px 0; }
.criterion-head { display: flex; gap: 9px; align-items: flex-start; }
.criterion-check { display: grid; width: 19px; height: 19px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: rgb(var(--ob-surface-soft-rgb) / 0.12); color: var(--tertiary); }
.criterion-check.tone-success { background: var(--green-soft); color: var(--green); }
.criterion-check.tone-active { background: var(--blue-soft); color: var(--blue); }
.criterion-check.tone-danger { background: var(--ob-danger-soft); color: var(--red); }
.criterion-check svg { width: 11px; }
.criterion-copy strong { display: block; color: var(--ink); font-size: 12px; font-weight: 620; line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }
.criterion-copy p { margin: 4px 0 0; color: var(--secondary); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.evidence-list { display: grid; gap: 6px; margin: 7px 0 0 28px; }
.evidence { width: 100%; max-width: 100%; min-width: 0; overflow: hidden; border: 1px solid rgb(var(--ob-blue-rgb) / 0.14); border-radius: 9px; background: var(--blue-soft); padding: 8px 10px; }
.evidence-title-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.evidence-kind { border-radius: 999px; background: rgb(var(--ob-blue-rgb) / 0.09); padding: 2px 6px; color: var(--ob-blue); font-size: 9.5px; font-weight: 650; }
.evidence-info { flex: 0 0 auto; border-bottom: 1px dotted var(--ob-blue); color: var(--ob-blue); font-size: 9.5px; cursor: help; }
.evidence-summary { margin: 6px 0 0; color: var(--ob-blue); font-size: 10.5px; font-weight: 560; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.evidence-tooltip { max-width: 360px; }
.evidence-tooltip strong { display: block; color: var(--ob-text); font-size: 11px; }
.evidence-tooltip p { margin: 4px 0 0; color: var(--ob-text); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.evidence-tooltip code { display: block; margin-top: 6px; color: var(--ob-text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 9.5px; overflow-wrap: anywhere; word-break: break-all; }
.evidence-missing { display: flex; align-items: center; gap: 5px; margin: 7px 0 0 28px; color: var(--orange); font-size: 10px; }
.evidence-missing svg { width: 11px; }
.step-times { display: flex; justify-content: space-between; gap: 10px; margin-top: 7px; color: var(--tertiary); font-size: 9.5px; }
.inline-empty { border: 1px dashed var(--line-strong); border-radius: 9px; padding: 14px; color: var(--tertiary); font-size: 11px; text-align: center; }

.completion-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 18px; border-top: 1px solid var(--line); padding-top: 12px; }
.completion-item { display: flex; align-items: flex-start; gap: 7px; min-width: 0; border: 0; background: transparent; padding: 2px 0; color: var(--secondary); text-align: left; cursor: pointer; }
.completion-item.done { cursor: default; }
.completion-item > span:first-child { display: grid; width: 15px; height: 15px; flex: 0 0 auto; place-items: center; color: var(--tertiary); }
.completion-item.done > span:first-child { color: var(--green); }
.completion-item svg { width: 13px; }
.completion-item > span:last-child { display: grid; min-width: 0; gap: 2px; }
.completion-item strong { color: var(--ob-text); font-size: 10.5px; font-weight: 560; overflow-wrap: anywhere; word-break: break-word; }
.completion-item small { color: var(--tertiary); font-size: 9.5px; line-height: 1.35; overflow-wrap: anywhere; word-break: break-word; }
.decision-note { display: flex; gap: 8px; margin-top: 13px; border-top: 1px solid var(--line); padding-top: 12px; color: var(--secondary); }
.decision-note > svg { width: 13px; flex: 0 0 auto; color: var(--green); }
.decision-note strong { font-size: 10.5px; }
.decision-note p { margin: 2px 0 0; font-size: 10px; line-height: 1.45; }

.deliverable-list { display: grid; gap: 0; border-top: 1px solid var(--line); }
.deliverable { display: flex; gap: 9px; border-bottom: 1px solid var(--line); padding: 10px 0; }
.deliverable-check { display: grid; width: 19px; height: 19px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: rgb(var(--ob-surface-soft-rgb) / 0.12); color: var(--tertiary); }
.deliverable.done .deliverable-check { background: var(--green-soft); color: var(--green); }
.deliverable-check svg { width: 11px; }
.deliverable > div { min-width: 0; }
.deliverable strong { color: var(--ink); font-size: 11.5px; }
.deliverable p { margin: 4px 0 0; color: var(--secondary); font-size: 10.5px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.source-list { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.source-list .source-chip { display: inline-block; max-width: 100%; overflow: hidden; border-radius: 999px; background: rgb(var(--ob-surface-soft-rgb) / 0.1); padding: 3px 7px; color: var(--secondary); font-size: 9.5px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; cursor: help; }

.plan-touch-details { display: none; }
.spin { animation: planSpin 1s linear infinite; }
@keyframes planSpin { to { transform: rotate(360deg); } }

/* The same card can be narrow inside a desktop work pane. Size this fallback
   by the actual card as well as the viewport, without changing wide plans. */
@container agent-detail (max-width: 600px) {
	.plan-overview { display: block; overflow: auto; }
	.plan-sidebar { overflow: visible; border-right: 0; border-bottom: 1px solid var(--line); }
	.plan-inspector { overflow: visible; }
	.step-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	.completion-list { grid-template-columns: 1fr; }
	.inspector-title-row { flex-wrap: wrap; }
}
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.plan-state-empty { overflow: auto; align-items: flex-start; justify-content: flex-start; flex-direction: column; gap: 8px; padding: 12px; }
	.plan-overview { display: block; overflow: auto; -webkit-overflow-scrolling: touch; }
	.plan-sidebar { overflow: visible; border-right: 0; border-bottom: 1px solid var(--line); }
	.plan-inspector { overflow: visible; padding: 12px 10px 16px; }
	.plan-sidebar { padding: 10px 6px; }
	.step-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	.completion-list { grid-template-columns: 1fr; }
	.inspector-title-row { flex-wrap: wrap; }
	.plan-touch-details { display: block; margin-top: 6px; font-size: 11px; overflow-wrap: anywhere; }
	.plan-touch-details summary { cursor: pointer; min-height: 32px; padding: 6px 0; color: var(--secondary); }
	.plan-touch-details code { display: block; white-space: pre-wrap; overflow-wrap: anywhere; }
	.step-activity-card > header { flex-wrap: wrap; }
	.step-activity-card > header span { white-space: normal; overflow-wrap: anywhere; }
}
</style>

<style>
/* OpenBear system dark theme */
html.dark .evidence-info {
		border-bottom: 1px dotted rgb(var(--ob-blue-rgb) / 0.52);
	}
</style>
