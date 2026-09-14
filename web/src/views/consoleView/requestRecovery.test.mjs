import test from 'node:test';
import assert from 'node:assert/strict';
import {createConversationStateRequests} from './conversationStateRequests.js';
import {createOperationFrameBuffer} from './operationFrameBuffer.js';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function requests(){const calls=[];const gate=createConversationStateRequests((uuid,signal)=>{const item={uuid,signal,...deferred()};calls.push(item);return item.promise;});return {calls,gate};}

test('identical reads share transport, mutations queue at most one newer read',async()=>{
  const {calls,gate}=requests();const first=gate.request('A');
  assert.equal(gate.request('A'),first);
  const fresh=gate.request('A',{fresh:true});
  for(let i=0;i<100;i++)assert.equal(gate.request('A',{fresh:true}),fresh);
  assert.equal(calls.length,1);calls[0].resolve('old');await first;await tick();assert.equal(calls.length,2);
  calls[1].resolve('new');assert.equal(await fresh,'new');await tick();assert.equal(gate.pending,false);
});

test('visit invalidation aborts transport and rejects queued work without issuing it; late completion cannot revive it',async()=>{
  const {calls,gate}=requests();const first=gate.request('A'),queued=gate.request('A',{fresh:true});
  const rejected=Promise.all([assert.rejects(first,{name:'AbortError'}),assert.rejects(queued,{name:'AbortError'})]);
  gate.invalidate();await rejected;
  assert.equal(calls[0].signal.aborted,true);assert.equal(gate.pending,false);
  const next=gate.request('A');calls[0].resolve('late old visit');await tick();assert.equal(calls.length,2);
  calls[1].resolve('new visit');assert.equal(await next,'new visit');
});

test('failed read releases its gate and does not retry until explicitly requested',async()=>{
  const {calls,gate}=requests();const first=gate.request('A');const rejected=assert.rejects(first,/offline/);
  calls[0].reject(new Error('offline'));await rejected;await tick();
  assert.equal(gate.pending,false);assert.equal(calls.length,1);
  const next=gate.request('A');calls[1].resolve('recovered');assert.equal(await next,'recovered');
});

test('frame buffer replays after authoritative baseline in order, preserving subsequent frames at a gap',()=>{
  const buffer=createOperationFrameBuffer();for(const frameSeq of [12,10,11])buffer.add({frameSeq});
  const first=[];assert.equal(buffer.replayAfter(9,frame=>{first.push(frame.frameSeq);return frame.frameSeq===11?{needsResync:true}:{};}),false);
  assert.deepEqual(first,[10,11]);assert.equal(buffer.size,2);assert.equal(buffer.blocked,true);
  const later=[];assert.equal(buffer.replayAfter(11,frame=>{later.push(frame.frameSeq);return {};}),true);
  assert.deepEqual(later,[12]);assert.equal(buffer.blocked,false);assert.equal(buffer.size,0);
});

test('overflow is bounded and requires a sufficiently new snapshot rather than acknowledging discarded frames',()=>{
  const buffer=createOperationFrameBuffer({limit:2});
  for(let frameSeq=1;frameSeq<=3;frameSeq++)buffer.add({frameSeq});
  assert.equal(buffer.size,0);assert.equal(buffer.blocked,true);
  assert.equal(buffer.replayAfter(2,()=>assert.fail('cannot replay an incomplete buffer')),false);
  buffer.add({frameSeq:4});const replay=[];
  assert.equal(buffer.replayAfter(3,frame=>{replay.push(frame.frameSeq);return {};}),true);
  assert.deepEqual(replay,[4]);buffer.reset();assert.equal(buffer.blocked,false);
});
