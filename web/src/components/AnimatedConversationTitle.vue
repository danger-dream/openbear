<script setup>
import {onBeforeUnmount, onMounted, ref, watch} from "vue";
import {titleGraphemes} from "../conversationTitle.js";

const props = defineProps({
  text: {type: String, default: ""},
  identity: {type: String, default: ""},
});

const shown = ref(String(props.text || ""));
const blurring = ref(false);
let mounted = false;
let blurTimer = null;
let typingTimer = null;
let generation = 0;

function reducedMotion() {
  return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
}
function clearAnimation() {
  if (blurTimer) window.clearTimeout(blurTimer);
  if (typingTimer) window.clearTimeout(typingTimer);
  blurTimer = typingTimer = null;
}
function reveal(next, currentGeneration) {
  const graphemes = titleGraphemes(next);
  let index = 0;
  shown.value = "";
  const type = () => {
    if (currentGeneration !== generation) return;
    index += 1;
    shown.value = graphemes.slice(0, index).join("");
    if (index < graphemes.length) typingTimer = window.setTimeout(type, 50);
    else typingTimer = null;
  };
  if (graphemes.length) type();
}

watch(() => [props.identity, props.text], ([identity, value], [previousIdentity, previous] = []) => {
  const next = String(value || "");
  clearAnimation();
  generation += 1;
  // Switching to another conversation is navigation, not a title change. Render
  // its existing title immediately; animate only a rename of the same identity.
  if (!mounted || String(identity || "") !== String(previousIdentity || "")
    || next === String(previous || "") || reducedMotion()) {
    blurring.value = false;
    shown.value = next;
    return;
  }
  const currentGeneration = generation;
  blurring.value = true;
  blurTimer = window.setTimeout(() => {
    if (currentGeneration !== generation) return;
    blurring.value = false;
    blurTimer = null;
    reveal(next, currentGeneration);
  }, 140);
}, {flush: "post"});

onMounted(() => { mounted = true; shown.value = String(props.text || ""); });
onBeforeUnmount(() => { generation += 1; clearAnimation(); });
</script>

<template>
  <span class="animated-conversation-title" :aria-label="props.text">
    <span class="animated-title-reserve" aria-hidden="true">{{ props.text }}</span>
    <span class="animated-title-visible" :class="{'is-blurring': blurring}" aria-hidden="true">{{ shown }}</span>
  </span>
</template>

<style scoped>
.animated-conversation-title {
  display: inline-grid;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  vertical-align: bottom;
}
.animated-title-reserve,
.animated-title-visible {
  grid-area: 1 / 1;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.animated-title-reserve { visibility: hidden; }
.animated-title-visible {
  transition: filter 140ms ease, opacity 140ms ease;
}
.animated-title-visible.is-blurring {
  filter: blur(5px);
  opacity: .24;
}
@media (prefers-reduced-motion: reduce) {
  .animated-title-visible { transition: none; }
}
</style>
