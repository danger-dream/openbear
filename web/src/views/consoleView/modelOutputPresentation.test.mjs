import test from 'node:test';
import assert from 'node:assert/strict';
import {formatArgumentBytes, modelOutputView} from './modelOutputPresentation.js';
import {deriveOperationRunState, reduceOperationFrame, withTransientIdleThinking} from '../../timelineProjection.js';

const progress = {toolNames:['Write'],receivedBytes:12010,startedAtMs:1000,updatedAtMs:202000,elapsedMs:201000,phase:'generating'};
const run = {opId:'run:r',opType:'run',turnId:'turn',runRootTurnId:'turn',lifecycle:'active',status:'running',displaySeq:1,payload:{},createdAtMs:1000};
const status = {opId:'status:r',opType:'status',turnId:'turn',runRootTurnId:'turn',lifecycle:'active',status:'running',displaySeq:2,payload:{modelOutput:progress,statusText:'正在生成文件内容'}};

test('uses decimal byte units, truthful operation labels and elapsed duration', () => {
  assert.equal(formatArgumentBytes(999),'999 B');
  assert.equal(formatArgumentBytes(12010),'12.01 kB');
  assert.equal(formatArgumentBytes(1201000),'1.20 MB');
  const view = modelOutputView(progress,202000);
  assert.equal(view.label,'正在生成文件内容');
  assert.equal(view.tools,'Write');
  assert.equal(view.elapsed,'3分21秒');
  assert.match(view.hint,/才会执行/);
  assert.equal(view.idle,'');
  assert.equal(modelOutputView({...progress,toolNames:['Bash']},202000).label,'正在生成工具参数');
});

test('idle time is explicit, timer still advances, and ready is not execution', () => {
  const view = modelOutputView(progress,232000);
  assert.equal(view.elapsed,'3分51秒');
  assert.equal(view.idle,'30秒未收到新参数');
  assert.equal(view.quiet,true);
  assert.equal(modelOutputView({...progress,phase:'ready'},202000).label,'参数已接收，等待模型结束');
});

test('explicit clear frame removes old activity and a later attempt starts fresh', () => {
  const old = {...status,revision:1};
  const cleared = reduceOperationFrame(old,{opId:status.opId,opType:'status',action:'patch',revision:2,
    payload:{modelOutput:false,statusText:'正在思考 …'}});
  assert.equal(cleared.payload.modelOutput,false);
  assert.equal(deriveOperationRunState([run,cleared]).modelOutput,null);
  const fresh = {...progress,toolNames:['Read'],receivedBytes:0,elapsedMs:0,attemptId:'new-attempt'};
  const resumed = reduceOperationFrame(cleared,{opId:status.opId,opType:'status',action:'patch',revision:3,
    payload:{modelOutput:fresh,statusText:'正在生成工具参数'}});
  assert.deepEqual(deriveOperationRunState([run,resumed]).modelOutput,fresh);
});

test('operation snapshot restores progress only for the active foreground run', () => {
  const state = deriveOperationRunState([run,status]);
  assert.deepEqual(state.modelOutput,progress);
  assert.equal(state.statusLabel,'正在生成文件内容');
  const tail = withTransientIdleThinking([{turnUuid:'turn',events:[]}],state,{modelOutput:state.modelOutput})[0].events.at(-1);
  assert.deepEqual(tail.modelOutput,progress);
  assert.equal(tail.startedAt,1000);
  assert.equal(deriveOperationRunState([run,{...status,payload:{modelOutput:null}}]).modelOutput,null);
  const tool = {opId:'tool:c',opType:'tool',turnId:'turn',lifecycle:'active',status:'running',displaySeq:3,payload:{name:'Write'}};
  assert.equal(deriveOperationRunState([run,status,tool]).modelOutput,null);
  assert.equal(deriveOperationRunState([{...run,lifecycle:'terminal',status:'completed'},status]).modelOutput,null);
  assert.equal(deriveOperationRunState([run,{...status,turnId:'old',runRootTurnId:'old'}]).modelOutput,null);
});
