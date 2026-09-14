import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';
import {parse, compileStyle} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {compile, createSSRApp, h, ref, nextTick} from 'vue';
import {renderToString} from 'vue/server-renderer';

const paths=['../../App.vue','ConsoleHeader.vue','ConsoleView.vue','ConsoleComposer.vue','TurnMinimap.vue','TaskMemoryDrawer.vue','MobileConversationTools.vue'];
const sources=Object.fromEntries(paths.map(path=>[path,fs.readFileSync(new URL(path,import.meta.url),'utf8')]));
const descriptors=Object.fromEntries(paths.map(path=>[path,parse(sources[path]).descriptor]));
const styles=Object.fromEntries(paths.map(path=>[path,postcss.parse(descriptors[path].styles.map(s=>s.content).join('\n'))]));
const phone={width:390,height:800,hover:'none',pointer:'coarse'},desktop={width:1440,height:900,hover:'hover',pointer:'fine'};
function matches(query,env){return query.split(',').some(part=>[...part.matchAll(/\(([^)]+)\)/g)].every(([,atom])=>{const[key,value]=atom.split(':').map(s=>s.trim());if(key==='max-width')return env.width<=parseFloat(value);if(key==='min-width')return env.width>=parseFloat(value);if(key==='max-height')return env.height<=parseFloat(value);return env[key]===value;}));}
// Parsed CSS boundaries and real component handlers, not browser screenshots.
function css(file,selector,env=phone){const out={};styles[file].walkRules(rule=>{if(!rule.selectors.includes(selector))return;for(let p=rule.parent;p;p=p.parent)if(p.type==='atrule'&&p.name==='media'&&!matches(p.params,env))return;rule.walkDecls(d=>{out[d.prop]=d.value;});});return out;}
const walk=nodes=>(nodes||[]).flatMap(n=>[n,...walk(Array.isArray(n.children)?n.children:[])]);
const nodes=file=>walk(baseParse(descriptors[file].template.content).children);
const hasClass=(n,name)=>n.props?.some(p=>p.name==='class'&&p.value?.content.split(/\s+/).includes(name));

test('phone header shows title/navigation/More, with no model, thinking, session ID or metrics subtitle',()=>{
  const app='../../App.vue',header='ConsoleHeader.vue';
  assert.equal(css(app,'.app-shell.is-console .mobile-app-bar').display,'none');
  assert.equal(css(app,'.app-shell.is-console .app-main')['padding-top'],'0');
  assert.equal(css(app,'.mobile-app-bar').display,'flex','other app views retain navigation');
  assert.equal(css(header,'.console-header').height,'calc(48px + env(safe-area-inset-top, 0px))');
  assert.equal(css(header,'.header-subtitle').display,'none');
  assert.equal(css(header,'.header-metrics').display,'none');
  assert.equal(css(header,'.header-mobile-actions').display,'flex');
  assert.equal(css(app,'.app-shell.is-console .mobile-sidebar-toggle').border,'0');
  const consoleNode=nodes(app).find(n=>n.tag==='ConsoleView');
  assert.match(consoleNode.loc.source,/#mobile-navigation/);assert.match(consoleNode.loc.source,/@click="sidebarOpen = true"/);
  assert.match(sources['ConsoleView.vue'],/<MobileConversationTools/);
  assert.match(sources[header],/<slot name="mobile-actions"\/>/);
});

test('phone gives all width back to transcript, with no permanent number strip or floating tools',()=>{
  for(const width of [320,360,390,430,760]){
    const env={...phone,width};
    assert.equal(css('ConsoleView.vue','.console-controls',env).display,'none');
    assert.equal(css('ConsoleView.vue','.conversation-column',env).display,'flex');
    assert.equal(css('ConsoleView.vue','.conversation-column',env)['grid-template-columns'],undefined);
    assert.equal(css('TurnMinimap.vue','.turn-minimap',env).display,'none');
  }
  const controls=nodes('ConsoleView.vue').find(n=>hasClass(n,'console-controls'));
  assert.equal(walk(controls.children).filter(n=>n.tag==='TaskMemoryDrawer').length,1,'reuse one mounted memory owner');
  for(const event of ['open-memory','toggle-work','toggle-scroll-lock','scroll-to-turn'])assert.ok(sources['ConsoleView.vue'].includes(`@${event}=`));
});

test('editor has primary vertical space and model is an unboxed secondary text entry',()=>{
  const f='ConsoleComposer.vue',link='.composer-toolbar button.run-config-chip';
  for(const width of [320,390,760]){
    const env={...phone,width};
    assert.equal(css(f,'.composer-toolbar',env)['flex-wrap'],'nowrap');
    assert.equal(css(f,link,env).border,'0');assert.equal(css(f,link,env).background,'transparent');
    assert.equal(css(f,link,env)['box-shadow'],'none');assert.equal(css(f,link,env).flex,'0 1 auto');
    assert.equal(css(f,link,env)['min-width'],'0','long model names shrink instead of pushing send off-screen');
    assert.equal(css(f,'.run-config-chip-model',env)['font-weight'],'500');
    assert.equal(css(f,':deep(.reference-editor-content)',env)['min-height'],'min(4.5rem, calc(var(--mobile-viewport-height, 100dvh) * .22))');
    assert.equal(css(f,':deep(.reference-editor-content)',env)['font-size'],undefined);
    assert.equal(css(f,'.composer-clear:disabled',env).display,'none');
    for(const s of ['.tool-btn','.send-button'])assert.equal(css(f,s,env).height,'44px');
  }
  // The theme leakage that produced the outlined model pill has lower specificity
  // than this mobile scoped selector, independently of bundle load order.
  const compiled=compileStyle({source:descriptors[f].styles[0].content,filename:f,id:'data-v-composer',scoped:true}).code;
  assert.match(compiled,/\.composer-toolbar button\.run-config-chip\[data-v-composer\]/);
  assert.ok(css(f,link+':focus-visible').outline,'keyboard focus remains visible');
});

test('desktop title/subtitle, toolbar, editor size and floating rail are unchanged',()=>{
  assert.deepEqual(css('../../App.vue','.app-shell.is-console .app-main',desktop),{});
  for(const s of ['.header-mobile-navigation','.header-mobile-actions','.header-mobile-running'])assert.equal(css('ConsoleHeader.vue',s,desktop).display,'none');
  assert.equal(css('ConsoleHeader.vue','.header-subtitle',desktop).display,undefined);
  assert.equal(css('ConsoleHeader.vue','.header-metrics',desktop).display,'flex');
  assert.equal(css('ConsoleHeader.vue','.console-header',desktop)['min-height'],'4rem');
  assert.equal(css('ConsoleView.vue','.console-controls',desktop).display,'contents');
  for(const [f,s] of [['ConsoleView.vue','.work-detail-toggle'],['ConsoleView.vue','.scroll-lock-toggle'],['TaskMemoryDrawer.vue','.task-memory-entry-wrap'],['TurnMinimap.vue','.turn-minimap']])assert.equal(css(f,s,desktop).position,'fixed');
  assert.equal(css('ConsoleComposer.vue','.composer-toolbar',desktop).display,'grid');
  assert.equal(css('ConsoleComposer.vue','.tool-btn',desktop).width,'2rem');
  assert.deepEqual(css('ConsoleComposer.vue','.composer-toolbar button.run-config-chip',desktop),{});
  assert.deepEqual(css('ConsoleComposer.vue',':deep(.reference-editor-content)',desktop),{});
});

// Exercise the production formatters without importing the unrelated .vue icons in display.js.
const displaySource=fs.readFileSync(new URL('./display.js',import.meta.url),'utf8');
const formatterContext=vm.createContext({});
for(const name of ['trimCompactNumber','fmtTokens','cachePct','tokenLine']){
  const source=displaySource.match(new RegExp(`(?:export )?function ${name}\\([^]*?^}`, 'm'))?.[0];
  assert.ok(source,`production formatter ${name}`);
  vm.runInContext(source.replace(/^export /,''),formatterContext);
}
const {fmtTokens,cachePct,tokenLine}=vm.runInContext('({fmtTokens,cachePct,tokenLine})',formatterContext);

function mobileRuntime(){
  const calls=[],mounted=[],unmounted=[],watchers=[];
  const props={conversationUuid:'A',turns:[{id:'1',user:{content:'first'}},{id:'2',user:{content:'second'}}],activeTurnIndex:1,canOpenWork:true,workOpen:false,autoScrollLocked:true,tokenParts:{input:1250000,output:8300,cache:1000000},durationText:'12m 34s',costText:'$3.4567'};
  const media={matches:true,addEventListener(){},removeEventListener(){}};
  const context=vm.createContext({ref,nextTick,fmtTokens,cachePct,defineProps:()=>props,defineEmits:()=>((...args)=>calls.push(args)),onMounted:fn=>mounted.push(fn),onBeforeUnmount:fn=>unmounted.push(fn),watch:(getter,fn)=>watchers.push(fn),window:{matchMedia:()=>media},shortText:(s,max)=>s.slice(0,max)});
  vm.runInContext(descriptors['MobileConversationTools.vue'].scriptSetup.content.replace(/^import .*;\n/gm,''),context);
  mounted.forEach(fn=>fn());
  return{context,props,media,calls,watchers,unmounted};
}
async function renderTools(runtime){
  const bindings=vm.runInContext('({props,fmtTokens,cachePct,phone:phone.value,menuOpen:menuOpen.value,navigationOpen:navigationOpen.value,navigationList:null,chooseAction,openNavigation,chooseTurn,revealCurrentTurn,turnLabel})',runtime.context);
  const slots=[],compiled=compile(descriptors['MobileConversationTools.vue'].template.content);let tree;
  const app=createSSRApp({render(){tree=compiled.call(this,bindings,[]);return tree;}});
  app.component('ElPopover',{inheritAttrs:false,props:['visible'],render(){const children=[...(this.$slots.reference?.()||[]),...(this.visible?(this.$slots.default?.()||[]):[])];slots.push(...children);return children;}});
  app.component('ElDrawer',{inheritAttrs:false,props:['modelValue'],emits:['opened'],render(){const children=this.modelValue?(this.$slots.default?.()||[]):[];slots.push(...children);return children;}});
  for(const name of ['ChatLineRound','CollectionTag','DataAnalysis','Lock','Money','MoreFilled','Timer','Unlock','WorkDetailIcon'])app.component(name,{render:()=>h('svg')});
  const html=await renderToString(app);return{html,nodes:walk([tree,...slots])};
}

test('mobile summary uses the same desktop total parts and formatters, with no new fetch or timer',()=>{
  const all=nodes('ConsoleView.vue'),header=all.find(n=>n.tag==='ConsoleHeader'),tools=all.find(n=>n.tag==='MobileConversationTools');
  const binding=(node,name)=>node.props.find(p=>p.name==='bind'&&p.arg?.content===name)?.exp?.content;
  assert.equal(binding(header,'tokens-text'),'totalTokensDisplay');
  assert.equal(binding(tools,'token-parts'),'totalTokenParts');
  assert.match(sources['ConsoleView.vue'],/const totalTokensDisplay = computed\(\(\) => tokenLine\(totalTokenParts.value\)\)/);
  assert.match(descriptors['MobileConversationTools.vue'].scriptSetup.content,/import \{cachePct, fmtTokens, shortText\} from "\.\/display.js"/);
  for(const [name,expression] of [['duration-text','totalDurationDisplay'],['cost-text','totalCostDisplay']]){
    assert.equal(binding(header,name),expression);assert.equal(binding(tools,name),expression);
  }
  assert.doesNotMatch(descriptors['MobileConversationTools.vue'].scriptSetup.content,/\bApi\b|fetch\(|setInterval\(/);
  assert.equal(css('MobileConversationTools.vue','.mobile-summary-metrics dd')['font-variant-numeric'],'tabular-nums');
  assert.equal(css('MobileConversationTools.vue','.mobile-summary-metrics dd')['overflow-wrap'],'anywhere');
  assert.equal(css('MobileConversationTools.vue','.mobile-conversation-menu.el-popper')['overflow-y'],'auto');
  assert.match(css('MobileConversationTools.vue','.mobile-conversation-menu.el-popper')['max-height'],/mobile-viewport-height/);
});

test('More shows read-only live totals on demand and retains every action',async()=>{
  const r=mobileRuntime();let view=await renderTools(r);
  assert.doesNotMatch(view.html,/会话统计|总 Tokens|1.25M/);
  vm.runInContext('menuOpen.value=true',r.context);view=await renderTools(r);
  for(const text of ['会话统计','总 Tokens','总耗时','总花费','1.25M','12m 34s','$3.4567'])assert.ok(view.html.includes(text),text);
  const summary=view.nodes.find(n=>n.type==='section'&&n.props?.class==='mobile-conversation-summary');
  assert.equal(walk([summary]).filter(n=>n.type==='button').length,0);
  assert.equal(view.nodes.filter(n=>n.type==='button').length,5);
  Object.assign(r.props,{tokenParts:{input:2500000,output:16600,cache:2000000},durationText:'25m 8s',costText:'$6.9134'});
  view=await renderTools(r);for(const text of ['2.5M','16.6K','2M（80.0%）','25m 8s','$6.9134'])assert.ok(view.html.includes(text));
  assert.doesNotMatch(view.html,/1.25M|12m 34s/);assert.deepEqual(r.calls,[]);
});

test('screenshot-sized token totals use three full-width rows, keeping cache count and percentage together',async()=>{
  const r=mobileRuntime();vm.runInContext('menuOpen.value=true',r.context);
  const cases=[
    [{input:618170000,output:2640000,cache:593500000},['618.17M','2.64M','593.5M（96.0%）']],
    [{input:1000000,output:1000,cache:1000000},['1M','1K','1M（100.0%）']],
    [{},['0','0','0（—）']],
  ];
  for(const [parts,expected] of cases){
    r.props.tokenParts=parts;
    const view=await renderTools(r);
    const rows=view.nodes.filter(n=>n.type==='div'&&n.props?.class==='mobile-token-row');
    assert.equal(rows.length,3);
    assert.deepEqual(rows.map(n=>n.children[0].children),['输入','输出','缓存']);
    assert.deepEqual(rows.map(n=>n.children[1].children),expected);
    for(const value of expected)assert.ok(tokenLine(parts).includes(value),'desktop uses the same formatting');
  }
  const f='MobileConversationTools.vue';
  for(const width of [320,390,760]){
    const env={...phone,width};
    assert.equal(css(f,'.mobile-summary-metrics > .mobile-summary-token-group',env)['grid-template-columns'],'minmax(0, 1fr)','token details occupy full card width, not the narrow value column');
    assert.equal(css(f,'.mobile-token-lines',env).display,'grid');
    assert.equal(css(f,'.mobile-token-row',env).display,'grid');
    assert.equal(css(f,'.mobile-token-value',env)['white-space'],'nowrap');
    assert.equal(css(f,'.mobile-token-label',env)['text-align'],'left');
  }
  assert.deepEqual(r.calls,[]);
});

test('running indicator is a mobile-only green dot plus text tag, and is absent when idle',async()=>{
  const compiled=compile(descriptors['ConsoleHeader.vue'].template.content);
  for(const running of [true,false]){
    const props={title:'测试会话',running,status:'就绪',runStartedAt:0,tokensText:'10',durationText:'1s',costText:'$0.0010'};
    let tree;const app=createSSRApp({render(){tree=compiled.call(this,{props,$slots:{},fmtElapsedFromStart:()=> '0s'},[]);return tree;}});
    for(const name of ['BearLogo','DataAnalysis','Money','Stopwatch','Timer'])app.component(name,{render:()=>h('svg')});
    app.directive('elapsed',{getSSRProps:()=>({})});
    await renderToString(app);
    const tag=walk([tree]).find(n=>n.type==='span'&&n.props?.class==='header-mobile-running');
    assert.equal(Boolean(tag),running);
    if(tag){assert.equal(tag.props.role,'status');assert.equal(tag.props['aria-label'],'运行中');
      assert.equal(tag.children[0].type,'i');assert.equal(tag.children[0].props['aria-hidden'],'true');
      assert.equal(tag.children[1].children,'运行中');}
  }
  for(const width of [320,390,760]){
    const env={...phone,width},tag=css('ConsoleHeader.vue','.header-mobile-running',env);
    assert.equal(tag.display,'inline-flex');assert.equal(tag['white-space'],'nowrap');assert.equal(tag.flex,'none');
    assert.equal(tag.color,'#047857');assert.equal(tag.height,'22px');
  }
  assert.equal(css('ConsoleHeader.vue','.header-mobile-running',desktop).display,'none');
  assert.equal(css('ConsoleHeader.vue','.header-mobile-running i')['background'],'currentColor');
  assert.equal(css('ConsoleHeader.vue','html.dark .header-mobile-running').color,'#6ee7a2');
});

test('More retains memory/work/follow actions and exposes titled turn navigation only on demand',async()=>{
  const r=mobileRuntime();
  let view=await renderTools(r);assert.match(view.html,/更多会话操作/);assert.doesNotMatch(view.html,/first|second/);
  vm.runInContext('menuOpen.value=true',r.context);view=await renderTools(r);
  const buttons=view.nodes.filter(n=>n.type==='button');
  assert.equal(buttons.length,5);
  for(const event of ['open-memory','toggle-work','toggle-scroll-lock']){
    await vm.runInContext(`chooseAction('${event}')`,r.context);
    assert.equal(vm.runInContext('menuOpen.value',r.context),false);
  }
  assert.deepEqual(r.calls,[['open-memory'],['toggle-work'],['toggle-scroll-lock']]);
  vm.runInContext('openNavigation()',r.context);view=await renderTools(r);
  const rows=view.nodes.filter(n=>n.type==='button'&&n.props.class==='mobile-turn-row');
  assert.equal(rows.length,2);assert.equal(rows[1].props['aria-current'],'true');assert.match(view.html,/first/);assert.match(view.html,/second/);
  rows[0].props.onClick();assert.deepEqual(r.calls.at(-1),['scroll-to-turn',0]);assert.equal(vm.runInContext('navigationOpen.value',r.context),false);
});

test('new drafts disable memory, unavailable work disables details, and route/desktop changes close mobile panels',async()=>{
  const r=mobileRuntime();r.props.conversationUuid='local:draft';r.props.canOpenWork=false;r.props.turns=[];
  vm.runInContext('menuOpen.value=true',r.context);const view=await renderTools(r);
  const buttons=view.nodes.filter(n=>n.type==='button');
  assert.equal(buttons[1].props.disabled,true);assert.equal(buttons[2].props.disabled,true);assert.equal(buttons[3].props.disabled,true);
  vm.runInContext('navigationOpen.value=true',r.context);r.watchers.forEach(fn=>fn());assert.equal(vm.runInContext('navigationOpen.value',r.context),false);
  vm.runInContext('menuOpen.value=true;navigationOpen.value=true',r.context);r.media.matches=false;vm.runInContext('updateMedia()',r.context);
  assert.equal(vm.runInContext('phone.value||menuOpen.value||navigationOpen.value',r.context),false);
  assert.doesNotMatch((await renderTools(r)).html,/<button|mobile-conversation-tools|mobile-turn-list/);
  r.unmounted.forEach(fn=>fn());
});
