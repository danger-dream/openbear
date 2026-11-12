<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue';
import * as L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import WorldMap from './WorldMap.vue';
import { homeBox, pinOf, unproject } from './mapPlace.js';

// Where an address is, on OpenStreetMap's map: brought in to its city when its coordinates are known, to
// its country when only that is (pinned at the country's label point, mapPlace.js). Zoomed with the buttons;
// with the wheel only after a click on the map, until the pointer leaves it, so that scrolling the page past
// it does not zoom it; on a touch screen with two fingers, one finger scrolling the page. Where no tile
// loads (OpenStreetMap out of reach), the built-in map instead (WorldMap.vue).
const props = defineProps({lat: {type: Number, default: null}, lon: {type: Number, default: null}, country: {type: String, default: ''}});
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
const CITY = 10;
// No tile loaded this long after the map is made, or this many failed first: out of reach.
const WAIT_MS = 8000, ERRORS = 3;
const el = ref(null), failed = ref(false), hint = ref('');
const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
let map = null, resize = null, waiting = 0, hinting = 0;

function fail() {
  clearTimeout(waiting);
  resize?.disconnect();
  map?.remove();
  map = null;
  failed.value = true;
}

function show(text) {
  hint.value = text;
  clearTimeout(hinting);
  hinting = setTimeout(() => { hint.value = ''; }, 1600);
}

function view() {
  const pin = pinOf(props.lat, props.lon, props.country);
  if (props.lat != null && props.lon != null) return map.setView([props.lat, props.lon], CITY);
  const box = homeBox(props.country, pin);
  if (box) return map.fitBounds([unproject([box.x0, box.y1]), unproject([box.x1, box.y0])], {padding: [24, 24], maxZoom: CITY});
  map.setView(pin ? unproject(pin) : [20, 0], pin ? CITY : 1);
}

onMounted(() => {
  map = L.map(el.value, {zoomControl: false, attributionControl: false, scrollWheelZoom: false, dragging: !coarse, touchZoom: true, boxZoom: false, worldCopyJump: true, minZoom: 1});
  L.control.zoom({position: 'topright', zoomInTitle: '放大', zoomOutTitle: '缩小'}).addTo(map);
  L.control.attribution({position: 'bottomright', prefix: false}).addTo(map);
  let loaded = 0, errors = 0;
  L.tileLayer(TILES, {maxZoom: 19, attribution: ATTRIBUTION})
    .on('tileload', () => { loaded++; clearTimeout(waiting); })
    .on('tileerror', () => { if (!loaded && ++errors >= ERRORS) fail(); })
    .addTo(map);
  const pin = pinOf(props.lat, props.lon, props.country);
  if (pin) L.marker(props.lat != null && props.lon != null ? [props.lat, props.lon] : unproject(pin),
    {icon: L.divIcon({className: 'ipm-pin', html: '<i></i>', iconSize: [16, 16]}), interactive: false, keyboard: false}).addTo(map);
  view();
  map.on('click', () => map.scrollWheelZoom.enable());
  waiting = setTimeout(() => { if (!loaded) fail(); }, WAIT_MS);
  if (typeof ResizeObserver === 'function') {
    resize = new ResizeObserver(() => map?.invalidateSize());
    resize.observe(el.value);
  }
});
onBeforeUnmount(() => {
  clearTimeout(waiting);
  clearTimeout(hinting);
  resize?.disconnect();
  map?.remove();
  map = null;
});

const onWheel = () => { if (map && !map.scrollWheelZoom.enabled()) show('先点一下地图，再用滚轮缩放'); };
const onLeave = () => map?.scrollWheelZoom.disable();
const onTouch = event => { if (coarse && event.touches.length === 1) show('用双指移动和缩放地图'); };
</script>
<template>
  <WorldMap v-if="failed" :lat="lat" :lon="lon" :country="country" />
  <div v-else class="ipm" @wheel.passive="onWheel" @mouseleave="onLeave" @touchmove.passive="onTouch">
    <div ref="el" class="ipm-map" role="application" aria-label="位置地图" />
    <p v-if="hint" class="ipm-hint" role="status">{{ hint }}</p>
  </div>
</template>
