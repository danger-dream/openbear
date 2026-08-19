<script setup>
import {computed, onBeforeUnmount, ref, watch} from "vue";
import {renderMarkdown} from "./markdown.js";

const props = defineProps({
	text: {type: String, default: ""},
	tag: {type: String, default: "div"},
	live: {type: Boolean, default: false},
	liveIntervalMs: {type: Number, default: 32},
});

const SOFT_PUNCT = new Set(["，", "、", "；", "：", ",", ";", ":"]);
const HARD_PUNCT = new Set(["。", "！", "？", ".", "!", "?"]);
const CLOSE_PUNCT = new Set([")", "]", "}", "）", "】", "》", "」", "』", "”", "’", "\""]);
const componentTag = computed(() => props.tag || "div");
const displayedText = ref(String(props.text || ""));
const imageViewerOpen = ref(false);
const imageViewerUrls = ref([]);
const imageViewerIndex = ref(0);
let liveTimer = 0;
let liveFrame = 0;
let lastLivePaintAt = 0;
let livePauseUntil = 0;
let fenceCacheSource = "";
let fenceCacheIndex = 0;
let fenceCacheCount = 0;

function nowMs() {
	return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function cancelLivePaint() {
	if (liveTimer) window.clearTimeout(liveTimer);
	if (liveFrame) window.cancelAnimationFrame(liveFrame);
	liveTimer = 0;
	liveFrame = 0;
	livePauseUntil = 0;
	fenceCacheSource = "";
	fenceCacheIndex = 0;
	fenceCacheCount = 0;
}

function codeFenceOpenAt(text, index) {
	if (text !== fenceCacheSource || index < fenceCacheIndex) {
		fenceCacheSource = text;
		fenceCacheIndex = 0;
		fenceCacheCount = 0;
	}
	let pos = fenceCacheIndex;
	while (pos < index) {
		const next = text.indexOf("```", pos);
		if (next < 0) {
			fenceCacheIndex = Math.max(fenceCacheIndex, index);
			return fenceCacheCount % 2 === 1;
		}
		if (next + 3 > index) {
			fenceCacheIndex = Math.max(fenceCacheIndex, pos);
			return fenceCacheCount % 2 === 1;
		}
		const lineStart = next === 0 || text[next - 1] === "\n";
		if (lineStart) fenceCacheCount += 1;
		pos = next + 3;
	}
	fenceCacheIndex = Math.max(fenceCacheIndex, index);
	return fenceCacheCount % 2 === 1;
}

function isAsciiWordChar(ch) {
	return /[A-Za-z0-9_$-]/.test(ch || "");
}

function stepForBacklog(backlog, inCode) {
	if (backlog <= 0) return 0;
	if (inCode) {
		if (backlog > 4000) return 1200;
		if (backlog > 1200) return 420;
		if (backlog > 300) return 140;
		return Math.min(72, Math.max(18, Math.ceil(backlog * 0.5)));
	}
	if (backlog > 5000) return 1100;
	if (backlog > 1600) return 380;
	if (backlog > 650) return 150;
	if (backlog > 220) return 64;
	if (backlog > 80) return 24;
	if (backlog > 24) return 8;
	return Math.min(4, Math.max(1, Math.ceil(backlog * 0.38)));
}

function naturalBoundary(target, from, rawStep, {backlog, inCode} = {}) {
	let to = Math.min(target.length, from + Math.max(1, rawStep));
	if (to >= target.length || inCode || backlog > 220) return to;
	while (to < target.length && CLOSE_PUNCT.has(target[to]) && to - from <= rawStep + 3) to += 1;
	if (to < target.length && SOFT_PUNCT.has(target[to]) && to - from <= rawStep + 2) to += 1;
	if (to < target.length && HARD_PUNCT.has(target[to]) && to - from <= rawStep + 2) to += 1;
	if (isAsciiWordChar(target[to - 1]) && isAsciiWordChar(target[to])) {
		const maxWordEnd = Math.min(target.length, to + 18);
		while (to < maxWordEnd && isAsciiWordChar(target[to])) to += 1;
	}
	return to;
}

function pauseForSlice(text, target) {
	const backlog = target.length - text.length;
	if (backlog > 180 || codeFenceOpenAt(target, text.length)) return 0;
	const last = text[text.length - 1] || "";
	const prev = text[text.length - 2] || "";
	if (last === "\n" && prev === "\n") return 96;
	if (last === "\n") return 46;
	if (HARD_PUNCT.has(last)) return 58;
	if (SOFT_PUNCT.has(last)) return 26;
	return 0;
}

function nextLiveSlice(current, target) {
	if (!target.startsWith(current)) return target;
	const backlog = target.length - current.length;
	if (backlog <= 0) return current;
	const inCode = codeFenceOpenAt(target, current.length);
	const rawStep = stepForBacklog(backlog, inCode);
	const to = naturalBoundary(target, current.length, rawStep, {backlog, inCode});
	return target.slice(0, to);
}

function paintLiveText() {
	liveFrame = 0;
	const now = nowMs();
	if (now < livePauseUntil) {
		scheduleLivePaint(livePauseUntil - now);
		return;
	}
	lastLivePaintAt = now;
	const target = String(props.text || "");
	if (!props.live) {
		displayedText.value = target;
		return;
	}
	const next = nextLiveSlice(displayedText.value, target);
	displayedText.value = next;
	if (next !== target) {
		livePauseUntil = now + pauseForSlice(next, target);
		scheduleLivePaint(Math.max(0, livePauseUntil - now));
	}
}

function scheduleLivePaint(delayMs = 0) {
	if (liveTimer || liveFrame) return;
	const interval = Math.max(16, Number(props.liveIntervalMs || 32));
	const wait = Math.max(Number(delayMs || 0), interval - (nowMs() - lastLivePaintAt), 0);
	liveTimer = window.setTimeout(() => {
		liveTimer = 0;
		liveFrame = window.requestAnimationFrame(paintLiveText);
	}, wait);
}

watch(() => [props.text, props.live, props.liveIntervalMs], () => {
	const target = String(props.text || "");
	if (!props.live) {
		cancelLivePaint();
		displayedText.value = target;
		return;
	}
	if (!target.startsWith(displayedText.value) || displayedText.value.length > target.length) {
		cancelLivePaint();
		displayedText.value = target;
		if (displayedText.value !== target) scheduleLivePaint();
		return;
	}
	if (displayedText.value !== target) scheduleLivePaint();
}, {immediate: true});

onBeforeUnmount(cancelLivePaint);

function appendLiveCaret(rendered) {
	if (!rendered || !props.live || !displayedText.value) return rendered;
	const caret = `<span class="md-live-caret" aria-hidden="true"></span>`;
	let insertAt = -1;
	for (const needle of ["</code>", "</p>", "</li>", "</td>", "</th>", "</blockquote>"]) {
		insertAt = Math.max(insertAt, rendered.lastIndexOf(needle));
	}
	if (insertAt >= 0) return `${rendered.slice(0, insertAt)}${caret}${rendered.slice(insertAt)}`;
	return `${rendered}${caret}`;
}

const html = computed(() => appendLiveCaret(renderMarkdown(displayedText.value, {live: props.live})));

function onMarkdownClick(event) {
	const img = event.target?.closest?.("img");
	if (!img) return;
	event.preventDefault();
	event.stopPropagation();
	const root = event.currentTarget;
	const images = Array.from(root?.querySelectorAll?.("img") || []);
	const urls = images.map((item) => item.currentSrc || item.src).filter(Boolean);
	const src = img.currentSrc || img.src || "";
	if (!src) return;
	imageViewerUrls.value = urls.length ? urls : [src];
	imageViewerIndex.value = Math.max(0, imageViewerUrls.value.indexOf(src));
	imageViewerOpen.value = true;
}
</script>

<template>
	<component :is="componentTag" class="bear-md" v-html="html" @click="onMarkdownClick"></component>
	<el-image-viewer v-if="imageViewerOpen"
	                 :url-list="imageViewerUrls"
	                 :initial-index="imageViewerIndex"
	                 hide-on-click-modal
	                 @close="imageViewerOpen = false"/>
</template>

<style scoped>
.bear-md :deep(h1),
.bear-md :deep(h2),
.bear-md :deep(h3),
.bear-md :deep(h4) {
	margin: 0.9rem 0 0.45rem;
	color: #111827;
	font-weight: 760;
	line-height: 1.35;
}

.bear-md :deep(h1) {
	font-size: 1.28em;
}

.bear-md :deep(h2) {
	font-size: 1.16em;
}

.bear-md :deep(h3) {
	font-size: 1.06em;
}

.bear-md :deep(h4) {
	font-size: 1em;
}

.bear-md {
	min-width: 0;
	max-width: 100%;
}

.bear-md :deep(.md-live-caret) {
	display: inline-block;
	width: 0.48em;
	height: 1.08em;
	margin-left: 0.08em;
	border-radius: 999px;
	background: currentColor;
	opacity: 0.52;
	vertical-align: -0.18em;
	animation: md-live-caret-breathe 1.05s ease-in-out infinite;
}

.bear-md :deep(pre .md-live-caret),
.bear-md :deep(code .md-live-caret) {
	width: 0.46em;
	height: 1em;
	vertical-align: -0.16em;
}

@keyframes md-live-caret-breathe {
	0%, 100% {
		opacity: 0.24;
		transform: translateY(0) scaleY(0.86);
	}
	45% {
		opacity: 0.62;
		transform: translateY(-0.02em) scaleY(1.04);
	}
}

.bear-md :deep(img) {
	display: block;
	width: auto;
	max-width: min(100%, 34rem);
	max-height: min(58vh, 32rem);
	margin: 0.7rem 0;
	border-radius: 14px;
	object-fit: contain;
	box-shadow: 0 14px 34px rgba(15, 23, 42, 0.12);
	cursor: zoom-in;
}

.bear-md :deep(.md-table-scroll) {
	max-width: 100%;
	margin: 0.72rem 0;
	overflow-x: auto;
	overflow-y: hidden;
	scrollbar-width: thin;
}

.bear-md :deep(table) {
	width: max-content;
	min-width: 100%;
	max-width: none;
	border-collapse: collapse;
	border-spacing: 0;
	font-size: 12px;
}

.bear-md :deep(th),
.bear-md :deep(td) {
	border: 1px solid #e5e7eb;
	padding: 0.42rem 0.55rem;
	vertical-align: top;
	line-height: 1.55;
}

.bear-md :deep(th) {
	background: #f8fafc;
	color: #334155;
	font-weight: 740;
	white-space: nowrap;
}

.bear-md :deep(td) {
	background: #fff;
	color: #374151;
}

.bear-md :deep(tr:nth-child(even) td) {
	background: #fcfcfd;
}

.bear-md :deep(hr) {
	margin: 0.95rem 0;
	border: 0;
	border-top: 1px solid #e5e7eb;
}

.bear-md :deep(.hljs) {
	background: transparent;
	color: #24292f;
	padding: 0;
}

.bear-md :deep(p) {
	margin: 0.55rem 0;
}

.bear-md :deep(p:first-child) {
	margin-top: 0;
}

.bear-md :deep(p:last-child) {
	margin-bottom: 0;
}

.bear-md :deep(pre) {
	margin: 0.75rem 0;
	max-width: none;
	overflow: auto;
	border-radius: 9px;
	background: #f8fafc;
	padding: 0.85rem;
	border: 1px solid #eef2f7;
	color: #24292f;
	font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
	font-size: 13.5px;
	line-height: 1.62;
	scrollbar-width: thin;
}

.bear-md :deep(code) {
	border-radius: 5px;
	background: #f3f4f6;
	padding: 0.1rem 0.3rem;
	color: #24292f;
	font-size: 0.9em;
}

.bear-md :deep(pre code) {
	background: transparent;
	padding: 0;
	color: inherit;
	font-family: inherit;
	font-size: inherit;
	line-height: inherit;
}

.bear-md :deep(.md-code-block) {
	position: relative;
	margin: 0.75rem 0;
	overflow: hidden;
	border: 1px solid #eef2f7;
	border-radius: 9px;
	background: #f8fafc;
}

.bear-md :deep(.md-code-head) {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: .75rem;
	background: transparent;
	padding: .32rem .5rem .18rem;
	color: #94a3b8;
	font-size: 10px;
	font-weight: 560;
}

.bear-md :deep(.md-code-copy) {
	border: 0;
	border-radius: 6px;
	background: transparent;
	padding: .1rem .3rem;
	color: #94a3b8;
	font-size: 10px;
	font-weight: 560;
	cursor: pointer;
	opacity: .72;
	transition: background .14s ease, color .14s ease, opacity .14s ease;
}

.bear-md :deep(.md-code-copy:hover) {
	background: #eef2f7;
	color: #475569;
	opacity: 1;
}

.bear-md :deep(.md-code-copy.copied) {
	background: #f0fdf4;
	color: #15803d;
	opacity: 1;
}

.bear-md :deep(.md-code-block pre) {
	margin: 0;
	border: 0;
	border-radius: 0;
	background: #f8fafc;
	padding-top: .45rem;
}

.bear-md :deep(a) {
	color: #2563eb;
	text-decoration: underline;
	text-underline-offset: 3px;
}

.bear-md :deep(ul), .bear-md :deep(ol) {
	margin: 0.55rem 0 0.55rem 1.25rem;
	padding-left: 1.15rem;
}

.bear-md :deep(ul) {
	list-style: disc;
}

.bear-md :deep(ol) {
	list-style: decimal;
}

.bear-md :deep(ul ul) {
	list-style: circle;
}

.bear-md :deep(ul ul ul) {
	list-style: square;
}

.bear-md :deep(ol ol), .bear-md :deep(ul ol) {
	list-style: lower-alpha;
}

.bear-md :deep(li) {
	margin: 0.22rem 0;
	padding-left: 0.12rem;
}

.bear-md :deep(blockquote) {
	margin: 0.75rem 0;
	border-left: 3px solid #d1d5db;
	padding-left: 0.8rem;
	color: #4b5563;
}

.bear-md :deep(pre::-webkit-scrollbar) {
	width: 8px;
	height: 8px;
}

.bear-md :deep(.md-table-scroll::-webkit-scrollbar) {
	width: 8px;
	height: 8px;
}

.bear-md :deep(pre::-webkit-scrollbar-thumb) {
	border-radius: 999px;
	background: #cbd5e1;
}

.bear-md :deep(.md-table-scroll::-webkit-scrollbar-thumb) {
	border-radius: 999px;
	background: #cbd5e1;
}

.bear-md :deep(pre::-webkit-scrollbar-track) {
	background: transparent;
}

.bear-md :deep(.md-table-scroll::-webkit-scrollbar-track) {
	background: transparent;
}
</style>
