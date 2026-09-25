<script setup>
import {inject, onBeforeUnmount} from 'vue';
import {WORK_MOTION} from './conversationWork.js';
const props = defineProps({open: Boolean});
const motion = inject(WORK_MOTION, null);
const active = new Map();
function clear(el) {
	const pending = active.get(el);
	if (!pending) return;
	active.delete(el);
	pending.animation?.cancel();
	if (pending.frame) cancelAnimationFrame(pending.frame);
	motion?.finish?.(pending.snapshot);
}
function prepare(el) {
	el.style.height = '0px';
	el.style.opacity = '0';
	el.style.overflow = 'hidden';
}
function animate(el, expanding, done) {
	clear(el);
	const snapshot = motion?.capture?.(el);
	let pending = null;
	const height = el.scrollHeight;
	el.style.overflow = 'hidden';
	const finish = () => {
		if (pending && active.get(el) !== pending) return;
		clear(el);
		el.style.height = '';
		el.style.opacity = '';
		el.style.overflow = '';
		done();
		motion?.finish?.(snapshot);
		queueMicrotask(() => motion?.restore?.(snapshot));
	};
	if (!el.animate || globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
		finish();
		return;
	}
	const animation = el.animate(expanding
		? [{height: '0px', opacity: 0}, {height: `${height}px`, opacity: 1}]
		: [{height: `${height}px`, opacity: 1}, {height: '0px', opacity: 0}],
		{duration: 260, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards'});
	pending = {animation, snapshot, frame: 0};
	active.set(el, pending);
	const tick = () => {
		motion?.restore?.(snapshot);
		if (active.get(el) === pending) pending.frame = requestAnimationFrame(tick);
	};
	pending.frame = requestAnimationFrame(tick);
	animation.finished.then(finish, () => {});
}
onBeforeUnmount(() => { for (const el of active.keys()) clear(el); });
</script>

<template>
	<Transition :css="false" @before-enter="prepare" @enter="(el, done) => animate(el, true, done)" @leave="(el, done) => animate(el, false, done)" @enter-cancelled="clear" @leave-cancelled="clear">
		<div v-if="props.open" class="work-disclosure"><slot/></div>
	</Transition>
</template>

<style scoped>
.work-disclosure { min-width: 0; }
</style>
