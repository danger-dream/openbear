<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch} from "vue";
import {ElDialog} from "element-plus";
import ConsoleMarkdown from "../views/consoleView/ConsoleMarkdown.vue";
import {htmlPreviewDocument} from "./htmlPreview.js";
import {highlightCodeHtml} from "../views/consoleView/markdown.js";
import {artifactFromUrl, artifactRecord, artifactFormat, artifactErrorMessage, formatFileSize, loadArtifactMetadata, loadArtifactText, readingState, clearArtifactCache} from "./artifactFiles.js";

const props = defineProps({navigationKey: {type: String, default: ""}});
const opened = ref(false), busy = ref(false), error = ref("");
const selected = shallowRef(null), record = shallowRef(null), text = ref("");
const mode = ref("preview"), wrap = ref(true), maximized = ref(false), mobile = ref(false);
const body = ref(null), imageZoom = ref(null), imageWidth = ref(0), imageLoading = ref(false), imageFailed = ref(false), imageVersion = ref(0);
const format = computed(() => artifactFormat(record.value?.metadata));
const title = computed(() => record.value?.summary?.title || selected.value?.label || record.value?.metadata?.fileName || "附件预览");
const sourceHtml = computed(() => highlightCodeHtml(text.value, format.value.kind === "markdown" ? "markdown" : format.value.language));
const fullscreen = computed(() => mobile.value || maximized.value);
const hasPreview = computed(() => ["markdown", "html"].includes(format.value.kind));
const htmlDocument = computed(() => format.value.kind === "html" && mode.value === "preview" ? htmlPreviewDocument(text.value) : "");
let generation = 0, saved = null, opener = null, media = null;

function savePosition() {
	if (opened.value && saved && body.value && !busy.value) saved[mode.value === "preview" ? "previewTop" : "sourceTop"] = body.value.scrollTop;
	if (saved) { saved.mode = mode.value; saved.wrap = wrap.value; }
}
async function restorePosition() {
	const current = generation;
	// Restore once after the content patch, not again when the dialog's opening
	// animation ends: the reader may already have scrolled by then.
	await nextTick();
	if (opened.value && current === generation && body.value && saved) body.value.scrollTop = saved[mode.value === "preview" ? "previewTop" : "sourceTop"] || 0;
}
async function loadSelected(force = false) {
	const current = ++generation, active = record.value;
	busy.value = true; error.value = ""; text.value = ""; saved = null;
	imageZoom.value = null; imageWidth.value = 0; imageLoading.value = true; imageFailed.value = false; imageVersion.value++;
	try {
		const meta = await loadArtifactMetadata(active, {force});
		if (current !== generation || !opened.value) return;
		const kind = artifactFormat(meta).kind;
		// HTML execution is opt-in on first open; remember the user's mode per file/version.
		saved = readingState(`${active.identity.key}:${meta.sha256 || ""}`, kind === "html" ? "source" : "preview");
		mode.value = ["markdown", "html"].includes(kind) ? saved.mode : "source";
		wrap.value = saved.wrap;
		if (["markdown", "html", "text", "code"].includes(kind)) {
			const content = await loadArtifactText(active);
			if (current !== generation || !opened.value) return;
			text.value = content;
		}
	} catch (failure) {
		if (current === generation && opened.value) error.value = artifactErrorMessage(failure);
	} finally {
		if (current === generation && opened.value) { busy.value = false; void restorePosition(); }
	}
}
function show(event) {
	const identity = artifactFromUrl(event.detail?.href);
	if (!identity) return;
	savePosition();
	if (!opened.value) { opener = event.detail?.opener instanceof HTMLElement ? event.detail.opener : document.activeElement; maximized.value = false; }
	selected.value = {identity, label: String(event.detail?.label || "")};
	record.value = artifactRecord(identity);
	opened.value = true;
	void loadSelected();
}
function close() { savePosition(); generation++; opened.value = false; }
function restoreFocus() {
	// ElDialog emits this notification without a DOM event. Restore after its
	// own focus-trap cleanup, and never steal focus after navigation/reopening.
	queueMicrotask(() => { if (!opened.value && opener?.isConnected) opener.focus({preventScroll: true}); });
}
function switchMode(next) { if (mode.value === next) return; savePosition(); mode.value = next; void restorePosition(); }
function zoom(delta) { const current = imageZoom.value ?? Math.min(1, (body.value?.clientWidth || 800) / (imageWidth.value || 800)); imageZoom.value = Math.max(.1, Math.min(4, Math.round((current + delta) * 10) / 10)); }
function imageLoaded(event) { imageWidth.value = event.target.naturalWidth; imageLoading.value = false; }
async function onContentClick(event) {
	const button = event.target.closest?.(".md-code-copy");
	if (!button) return;
	event.preventDefault(); event.stopPropagation();
	const source = button.closest(".md-code-block")?.querySelector("code")?.textContent;
	if (source == null) return;
	try { await navigator.clipboard.writeText(source); button.textContent = "已复制"; setTimeout(() => { if (button.isConnected) button.textContent = "复制"; }, 1200); }
	catch { button.textContent = "复制失败"; }
}
function mediaChanged() { mobile.value = Boolean(media?.matches); }
watch(() => props.navigationKey, close);
onMounted(() => { window.addEventListener("openbear:preview-artifact", show); media = window.matchMedia("(max-width: 760px)"); mediaChanged(); media.addEventListener("change", mediaChanged); });
onBeforeUnmount(() => { savePosition(); generation++; window.removeEventListener("openbear:preview-artifact", show); media?.removeEventListener("change", mediaChanged); clearArtifactCache(); });
</script>

<template>
	<ElDialog :model-value="opened" class="artifact-preview-dialog" width="min(1120px, calc(100vw - 48px))" top="5vh" :fullscreen="fullscreen" append-to-body destroy-on-close :show-close="false" :close-on-click-modal="false" @update:model-value="value => { if (!value) close(); }" @close-auto-focus="restoreFocus">
		<template #header="{titleId}">
			<div class="artifact-preview-header">
				<span class="artifact-preview-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10zM13 3v7h7M8 14h8M8 17h5"/></svg></span>
				<div class="artifact-preview-heading"><h2 :id="titleId">{{ title }}</h2><p>{{ record?.metadata?.fileName || '附件' }}<template v-if="record?.metadata"> · {{ formatFileSize(record.metadata.sizeBytes) }}</template></p></div>
				<div class="artifact-preview-actions">
					<a v-if="selected" class="artifact-preview-action" :href="selected.identity.downloadUrl" download aria-label="下载原文件" title="下载原文件"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/></svg></a>
					<button v-if="!mobile" type="button" class="artifact-preview-action" :aria-label="maximized ? '还原窗口' : '最大化预览'" :title="maximized ? '还原窗口' : '最大化预览'" @click="maximized = !maximized"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path v-if="maximized" d="M8 8V4h12v12h-4M4 8h12v12H4z"/><path v-else d="M4 9V4h5m6 0h5v5m0 6v5h-5M9 20H4v-5"/></svg></button>
					<button type="button" class="artifact-preview-action" aria-label="关闭附件预览" title="关闭预览（Esc）" @click="close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>
				</div>
			</div>
		</template>
		<div class="artifact-preview-toolbar">
			<div v-if="hasPreview && !busy && !error" class="artifact-preview-tabs" role="group" aria-label="文档查看方式"><button type="button" :class="{active: mode === 'preview'}" :aria-pressed="mode === 'preview'" @click="switchMode('preview')">{{ format.kind === 'html' ? '页面预览' : '文档预览' }}</button><button type="button" :class="{active: mode === 'source'}" :aria-pressed="mode === 'source'" @click="switchMode('source')">源码</button></div>
			<span v-else class="artifact-preview-type">{{ busy ? '正在读取附件' : format.label }}</span>
			<span v-if="format.sourceOnly" class="artifact-preview-source-note">仅显示源码，不执行内容</span>
			<label v-if="['text', 'code'].includes(format.kind) || hasPreview && mode === 'source'" class="artifact-preview-wrap"><input v-model="wrap" type="checkbox" @change="savePosition">自动换行</label>
			<div v-if="format.kind === 'image' && !busy" class="artifact-preview-zoom"><button type="button" aria-label="缩小图片" :disabled="imageLoading || imageFailed" @click="zoom(-.2)">−</button><button type="button" :disabled="imageLoading || imageFailed" @click="imageZoom = null">{{ imageZoom == null ? '适应窗口' : `${Math.round(imageZoom * 100)}%` }}</button><button type="button" aria-label="放大图片" :disabled="imageLoading || imageFailed" @click="zoom(.2)">＋</button></div>
		</div>
		<div ref="body" class="artifact-preview-body" :class="{'is-image': format.kind === 'image', 'is-html': format.kind === 'html' && mode === 'preview' && !busy && !error}" :aria-busy="busy" @scroll="savePosition" @click="onContentClick">
			<div v-if="busy" class="artifact-preview-empty" role="status"><span class="artifact-preview-spinner" aria-hidden="true"></span><strong>正在打开附件</strong><p>内容将在这里呈现，不会离开当前对话。</p></div>
			<div v-else-if="error" class="artifact-preview-empty" role="alert"><strong>暂时无法预览</strong><p>{{ error }}</p><button type="button" @click="loadSelected(true)">重新读取</button></div>
			<template v-else-if="format.kind === 'image'">
				<div v-if="imageFailed" class="artifact-preview-empty" role="alert"><strong>图片未能加载</strong><p>可以重新读取，或下载原文件查看。</p><button type="button" @click="loadSelected(true)">重新读取</button></div>
				<div v-else class="artifact-preview-image-stage" :class="{'is-fit': imageZoom == null}"><span v-if="imageLoading" class="artifact-preview-image-status" role="status">正在加载图片…</span><img :key="imageVersion" :src="selected.identity.contentUrl" :alt="record?.metadata?.fileName || title" :style="imageZoom == null ? {} : {width: `${Math.round(imageWidth * imageZoom)}px`}" @load="imageLoaded" @error="imageFailed = true; imageLoading = false"></div>
			</template>
			<div v-else-if="format.kind === 'unsupported'" class="artifact-preview-empty"><span class="artifact-preview-file-type">{{ format.label }}</span><strong>这个文件暂不支持站内预览</strong><p>原文件已保留，可以直接下载查看。</p><a :href="selected.identity.downloadUrl" download>下载原文件</a></div>
			<div v-else-if="text.length === 0" class="artifact-preview-empty"><strong>这是一个空文件</strong><p>仍可通过右上角下载原文件。</p></div>
			<template v-else-if="format.kind === 'html' && mode === 'preview'">
				<iframe v-if="opened" class="artifact-preview-html" :title="`HTML 页面预览：${title}`" :srcdoc="htmlDocument" sandbox="allow-scripts" referrerpolicy="no-referrer" allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"></iframe>
			</template>
			<article v-else-if="format.kind === 'markdown' && mode === 'preview'" class="artifact-preview-document"><ConsoleMarkdown :text="text" :artifact-cards="false"/></article>
			<div v-else class="artifact-preview-source"><p v-if="text.length > 8000" class="artifact-preview-large-note">较大文本以纯文本显示，原始内容保持完整。</p><pre :class="{'is-wrapped': wrap}"><code class="hljs" v-html="sourceHtml"></code></pre></div>
		</div>
		<div class="artifact-preview-footer"><span>对话附件<span v-if="!mobile"> · 只读预览</span></span><span>{{ format.kind === 'html' && mode === 'preview' && !error && !busy ? '隔离预览 · 外部资源与接口访问受限' : format.kind === 'markdown' && !error && !busy ? (mode === 'preview' ? 'Markdown 已渲染' : '显示原始 Markdown') : '下载始终保留原文件' }}</span></div>
	</ElDialog>
</template>

<style>
.artifact-preview-dialog.el-dialog { --el-dialog-padding-primary: 0; display: flex; flex-direction: column; height: 88dvh; padding: 0; margin-bottom: 0; overflow: hidden; border: 1px solid var(--ob-border); border-radius: 18px; background: var(--ob-surface-raised); box-shadow: var(--ob-shadow-dialog); }
.artifact-preview-dialog.el-dialog.is-fullscreen { height: 100dvh; margin: 0; border-radius: 0; }
.artifact-preview-dialog .el-dialog__header { flex: none; padding: 0; margin: 0; }
.artifact-preview-dialog .el-dialog__body { display: flex; flex: 1; min-height: 0; flex-direction: column; padding: 0; color: var(--ob-text); }
.artifact-preview-header { display: flex; align-items: center; gap: 12px; min-height: 82px; padding: 17px 23px; border-bottom: 1px solid var(--ob-border-soft); }
.artifact-preview-mark { display: grid; place-items: center; flex: none; width: 38px; height: 43px; border-radius: 9px; background: var(--ob-surface-soft); color: var(--ob-blue); }
.artifact-preview-mark svg { width: 23px; height: 23px; }
.artifact-preview-heading { flex: 1; min-width: 0; }
.artifact-preview-heading h2 { overflow: hidden; margin: 0; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; line-height: 1.6; font-weight: 660; color: var(--ob-text-strong); }
.artifact-preview-heading p { overflow: hidden; margin: 3px 0 0; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; line-height: 1.5; color: var(--ob-text-subtle); }
.artifact-preview-actions { display: flex; flex: none; align-items: center; gap: 4px; }
.artifact-preview-action { display: grid; place-items: center; width: 34px; height: 34px; border: 0; border-radius: 8px; background: transparent; color: var(--ob-text-subtle); cursor: pointer; }
.artifact-preview-action svg { width: 18px; height: 18px; }
.artifact-preview-action:hover { background: var(--ob-surface-soft); color: var(--ob-text-strong); }
.artifact-preview-toolbar { display: flex; flex: none; align-items: center; gap: 12px; min-height: 49px; padding: 8px 24px; border-bottom: 1px solid var(--ob-border-soft); background: var(--ob-surface); }
.artifact-preview-tabs { display: flex; padding: 3px; gap: 2px; border-radius: 8px; background: var(--ob-surface-soft); }
.artifact-preview-tabs button { padding: 5px 12px; border: 0; border-radius: 5px; background: transparent; color: var(--ob-text-subtle); font-size: 11px; line-height: 1.4; cursor: pointer; }
.artifact-preview-tabs button.active { color: var(--ob-text-strong); background: var(--ob-surface-raised); box-shadow: 0 1px 3px rgb(0 0 0 / 7%); }
.artifact-preview-type { color: var(--ob-text-subtle); font-size: 11px; font-weight: 600; letter-spacing: .035em; }
.artifact-preview-source-note { color: var(--ob-text-muted); font-size: 11px; }
.artifact-preview-wrap { display: flex; align-items: center; gap: 6px; margin-left: auto; color: var(--ob-text-subtle); font-size: 11px; cursor: pointer; }
.artifact-preview-wrap input { accent-color: var(--ob-text); }
.artifact-preview-zoom { display: flex; align-items: center; gap: 4px; margin-left: auto; }
.artifact-preview-zoom button { min-width: 29px; height: 29px; padding: 0 8px; border: 1px solid var(--ob-border-soft); border-radius: 6px; color: var(--ob-text); background: var(--ob-surface); font-size: 11px; cursor: pointer; }
.artifact-preview-zoom button:disabled { opacity: .4; cursor: default; }
.artifact-preview-body { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; background: var(--ob-surface); scrollbar-gutter: stable; }
.artifact-preview-document { max-width: 880px; padding: 25px 38px 50px; margin: 0 auto; font-size: 14px; line-height: 1.85; overflow-wrap: anywhere; }
.artifact-preview-document .bear-md > :first-child { margin-top: 0; }
.artifact-preview-source { min-height: 100%; padding: 22px 28px 42px; }
.artifact-preview-source pre { margin: 0; padding: 0; border: 0; white-space: pre; tab-size: 4; font: 12px/1.85 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: var(--ob-text); }
.artifact-preview-source pre.is-wrapped { white-space: pre-wrap; overflow-wrap: anywhere; }
.artifact-preview-source code.hljs { padding: 0; background: transparent; font: inherit; }
.artifact-preview-large-note { margin: 0 0 12px; color: var(--ob-text-muted); font-size: 11px; }
.artifact-preview-empty { display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; min-height: 100%; padding: 45px 30px; text-align: center; }
.artifact-preview-empty strong { color: var(--ob-text-strong); font-size: 15px; font-weight: 580; }
.artifact-preview-empty p { max-width: 440px; margin: 0; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.8; }
.artifact-preview-empty > button, .artifact-preview-empty > a { margin-top: 5px; padding: 8px 17px; border: 1px solid var(--ob-border); border-radius: 8px; background: var(--ob-surface-soft); color: var(--ob-blue); text-decoration: none; font-size: 12px; cursor: pointer; }
.artifact-preview-file-type { padding: 8px 13px; border: 1px solid var(--ob-border); border-radius: 8px; font: 11px ui-monospace, monospace; letter-spacing: .06em; color: var(--ob-text-muted); }
.artifact-preview-spinner { width: 22px; height: 22px; border: 2px solid var(--ob-border-soft); border-top-color: var(--ob-blue); border-radius: 50%; animation: artifact-spin .8s linear infinite; }
.artifact-preview-body.is-image { background: var(--ob-bg); }
.artifact-preview-body.is-html { overflow: hidden; scrollbar-gutter: auto; }
.artifact-preview-html { display: block; width: 100%; height: 100%; border: 0; background: #fff; color-scheme: normal; }
.artifact-preview-image-stage { display: grid; place-items: center; position: relative; width: max-content; min-width: 100%; min-height: 100%; padding: 24px; box-sizing: border-box; }
.artifact-preview-image-stage img { display: block; max-width: none; max-height: none; object-fit: contain; box-shadow: 0 5px 25px rgb(0 0 0 / 8%); }
.artifact-preview-image-stage.is-fit { width: 100%; height: 100%; }
.artifact-preview-image-stage.is-fit img { width: auto; max-width: 100%; max-height: 100%; min-height: 0; }
.artifact-preview-image-status { position: absolute; padding: 7px 12px; border-radius: 6px; background: var(--ob-surface); color: var(--ob-text-subtle); font-size: 12px; }
.artifact-preview-footer { display: flex; flex: none; justify-content: space-between; gap: 12px; padding: 10px 24px; border-top: 1px solid var(--ob-border-soft); color: var(--ob-text-muted); font-size: 10px; line-height: 1.4; }
.artifact-preview-dialog button:focus-visible, .artifact-preview-dialog a:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
@keyframes artifact-spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .artifact-preview-header { min-height: 76px; padding: 14px 14px; gap: 9px; } .artifact-preview-mark { display: none; } .artifact-preview-heading h2 { font-size: 14px; } .artifact-preview-actions { gap: 0; } .artifact-preview-toolbar { padding: 8px 15px; gap: 8px; } .artifact-preview-document { padding: 20px 20px 35px; font-size: 13px; } .artifact-preview-source { padding: 18px 18px 35px; } .artifact-preview-footer { padding: 10px 16px max(10px, env(safe-area-inset-bottom)); } }
@media (prefers-reduced-motion: reduce) { .artifact-preview-spinner { animation: none; } }
</style>
