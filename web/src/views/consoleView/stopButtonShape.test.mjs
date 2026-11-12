import { userStorage } from '../../sessionBoundary.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {compile, createSSRApp, h, nextTick, proxyRefs, reactive, ref, watch} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import postcss from 'postcss';
const source=fs.readFileSync(new URL('./ConsoleComposer.vue',import.meta.url),'utf8');
const descriptor=parse(source).descriptor;
const script=descriptor.scriptSetup.content;
const actual=script.slice(script.indexOf("const STOP_BUTTON_SHAPE_KEY ="),script.indexOf('function getReferenceOrder('));
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[])]);
const templateNodes=walk(baseParse(descriptor.template.content).children);
const button=templateNodes.find(n=>n.type===1&&n.tag==='button'&&n.props.some(p=>p.name==='class'&&p.value?.content==='send-button stop-button'));
const sendButton=templateNodes.find(n=>n.type===1&&n.tag==='button'&&n.props.some(p=>p.name==='class'&&p.value?.content==='send-button'));
const stopBranch=templateNodes.find(n=>n.type===1&&n.tag==='el-tooltip'&&n.children.includes(button));
const sendBranch=templateNodes.find(n=>n.type===1&&n.tag==='template'&&n.children.includes(sendButton));
const renderControl=compile(`${stopBranch.loc.source}\n${sendBranch.loc.source}`);
const render=compile(button.loc.source);
function harness(storage=new Map(), initialProps={}){
  const timers=new Map(), emitted=[], stops=[];let sequence=0;
  const props=reactive({conversationUuid:'a',running:true,draft:'',canSend:false,pendingAttachments:[],...initialProps});
  const context=vm.createContext({userStorage,props,ref,watch:(...args)=>{const stop=watch(...args);stops.push(stop);return stop;},emit:(...args)=>emitted.push(args),
    window:{localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},setTimeout:(fn,ms)=>{timers.set(++sequence,{fn,ms});return sequence;},clearTimeout:id=>timers.delete(id)}});
  vm.runInContext(actual,context);
  const bindings=proxyRefs(vm.runInContext('({props,emit,stopButtonShape,clickStopButton,clickSendButton,buttonShapeKeydown,cancelButtonShapeInteraction,stopButtonContextMenu,startStopShapePress,moveStopShapePress,endStopShapePress,cancelStopShapePress})',context));
  return {props,storage,timers,emitted,bindings,tick(){for(const [id,item] of [...timers]){timers.delete(id);assert.equal(item.ms,550);item.fn();}},dispose(){bindings.cancelStopShapePress();stops.forEach(stop=>stop());},
    async render(){let tree;const app=createSSRApp({render(){tree=render.call(this,bindings,[]);return tree;}});const html=await renderToString(app);return {html,events:tree.props};},
    async renderControl(){
      let nodes=[],tree;
      const app=createSSRApp({render(){tree=renderControl.call(this,bindings,[]);return tree;}});
      app.component('ElTooltip',{inheritAttrs:false,render(){nodes=this.$slots.default?.()||[];return nodes;}});
      app.component('Promotion',{render:()=>h('svg',{'data-icon':'send'})});
      const html=await renderToString(app);
      return {html,events:walk([tree,...nodes]).find(n=>n.type==='button').props};
    }};
}
const pointer=(extra={})=>({pointerType:'touch',pointerId:1,isPrimary:true,button:0,clientX:20,clientY:20,...extra});
const click=(extra={})=>({detail:1,preventDefault(){this.prevented=true;},...extra});

test('default square; right-click changes only the shape, persists across mounts and toggles back',async()=>{
  const storage=new Map();const h=harness(storage);
  try{
    let view=await h.render();assert.doesNotMatch(view.html,/is-round/);
    view.events.onPointerdown(pointer({pointerType:'mouse',button:2}));
    const event=click();view.events.onContextmenu(event);assert.ok(event.prevented);
    view=await h.render();assert.match(view.html,/is-round/);assert.deepEqual(h.emitted,[]);
    const next=harness(storage);try{assert.match((await next.render()).html,/is-round/);}finally{next.dispose();}
    view.events.onPointerdown(pointer({pointerType:'mouse',button:2}));view.events.onContextmenu(click());
    assert.doesNotMatch((await h.render()).html,/is-round/);assert.equal(storage.values().next().value,'square');
  }finally{h.dispose();}
});

for(const contextFirst of [false,true])test(`touch long-press toggles once and never stops (contextmenu ${contextFirst?'first':'last'})`,async()=>{
  const h=harness();try{
    const {events}=await h.render();events.onPointerdown(pointer());
    if(contextFirst)events.onContextmenu(click());
    h.tick();
    if(!contextFirst)events.onContextmenu(click());
    events.onPointerup(pointer());const e=click({pointerType:'touch'});events.onClick(e);
    assert.ok(e.prevented);assert.equal(h.bindings.stopButtonShape,'circle');assert.deepEqual(h.emitted,[]);
    // The next intentional short tap still stops normally.
    events.onPointerdown(pointer());events.onPointerup(pointer());events.onClick(click({pointerType:'touch'}));
    assert.deepEqual(h.emitted,[['stop']]);assert.equal(h.timers.size,0);
  }finally{h.dispose();}
});

test('ordinary click, short touch and keyboard activation preserve the original stop action',async()=>{
  const h=harness();try{
    const {events}=await h.render();
    events.onPointerdown(pointer({pointerType:'mouse'}));events.onClick(click());
    events.onPointerdown(pointer());events.onPointerup(pointer());events.onClick(click({pointerType:'touch'}));
    events.onClick(click({detail:0}));
    assert.deepEqual(h.emitted,[['stop'],['stop'],['stop']]);assert.equal(h.bindings.stopButtonShape,'square');assert.equal(h.storage.size,0);
  }finally{h.dispose();}
});

test('dragging, cancellation, navigation and disappearing stop buttons cancel pending long-press timers',async()=>{
  const h=harness();try{
    const {events}=await h.render();events.onPointerdown(pointer());events.onPointermove(pointer({clientX:45}));h.tick();
    events.onClick(click({pointerType:'touch'}));assert.deepEqual(h.emitted,[]);assert.equal(h.bindings.stopButtonShape,'square');
    for(const type of ['onPointercancel','onPointerleave']){events.onPointerdown(pointer());events[type](pointer());h.tick();assert.equal(h.bindings.stopButtonShape,'square');}
    for(const [key,value] of [['conversationUuid','b'],['draft','new input'],['running',false]]){
      events.onPointerdown(pointer());h.props[key]=value;await nextTick();h.tick();assert.equal(h.timers.size,0);assert.equal(h.bindings.stopButtonShape,'square');
    }
    events.onPointerdown(pointer());h.dispose();h.tick();assert.equal(h.bindings.stopButtonShape,'square');
  }finally{h.dispose();}
});

test('invalid or unavailable localStorage does not break stop or local shape switching',async()=>{
  for(const storage of [new Map([['openbear.console.stopButtonShape.v1','bad']]),{get(){throw new Error('blocked');},set(){throw new Error('blocked');}}]){
    const h=harness(storage);try{
      assert.equal(h.bindings.stopButtonShape,'square');const {events}=await h.render();events.onContextmenu(click());assert.equal(h.bindings.stopButtonShape,'circle');
      events.onPointerdown(pointer({pointerType:'mouse'}));events.onClick(click());assert.deepEqual(h.emitted,[['stop']]);
    }finally{h.dispose();}
  }
});

test('saved shape applies on the first idle render and throughout send/stop branch changes',async()=>{
  const states=[
    {running:false,draft:'',canSend:false},
    {running:false,draft:'待发送',canSend:true},
    {running:true,draft:'',canSend:false},
    {running:true,draft:'补充内容',canSend:true},
    {running:true,draft:'  ',canSend:false},
    // An image alone is an interjection to send, not a reason to stop.
    {running:true,draft:'',canSend:true,pendingAttachments:[{id:'img'}]},
    {running:false,draft:'',canSend:false},
  ];
  for(const shape of ['circle','square']){
    const storage=new Map([['openbear.console.stopButtonShape.v1',shape]]);
    // Every possible entry state gets a fresh setup, as on a page reload.
    for(const initialProps of states){
      const instance=harness(storage,initialProps);
      try{
        assert.equal(instance.bindings.stopButtonShape,shape,'preference is available synchronously, before first render');
        for(const state of [initialProps,...states]){
          Object.assign(instance.props,{pendingAttachments:[],...state});
          const {html,events}=await instance.renderControl();
          const stopping=state.running&&!state.draft.trim()&&!state.pendingAttachments?.length;
          assert.equal(/is-round/.test(html),shape==='circle',JSON.stringify(state));
          assert.equal(/stop-button/.test(html),stopping);
          if(stopping){
            assert.match(html,shape==='circle'?/<rect x="5" y="5" width="14" height="14" rx="1.5"/:/<rect x="4" y="4" width="16" height="16" rx="3"/);
            events.onClick(click());
          }
          else{
            if(shape==='circle'){
              assert.match(html,/<path d="M12 20V4M5 11l7-7 7 7"/);
              assert.match(html,/stroke-width="3"/);
              assert.doesNotMatch(html,/data-icon="send"/);
            }else assert.match(html,/data-icon="send"/);
            assert.equal(events.disabled,undefined);
            assert.equal(events['aria-disabled'],!state.canSend);
            if(state.canSend)events.onClick(click());
          }
        }
        assert.ok(instance.emitted.some(([action])=>action==='send'));
        assert.ok(instance.emitted.some(([action])=>action==='stop'));
        assert.equal(storage.get('openbear.console.stopButtonShape.v1'),shape);
      }finally{instance.dispose();}
    }
  }
});

test('empty idle send button supports repeated desktop right-click without sending',async()=>{
  const instance=harness(new Map(),{running:false,draft:'',canSend:false});
  try{
    let view=await instance.renderControl();assert.equal(view.events.disabled,undefined);assert.equal(view.events['aria-disabled'],true);
    for(const shape of ['circle','square']){
      view.events.onPointerdown(pointer({pointerType:'mouse',button:2}));
      const e=click();view.events.onContextmenu(e);assert.ok(e.prevented);
      assert.equal(instance.bindings.stopButtonShape,shape);
      view=await instance.renderControl();
    }
    view.events.onPointerdown(pointer({pointerType:'mouse'}));view.events.onClick(click());
    view.events.onKeydown({key:'Enter'});view.events.onClick(click({detail:0}));
    assert.deepEqual(instance.emitted,[]);
  }finally{instance.dispose();}
});

for(const canSend of [false,true])for(const contextFirst of [false,true])test(`send long-press works when canSend=${canSend}, contextmenu first=${contextFirst}, without accidental action`,async()=>{
  const instance=harness(new Map(),{running:false,draft:canSend?'draft':'',canSend});
  try{
    const {events}=await instance.renderControl();events.onPointerdown(pointer());
    if(contextFirst)events.onContextmenu(click());instance.tick();
    if(!contextFirst)events.onContextmenu(click());
    // An ignored second finger must not reset suppression, even when click has
    // detail=0 and no pointerType (legacy touch click representation).
    events.onPointerdown(pointer({pointerId:2,isPrimary:false}));
    events.onPointerup(pointer({pointerId:2,isPrimary:false}));events.onPointerup(pointer());
    const e=click({detail:0});events.onClick(e);
    assert.ok(e.prevented);assert.equal(instance.bindings.stopButtonShape,'circle');assert.deepEqual(instance.emitted,[]);
    events.onPointerdown(pointer());events.onPointerup(pointer());events.onClick(click({pointerType:'touch'}));
    assert.deepEqual(instance.emitted,canSend?[['send']]:[]);
    assert.equal(instance.storage.get('openbear.console.stopButtonShape.v1'),'circle');
  }finally{instance.dispose();}
});

test('send gesture cancels on dragging or branch changes and keyboard activation remains intentional',async()=>{
  const instance=harness(new Map(),{running:true,draft:'draft',canSend:true});
  try{
    let {events}=await instance.renderControl();events.onPointerdown(pointer());events.onPointermove(pointer({clientX:45}));instance.tick();
    events.onClick(click());assert.deepEqual(instance.emitted,[]);assert.equal(instance.bindings.stopButtonShape,'square');
    events.onPointerdown(pointer());instance.props.draft='';instance.props.canSend=false;await nextTick();instance.tick();
    assert.equal(instance.timers.size,0);assert.equal(instance.bindings.stopButtonShape,'square');
    ({events}=await instance.renderControl());assert.ok(events.class.includes('stop-button'));
    events.onPointerup(pointer());const late=click();events.onClick(late);assert.ok(late.prevented);assert.deepEqual(instance.emitted,[]);
    // Right-click can be followed by a genuinely new keyboard stop, rather than
    // mistaking a touch-generated zero-detail click for keyboard activation.
    events.onPointerdown(pointer({pointerType:'mouse',button:2}));events.onContextmenu(click());
    events.onKeydown({key:'Enter'});events.onClick(click({detail:0}));assert.deepEqual(instance.emitted,[['stop']]);
    instance.props.running=false;instance.props.draft='ready';instance.props.canSend=true;await nextTick();
    ({events}=await instance.renderControl());events.onKeydown({key:' '});events.onClick(click({detail:0}));
    assert.deepEqual(instance.emitted,[['stop'],['send']]);
  }finally{instance.dispose();}
});

test('circle appearance is flat with a larger arrow, retaining dimensions, theme and send-disabled appearance',()=>{
  const css=postcss.parse(descriptor.styles.map(s=>s.content).join('\n'));
  const rules=selector=>{const found=[];css.walkRules(rule=>{if(rule.selectors.includes(selector))found.push(Object.fromEntries(rule.nodes.filter(n=>n.type==='decl').map(n=>[n.prop,n.value])));});return found;};
  assert.deepEqual(rules('.send-button.is-round'),[{'border-radius':'50%','box-shadow':'none'}]);
  assert.deepEqual(rules('.send-button.is-round:not(.stop-button) svg'),[{width:'60%',height:'60%'}]);
  const sizes=rules('.send-button');
  assert.equal(sizes[0].width,'2rem');assert.equal(sizes[0].height,'2rem');
  assert.equal(sizes[0]['border-radius'],'8px');assert.equal(sizes[0]['box-shadow'],'var(--ob-shadow-panel)');
  assert.equal(sizes[0].background,'var(--ob-chat-button)');assert.equal(sizes[0].color,'var(--ob-chat-button-text)');
  assert.deepEqual(sizes.at(-1),{width:'38px',height:'38px'});
  assert.equal(rules('.stop-button')[0].background,'var(--ob-danger)');
  assert.equal(rules('.send-button:disabled')[0].background,'var(--ob-text-disabled)');
  assert.equal(rules('.send-button[aria-disabled="true"]')[0].background,'var(--ob-text-disabled)');
  assert.match(sendButton.loc.source,/:aria-disabled="!props.canSend"/);
  assert.match(script,/onBeforeUnmount\(\(\) => \{\s*cancelStopShapePress\(\)/);
});
