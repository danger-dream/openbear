import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {compile,createSSRApp,h,ref} from 'vue';
import {renderToString} from 'vue/server-renderer';
const app=fs.readFileSync(new URL('../App.vue',import.meta.url),'utf8');
const component=fs.readFileSync(new URL('./MobileSidebarResources.vue',import.meta.url),'utf8');
const descriptor=parse(component).descriptor;
const appCSS=postcss.parse(parse(app).descriptor.styles.map(s=>s.content).join('\n'));
const gridCSS=postcss.parse(fs.readFileSync(new URL('./sidebarResources.css',import.meta.url),'utf8'));
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[])]);
const appNodes=walk(baseParse(parse(app).descriptor.template.content).children);
const hasClass=(n,name)=>n.props?.some(p=>p.name==='class'&&p.value?.content.split(/\s+/).includes(name));
function phoneRules(selector){const out={};appCSS.walkRules(rule=>{if(rule.parent.name==='media'&&rule.parent.params==='(max-width: 760px)'&&rule.selectors.includes(selector))rule.walkDecls(d=>{out[d.prop]=d.value;});});return out;}

test('mobile sidebar devotes the flex remainder to the same conversation tree, rather than six management rows',()=>{
  assert.equal(phoneRules('.app-sidebar .sidebar-desktop-nav').display,'none');
  assert.equal(phoneRules('.app-sidebar .sidebar-caption').display,'none');
  for(const selector of ['.app-sidebar .sidebar-heading','.app-sidebar .sidebar-new-session'])assert.equal(phoneRules(selector).flex,'0 0 44px');
  const treeArea=appNodes.find(n=>hasClass(n,'sidebar-conversations'));
  const classes=treeArea.props.find(p=>p.name==='class').value.content;
  assert.match(classes,/min-h-0 flex-1 flex-col/);
  assert.equal(walk(treeArea.children).filter(n=>n.tag==='ConversationTree').length,1);
  assert.ok(treeArea.loc.end.offset<appNodes.find(n=>n.tag==='MobileSidebarResources').loc.start.offset,'resource launcher is below, not above, the tree');
  assert.equal(phoneRules('.app-sidebar .sidebar-conversations')['margin-top'],'8px');
});

test('top new-conversation button is hidden on desktop but retains its mobile touch target and action',()=>{
  const base={};
  appCSS.walkRules(rule=>{if(rule.parent.type==='root'&&rule.selectors.includes('.sidebar-new-session'))rule.walkDecls(d=>{base[d.prop]=d.value;});});
  assert.equal(base.display,'none','desktop button must occupy no space and leave the tab order');
  const phone=phoneRules('.app-sidebar .sidebar-new-session');
  assert.equal(phone.display,'flex');
  assert.equal(phone.height,'44px');
  assert.equal(phone.flex,'0 0 44px');
  const buttons=appNodes.filter(n=>hasClass(n,'sidebar-new-session'));
  assert.equal(buttons.length,1,'keep one responsive button rather than duplicate actions');
  assert.match(buttons[0].loc.source,/@click="startConsoleNewSession"/);
  assert.match(buttons[0].loc.source,/<span>新会话<\/span>/);
});

test('desktop navigation/source order, new-conversation ownership, theme and version actions remain intact',()=>{
  assert.match(app,/<nav class="sidebar-desktop-nav sidebar-resource-grid text-sm" aria-label="资源与设置">/);
  assert.match(app,/@click="closeReferenceShelf\(\); selectNav\(n.key\)"/);
  assert.match(app,/@pointerenter="showReferenceShelf\(\$event,n.key\)"/);
  const newButton=appNodes.find(n=>hasClass(n,'sidebar-new-session'));
  assert.match(newButton.loc.source,/@click="startConsoleNewSession"/);
  assert.match(app,/@command="chooseThemeMode"/);assert.match(app,/@click="openVersionDialog"/);
  assert.match(app,/<MobileSidebarResources :items="nav" :active="active" :sidebar-open="sidebarOpen" @select="closeReferenceShelf\(\); selectNav\(\$event\)"\/>/);
  for(const selector of ['.app-sidebar .sidebar-desktop-nav','.app-sidebar .sidebar-heading','.app-sidebar .sidebar-new-session']){
    appCSS.walkRules(rule=>{if(rule.selectors.includes(selector))assert.equal(rule.parent.params,'(max-width: 760px)','new overrides cannot affect desktop');});
  }
});

function baseGridRules(selector) {
  const out={};
  gridCSS.walkRules(rule=>{if(rule.parent.type==='root'&&rule.selectors.includes(selector))rule.walkDecls(d=>{out[d.prop]=d.value;});});
  return out;
}

test('both resource surfaces share three columns, quiet vertical tiles, inherited type and theme-aware states',()=>{
  const grid=baseGridRules('.sidebar-resource-grid');
  assert.equal(grid.display,'grid');
  assert.equal(grid['grid-template-columns'],'repeat(3, minmax(0, 1fr))');
  assert.equal(grid.flex,'0 0 auto');
  const tile=baseGridRules('.sidebar-resource-grid > .sidebar-resource-tile');
  assert.equal(tile['flex-direction'],'column');
  assert.equal(tile['align-items'],'center');
  assert.equal(tile['min-height'],'60px');
  assert.equal(tile.font,'inherit');
  assert.equal(tile.background,'transparent');
  assert.equal(tile.border,'0');
  assert.equal(tile['box-shadow'],undefined);
  assert.equal(baseGridRules(".sidebar-resource-grid > .sidebar-resource-tile[aria-current='page']").background,'var(--el-fill-color-darker)');
  assert.equal(baseGridRules('.sidebar-resource-grid > .sidebar-resource-tile:focus-visible').outline,'2px solid var(--el-color-primary)');
  assert.match(component,/sidebar-resources-grid sidebar-resource-grid/);
  assert.match(component,/font-size: 13px/);
  assert.match(app,/sidebar-desktop-nav sidebar-resource-grid text-sm/);
  assert.match(app,/import "\.\/components\/sidebarResources.css"/);
  assert.match(component,/import "\.\/sidebarResources.css"/);
  let reduced=false;
  gridCSS.walkAtRules('media',rule=>{if(rule.params==='(prefers-reduced-motion: reduce)')reduced=rule.toString().includes('transition: none');});
  assert.equal(reduced,true);
});

test('desktop six-grid renders native buttons in original order, marks the active page and preserves all reference entry gestures',async()=>{
  const r=runtime(),calls=[];
  assert.deepEqual(Array.from(r.props.items,item=>item.key),['memory','secrets','docs','skills','mcp','settings']);
  const markup=appNodes.find(n=>hasClass(n,'sidebar-desktop-nav')).loc.source;
  const scope={nav:r.props.items,active:'docs',closeReferenceShelf:()=>calls.push(['close']),selectNav:key=>calls.push(['select',key]),
    showReferenceShelf:(event,key)=>calls.push(['hover',key,event]),leaveReferenceShelf:()=>calls.push(['leave']),referenceNavKey:(event,key)=>calls.push(['key',key,event])};
  const compiled=compile(markup);let tree;
  const ssr=createSSRApp({render(){tree=compiled.call(this,scope,[]);return tree;}});
  ssr.component('ElIcon',{inheritAttrs:false,render:()=>h('i')});
  ssr.config.warnHandler=message=>assert.fail(message);
  const html=await renderToString(ssr);
  const buttons=walk([tree]).filter(n=>n.type==='button');
  assert.equal(buttons.length,5);
  const desktopItems=r.props.items.filter(item=>item.key!=='settings');
  for(const [index,item] of desktopItems.entries()){
    const button=buttons[index];
    assert.equal(button.props.type,'button');
    assert.equal(button.props.class,'sidebar-resource-tile');
    assert.equal(button.props['aria-current'],item.key==='docs'?'page':undefined);
    assert.ok(html.includes(item.shortLabel||item.label));
    button.props.onClick();assert.deepEqual(calls.slice(-2),[['close'],['select',item.key]]);
    const event={key:'ArrowRight',currentTarget:{}};
    button.props.onPointerenter(event);assert.deepEqual(calls.at(-1),['hover',item.key,event]);
    button.props.onPointerleave();assert.deepEqual(calls.at(-1),['leave']);
    button.props.onKeydown(event);assert.deepEqual(calls.at(-1),['key',item.key,event]);
  }
  const footerButton=appNodes.find(n=>hasClass(n,'sidebar-footer-settings'));
  assert.ok(footerButton,'footer contains dedicated desktop settings action');
  assert.match(footerButton.loc.source,/@click="closeReferenceShelf\(\); selectNav\('settings'\)"/);
  r.unmount.forEach(fn=>fn());
});

test('reference shelf anchors outside the whole launcher, retaining desktop hover delay and keyboard-only touch access',()=>{
  const shelf={value:{open:false}},active={value:'console'},timers=[];
  let hover=true;
  const grid={},target={closest:selector=>{assert.equal(selector,'.sidebar-desktop-nav');return grid;}};
  const context=vm.createContext({referenceShelf:shelf,active,referenceShelfTimer:null,window:{
    matchMedia:()=>({matches:hover}),clearTimeout(){},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},
  }});
  const source=app.slice(app.indexOf('function showReferenceShelf('),app.indexOf('function insertShelfReference('));
  vm.runInContext(source,context);
  for(const [index,[key,kind]] of [['memory','mem'],['secrets','secret'],['docs','doc']].entries()){
    context.showReferenceShelf({currentTarget:target},key);
    assert.equal(timers.at(-1).delay,index===0?220:500);timers.at(-1).fn();
    assert.equal(shelf.value.kind,kind);assert.equal(shelf.value.anchor,grid);
  }
  const count=timers.length;
  context.showReferenceShelf({currentTarget:target},'settings');
  active.value='docs';context.showReferenceShelf({currentTarget:target},'memory');active.value='console';
  hover=false;context.showReferenceShelf({currentTarget:target},'memory');
  assert.equal(timers.length,count,'unsupported destinations, inactive chat and touch hover stay closed');
  let prevented=false;
  context.referenceNavKey({currentTarget:target,key:'ArrowRight',preventDefault:()=>{prevented=true;}},'memory');
  assert.equal(prevented,true);assert.equal(timers.at(-1).delay,0);timers.at(-1).fn();
  assert.equal(shelf.value.anchor,grid);
});

function runtime(){
  const items=vm.runInNewContext(app.slice(app.indexOf('const nav = ['),app.indexOf('const pageToPath =')).replace('const nav =','result ='),{MemoryView:null,SecretsView:null,DocsView:null,SkillsView:null,McpView:null,SettingsHubView:null});
  const props={items,active:'docs',sidebarOpen:true},calls=[],mount=[],unmount=[],watchers=[];
  const media={matches:true,addEventListener(){},removeEventListener(){}};
  const context=vm.createContext({defineProps:()=>props,defineEmits:()=>((...args)=>calls.push(args)),ref,onMounted:fn=>mount.push(fn),onBeforeUnmount:fn=>unmount.push(fn),watch:(getter,fn)=>watchers.push(fn),window:{matchMedia:()=>media}});
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm,''),context);mount.forEach(fn=>fn());
  return{props,calls,context,media,watchers,unmount};
}
async function render(r){
  const binding=vm.runInContext('({props,phone:phone.value,menuOpen:menuOpen.value,selectPage})',r.context),slots=[];
  const compiled=compile(descriptor.template.content);let tree;
  const app=createSSRApp({render(){tree=compiled.call(this,binding,[]);return tree;}});
  app.component('ElPopover',{inheritAttrs:false,props:['visible'],render(){const nodes=[...(this.$slots.reference?.()||[]),...(this.visible?(this.$slots.default?.()||[]):[])];slots.push(...nodes);return nodes;}});
  app.component('ElIcon',{inheritAttrs:false,render:()=>h('i')});
  for(const name of ['Grid','ArrowUp'])app.component(name,{render:()=>h('svg')});
  const html=await renderToString(app);return{html,nodes:walk([tree,...slots])};
}
test('collapsed resource footer is one entry; expanded grid keeps all six destinations, active page and click routing',async()=>{
  const r=runtime();let view=await render(r);
  assert.match(view.html,/资源与设置/);assert.doesNotMatch(view.html,/记忆管理|凭证库|MCP 管理/);
  vm.runInContext('menuOpen.value=true',r.context);view=await render(r);
  const buttons=view.nodes.filter(n=>n.type==='button');assert.equal(buttons.length,7);
  for(const [index,item] of r.props.items.entries()){
    assert.ok(view.html.includes(item.label));
    assert.equal(buttons[index+1].props['aria-current'],item.key==='docs'?'page':undefined);
    buttons[index+1].props.onClick();assert.deepEqual(r.calls.at(-1),['select',item.key]);
    assert.equal(vm.runInContext('menuOpen.value',r.context),false);
  }
});
test('closing sidebar or changing page/viewport dismisses the resource popup, without desktop markup',async()=>{
  const r=runtime();vm.runInContext('menuOpen.value=true',r.context);r.props.sidebarOpen=false;r.watchers.forEach(fn=>fn());
  assert.equal(vm.runInContext('menuOpen.value',r.context),false);
  vm.runInContext('menuOpen.value=true',r.context);r.media.matches=false;vm.runInContext('updateMedia()',r.context);
  assert.equal(vm.runInContext('phone.value||menuOpen.value',r.context),false);
  assert.doesNotMatch((await render(r)).html,/<footer|资源与设置/);r.unmount.forEach(fn=>fn());
});
