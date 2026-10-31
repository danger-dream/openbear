import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {SetUp,ArrowRight} from '@element-plus/icons-vue';
import {projectOperationMessages} from '../../timelineProjection.js';
import * as config from '../../components/cron/cronConfig.js';
const read=file=>fs.readFileSync(new URL(file,import.meta.url),'utf8');
const input='请把这个实例实际部署好：交付文件、按需构建镜像、启动容器。\n这些操作不会逐条请用户确认 <script>not html</script>';
const card={operationId:'5b09d7e8-09d0-4a6d-a3c0-41beecc2fb58',instanceId:'inst',kind:'deployment',name:'Example-Node',server:'Example-Node',workspacePath:'/opt/openbear-node',port:18970,instructions:'',startedAtMs:1791849876295};
const message={source:'instance',opId:'msg:'+card.operationId,createdAt:1791849876,content:input,instanceCard:card};
async function render(props){
  const file='./InstanceMessageCard.vue',d=parse(read(file)).descriptor,scope=Vue.effectScope();
  const context={...Vue,...config,SetUp,ArrowRight,defineProps:()=>props};
  vm.createContext(context);scope.run(()=>vm.runInContext(d.scriptSetup.content.replace(/^import .*;\n/gm,''),context));
  const bindings=compileScript(d,{id:'instance-card-test'}).bindings;
  const names=Object.keys(bindings).filter(n=>bindings[n]!=='props');
  const setup=Vue.proxyRefs({...vm.runInContext(`({${names.join(',')}})`,context),...props});
  const tpl=Vue.compile(d.template.content),app=Vue.createSSRApp({render(){return tpl.call(this,setup,[]);}});
  app.component('ElDialog',{props:['modelValue','title'],setup(p,{slots}){return()=>p.modelValue?Vue.h('section',{'data-title':p.title},slots.default?.()):null;}});
  app.component('ElIcon',{setup(_,{slots}){return()=>Vue.h('span',slots.default?.());}});
  app.component('SetUp',SetUp);app.component('ArrowRight',ArrowRight);app.config.warnHandler=()=>{};
  const html=await renderToString(app);
  return {html,run:code=>vm.runInContext(code,context),setup,app,again:()=>renderToString(app),close:()=>scope.stop()};
}
test('instance source and card survive projection; human text is never classified as instance',()=>{
  const [projected]=projectOperationMessages([{opId:message.opId,opType:'user_message',turnUuid:'root',source:'instance',displaySeq:1,payload:{text:input,source:'instance',instanceCard:card}}]);
  assert.equal(projected.source,'instance');assert.equal(projected.content,input);assert.deepEqual(projected.instanceCard,card);
  const [human]=projectOperationMessages([{opId:'human',opType:'user_message',turnUuid:'root',displaySeq:1,payload:{text:'请把这个实例实际部署好'}}]);
  assert.equal(human.source,'user');
});
test('deployment card shows the instance and target, not the long model instruction; dialog shows it as text',async()=>{
  const r=await render(Vue.reactive({message:structuredClone(message)}));
  assert.match(r.html,/实例部署/);assert.match(r.html,/Example-Node/);assert.match(r.html,/\/opt\/openbear-node/);assert.match(r.html,/端口 18970/);assert.match(r.html,/#5b09d7e8/);
  assert.doesNotMatch(r.html,/交付文件、按需构建镜像|逐条请用户确认|<script>/);
  r.run('open.value=true');const opened=await r.again();
  assert.match(opened,/部署指令 · Example-Node/);assert.match(opened,/逐条请用户确认/);assert.doesNotMatch(opened,/<script>/);r.close();
});
test('maintenance card shows the requested scope',async()=>{
  const r=await render(Vue.reactive({message:{...structuredClone(message),instanceCard:{...card,kind:'maintenance',instructions:'安装 LibreOffice'}}}));
  assert.match(r.html,/实例维护/);assert.match(r.html,/安装 LibreOffice/);r.close();
});
test('card reuses the shared event card CSS and TurnList routes instance turns to it',()=>{
  assert.match(read('./InstanceMessageCard.vue'),/style scoped src="\.\/eventMessageCard.css"/);
  assert.match(read('./TurnList.vue'),/InstanceMessageCard v-else-if="turn.user.source === 'instance'"/);
});

test('InstanceControl calls are translated: name, each action, and remote commands by purpose',async()=>{
  const {toolActionSummary,toolDisplayLabel}=await import('./toolCallPresentation.js');
  const t=args=>toolActionSummary('InstanceControl',args);
  assert.equal(toolDisplayLabel('InstanceControl'),'实例管理');
  assert.equal(t({action:'describe'}),'查看实例工具用法');
  assert.equal(t({action:'status'}),'查看本次部署目标');
  assert.equal(t({action:'deliver',params:{revision:1,source:true,initialize:true}}),'交付部署文件和同版本源码');
  assert.equal(t({action:'verify',params:{instanceId:'x'}}),'检查实例运行状态');
  assert.equal(t({action:'report',params:{revision:1,outcome:'completed',summary:'容器已启动'}}),'报告结果：完成 · 容器已启动');
  assert.equal(t({action:'maintenance',params:{instanceId:'x',instructions:'安装 LibreOffice'}}),'开始维护 · 安装 LibreOffice');
  // The model's own purpose wins; otherwise common commands are recognized.
  assert.equal(t({action:'remote',params:{revision:1,description:'清理旧镜像',command:'docker image prune -f'}}),'清理旧镜像');
  assert.equal(t({action:'remote',params:{revision:1,command:"set -eu\npython3 -I - <<'PY'\nprint(1)\nPY\ndocker build --progress=plain -t openbear:0.12.0 /opt/x/source"}}),'构建镜像');
  assert.equal(t({action:'remote',params:{revision:1,command:'set -eu\ndocker compose -f /opt/x/compose.json up -d --wait'}}),'启动容器');
  assert.equal(t({action:'remote',params:{revision:1,command:"set -eu\nprintf '== Docker ==\\n'\ndocker version --format x"}}),'检查 Docker 环境');
  assert.equal(t({action:'remote',params:{revision:1,command:'set -eu\nls -la /opt/openbear-node'}}),'执行命令：ls -la /opt/openbear-node');
  assert.equal(t({action:'unknown_new_action'}),'unknown_new_action');
});
