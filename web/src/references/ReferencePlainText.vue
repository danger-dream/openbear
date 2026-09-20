<script setup>
import {computed} from 'vue';
import ReferenceCapsule from './ReferenceCapsule.vue';
import {parseReferenceText,referenceKey,boundReference} from './codec.js';
const props=defineProps({text:{type:String,default:''},references:{type:Array,default:()=>[]},bundleId:{type:String,default:''}});
const parts=computed(()=>{let ordinal=0;return parseReferenceText(props.text).map(part=>part.type==='reference'?{...part,attrs:boundReference(part.attrs,props.references[ordinal]),ordinal:ordinal++}:part);});
function inspect(part,event){const chip=event.target?.closest?.('[data-reference]');const binding=props.references[part.ordinal];window.dispatchEvent(new CustomEvent('openbear:inspect-reference',{detail:{reference:part.attrs,anchor:chip?.getBoundingClientRect(),bundleId:binding?.key===referenceKey(part.attrs)?binding.bundleId:props.bundleId}}));}
</script>
<template><span class="reference-inline-text"><template v-for="(part,index) in parts" :key="index"><ReferenceCapsule v-if="part.type==='reference'" :reference="part.attrs" @activate="inspect(part,$event)"/><template v-else>{{part.text}}</template></template></span></template>
