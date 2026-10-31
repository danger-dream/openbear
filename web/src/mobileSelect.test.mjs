import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import postcss from 'postcss';
import {mobileSelectOptions} from './mobileSelect.js';

function harness({mobile=true, height=260, viewport=true}={}) {
  const frames=new Map(), classes=new Set(), values=new Map();let id=0,updates=0,nextOptions;
  const win=Object.assign(new EventTarget(),{innerHeight:height,mobile,matchMedia(){return {matches:this.mobile};},requestAnimationFrame(fn){frames.set(++id,fn);return id;},cancelAnimationFrame(key){frames.delete(key);}});
  win.visualViewport=viewport?Object.assign(new EventTarget(),{height,offsetTop:80,scale:1}):null;
  const panel={classList:{contains:n=>classes.has(n),add:n=>classes.add(n),remove:n=>classes.delete(n)},style:{getPropertyValue:n=>values.get(n)||'',getPropertyPriority:()=>'',setProperty:(n,v)=>values.set(n,v),removeProperty:n=>values.delete(n)}};
  const options=mobileSelectOptions(win), state={elements:{popper:panel},options};
  const cleanup=options.modifiers.find(m=>m.name==='mobileSelectViewport').effect({state,instance:{update(){updates++;},setOptions(fn){nextOptions=fn(state.options);}}});
  return {win,state,options,values,classes,cleanup,get updates(){return updates;},get nextOptions(){return nextOptions;},flush(){const jobs=[...frames.values()];frames.clear();jobs.forEach(fn=>fn());}};
}
test('mobile selects size their own list and reposition once per keyboard resize/pan frame',()=>{
  const h=harness();assert.equal(h.options.strategy,'fixed');assert.equal(h.values.get('--mobile-select-height'),'260px');
  assert.deepEqual(h.options.modifiers[0].options,{altAxis:true,tether:false,padding:12});
  h.win.visualViewport.height=220;for(const name of ['resize','scroll','resize'])h.win.visualViewport.dispatchEvent(new Event(name));
  h.flush();assert.equal(h.updates,1);assert.equal(h.values.get('--mobile-select-height'),'220px');
  h.win.visualViewport.scale=2;h.win.visualViewport.height=110;h.win.visualViewport.dispatchEvent(new Event('resize'));h.flush();assert.equal(h.values.get('--mobile-select-height'),'220px');
  h.win.visualViewport.dispatchEvent(new Event('scroll'));h.cleanup();h.flush();assert.equal(h.updates,2);assert.equal(h.classes.size,0);assert.equal(h.values.size,0);
  h.win.visualViewport.dispatchEvent(new Event('resize'));h.flush();assert.equal(h.updates,2);
});
test('desktop retains normal placement and rotation switches the complete mobile modifier options',()=>{
  const h=harness({mobile:false});assert.equal(h.options.strategy,'absolute');assert.deepEqual(h.options.modifiers[0].options,{});assert.equal(h.values.size,0);
  h.win.mobile=true;h.win.dispatchEvent(new Event('resize'));h.flush();assert.equal(h.nextOptions.strategy,'fixed');assert.equal(h.nextOptions.modifiers[0].options.tether,false);h.cleanup();
  const fallback=harness({height:180,viewport:false});assert.equal(fallback.values.get('--mobile-select-height'),'180px');fallback.cleanup();
});
test('all nine filterable select entry points use the common viewport options without replacing their filters',()=>{
  const paths=['ChannelsView','MemoryView','DocsView','SecretsView','RathAgentsView','TemplateView','consoleView/TaskMemoryDrawer'];let count=0;
  for(const path of paths){
    const s=fs.readFileSync(new URL(`./views/${path}.vue`,import.meta.url),'utf8');const ast=baseParse(parse(s).descriptor.template.content);
    const visit=n=>{if(n.type===1&&n.tag==='el-select'&&n.props.some(p=>p.name==='filterable')){count++;assert.ok(n.props.some(p=>p.name==='bind'&&p.arg?.content==='popper-options'&&p.exp?.content==='mobileSelectOptions()'),path);}
      for(const c of n.children||[])visit(c);};visit(ast);
  }
  assert.equal(count,9);
});
test('dropdown scroller never exceeds the shorter viewport, retains normal 274px cap and safe areas',()=>{
  const root=postcss.parse(fs.readFileSync(new URL('./mobile-overlays.css',import.meta.url),'utf8'));let limit;
  root.walkRules('html .mobile-select-popper .el-select-dropdown__wrap',r=>{assert.equal(r.parent.params,'(max-width: 760px), (hover: none) and (pointer: coarse)');r.walkDecls('max-height',d=>limit=d.value);});
  assert.equal(limit,'max(0px, min(274px, calc(var(--mobile-select-height, var(--mobile-viewport-height, 100dvh)) - 32px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px))))');
});
