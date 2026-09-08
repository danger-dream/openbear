import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref} from 'vue';

const source = fs.readFileSync(new URL('./ConversationPromptDialog.vue', import.meta.url), 'utf8');
const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*;\n/gm, '');
const reviewed = (id = 'a') => ({conversationUuid: id, changed: true, beforeHash: '1'.repeat(64), afterHash: '2'.repeat(64), beforeChars: 1, afterChars: 2, diff: '-old\n+new'});
function defer() { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;});return {promise,resolve,reject}; }
function harness({get = async id => reviewed(id), put = async () => ({updated:true}), local = false} = {}) {
  const props = {modelValue:true, conversation:{conversationUuid:local?'local:new':'a', title:'A', local}};
  const calls = {get:[],put:[],emit:[],messages:[]};
  let changed, unmount;
  const ctx=vm.createContext({ref, defineProps:()=>props, defineEmits:()=>(...args)=>calls.emit.push(args),
    watch:(_get,callback)=>{changed=callback;}, onBeforeUnmount:callback=>{unmount=callback;},
    ElMessage:{success:text=>calls.messages.push(text)}, apiError:error=>String(error),
    Api:{conversationSystemPromptPreview:id=>{calls.get.push(id);return get(id);},
      updateConversationSystemPrompt:(id,body)=>{calls.put.push({id,body});return put(id,body);}},
  });
  vm.runInContext(script+'\nglobalThis.state={preview,loading,saving,errorText};',ctx);
  return {ctx,props,calls,state:ctx.state,run:code=>vm.runInContext(code,ctx),change:open=>changed([open]),unmount:()=>unmount()};
}

test('preview and cancel do not update; late response after closing is ignored',async()=>{
  const pending=defer(),h=harness({get:()=>pending.promise});
  const job=h.run('loadPreview()');h.run('close()');pending.resolve(reviewed());await job;
  assert.equal(h.state.preview.value,null);assert.equal(h.calls.put.length,0);assert.deepEqual(h.calls.emit,[['update:modelValue',false]]);
});
test('save sends only the reviewed conversation and exact hashes without another confirmation',async()=>{
  const h=harness();await h.run('loadPreview()');h.props.conversation={conversationUuid:'other'};
  await h.run('save()');assert.equal(h.calls.put.length,1);assert.equal(h.calls.put[0].id,'a');
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls.put[0].body)),{confirmed:true,beforeHash:'1'.repeat(64),afterHash:'2'.repeat(64)});
  assert.equal(h.calls.messages.length,1);assert.equal(h.state.saving.value,false);assert.equal(h.state.preview.value,null);
});
test('double submit is suppressed and dialog cannot close during update',async()=>{
  const pending=defer(),h=harness({put:()=>pending.promise});await h.run('loadPreview()');const job=h.run('save()');
  await h.run('save()');h.run('close()');assert.equal(h.calls.put.length,1);assert.equal(h.calls.emit.length,0);
  pending.resolve({updated:true});await job;assert.equal(h.calls.emit.length,1);
});
test('changed preview fails closed, keeps dialog open, and needs explicit repreview',async()=>{
  const h=harness({put:async()=>{throw {response:{data:{error:'prompt_preview_changed'}}};}});await h.run('loadPreview()');await h.run('save()');
  assert.equal(h.calls.emit.length,0);assert.equal(h.state.preview.value,null);assert.match(h.state.errorText.value,/重新预览/);
  await h.run('save()');assert.equal(h.calls.put.length,1);assert.equal(h.calls.get.length,1);
});
test('busy and unchanged previews cannot be submitted',async()=>{
  const h=harness({get:async()=>({...reviewed(),changed:false})});await h.run('loadPreview()');await h.run('save()');assert.equal(h.calls.put.length,0);
  const busy=harness({get:async()=>{throw {response:{data:{error:'busy'}}};}});await busy.run('loadPreview()');await busy.run('save()');
  assert.match(busy.state.errorText.value,/不会延后自动更新/);assert.equal(busy.calls.put.length,0);
});
test('local draft never requests preview or update',async()=>{
  const h=harness({local:true});await h.run('loadPreview()');await h.run('save()');assert.deepEqual(h.calls.get,[]);assert.deepEqual(h.calls.put,[]);
});
test('switching target invalidates older request and resets loading correctly',async()=>{
  const old=defer(),h=harness({get:id=>id==='a'?old.promise:Promise.resolve(reviewed(id))});
  const job=h.run('loadPreview()');h.props.conversation={conversationUuid:'b'};h.change(true);await Promise.resolve();await Promise.resolve();
  old.resolve(reviewed('a'));await job;assert.equal(h.state.preview.value.conversationUuid,'b');assert.equal(h.state.loading.value,false);
});
test('unmount discards private preview result',async()=>{
  const pending=defer(),h=harness({get:()=>pending.promise});const job=h.run('loadPreview()');h.unmount();pending.resolve(reviewed());await job;assert.equal(h.state.preview.value,null);
});
test('menu respects running/local state and diff is rendered as text, never HTML',()=>{
  const tree=fs.readFileSync(new URL('./ConversationTree.vue',import.meta.url),'utf8');
  assert.match(tree,/:disabled="menu\.row\?\.local \|\| running\(menu\.row\)" @click="runMenuAction\('refresh-prompt'\)"/);
  assert.match(source,/<pre>\{\{ preview\.diff \}\}<\/pre>/);assert.doesNotMatch(source,/v-html|ElMessageBox/);
});
