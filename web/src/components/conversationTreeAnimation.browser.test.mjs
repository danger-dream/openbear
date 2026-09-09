import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parse,compileStyle} from '@vue/compiler-sfc';
import {chromium} from 'playwright-core';
import {browserExecutable,webRoot} from '../../test-support/realBrowser.mjs';

const executablePath=browserExecutable();
test('running ring keeps rotating with reduced motion, without re-enabling decorative animations', {skip:!executablePath,timeout:20000},async t=>{
  const filename=new URL('./ConversationTree.vue',import.meta.url).pathname;
  const {descriptor}=parse(await readFile(filename,'utf8'),{filename});
  const id='data-v-tree-animation';
  const css=descriptor.styles.map(style=>{
    const result=compileStyle({source:style.content,filename,id,scoped:style.scoped});
    assert.deepEqual(result.errors,[]);return result.code;
  }).join('\n');
  const browser=await chromium.launch({executablePath,headless:true,args:['--no-sandbox']});
  t.after(()=>browser.close());
  const page=await browser.newPage();
  const baseCss=await readFile(webRoot+'/node_modules/element-plus/dist/index.css','utf8');
  await page.setContent(`<style>${baseCss}\n${css}</style>
    <section ${id} class="conversation-tree" style="width:300px">
      <div ${id} id="row" class="tree-row-wrap"><button ${id} class="tree-node-main conversation">
        <i ${id} id="icon" class="el-icon node-icon is-working"><svg viewBox="0 0 24 24"><path d="M3 3h18v15H3z"/></svg></i>
        <span ${id} class="node-label">运行中的会话</span>
        <span ${id} class="running-leaf"><i ${id} id="dot"></i><span>运行中</span></span>
      </button></div>
      <i ${id} id="loading" class="el-icon is-spinning"></i>
    </section>`);
  const sample=()=>page.evaluate(()=>{
    const icon=document.querySelector('#icon'),style=getComputedStyle(icon,'::before');
    const animation=icon.getAnimations({subtree:true}).find(a=>a.animationName?.startsWith('tree-work-border-spin'));
    const box=icon.getBoundingClientRect();
    return {name:style.animationName,duration:style.animationDuration,transform:style.transform,time:animation?.currentTime,start:animation?.startTime,playState:animation?.playState,
      iconTransform:getComputedStyle(icon).transform,width:box.width,height:box.height,rowHeight:document.querySelector('#row').getBoundingClientRect().height,
      dot:getComputedStyle(document.querySelector('#dot')).animationName,loading:getComputedStyle(document.querySelector('#loading')).animationName};
  });
  let geometry;
  for(const dark of [false,true]){
    await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
    for(const reducedMotion of ['no-preference','reduce']){
      await page.emulateMedia({reducedMotion});
      await page.waitForFunction(()=>document.querySelector('#icon').getAnimations({subtree:true}).some(a=>a.currentTime>0));
      const before=await sample();
      assert.match(before.name,/^tree-work-border-spin/);assert.equal(before.duration,'0.9s');assert.equal(before.playState,'running');
      await page.waitForFunction(time=>document.querySelector('#icon').getAnimations({subtree:true}).some(a=>a.animationName?.startsWith('tree-work-border-spin')&&a.currentTime>time+1150),before.time);
      const after=await sample();
      assert.equal(after.start,before.start,'animation is not restarted');assert.notEqual(after.transform,before.transform,'rotation actually advances, rather than merely having a CSS name');
      assert.equal(after.iconTransform,'none','only the ring rotates');
      const sizes=[after.width,after.height,after.rowHeight];geometry??=sizes;assert.deepEqual(sizes,geometry);
      if(reducedMotion==='reduce'){assert.equal(after.dot,'none');assert.equal(after.loading,'none');}
      else {assert.match(after.dot,/tree-pulse/);assert.match(after.loading,/tree-spin/);}
    }
  }
  await page.evaluate(()=>document.querySelector('#icon').classList.remove('is-working'));
  assert.equal((await sample()).name,'none','finishing a run removes its animation');
  await page.evaluate(()=>document.querySelector('#icon').classList.add('is-working'));
  await page.waitForFunction(()=>document.querySelector('#icon').getAnimations({subtree:true}).some(a=>a.currentTime>0));
  assert.equal((await sample()).playState,'running','a new run animates even when reduced motion remains enabled');
});
