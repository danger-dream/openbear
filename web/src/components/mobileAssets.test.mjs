import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import * as Icons from '@element-plus/icons-vue';
import {ElButton, ElCheckbox, ElSwitch, ElTag, ElIcon} from 'element-plus';
import draggable from 'vuedraggable';
import {renderToString} from 'vue/server-renderer';
import {parse, compileScript, compileStyle, compileTemplate} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import postcss from 'postcss';
import {mobileAssetsRuntime} from '../testHelpers/mobileAssets.mjs';
import {assetTimeLine, formatAssetTime} from '../utils/assetTime.js';

const read = file => fs.readFileSync(new URL(file, import.meta.url), 'utf8');
const names = ['Memory','Secrets','Docs'];
const sfcs = Object.fromEntries([...names.map(n=>`../views/${n}View.vue`), './MobileAssetRow.vue','./MobileAssetSheet.vue'].map(path => [path, parse(read(path)).descriptor]));
const row = sfcs['./MobileAssetRow.vue'], sheet = sfcs['./MobileAssetSheet.vue'];
const clean = source => source.replace(/^import .*;\n/gm, '');
const walk = nodes => (nodes || []).flatMap(n => [n, ...walk(Array.isArray(n.children) ? n.children : []), ...walk(n.component?.subTree ? [n.component.subTree] : [])]);
const hasClass = (n,c) => n.props?.some?.(p=>p.name==='class'&&p.value?.content.split(/\s+/).includes(c));
const vnodeClass = (n,c) => typeof n.props?.class === 'string' && n.props.class.split(/\s+/).includes(c);
const deferred = () => {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
function rowComponent() {
  return {props:['title','summary','reference','meta','enabled','archived','expanded','selected','mode'], emits:['open','more','select'], setup(props,{emit}) {return {props,emit};}, render:Vue.compile(row.template.content)};
}
async function render(markup, scope, components = {}) {
  const compiled = Vue.compile(markup); let tree;
  const app=Vue.createSSRApp({render(){tree=compiled.call(this,scope,[]);return tree;}});
  let emptyTagTypeWarning = false;
  app.config.warnHandler=message=>{
    // Existing desktop P3 tags use an empty Element Plus type; preserve that markup.
    if (message === 'Invalid prop: validation failed for prop "type". Expected one of ["primary", "success", "info", "warning", "danger"], got value "".') { emptyTagTypeWarning = true; return; }
    if (emptyTagTypeWarning && message === 'Invalid prop: custom validator check failed for prop "type".') { emptyTagTypeWarning = false; return; }
    assert.fail(message);
  };
  app.directive('loading',{getSSRProps:()=>({})});
  for(const [name,component] of Object.entries({ElButton,ElCheckbox,ElSwitch,ElTag,ElIcon,...Icons,draggable,MobileAssetRow:rowComponent(),...components}))app.component(name,component);
  const html=await renderToString(app);return {html,tree,nodes:walk([tree])};
}
function state(overrides={}) {
  const rows=Vue.ref([{id:1,title:'一',enabled:1},{id:2,title:'二',enabled:0}]);
  const selected=new Set(),calls=[],notices=[],unmount=[];
  const runtime=mobileAssetsRuntime({phone:true,onBeforeUnmount:fn=>unmount.push(fn),notices});
  const controller=runtime.useMobileAssets({items:rows,loadDetail:async id=>{calls.push(['read',id]);return {item:{id,body:'完整正文'}};},isSelected:id=>selected.has(id),setSelected:(id,v)=>v?selected.add(id):selected.delete(id),clearSelection:()=>selected.clear(),actions:{enabled:async item=>{calls.push(['enabled',item.id]);rows.value=rows.value.map(r=>r.id===item.id?{...r,enabled:!r.enabled}:r);},archive:async()=>{throw 'cancel';},edit:async item=>calls.push(['edit',item.id])},...overrides});
  return {c:controller,rows,calls,selected,notices,unmount,isPhone:runtime.isPhone};
}

test('browse/select/sort states separate read, selection and drag; rotation resets only presentation state',async()=>{
  const s=state();s.c.setMobileMode('select');await s.c.openMobileAsset(s.rows.value[0]);assert.ok(s.selected.has(1));assert.equal(s.calls.length,0);
  s.c.setMobileMode('sort');assert.equal(s.selected.size,0);await s.c.openMobileAsset(s.rows.value[0]);assert.equal(s.calls.length,0);
  s.c.setMobileMode('browse');await s.c.openMobileAsset(s.rows.value[0],'actions');assert.equal(s.calls.length,0);
  await s.c.runMobileAction('enabled');assert.equal(s.c.mobileItem.value.enabled,false);assert.equal(s.c.mobileOpen.value,true);
  await s.c.runMobileAction('archive');assert.equal(s.c.mobileOpen.value,true);assert.equal(s.notices.length,0);
  s.isPhone.value=false;await Vue.nextTick();assert.equal(s.c.mobileOpen.value,false);assert.equal(s.c.mobileMode.value,'browse');
  s.unmount.forEach(fn=>fn());
});

test('late detail responses cannot replace a newer item, reopen closed sheets, or survive unmount; failures can retry',async()=>{
  const first=deferred(),second=deferred();let count=0;
  const s=state({loadDetail:()=>++count===1?first.promise:second.promise});
  const a=s.c.openMobileAsset(s.rows.value[0]),b=s.c.openMobileAsset(s.rows.value[1]);
  second.resolve({item:{id:2,body:'second'}});await b;first.resolve({item:{id:1,body:'first'}});await a;
  assert.equal(s.c.mobileDetail.value.id,2);
  s.c.closeMobileAsset();await Vue.nextTick();assert.equal(s.c.mobileDetail.value,null);
  const pending=deferred();const t=state({loadDetail:()=>pending.promise});const request=t.c.openMobileAsset(t.rows.value[0]);t.unmount.forEach(fn=>fn());pending.resolve({item:{id:1}});await request;assert.equal(t.c.mobileOpen.value,false);assert.equal(t.c.mobileDetail.value,null);
  let failed=true;const retry=state({loadDetail:async id=>{if(failed)throw new Error('读取失败');return {item:{id,body:'recovered'}};}});
  await retry.c.openMobileAsset(retry.rows.value[0]);assert.equal(retry.c.mobileError.value,'读取失败');failed=false;await retry.c.reloadMobileDetail();assert.equal(retry.c.mobileDetail.value.body,'recovered');assert.equal(retry.c.mobileError.value,'');
});

test('mutation reentry is blocked and edit closes the sheet before invoking the existing editor',async()=>{
  const gate=deferred();let writes=0;const s=state({actions:{enabled:async()=>{writes++;await gate.promise;}}});await s.c.openMobileAsset(s.rows.value[0],'actions');
  const run=s.c.runMobileAction('enabled');await s.c.runMobileAction('enabled');await s.c.openMobileAsset(s.rows.value[1]);assert.equal(writes,1);assert.equal(s.c.mobileItem.value.id,1);gate.resolve();await run;
  const e=state();await e.c.openMobileAsset(e.rows.value[0],'actions');await e.c.runMobileAction('edit');assert.equal(e.c.mobileOpen.value,false);assert.deepEqual(e.calls,[['edit',1]]);
});

test('actual compact row renders readable values; only selection mode has checkbox and only sort mode has touch handle',async()=>{
  for(const mode of ['browse','select','sort']){
    const events=[],props={title:'很长的资料标题',summary:'摘要正文',reference:'@mem/example',meta:'328 tk',enabled:true,archived:false,expanded:true,selected:true,mode};
    const view=await render(row.template.content,{props,emit:(...args)=>events.push(args)});
    for(const text of ['很长的资料标题','摘要正文','@mem/example','328 tk','展开'])assert.ok(view.html.includes(text));
    assert.equal(view.nodes.some(n=>vnodeClass(n,'asset-select')),mode==='select');assert.equal(view.nodes.some(n=>vnodeClass(n,'drag-handle')),mode==='sort');assert.equal(view.nodes.some(n=>vnodeClass(n,'asset-row-more')),mode==='browse');
    const main=view.nodes.find(n=>vnodeClass(n,'asset-row-main'));
    if(mode==='sort')assert.equal(main.props.disabled,true);else {main.props.onClick();assert.deepEqual(events[0],mode==='select'?['select',false]:['open']);}
    if(mode==='browse'){view.nodes.find(n=>vnodeClass(n,'asset-row-more')).props.onClick();assert.deepEqual(events.at(-1),['more']);}
  }
});

async function renderSheet(overrides={}){
  const props=Vue.reactive({modelValue:true,item:{id:1,name:'fixture',title:'测试条目',enabled:1,archived:0,sort:20},detail:{id:1,name:'fixture',content:'全文正文',kv:[{key:'token',value:'fixture-secret-value'}],importance:4,tags:'甲,乙',project:'项目'},kind:'docs',view:'detail',tokens:328,secretValues:false,loading:false,error:'',busy:false,...overrides});
  const events=[],ctx=vm.createContext({...Vue,defineProps:()=>props,defineEmits:()=> (...args)=>events.push(args),formatAssetTime,copyTextToClipboard:async()=>{},ElMessage:{success(){},error(){}}});
  vm.runInContext(clean(sheet.scriptSetup.content),ctx);
  const scope=Vue.proxyRefs(vm.runInContext('({props,emit,title,reference,fields,maskValue,copyReference})',ctx));
  const Drawer={props:['modelValue','title'],emits:['update:modelValue'],render(){return this.modelValue?Vue.h('section',this.$slots.default?.()):null;}};
  return {...await render(sheet.template.content,scope,{ElDrawer:Drawer}),props,events};
}
test('actual sheet shows full document metadata/text, defaults credential fields to masked, and preserves action/disabled semantics',async()=>{
  let view=await renderSheet();for(const text of ['全文正文','P4','甲,乙','项目','历史数据，时间不可追溯'])assert.ok(view.html.includes(text),text);
  view=await renderSheet({kind:'secrets'});assert.doesNotMatch(view.html,/fixture-secret-value/);assert.match(view.html,/fi••••••ue/);
  const reveal=view.nodes.find(n=>n.type==='button'&&n.children?.some?.(child=>child.children==='显示明文'));
  assert.ok(reveal);reveal.props.onClick();assert.deepEqual(view.events.at(-1),['update:secretValues',true]);
  view=await renderSheet({kind:'secrets',secretValues:true});assert.match(view.html,/fixture-secret-value/);
  view=await renderSheet({view:'actions',kind:'memory',item:{id:1,title:'条目',ref:'ref',enabled:0,archived:1,expanded:1}});
  const switches=view.nodes.filter(n=>n.props?.role==='switch');assert.equal(switches.length,2);for(const button of switches)assert.equal(button.props.disabled,true);
  const buttons=view.nodes.filter(n=>n.type==='button'&&vnodeClass(n,'asset-sheet-action')&&!n.props.disabled);
  for(const button of buttons)if(!button.children?.some?.(c=>c.children==='复制引用'))button.props.onClick();
  assert.deepEqual(view.events.filter(e=>e[0]==='action').map(e=>e[1]),['edit','archive','remove']);
});

function page(name,phone=true){
  const sfc=sfcs[`../views/${name}View.vue`],script=compileScript(sfc,{id:name}),calls=[];
  const api=new Proxy({}, {get:(_,method)=>(...args)=>{calls.push({method,args});return Promise.resolve({ok:true,item:{...sample[0],content:'全文'},items:[]});}});
  const ctx=vm.createContext({...Vue,onMounted:()=>{},onBeforeUnmount:()=>{},defineProps:()=>({activeType:'memory'}),defineEmits:()=>()=>{},Api:api,apiError:e=>e.message,ElMessage:{success(){},warning(){},error(){}},ElMessageBox:{confirm:async()=>{}},encode:text=>[...text],pinyin:text=>[text],assetTimeLine,formatAssetTime,useMobileAssets:mobileAssetsRuntime({phone}).useMobileAssets});
  vm.runInContext(clean(sfc.scriptSetup.content),ctx);
  const key={Memory:'entries',Secrets:'secrets',Docs:'docs'}[name];ctx.fixtures=structuredClone(sample);vm.runInContext(`${key}.value=fixtures;rebuildGroups()`,ctx);
  const imports=new Set(['Api','apiError','draggable','MdEditor','AdminPageHeader','MobileAssetRow','MobileAssetSheet','MobileAdminSummary','encode','pinyin','dragAutoScrollOptions','useMobileAssets']);
  const bindings=Object.keys(script.bindings).filter(k=>script.bindings[k]!=='props'&&!imports.has(k)&&!['computed','onMounted','onBeforeUnmount','ref','watch'].includes(k));
  const scope=Vue.proxyRefs(vm.runInContext(`({${bindings.join(',')}})`,ctx));scope.dragAutoScrollOptions={};
  const list=walk(baseParse(sfc.template.content).children).find(n=>hasClass(n,'admin-list'));
  return {scope,calls,run:c=>vm.runInContext(c,ctx),render:()=>render(list.loc.source,scope)};
}
const sample=[{id:1,title:'条目一',name:'first',ref:'first',body:'完整正文一',summary:'摘要一',note:'说明一',grp:'项目',enabled:1,archived:0,expanded:0,sort:10,importance:3,project:'OpenBear',tags:'标签',kv:[{key:'token',value:'secret-fixture'}],updated_at:1790330000},{id:2,title:'条目二',name:'second',ref:'second',body:'正文二',summary:'摘要二',grp:'项目',enabled:0,archived:0,expanded:0,sort:20,importance:2,kv:[]}];

test('real three-page + vuedraggable SSR keeps single item roots, desktop cards and exact reorder payloads',async()=>{
  for(const name of names){
    const p=page(name,true);let view=await p.render();assert.equal(view.nodes.filter(n=>vnodeClass(n,'mobile-asset-row')).length,2,name);assert.doesNotMatch(view.html,/class="(?:memory|secret|doc)-card/);
    for(const n of view.nodes.filter(n=>n.type===draggable))assert.equal(n.props.disabled,true,name);
    if(name==='Secrets')assert.doesNotMatch(view.html,/secret-fixture/);
    p.run("setMobileMode('select')");view=await p.render();view.nodes.find(n=>vnodeClass(n,'asset-row-main')).props.onClick();assert.deepEqual(Array.from(p.run('selectedIds.value')),[1]);assert.equal(p.calls.length,0);
    p.run("setMobileMode('sort')");view=await p.render();for(const n of view.nodes.filter(n=>n.type===draggable))assert.equal(n.props.disabled,false,name);
    p.run("groups.value.find(g=>g.name==='项目').items.reverse()");await p.run('finishDrag()');await Vue.nextTick();
    const reorder=p.calls.find(c=>c.method==='reorder');assert.ok(reorder,name);assert.equal(reorder.args[0],{Memory:'entries',Secrets:'secrets',Docs:'docs'}[name]);assert.deepEqual(Array.from(reorder.args[1],r=>r.id),[2,1]);assert.deepEqual(Array.from(reorder.args[1],r=>r.sort),[10,20]);assert.ok(reorder.args[1].every(r=>r.grp==='项目'));
    const desktop=await page(name,false).render();assert.equal(desktop.nodes.filter(n=>vnodeClass(n,'mobile-asset-row')).length,0);assert.ok(desktop.html.includes({Memory:'memory-card',Secrets:'secret-card',Docs:'doc-card'}[name]));
  }
});

test('selection footer is available before the first selection, but empty batch mutations remain disabled',async()=>{
  for(const name of names){
    const p=page(name);p.run("setMobileMode('select')");
    const node=walk(baseParse(sfcs[`../views/${name}View.vue`].template.content).children).find(n=>hasClass(n,'admin-batch'));
    let view=await render(node.loc.source,p.scope);
    const buttons=()=>view.nodes.filter(n=>n.type===ElButton);
    assert.equal(buttons().length,6);assert.ok(buttons().slice(0,5).every(n=>n.props.disabled));assert.ok(!buttons()[5].props.disabled);
    p.run('setSelected(1,true)');view=await render(node.loc.source,p.scope);assert.ok(buttons().slice(0,5).every(n=>!n.props.disabled));
  }
});

test('memory cross-group drag and expanded menu retain exact item content and expanded state semantics',async()=>{
  const p=page('Memory');p.run("setMobileMode('sort'); const normal=groups.value.find(g=>g.name==='项目'); groups.value[0].items.push(normal.items.shift())");await p.run('finishDrag()');await Vue.nextTick();
  const reordered=p.calls.find(c=>c.method==='reorder').args[1];assert.equal(reordered[0].id,1);assert.equal(reordered[0].expanded,1);assert.equal(reordered[0].grp,'项目');
  await p.run('toggleExpanded(entries.value[1])');const update=p.calls.find(c=>c.method==='updateEntry');assert.equal(update.args[0],2);assert.equal(update.args[1].expanded,true);assert.equal(update.args[1].body,'正文二');
});

test('owned phone CSS is width-bounded; components compile and action surfaces remain scrollable',()=>{
  const layout=postcss.parse(read('../mobile-assets.css'));
  layout.walkRules(rule=>{assert.equal(rule.parent.type,'atrule');assert.equal(rule.parent.params,'(max-width: 760px)');});
  assert.match(read('../mobile-assets.css'),/\.mobile-assets-page \.admin-batch \{ order: 2;/);
  assert.match(row.styles[0].content,/min-height: 77px/);assert.match(sheet.styles[0].content,/\.el-drawer__body \{[^}]*overflow-y: auto/);
  for(const [path,sfc] of Object.entries(sfcs)){
    const script=compileScript(sfc,{id:path});assert.deepEqual(compileTemplate({source:sfc.template.content,filename:path,id:path,compilerOptions:{bindingMetadata:script.bindings}}).errors,[],path);
    for(const style of sfc.styles)assert.deepEqual(compileStyle({source:style.content,filename:path,id:path,scoped:style.scoped}).errors,[]);
  }
});
