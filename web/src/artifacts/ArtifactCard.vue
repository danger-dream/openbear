<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from "vue";
import {ElMessage} from "element-plus";
import {Globe, FileText, FileCode, FileArchive, FileSpreadsheet, Presentation, FileAudio, FileVideo, File, Braces, Type, ArrowUpRight, Copy, Download} from "@lucide/vue";
import {artifactPresentation} from "./artifactPresentation.js";
import {onArtifactDownload} from "./artifactDownload.js";
import {copyTextToClipboard} from "../utils/clipboard.js";
import {artifactFromUrl, artifactRecord, artifactSharedPath, formatFileSize, loadArtifactCard, openArtifactPreview} from "./artifactFiles.js";

const props = defineProps({href: {type: String, required: true}, label: {type: String, default: ""}});
const identity = artifactFromUrl(props.href);
const record = identity ? artifactRecord(identity) : null;
const root = ref(null);
const presentation = computed(() => artifactPresentation(record?.metadata));
const icons = {html: Globe, markdown: FileText, pdf: FileText, document: FileText, spreadsheet: FileSpreadsheet, presentation: Presentation, archive: FileArchive, audio: FileAudio, video: FileVideo, font: Type, code: FileCode, data: Braces, text: FileText, file: File};
const sharedPath = computed(() => artifactSharedPath(record?.metadata));
const title = computed(() => record?.summary?.title || props.label || record?.metadata?.fileName || "附件");
const excerpt = computed(() => {
	if (record?.metadataError) return "暂时无法读取附件信息，点击查看详情。";
	if (record?.metadata?.sizeBytes === 0) return "空文件";
	return record?.summary?.excerpt || "";
});
let observer;
function load() { observer?.disconnect(); if (record) void loadArtifactCard(record).catch(() => {}); }
function open(event) { if (identity) openArtifactPreview(identity, title.value, event.currentTarget); }
const openLabel = computed(() => `${record?.metadata && !presentation.value.previewable ? '查看附件详情' : '预览附件'}：${title.value}`);
async function copyPath() {
	if (!sharedPath.value) return;
	try { await copyTextToClipboard(sharedPath.value); ElMessage.success("工作区路径已复制"); }
	catch { ElMessage.error("复制失败，请手动选择路径"); }
}
onMounted(() => {
	if (typeof IntersectionObserver === "undefined") return load();
	observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) load(); }, {rootMargin: "160px"});
	observer.observe(root.value);
});
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
	<!-- Classify from shared metadata, including MIME-only extensionless images.
	     The stable Markdown slot stays mounted during streaming; images never
	     acquire a second thumbnail, even when metadata arrives asynchronously. -->
	<span v-if="presentation.category === 'image'" ref="root" class="artifact-image-link" :data-artifact-id="identity?.artifactUuid">
		<a class="artifact-image-open" :href="identity?.contentUrl || href" :title="record?.metadata?.fileName || title" :aria-label="`查看图片：${title}`" @click.stop.prevent="open">{{ title }}</a>
		<a class="artifact-image-download" :href="identity?.downloadUrl || href" download title="下载原图" :aria-label="`下载原图：${record?.metadata?.fileName || title}`" @click.stop="onArtifactDownload($event, identity)">下载</a>
		<button v-if="sharedPath" type="button" :aria-label="`复制工作区路径：${sharedPath}`" title="复制工作区路径" @click.stop="copyPath">复制路径</button>
	</span>
	<div v-else ref="root" class="artifact-card" :class="[`artifact-card--${presentation.category}`, {'is-unavailable': record?.metadataError, 'has-shared-path': sharedPath}]" :data-artifact-id="identity?.artifactUuid">
		<button type="button" class="artifact-card-open" :aria-label="openLabel" @click.stop="open">
			<span class="artifact-card-icon" aria-hidden="true">
				<component :is="icons[presentation.category]" :size="23" :stroke-width="1.6"/>
				<span class="artifact-card-extension">{{ presentation.badge }}</span>
			</span>
			<span class="artifact-card-copy">
				<span class="artifact-card-title" :title="record?.metadata?.fileName || title">{{ title }}</span>
				<span v-if="record?.metadata?.fileName && record.metadata.fileName !== title" class="artifact-card-filename" :title="record.metadata.fileName">{{ record.metadata.fileName }}</span>
				<span v-if="excerpt" class="artifact-card-excerpt" :class="{'is-code': ['code', 'data'].includes(presentation.category)}">{{ excerpt }}</span>
				<span class="artifact-card-meta"><span class="artifact-card-type">{{ presentation.label }}</span><span aria-hidden="true">·</span><span>{{ record?.metadata ? formatFileSize(record.metadata.sizeBytes) : (record?.busy ? '读取信息中…' : '等待读取') }}</span><span v-if="record?.metadata" class="artifact-card-intent">{{ presentation.action }}<ArrowUpRight v-if="presentation.previewable" :size="12" aria-hidden="true"/></span></span>
			</span>
		</button>
		<div class="artifact-card-actions">
			<button v-if="sharedPath" type="button" class="artifact-card-copy-path" :aria-label="`复制工作区路径：${sharedPath}`" title="复制工作区路径" @click.stop="copyPath"><Copy :size="16" :stroke-width="1.7" aria-hidden="true"/></button>
			<a class="artifact-card-download" :href="identity?.downloadUrl || href" download :aria-label="`下载原文件：${record?.metadata?.fileName || title}`" title="下载原文件" @click.stop="onArtifactDownload($event, identity)"><Download :size="17" :stroke-width="1.7" aria-hidden="true"/></a>
		</div>
	</div>
</template>

<style>
.artifact-image-link { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 12px; overflow-wrap: anywhere; }
.artifact-image-link .artifact-image-open { min-width: 0; }
.artifact-image-link .artifact-image-download, .artifact-image-link button { padding: 5px 8px; border: 1px solid var(--ob-border-soft); border-radius: 7px; background: var(--ob-surface); color: var(--ob-text-subtle); text-decoration: none; font: inherit; cursor: pointer; }
.artifact-image-link .artifact-image-download:hover, .artifact-image-link button:hover { color: var(--ob-blue); background: var(--ob-surface-soft); }
.artifact-image-link a:focus-visible, .artifact-image-link button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
.md-artifact-slot { margin: 10px 0; max-width: 620px; min-width: 0; }
.artifact-card { --artifact-accent: var(--ob-text-subtle); --artifact-tint: var(--ob-surface-soft); position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; width: 100%; overflow: hidden; border: 1px solid var(--ob-border); border-radius: 12px; background: var(--ob-surface); color: var(--ob-text); transition: border-color .15s; }
.artifact-card--html, .artifact-card--document { --artifact-accent: var(--ob-blue); --artifact-tint: var(--ob-blue-soft); }
.artifact-card--pdf { --artifact-accent: var(--ob-danger); --artifact-tint: var(--ob-danger-soft); }
.artifact-card--spreadsheet, .artifact-card--data { --artifact-accent: var(--ob-success); --artifact-tint: var(--ob-success-soft); }
.artifact-card--presentation { --artifact-accent: var(--ob-orange); --artifact-tint: var(--ob-orange-soft); }
.artifact-card--archive { --artifact-accent: var(--ob-warning); --artifact-tint: var(--ob-warning-soft); }
.artifact-card--audio, .artifact-card--video { --artifact-accent: var(--ob-violet); --artifact-tint: var(--ob-violet-soft); }
.artifact-card:hover { border-color: var(--artifact-accent); }
.artifact-card-open { display: grid; grid-template-columns: 46px minmax(0, 1fr); align-items: start; gap: 12px; min-width: 0; width: 100%; padding: 14px; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.artifact-card-icon { display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 5px; width: 46px; min-height: 56px; padding: 7px 2px; border-radius: 8px; color: var(--artifact-accent); background: var(--artifact-tint); }
.artifact-card-extension { max-width: 100%; font: 600 9px/1.1 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.artifact-card-copy { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.artifact-card-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-text-strong); font-size: 14px; font-weight: 600; line-height: 1.6; }
.artifact-card-filename { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-text-subtle); font-size: 11px; line-height: 1.5; }
.artifact-card-excerpt { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin: 3px 0; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.artifact-card-excerpt.is-code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; }
.artifact-card-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 3px 7px; margin-top: 3px; color: var(--ob-text-muted); font-size: 11px; line-height: 1.5; }
.artifact-card-type { color: var(--ob-text-subtle); }
.artifact-card-intent { display: inline-flex; align-items: center; gap: 3px; margin-left: auto; color: var(--artifact-accent); }
.artifact-card-actions { display: flex; align-self: start; gap: 2px; padding: 12px 10px 0 0; }
.artifact-card-copy-path, .artifact-card-download { display: grid; place-items: center; flex: none; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 7px; background: transparent; color: var(--ob-text-subtle); text-decoration: none !important; cursor: pointer; }
.artifact-card-copy-path:hover, .artifact-card-download:hover { background: var(--ob-surface-soft); color: var(--ob-text-strong); }
.artifact-card-open:focus-visible, .artifact-card-copy-path:focus-visible, .artifact-card-download:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: -3px; }
.artifact-card.is-unavailable .artifact-card-excerpt { color: var(--ob-text-muted); }
@media (max-width: 600px) { .artifact-card-open { padding: 12px 10px; gap: 9px; grid-template-columns: 40px minmax(0, 1fr); } .artifact-card-icon { width: 40px; min-height: 52px; } .artifact-card-title { font-size: 13px; } .artifact-card-actions { padding: 10px 6px 0 0; gap: 0; } .artifact-card-intent { margin-left: 0; } }
@media (prefers-reduced-motion: reduce) { .artifact-card { transition: none; } }
</style>
