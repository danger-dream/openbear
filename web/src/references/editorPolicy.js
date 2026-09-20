import {referenceKey,boundReference} from './codec.js';
import {applyConversationPolicy} from './sizePolicy.js';

export const SIZE_POLICY_META='reference-size-policy';
export function referenceNodes(doc) {
 const found=[];doc.descendants((node,pos)=>{if(node.type.name==='reference')found.push({node,pos,ref:node.attrs});});return found;
}
export function referenceOrder(previous,doc) {
 const keys=[...new Set(referenceNodes(doc).map(item=>referenceKey(item.ref)))];
 return [...previous.filter(key=>keys.includes(key)),...keys.filter(key=>!previous.includes(key))];
}
export function enforceNewReferencePolicy(state,oldDoc,options) {
 const current=referenceNodes(state.doc),keys=new Set(current.map(item=>referenceKey(item.ref)));
 const existing=referenceNodes(oldDoc).map(item=>item.ref).filter(ref=>keys.has(referenceKey(ref)));
 const oldKeys=new Set(existing.map(referenceKey));
 const added=current.filter(item=>!oldKeys.has(referenceKey(item.ref)));
 const values=applyConversationPolicy(added.map(item=>item.ref),existing,options);
 let tr=state.tr;
 added.forEach(({node,pos},index)=>{const value=values[index];if(value&&((node.attrs.mode||'content')!==(value.mode||'content')||(node.attrs.modeReason||'')!==(value.modeReason||'')))tr=tr.setNodeMarkup(pos,undefined,{...node.attrs,...value,mode:value.mode||'',modeReason:value.modeReason||''});});
 return tr.docChanged?tr.setMeta(SIZE_POLICY_META,true):null;
}
export function applyPreviewBindings(state,snapshot,bindings) {
 // A late response cannot replace a newer edit, selection, or whole draft.
 if(!state.doc.eq(snapshot))return null;
 let tr=state.tr;
 for(const {node,pos,ref} of referenceNodes(state.doc)){
  const binding=bindings.find(item=>(item.requestedKey||item.key)===referenceKey(ref));
  if(!binding)continue;
  const value=boundReference(ref,binding),attrs={...node.attrs,...value,mode:value.mode||'',modeReason:value.modeReason||''};
  if(Object.keys(attrs).some(key=>attrs[key]!==node.attrs[key]))tr=tr.setNodeMarkup(pos,undefined,attrs);
 }
 return tr.docChanged?tr.setMeta(SIZE_POLICY_META,true).setMeta('addToHistory',false):null;
}
