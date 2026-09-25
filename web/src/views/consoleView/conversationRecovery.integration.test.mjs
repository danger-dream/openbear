import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {applyOperationFrame,isRootRunTerminalFrame,isTerminalOperationFrame,normalizeOperations,shouldApplyOperationFrame} from '../../timelineProjection.js';
import {mergeOperationSnapshots} from './timelinePagination.js';
import {createOperationFrameBuffer} from './operationFrameBuffer.js';

// Actual component recovery functions and reducer, with only transport/UI seams.
const source=fs.readFileSync(new URL('./ConsoleView.vue',import.meta.url),'utf8');
function between(start,end){
  const a=source.indexOf(start),b=source.indexOf(end,a+start.length);
  assert.ok(a>=0&&b>a,`${start}..${end}`);return source.slice(a,b);
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function harness(){
  const requests=[],loads=[],connections=[],flushes=[],timers=new Map();let nextTimer=0;
  const props={conversationUuid:'A'};
  const snapshot=deferred();
  const context=vm.createContext({
    Map,props,console:{warn(){}},applyOperationFrame,normalizeOperations,mergeOperationSnapshots,shouldApplyOperationFrame,
    stateStatsByOpId:new Map(),chatState:{value:null},
    mergeLedgerUsageIntoState(){},operationScrollImpact:()=> 'none',mergeScrollImpact:()=> 'none',
    isRootRunTerminalFrame,isTerminalOperationFrame,scheduleProjectedMessagesFlush:options=>flushes.push(options),
    operationFrameBuffer:createOperationFrameBuffer(),conversationStateRequests:{pending:false},
    document:{visibilityState:'visible'},
    window:{setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id)},
    activeConversationUuid:{get value(){return props.conversationUuid;}},isLocalConversation:{value:false},
    operationsById:{value:new Map()},orderedOpIds:{value:[]},revisionByOpId:{value:new Map()},lastFrameSeq:{value:40000},
    debugFrames(){},operationDebugRow:x=>x,
    outboundSends:{current:null,checkDeadline(){}},
    Api:{conversationFrames:(uuid,after,limit)=>{const request={uuid,after,limit,...deferred()};requests.push(request);return request.promise;}},
    load:options=>{loads.push(options);return snapshot.promise;},
    connectWs:async uuid=>{connections.push(uuid);return vm.runInContext('ws',context);},
    waitForSocketOpen:async()=>{},probeSocket:async()=>{},closeWs(){},
  });
  vm.runInContext(`
    let operationResyncInFlight=null,operationResyncTimer=null,pendingLoadReplaceOperations=false;
    let pendingProjectionOps=null,pendingScrollImpact='none',pendingTerminalFrame=null;
    const orderedOperationsList=()=>[...operationsById.value.values()];
    ${between('function replaceOperationSnapshots(', 'function applyTimelinePageMetadata(')}
    let componentMounted=true,ws={id:'socket-a'},wsConversationUuid='A';
    let timelinePageInitialized=true,timelinePageConversationUuid='A';
    let sendAttemptGeneration=0,connectionResumePromise=null;
    ${between('async function resyncOperationFrames(', 'const terminalStateRefreshScheduler =')}
    ${between('function applyOperationFrameMessage(', 'function textSignal(')}
    ${between('function replayBufferedOperationFrames(', 'async function load(')}
    ${between('function handleWsMessage(', 'async function connectWs(')}
    ${between('function checkConnectionOnResume()', 'async function ensureServerConversationForSend(')}
  `,context);
  const run=code=>vm.runInContext(code,context);
  return {context,props,requests,loads,connections,flushes,timers,snapshot,run};
}
const missingFrames=()=>Array.from({length:1000},(_,i)=>({opId:`retained-tool-${i}`,opType:'tool',action:'end',revision:2,frameSeq:i+11,displaySeq:i+60,payload:{status:'completed'}}));

test('terminal frame arriving before its HTTP base is retained, then replayed without skipping its cursor',async()=>{
  const h=harness();const recovering=h.run('resyncOperationFrames({requiresFullState:true})');
  h.run(`handleWsMessage(JSON.stringify({type:'frame',frame:{opId:'tool-new',opType:'tool',action:'end',revision:2,frameSeq:40002,displaySeq:1,payload:{status:'completed'}}}))`);
  assert.equal(h.context.lastFrameSeq.value,40000,'unapplied terminal is not acknowledged');
  assert.equal(h.context.operationFrameBuffer.size,1);
  h.run(`loadOperationsFromState({frameSeq:40001,operations:[{opId:'tool-new',opType:'tool',revision:1,displaySeq:1,status:'running',lifecycle:'active',payload:{status:'running'}}]},{merge:true});replayBufferedOperationFrames(40001)`);
  assert.equal(h.context.operationsById.value.get('tool-new').revision,2);
  assert.equal(h.context.operationsById.value.get('tool-new').status,'completed');
  assert.equal(h.context.lastFrameSeq.value,40002);assert.equal(h.context.operationFrameBuffer.blocked,false);
  h.snapshot.resolve({applied:true});await recovering;
  assert.equal(h.loads.length,1);assert.equal(h.timers.size,0);
});

test('ordinary terminal frames still flush projection immediately without becoming state refresh boundaries',()=>{
  const h=harness();
  h.context.frames=Array.from({length:1322},(_,index)=>({
    opId:`tool:${index}`,opType:'tool',action:'end',revision:1,frameSeq:40001+index,
    displaySeq:index+1,targetType:'run',turnUuid:'root-turn',runRootTurnId:'root-turn',payload:{status:'completed'},
  }));
  h.run('for(const frame of frames)applyOperationFrameMessage(frame)');
  assert.equal(h.flushes.length,1322);
  assert.equal(h.flushes.every(options=>options.force===true),true);
  assert.equal(h.run('pendingTerminalFrame'),null);

  h.run(`applyOperationFrameMessage({
    opId:'run:root',opType:'run',action:'end',revision:1,frameSeq:50000,displaySeq:2000,
    targetType:'run',turnUuid:'root-turn',runRootTurnId:'root-turn',payload:{status:'completed'}
  })`);
  assert.equal(h.flushes.at(-1).force,true);
  assert.equal(h.run('pendingTerminalFrame.frame.opType'),'run');
});

test('failed snapshot leaves buffered data recoverable but does not start a zero-delay retry loop',async()=>{
  const h=harness();h.context.operationFrameBuffer.block({frameSeq:40002});
  const recovery=h.run('resyncOperationFrames({requiresFullState:true})');
  h.snapshot.resolve({applied:false});await recovery;
  assert.equal(h.loads.length,1);assert.equal(h.timers.size,0);assert.equal(h.context.operationFrameBuffer.blocked,true);
});

test('old probe failure cannot close the same conversation healthy replacement socket',async()=>{
  const h=harness(),probe=deferred(),started=deferred(),closed=[];
  h.context.probeSocket=()=>{started.resolve();return probe.promise;};
  h.context.closeWs=()=>closed.push(h.run('ws.id'));
  h.run('checkConnectionOnResume()');await started.promise;
  h.run("ws={id:'healthy-replacement'}");probe.reject(new Error('old lost'));
  await h.run('connectionResumePromise');
  assert.deepEqual(closed,[]);assert.equal(h.loads.length,0);
});

test('1000 retained deltas without their base stop replay and request exactly one full snapshot',async()=>{
  const h=harness();
  const recovery=h.run('resyncOperationFrames({afterFrameSeq:0})');
  h.requests[0].resolve({frames:missingFrames(),frameSeq:9433});await tick();
  assert.equal(h.loads.length,1,'never launch one HTTP state request per missing-base frame');
  assert.equal(h.requests.length,1,'do not continue replaying obsolete pages while state is reloading');
  h.snapshot.resolve();await recovery;
  assert.equal(h.loads[0].conversationUuid,'A');
});

test('concurrent recovery signals coalesce while full state is in flight',async()=>{
  const h=harness();const recovery=h.run('resyncOperationFrames({requiresFullState:true})');
  for(let i=0;i<100;i++)h.run('resyncOperationFrames({requiresFullState:true})');
  assert.equal(h.loads.length,1);h.snapshot.resolve();await recovery;
});

test('actual websocket resync messages share the recovery gate and retain authoritative reset semantics',async()=>{
  const h=harness();
  for(let i=0;i<100;i++)h.run('handleWsMessage(JSON.stringify({type:"resync_required",resetOperations:true}))');
  assert.equal(h.loads.length,1);
  assert.equal(h.loads[0].replaceOperations,true);
  assert.equal(h.loads[0].conversationUuid,'A');
  h.snapshot.resolve();await tick();
  assert.equal(h.run('operationResyncInFlight'),null);
});

test('ordinary replay stays incremental and terminates without a redundant full state request',async()=>{
  const h=harness(),applied=[];
  h.context.applyOperationFrameMessage=frame=>{applied.push(frame.frameSeq);return {applied:true};};
  const recovery=h.run('resyncOperationFrames({afterFrameSeq:40})');
  h.requests[0].resolve({frames:[{frameSeq:41},{frameSeq:42}],frameSeq:42});await recovery;
  assert.deepEqual(applied,[41,42]);assert.equal(h.loads.length,0);
});

test('live missing-base frames cannot queue another replay while full state is already recovering',async()=>{
  const h=harness();const recovery=h.run('resyncOperationFrames({requiresFullState:true})');
  h.context.frames=missingFrames();
  h.run('for(const frame of frames)applyOperationFrameMessage(frame)');
  assert.equal(h.timers.size,0);assert.equal(h.loads.length,1);
  h.snapshot.resolve();await recovery;
});

test('late frame response after A -> B -> A cannot touch the new socket or launch recovery',async()=>{
  const h=harness();const recovery=h.run('resyncOperationFrames({afterFrameSeq:0})');
  h.run("ws={id:'new-visit-a'}; operationResyncInFlight=null");
  h.requests[0].resolve({frames:missingFrames(),frameSeq:9433});await tick();
  assert.equal(h.loads.length,0);assert.equal(h.requests.length,1);
  await recovery;
});

test('old recovery finalizer cannot unlock a newer recovery after socket replacement',async()=>{
  const h=harness();const old=h.run('resyncOperationFrames({afterFrameSeq:0})');
  h.run("ws={id:'new-visit-a'}; operationResyncInFlight=null");
  const current=h.run('resyncOperationFrames({requiresFullState:true})');
  h.requests[0].resolve({frames:[],frameSeq:0});await old;
  h.run('resyncOperationFrames({requiresFullState:true})');
  assert.equal(h.loads.length,1,'old finally must not release the current single-flight gate');
  h.snapshot.resolve();await current;
});

test('mobile resume waits for the selected conversation HTTP snapshot, including zero-frame empty conversations',async()=>{
  const h=harness();h.run('timelinePageInitialized=false; lastFrameSeq.value=0');
  for(let i=0;i<4;i++)h.run('checkConnectionOnResume()');await tick();
  assert.equal(h.connections.length,0,'no from-zero websocket while initial snapshot is pending');
  h.run("timelinePageInitialized=true;timelinePageConversationUuid='B';checkConnectionOnResume()");await tick();
  assert.equal(h.connections.length,0,'an old conversation snapshot is not a baseline');
  h.run("timelinePageConversationUuid='A';checkConnectionOnResume()");await tick();
  assert.deepEqual(h.connections,['A'],'a legitimate loaded empty conversation may connect with zero cursor');
});

test('unmount while frame request is in flight neither applies nor launches a state request',async()=>{
  const h=harness();const recovery=h.run('resyncOperationFrames({afterFrameSeq:0})');
  h.run('componentMounted=false');h.requests[0].reject(new Error('offline'));await tick();
  assert.equal(h.loads.length,0);await recovery;
});
