<script setup>
import {onBeforeUnmount} from "vue";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import {copyTextToClipboard} from "../../utils/clipboard.js";

defineProps({text: {type: String, default: ""}});
const feedbackTimers = new Map();
let disposed = false;

async function copyCode(event) {
	const button = event.target?.closest?.(".md-code-copy");
	if (!button || !event.currentTarget.contains(button)) return;
	event.preventDefault();
	// Own this click in both the composer and history: do not also invoke the
	// surrounding timeline's delegated copier or an interaction action.
	event.stopPropagation();
	const code = button.closest(".md-code-block")?.querySelector("pre code");
	if (!code) return;
	clearTimeout(feedbackTimers.get(button));
	feedbackTimers.delete(button);
	let copied = false;
	try { copied = await copyTextToClipboard(code.textContent || ""); } catch { /* Show local feedback; never log the body. */ }
	if (disposed || !button.isConnected) return;
	button.textContent = copied ? "已复制" : "复制失败";
	button.classList.toggle("copied", copied);
	feedbackTimers.set(button, setTimeout(() => {
		feedbackTimers.delete(button);
		if (!button.isConnected) return;
		button.textContent = "复制";
		button.classList.remove("copied");
	}, 1200));
}

onBeforeUnmount(() => {
	disposed = true;
	for (const timer of feedbackTimers.values()) clearTimeout(timer);
	feedbackTimers.clear();
});
</script>

<template>
	<div class="interaction-markdown" @click="copyCode">
		<ConsoleMarkdown :text="text" :artifact-cards="false"/>
	</div>
</template>

<style scoped>
.interaction-markdown { min-width:0; max-width:100%; white-space:normal; overflow-wrap:anywhere; }
.interaction-markdown :deep(.bear-md) { color:inherit; font-size:inherit; line-height:inherit; white-space:normal; }
.interaction-markdown :deep(.bear-md > :first-child) { margin-top:0; }
.interaction-markdown :deep(.bear-md > :last-child) { margin-bottom:0; }
.interaction-markdown :deep(pre) { box-sizing:border-box; max-width:100%; white-space:pre; overflow-wrap:normal; word-break:normal; }
/* Keep horizontal overflow local to structured content, not the whole form. */
.interaction-markdown :deep(pre),
.interaction-markdown :deep(.md-table-scroll) { overscroll-behavior-x:contain; scrollbar-width:thin; scrollbar-color:var(--ob-interaction-border-strong) transparent; }
.interaction-markdown :deep(th),
.interaction-markdown :deep(td) { min-width:8rem; max-width:28rem; overflow-wrap:break-word; word-break:normal; }
</style>
