<script setup>
import {ref, watch} from "vue";
import {ChevronDown, Sparkles, X} from "@lucide/vue";

const props = defineProps({
	items: {type: Array, default: () => []},
	// A draft folds the list to one line; it stays expandable and pickable.
	collapsed: {type: Boolean, default: false},
});
const emit = defineEmits(["pick", "dismiss"]);

const expanded = ref(!props.collapsed);
watch(() => props.collapsed, (collapsed) => { expanded.value = !collapsed; });
</script>

<template>
	<section class="prompt-suggestions" :class="{'is-collapsed': !expanded}" aria-label="推荐的下一句">
		<header class="prompt-suggestions-head">
			<button type="button" class="prompt-suggestions-toggle" :aria-expanded="expanded"
			        @click="expanded = !expanded">
				<Sparkles class="prompt-suggestions-icon" aria-hidden="true"/>
				<span>接着说</span>
				<span v-if="!expanded" class="prompt-suggestions-count">{{ props.items.length }} 条</span>
				<ChevronDown class="prompt-suggestions-chevron" aria-hidden="true"/>
			</button>
			<button type="button" class="prompt-suggestions-close" aria-label="关闭推荐" @click="emit('dismiss')">
				<X aria-hidden="true"/>
			</button>
		</header>
		<div v-if="expanded" class="prompt-suggestions-list">
			<button v-for="(text, index) in props.items" :key="`${index}:${text}`" type="button"
			        class="prompt-suggestion" :aria-label="`填入：${text}`" @click="emit('pick', text)">{{ text }}</button>
		</div>
	</section>
</template>

<style scoped>
.prompt-suggestions {
	margin-top: .55rem;
	padding: .5rem .18rem 0;
	border-top: 1px dashed var(--ob-chat-line);
	animation: prompt-suggestions-in .18s ease-out;
}

.prompt-suggestions-head {
	display: flex;
	align-items: center;
	min-height: 1.5rem;
	margin-bottom: .3rem;
}

.prompt-suggestions.is-collapsed .prompt-suggestions-head {
	margin-bottom: 0;
}

.prompt-suggestions-toggle {
	display: inline-flex;
	align-items: center;
	gap: .32rem;
	min-height: 1.5rem;
	margin-left: -.3rem;
	border-radius: .45rem;
	padding: 0 .3rem;
	color: var(--ob-chat-muted);
	font-size: 11.5px;
	line-height: 1;
	transition: background-color .15s, color .15s;
}

.prompt-suggestions-toggle:hover {
	background: var(--ob-chat-hover);
	color: var(--ob-chat-text);
}

.prompt-suggestions-icon {
	width: .78rem;
	height: .78rem;
	flex: none;
	color: var(--ob-violet);
}

.prompt-suggestions-count {
	font-variant-numeric: tabular-nums;
}

.prompt-suggestions-chevron {
	width: .8rem;
	height: .8rem;
	flex: none;
	transform: rotate(180deg);
	transition: transform .15s;
}

.prompt-suggestions.is-collapsed .prompt-suggestions-chevron {
	transform: none;
}

.prompt-suggestions-close {
	display: inline-grid;
	place-items: center;
	width: 1.5rem;
	height: 1.5rem;
	margin-left: auto;
	border-radius: .45rem;
	color: var(--ob-chat-muted);
	transition: background-color .15s, color .15s;
}

.prompt-suggestions-close svg {
	width: .85rem;
	height: .85rem;
}

.prompt-suggestions-close:hover {
	background: var(--ob-chat-hover);
	color: var(--ob-chat-text);
}

.prompt-suggestions-list {
	display: flex;
	flex-wrap: wrap;
	gap: .38rem;
	min-width: 0;
}

.prompt-suggestion {
	max-width: 100%;
	min-width: 0;
	border: 1px solid var(--ob-chat-border);
	border-radius: .75rem;
	background: var(--ob-chat-panel);
	padding: .3rem .7rem;
	color: var(--ob-chat-text);
	font-size: 13px;
	line-height: 1.45;
	text-align: left;
	overflow-wrap: anywhere;
	transition: background-color .15s, border-color .15s;
}

.prompt-suggestion:hover {
	background: var(--ob-chat-hover);
}

.prompt-suggestion:active {
	background: var(--ob-chat-selected);
}

.prompt-suggestion:focus-visible,
.prompt-suggestions-toggle:focus-visible,
.prompt-suggestions-close:focus-visible {
	outline: 2px solid var(--ob-focus);
	outline-offset: 1px;
}

@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	.prompt-suggestion {
		min-height: 38px;
		padding: .4rem .75rem;
	}

	.prompt-suggestions-toggle {
		min-height: 2rem;
	}

	.prompt-suggestions-close {
		width: 2rem;
		height: 2rem;
		margin-block: -.25rem;
	}
}

@keyframes prompt-suggestions-in {
	from { opacity: 0; }
}
</style>
