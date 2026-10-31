import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {Timer,ArrowRight} from '@element-plus/icons-vue';
import {projectOperationMessages} from '../../timelineProjection.js';
import {createQuery} from '../../components/cron/useCron.js';
import * as config from '../../components/cron/cronConfig.js';
const read=file=>fs.readFileSync(new URL(file,import.meta.url),'utf8');
const flush=async()=>{for(let i=0;i<10;i++)await Vue.nextTick();};
const message={source:'cron',opId:'msg:12345678-full-run-id',createdAt:1791115193,content:'FULL ORIGINAL INPUT <script>not html</script>\nPRE_OUTPUT',cronCard:{name:'一次性链路验收',runId:'12345678-full-run-id',trigger:'scheduled',startedAtMs:1791115193004,scheduledAtMs:1791115193000,schedule:{kind:'at',at:'2026-10-04T11:59:53Z',timezone:'Asia/Shanghai'}}};
function harness(file,props,extra={}){
  const d=parse(read(file)).descriptor,scope=Vue.effectScope(),cleanups=[],copied=[],dialogs=[];
  const context={...Vue,...config,createQuery,Timer,ArrowRight,CronRunDialog:{},defineProps:()=>props,defineEmits:()=>()=>{},onBeforeUnmount:fn=>cleanups.push(fn),copyTextToClipboard:async value=>copied.push(value),ElMessage:{success(){},warning(){}},...extra};
  vm.createContext(context);scope.run(()=>vm.runInContext(d.scriptSetup.content.replace(/^import .*;\n/gm,''),context));
  const run=code=>vm.runInContext(code,context);
  async function html(){
    const bindings=compileScript(d,{id:'cron-card-test'}).bindings;
    const names=Object.keys(bindings).filter(n=>bindings[n]!=='props');
    const setup=Vue.proxyRefs({...run(`({${names.join(',')}})`),...props});
    const render=Vue.compile(d.template.content),app=Vue.createSSRApp({render(){return render.call(this,setup,[]);}});
    app.component('CronRunDialog',{props:['runId','modelInput','showConversationLink'],setup(p){dialogs.push(p);return()=>Vue.h('section',{'data-run-id':p.runId},p.modelInput);}});
    app.component('ElDialog',{setup(_,{slots}){return()=>Vue.h('section',[slots.default?.(),slots.footer?.()]);}});
    for(const name of ['ElButton','ElIcon','ElSkeleton'])app.component(name,{setup(_,{slots,attrs}){return()=>Vue.h('span',attrs,slots.default?.());}});
    app.component('Timer',Timer);app.component('ArrowRight',ArrowRight);app.config.warnHandler=()=>{};
    return renderToString(app);
  }
  return{run,html,copied,dialogs,close(){cleanups.forEach(fn=>fn());scope.stop();}};
}
test('Cron metadata and full original input survive projection without classifying human text as Cron',()=>{
  const [projected]=projectOperationMessages([{opId:message.opId,opType:'user_message',turnUuid:'root',source:'cron',displaySeq:1,payload:{text:message.content,cronCard:message.cronCard,cronRunId:message.cronCard.runId}}]);
  assert.equal(projected.source,'cron');assert.equal(projected.content,message.content);assert.deepEqual(projected.cronCard,message.cronCard);
  const [human]=projectOperationMessages([{opId:'human',opType:'user_message',turnUuid:'root',displaySeq:1,payload:{text:'[定时] Cron task'}}]);
  assert.equal(human.source,'user');
});
test('compact card shows name, trigger/time and short ID, not long model input; opens existing run dialog with exact original input',async()=>{
  const props=Vue.reactive({message:structuredClone(message)}),h=harness('./CronMessageCard.vue',props);
  let html=await h.html();assert.match(html,/一次性链路验收/);assert.match(html,/自动触发/);assert.match(html,/#12345678/);
  assert.doesNotMatch(html,/FULL ORIGINAL INPUT|PRE_OUTPUT|full-run-id/);assert.equal(h.dialogs.length,0);
  h.run('open.value=true');html=await h.html();assert.equal(h.dialogs[0].runId,message.cronCard.runId);assert.equal(h.dialogs[0].modelInput,message.content);assert.equal(h.dialogs[0].showConversationLink,false);
  assert.doesNotMatch(html,/<script>/);
  props.message.cronCard.trigger='manual';assert.match(await h.html(),/手动执行/);
  props.message={source:'cron',opId:'msg:another-run',content:'new'};await flush();assert.equal(h.run('open.value'),false);assert.match(await h.html(),/任务执行/);h.close();
});
test('run dialog keeps original input readable/copyable even when run detail is unavailable',async()=>{
  const h=harness('../../components/cron/CronRunDialog.vue',{runId:'missing',modelInput:message.content,showConversationLink:false},{Api:{cronRun:async()=>{throw Error('记录不可用');}}});
  await flush();const html=await h.html();assert.match(html,/记录不可用/);assert.match(html,/模型输入 · 原文/);assert.match(html,/FULL ORIGINAL INPUT/);assert.doesNotMatch(html,/<script>/);
  await h.run('copyInput()');assert.deepEqual(h.copied,[message.content]);assert.doesNotMatch(html,/打开会话/);h.close();
});
test('Cron and Webhook reuse the same card CSS',()=>{
  for(const file of ['./CronMessageCard.vue','./WebhookMessageCard.vue'])assert.match(read(file),/style scoped src="\.\/eventMessageCard.css"/);
});
