import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {compile, createSSRApp, h} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {createOutboundSendTracker, restoreOutboundDraft, waitForSocketOpen, probeSocket} from './outboundSend.js';
import {conversationWsUrl} from '../../api.js';
import {extensionDraft, forgetExtensionDraft} from './extensionDrafts.js';

const source = fs.readFileSync(new URL('./ConsoleView.vue', import.meta.url), 'utf8');
const composer = fs.readFileSync(new URL('./ConsoleComposer.vue', import.meta.url), 'utf8');
function between(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `${start} -> ${end}`);
  return source.slice(a, b);
}
const snippets = [
  between('function setDraftForConversation(', 'function clearDraftForConversation('),
  between('function stashAttachmentsForConversation(', 'function warnSkippedAttachments('),
  between('function finishPendingOutboundSend(', 'function handleWsMessage('),
  between('async function ensureServerConversationForSend(', 'function applyLoadedConversationState('),
  between('async function connectWs(', 'function checkConnectionOnResume('),
  between('async function send(', 'async function newSession()'),
  between('async function switchConversation(', 'watch(() => props.conversationUuid,'),
];
const deferred = () => {let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject};};
const noop = () => {};
function harness() {
  const upload = deferred(), store = {A:'message A', B:'message B'}, uploads = [], sockets = [];
  const file = {name:'a.txt',size:12,type:'text/plain'};
  const props = {conversationUuid:'A',folderId:''};
  const context = {
    props, draft:{value:'message A'}, draftByConversation:{value:store}, pendingAttachments:{value:[{id:'fileA',file}]}, attachmentPreviews:{value:{}}, attachmentsByConversation:new Map(), attachmentsLoadedKey:'A',
    attachmentRestoring:{value:false}, restoringDraft:{value:false}, componentMounted:true, conversationSwitchGeneration:0, sendAttemptGeneration:0,
    sendPending:{value:false}, modelMutating:{value:false}, running:{value:false}, foregroundRunning:{value:false}, rootTurnRunning:{value:false},
    activeTurnIndex:{value:0}, autoScrollLocked:{value:true}, status:{value:''}, messages:{value:[]}, lastStats:{value:null}, chatState:{value:null},
    localToServerTransitionUuid:{value:''}, runConfigOverride:{value:null}, runStartedAt:{value:0}, pendingLoadBottomScroll:null,
    readingAnchor:null, pinnedActiveTurnIndex:null, agentAutoOpenBoundaryConversation:'', runConfigInteractionGeneration:0,
    uploadProgressByConversation:{value:{}}, draftKey:uuid=>uuid || props.conversationUuid,
    timelinePageInitialized:true,timelinePageConversationUuid:'A',lastFrameSeq:{value:91},
    ws:null,wsConversationUuid:'',reconnectTimer:null,terminalStateRefreshScheduler:{invalidate:noop},
    window:{setTimeout:fn=>{context.reconnect=fn;return 1;}},handleWsMessage:noop,
    saveDraftStore:next => {context.draftByConversation.value = next;},
    activeConversationUuid:{get value(){return props.conversationUuid;}}, isLocalConversation:{get value(){return props.conversationUuid.startsWith('local:');}},
    outboundSends:createOutboundSendTracker(), restoreOutboundDraft, waitForSocketOpen, probeSocket,
    crypto:{randomUUID:()=> 'request-A'},
    composer:{value:{getReferenceOrder:()=> []}}, referencesInText:()=>[],referenceCatalog:{ready:true,connected:true},
    localAttachmentPayload:()=>[], noteVisibleOutput:noop, clearActiveRun:noop, lockAutoScroll:noop, closeComposerMenus:noop, adjustComposerHeight:noop,
    scrollBottom:async()=>{}, ensureLocalRunDefaults:async()=>{throw Error('persisted conversation must not create new one');}, forgetLocalDraftChoices:noop, extensionDraft, forgetExtensionDraft, updatePromptSuggestions:noop,
    Api:{uploadConversationFiles:(uuid, files, options)=>{uploads.push({uuid,files,options}); return upload.promise;}},
    attachmentDrafts:{removeItems:async()=>true}, emit:noop,
    closeWs:noop, clearUiCaches:noop, resetOperationStore:noop, resetAgentAutoOpenBoundary:noop, resetTransientThinking:noop,
    stashAttachmentsForConversation:null, releaseSentAttachments:noop, restoreReleasedAttachments:noop, queueSentAttachmentPreviewRevokes:noop,
    clearDraftForConversation:noop, migrateAttachmentDraft:noop, releaseStashedAttachments:noop,
    restoreAttachmentsForConversation:async uuid=>{
      const parked=context.attachmentsByConversation.get(uuid);
      if(parked){context.pendingAttachments.value=parked.attachments;context.attachmentPreviews.value=parked.previews;context.attachmentsByConversation.delete(uuid);}
      context.attachmentsLoadedKey=uuid;
    }, nextTick:async()=>{}, load:async()=>{}, focusComposer:async()=>{},

    syncRunStateFromOperations:noop, orderedOperationsList:()=>[], ElMessage:{error:noop,warning:noop,success:noop}, apiError:e=>String(e),
    WebSocket:class {
      static OPEN=1;
      constructor(url) {this.url=url;this.readyState=1;this.listeners=new Map();this.sent=[];sockets.push(this);}
      addEventListener(type,fn) {this.listeners.set(type,fn);}
      removeEventListener(type,fn) {if(this.listeners.get(type)===fn)this.listeners.delete(type);}
      send(payload) {this.sent.push(JSON.parse(payload)); if (this.sent.at(-1).type==='ping') this.listeners.get('message')?.({type:'message',data:'{"type":"pong"}'});}
      close() {this.readyState=3;}
      ack() {this.onmessage?.({data:JSON.stringify({type:'ack',requestId:'request-A'})});}
    },
    conversationWsUrl:(uuid,seq,opts)=>{
      const previous=globalThis.window;
      globalThis.window={location:{protocol:'http:',host:'test.local'}};
      try{return conversationWsUrl(uuid,seq,opts);}finally{globalThis.window=previous;}
    },
    setTimeout,clearTimeout,Date,AbortController,
  };
  context.setUploadProgress = (uuid, entries) => {context.uploadProgressByConversation.value={...context.uploadProgressByConversation.value,[uuid]:entries};};
  context.queueSentAttachmentPreviewRevokes = noop;
  context.restoreReleasedAttachments = (uuid, items)=>{ if(uuid==='A') context.attachmentsByConversation.set('A',{attachments:items,previews:{}}); };
  context.releaseSentAttachments = (uuid,ids)=>{assert.equal(uuid,'A');assert.deepEqual([...ids],['fileA']);context.attachmentsByConversation.delete('A');};
  context.clearDraftForConversation = uuid=>context.setDraftForConversation(uuid,'');
  context.contextWebSocket = (...args) => new context.WebSocket(...args); context.pageSession = {blocked:false}; vm.createContext(context);
  for(const snippet of snippets) vm.runInContext(snippet,context);
  // The real switch and send use the real scoping methods, not a fixture-only reducer.
  return {context,props,uploads,upload,sockets,file};
}

for (const fail of [false,true]) test(`real send/switch keep upload in A and never send B (${fail ? 'failure' : 'success'})`, async () => {
  const {context:c,props,uploads,upload,sockets,file}=harness();
  // Install the real draft persistence and restoration functions (no hand-written state machine).
  vm.runInContext(between('function persistComposerDraft(', 'function clearDraftForConversation('),c);
  const sending=vm.runInContext('send()',c);
  for(let i=0;i<12 && !uploads.length;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(uploads.length,1);assert.equal(uploads[0].uuid,'A');
  uploads[0].options.onProgress({loaded:6,total:12,fileIndex:0,fileCount:1,fileLoaded:6,fileSize:12,phase:'uploading'});
  assert.equal(c.uploadProgressByConversation.value.A.fileA.percent,50);
  props.conversationUuid='B';await vm.runInContext('switchConversation("B","A")',c);
  assert.equal(c.draft.value,'message B');
  assert.equal(c.draftByConversation.value.A,'message A');
  assert.equal(c.uploadProgressByConversation.value.B,undefined);
  c.draft.value='message B edited';vm.runInContext('persistComposerDraft(draft.value)',c);
  assert.equal(c.draftByConversation.value.A,'message A');
  uploads[0].options.onProgress({loaded:12,total:12,fileIndex:0,fileCount:1,fileLoaded:12,fileSize:12,phase:'finalizing'});
  assert.equal(c.uploadProgressByConversation.value.A.fileA.phase,'finalizing');
  if(fail){
    upload.reject(new Error('connection down'));await sending;
    assert.equal(sockets.length,0);
    assert.equal(c.draft.value,'message B edited');
    assert.equal(c.draftByConversation.value.A,'message A');
    assert.equal(c.uploadProgressByConversation.value.A.fileA.phase,'failed');
    props.conversationUuid='A';await vm.runInContext('switchConversation("A","B")',c);
    assert.equal(c.draft.value,'message A');
    assert.equal(c.pendingAttachments.value[0].id,'fileA');
    return;
  }
  upload.resolve([{uploadId:'upload-A'}]);await sending;
  assert.equal(sockets.length,1);
  assert.match(sockets[0].url,/\/A\/ws\?afterFrameSeq=91&bootstrap=incremental/);
  assert.deepEqual(sockets[0].sent.at(-1),{type:'send',requestId:'request-A',text:'message A',files:[{uploadId:'upload-A'}],referenceOrder:[]});
  assert.equal(c.draftByConversation.value.B,'message B edited');
  sockets[0].ack();
  assert.equal(c.outboundSends.current,null);
  assert.equal(c.uploadProgressByConversation.value.A.fileA,undefined);
  props.conversationUuid='A';await vm.runInContext('switchConversation("A","B")',c);
  assert.equal(c.draft.value,'');
  assert.equal(c.pendingAttachments.value.length,0);
});

test('navigation before async preparation resolves still uploads and sends exclusively to A', async()=>{
  const {context:c,props,uploads,upload,sockets}=harness();
  const scroll=deferred();c.scrollBottom=()=>scroll.promise;
  const sending=vm.runInContext('send()',c);
  props.conversationUuid='B';await vm.runInContext('switchConversation("B","A")',c);
  scroll.resolve();
  for(let i=0;i<12 && !uploads.length;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(uploads[0].uuid,'A');
  c.timelinePageConversationUuid='B';c.lastFrameSeq.value=777;
  upload.resolve([{uploadId:'upload-A'}]);await sending;
  assert.match(sockets[0].url,/\/A\/ws\?afterFrameSeq=91&bootstrap=incremental/);
  assert.equal(sockets[0].sent.at(-1).text,'message A');
  sockets[0].ack();
});

test('B timeline socket error/close reconnects B without touching A pending independent ACK', async()=>{
  const {context:c,props,uploads,upload,sockets}=harness();const warnings=[];
  c.ElMessage.warning=value=>warnings.push(value);
  const sending=vm.runInContext('send()',c);
  for(let i=0;i<12 && !uploads.length;i++) await new Promise(resolve=>setImmediate(resolve));
  props.conversationUuid='B';await vm.runInContext('switchConversation("B","A")',c);
  c.timelinePageConversationUuid='B';c.lastFrameSeq.value=777;
  upload.resolve([{uploadId:'upload-A'}]);await sending;
  const ackSocket=sockets[0],pending=c.outboundSends.current;
  assert.equal(pending.phase,'sent');assert.equal(pending.sendSocket,ackSocket);
  assert.match(ackSocket.url,/\/A\/ws\?afterFrameSeq=91&bootstrap=incremental/);
  const timelineSocket=await vm.runInContext('connectWs("B")',c);
  assert.match(timelineSocket.url,/\/B\/ws/);
  timelineSocket.onerror();
  assert.equal(c.status.value,'连接异常');
  assert.equal(c.outboundSends.current,pending);
  timelineSocket.readyState=3;timelineSocket.onclose();
  assert.equal(c.outboundSends.current,pending);
  assert.equal(pending.sendSocket,ackSocket);
  assert.equal(ackSocket.readyState,1);
  assert.equal(warnings.length,0);
  assert.equal(typeof c.reconnect,'function', 'ordinary B timeline reconnection is still scheduled');
  c.reconnect();
  assert.equal(c.wsConversationUuid,'B');
  assert.notEqual(c.ws,timelineSocket);
  assert.equal(c.outboundSends.current,pending);
  ackSocket.ack();
  assert.equal(c.outboundSends.current,null);
  assert.equal(c.draftByConversation.value.B,'message B');
  assert.equal(warnings.length,0);
});

test('local send creation after navigation keeps captured folder/model and never hijacks B', async()=>{
  const {context:c,props}=harness();const events=[],created=[];
  props.conversationUuid='local:new';props.folderId='folder-A';
  c.initialConversationTitle=text=>text;c.referenceDisplayText=text=>text;
  c.Api.createConversation=async payload=>{created.push(payload);return {conversation:{conversationUuid:'server-A'}};};
  c.emit=(...args)=>events.push(args);
  c.outboundSends.begin({requestId:'local-1',conversationUuid:'local:new',localRunConfig:{mainModel:'model-A'},localFolderId:'folder-A'});
  props.conversationUuid='B';props.folderId='folder-B';
  assert.equal(await vm.runInContext('ensureServerConversationForSend("hello",outboundSends.current)',c),'server-A');
  assert.equal(created[0].runConfig.mainModel,'model-A');assert.equal(created[0].folderId,'folder-A');
  assert.equal(props.conversationUuid,'B');assert.equal(c.chatState.value,null);
  assert.deepEqual(events,[['conversations-refresh']]);
  c.outboundSends.take('local-1');
});

test('stale migration marker cannot retarget navigation while another local draft is being created', async t=>{
  const {context:c,props}=harness(),loads=[];
  vm.runInContext(between('function persistComposerDraft(', 'function clearDraftForConversation('),c);
  vm.runInContext(between('function hasOptimisticLocalTurn()', 'function shouldPreserveOptimisticMessages('),c);
  c.load=async()=>{loads.push(props.conversationUuid);};
  c.applyTimelinePageMetadata=noop;
  // A has its own draft and attachment; the next unsaved conversation gets B's.
  props.conversationUuid='local:new';
  await vm.runInContext('switchConversation("local:new","A")',c);
  const fileB={id:'fileB',file:{name:'b.txt',size:8,type:'text/plain'}};
  c.pendingAttachments.value=[fileB];
  c.draft.value='next draft for B';
  c.messages.value=[{id:'local-B',role:'user',content:'question B'}];
  c.foregroundRunning.value=true;
  c.outboundSends.begin({requestId:'creating-B',conversationUuid:'local:new',draftText:'question B',attachments:[fileB]});
  t.after(()=>c.outboundSends.take('creating-B'));
  // Deliberately retain A's old marker: request ownership must protect the UI
  // even if stale state survives from a previous migration.
  c.localToServerTransitionUuid.value='A';
  props.conversationUuid='A';
  await vm.runInContext('switchConversation("A","local:new")',c);
  assert.equal(loads.at(-1),'A','ordinary navigation must reload A instead of taking the migration shortcut');
  assert.equal(c.localToServerTransitionUuid.value,'');
  assert.equal(c.draft.value,'message A');
  assert.equal(c.attachmentsLoadedKey,'A');
  assert.deepEqual(Array.from(c.pendingAttachments.value,item=>item.id),['fileA']);
  assert.equal(c.messages.value.some(message=>message.id==='local-B'),false);
  assert.equal(c.outboundSends.current.conversationUuid,'local:new','B preparation is not cancelled or redirected');
  assert.equal(c.draftByConversation.value['local:new'],'question B\n\nnext draft for B');
  assert.deepEqual(Array.from(c.attachmentsByConversation.get('local:new').attachments,item=>item.id),['fileB']);
  props.conversationUuid='local:new';
  await vm.runInContext('switchConversation("local:new","A")',c);
  assert.equal(c.draft.value,'next draft for B');
  assert.deepEqual(Array.from(c.pendingAttachments.value,item=>item.id),['fileB']);
});

test('switch back during upload restores A live progress and draft editor without duplicating sent text', async()=>{
  const {context:c,props,uploads,upload,sockets}=harness();
  const sending=vm.runInContext('send()',c);
  for(let i=0;i<12 && !uploads.length;i++) await new Promise(resolve=>setImmediate(resolve));
  props.conversationUuid='B';await vm.runInContext('switchConversation("B","A")',c);
  props.conversationUuid='A';await vm.runInContext('switchConversation("A","B")',c);
  assert.equal(c.draft.value,'', 'the editable draft excludes the text being sent');
  assert.equal(c.pendingAttachments.value[0].id,'fileA');
  c.draft.value='next message for A';vm.runInContext('persistComposerDraft(draft.value)',c);
  uploads[0].options.onProgress({loaded:8,total:12,fileIndex:0,fileCount:1,fileLoaded:8,fileSize:12,phase:'uploading'});
  assert.equal(c.uploadProgressByConversation.value.A.fileA.percent,66);
  upload.resolve([{uploadId:'upload-A'}]);await sending;sockets[0].ack();
  assert.equal(c.draftByConversation.value.A,'next message for A');
  assert.equal(c.draft.value,'next message for A');
});

const ast=baseParse(parse(composer).descriptor.template.content);
function walk(nodes){return (nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[])]);}
const strip=walk(ast.children).find(n=>n.type===1 && n.props.some(p=>p.name==='class'&&p.value?.content==='attachment-strip'));
test('actual composer card renders upload bytes, finalization and failure per owner', async()=>{
  const render=async phase=>{
    const props={pendingAttachments:[{id:'fileA',file:{name:'a.txt',size:12}}],uploadProgress:{fileA:{phase,percent:phase==='uploading'?50:100}}};
    const app=createSSRApp({render(){return compile(strip.loc.source).call(this,{props,attachmentPreviewUrl:()=>'',fmtBytes:n=>`${n} B`,emit:noop},[]);}});
    app.component('Document',{render:()=>h('svg')});app.component('Close',{render:()=>h('svg')});
    app.component('ElImage',{render:()=>h('img')});
    app.component('ElTooltip',{inheritAttrs:false,render(){return this.$slots.default?.();}});
    return renderToString(app);
  };
  assert.match(await render('uploading'),/a.txt：上传 50%/);
  assert.match(await render('uploading'),/width:50%/);
  assert.match(await render('finalizing'),/a.txt：保存中/);
  assert.match(await render('failed'),/上传失败，附件已保留/);
});
