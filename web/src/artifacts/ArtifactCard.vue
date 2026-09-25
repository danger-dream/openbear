<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from "vue";
import {ElMessage} from "element-plus";
import {copyTextToClipboard} from "../utils/clipboard.js";
import {artifactFromUrl, artifactRecord, artifactFormat, artifactSharedPath, formatFileSize, loadArtifactCard, openArtifactPreview} from "./artifactFiles.js";

const props = defineProps({href: {type: String, required: true}, label: {type: String, default: ""}});
const identity = artifactFromUrl(props.href);
const record = identity ? artifactRecord(identity) : null;
const root = ref(null);
const format = computed(() => artifactFormat(record?.metadata));
const sharedPath = computed(() => artifactSharedPath(record?.metadata));
const title = computed(() => record?.summary?.title || props.label || record?.metadata?.fileName || "附件");
const excerpt = computed(() => {
	if (record?.metadataError) return "暂时无法读取附件信息，点击查看详情。";
	if (format.value.kind === "html") return "HTML 页面 · 可切换页面预览与源码";
	if (record?.summary?.excerpt) return record.summary.excerpt;
	if (!record?.metadata) return "点击即可在对话中查看附件";
	if (format.value.kind === "image") return "图片附件 · 在对话中查看与缩放";
	if (format.value.kind === "unsupported") return "此格式暂不支持站内预览，可下载原文件查看。";
	if (record.metadata.sizeBytes === 0) return "空文件";
	return record.metadata.fileName;
});
let observer;
function load() { observer?.disconnect(); if (record) void loadArtifactCard(record).catch(() => {}); }
function open(event) { if (identity) openArtifactPreview(identity, title.value, event.currentTarget); }
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
	<div ref="root" class="artifact-card" :class="{'is-unavailable': record?.metadataError, 'has-shared-path': sharedPath}" :data-artifact-id="identity?.artifactUuid">
		<button type="button" class="artifact-card-open" :aria-label="`预览附件：${title}`" @click="open">
			<span class="artifact-card-icon" aria-hidden="true">
				<svg v-if="format.kind === 'image'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3.5" y="3.5" width="17" height="17" rx="3"/><circle cx="9" cy="8.5" r="1.5"/><path d="m4 17 5-5 3 3 4-5 4 5"/></svg>
				<svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13.5 3.5H6A2 2 0 0 0 4 5.5v13a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8z"/><path d="M13 4v7h7M8 14h8M8 17h5"/></svg>
			</span>
			<span class="artifact-card-copy">
				<span class="artifact-card-title" :title="record?.metadata?.fileName || title">{{ title }}</span>
				<span class="artifact-card-excerpt">{{ excerpt }}</span>
				<span class="artifact-card-meta"><span>{{ record?.metadata ? format.label : '附件' }}</span><span aria-hidden="true">·</span><span>{{ record?.metadata ? formatFileSize(record.metadata.sizeBytes) : (record?.busy ? '读取信息中…' : '点击预览') }}</span></span>
			</span>
		</button>
		<button v-if="sharedPath" type="button" class="artifact-card-copy-path" :aria-label="`复制工作区路径：${sharedPath}`" title="复制工作区路径" @click.stop="copyPath">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>
		</button>
		<a class="artifact-card-download" :href="identity?.downloadUrl || href" download :aria-label="`下载原文件：${record?.metadata?.fileName || title}`" title="下载原文件" @click.stop>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/></svg>
		</a>
	</div>
</template>

<style>
.md-artifact-slot { margin: 12px 0; max-width: 620px; min-width: 0; }
.artifact-card { position: relative; width: 100%; overflow: hidden; border: 1px solid var(--ob-border-soft); border-radius: 14px; background: var(--ob-surface); color: var(--ob-text); box-shadow: 0 2px 5px rgb(20 30 50 / 3%); transition: border-color .15s, box-shadow .15s; }
.artifact-card:hover { border-color: var(--ob-blue); box-shadow: 0 4px 14px rgb(20 30 50 / 6%); }
.artifact-card-open { display: grid; grid-template-columns: 42px minmax(0, 1fr); align-items: start; gap: 13px; width: 100%; padding: 16px 53px 16px 16px; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.artifact-card-icon { display: grid; place-items: center; width: 42px; height: 48px; border: 1px solid var(--ob-border-soft); border-radius: 10px; color: var(--ob-blue); background: var(--ob-surface-soft); }
.artifact-card-icon svg { width: 25px; height: 25px; }
.artifact-card-copy { display: flex; min-width: 0; flex-direction: column; gap: 6px; }
.artifact-card-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ob-text-strong); font-size: 14px; font-weight: 650; line-height: 1.6; }
.artifact-card-excerpt { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; height: 37px; color: var(--ob-text-subtle); font-size: 12px; line-height: 18.5px; overflow-wrap: anywhere; }
.artifact-card-meta { display: flex; align-items: center; gap: 7px; padding-top: 1px; font-size: 10.5px; font-weight: 500; line-height: 16px; color: var(--ob-text-muted); }
.artifact-card.has-shared-path .artifact-card-open { padding-right: 90px; }
.artifact-card-copy-path, .artifact-card-download { position: absolute; top: 12px; display: grid; place-items: center; width: 32px; height: 32px; border: 0; border-radius: 8px; background: transparent; color: var(--ob-text-subtle); text-decoration: none !important; cursor: pointer; }
.artifact-card-copy-path { right: 47px; }
.artifact-card-download { right: 11px; }
.artifact-card-copy-path:hover, .artifact-card-download:hover { background: var(--ob-surface-soft); color: var(--ob-blue); }
.artifact-card-copy-path svg, .artifact-card-download svg { width: 18px; height: 18px; }
.artifact-card-open:focus-visible, .artifact-card-copy-path:focus-visible, .artifact-card-download:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: -3px; }
.artifact-card.is-unavailable .artifact-card-excerpt { color: var(--ob-text-muted); }
@media (max-width: 600px) { .artifact-card-open { padding-left: 12px; gap: 10px; grid-template-columns: 34px minmax(0, 1fr); } .artifact-card-icon { width: 34px; height: 42px; } .artifact-card-title { font-size: 13px; } }
@media (prefers-reduced-motion: reduce) { .artifact-card { transition: none; } }
</style>
