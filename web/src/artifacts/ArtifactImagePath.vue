<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from "vue";
import {ElMessage} from "element-plus";
import {copyTextToClipboard} from "../utils/clipboard.js";
import {artifactFromUrl, artifactRecord, artifactSharedPath, loadArtifactMetadata} from "./artifactFiles.js";

const props = defineProps({href: {type: String, required: true}});
const identity = artifactFromUrl(props.href);
const record = identity ? artifactRecord(identity) : null;
const sharedPath = computed(() => artifactSharedPath(record?.metadata));
const root = ref(null);
let observer;
function load() { observer?.disconnect(); if (record) void loadArtifactMetadata(record).catch(() => {}); }
onMounted(() => {
	if (typeof IntersectionObserver === "undefined") return load();
	observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) load(); }, {rootMargin: "160px"});
	observer.observe(root.value);
});
onBeforeUnmount(() => observer?.disconnect());
async function copyPath() {
	if (!sharedPath.value) return;
	try { await copyTextToClipboard(sharedPath.value); ElMessage.success("工作区路径已复制"); }
	catch { ElMessage.error("复制失败，请手动选择路径"); }
}
</script>

<template>
	<span ref="root" class="artifact-image-actions">
		<a :href="identity?.downloadUrl" download title="下载原图片" aria-label="下载原图片" @click.stop>下载</a>
		<button v-if="sharedPath" type="button" :aria-label="`复制工作区路径：${sharedPath}`" title="复制工作区路径" @click.stop="copyPath">复制路径</button>
	</span>
</template>

<style>
.artifact-image-actions { display: inline-flex; align-items: center; gap: 8px; margin: 0 0 0.7rem 8px; vertical-align: bottom; font-size: 12px; }
.artifact-image-actions a, .artifact-image-actions button { padding: 5px 8px; border: 1px solid var(--ob-border-soft); border-radius: 7px; background: var(--ob-surface); color: var(--ob-text-subtle); text-decoration: none; font: inherit; cursor: pointer; }
.artifact-image-actions a:hover, .artifact-image-actions button:hover { color: var(--ob-blue); background: var(--ob-surface-soft); }
.artifact-image-actions a:focus-visible, .artifact-image-actions button:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
</style>
