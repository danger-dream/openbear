<script setup>
import {NodeViewWrapper,nodeViewProps} from '@tiptap/vue-3';
import {computed} from 'vue';
import {referenceKey} from './codec.js';
import {referenceNodes} from './editorPolicy.js';
import {applyConversationPolicy} from './sizePolicy.js';
import {referencePolicyOptions} from './catalog.js';
import ReferenceCapsule from './ReferenceCapsule.vue';
const props=defineProps(nodeViewProps);
const invalid=computed(()=>props.editor.storage.reference.invalid.has(referenceKey(props.node.attrs)));
function updateReference(value){
 const position=props.getPos(),existing=referenceNodes(props.editor.state.doc).filter(item=>item.pos!==position).map(item=>item.ref);
 const next=applyConversationPolicy([value],existing,referencePolicyOptions())[0];
 if(next)props.updateAttributes({...next,mode:next.mode||'',modeReason:next.modeReason||'',bodyChars:next.bodyChars??'',contentLimit:next.contentLimit??''});
 return next;
}
function inspect(event){
 const target=event.target?.closest?.('.reference-chip')||event.currentTarget;
 window.dispatchEvent(new CustomEvent('openbear:inspect-reference',{detail:{reference:{...props.node.attrs},anchor:target?.getBoundingClientRect(),updateReference}}));
}
</script>
<template><NodeViewWrapper as="span" class="reference-inline-node" contenteditable="false"><ReferenceCapsule :reference="node.attrs" :invalid="invalid" @activate="inspect"/></NodeViewWrapper></template>
