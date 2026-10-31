<script setup>
import {computed} from "vue";

const props = defineProps({status: {type: String, default: "todo"}, spinning: {type: Boolean, default: false}});

const TONES = {
	todo: "todo", doing: "doing", in_progress: "doing", blocked: "blocked", ask: "ask",
	done: "done", dropped: "dropped", abandoned: "dropped", discussing: "discuss", decided: "decided",
};
const tone = computed(() => TONES[props.status] || "todo");
</script>

<template>
	<span class="wi-status-icon" :class="`wi-tone-${tone}`" aria-hidden="true">
		<svg viewBox="0 0 24 24" :class="{'wi-spin': props.spinning}">
			<template v-if="tone === 'doing'"><circle cx="12" cy="12" r="7.5" opacity=".3"/><path d="M12 4.5a7.5 7.5 0 017.5 7.5"/></template>
			<template v-else-if="tone === 'done'"><circle cx="12" cy="12" r="7.5"/><path d="M8.5 12.2l2.4 2.3 4.6-4.8"/></template>
			<template v-else-if="tone === 'ask'"><circle cx="12" cy="12" r="7.5"/><path d="M9.9 9.6a2.2 2.2 0 114.1 1.1c-.6.9-1.9 1.3-1.9 2.6"/><path d="M12 16.3v.1"/></template>
			<template v-else-if="tone === 'blocked'"><path d="M12 4l8.5 15h-17z"/><path d="M12 10v4M12 16.8v.1"/></template>
			<template v-else-if="tone === 'dropped'"><circle cx="12" cy="12" r="7.5"/><path d="M8.5 15.5l7-7"/></template>
			<template v-else-if="tone === 'discuss'"><path d="M5 6h14v9H10l-4 3v-3H5z"/></template>
			<template v-else-if="tone === 'decided'"><path d="M6 21V4M6 4h11l-2.5 4L17 12H6"/></template>
			<circle v-else cx="12" cy="12" r="7.5"/>
		</svg>
	</span>
</template>

<style scoped>
.wi-status-icon { display: inline-grid; place-items: center; width: 20px; height: 20px; flex: none; }
.wi-status-icon svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.wi-spin { animation: wi-spin 1.1s linear infinite; transform-origin: 50% 50%; }
.wi-tone-todo, .wi-tone-dropped { color: var(--ob-text-disabled); }
.wi-tone-doing { color: var(--ob-text-strong); }
.wi-tone-done, .wi-tone-discuss, .wi-tone-decided { color: var(--ob-text-subtle); }
.wi-tone-ask { color: var(--ob-orange); }
.wi-tone-blocked { color: var(--ob-danger); }
@keyframes wi-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .wi-spin { animation: none; } }
</style>
