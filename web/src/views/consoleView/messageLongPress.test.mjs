import test from 'node:test';
import assert from 'node:assert/strict';
import {ref} from 'vue';
import {bindMessageLongPress} from './messageLongPress.js';
function harness() {
  const handlers=new Map(),globalHandlers=new Map(),timers=new Map(),calls=[];
  let sequence=0,phone=true;
  const el={isConnected:true,addEventListener:(n,f)=>handlers.set(n,f),removeEventListener:n=>handlers.delete(n)};
  const viewport={matchMedia:()=>({matches:phone}),addEventListener:(n,f)=>globalHandlers.set(n,f),removeEventListener:n=>globalHandlers.delete(n),setTimeout:(f,ms)=>{assert.ok([450,800].includes(ms));timers.set(++sequence,f);return sequence;},clearTimeout:id=>timers.delete(id)};
  const value={target:{id:'a'},turn:{id:'turn'},visibility:{selecting:ref(false),busy:ref(false),canTarget:()=>true,isHidden:()=>false,openMobileMenu:(...args)=>calls.push(args)}};
  const dispose=bindMessageLongPress(el,()=>value,viewport);
  const event=extra=>({pointerId:1,pointerType:'touch',isPrimary:true,button:0,clientX:80,clientY:120,target:{closest:()=>null},...extra});
  const down=extra=>handlers.get('pointerdown')(event(extra));
  const fire=()=>{for(const f of [...timers.values()])f();};
  return {value,calls,handlers,globalHandlers,timers,el,dispose,down,fire,event,desktop:()=>phone=false};
}
test('450ms hold opens the intended message menu once and consumes the following click',()=>{
  const h=harness();h.down();assert.equal(h.calls.length,0);h.fire();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0][0].id,'a');assert.equal(h.calls[0][1].id,'turn');
  assert.equal(h.globalHandlers.size,3);assert.equal(h.timers.size,1);
  let prevented=0,stopped=0;
  const release={clientX:80,clientY:120,preventDefault:()=>prevented++,stopImmediatePropagation:()=>stopped++};
  h.globalHandlers.get('click')({...release,type:'click',clientX:250});
  assert.equal(prevented,0,'a deliberate tap elsewhere in the sheet is not swallowed');
  for(const type of ['mousedown','mouseup','click']) h.globalHandlers.get(type)({...release,type});
  assert.deepEqual([prevented,stopped],[3,3]);assert.equal(h.globalHandlers.size,0);assert.equal(h.timers.size,0);
  h.dispose();assert.equal(h.handlers.size,0);
});
test('tap, pan, scroll, pointer cancellation and a second finger cancel the hold',()=>{
  for(const [name,extra] of [['pointerup',{}],['pointercancel',{}],['scroll',{}],['pointermove',{clientX:92}],['pointerdown',{pointerId:2}]]){
    const h=harness();h.down();h.globalHandlers.get(name)(h.event(extra));h.fire();assert.equal(h.calls.length,0,name);assert.equal(h.globalHandlers.size,0);h.dispose();
  }
});
test('links, buttons, mouse, multi-select, busy rows and desktop keep their own interaction',()=>{
  for(const mode of ['interactive','mouse','secondary','selecting','busy','desktop']){
    const h=harness();let extra={};
    if(mode==='interactive')extra.target={closest:()=>({})};
    if(mode==='mouse')extra.pointerType='mouse';
    if(mode==='secondary')extra.isPrimary=false;
    if(mode==='selecting')h.value.visibility.selecting.value=true;
    if(mode==='busy')h.value.visibility.busy.value=true;
    if(mode==='desktop')h.desktop();
    h.down(extra);h.fire();assert.equal(h.calls.length,0,mode);assert.equal(h.timers.size,0);h.dispose();
  }
});
test('replaced, hidden or unmounted rows cannot open a stale menu; small finger jitter is tolerated',()=>{
  for(const mode of ['replaced','hidden','removed','dispose','jitter']){
    const h=harness();h.down();
    if(mode==='replaced')h.value.target={id:'b'};
    if(mode==='hidden')h.value.visibility.isHidden=()=>true;
    if(mode==='removed')h.el.isConnected=false;
    if(mode==='dispose')h.dispose();
    if(mode==='jitter')h.globalHandlers.get('pointermove')(h.event({clientX:83,clientY:122}));
    h.fire();assert.equal(h.calls.length,mode==='jitter'?1:0,mode);h.dispose();
  }
});
