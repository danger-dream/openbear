<script setup>
import {computed, ref} from "vue";
import {Play, Pause} from "@lucide/vue";

// Native audio controls stay light in dark mode and differ per browser.
const props = defineProps({src: {type: String, required: true}, label: {type: String, default: ""}, autoplay: {type: Boolean, default: false}});
const emit = defineEmits(["error"]);
const RATES = [1, 1.25, 1.5, 2];
const audio = ref(null), playing = ref(false), current = ref(0), duration = ref(0), rate = ref(1), seeking = ref(false);
const length = computed(() => Number.isFinite(duration.value) && duration.value > 0 ? duration.value : 0);

function clock(value) {
	if (!Number.isFinite(value) || value < 0) return "--:--";
	const total = Math.floor(value), hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = String(total % 60).padStart(2, "0");
	return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}
function toggle() {
	const element = audio.value;
	if (!element) return;
	if (element.paused) element.play().catch(() => {}); else element.pause();
}
function timeChanged() { if (!seeking.value) current.value = audio.value.currentTime; }
function durationChanged() { duration.value = audio.value.duration; }
function seekInput(event) { seeking.value = true; current.value = Number(event.target.value); }
function seekCommit(event) { seeking.value = false; audio.value.currentTime = Number(event.target.value); }
function cycleRate() {
	rate.value = RATES[(RATES.indexOf(rate.value) + 1) % RATES.length];
	audio.value.playbackRate = rate.value;
}
defineExpose({pause: () => audio.value?.pause()});
</script>

<template>
	<div class="audio-player" :class="{'is-playing': playing}">
		<audio ref="audio" :src="src" preload="metadata" :autoplay="autoplay" @play="playing = true" @pause="playing = false" @ended="playing = false" @timeupdate="timeChanged" @loadedmetadata="durationChanged" @durationchange="durationChanged" @error="emit('error')"></audio>
		<button type="button" class="audio-player-toggle" :aria-label="`${playing ? '暂停' : '播放'}：${label}`" @click="toggle"><Pause v-if="playing" :size="16" :stroke-width="2" fill="currentColor"/><Play v-else :size="16" :stroke-width="2" fill="currentColor"/></button>
		<span class="audio-player-time">{{ clock(current) }}<span v-if="length"> / {{ clock(length) }}</span></span>
		<input class="audio-player-seek" type="range" min="0" :max="length" step="0.1" :value="Math.min(current, length)" :disabled="!length" :aria-label="`播放进度：${label}`" :aria-valuetext="`${clock(current)} / ${clock(length)}`" @input="seekInput" @change="seekCommit">
		<button type="button" class="audio-player-rate" :aria-label="`播放速度 ${rate} 倍，点击切换`" @click="cycleRate">{{ rate }}×</button>
	</div>
</template>

<style>
.audio-player { display: flex; align-items: center; gap: 10px; min-width: 0; height: 44px; padding: 0 6px 0 4px; border-radius: 22px; background: var(--ob-surface-soft); color: var(--ob-text); box-sizing: border-box; }
.audio-player audio { display: none; }
.audio-player-toggle { display: grid; flex: none; place-items: center; width: 36px; height: 36px; padding: 0; border: 0; border-radius: 50%; background: var(--ob-violet); color: #fff; cursor: pointer; }
.audio-player-toggle svg { margin-left: 1px; }
.audio-player.is-playing .audio-player-toggle svg { margin-left: 0; }
.audio-player-time { flex: none; color: var(--ob-text-subtle); font-size: 12px; font-variant-numeric: tabular-nums; line-height: 1; white-space: nowrap; }
.audio-player-seek { flex: 1; min-width: 48px; height: 20px; margin: 0; accent-color: var(--ob-violet); cursor: pointer; }
.audio-player-seek:disabled { cursor: default; opacity: .5; }
.audio-player-rate { flex: none; min-width: 40px; height: 32px; padding: 0 6px; border: 0; border-radius: 16px; background: transparent; color: var(--ob-text-subtle); font: 600 12px/1 ui-monospace, SFMono-Regular, Consolas, monospace; cursor: pointer; }
.audio-player-rate:hover { background: var(--ob-hover); color: var(--ob-text-strong); }
.audio-player button:focus-visible, .audio-player-seek:focus-visible { outline: 2px solid var(--ob-blue); outline-offset: 2px; }
</style>
