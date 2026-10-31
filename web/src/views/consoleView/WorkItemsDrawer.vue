<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {ArrowLeft, ChevronRight, ExternalLink, FileText, GitCommitHorizontal, ListChecks, Pencil, Plus, Ban, RotateCcw, X, FlaskConical, Terminal, Link as LinkIcon, StickyNote} from "@lucide/vue";
import {Api} from "../../api.js";
import {openArtifactPreview, workItemFileIdentity} from "../../artifacts/artifactFiles.js";
import WorkItemStatusIcon from "./WorkItemStatusIcon.vue";
import WorkItemTree from "./WorkItemTree.vue";
import {STATUS_LABELS, formatTime, isClosed, leaves, statusOf, useWorkItems, walk} from "./workItems.js";

const props = defineProps({conversationUuid: {type: String, default: ""}});
const emit = defineEmits(["locate"]);
const store = useWorkItems();

const drawerOpen = ref(false);
const stack = ref([]);
const detail = ref(null), detailLoading = ref(false), detailError = ref("");
const expanded = reactive({}), droppedOpen = reactive({});
const filter = ref("open"), outlineOpen = ref(false);
const addDraft = ref(null), addInput = ref(null);
const editor = reactive({open: false, number: "", revision: 0, title: "", description: "", criteria: "", saving: false});
const dropper = reactive({open: false, number: "", title: "", reason: "", saving: false});
let detailGeneration = 0, editGeneration = 0;

const usable = computed(() => Boolean(props.conversationUuid) && !props.conversationUuid.startsWith("local:"));
const tasks = computed(() => store?.state.tasks || []);
const openTasks = computed(() => {
	const order = {in_progress: 0, decided: 1, discussing: 2};
	return tasks.value.filter(task => !isClosed(task.status)).sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));
});
const closedTasks = computed(() => tasks.value.filter(task => isClosed(task.status)));
const summary = computed(() => store?.summary() || {open: 0, done: 0, total: 0, attention: 0});
const focus = computed(() => store?.state.running && store.state.focus ? store.state.focus : null);
const focusRoots = computed(() => focus.value?.numbers || []);
const workingItems = computed(() => store?.currentItems() || []);
const currentItem = computed(() => workingItems.value[0] || null);
const focusRemaining = computed(() => (store?.focusItems() || []).filter(item => !["done", "dropped"].includes(item.status)).length);
const currentNumber = computed(() => stack.value[stack.value.length - 1] || "");
const node = computed(() => currentNumber.value ? store?.find(currentNumber.value) : null);
const tooltip = computed(() => {
	const {done, total, attention} = summary.value;
	return ["任务", focus.value ? "执行中" : "", total ? `${done}/${total}` : "", attention ? `${attention} 项等你定` : ""].filter(Boolean).join(" · ");
});

function focusLabel(numbers = focusRoots.value) {
	return numbers.map(number => `#${number} ${store?.find(number)?.title || ""}`.trim()).join("、");
}
function taskProgress(task) {
	if (!task.children?.length) return null;
	const items = leaves(task), live = items.filter(item => item.status !== "dropped");
	return {
		done: live.filter(item => item.status === "done").length, total: live.length, dropped: items.length - live.length,
		ask: live.filter(item => item.status === "ask").length,
		blocked: live.filter(item => item.status === "blocked").length,
	};
}
function percent(progress) {
	return progress?.total ? Math.round(progress.done / progress.total * 100) : 0;
}

function defaultExpand() {
	for (const task of tasks.value) {
		if (expanded[task.number] !== undefined) continue;
		expanded[task.number] = task.status === "in_progress";
		for (const item of walk(task.children)) {
			if (!item.children?.length || expanded[item.number] !== undefined) continue;
			expanded[item.number] = focusRoots.value.some(root => root === item.number || item.number.startsWith(`${root}.`) || root.startsWith(`${item.number}.`))
				|| leaves(item).some(leaf => ["doing", "ask", "blocked"].includes(leaf.status));
		}
	}
}
watch(() => store?.state.revision, () => {
	defaultExpand();
	if (drawerOpen.value && currentNumber.value) void loadDetail();
});

async function loadDetail() {
	const number = currentNumber.value;
	if (!number || !usable.value) { detail.value = null; return; }
	const current = ++detailGeneration;
	detailLoading.value = !detail.value || detail.value.number !== number;
	detailError.value = "";
	try {
		const data = await Api.workItem(props.conversationUuid, number);
		if (current !== detailGeneration) return;
		detail.value = data.item;
	} catch (error) {
		if (current !== detailGeneration) return;
		if (error?.response?.status === 404) { stack.value = stack.value.slice(0, -1); detail.value = null; void loadDetail(); return; }
		detailError.value = error?.response?.data?.message || "读取失败";
	} finally {
		if (current === detailGeneration) detailLoading.value = false;
	}
}

function open(number = "") {
	if (!usable.value) return;
	drawerOpen.value = true;
	void store?.refresh();
	stack.value = number ? [...ancestorsOf(number), number] : [];
	detail.value = null;
	if (number) {
		for (const ancestor of ancestorsOf(number)) expanded[ancestor] = true;
		void loadDetail();
	} else detail.value = null;
}
function ancestorsOf(number) {
	const parts = String(number).split("."), out = [];
	for (let index = 1; index < parts.length; index++) out.push(parts.slice(0, index).join("."));
	return out;
}
function push(number) {
	stack.value = [...stack.value, number];
	detail.value = null;
	void loadDetail();
}
function back() {
	stack.value = stack.value.slice(0, -1);
	detail.value = null;
	if (stack.value.length) void loadDetail();
}
function toggle(number) { expanded[number] = !expanded[number]; }
function toggleDropped(parent) { droppedOpen[parent] = !droppedOpen[parent]; }

// On a phone the panel is a bottom sheet: a dimmed strip stays above it to tap, and a
// downward drag on its header follows the finger and slides it out from where it is let go.
const phone = ref(false);
const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let media = null, gesture = null;
const sheetStyle = computed(() => phone.value && sheetOffset.value
	? {"--wi-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
	: undefined);
function updateMedia() {
	phone.value = Boolean(media?.matches);
}
function dragStart(event) {
	if (!phone.value || sheetClosing.value || event.button > 0 || event.target.closest("button, input, textarea, a")) return;
	gesture = {id: event.pointerId, x: event.clientX, y: event.clientY, lastY: event.clientY, lastT: event.timeStamp, speed: 0};
	event.currentTarget.setPointerCapture?.(event.pointerId);
}
function dragMove(event) {
	if (!gesture || event.pointerId !== gesture.id) return;
	const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
	if (!sheetDragging.value) {
		if (Math.hypot(dx, dy) < 4) return;
		if (dy < 0 || Math.abs(dx) > Math.abs(dy)) { dragCancel(); return; }
		sheetDragging.value = true;
	}
	gesture.speed = (event.clientY - gesture.lastY) / Math.max(1, event.timeStamp - gesture.lastT);
	gesture.lastY = event.clientY; gesture.lastT = event.timeStamp;
	sheetOffset.value = Math.max(0, dy);
}
function dragCancel() {
	gesture = null; sheetDragging.value = false; sheetOffset.value = 0;
}
function dragEnd() {
	if (!gesture) return;
	if (sheetOffset.value >= 88 || (sheetOffset.value > 24 && gesture.speed > 0.5)) {
		gesture = null; sheetDragging.value = false; sheetClosing.value = true;
		drawerOpen.value = false;
	} else dragCancel();
}
function sheetClosed() {
	sheetClosing.value = false; sheetOffset.value = 0;
}

onMounted(() => {
	store?.setOpener(open);
	media = window.matchMedia?.("(max-width: 760px)") || null;
	updateMedia();
	media?.addEventListener?.("change", updateMedia);
});
onBeforeUnmount(() => {
	store?.setOpener(null);
	media?.removeEventListener?.("change", updateMedia);
});
watch(() => props.conversationUuid, () => {
	drawerOpen.value = false; stack.value = []; detail.value = null;
	for (const key of Object.keys(expanded)) delete expanded[key];
	for (const key of Object.keys(droppedOpen)) delete droppedOpen[key];
});
watch(() => [props.conversationUuid, currentNumber.value], () => {
	detailGeneration++; editGeneration++;
	addDraft.value = null;
	editor.open = false; editor.saving = false;
	dropper.open = false; dropper.saving = false;
}, {flush: 'sync'});
defineExpose({open});

// ---------- actions ----------

function failure(error, fallback) {
	ElMessage.error(error?.response?.data?.message || error?.message || fallback);
}
function done(text) {
	ElMessage.success(`${text}，模型下一次请求会看到`);
	void store?.refresh();
}
const closed = computed(() => node.value ? isClosed(node.value.status) || ["done", "dropped"].includes(statusOf(node.value)) : false);
function openAdd() {
	addDraft.value = {conversationUuid: props.conversationUuid, number: node.value.number, title: "", saving: false};
	void nextTick(() => addInput.value?.focus());
}
async function saveAdd() {
	const draft = addDraft.value;
	if (!draft?.title.trim() || draft.saving || draft.conversationUuid !== props.conversationUuid || draft.number !== currentNumber.value) return;
	draft.saving = true;
	try {
		await Api.addWorkItem(draft.conversationUuid, draft.number, {title: draft.title.trim()});
		if (addDraft.value !== draft || props.conversationUuid !== draft.conversationUuid) return;
		expanded[draft.number] = true;
		addDraft.value = null;
		done("已加上");
		void loadDetail();
	} catch (error) { if (addDraft.value === draft) failure(error, "没能加上"); }
	finally { draft.saving = false; }
}
function openEditor() {
	const item = detail.value;
	Object.assign(editor, {open: true, number: item.number, revision: item.revision, title: item.title,
		description: item.description || "", criteria: (item.criteria || []).join("\n"), saving: false});
}
async function saveEditor() {
	if (editor.saving || !editor.open) return;
	const generation = editGeneration;
	editor.saving = true;
	try {
		await Api.updateWorkItem(props.conversationUuid, editor.number, {
			revision: editor.revision, title: editor.title.trim(), description: editor.description.trim(),
			criteria: editor.criteria.split("\n").map(line => line.trim()).filter(Boolean),
		});
		if (generation !== editGeneration) return;
		editor.open = false;
		done("已保存");
		void loadDetail();
	} catch (error) {
		if (generation === editGeneration) failure(error, error?.response?.status === 409 ? "这一项刚被改过，请重新打开再改" : "没能保存");
	} finally { if (generation === editGeneration) editor.saving = false; }
}
function openDropper() {
	Object.assign(dropper, {open: true, number: node.value.number, title: node.value.title, reason: "", saving: false});
}
async function saveDropper() {
	if (dropper.saving || !dropper.open) return;
	const generation = editGeneration;
	dropper.saving = true;
	try {
		await Api.dropWorkItem(props.conversationUuid, dropper.number, {reason: dropper.reason.trim()});
		if (generation !== editGeneration) return;
		dropper.open = false;
		done("已标为不做");
		void loadDetail();
	} catch (error) { if (generation === editGeneration) failure(error, "没能标为不做"); }
	finally { if (generation === editGeneration) dropper.saving = false; }
}
async function reopen() {
	try {
		await Api.reopenWorkItem(props.conversationUuid, node.value.number, {});
		done("已重新打开");
		void loadDetail();
	} catch (error) { failure(error, "没能重新打开"); }
}
async function remove() {
	try {
		await ElMessageBox.confirm("只用于建错的项。删除后不再显示，经过里留一条删除记录。你决定不做的项，请用“标为不做”。",
			`删除 #${node.value.number}？`, {confirmButtonText: "删除", cancelButtonText: "取消", type: "warning", customClass: "work-items-confirm"});
	} catch { return; }
	try {
		await Api.deleteWorkItem(props.conversationUuid, node.value.number, {});
		back();
		done("已删除");
	} catch (error) { failure(error, "没能删除"); }
}

// ---------- detail presentation ----------

const crumbs = computed(() => (detail.value?.ancestors || []).map(item => `#${item.number} ${item.title}`).join(" › "));
const EVIDENCE_ICONS = {file: FileText, report: FileText, commit: GitCommitHorizontal, test: FlaskConical, command: Terminal, link: LinkIcon, message: StickyNote, note: StickyNote};
function evidenceText(entry) {
	if (entry.kind === "commit") return `提交 ${String(entry.commit || "").slice(0, 10)}${entry.label ? ` · ${entry.label}` : ""}`;
	return entry.path || entry.url || entry.text || entry.label || "";
}
// A file on another server is shown with its server; this machine cannot open it.
function openable(entry) {
	if (entry.server) return false;
	return ["file", "report"].includes(entry.kind) && entry.path || entry.kind === "commit" || entry.hasDiff;
}
function openEvidence(entry, ref, event) {
	if (entry.kind === "link" && entry.url) { window.open(entry.url, "_blank", "noopener"); return; }
	if (!openable(entry)) return;
	openArtifactPreview(workItemFileIdentity(props.conversationUuid, detail.value.number, ref), evidenceText(entry), event?.currentTarget);
}
function eventSourceLink(event) {
	const source = event.source || {};
	return source.opId && !String(source.opId).startsWith("tool:") ? source.opId : "";
}
function locate(event) {
	const opId = eventSourceLink(event);
	if (!opId) return;
	drawerOpen.value = false;
	emit("locate", {opId, label: `${event.actorLabel} ${formatTime(event.createdAtMs)} 的${event.actor === "user" ? "消息" : "回复"}`, number: detail.value.number});
}
function stateLine(data) {
	if (!data?.from && !data?.to) return "";
	return `${STATUS_LABELS[data.from] || data.from || ""} → ${STATUS_LABELS[data.to] || data.to || ""}`;
}
function changeRows(data) {
	if (data?.old || data?.new) return [["原来", data.old, true], ["改为", data.new], ...(data.why ? [["原因", data.why]] : [])].filter(row => row[1]);
	if (data?.before && data?.after) {
		const labels = {title: "标题", description: "说明", criteria: "完成标准", scopeMethod: "范围说明"};
		return Object.keys(data.after).flatMap(key => [[`${labels[key] || key}原来`, show(data.before[key]), true], [`${labels[key] || key}改为`, show(data.after[key])]]);
	}
	return [];
}
function show(value) {
	return Array.isArray(value) ? value.join("；") : String(value ?? "");
}
</script>

<template>
	<div v-if="usable" class="work-items-entry-wrap">
		<el-tooltip :content="tooltip" placement="left" :show-after="260">
			<button type="button" class="work-items-entry" :class="{active: drawerOpen, 'is-working': focus}"
			        :aria-label="tooltip" aria-haspopup="dialog" :aria-expanded="drawerOpen ? 'true' : 'false'" @click="open()">
				<span class="work-items-entry-icon"><ListChecks :stroke-width="1.6" aria-hidden="true"/></span>
				<span v-if="summary.attention" class="work-items-badge" aria-hidden="true">{{ summary.attention > 99 ? '99+' : summary.attention }}</span>
			</button>
		</el-tooltip>
	</div>

	<el-drawer v-model="drawerOpen" :direction="phone ? 'btt' : 'rtl'" :size="phone ? 'calc(var(--mobile-viewport-height, 100dvh) - max(56px, env(safe-area-inset-top, 0px) + 40px))' : 'min(42rem, 100vw)'"
	           :class="['work-items-drawer', phone ? 'mobile-viewport-sheet' : 'mobile-viewport-drawer', {'is-dragging': sheetDragging}]" :style="sheetStyle"
	           modal-class="work-items-overlay" append-to-body :with-header="false" :destroy-on-close="false" aria-labelledby="work-items-title" @closed="sheetClosed">
		<header class="wi-head" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
			<i v-if="phone" class="wi-grip" aria-hidden="true"></i>
			<div><h2 id="work-items-title">任务</h2><p>本会话的任务：讨论经过、你的决定和执行情况。</p></div>
			<button type="button" class="wi-icon-action" aria-label="关闭任务面板" @click="drawerOpen = false"><X :stroke-width="1.7"/></button>
		</header>

		<section v-if="!node" class="wi-browse" aria-label="任务列表">
			<div v-if="focus" class="wi-focus-box">
				<div class="wi-focus-cap"><ListChecks :stroke-width="1.7" aria-hidden="true"/>本轮执行范围<span class="wi-run"><i class="wi-dot"></i>运行中</span></div>
				<strong>{{ focusLabel() }}</strong>
				<div class="wi-focus-sub">还剩 {{ focusRemaining }} 项</div>
				<button v-if="currentItem" type="button" class="wi-focus-cur" @click="push(currentItem.number)">
					<span class="wi-now"><i class="wi-dot"></i>正在做</span><span class="wi-cur-num">#{{ currentItem.number }}</span>
					<span class="wi-cur-tx">{{ currentItem.title }}</span><span v-if="workingItems.length > 1" class="wi-cur-more">等 {{ workingItems.length }} 项</span><ChevronRight :stroke-width="1.8" aria-hidden="true"/>
				</button>
			</div>
			<div class="wi-ltools">
				<div class="wi-seg" role="group" aria-label="筛选">
					<button type="button" :aria-pressed="filter === 'open'" @click="filter = 'open'">未完成 {{ openTasks.length }}</button>
					<button type="button" :aria-pressed="filter === 'all'" @click="filter = 'all'">全部 {{ tasks.length }}</button>
				</div>
				<span v-if="summary.attention" class="wi-attn-sum">{{ summary.attention }} 项等你定</span>
			</div>
			<div class="wi-scroll">
				<div v-if="!tasks.length" class="wi-empty" role="status">
					<ListChecks :stroke-width="1.5" aria-hidden="true"/>
					<strong>还没有任务</strong>
					<p>需要调查、决定或动手的问题提出来后，模型会在这里建任务，记下讨论经过、你的决定和执行情况。</p>
				</div>
				<article v-for="task in (filter === 'all' ? [...openTasks, ...closedTasks] : openTasks)" :key="task.number" class="wi-card" :class="{'is-done': isClosed(task.status)}">
					<div class="wi-card-main" role="button" tabindex="0" @click="push(task.number)" @keydown.enter.prevent="push(task.number)">
						<button v-if="task.children?.length" type="button" class="wi-tog" :class="{open: expanded[task.number]}" :aria-label="expanded[task.number] ? '收起子项' : '展开子项'" @click.stop="toggle(task.number)"><ChevronRight :stroke-width="1.8"/></button>
						<span v-else class="wi-pad"></span>
						<span class="wi-ttl"><strong>{{ task.title }}</strong><span class="wi-num">#{{ task.number }}</span></span>
						<span class="wi-meta">
							<span v-if="store?.isCurrent(task.number)" class="wi-now"><i class="wi-dot"></i>正在做</span>
							<span v-else :class="`wi-s-${task.status}`">{{ STATUS_LABELS[task.status] }}</span>
							<template v-if="taskProgress(task)">
								<span class="wi-bar"><i :style="{width: `${percent(taskProgress(task))}%`}"></i></span>
								<span>{{ taskProgress(task).done }}/{{ taskProgress(task).total }}</span>
								<span v-if="taskProgress(task).ask" class="wi-warn">{{ taskProgress(task).ask }} 项等你定</span>
								<span v-if="taskProgress(task).blocked" class="wi-danger">{{ taskProgress(task).blocked }} 项受阻</span>
								<span v-if="taskProgress(task).dropped">{{ taskProgress(task).dropped }} 项按决定不做</span>
							</template>
							<span v-else>还没有子项</span>
							<span>更新于 {{ formatTime(task.updatedAtMs) }}</span>
						</span>
					</div>
					<div v-if="task.children?.length && expanded[task.number]" class="wi-tree">
						<WorkItemTree :nodes="task.children" :parent="task.number" :expanded="expanded" :dropped-open="droppedOpen" :focus-roots="focusRoots"
						              @open="push" @toggle="toggle" @toggle-dropped="toggleDropped"/>
					</div>
				</article>
				<button v-if="filter === 'open' && closedTasks.length" type="button" class="wi-section-cap" @click="filter = 'all'"><ChevronRight :stroke-width="1.8"/>已完成、已放弃 {{ closedTasks.length }}</button>
			</div>
			<div class="wi-outline">
				<button type="button" class="wi-outline-toggle" :class="{open: outlineOpen}" :aria-expanded="outlineOpen" @click="outlineOpen = !outlineOpen">
					<span>模型每次请求看到的大纲</span><small>约 {{ store?.state.outline?.tokens || 0 }} tokens</small><ChevronRight :stroke-width="1.8"/>
				</button>
				<pre v-if="outlineOpen" tabindex="0" aria-label="模型看到的任务大纲">{{ store?.state.outline?.text || '没有进行中的任务时，不给模型带大纲。' }}</pre>
			</div>
		</section>

		<section v-else class="wi-detail" aria-label="任务详情" :aria-busy="detailLoading ? 'true' : 'false'">
			<div class="wi-dtool">
				<button type="button" class="wi-back" @click="back"><ArrowLeft :stroke-width="1.8"/>{{ stack.length > 1 ? '返回上一级' : '返回列表' }}</button>
				<span v-if="crumbs" class="wi-crumb" :title="crumbs">{{ crumbs }}</span>
			</div>
			<div class="wi-scroll">
				<h3 class="wi-dtitle">{{ node.title }}</h3>
				<div class="wi-dmeta">
					<span v-if="store?.isCurrent(node.number) && !closed" class="wi-chip is-now"><i class="wi-dot"></i>正在做</span>
					<span v-else class="wi-chip" :class="`wi-chip-${statusOf(node)}`"><WorkItemStatusIcon :status="statusOf(node)"/>{{ STATUS_LABELS[statusOf(node)] }}</span>
					<span class="wi-num">#{{ node.number }}</span>
					<span v-if="taskProgress(node)">{{ taskProgress(node).done }}/{{ taskProgress(node).total }} 完成</span>
					<span v-if="store?.inFocus(node.number)" class="wi-tag">本轮范围内</span>
				</div>
				<div class="wi-acts">
					<button v-if="!closed && node.number.split('.').length < 3" type="button" class="wi-btn" @click="openAdd"><Plus :stroke-width="1.8"/>加子项</button>
					<button type="button" class="wi-btn" :disabled="!detail" @click="openEditor"><Pencil :stroke-width="1.8"/>编辑</button>
					<button v-if="!closed" type="button" class="wi-btn" @click="openDropper"><Ban :stroke-width="1.8"/>标为不做</button>
					<button v-else type="button" class="wi-btn" @click="reopen"><RotateCcw :stroke-width="1.8"/>重新打开</button>
				</div>
				<div v-if="node.status === 'ask' && node.statusReason" class="wi-qbox"><div class="wi-qh"><WorkItemStatusIcon status="ask"/>要你定</div><p>{{ node.statusReason }}</p><small>在对话里回复就行；也可以直接把这一项标为不做。</small></div>
				<div v-else-if="node.status === 'blocked' && node.statusReason" class="wi-qbox is-blocked"><div class="wi-qh"><WorkItemStatusIcon status="blocked"/>受阻</div><p>{{ node.statusReason }}</p></div>
				<div v-if="detailError" class="wi-notice" role="alert">{{ detailError }}<button type="button" class="wi-link" @click="loadDetail">重新读取</button></div>
				<template v-if="detail">
					<div v-if="detail.description" class="wi-blk"><h4>说明</h4><p>{{ detail.description }}</p></div>
					<div v-if="detail.criteria?.length" class="wi-blk"><h4>完成标准</h4><ul><li v-for="(line, index) in detail.criteria" :key="index">{{ line }}</li></ul></div>
					<div v-if="detail.scopeMethod" class="wi-blk"><h4>范围怎么找全的</h4><div class="wi-scope">{{ detail.scopeMethod }}</div></div>
				</template>
				<div v-if="node.children?.length || addDraft" class="wi-blk wi-kids">
					<h4>子项<template v-if="taskProgress(node)"> · {{ taskProgress(node).done }}/{{ taskProgress(node).total }}</template></h4>
					<WorkItemTree v-if="node.children?.length" :nodes="node.children" :parent="node.number" :expanded="expanded" :dropped-open="droppedOpen" :focus-roots="focusRoots"
					              @open="push" @toggle="toggle" @toggle-dropped="toggleDropped"/>
					<form v-if="addDraft" class="wi-add" @submit.prevent="saveAdd">
						<el-input ref="addInput" v-model="addDraft.title" :disabled="addDraft.saving" placeholder="新子项的标题" maxlength="300" aria-label="新子项的标题"/>
						<button type="submit" class="wi-btn primary" :disabled="addDraft.saving || !addDraft.title.trim()">{{ addDraft.saving ? '添加中…' : '加上' }}</button>
						<button type="button" class="wi-btn" :disabled="addDraft.saving" @click="addDraft = null">取消</button>
					</form>
				</div>
				<div v-if="detail?.evidence?.length" class="wi-blk"><h4>证据</h4>
					<div class="wi-ev">
						<component :is="openable(entry) || entry.kind === 'link' ? 'button' : 'div'" v-for="(entry, index) in detail.evidence" :key="index"
						           :type="openable(entry) || entry.kind === 'link' ? 'button' : undefined" class="wi-evi" :class="{openable: openable(entry) || entry.kind === 'link'}"
						           @click="openEvidence(entry, `item:${index}`, $event)">
							<component :is="EVIDENCE_ICONS[entry.kind] || StickyNote" :stroke-width="1.7" aria-hidden="true"/>
							<code v-if="['file', 'report', 'commit'].includes(entry.kind)">{{ evidenceText(entry) }}</code><span v-else>{{ evidenceText(entry) }}</span>
							<span v-if="entry.server" class="wi-server">服务器 {{ entry.server }}</span>
							<span v-else-if="openable(entry) || entry.kind === 'link'" class="wi-op">打开</span>
						</component>
					</div>
				</div>
				<div class="wi-blk"><h4>经过</h4>
					<div v-if="detailLoading && !detail" class="wi-placeholder" aria-label="正在读取"></div>
					<ol v-else class="wi-tl">
						<li v-for="event in detail?.events || []" :key="event.eventUuid" :class="{'by-you': event.actor === 'user'}">
							<span class="wi-pin"></span>
							<div class="wi-tlh"><b>{{ event.label }}</b><span>{{ formatTime(event.createdAtMs) }}</span><span>{{ event.actorLabel }}</span>
								<button v-if="eventSourceLink(event)" type="button" class="wi-link" @click="locate(event)">原对话<ExternalLink :stroke-width="1.8"/></button>
							</div>
							<div v-if="event.summary && event.kind !== 'raise'" class="wi-tlb">{{ event.summary }}</div>
							<div v-if="event.kind === 'state' && stateLine(event.data)" class="wi-tlb wi-sub">{{ stateLine(event.data) }}</div>
							<dl v-if="changeRows(event.data).length" class="wi-chg"><template v-for="(row, index) in changeRows(event.data)" :key="index"><dt>{{ row[0] }}</dt><dd :class="{old: row[2]}">{{ row[1] }}</dd></template></dl>
							<div v-if="event.data?.quote" class="wi-quote">{{ event.data.quote }}</div>
							<div v-for="question in event.data?.questions || []" :key="question.id || question.question" class="wi-dec">
								<div class="wi-q">{{ question.question }}</div>
								<ul>
									<li v-for="option in question.options" :key="option" :class="{on: question.selected?.includes(option)}"><span class="wi-radio" aria-hidden="true"></span><span>{{ option }}</span><em v-if="question.selected?.includes(option)">你的选择</em></li>
									<li v-for="choice in (question.selected || []).filter(label => !question.options?.includes(label))" :key="`x-${choice}`" class="on"><span class="wi-radio" aria-hidden="true"></span><span>{{ choice }}</span><em>你的选择</em></li>
								</ul>
								<div v-if="question.text" class="wi-ut">补充：{{ question.text }}</div>
							</div>
							<div v-if="event.data?.added?.length" class="wi-tlb wi-sub">加了：{{ event.data.added.map(n => `#${n}`).join('、') }}</div>
							<div v-if="event.data?.removed?.length" class="wi-tlb wi-sub">删了：{{ event.data.removed.map(n => `#${n}`).join('、') }}</div>
							<div v-if="(event.data?.evidence || event.data?.files || []).length" class="wi-ev">
								<component :is="openable(entry) || entry.kind === 'link' ? 'button' : 'div'" v-for="(entry, index) in event.data.evidence || event.data.files" :key="index"
								           :type="openable(entry) || entry.kind === 'link' ? 'button' : undefined" class="wi-evi" :class="{openable: openable(entry) || entry.kind === 'link'}"
								           @click="openEvidence(entry, `event:${event.eventUuid}:${index}`, $event)">
									<component :is="EVIDENCE_ICONS[entry.kind] || StickyNote" :stroke-width="1.7" aria-hidden="true"/>
									<code v-if="['file', 'report', 'commit'].includes(entry.kind)">{{ evidenceText(entry) }}</code><span v-else>{{ evidenceText(entry) }}</span>
									<span v-if="entry.server" class="wi-server">服务器 {{ entry.server }}</span>
									<span v-else-if="openable(entry) || entry.kind === 'link'" class="wi-op">打开</span>
								</component>
							</div>
						</li>
						<li v-if="detail && !detail.events?.length" class="wi-empty-event"><span class="wi-pin"></span><div class="wi-tlb wi-sub">建立时没有单独的记录，见上一级。</div></li>
					</ol>
				</div>
				<details v-if="detail" class="wi-tech"><summary>详细信息</summary>
					<dl><dt>编号</dt><dd>#{{ detail.number }}</dd><dt>建立于</dt><dd>{{ formatTime(detail.createdAtMs) }}</dd><dt>版本</dt><dd>第 {{ detail.revision }} 版</dd>
						<dt>删除</dt><dd><button type="button" class="wi-danger-link" @click="remove">删除这一项</button>　只用于建错的项，会留一条删除记录</dd></dl>
				</details>
			</div>
		</section>
	</el-drawer>

	<el-dialog v-model="editor.open" class="work-items-dialog mobile-viewport-dialog" :title="`编辑 #${editor.number}`" width="min(34rem, 96vw)" append-to-body :close-on-click-modal="false">
		<p class="wi-dsub">你的修改会记进经过，模型下一次请求就能看到。</p>
		<form class="wi-form" @submit.prevent="saveEditor">
			<label for="wi-edit-title">标题</label>
			<el-input id="wi-edit-title" v-model="editor.title" maxlength="300" :disabled="editor.saving"/>
			<label for="wi-edit-description">说明 <span>可选</span></label>
			<el-input id="wi-edit-description" v-model="editor.description" type="textarea" :rows="3" resize="none" :disabled="editor.saving"/>
			<label for="wi-edit-criteria">完成标准 <span>每行一条</span></label>
			<el-input id="wi-edit-criteria" v-model="editor.criteria" type="textarea" :rows="3" resize="none" :disabled="editor.saving"/>
		</form>
		<template #footer>
			<el-button @click="editor.open = false">取消</el-button>
			<el-button type="primary" :loading="editor.saving" :disabled="!editor.title.trim()" @click="saveEditor">保存</el-button>
		</template>
	</el-dialog>

	<el-dialog v-model="dropper.open" class="work-items-dialog mobile-viewport-dialog" :title="`把 #${dropper.number} 标为不做`" width="min(34rem, 96vw)" append-to-body :close-on-click-modal="false">
		<p class="wi-dsub">{{ dropper.title }}</p>
		<form class="wi-form" @submit.prevent="saveDropper">
			<label for="wi-drop-reason">原因 <span>可选，会记进经过</span></label>
			<el-input id="wi-drop-reason" v-model="dropper.reason" type="textarea" :rows="3" resize="none" placeholder="例如：属于极端情况，不处理" :disabled="dropper.saving"/>
		</form>
		<template #footer>
			<el-button @click="dropper.open = false">取消</el-button>
			<el-button type="primary" :loading="dropper.saving" @click="saveDropper">标为不做</el-button>
		</template>
	</el-dialog>
</template>

<style scoped>
.work-items-entry-wrap { position: fixed; top: calc(var(--console-float-rail-top, calc(48% - 3.25rem)) - 3 * (var(--console-float-control-size, 30px) + var(--console-float-control-gap, 8px))); right: var(--console-float-rail-right, 1rem); z-index: 32; transition: right .24s cubic-bezier(.22, 1, .36, 1); }
.work-items-entry { position: relative; display: grid; place-items: center; width: var(--console-float-control-size, 30px); height: var(--console-float-control-size, 30px); border: 1px solid transparent; border-radius: 7px; background: var(--ob-chat-bg); color: var(--ob-chat-subtle); cursor: pointer; transition: border-color .15s ease, background .15s ease, color .15s ease; }
.work-items-entry:hover { color: var(--ob-chat-text); background: var(--ob-chat-hover); }
.work-items-entry.active { background: var(--ob-chat-selected); color: var(--ob-chat-text); }
.work-items-entry:focus-visible { box-shadow: 0 0 0 3px var(--ob-focus); outline: none; }
.work-items-entry-icon { position: relative; display: grid; place-items: center; width: 17px; height: 17px; }
.work-items-entry-icon svg { width: 17px; height: 17px; }
/* The same running mark as a conversation in the tree; while it spins the button is
   round, so hover, selection and focus share the spinner's shape. */
.work-items-entry.is-working { border-radius: 50%; }
.work-items-entry.is-working .work-items-entry-icon { color: var(--ob-blue); }
.work-items-entry.is-working .work-items-entry-icon::before { content: ""; pointer-events: none; position: absolute; inset: -4px; border: 1.5px solid transparent; border-top-color: var(--ob-blue); border-right-color: rgb(var(--ob-blue-rgb) / .42); border-radius: 50%; animation: wi-entry-spin .9s linear infinite; }
@keyframes wi-entry-spin { to { transform: rotate(360deg); } }
.work-items-badge { position: absolute; top: -.38rem; right: -.38rem; display: grid; place-items: center; min-width: 1rem; height: 1rem; padding: 0 .2rem; border: 2px solid var(--ob-chat-bg); border-radius: 5px; background: var(--ob-orange); color: #fff; font-size: 10px; font-weight: 600; line-height: 1; }

.wi-head { position: relative; display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-shrink: 0; padding: 0 var(--wi-inset); }
.wi-grip { position: absolute; top: -11px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 4px; background: var(--ob-chat-border); }
.wi-head h2 { margin: 0; color: var(--ob-text-strong); font-size: 18px; font-weight: 650; line-height: 1.5; }
.wi-head p { margin: .3rem 0 0; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.6; }
.wi-icon-action { display: inline-grid; place-items: center; flex: none; width: 2.1rem; height: 2.1rem; padding: 0; border: 1px solid var(--ob-border); border-radius: .5rem; background: transparent; color: var(--ob-text-subtle); cursor: pointer; }
.wi-icon-action:hover { background: var(--ob-surface-soft); color: var(--ob-text-strong); }
.wi-icon-action svg { width: 15px; height: 15px; }
.wi-browse, .wi-detail { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.wi-scroll { flex: 1; min-height: 0; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; padding: 0 var(--wi-inset) max(1rem, env(safe-area-inset-bottom)); }
.wi-focus-box { flex: none; margin: 1rem var(--wi-inset) 0; padding: .65rem .8rem; border-radius: .65rem; background: var(--ob-surface-soft); font-size: 13px; line-height: 1.6; color: var(--ob-text); }
.wi-focus-cap { display: flex; align-items: center; gap: 6px; color: var(--ob-text-subtle); font-size: 12px; }
.wi-focus-cap svg { width: 14px; height: 14px; }
.wi-run { margin-left: auto; display: inline-flex; align-items: center; gap: 5px; color: var(--ob-success); }
.wi-focus-box strong { display: block; margin-top: 2px; font-weight: 600; color: var(--ob-text-strong); overflow-wrap: anywhere; }
.wi-focus-sub { font-size: 12px; color: var(--ob-text-subtle); }
.wi-focus-cur { display: flex; align-items: center; gap: 6px; width: 100%; margin-top: .35rem; padding: .15rem 0; border: 0; background: transparent; color: var(--ob-text); font-size: 12.5px; text-align: left; cursor: pointer; }
.wi-focus-cur .wi-cur-num { flex: none; color: var(--ob-success); font-size: 12px; }
.wi-focus-cur .wi-cur-tx { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; color: var(--ob-success); }
.wi-focus-cur svg { width: 12px; height: 12px; color: var(--ob-text-muted); }
.wi-cur-more { flex: none; color: var(--ob-success); font-size: 12px; white-space: nowrap; }
.wi-now { flex: none; display: inline-flex; align-items: center; gap: 5px; font-size: 12px; line-height: 20px; color: var(--ob-success); white-space: nowrap; }
.wi-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: wi-pulse 1.4s ease-in-out infinite; }
.wi-link { display: inline-flex; align-items: center; gap: 2px; border: 0; background: transparent; padding: 0; color: var(--ob-blue); font-size: 12px; cursor: pointer; }
.wi-link svg { width: 12px; height: 12px; }
.wi-ltools { display: flex; align-items: center; justify-content: space-between; gap: .5rem; flex: none; padding: .85rem var(--wi-inset) .6rem; }
.wi-seg { display: inline-flex; padding: 2px; border-radius: 8px; background: var(--ob-surface-soft); }
.wi-seg button { min-height: 28px; padding: 0 12px; border: 0; border-radius: 6px; background: transparent; color: var(--ob-text-subtle); font-size: 12px; cursor: pointer; }
.wi-seg button[aria-pressed="true"] { background: var(--ob-surface); color: var(--ob-text-strong); box-shadow: var(--ob-shadow-panel); }
.wi-attn-sum { font-size: 12px; color: var(--ob-orange); }
.wi-empty { display: flex; min-height: 13rem; flex-direction: column; justify-content: center; align-items: center; padding: 1rem; text-align: center; color: var(--ob-text-subtle); }
.wi-empty > svg { width: 30px; height: 30px; margin-bottom: .85rem; opacity: .55; }
.wi-empty strong { color: var(--ob-text-strong); font-size: 14px; font-weight: 550; }
.wi-empty p { max-width: 24rem; font-size: 13px; line-height: 1.75; margin: .5rem 0; }
.wi-card { margin-bottom: .6rem; border: 1px solid var(--ob-border); border-radius: .65rem; background: var(--ob-surface); }
.wi-card:hover { border-color: var(--ob-border-strong); }
.wi-card-main { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .2rem .55rem; width: 100%; padding: .85rem .9rem .75rem .6rem; text-align: left; cursor: pointer; box-sizing: border-box; }
.wi-card-main:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: -2px; border-radius: .65rem; }
.wi-card .wi-tog { margin-top: 1px; }
.wi-tog { display: grid; place-items: center; width: 22px; height: 22px; padding: 0; border: 0; background: transparent; color: var(--ob-text-muted); cursor: pointer; }
.wi-tog:hover { color: var(--ob-text-strong); }
.wi-tog svg { width: 13px; height: 13px; transition: transform .15s; }
.wi-tog.open svg { transform: rotate(90deg); }
.wi-pad { width: 22px; }
.wi-ttl { display: flex; align-items: flex-start; gap: .5rem; min-width: 0; }
.wi-ttl strong { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; flex: 1; min-width: 0; color: var(--ob-text-strong); font-size: 14px; font-weight: 600; line-height: 1.5; }
.wi-card.is-done .wi-ttl strong { color: var(--ob-text-muted); font-weight: 500; }
.wi-num { flex: none; color: var(--ob-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; line-height: 21px; }
.wi-meta { grid-column: 2; display: flex; flex-wrap: wrap; align-items: center; gap: .35rem .55rem; font-size: 12px; color: var(--ob-text-subtle); }
.wi-s-in_progress, .wi-s-decided { color: var(--ob-text-strong); }
.wi-warn { color: var(--ob-orange); }
.wi-danger { color: var(--ob-danger); }
.wi-bar { flex: none; width: 84px; height: 4px; border-radius: 2px; background: var(--ob-surface-soft); overflow: hidden; }
.wi-bar i { display: block; height: 100%; background: var(--ob-text-subtle); border-radius: 2px; }
.wi-tree { padding: 0 .4rem .45rem; }
.wi-section-cap { display: flex; align-items: center; gap: .4rem; margin: .9rem 0 .5rem; padding: 0; border: 0; background: transparent; color: var(--ob-text-subtle); font-size: 12px; cursor: pointer; }
.wi-section-cap svg { width: 12px; height: 12px; }
.wi-outline { flex: none; margin: .35rem var(--wi-inset) 0; padding-bottom: max(1rem, env(safe-area-inset-bottom)); border-top: 1px solid var(--ob-border); }
.wi-outline-toggle { display: flex; align-items: center; gap: .6rem; width: 100%; padding: .8rem 0 .1rem; border: 0; background: transparent; color: var(--ob-text-subtle); font-size: 12px; text-align: left; cursor: pointer; }
.wi-outline-toggle small { margin-left: auto; font-size: 11px; font-variant-numeric: tabular-nums; }
.wi-outline-toggle svg { width: 12px; height: 12px; transition: transform .15s; }
.wi-outline-toggle.open svg { transform: rotate(90deg); }
.wi-outline pre { max-height: 18dvh; margin: .5rem 0 0; overflow: auto; padding: .7rem; border-radius: .5rem; background: var(--ob-surface-soft); color: var(--ob-text-subtle); font-size: 11px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }

.wi-dtool { display: flex; align-items: center; gap: .5rem; flex: none; padding: .9rem var(--wi-inset) .5rem; min-width: 0; }
.wi-back { display: inline-flex; align-items: center; gap: .3rem; flex: none; border: 0; background: transparent; padding: .3rem 0; color: var(--ob-text-subtle); font-size: 13px; cursor: pointer; }
.wi-back svg { width: 15px; height: 15px; }
.wi-crumb { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--ob-text-muted); }
.wi-dtitle { margin: .3rem 0 0; color: var(--ob-text-strong); font-size: 18px; font-weight: 600; line-height: 1.55; overflow-wrap: anywhere; }
.wi-dmeta { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem .6rem; margin: .6rem 0 0; font-size: 12px; color: var(--ob-text-subtle); }
.wi-dmeta .wi-num { line-height: 20px; }
.wi-chip { display: inline-flex; align-items: center; gap: 2px; padding: 0 .45rem 0 .2rem; border-radius: .3rem; background: var(--ob-surface-soft); font-size: 11px; line-height: 20px; color: var(--ob-text-subtle); white-space: nowrap; }
.wi-chip :deep(.wi-status-icon) { width: 16px; height: 16px; }
.wi-chip-ask { background: var(--ob-orange-soft); color: var(--ob-orange); }
.wi-chip-blocked { background: var(--ob-danger-soft); color: var(--ob-danger); }
.wi-chip.is-now { gap: 5px; padding: 0 .45rem; background: var(--ob-success-soft); color: var(--ob-success); }
.wi-tag { flex: none; font-size: 11px; line-height: 20px; color: var(--ob-text-subtle); font-style: normal; }
.wi-acts { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0 .2rem; }
.wi-btn { display: inline-flex; align-items: center; justify-content: center; gap: .35rem; height: 34px; padding: 0 .85rem; border: 1px solid var(--ob-border-strong); border-radius: 8px; background: var(--ob-surface); color: var(--ob-text); font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; white-space: nowrap; }
.wi-btn svg { width: 14px; height: 14px; }
.wi-btn:hover:not(:disabled) { background: var(--ob-surface-soft); }
.wi-btn.primary { border-color: transparent; background: var(--ob-chat-button); color: var(--ob-chat-button-text); }
.wi-btn.primary:hover:not(:disabled) { background: var(--ob-chat-button); opacity: .9; }
.wi-btn:disabled { opacity: .45; cursor: not-allowed; }
.wi-qbox { margin-top: 1rem; padding: .7rem .85rem; border-radius: .6rem; background: var(--ob-orange-soft); font-size: 13px; line-height: 1.7; }
.wi-qbox.is-blocked { background: var(--ob-danger-soft); }
.wi-qh { display: flex; align-items: center; gap: 3px; color: var(--ob-orange); font-size: 12px; }
.wi-qbox.is-blocked .wi-qh { color: var(--ob-danger); }
.wi-qbox p { margin: .25rem 0 0; color: var(--ob-text); overflow-wrap: anywhere; }
.wi-qbox small { display: block; margin-top: .35rem; color: var(--ob-text-subtle); font-size: 12px; }
.wi-notice { margin: .75rem 0; padding: .6rem .8rem; border-radius: .5rem; background: var(--ob-surface-soft); color: var(--ob-text-subtle); font-size: 13px; }
.wi-notice .wi-link { margin-left: .6rem; }
.wi-blk { margin-top: 1.15rem; }
.wi-blk h4 { margin: 0 0 .45rem; font-size: 12px; font-weight: 500; color: var(--ob-text-subtle); }
.wi-blk p { margin: 0; font-size: 14px; line-height: 1.8; color: var(--ob-text); overflow-wrap: anywhere; white-space: pre-wrap; }
.wi-blk ul { margin: 0; padding-left: 1.1rem; font-size: 14px; line-height: 1.8; color: var(--ob-text); }
.wi-scope { padding: .6rem .8rem; border-radius: .55rem; background: var(--ob-surface-soft); font-size: 13px; line-height: 1.7; color: var(--ob-text-subtle); overflow-wrap: anywhere; }
.wi-add { display: flex; gap: .5rem; margin-top: .4rem; }
.wi-add .el-input { flex: 1; min-width: 0; }
.wi-ev { display: grid; gap: .2rem; margin-top: .35rem; }
.wi-evi { display: flex; align-items: center; gap: .5rem; min-width: 0; width: calc(100% + .6rem); padding: .15rem .3rem; margin: 0 -.3rem; border: 0; background: transparent; color: var(--ob-text); font: inherit; font-size: 13px; text-align: left; box-sizing: border-box; }
.wi-evi.openable { cursor: pointer; }
.wi-evi.openable:hover { background: var(--ob-surface-soft); }
.wi-evi svg { width: 14px; height: 14px; flex: none; color: var(--ob-text-muted); }
.wi-evi code { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.wi-evi span:not(.wi-op) { min-width: 0; overflow-wrap: anywhere; }
.wi-op { margin-left: auto; flex: none; color: var(--ob-blue); font-size: 12px; }
.wi-server { margin-left: auto; flex: none; color: var(--ob-text-subtle); font-size: 12px; white-space: nowrap; }
.wi-placeholder { min-height: 8rem; }
.wi-tl { position: relative; margin: .2rem 0 0; padding: 0; list-style: none; }
.wi-tl > li { position: relative; padding: 0 0 1rem 1.5rem; }
.wi-tl > li::before { content: ""; position: absolute; left: 6px; top: 20px; bottom: 0; width: 1px; background: var(--ob-border-strong); }
.wi-tl > li:last-child::before { display: none; }
.wi-pin { position: absolute; left: 0; top: 5px; width: 13px; height: 13px; border-radius: 50%; border: 2px solid var(--ob-surface-raised); background: var(--ob-text-disabled); box-sizing: border-box; }
.wi-tl > li.by-you .wi-pin { background: var(--ob-text-strong); }
.wi-tlh { display: flex; flex-wrap: wrap; align-items: baseline; gap: .25rem .5rem; font-size: 12px; color: var(--ob-text-subtle); }
.wi-tlh b { color: var(--ob-text-strong); font-weight: 600; font-size: 12.5px; }
.wi-tlh .wi-link { margin-left: auto; }
.wi-tlb { margin-top: .2rem; font-size: 13.5px; line-height: 1.75; color: var(--ob-text); overflow-wrap: anywhere; white-space: pre-wrap; }
.wi-sub { color: var(--ob-text-subtle); font-size: 13px; }
.wi-quote { margin-top: .35rem; padding: .35rem .7rem; border-left: 2px solid var(--ob-border-strong); color: var(--ob-text-subtle); font-size: 13px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.wi-dec { margin-top: .45rem; padding: .6rem .75rem; border: 1px solid var(--ob-border); border-radius: .55rem; background: var(--ob-surface); }
.wi-dec + .wi-dec { margin-top: .4rem; }
.wi-q { font-size: 13px; font-weight: 500; color: var(--ob-text-strong); }
.wi-dec ul { list-style: none; margin: .35rem 0 0; padding: 0; display: grid; gap: .2rem; }
.wi-dec li { display: flex; align-items: flex-start; gap: .4rem; font-size: 13px; line-height: 1.6; color: var(--ob-text-subtle); }
.wi-dec li.on { color: var(--ob-text-strong); }
.wi-radio { flex: none; width: 12px; height: 12px; margin-top: 4px; border: 1.5px solid var(--ob-text-disabled); border-radius: 50%; box-sizing: border-box; }
.wi-dec li.on .wi-radio { border-color: var(--ob-text-strong); background: radial-gradient(circle, var(--ob-text-strong) 0 2.5px, transparent 3px); }
.wi-dec em { flex: none; font-style: normal; font-size: 11px; color: var(--ob-text-subtle); background: var(--ob-surface-soft); border-radius: .3rem; padding: 0 .35rem; line-height: 20px; }
.wi-ut { margin-top: .4rem; font-size: 12.5px; color: var(--ob-text-subtle); white-space: pre-wrap; }
.wi-chg { margin: .45rem 0 0; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .25rem .7rem; font-size: 13px; line-height: 1.65; }
.wi-chg dt { color: var(--ob-text-muted); font-size: 12px; line-height: 21px; }
.wi-chg dd { margin: 0; color: var(--ob-text); overflow-wrap: anywhere; white-space: pre-wrap; }
.wi-chg dd.old { color: var(--ob-text-subtle); text-decoration: line-through; text-decoration-color: var(--ob-text-disabled); }
.wi-tech { margin-top: 1.3rem; border-top: 1px solid var(--ob-border); font-size: 12px; color: var(--ob-text-subtle); }
.wi-tech summary { padding: .9rem 0 .5rem; cursor: pointer; }
.wi-tech dl { display: grid; grid-template-columns: 5.5rem minmax(0, 1fr); gap: .55rem 1rem; margin: 0; line-height: 1.7; }
.wi-tech dt, .wi-tech dd { margin: 0; overflow-wrap: anywhere; }
.wi-danger-link { border: 0; background: transparent; padding: 0; color: var(--ob-danger); font-size: 12px; cursor: pointer; }
.wi-dsub { margin: -.4rem 0 .8rem; font-size: 12px; color: var(--ob-text-subtle); overflow-wrap: anywhere; }
.wi-form { display: grid; gap: .45rem; }
.wi-form label { display: flex; justify-content: space-between; margin-top: .45rem; font-size: 13px; font-weight: 550; color: var(--ob-text); }
.wi-form label:first-child { margin-top: 0; }
.wi-form label span { font-weight: 400; font-size: 12px; color: var(--ob-text-subtle); }
button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
@keyframes wi-pulse { 50% { opacity: .35; } }
@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.wi-head { touch-action: none; }
	.wi-icon-action { width: 44px; height: 44px; border: 0; }
	.wi-btn { height: 40px; }
	.wi-tog { width: 32px; height: 32px; }
	.wi-pad { width: 32px; }
	.wi-back { min-height: 40px; }
	.wi-dtitle { font-size: 17px; }
	.wi-evi { min-height: 36px; }
}
@media (prefers-reduced-motion: reduce) { .wi-dot, .work-items-entry.is-working .work-items-entry-icon::before { animation: none; } .work-items-entry { transition: none; } }
</style>

<style>
.work-items-drawer.el-drawer { background: var(--ob-surface-raised); }
.work-items-drawer.el-drawer.rtl { height: 100dvh; max-height: 100dvh; }
.work-items-drawer .el-drawer__body { --wi-inset: 1.4rem; display: flex; flex-direction: column; min-height: 0; overflow: hidden; padding: 1.4rem 0 0; }
.work-items-dialog.el-dialog { padding: 1.2rem; border-radius: .85rem; background: var(--ob-surface-raised); }
.work-items-dialog .el-dialog__title { font-size: 16px; font-weight: 600; }
.work-items-dialog .el-textarea__inner, .work-items-dialog .el-input__inner { font-family: inherit; font-size: 14px; line-height: 1.7; }
.work-items-dialog .el-dialog__footer { padding-top: .85rem; border-top: 1px solid var(--ob-border); }
@media (max-width: 760px) {
	.work-items-drawer .el-drawer__body { --wi-inset: 1rem; padding-top: 1rem; }
	/* The shared phone drawer pads its body; here each region keeps the inset so the scrollbar meets the edge. */
	html .work-items-drawer.mobile-viewport-drawer > .el-drawer__body,
	html .work-items-drawer.mobile-viewport-sheet > .el-drawer__body {
		--wi-inset: max(16px, env(safe-area-inset-left, 0px), env(safe-area-inset-right, 0px));
		padding-left: 0; padding-right: 0; padding-bottom: 0;
	}
	/* Bottom sheet, as on the servers page. */
	.work-items-drawer.el-drawer.btt { border-radius: 16px 16px 0 0; transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
	html .work-items-drawer.el-drawer.btt > .el-drawer__body { padding-top: 18px; }
	.work-items-drawer.is-dragging { transition: none; user-select: none; }
	.work-items-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
	.work-items-overlay.el-drawer-fade-leave-from .work-items-drawer { transform: translateY(var(--wi-drag-y, 0px)); }
	.work-items-overlay.el-drawer-fade-leave-active .work-items-drawer { pointer-events: none; }
	.work-items-dialog.el-dialog { padding: 1rem; }
}
</style>
