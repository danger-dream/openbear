import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {computed,nextTick,reactive,ref,watch,effectScope} from 'vue';
import {compareTreeItems} from './conversationTreeInteractions.js';
import {ledgerTokenParts,totalSessionDurationMs} from '../views/consoleView/ledgerUsage.js';
import {startReferenceCatalog,stopReferenceCatalog,watchConversationOverview,applyConversationOverviewPacket} from '../references/catalog.js';

const treeSource=fs.readFileSync(new URL('./ConversationTree.vue',import.meta.url),'utf8');
const strip=source=>source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[\s\S]*?;\n/gm,'');
function hoverHarness(){
 let time=0,serial=0;const timers=new Map();
 const env={computed,nextTick,reactive,ref,compareTreeItems,watch(){},onMounted(){},onBeforeUnmount(){},defineProps:()=>({}),defineEmits:()=>()=>{},defineExpose(){},
  setTimeout:(fn,ms)=>{timers.set(++serial,{fn,at:time+ms});return serial;},clearTimeout:id=>timers.delete(id),
  window:{matchMedia:()=>({matches:true})}};
 const context=vm.createContext(env);
 vm.runInContext(strip(treeSource)+'\nglobalThis.hover={overview,enterOverview,leaveOverview,keepOverview,closeOverview,drag,menu};',context);
 function tick(ms){time+=ms;for(const [id,value] of [...timers])if(value.at<=time){timers.delete(id);value.fn();}}
 const row=id=>({kind:'conversation',conversationUuid:id,title:id});
 const event={pointerType:'mouse',currentTarget:{querySelector:()=>({isConnected:true})}};
 return {...context.hover,tick,row,event,timers};
}
test('hover waits 280ms; crossing the tree never launches hidden overview work',()=>{
 const h=hoverHarness();h.enterOverview(h.event,h.row('a'));h.tick(279);assert.equal(h.overview.value.open,false);
 h.leaveOverview();h.tick(500);assert.equal(h.overview.value.open,false);
 h.enterOverview(h.event,h.row('b'));h.tick(280);assert.equal(h.overview.value.row.conversationUuid,'b');
});
test('pointer may enter the floating card; leaving both cancels it',()=>{
 const h=hoverHarness();h.enterOverview(h.event,h.row('a'));h.tick(280);h.leaveOverview();h.tick(100);h.keepOverview();h.tick(300);assert.equal(h.overview.value.open,true);
 h.leaveOverview();h.tick(160);assert.equal(h.overview.value.open,false);
});
test('new hover supersedes old timer and ignores folders, drafts, touch and drag/menu',()=>{
 const h=hoverHarness();h.enterOverview(h.event,h.row('a'));h.tick(100);h.enterOverview(h.event,h.row('b'));h.tick(280);assert.equal(h.overview.value.row.conversationUuid,'b');h.closeOverview();
 for(const [event,row] of [[h.event,{kind:'folder'}],[h.event,{...h.row('draft'),local:true}],[{...h.event,pointerType:'touch'},h.row('touch')]]){h.enterOverview(event,row);h.tick(500);assert.equal(h.overview.value.open,false);}
 h.drag.value.row=h.row('drag');h.enterOverview(h.event,h.row('a'));h.tick(300);assert.equal(h.overview.value.open,false);h.drag.value.row=null;
 h.menu.value.open=true;h.enterOverview(h.event,h.row('a'));h.tick(300);assert.equal(h.overview.value.open,false);
});
test('subscription nonce rejects switched/closed packets and reconnect re-subscribes only current card',()=>{
 const globals=Object.fromEntries(['window','document','location','WebSocket'].map(key=>[key,globalThis[key]]));const sockets=[];
 class Socket {constructor(){this.readyState=1;this.sent=[];sockets.push(this);}send(text){this.sent.push(JSON.parse(text));}close(){this.readyState=3;}}
 try{
  globalThis.window={addEventListener(){},removeEventListener(){}};globalThis.document={addEventListener(){},removeEventListener(){},visibilityState:'visible'};globalThis.location={protocol:'http:',host:'fixture.invalid'};globalThis.WebSocket=Socket;
  stopReferenceCatalog({clear:true});startReferenceCatalog();const socket=sockets[0];socket.onopen();
  const events=[];const closeA=watchConversationOverview('a',value=>events.push(value));const a=socket.sent.at(-1);
  const closeB=watchConversationOverview('b',value=>events.push(value));const b=socket.sent.at(-1);
  assert.equal(applyConversationOverviewPacket({...a,overview:{old:true}}),false);
  assert.equal(applyConversationOverviewPacket({...b,overview:{current:true}}),true);assert.equal(events.length,1);
  closeA();assert.equal(applyConversationOverviewPacket({...b,overview:{stillCurrent:true}}),true);
  socket.onopen();assert.equal(socket.sent.at(-1).conversationUuid,'b');
  closeB();assert.equal(socket.sent.at(-1).conversationUuid,'');assert.equal(applyConversationOverviewPacket(b),false);
 }finally{stopReferenceCatalog({clear:true});for(const [key,value]of Object.entries(globals)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
test('overview shares header token/duration functions and does not sum overlapping billing',async()=>{
 const source=fs.readFileSync(new URL('./ConversationOverview.vue',import.meta.url),'utf8');
 const props=reactive({open:false,row:null});let listener;const cancellations=[];const cleanup=[];const scope=effectScope();
 const env={computed,ref,watch,defineProps:()=>props,defineEmits:()=>()=>{},onBeforeUnmount:fn=>cleanup.push(fn),ledgerTokenParts,totalSessionDurationMs,
  referenceCatalog:reactive({connected:true,ready:true,stale:false}),watchConversationOverview:(uuid,fn)=>{listener=fn;return ()=>cancellations.push(uuid);}};
 const context=vm.createContext(env);scope.run(()=>vm.runInContext(strip(source)+'\nglobalThis.card={data,error,fresh,tokenParts,duration,currentRunning,stateLabel};',context));const card=context.card;
 try{
  props.row={conversationUuid:'a',running:true,currentStatus:'正在执行工具'};props.open=true;await nextTick();
  assert.equal(card.tokenParts.value,null);assert.equal(card.duration.value,null);
  assert.equal(card.currentRunning.value,true);assert.equal(card.stateLabel.value,'正在执行工具');
  listener({overview:{conversationUuid:'a',usage:{input_tokens:100,output_tokens:20,cache_read_tokens:300,cache_write_tokens:40,cost_usd:1.25,stat_total_time_ms_sum:99999},duration:{timelineTotalDurationMs:12000,modelCallsMs:40,liveMs:0},calls:{model:9,tool:13},messageCount:4}});
  assert.equal(card.tokenParts.value.input,440);assert.equal(card.duration.value,12000);
  props.open=false;await nextTick();assert.equal(card.data.value,null);assert.deepEqual(cancellations,['a']);
  props.open=true;await nextTick();assert.equal(card.data.value.usage.cost_usd,1.25);assert.equal(card.fresh.value,false);
  assert.equal(card.currentRunning.value,true);assert.equal(card.stateLabel.value,'正在执行工具');
  env.referenceCatalog.connected=false;assert.equal(card.stateLabel.value,'连接已断开');
  env.referenceCatalog.connected=true;listener({error:'not_found'});assert.equal(card.data.value,null);assert.equal(card.error.value,'会话已删除或不可访问');
  assert.equal(card.stateLabel.value,'会话已删除或不可访问');assert.equal(card.currentRunning.value,false);
 }finally{for(const fn of cleanup)fn();scope.stop();}
});
test('overview hides scrollbar chrome without disabling overflow and omits the redundant footer',()=>{
 const source=fs.readFileSync(new URL('./ConversationOverview.vue',import.meta.url),'utf8');
 assert.match(source,/overflow:auto;scrollbar-width:none;-ms-overflow-style:none/);
 assert.match(source,/\.conversation-overview::-webkit-scrollbar\{display:none;width:0;height:0\}/);
 assert.doesNotMatch(source,/<footer|overview-loading|overview-notice|正在校准统计|正在读取统计|正在读取状态|等待连接恢复|会话累计 · 与会话内统计同源/);
 assert.doesNotMatch(source,/<template v-if="data">/);
 assert.match(source,/data\?fmtNum\(data\.messageCount\):'—'/);
 assert.match(source,/data\?fmtNum\(data\.calls\?\.model\):'—'/);
 assert.match(source,/data\?fmtNum\(data\.calls\?\.tool\):'—'/);
 assert.match(source,/'is-running':currentRunning&&referenceCatalog.connected/);
 assert.match(source,/role="status"/);
 assert.match(source,/if\(error\.value\)return error\.value/);
});
test('running icon ring reuses work-detail border colors/speed and does not change icon geometry',()=>{
 const detail=fs.readFileSync(new URL('../views/consoleView/ConsoleView.vue',import.meta.url),'utf8');
 assert.match(detail,/border-top-color: #2563eb/);assert.match(treeSource,/border-top-color:#2563eb/);
 assert.match(treeSource,/border-right-color:rgba\(37,99,235,.42\)/);assert.match(treeSource,/tree-work-border-spin .9s linear infinite/);
 assert.match(treeSource,/position:absolute; inset:-3px/);assert.match(treeSource,/'is-working': running\(row\) && !rowLoading\(row\)/);
 assert.match(treeSource,/@media \(prefers-reduced-motion: reduce\) \{ \.is-spinning,\.running-leaf i \{ animation:none; \} \}/);
 assert.doesNotMatch(treeSource,/<el-popover/i);
});
