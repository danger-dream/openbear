<script setup>
import {NodeViewWrapper,nodeViewProps} from '@tiptap/vue-3';
import {computed} from 'vue';
import {referenceKey} from './codec.js';
import ReferenceCapsule from './ReferenceCapsule.vue';
const props=defineProps(nodeViewProps);
const invalid=computed(()=>props.editor.storage.reference.invalid.has(referenceKey(props.node.attrs)));
function inspect(event){
 const target=event.target?.closest?.('.reference-chip')||event.currentTarget;
 window.dispatchEvent(new CustomEvent('openbear:inspect-reference',{detail:{reference:{...props.node.attrs},anchor:target?.getBoundingClientRect(),updateReference:value=>props.updateAttributes(value)}}));
}
</script>
<template><NodeViewWrapper as="span" class="reference-inline-node" contenteditable="false"><ReferenceCapsule :reference="node.attrs" :invalid="invalid" @activate="inspect"/></NodeViewWrapper></template>
