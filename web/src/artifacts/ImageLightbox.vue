<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {artifactFromUrl} from "./artifactFiles.js";
import {onArtifactDownload} from "./artifactDownload.js";

const props = defineProps({
	urlList: {type: Array, default: () => []},
	initialIndex: {type: Number, default: 0},
	infinite: {type: Boolean, default: true},
	closeOnPressEscape: {type: Boolean, default: true},
	hideOnClickModal: {type: Boolean, default: true},
	teleported: {type: Boolean, default: true},
	zIndex: {type: Number, default: 3000},
	scale: {type: Number, default: 1},
	minScale: {type: Number, default: 0.2},
	maxScale: {type: Number, default: 5},
	zoomRate: {type: Number, default: 1.25},
	crossorigin: {type: String, default: undefined},
});

const emit = defineEmits(["close", "switch", "rotate"]);

const wrapper = ref(null);
const stage = ref(null);
const imgRef = ref(null);
const activeIndex = ref(Math.max(0, Math.min(props.initialIndex || 0, Math.max(0, props.urlList.length - 1))));
const loading = ref(true);
const loadError = ref(false);
const mode = ref("contain");
const scale = ref(clampScale(props.scale));
const deg = ref(0);
const offsetX = ref(0);
const offsetY = ref(0);
const animating = ref(false);
const dismissY = ref(0);
const dismissX = ref(0);
const dismissing = ref(false);

const isSingle = computed(() => (props.urlList?.length || 0) <= 1);
const isFirst = computed(() => activeIndex.value <= 0);
const isLast = computed(() => activeIndex.value >= (props.urlList?.length || 1) - 1);
const currentImg = computed(() => String(props.urlList?.[activeIndex.value] || ""));
const currentIdentity = computed(() => artifactFromUrl(currentImg.value));
const downloadHref = computed(() => currentIdentity.value?.downloadUrl || currentImg.value);
const downloadFileName = computed(() => {
	const raw = currentImg.value.split("?")[0].split("#")[0];
	const tail = raw.split("/").pop() || "";
	let seg = tail;
	try {
		seg = decodeURIComponent(tail);
	} catch {
		// Keep the raw segment when a filename contains an unescaped '%'.
	}
	return seg && seg !== "content" ? seg : "image.png";
});
const zoomPercent = computed(() => `${Math.round(scale.value * 100)}%`);
const backdropOpacity = computed(() => {
	if (dismissing.value) return 0;
	const dragRatio = Math.min(1, Math.abs(dismissY.value) / 260);
	return Math.max(0.15, 0.92 * (1 - dragRatio * 0.72));
});
const imgStyle = computed(() => {
	const s = scale.value;
	const totalX = offsetX.value + dismissX.value;
	const totalY = offsetY.value + dismissY.value;
	const dismissScale = dismissY.value !== 0 ? Math.max(0.76, 1 - Math.abs(dismissY.value) / 900) : 1;
	const style = {
		transform: `translate3d(${totalX}px, ${totalY}px, 0) scale(${(s * dismissScale).toFixed(4)}) rotate(${deg.value}deg)`,
		transition: animating.value ? "transform 0.22s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.18s ease" : "none",
	};
	if (mode.value === "contain") {
		style.maxWidth = "100%";
		style.maxHeight = "100%";
	} else {
		style.maxWidth = "none";
		style.maxHeight = "none";
	}
	if (dismissing.value) {
		style.opacity = "0";
	}
	return style;
});

function clamp(val, min, max) {
	return Math.min(max, Math.max(min, val));
}

function clampScale(val) {
	return clamp(Number(val) || 1, props.minScale, props.maxScale);
}

function stageCenter() {
	const rect = stage.value?.getBoundingClientRect?.();
	if (!rect || !rect.width) return {cx: (globalThis.innerWidth || 390) / 2, cy: (globalThis.innerHeight || 844) / 2, width: globalThis.innerWidth || 390, height: globalThis.innerHeight || 844};
	return {cx: rect.left + rect.width / 2, cy: rect.top + rect.height / 2, width: rect.width, height: rect.height};
}

function baseImageSize() {
	const el = imgRef.value;
	const {width: sw, height: sh} = stageCenter();
	const nw = el?.naturalWidth || el?.offsetWidth || sw;
	const nh = el?.naturalHeight || el?.offsetHeight || sh;
	if (!nw || !nh) return {w: sw, h: sh};
	if (mode.value === "original") return {w: nw, h: nh};
	const ratio = Math.min(1, sw / nw, sh / nh);
	return {w: nw * ratio, h: nh * ratio};
}

function clampOffsets(nx, ny, targetScale = scale.value) {
	const {width: sw, height: sh} = stageCenter();
	const {w, h} = baseImageSize();
	const rad = Math.abs(deg.value % 180) === 90;
	const rw = (rad ? h : w) * targetScale;
	const rh = (rad ? w : h) * targetScale;
	const maxX = Math.max(0, (rw - sw) / 2 + 32);
	const maxY = Math.max(0, (rh - sh) / 2 + 32);
	return {
		x: rw <= sw ? 0 : clamp(nx, -maxX, maxX),
		y: rh <= sh ? 0 : clamp(ny, -maxY, maxY),
	};
}

function resetTransform(withTransition = false) {
	animating.value = withTransition;
	scale.value = clampScale(props.scale);
	deg.value = 0;
	offsetX.value = 0;
	offsetY.value = 0;
	dismissX.value = 0;
	dismissY.value = 0;
}

function toggleMode() {
	if (loading.value || loadError.value) return;
	if (scale.value > 1.05 || mode.value === "original") {
		mode.value = "contain";
		resetTransform(true);
	} else {
		mode.value = "original";
		resetTransform(true);
	}
}

function zoomAt(targetScale, clientX, clientY, withTransition = true) {
	const prev = scale.value;
	const next = clampScale(targetScale);
	if (Math.abs(next - prev) < 0.001) return;
	const {cx, cy} = stageCenter();
	const px = Number.isFinite(clientX) ? clientX : cx;
	const py = Number.isFinite(clientY) ? clientY : cy;
	const ratio = next / prev;
	const rawX = (px - cx) - ((px - cx) - offsetX.value) * ratio;
	const rawY = (py - cy) - ((py - cy) - offsetY.value) * ratio;
	const clamped = next <= 1.02 ? {x: 0, y: 0} : clampOffsets(rawX, rawY, next);
	animating.value = withTransition;
	scale.value = Number(next.toFixed(3));
	offsetX.value = Math.round(clamped.x);
	offsetY.value = Math.round(clamped.y);
}

function handleActions(action, options = {}) {
	if (loading.value || loadError.value) return;
	const {zoomRate = props.zoomRate, rotateDeg = 90, enableTransition = true, clientX, clientY} = options;
	switch (action) {
		case "zoomOut":
			zoomAt(scale.value / zoomRate, clientX, clientY, enableTransition);
			break;
		case "zoomIn":
			zoomAt(scale.value * zoomRate, clientX, clientY, enableTransition);
			break;
		case "clockwise":
			animating.value = enableTransition;
			deg.value += rotateDeg;
			emit("rotate", deg.value);
			break;
		case "anticlockwise":
			animating.value = enableTransition;
			deg.value -= rotateDeg;
			emit("rotate", deg.value);
			break;
		case "reset":
			mode.value = "contain";
			resetTransform(enableTransition);
			break;
	}
}

function setActiveItem(index) {
	const len = props.urlList?.length || 0;
	if (!len) return;
	loadError.value = false;
	const next = ((index % len) + len) % len;
	if (next === activeIndex.value) return;
	activeIndex.value = next;
}

function prev() {
	if (isFirst.value && !props.infinite) return;
	setActiveItem(activeIndex.value - 1);
}

function next() {
	if (isLast.value && !props.infinite) return;
	setActiveItem(activeIndex.value + 1);
}

function hide() {
	emit("close");
}

function onStageBackdropClick(event) {
	if (!props.hideOnClickModal) return;
	if (gestureMoved) {
		gestureMoved = false;
		return;
	}
	if (event.target === stage.value || event.target?.classList?.contains("image-lightbox-backdrop")) {
		hide();
	}
}

function onDownloadClick(event) {
	if (currentIdentity.value) {
		onArtifactDownload(event, currentIdentity.value);
	}
}

function handleImgLoad() {
	loading.value = false;
	loadError.value = false;
}

function handleImgError() {
	loading.value = false;
	loadError.value = true;
}

function onDoubleTapOrClick(clientX, clientY) {
	if (loading.value || loadError.value) return;
	if (scale.value > 1.08 || mode.value === "original") {
		mode.value = "contain";
		resetTransform(true);
	} else {
		zoomAt(2.5, clientX, clientY, true);
	}
}

function onImgDblClick(event) {
	event.preventDefault();
	event.stopPropagation();
	onDoubleTapOrClick(event.clientX, event.clientY);
}

let gestureMoved = false;
let mouseDragging = false;
let mouseStartX = 0;
let mouseStartY = 0;
let mouseStartOffsetX = 0;
let mouseStartOffsetY = 0;

function onMouseDown(event) {
	if (loading.value || event.button !== 0) return;
	if (scale.value <= 1.02 && mode.value === "contain") return;
	event.preventDefault();
	mouseDragging = true;
	gestureMoved = false;
	animating.value = false;
	mouseStartX = event.clientX;
	mouseStartY = event.clientY;
	mouseStartOffsetX = offsetX.value;
	mouseStartOffsetY = offsetY.value;
	window.addEventListener("mousemove", onMouseMove);
	window.addEventListener("mouseup", onMouseUp);
}

function onMouseMove(event) {
	if (!mouseDragging) return;
	const dx = event.clientX - mouseStartX;
	const dy = event.clientY - mouseStartY;
	if (Math.hypot(dx, dy) > 4) gestureMoved = true;
	const clamped = clampOffsets(mouseStartOffsetX + dx, mouseStartOffsetY + dy);
	offsetX.value = Math.round(clamped.x);
	offsetY.value = Math.round(clamped.y);
}

function onMouseUp() {
	mouseDragging = false;
	window.removeEventListener("mousemove", onMouseMove);
	window.removeEventListener("mouseup", onMouseUp);
}

function onWheel(event) {
	if (loading.value || loadError.value) return;
	event.preventDefault();
	const delta = event.deltaY || event.deltaX;
	if (!delta) return;
	const factor = delta < 0 ? 1.15 : 1 / 1.15;
	const next = clamp(scale.value * factor, props.minScale, props.maxScale);
	zoomAt(next, event.clientX, event.clientY, false);
}

// Touch state for single-finger pan/swipe/dismiss, two-finger pinch-zoom, and double-tap.
let touchMode = ""; // "single" | "pinch"
// The one finger left of a pinch: it may pan, and on lifting the zoom settles as the pinch's would.
let afterPinch = false;
let touchStartX = 0;
let touchStartY = 0;
let touchStartOffsetX = 0;
let touchStartOffsetY = 0;
let pinchStartDist = 0;
let pinchStartScale = 1;
let pinchStartMidX = 0;
let pinchStartMidY = 0;
let pinchStartOffsetX = 0;
let pinchStartOffsetY = 0;
let lastTapAt = 0;
let lastTapX = 0;
let lastTapY = 0;
let singleTapMoved = false;

function touchDistance(t0, t1) {
	return Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
}

function touchMidpoint(t0, t1) {
	return {x: (t0.clientX + t1.clientX) / 2, y: (t0.clientY + t1.clientY) / 2};
}

function beginPinch(t0, t1) {
	touchMode = "pinch";
	singleTapMoved = true;
	gestureMoved = true;
	animating.value = false;
	dismissX.value = 0;
	dismissY.value = 0;
	pinchStartDist = Math.max(12, touchDistance(t0, t1));
	pinchStartScale = scale.value;
	const mid = touchMidpoint(t0, t1);
	pinchStartMidX = mid.x;
	pinchStartMidY = mid.y;
	pinchStartOffsetX = offsetX.value;
	pinchStartOffsetY = offsetY.value;
}

function beginSingleTouch(touch, fromPinch = false) {
	touchMode = "single";
	afterPinch = fromPinch;
	animating.value = false;
	touchStartX = touch.clientX;
	touchStartY = touch.clientY;
	touchStartOffsetX = offsetX.value;
	touchStartOffsetY = offsetY.value;
}

function onTouchStart(event) {
	if (loading.value || dismissing.value) return;
	const touches = event.touches;
	if (!touches || touches.length === 0) return;
	if (touches.length >= 2) {
		if (event.cancelable) event.preventDefault();
		beginPinch(touches[0], touches[1]);
		return;
	}
	// Do not call preventDefault on single-finger touchstart so mobile browsers
	// preserve native long-press "Save/Share Image" when the finger stays still.
	singleTapMoved = false;
	gestureMoved = false;
	beginSingleTouch(touches[0]);
}

function onTouchMove(event) {
	if (loading.value || dismissing.value) return;
	const touches = event.touches;
	if (!touches || touches.length === 0) return;

	if (touches.length >= 2) {
		if (touchMode !== "pinch") {
			beginPinch(touches[0], touches[1]);
		}
		if (event.cancelable) event.preventDefault();
		const dist = Math.max(12, touchDistance(touches[0], touches[1]));
		const mid = touchMidpoint(touches[0], touches[1]);
		const nextScale = clamp(pinchStartScale * (dist / pinchStartDist), props.minScale, props.maxScale);
		const {cx, cy} = stageCenter();
		const ratio = nextScale / pinchStartScale;
		const nextX = (mid.x - cx) - ((pinchStartMidX - cx) - pinchStartOffsetX) * ratio;
		const nextY = (mid.y - cy) - ((pinchStartMidY - cy) - pinchStartOffsetY) * ratio;
		scale.value = Number(nextScale.toFixed(3));
		offsetX.value = Math.round(nextX);
		offsetY.value = Math.round(nextY);
		return;
	}

	if (touchMode !== "single") return;
	const touch = touches[0];
	const dx = touch.clientX - touchStartX;
	const dy = touch.clientY - touchStartY;
	if (Math.hypot(dx, dy) > 6) {
		singleTapMoved = true;
		gestureMoved = true;
		if (event.cancelable) event.preventDefault();
	} else {
		return;
	}

	if (scale.value > 1.02 || mode.value === "original") {
		const clamped = clampOffsets(touchStartOffsetX + dx, touchStartOffsetY + dy);
		offsetX.value = Math.round(clamped.x);
		offsetY.value = Math.round(clamped.y);
		return;
	}
	if (afterPinch) return;

	// At 1x scale: support vertical pull-down to dismiss or horizontal swipe to switch images.
	if (Math.abs(dy) > Math.abs(dx) * 1.15 && dy > 0) {
		dismissY.value = Math.round(dy);
		dismissX.value = Math.round(dx * 0.35);
	} else if (!isSingle.value && Math.abs(dx) > Math.abs(dy)) {
		dismissX.value = Math.round(dx * 0.45);
		dismissY.value = 0;
	}
}

function onTouchEnd(event) {
	if (loading.value || dismissing.value) return;
	const remaining = event.touches;
	if (remaining && remaining.length === 1) {
		// Transition smoothly from 2-finger pinch to 1-finger pan without jumping.
		beginSingleTouch(remaining[0], touchMode === "pinch" || afterPinch);
		return;
	}
	if (remaining && remaining.length >= 2) return;

	if (touchMode === "pinch" || touchMode === "single" && afterPinch) {
		touchMode = "";
		afterPinch = false;
		window.setTimeout(() => { gestureMoved = false; }, 0);
		animating.value = true;
		if (scale.value <= 1.02) {
			scale.value = 1;
			offsetX.value = 0;
			offsetY.value = 0;
		} else {
			const clamped = clampOffsets(offsetX.value, offsetY.value, scale.value);
			offsetX.value = Math.round(clamped.x);
			offsetY.value = Math.round(clamped.y);
		}
		return;
	}

	if (touchMode === "single") {
		touchMode = "";
		window.setTimeout(() => { gestureMoved = false; }, 0);
		const changed = event.changedTouches?.[0];
		if (!singleTapMoved && changed) {
			const now = Date.now();
			const dist = Math.hypot(changed.clientX - lastTapX, changed.clientY - lastTapY);
			if (now - lastTapAt <= 280 && dist <= 32) {
				lastTapAt = 0;
				if (event.cancelable) event.preventDefault();
				onDoubleTapOrClick(changed.clientX, changed.clientY);
				return;
			}
			lastTapAt = now;
			lastTapX = changed.clientX;
			lastTapY = changed.clientY;
			return;
		}

		// Handle pull-down dismiss or horizontal swipe at 1x
		if (scale.value <= 1.02 && mode.value === "contain") {
			if (dismissY.value > 88) {
				animating.value = true;
				dismissing.value = true;
				dismissY.value = Math.max(dismissY.value + 160, 260);
				window.setTimeout(() => hide(), 170);
				return;
			}
			const dx = changed ? changed.clientX - touchStartX : dismissX.value;
			const dy = changed ? changed.clientY - touchStartY : dismissY.value;
			animating.value = true;
			dismissX.value = 0;
			dismissY.value = 0;
			if (!isSingle.value && Math.abs(dx) > 54 && Math.abs(dx) > Math.abs(dy) * 1.2) {
				if (dx < 0) next();
				else prev();
			}
		}
	}
}

function onKeydown(event) {
	switch (event.key) {
		case "Escape":
			if (props.closeOnPressEscape) {
				event.preventDefault();
				event.stopPropagation();
				hide();
			}
			break;
		case " ":
			event.preventDefault();
			toggleMode();
			break;
		case "ArrowLeft":
			event.preventDefault();
			prev();
			break;
		case "ArrowRight":
			event.preventDefault();
			next();
			break;
		case "ArrowUp":
		case "+":
		case "=":
			event.preventDefault();
			handleActions("zoomIn");
			break;
		case "ArrowDown":
		case "-":
			event.preventDefault();
			handleActions("zoomOut");
			break;
	}
}

watch(() => props.initialIndex, (val) => {
	activeIndex.value = Math.max(0, Math.min(val || 0, Math.max(0, props.urlList.length - 1)));
});

watch(currentImg, () => {
	loading.value = true;
	loadError.value = false;
	nextTick(() => {
		if (imgRef.value?.complete && imgRef.value?.naturalWidth > 0) {
			loading.value = false;
		}
	});
});

watch(activeIndex, (val) => {
	mode.value = "contain";
	resetTransform(false);
	emit("switch", val);
});

let previousOverflow = "";
onMounted(() => {
	if (typeof document !== "undefined") {
		previousOverflow = document.body.style.overflow;
		document.body.style.overflow = "hidden";
		window.addEventListener("keydown", onKeydown, true);
	}
	nextTick(() => {
		wrapper.value?.focus?.({preventScroll: true});
		if (imgRef.value?.complete && imgRef.value?.naturalWidth > 0) {
			loading.value = false;
		}
	});
});

onBeforeUnmount(() => {
	onMouseUp();
	if (typeof document !== "undefined") {
		document.body.style.overflow = previousOverflow;
		window.removeEventListener("keydown", onKeydown, true);
	}
});

defineExpose({setActiveItem, prev, next, reset: () => resetTransform(true), toggleMode, handleActions});
</script>

<template>
	<Teleport to="body" :disabled="!teleported">
		<div ref="wrapper"
		     tabindex="-1"
		     role="dialog"
		     aria-modal="true"
		     aria-label="图片预览"
		     class="el-image-viewer__wrapper image-lightbox"
		     :style="{zIndex}"
		     @wheel="onWheel">
			<div class="el-image-viewer__mask image-lightbox-backdrop"
			     :style="{opacity: backdropOpacity}"
			     @click.self="hideOnClickModal && hide()"></div>

			<header class="image-lightbox-topbar">
				<div class="image-lightbox-status">
					<span v-if="!isSingle" class="el-image-viewer__progress image-lightbox-pill" aria-label="图片序号">{{ activeIndex + 1 }} / {{ urlList.length }}</span>
					<button v-if="Math.round(scale * 100) !== 100 || mode === 'original'"
					        type="button"
					        class="image-lightbox-pill is-interactive"
					        title="点击还原到适应屏幕"
					        aria-label="还原缩放比例"
					        @click.stop="handleActions('reset')">{{ zoomPercent }}</button>
				</div>
				<div class="image-lightbox-top-actions">
					<a v-if="currentImg"
					   class="image-lightbox-icon-btn image-lightbox-download"
					   :href="downloadHref"
					   :download="downloadFileName"
					   target="_blank"
					   rel="noopener noreferrer"
					   aria-label="下载原图"
					   title="下载原图"
					   @click.stop="onDownloadClick">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 17v3h14v-3"/></svg>
					</a>
					<button type="button"
					        class="el-image-viewer__btn el-image-viewer__close image-lightbox-icon-btn"
					        aria-label="关闭图片预览"
					        title="关闭（Esc）"
					        @click.stop="hide">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg>
					</button>
				</div>
			</header>

			<template v-if="!isSingle">
				<button type="button"
				        class="el-image-viewer__btn el-image-viewer__prev image-lightbox-nav is-prev"
				        :class="{'is-disabled': !infinite && isFirst}"
				        :disabled="!infinite && isFirst"
				        aria-label="上一张图片"
				        title="上一张"
				        @click.stop="prev">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
				</button>
				<button type="button"
				        class="el-image-viewer__btn el-image-viewer__next image-lightbox-nav is-next"
				        :class="{'is-disabled': !infinite && isLast}"
				        :disabled="!infinite && isLast"
				        aria-label="下一张图片"
				        title="下一张"
				        @click.stop="next">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
				</button>
			</template>

			<div ref="stage"
			     class="el-image-viewer__canvas image-lightbox-stage"
			     @click="onStageBackdropClick"
			     @touchstart="onTouchStart"
			     @touchmove="onTouchMove"
			     @touchend="onTouchEnd"
			     @touchcancel="onTouchEnd">
				<div v-if="loadError" class="image-lightbox-error" role="alert">
					<strong>图片加载失败</strong>
					<a v-if="currentImg" :href="downloadHref" :download="downloadFileName" @click.stop="onDownloadClick">尝试下载原图</a>
				</div>
				<img v-else
				     ref="imgRef"
				     :key="currentImg"
				     :src="currentImg"
				     :crossorigin="crossorigin"
				     :style="imgStyle"
				     class="el-image-viewer__img image-lightbox-img"
				     alt="预览大图"
				     draggable="false"
				     @load="handleImgLoad"
				     @error="handleImgError"
				     @mousedown="onMouseDown"
				     @dblclick="onImgDblClick"/>
			</div>

			<div class="el-image-viewer__btn el-image-viewer__actions image-lightbox-toolbar" role="toolbar" aria-label="图片操作栏" @click.stop>
				<div class="el-image-viewer__actions__inner image-lightbox-toolbar-inner">
					<button type="button" class="image-lightbox-tool" aria-label="缩小图片" title="缩小" @click="handleActions('zoomOut')">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/></svg>
					</button>
					<button type="button" class="image-lightbox-tool is-text" aria-label="切换适应屏幕或原图比例" :title="mode === 'original' || scale > 1.05 ? '适应屏幕' : '原始尺寸'" @click="toggleMode">
						{{ zoomPercent }}
					</button>
					<button type="button" class="image-lightbox-tool" aria-label="放大图片" title="放大" @click="handleActions('zoomIn')">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M11 8v6M8 11h6"/></svg>
					</button>
					<i class="el-image-viewer__actions__divider image-lightbox-divider" aria-hidden="true"></i>
					<button type="button" class="image-lightbox-tool" aria-label="向左旋转" title="向左旋转 90°" @click="handleActions('anticlockwise')">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7v6h6"/><path d="M3.5 13a9 9 0 1 0 2.2-6.3L3 9.5"/></svg>
					</button>
					<button type="button" class="image-lightbox-tool" aria-label="向右旋转" title="向右旋转 90°" @click="handleActions('clockwise')">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 7v6h-6"/><path d="M20.5 13a9 9 0 1 1-2.2-6.3L21 9.5"/></svg>
					</button>
					<i class="el-image-viewer__actions__divider image-lightbox-divider" aria-hidden="true"></i>
					<a v-if="currentImg"
					   class="image-lightbox-tool"
					   :href="downloadHref"
					   :download="downloadFileName"
					   target="_blank"
					   rel="noopener noreferrer"
					   aria-label="下载原图"
					   title="下载原图"
					   @click="onDownloadClick">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 17v3h14v-3"/></svg>
					</a>
				</div>
			</div>
		</div>
	</Teleport>
</template>

<style>
.image-lightbox.el-image-viewer__wrapper {
	position: fixed;
	inset: 0;
	display: flex;
	align-items: center;
	justify-content: center;
	outline: none !important;
	overscroll-behavior: none;
}
.image-lightbox-backdrop.el-image-viewer__mask {
	position: absolute;
	inset: 0;
	background: #0a0a0c;
	transition: opacity 0.16s ease;
}
.image-lightbox-topbar {
	position: absolute;
	top: max(12px, calc(var(--mobile-viewport-top, 0px) + env(safe-area-inset-top, 0px) + 10px));
	left: max(14px, env(safe-area-inset-left, 0px));
	right: max(14px, env(safe-area-inset-right, 0px));
	z-index: 3;
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 10px;
	pointer-events: none;
}
.image-lightbox-status,
.image-lightbox-top-actions {
	display: flex;
	align-items: center;
	gap: 8px;
	pointer-events: auto;
}
.image-lightbox-pill {
	position: static;
	transform: none;
	display: inline-flex;
	align-items: center;
	height: 34px;
	padding: 0 12px;
	border: 1px solid rgb(255 255 255 / 0.14);
	border-radius: 999px;
	background: rgb(22 22 26 / 0.76);
	backdrop-filter: blur(10px);
	color: #f4f4f6;
	font: 600 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	letter-spacing: 0.02em;
}
.image-lightbox-pill.is-interactive {
	cursor: pointer;
}
.image-lightbox-icon-btn {
	position: static !important;
	display: grid !important;
	place-items: center;
	width: 40px !important;
	height: 40px !important;
	padding: 0;
	border: 1px solid rgb(255 255 255 / 0.14);
	border-radius: 50%;
	background: rgb(22 22 26 / 0.78) !important;
	backdrop-filter: blur(10px);
	color: #fff !important;
	text-decoration: none;
	cursor: pointer;
	opacity: 0.94;
	transition: background 0.15s ease, transform 0.12s ease;
}
.image-lightbox-icon-btn:hover {
	background: rgb(38 38 44 / 0.92) !important;
}
.image-lightbox-icon-btn svg {
	width: 19px;
	height: 19px;
}
.image-lightbox-stage.el-image-viewer__canvas {
	position: relative;
	z-index: 1;
	display: flex;
	align-items: center;
	justify-content: center;
	width: 100%;
	height: 100%;
	overflow: hidden;
	touch-action: none;
	user-select: none;
}
.image-lightbox-img {
	display: block;
	object-fit: contain;
	cursor: grab;
	will-change: transform;
	/* Allow mobile browser native long-press save/share callout on the image */
	-webkit-touch-callout: default;
}
.image-lightbox-img:active {
	cursor: grabbing;
}
.image-lightbox-error {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 10px;
	padding: 20px 24px;
	border-radius: 12px;
	background: rgb(28 28 32 / 0.9);
	color: #f4f4f6;
	font-size: 13px;
}
.image-lightbox-error a {
	padding: 6px 12px;
	border-radius: 7px;
	background: rgb(255 255 255 / 0.14);
	color: #fff;
	text-decoration: none;
	font-size: 12px;
}
.image-lightbox-nav {
	z-index: 2 !important;
	width: 42px !important;
	height: 42px !important;
	border: 1px solid rgb(255 255 255 / 0.14);
	background: rgb(22 22 26 / 0.72) !important;
	backdrop-filter: blur(10px);
	color: #fff !important;
}
.image-lightbox-nav.is-prev {
	left: max(14px, env(safe-area-inset-left, 0px)) !important;
}
.image-lightbox-nav.is-next {
	right: max(14px, env(safe-area-inset-right, 0px)) !important;
}
.image-lightbox-nav svg {
	width: 20px;
	height: 20px;
}
.image-lightbox-nav.is-disabled {
	opacity: 0.32 !important;
	cursor: default !important;
}
.image-lightbox-toolbar.el-image-viewer__actions {
	bottom: max(18px, calc(env(safe-area-inset-bottom, 0px) + 12px)) !important;
	z-index: 3 !important;
	width: auto !important;
	max-width: calc(100vw - 24px);
	height: 44px !important;
	padding: 0 8px !important;
	border: 1px solid rgb(255 255 255 / 0.14) !important;
	border-radius: 999px !important;
	background: rgb(22 22 26 / 0.82) !important;
	backdrop-filter: blur(12px);
	opacity: 0.96 !important;
}
.image-lightbox-toolbar-inner.el-image-viewer__actions__inner {
	display: flex;
	align-items: center;
	gap: 4px !important;
	padding: 0 !important;
	font-size: 13px !important;
}
.image-lightbox-tool {
	display: inline-grid;
	place-items: center;
	min-width: 36px;
	height: 34px;
	padding: 0 6px;
	border: 0;
	border-radius: 999px;
	background: transparent;
	color: #f4f4f6;
	text-decoration: none;
	font: 600 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	cursor: pointer;
	transition: background 0.14s ease;
}
.image-lightbox-tool:hover {
	background: rgb(255 255 255 / 0.14);
}
.image-lightbox-tool.is-text {
	min-width: 52px;
	padding: 0 8px;
}
.image-lightbox-tool svg {
	width: 18px;
	height: 18px;
}
.image-lightbox-divider {
	width: 1px;
	height: 16px;
	margin: 0 2px !important;
	background: rgb(255 255 255 / 0.16);
}
@media (max-width: 600px) {
	.image-lightbox-nav {
		width: 36px !important;
		height: 36px !important;
	}
	.image-lightbox-nav.is-prev {
		left: max(8px, env(safe-area-inset-left, 0px)) !important;
	}
	.image-lightbox-nav.is-next {
		right: max(8px, env(safe-area-inset-right, 0px)) !important;
	}
}
</style>
