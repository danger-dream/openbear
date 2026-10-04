import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref} from 'vue';
const source=fs.readFileSync(new URL('./ConsoleComposer.vue',import.meta.url),'utf8');
const code=source.slice(source.indexOf('const SEND_STYLE_KEY'),source.indexOf('function focusInteraction'));
function harness(saved='round',canSend=true){
 let timer;const events=[];const storage=new Map([['openbear.console.sendButtonStyle.v1',saved]]);
 const c=vm.createContext({ref,props:{canSend},emit:e=>events.push(e),watch(){},window:{localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},setTimeout:f=>(timer=f,1),clearTimeout:()=>timer=null}});
 vm.runInContext(code,c);return {c,events,storage,tick(){timer?.();},run(s){return vm.runInContext(s,c);}};
}
test('long press switches full styles, persists and suppresses send; next short click sends',()=>{
 const h=harness();h.run('startSendStylePress({button:0,isPrimary:true,pointerId:1,clientX:0,clientY:0})');h.tick();
 assert.equal(h.run('sendButtonStyle.value'),'original');assert.equal(h.storage.values().next().value,'original');
 h.run('cancelSendStylePress();clickSendButton({detail:1,preventDefault(){}})');assert.deepEqual(h.events,[]);
 h.run('startSendStylePress({button:0,pointerId:2,clientX:0,clientY:0});cancelSendStylePress();clickSendButton({detail:1})');assert.deepEqual(h.events,['send']);
 assert.equal(harness('original').run('sendButtonStyle.value'),'original');
});
test('empty input can toggle but cannot send; drag and cancellation cancel long press',()=>{
 const h=harness('round',false);h.run('startSendStylePress({button:0,pointerId:1,clientX:0,clientY:0})');h.tick();h.run('clickSendButton({detail:1,preventDefault(){}});clickSendButton({detail:0})');assert.deepEqual(h.events,[]);
 h.run('startSendStylePress({button:0,pointerId:2,clientX:0,clientY:0});moveSendStylePress({pointerId:2,clientX:20,clientY:0})');h.tick();assert.equal(h.run('sendButtonStyle.value'),'original');
 h.run('startSendStylePress({button:0,pointerId:3,clientX:0,clientY:0});cancelSendStylePress()');h.tick();assert.equal(h.run('sendButtonStyle.value'),'original');
});
test('context menu after timer toggles only once and missing storage is safe',()=>{
 const h=harness();h.run('startSendStylePress({button:0,pointerId:1,clientX:0,clientY:0})');h.tick();h.run('sendStyleContextMenu();clickSendButton({detail:1,preventDefault(){}})');assert.equal(h.run('sendButtonStyle.value'),'original');assert.deepEqual(h.events,[]);
 h.run('window.localStorage.getItem=()=>{throw Error()};window.localStorage.setItem=()=>{throw Error()};toggleSendStyle()');assert.equal(h.run('readSendStyle()'),'original');
});
test('both reference and original styles retain their colors, icons and dimensions',()=>{
 for(const s of ['background: #fff','background: #ff5058','background: #414141','width: 38px; height: 38px','background: var(--ob-chat-button)','background: var(--ob-danger)','<Promotion v-if','M12 20V4M5 11l7-7 7 7'])assert.ok(source.includes(s),s);
 assert.match(source,/@click="emit\('stop'\)"/);
});

test('press hides the send tooltip and circle matches original dimensions',()=>{
 assert.match(source,/:disabled="sendStylePressActive" :visible="sendStylePressActive \? false : undefined"/);
 assert.match(source,/sendStylePressActive.value = true/);
 assert.match(source,/sendStylePressActive.value = false/);
 assert.match(source,/\.send-button \{\s*width: 2rem;\s*height: 2rem;/);
});

test("new browsers default to original square while explicit round stays saved",()=>{
 assert.equal(harness(null).run("sendButtonStyle.value"),"original");
 assert.equal(harness("bad").run("sendButtonStyle.value"),"original");
 assert.equal(harness("round").run("sendButtonStyle.value"),"round");
});
