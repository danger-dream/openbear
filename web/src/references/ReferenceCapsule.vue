<script setup>
import {computed} from 'vue';
import {referenceChipOpen,referenceChipClose,escapeReferenceHtml} from './presentation.js';
const props=defineProps({reference:{type:Object,required:true},invalid:Boolean});
const emit=defineEmits(['activate']);
const html=computed(()=>referenceChipOpen(props.reference)+escapeReferenceHtml(props.reference.label)+referenceChipClose(props.reference));
</script>
<template><span class="reference-capsule" :class="{'is-invalid':invalid}" v-html="html" @click.stop="emit('activate',$event)" @keydown.enter.stop.prevent="emit('activate',$event)"/></template>
