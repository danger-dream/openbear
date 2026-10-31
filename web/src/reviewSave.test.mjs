import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';
import {createCronEditor} from './components/cron/useCron.js';
import * as cronConfig from './components/cron/cronConfig.js';
import {createWebhookEditor} from './components/webhooks/useWebhookEditor.js';
import * as webhookConfig from './components/webhooks/webhookConfig.js';
import * as ddnsHelpers from './ddns/ddnsHelpers.js';

test('DDNS input changes invalidate preview and consent before debounce or late response',async t=>{
  const pending=deferred(),writes=[],nodes=[{serverId:'S',online:true,ddns:true,exec:true}],accounts=[{id:'A',type:'cloudflare'}];
  const h=harness('./ddns/RecordForm.vue',{...ddnsHelpers,defineProps:()=>({nodes,accounts,providers:[],record:null,serverId:'S'}),
    setTimeout:()=>1,clearTimeout(){},DdnsApi:{preview:()=>pending.promise,addRecords:async body=>{writes.push(body);return {records:[]};}}});t.after(()=>h.close());
  h.run("form.names='old.example.com';preview.value={rows:[{name:'old.example.com',type:'A',accountId:'A',action:'takeover'}]};confirmed.value=new Set(['old.example.com A'])");
  assert.equal(h.run('canSave.value'),true);
  const old=h.run('runPreview()');h.run("form.names='new.example.com'");
  assert.equal(h.run('preview.value'),null);assert.equal(h.run('confirmed.value.size'),0);
  await h.run('save()');assert.equal(writes.length,0);
  pending.resolve({rows:[{name:'old.example.com',type:'A',accountId:'A',action:'same'}]});await old;
  assert.equal(h.run('preview.value'),null);await h.run('save()');assert.equal(writes.length,0);
});

const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {resolve,promise};};
const flush=async()=>{for(let i=0;i<20;i++)await Vue.nextTick();};
const clone=value=>JSON.parse(JSON.stringify(value));
function harness(path,extra={}) {
  const scope=Vue.effectScope(),cleanups=[];
  const context={...Vue,console,Blob,AbortController,crypto:{randomUUID:()=> 'review-save'},
    defineProps:()=>({}),defineEmits:()=>()=>{},defineExpose:()=>{},onMounted:()=>{},onBeforeUnmount:fn=>cleanups.push(fn),
    window:{addEventListener(){},removeEventListener(){}},ElMessage:{success(){},error(){}},ElMessageBox:{confirm:async()=>{}},...extra};
  const source=parse(fs.readFileSync(new URL(path,import.meta.url),'utf8')).descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm,'');
  vm.createContext(context);scope.run(()=>vm.runInContext(source,context));
  return {context,run:code=>vm.runInContext(code,context),close(){cleanups.forEach(fn=>fn());scope.stop();}};
}

for(const kind of ['cron','webhook']) {
  test(`${kind}: real retention preview and save share one snapshot, pending edits stay dirty`,async t=>{
    const preview=deferred(),consent=deferred(),writes=[],seen=[],props=kind==='cron'?{jobId:'J1',folderId:'F1'}:{scope:{type:'folder',id:'F1'}};
    const config=kind==='cron'?cronConfig.defaultConfig():webhookConfig.defaultConfig(props.scope);
    const item={id:kind==='cron'?'J1':'E1',folderId:'F1',scope:props.scope,name:'Review',description:'',enabled:false,revision:1,controlRevision:1,config};
    const api={cronJob:async()=>({job:clone(item)}),cronEnvironment:async()=>({}),rathOptions:async()=>({models:[]}),cronFolders:async()=>({items:[]}),conversationFolderProperties:async()=>({}),
      webhooks:async()=>({items:[{id:'E1'}]}),webhook:async()=>({endpoint:clone(item)}),webhookEnvironment:async()=>({}),webhookTargets:async()=>({items:[]}),
      previewConversationRetention:body=>{seen.push(clone(body));return preview.promise;},
      updateCronJob:async(id,body)=>{writes.push({id,...clone(body)});return {job:{...item,...clone(body),revision:2}};},
      updateWebhook:async(id,body)=>{writes.push({id,...clone(body)});return {endpoint:{...item,...clone(body),revision:2}};}};
    const h=harness(kind==='cron'?'./components/cron/CronEditor.vue':'./components/webhooks/WebhookEditor.vue',{
      ...cronConfig,...webhookConfig,createCronEditor,createWebhookEditor,defineProps:()=>props,Api:api,
      ElMessageBox:{confirm:()=>consent.promise},normalizedRunDefaults:()=>({}),updateRunDefault:()=>({}),
    });t.after(()=>h.close());await flush();
    const path=kind==='cron'?'draft.config.conversationRetention':'draft.config.target.conversationRetention';
    h.run(`${path}.keepLast=20`);
    const saved=h.run('save()');await flush();
    assert.equal(h.run('saving.value'),true);assert.equal(seen.length,1);assert.equal(seen[0].retention.keepLast,20);
    assert.equal(await h.run('save()'),false);assert.equal(await h.run('canLeave()'),false);
    h.run(`${path}.keepLast=1`);preview.resolve({count:7});await flush();assert.equal(writes.length,0);
    consent.resolve();assert.equal(await saved,true);assert.equal(writes.length,1);
    const sent=kind==='cron'?writes[0].config.conversationRetention:writes[0].config.target.conversationRetention;
    assert.equal(sent.keepLast,20);assert.equal(h.run(`${path}.keepLast`),1);assert.equal(h.run('dirty.value'),true);
  });
  test(`${kind}: cancelled preview never writes; false opt-in omitted, true retained`,async()=>{
    const writes=[],config=kind==='cron'?cronConfig.defaultConfig():webhookConfig.defaultConfig({type:'folder'});
    const item={id:'I',folderId:'F',name:'Review',description:'',enabled:false,revision:1,controlRevision:1,config};
    const api={cronJob:async()=>({job:item}),cronEnvironment:async()=>({}),rathOptions:async()=>({models:[]}),cronFolders:async()=>({items:[]}),conversationFolderProperties:async()=>({}),
      webhooks:async()=>({items:[{id:'I'}]}),webhook:async()=>({endpoint:item}),webhookEnvironment:async()=>({}),
      updateCronJob:async(id,body)=>{writes.push(body);return {job:{...item,...body}};},updateWebhook:async(id,body)=>{writes.push(body);return {endpoint:{...item,...body}};}};
    const editor=kind==='cron'?createCronEditor(api):createWebhookEditor(api);
    await editor.load(kind==='cron'?'I':{type:'folder',id:'F'});
    const save=confirm=>kind==='cron'?editor.save({confirm}):editor.save(undefined,{confirm});
    assert.equal(await save(async()=>false),false);assert.equal(writes.length,0);assert.equal(editor.saving.value,false);
    assert.equal(await save(),true);
    const submitted=kind==='cron'?writes[0].config:writes[0].config.processing;
    assert.equal(Object.hasOwn(submitted,'allowServerChanges'),false);
    (kind==='cron'?editor.draft.config:editor.draft.config.processing).allowServerChanges=true;
    assert.equal(await save(),true);assert.equal((kind==='cron'?writes[1].config:writes[1].config.processing).allowServerChanges,true);
    editor.dispose();
  });
}

test('file viewer keeps text typed during upload unsaved',async t=>{
  const pending=deferred();let uploaded;
  const h=harness('./servers/FileViewer.vue',{defineProps:()=>({entry:{name:'file.txt',mtime:1,size:1},path:'/tmp/file.txt',serverId:'S',asText:true}),
    Api:{serverFiles:async()=>({entries:[]}),uploadServerFile:async(id,path,blob)=>{uploaded=await blob.text();return pending.promise;}},
    ok:x=>x,imageType:()=>null,language:()=>'',safeError:String});t.after(()=>h.close());
  h.run("original.value='old';text.value='submitted'");const save=h.run('save()');await flush();
  h.run("text.value='typed later'");pending.resolve();await save;
  assert.equal(uploaded,'submitted');assert.equal(h.run('original.value'),'submitted');assert.equal(h.run('dirty.value'),true);
});

test('task drawer scopes pending add/edit/drop to conversation and item; add is single flight',async t=>{
  const props=Vue.reactive({conversationUuid:'A'}),pending=deferred(),calls=[];
  const state=Vue.reactive({tasks:[],revision:1});
  const store={state,find:number=>({number,title:number,children:[],status:'todo'}),refresh(){},setOpener(){}};
  const h=harness('./views/consoleView/WorkItemsDrawer.vue',{defineProps:()=>props,useWorkItems:()=>store,
    Api:{addWorkItem:async(...args)=>{calls.push(args);return pending.promise;}},isClosed:()=>false,statusOf:x=>x.status,
    FileText:{},GitCommitHorizontal:{},FlaskConical:{},Terminal:{},LinkIcon:{},StickyNote:{}});t.after(()=>h.close());
  h.run("stack.value=['1'];openAdd();addDraft.value.title='first'");
  const first=h.run('saveAdd()');await h.run('saveAdd()');assert.equal(calls.length,1);assert.equal(calls[0][0],'A');
  h.run("editor.open=true;dropper.open=true");props.conversationUuid='B';await flush();
  assert.equal(h.run('addDraft.value'),null);assert.equal(h.run('editor.open'),false);assert.equal(h.run('dropper.open'),false);
  h.run("stack.value=['1'];openAdd();addDraft.value.title='second'");pending.resolve();await first;
  assert.equal(h.run('addDraft.value.title'),'second');
  h.run("stack.value=['2']");assert.equal(h.run('addDraft.value'),null);
});
