<script setup>
import {computed, onBeforeUnmount, ref} from "vue";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {highlightCodeHtml} from "./markdown.js";
import {buildToolArgumentsView} from "./toolArgumentsPresentation.js";

const props = defineProps({
	toolName: {type: String, default: "Tool"},
	rawArguments: {type: [String, Object], default: ""},
	compact: {type: Boolean, default: false},
});

const copiedKey = ref("");
let copiedTimer = 0;

const view = computed(() => buildToolArgumentsView(props.toolName, props.rawArguments));
const primaryTags = computed(() => view.value.tags.filter((item) => item.primary));
const secondaryTags = computed(() => view.value.tags.filter((item) => !item.primary));
const blocks = computed(() => view.value.blocks.map((block, index) => {
	const source = String(block.content ?? "");
	return {
		...block,
		key: `${block.label}-${index}`,
		source,
		charCount: source.length,
		html: highlightCodeHtml(source, block.language),
	};
}));

function formatCount(value) {
	return Number(value || 0).toLocaleString();
}

function blockMeta(block) {
	const labels = [];
	if (block.language) labels.push(block.language);
	labels.push(`${formatCount(block.charCount)} 字符`);
	return labels.join(" · ");
}

async function copyText(text, key) {
	try {
		await copyTextToClipboard(text);
		copiedKey.value = key;
		if (copiedTimer) window.clearTimeout(copiedTimer);
		copiedTimer = window.setTimeout(() => { copiedKey.value = ""; }, 1400);
	} catch {
		copiedKey.value = "";
	}
}

onBeforeUnmount(() => {
	if (copiedTimer) window.clearTimeout(copiedTimer);
});
</script>

<template>
	<div class="tool-arguments-view" :class="{compact}">
		<div v-if="view.mode === 'empty'" class="tool-arguments-empty">无参数</div>
		<template v-else>
			<div v-if="primaryTags.length" class="tool-argument-primary">
				<span
					v-for="(tag, index) in primaryTags"
					:key="`${tag.label}-${index}`"
					class="tool-argument-tag is-primary"
					:class="{'is-wide': tag.wide, 'is-mono': tag.mono}"
					:title="`${tag.label}：${tag.value}`"
				>
					<b>{{ tag.label }}</b><span>{{ tag.value }}</span>
				</span>
			</div>

			<div v-if="secondaryTags.length" class="tool-argument-tags">
				<span
					v-for="(tag, index) in secondaryTags"
					:key="`${tag.label}-${index}`"
					class="tool-argument-tag"
					:class="{'is-wide': tag.wide, 'is-mono': tag.mono}"
					:title="`${tag.label}：${tag.value}`"
				>
					<b>{{ tag.label }}</b><span>{{ tag.value }}</span>
				</span>
			</div>

			<div v-if="view.rows.length" class="tool-argument-rows">
				<div v-for="(row, index) in view.rows" :key="`${row.label}-${index}`" class="tool-argument-row" :class="{'is-primary': row.primary}">
					<div class="tool-argument-row-head">
						<span>{{ row.label }}</span>
						<button v-if="row.copyable" type="button" @click="copyText(row.value, `row-${index}`)">
							{{ copiedKey === `row-${index}` ? "已复制" : "复制" }}
						</button>
					</div>
					<a v-if="row.kind === 'url'" :href="row.value" target="_blank" rel="noopener noreferrer">{{ row.value }}</a>
					<code v-else-if="row.mono">{{ row.value }}</code>
					<p v-else>{{ row.value }}</p>
				</div>
			</div>

			<div v-if="blocks.length" class="tool-argument-blocks">
				<component
					:is="block.secondary ? 'details' : 'section'"
					v-for="block in blocks"
					:key="block.key"
					class="tool-argument-block"
					:class="[block.role ? `role-${block.role}` : '', {'is-secondary': block.secondary}]"
				>
					<summary v-if="block.secondary">
						<div><span>{{ block.label }}</span><em>{{ block.itemCount ? `${block.itemCount} 项` : blockMeta(block) }}</em></div>
						<i>展开</i>
					</summary>
					<header v-else>
						<div><span>{{ block.label }}</span><em>{{ blockMeta(block) }}</em></div>
						<button type="button" @click="copyText(block.source, `block-${block.key}`)">
							{{ copiedKey === `block-${block.key}` ? "已复制" : "复制" }}
						</button>
					</header>
					<div v-if="block.secondary" class="secondary-block-actions">
						<span>{{ blockMeta(block) }}</span>
						<button type="button" @click="copyText(block.source, `block-${block.key}`)">
							{{ copiedKey === `block-${block.key}` ? "已复制" : "复制" }}
						</button>
					</div>
					<pre tabindex="0" :aria-label="`${block.label}，可滚动`"><code class="hljs" :class="block.language ? `language-${block.language}` : ''" v-html="block.html"></code></pre>
				</component>
			</div>
		</template>
	</div>
</template>

<style scoped>
.tool-arguments-view {
	display: grid;
	min-width: 0;
	gap: 8px;
	color: var(--ob-text);
}

.tool-argument-primary,
.tool-argument-tags {
	display: flex;
	min-width: 0;
	flex-wrap: wrap;
	gap: 5px;
}

.tool-argument-primary {
	border-left: 2px solid var(--ob-violet);
	border-radius: 8px;
	background: var(--ob-code-bg);
	padding: 6px 7px;
}

.tool-argument-tag {
	display: inline-flex;
	min-width: 0;
	max-width: min(100%, 320px);
	align-items: center;
	gap: 5px;
	overflow: hidden;
	border: 1px solid var(--ob-border);
	border-radius: 999px;
	background: var(--ob-surface-soft);
	padding: 2px 8px;
	color: var(--ob-text);
	font-size: 10.5px;
	line-height: 1.45;
	white-space: normal;
	overflow-wrap: anywhere;
}

.tool-argument-tag.is-wide {
	max-width: min(100%, 480px);
}

.tool-argument-tag.is-primary {
	border-color: var(--ob-border);
	background: var(--ob-surface);
	color: var(--ob-text);
	box-shadow: var(--ob-shadow-panel);
}

.tool-argument-tag.is-primary b {
	color: var(--ob-violet);
}

.tool-argument-tag b {
	flex: 0 0 auto;
	color: var(--ob-text-muted);
	font-size: 9.5px;
	font-weight: 650;
}

.tool-argument-tag span {
	min-width: 0;
	overflow-wrap: anywhere;
}

.tool-argument-tag.is-mono span,
.tool-argument-row code,
.tool-argument-row a {
	font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.tool-argument-rows,
.tool-argument-blocks {
	display: grid;
	min-width: 0;
	gap: 7px;
}

.tool-argument-row,
.tool-argument-block {
	min-width: 0;
	overflow: hidden;
	border: 1px solid var(--ob-border);
	border-radius: 9px;
	background: var(--ob-surface);
}

.tool-argument-row {
	padding: 7px 9px 8px;
}

.tool-argument-row.is-primary {
	border-color: var(--ob-border);
	box-shadow: var(--ob-shadow-panel);
}

.tool-argument-row.is-primary .tool-argument-row-head > span {
	color: var(--ob-violet);
}

.tool-argument-row-head,
.tool-argument-block header {
	display: flex;
	min-width: 0;
	align-items: center;
	justify-content: space-between;
	gap: 10px;
}

.tool-argument-row-head {
	margin-bottom: 4px;
}

.tool-argument-row-head > span,
.tool-argument-block header span {
	color: var(--ob-text-subtle);
	font-size: 10px;
	font-weight: 650;
}

.tool-argument-row button,
.tool-argument-block button {
	flex: 0 0 auto;
	border: 0;
	background: transparent;
	padding: 1px 2px;
	color: var(--ob-text-muted);
	font-size: 9.5px;
	cursor: pointer;
}

.tool-argument-row button:hover,
.tool-argument-block button:hover,
.tool-argument-row button:focus-visible,
.tool-argument-block button:focus-visible {
	color: var(--ob-text);
	outline: none;
}

.tool-argument-row code,
.tool-argument-row a,
.tool-argument-row p {
	display: block;
	min-width: 0;
	margin: 0;
	overflow-wrap: anywhere;
	color: var(--ob-text);
	font-size: 11.5px;
	line-height: 1.55;
	text-decoration: none;
	word-break: break-word;
}

.tool-argument-row a:hover {
	color: var(--ob-violet);
	text-decoration: underline;
	text-underline-offset: 2px;
}

.tool-argument-block header {
	min-height: 30px;
	border-bottom: 1px solid var(--ob-border);
	background: var(--ob-header);
	padding: 0 9px;
}

.tool-argument-block.is-secondary {
	border-style: dashed;
	background: var(--ob-surface);
}

.tool-argument-block.is-secondary > summary {
	display: flex;
	min-height: 30px;
	align-items: center;
	justify-content: space-between;
	gap: 10px;
	padding: 0 9px;
	color: var(--ob-text-subtle);
	cursor: pointer;
	list-style: none;
}

.tool-argument-block.is-secondary > summary::-webkit-details-marker { display: none; }
.tool-argument-block.is-secondary > summary > div { display: flex; min-width: 0; align-items: baseline; gap: 7px; }
.tool-argument-block.is-secondary > summary span { font-size: 10px; font-weight: 600; }
.tool-argument-block.is-secondary > summary em { color: var(--ob-text-muted); font-size: 9px; font-style: normal; }
.tool-argument-block.is-secondary > summary i { color: var(--ob-text-muted); font-size: 9px; font-style: normal; }
.tool-argument-block.is-secondary[open] > summary { border-bottom: 1px solid var(--ob-border); }
.tool-argument-block.is-secondary[open] > summary i::before { content: "收起"; font-size: 9px; }
.tool-argument-block.is-secondary[open] > summary i { font-size: 0; }

.secondary-block-actions {
	display: flex;
	min-height: 26px;
	align-items: center;
	justify-content: space-between;
	border-bottom: 1px solid var(--ob-border);
	padding: 0 9px;
	color: var(--ob-text-muted);
	font-size: 9px;
}

.secondary-block-actions button {
	border: 0;
	background: transparent;
	padding: 1px 2px;
	color: var(--ob-text-muted);
	font-size: 9.5px;
	cursor: pointer;
}

.tool-argument-block header > div {
	display: flex;
	min-width: 0;
	align-items: baseline;
	gap: 7px;
	overflow: hidden;
}

.tool-argument-block header em {
	overflow: hidden;
	color: var(--ob-text-muted);
	font-size: 9px;
	font-style: normal;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.tool-argument-block.role-old header span::before,
.tool-argument-block.role-new header span::before {
	display: inline-block;
	width: 12px;
	color: var(--ob-text-muted);
}

.tool-argument-block.role-old header span::before { content: "−"; }
.tool-argument-block.role-new header span::before { content: "+"; }

.tool-argument-block pre {
	box-sizing: border-box;
	min-width: 0;
	max-width: 100%;
	max-height: 280px;
	overflow: auto;
	margin: 0;
	background: var(--ob-code-bg);
	padding: 9px 10px 10px;
	scrollbar-color: var(--ob-scrollbar) transparent;
	scrollbar-width: thin;
	white-space: pre;
}

.tool-argument-block code.hljs {
	display: block;
	min-width: max-content;
	background: transparent;
	padding: 0;
	color: var(--ob-text);
	font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	font-size: 11px;
	line-height: 1.55;
	tab-size: 4;
}

.tool-arguments-empty {
	color: var(--ob-text-muted);
	font-size: 11px;
}

.tool-arguments-view.compact {
	gap: 6px;
}

.tool-arguments-view.compact .tool-argument-tag {
	padding: 1px 7px;
	font-size: 10px;
}

.tool-arguments-view.compact .tool-argument-block pre {
	max-height: 220px;
}

@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.tool-argument-tag,
	.tool-argument-tag.is-wide {
		max-width: 100%;
	}

	.tool-argument-block pre,
	.tool-arguments-view.compact .tool-argument-block pre {
		max-height: min(230px, calc(var(--mobile-viewport-height, 100dvh) * .35));
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		-webkit-overflow-scrolling: touch;
	}
	.tool-argument-block code.hljs { min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
}
</style>

<style>
/* OpenBear system dark theme */
html.dark .tool-argument-primary {
		border-left: 2px solid rgb(var(--ob-blue-rgb) / 0.52);
	}
</style>
