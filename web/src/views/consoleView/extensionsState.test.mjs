import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {computed, ref, reactive, watch, effectScope, nextTick} from 'vue';
import {extensionDraft, setExtensionDraft, forgetExtensionDraft, onExtensionDraftForgotten} from './extensionDrafts.js';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const source = read('./ExtensionsMenu.vue');
const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*;\n/gm, '');
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; };
const state = (enabled = false, revision = 1) => ({revision, autoAllow: enabled, extensions: [{key:'servers',enabled}, {key:'browser',enabled:false}]});
const folder = () => ({default:{enabled:[],autoAllow:false},extensions:state().extensions});
function panel(t, api = {}, initial = {}) {
 const props = reactive({conversationUuid:'A',folderId:'F1',running:false,...initial});
 const scope = effectScope(), cleanup = [];
 const make = new Function('defineProps','ref','computed','watch','onMounted','onBeforeUnmount','Api','extensionDraft','setExtensionDraft','onExtensionDraftForgotten','fmtTokens', script + '\nreturn {load,change,changeDraft,state,loading,saving,error,canChange};');
 const p = scope.run(() => make(() => props,ref,computed,watch,()=>{},fn=>cleanup.push(fn),{conversationExtensions:async()=>state(),folderExtensions:async()=>folder(),...api},extensionDraft,setExtensionDraft,onExtensionDraftForgotten,String));
 const stop = () => {cleanup.splice(0).forEach(fn=>fn()); scope.stop();};
 t.after(() => {stop(); forgetExtensionDraft('local:new');});
 return {...p,props,stop};
}

test('a new identity clears old state/errors and cannot edit before successful loading', async t => {
 const get = deferred();
 const p = panel(t,{conversationExtensions:async()=>state(true),folderExtensions:()=>get.promise});
 await p.load(); p.error.value='old error';
 p.props.conversationUuid='local:new'; await nextTick();
 assert.equal(p.state.value,null); assert.equal(p.error.value,''); assert.ok(!p.canChange.value);
 p.changeDraft({enable:['browser']}); assert.equal(extensionDraft('local:new'),null);
 get.reject(new Error('offline')); await nextTick(); await nextTick();
 assert.equal(p.state.value,null); assert.ok(!p.canChange.value);
 p.changeDraft({autoAllow:true}); assert.equal(extensionDraft('local:new'),null);
 // All four switches (phone and desktop) use the identity-aware gate.
 assert.equal((source.match(/:disabled="!canChange"/g)||[]).length,4);
});

test('folder changes invalidate late loads; normal navigation preserves explicit draft choices', async t => {
 const old = deferred(); let calls=0;
 const p = panel(t,{folderExtensions:()=> ++calls===1 ? old.promise : Promise.resolve(folder())},{conversationUuid:'local:new'});
 const loading=p.load(); p.props.folderId='F2'; await nextTick();
 assert.equal(p.state.value,null); await nextTick(); await nextTick();
 p.changeDraft({enable:['browser']});
 old.resolve({default:{enabled:['servers'],autoAllow:true},extensions:state().extensions}); await loading;
 assert.deepEqual(extensionDraft('local:new'),{enabled:['browser'],autoAllow:false});
 assert.equal(p.state.value.autoAllow,false);
 p.props.conversationUuid='A'; await nextTick();
 p.props.conversationUuid='local:new'; await nextTick(); await nextTick();
 assert.equal(p.state.value.extensions[1].enabled,true);
});

test('a late GET cannot overwrite a successful write', async t => {
 const get=deferred(), put=deferred(); let reads=0;
 const p=panel(t,{conversationExtensions:()=> ++reads===1 ? Promise.resolve(state(true)) : reads===2 ? get.promise : Promise.resolve(state(true,1)),updateConversationExtensions:()=>put.promise});
 await p.load(); const loading=p.load(); const saving=p.change({disable:['servers']},'servers');
 await p.load(); assert.equal(reads,2,'refresh cannot supersede an in-flight write');
 put.resolve(state(false,2)); await saving; get.resolve(state(true,1)); await loading;
 assert.equal(p.state.value.revision,2); assert.equal(p.state.value.extensions[0].enabled,false);
});

test('an old visit PUT cannot overwrite a new visit or clear its saving indicator', async t => {
 const a=deferred(), b=deferred(); let writes=0;
 const p=panel(t,{updateConversationExtensions:()=> ++writes===1 ? a.promise : b.promise});
 await p.load(); const first=p.change({autoAllow:true},'autoAllow');
 p.props.conversationUuid='B'; await nextTick(); await nextTick();
 p.props.conversationUuid='A'; await nextTick(); await nextTick();
 const second=p.change({enable:['browser']},'browser');
 a.resolve(state(true,8)); await first;
 assert.equal(p.saving.value,'browser'); assert.equal(p.state.value.autoAllow,false);
 b.resolve(state(false,9)); await second; assert.equal(p.saving.value,'');
});

// Exercise the actual deletion entry points with unrelated text/attachment storage stubbed.
function discard(mounted) {
 const noop=()=>{};
 const view=read('./ConsoleView.vue');
 const body=view.slice(view.indexOf('function discardConversationDraft('),view.indexOf('async function savePropertyRunConfig('));
 const deps={clearDraftForConversation:noop,forgetExtensionDraft,forgetLocalDraftChoices:noop,attachmentHydrations:new Map(),draftKey:x=>x,activeAttachmentKey:()=>'',attachmentRestoring:ref(false),releaseStashedAttachments:noop,attachmentDrafts:{remove:noop},props:{conversationUuid:'local:new'},draft:ref(''),clearAttachments:noop,adjustComposerHeight:noop};
 const consoleDiscard=new Function(...Object.keys(deps),body+'; return discardConversationDraft;')(...Object.values(deps));
 const app=read('../../App.vue');
 const appBody=app.slice(app.indexOf('function discardConversationDraft('),app.indexOf('async function conversationAfterRemoval('));
 const appDeps={consoleViewRef:ref(mounted?{discardConversationDraft:consoleDiscard}:null),forgetExtensionDraft,userStorage:()=>({getItem:()=>null}),window:{localStorage:{}},attachmentDrafts:{remove:noop}};
 return new Function(...Object.keys(appDeps),appBody+'; return discardConversationDraft;')(...Object.values(appDeps));
}
for (const mounted of [true,false]) test(`deleting a draft clears choices and mounted panel cache (ConsoleView mounted=${mounted})`, async t => {
 setExtensionDraft('local:new',{enabled:['servers'],autoAllow:true});
 const p=panel(t,{}, {conversationUuid:'local:new'}); await p.load();
 assert.equal(p.state.value.autoAllow,true);
 discard(mounted)('local:new');
 assert.equal(extensionDraft('local:new'),null); assert.equal(p.state.value,null); assert.ok(!p.canChange.value);
 await nextTick(); await nextTick();
 assert.equal(p.state.value.autoAllow,false); assert.equal(p.state.value.extensions[0].enabled,false);
});

test('unmount unregisters draft invalidation', async t => {
 const p=panel(t,{},{conversationUuid:'local:new'}); await p.load();
 p.stop(); forgetExtensionDraft('local:new'); assert.ok(p.state.value);
});
