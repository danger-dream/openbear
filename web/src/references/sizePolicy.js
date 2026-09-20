import {normalizeReference,referenceKey} from './codec.js';

export const conversationKinds=new Set(['chat','turn','message']);
export function policyReasonText(reason) {
 return reason==='conversation_too_long'?'会话过长，将仅提及':reason==='conversation_total_limit'?'合计超限，将仅提及':'';
}
export function policyReasonTitle(ref) {
 const text=policyReasonText(ref?.modeReason);
 return text?`${text} · 所选正文 ${Number(ref.bodyChars||0).toLocaleString()} 字符 · 每条消息上限 ${Number(ref.contentLimit||0).toLocaleString()} 字符`:'';
}

// The cap comes exclusively from the server. Reserve existing references before
// considering new ones, regardless of their insertion position in the document.
export function applyConversationPolicy(items,existing=[],{limit=0,sizeOf=ref=>ref.bodyChars}={}) {
 let used=0;
 const accepted=new Set(),decisions=new Map();
 for(const input of existing){const ref=normalizeReference(input);if(!ref||!conversationKinds.has(ref.kind)||ref.mode==='mention')continue;
  const key=referenceKey(ref);if(accepted.has(key))continue;accepted.add(key);used+=Number(sizeOf(input)||0);
 }
 return items.map(input=>{
  const ref=normalizeReference(input);if(!ref)return null;
  const key=referenceKey(ref),chars=sizeOf(input);
  if(decisions.has(key))return {...decisions.get(key),label:ref.label};
  const value={...ref,...(Number.isFinite(chars)?{bodyChars:chars}:{}),...(limit>0?{contentLimit:limit}:{})};
  if(conversationKinds.has(ref.kind)&&ref.mode!=='mention'&&!accepted.has(key)){
   if(limit>0&&Number.isFinite(chars)){
    const reason=chars>limit?'conversation_too_long':used+chars>limit?'conversation_total_limit':'';
    if(reason)Object.assign(value,{mode:'mention',modeReason:reason});
    else {accepted.add(key);used+=chars;}
   }
  }
  decisions.set(key,value);return value;
 }).filter(Boolean);
}
