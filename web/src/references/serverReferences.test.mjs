import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {computed,ref,reactive,watch,nextTick,compile,createSSRApp,h} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {Schema} from '@tiptap/pm/model';
import {TextSelection} from '@tiptap/pm/state';
import * as codec from './codec.js';
import * as catalog from './catalog.js';
import * as policy from './sizePolicy.js';
import * as presentation from './presentation.js';
import * as servers from './servers.js';
import {conversationMessages} from './conversationMessages.js';
import {mentionAtPosition} from './mention.js';
import {applyFeatureSettings,resetFeatureTogglesForTest} from '../featureToggles.js';
import {renderMarkdown} from './rendererHarness.mjs';

const pickerSource=parse(readFileSync(new URL('./ReferencePicker.vue',import.meta.url),'utf8')).descriptor;
const settled=async()=>{await new Promise(setImmediate);await nextTick();};
const plain=value=>JSON.parse(JSON.stringify(value));
// The shared size policy also stamps the server-supplied contentLimit; identity is what matters here.
const identity=value=>Array.isArray(value)?value.map(identity):(({contentLimit,...rest})=>rest)(plain(value));
// Rows as GET /api/references/servers returns them (app/reference_servers.py picker_row).
const rows=[
 {key:'server:local',kind:'server',id:'local',name:'local',label:'本机',group:'',region:'CN',platform:'linux',os:'Ubuntu 24.04',ip:'203.0.113.10',meshAddress:'100.96.0.1',local:true,online:true,nodeInstalled:true,canExec:true,sshLogin:false,searchText:'CN 203.0.113.10 100.96.0.1 linux Ubuntu 24.04 本机 local'},
 {key:'server:hk-1',kind:'server',id:'hk-1',name:'hk-1',label:'香港一号',group:'生产',region:'HK',platform:'linux',os:'Debian 12',ip:'198.51.100.7',meshAddress:'',local:false,online:false,nodeInstalled:true,canExec:true,sshLogin:true,searchText:'生产 HK 198.51.100.7 linux Debian 12'},
 {key:'server:tokyo-2',kind:'server',id:'tokyo-2',name:'tokyo-2',label:'Tokyo Edge',group:'边缘',region:'JP',platform:'windows',os:'Windows Server 2022',ip:'192.0.2.9',meshAddress:'100.96.0.9',local:false,online:true,nodeInstalled:true,canExec:false,sshLogin:false,searchText:'边缘 JP 192.0.2.9 100.96.0.9 windows Windows Server 2022'},
];
let loads=0;
servers.setServerDirectoryLoader(async()=>{loads++;return {ok:true,enabled:true,items:rows};});
async function directory(){servers.clearServerDirectory();await servers.loadServerDirectory({force:true});}

function picker(t,props={}){
 catalog.applyCatalogPacket({type:'snapshot',epoch:'servers',seq:1,includeArchived:false,items:[{kind:'doc',key:'doc:7',id:'7',label:'部署手册',group:'运维'}],conversationContentLimit:20000});
 const state=reactive({open:true,anchor:null,placement:'top-start',kind:'',query:'',currentConversation:'current',selectedReferences:[],searchable:false,allowDrag:false,...props});
 const emitted=[];
 const ctx=vm.createContext({...codec,...catalog,...policy,...presentation,...servers,conversationMessages,computed,ref,nextTick,
  watch:(...args)=>{const stop=watch(...args);t.after(stop);return stop;},defineProps:()=>state,defineEmits:()=>(...args)=>emitted.push(args),defineExpose:()=>{},Api:{}});
 vm.runInContext(pickerSource.scriptSetup.content.replace(/^import .*?;\n/gm,''),ctx);
 const run=code=>vm.runInContext(code,ctx);
 const key=(name,target={tagName:'DIV'})=>{ctx.event={key:name,target,preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};return run('key(event)');};
 const names='props,emit,list,searchInput,searchOpen,toggleSearch,localQuery,filter,selected,mode,child,childQuery,checked,queryText,results,groups,tabs,historyMode,historyError,historyBusy,historyMore,selectionError,referenceCatalog,KIND_LABELS,referenceLeadHtml,referenceCandidateDetail,includeArchivedReferences,key,back,changeTab,choose,clickItem,toggle,inspectHistory,loadHistory,dragStart,dragEnd,dragging,chooseTurn,candidateState,candidateWarning,policyReasonTitle,multiSelect,serverMode,serverDirectory,loadServerDirectory';
 const render=()=>renderToString(createSSRApp({render:compile(pickerSource.template.content),setup:()=>({...state,...run(`({${names}})`)}),components:{FloatingPanel:{render(){return h('section',{},this.$slots.default?.());}}}}));
 return {state,emitted,run,key,render};
}

test('server references keep identity through token, draft document and restore; bad ids never become capsules',()=>{
 const refs=[{kind:'server',id:'local',label:'本机',scope:'full'},{kind:'server',id:'9f0c2d4e-1b2a-4c3d-8e9f-001122334455',label:'香港 [生产]',scope:'full',mode:'mention'}];
 const text='在 '+refs.map(codec.referenceToken).join(' 和 ')+' 上装 htop';
 assert.equal(codec.documentToText(codec.textToDocument(text)),text);
 assert.deepEqual(codec.referencesInText(text),refs);
 assert.equal(codec.referenceDisplayText(text),'在 本机 和 香港 [生产] 上装 htop');
 assert.match(codec.referenceUrl(refs[1]),/^openbear:\/\/ref\/server\/9f0c2d4e-1b2a-4c3d-8e9f-001122334455\?mode=mention$/);
 for(const id of ['has space','a.b','x'.repeat(65),'semi;colon'])assert.equal(codec.normalizeReference({kind:'server',id,label:'x'}),null,id);
 assert.deepEqual(codec.referencesInText('[x](openbear://ref/server/local/extra)'),[]);
 assert.equal(codec.KIND_LABELS.server,'服务器');
});

test('@server/ opens the server kind like the other namespaces',()=>{
 const schema=new Schema({nodes:{doc:{content:'paragraph+'},paragraph:{content:'inline*'},text:{group:'inline'},hardBreak:{group:'inline',inline:true},
  reference:{group:'inline',inline:true,atom:true,attrs:{kind:{},id:{},label:{},scope:{default:'full'}}}}});
 const doc=schema.nodeFromJSON(codec.textToDocument('帮我在 @server/香港'));
 const pos=TextSelection.atEnd(doc).$from;
 assert.deepEqual(mentionAtPosition(pos),{from:pos.pos-'@server/香港'.length,to:pos.pos,query:'香港',kind:'server'});
});

test('search finds servers by name, pinyin, group, region and IP, and only where servers are offered',async()=>{
 await directory();
 const ids=(query,kind='server')=>catalog.searchReferences(query,{kind}).map(item=>item.id);
 assert.deepEqual(ids(''),['local','hk-1','tokyo-2'],'empty query keeps the saved server order');
 assert.deepEqual(ids('香港'),['hk-1']);assert.deepEqual(ids('xianggang'),['hk-1']);
 assert.deepEqual(ids('生产'),['hk-1']);assert.deepEqual(ids('jp'),['tokyo-2']);
 assert.deepEqual(ids('198.51.100'),['hk-1']);assert.deepEqual(ids('100.96.0.9'),['tokyo-2']);
 assert.deepEqual(ids('本机'),['local']);assert.deepEqual(ids('tokyo-2'),['tokyo-2']);
 assert.ok(catalog.searchReferences('香港').some(item=>item.kind==='server'),'the 全部 tab includes servers');
 assert.ok(!catalog.searchReferences('',{kind:'doc'}).some(item=>item.kind==='server'));
 assert.equal(catalog.referenceItem({kind:'server',id:'hk-1'}).label,'香港一号');
 assert.equal(presentation.referenceCandidateDetail(servers.serverItem('hk-1')),'离线 · 可走 SSH · linux · 生产 · 198.51.100.7');
 assert.equal(presentation.referenceCandidateDetail(servers.serverItem('tokyo-2')),'在线 · 未开放命令 · windows · 边缘 · 192.0.2.9');
 assert.equal(presentation.referenceCandidateDetail(servers.serverItem('local')),'在线 · linux · 未分组 · 203.0.113.10');
 // The 主机 switch off: the backend answers enabled:false and nothing is offered.
 servers.acceptServerDirectory({ok:true,enabled:false,items:[]});
 assert.equal(servers.serverReferencesOffered(),false);assert.deepEqual(ids('香港'),[]);
 applyFeatureSettings({'features.servers':false});
 const before=loads;await servers.loadServerDirectory({force:true});assert.equal(loads,before,'no request while the switch is off');
 assert.equal(servers.serverReferencesOffered(),false);
 applyFeatureSettings({'features.servers':true});
 assert.equal(servers.serverReferencesOffered(),true);
 assert.equal(servers.serverDirectory.ready,false);
 await servers.loadServerDirectory();assert.equal(loads,before+1);
 assert.deepEqual(ids('香港'),['hk-1']);
 resetFeatureTogglesForTest();
});

test('real picker: 服务器 tab multi-selects by tap, shows state, and inserts all checked as capsules',async t=>{
 await directory();
 const p=picker(t);await settled();
 assert.deepEqual(plain(p.run('tabs.value')).map(tab=>tab[1]),['全部','记忆','文档','凭证','会话','服务器','历史']);
 p.run("changeTab('server')");await settled();
 assert.equal(p.run('serverMode.value'),true);assert.equal(p.run('results.value.length'),3);
 let html=await p.render();
 assert.match(html,/aria-multiselectable="true"/);assert.match(html,/class="reference-picker-status is-online"/);
 assert.match(html,/离线 · 可走 SSH · linux · 生产/);assert.match(html,/点选可多选/);
 // Each row shows its region's flag beside the check box.
 assert.equal((html.match(/class="reference-chip__flag"/g)||[]).length,3);assert.match(html,/🇭🇰/);
 p.run('clickItem(results.value[1],1)');p.run('clickItem(results.value[0],0)');
 assert.deepEqual(p.emitted,[],'tapping a server checks it instead of inserting');
 html=await p.render();assert.match(html,/已选 2 台/);assert.match(html,/引用 2 台/);
 assert.equal((html.match(/class="reference-picker-option[^"]*is-checked/g)||[]).length,2);
 // Space typed in the editor stays text; Space in the list checks the highlit row.
 assert.equal(p.key(' ',{tagName:'DIV',isContentEditable:true}),false);assert.equal(p.run('checked.value.size'),2);
 p.run('selected.value=2');assert.equal(p.key(' '),true);assert.equal(p.run('checked.value.size'),3);p.key(' ');
 p.run("mode.value='mention'");assert.equal(p.key('Enter'),true);
 const [event,values]=plain(p.emitted[0]);
 assert.equal(event,'select');
 assert.deepEqual(identity(values),[{kind:'server',id:'hk-1',label:'香港一号',scope:'full',mode:'mention'},{kind:'server',id:'local',label:'本机',scope:'full',mode:'mention'}]);
 const token=values.map(codec.referenceToken).join(' ');
 assert.equal(codec.documentToText(codec.textToDocument(token)),token);
});

test('@server/ picker: unchecked Enter inserts the highlit server; 全部 tab inserts a single server directly',async t=>{
 await directory();
 const p=picker(t,{kind:'server',query:'tokyo'});await settled();
 assert.equal(p.run('multiSelect.value'),true);p.key('Enter');
 assert.deepEqual(identity(p.emitted[0][1]),{kind:'server',id:'tokyo-2',label:'Tokyo Edge',scope:'full'});
 const all=picker(t,{query:'香港'});await settled();
 assert.equal(all.run('multiSelect.value'),false);
 all.run('clickItem(results.value.find(item=>item.kind==="server"),0)');
 assert.deepEqual(identity(all.emitted[0][1]),{kind:'server',id:'hk-1',label:'香港一号',scope:'full'});
});

test('capsules mark a deleted server in the composer and in old messages, without errors',async()=>{
 await directory();
 const present={kind:'server',id:'hk-1',label:'香港一号',scope:'full'},gone={kind:'server',id:'deleted-9',label:'旧机器',scope:'full'};
 assert.doesNotMatch(presentation.referenceChipOpen(present),/is-missing/);
 assert.equal(presentation.referenceScopeLabel(present),'');
 assert.match(presentation.referenceChipOpen(gone),/reference-chip--server is-missing/);
 assert.equal(presentation.referenceScopeLabel(gone),'已不存在');
 assert.equal(presentation.referenceScopeLabel({...gone,mode:'mention'}),'仅提及 · 已不存在');
 assert.match(presentation.referenceTitle(gone),/已删除或无权访问/);
 const html=renderMarkdown('检查 '+codec.referenceToken(gone)+' 和 '+codec.referenceToken(present));
 assert.match(html,/reference-chip--server is-missing[^>]*>.*旧机器.*已不存在/s);
 assert.match(html,/香港一号/);
 // A known server shows its flag; a deleted one, or any before the directory is read, the server icon.
 assert.match(presentation.referenceChipOpen(present),/reference-chip__flag[^>]*>🇭🇰</);
 assert.match(presentation.referenceChipOpen(gone),/reference-chip__icon/);
 // Before the directory is known nothing is claimed either way.
 servers.clearServerDirectory();
 assert.doesNotMatch(presentation.referenceChipOpen(gone),/is-missing/);
 assert.match(presentation.referenceChipOpen(present),/reference-chip__icon/);
 assert.equal(servers.serverPresence('deleted-9'),'unknown');
});
