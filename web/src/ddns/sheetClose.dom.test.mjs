import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h, reactive} from 'vue';
import {openPage} from '../testing/appHarness.mjs';
import {settle, sleep} from '../testing/domPage.mjs';
const Sheet = (await import('./DdnsSheet.vue')).default;
const MeshSheet = (await import('../servers/mesh/MeshSheet.vue')).default;

async function setup(t, extra = {}, Component = Sheet) {
 const props=reactive({modelValue:true,phone:true,title:'设置',...extra});
 const events=[];
 const {page,unmount}=await openPage(t,{render:()=>h(Component,{...props,'onUpdate:modelValue':value=>{events.push(value);props.modelValue=value;}},{default:()=>h('input',{'aria-label':'内容'})})},{width:390,height:844});
 await page.locator('.mesh-sheet-head').waitFor();
 await settle();
 return {page,props,events,unmount};
}
async function attempt(kind, page) {
 if (kind==='button') await page.locator('.mesh-sheet-close').click();
 if (kind==='overlay') {
  const el=document.querySelector('.mesh-sheet-overlay');
  for (const type of ['mousedown','mouseup','click']) el.dispatchEvent(new window.MouseEvent(type,{bubbles:true}));
 }
 if (kind==='escape') document.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true}));
 if (kind==='drag') {
  const head=document.querySelector('.mesh-sheet-head');
  for (const [type,y] of [['pointerdown',10],['pointermove',150],['pointerup',150]]) head.dispatchEvent(new window.PointerEvent(type,{pointerId:1,clientX:30,clientY:y,button:0,bubbles:true}));
 }
 await settle(); await sleep(80); await settle();
}
for (const kind of ['button','overlay','escape','drag']) test(`phone busy without dirty rejects ${kind}, then allows it once idle`, async t => {
 const p=await setup(t,{busy:true});
 await attempt(kind,p.page);
 assert.equal(p.props.modelValue,true); assert.deepEqual(p.events,[]);
 assert.equal(document.querySelector('.mesh-sheet').style.transform,'','rejected drag returns to rest');
 p.props.busy=false; await settle();
 await attempt(kind,p.page);
 await p.page.locator('.mesh-sheet').waitFor({state:'hidden'});
 assert.equal(p.props.modelValue,false);
 await p.unmount();
});

test('dirty still asks before leaving; cancel keeps it open and discard closes it', async t => {
 const p=await setup(t,{dirty:true});
 await attempt('button',p.page);
 await p.page.getByRole('button',{name:'继续编辑'}).click();
 await settle(); assert.equal(p.props.modelValue,true);
 await attempt('button',p.page);
 await p.page.getByRole('button',{name:'放弃修改',exact:true}).click();
 await p.page.locator('.mesh-sheet').waitFor({state:'hidden'});
 assert.equal(p.props.modelValue,false); await p.unmount();
});
for (const Component of [Sheet,MeshSheet]) test(`unguarded ${Component===Sheet?'DDNS':'mesh'} caller still closes without a confirmation`, async t => {
 const p=await setup(t,{},Component);
 await attempt('button',p.page);
 await p.page.locator('.mesh-sheet').waitFor({state:'hidden'});
 assert.equal(p.props.modelValue,false);
 assert.equal(document.querySelector('.el-message-box'),null);
 await p.unmount();
});
