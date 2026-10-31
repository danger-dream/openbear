import * as promptPolicy from './promptPolicy.js';
import { userStorage } from '../sessionBoundary.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {computed, nextTick, reactive, ref, watch, effectScope, proxyRefs, compile, createSSRApp, h} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse, compileTemplate} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import * as icons from '@element-plus/icons-vue';
import postcss from 'postcss';
import {useRecentConversationRows} from './conversationRecentRows.js';
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from './conversationTreeInteractions.js';
import {activityLabel, activityState, activityReadRequests, isActivityWaiting, pendingConversationRows, pendingFolderCounts, folderActivityCounts} from '../conversationActivity.js';
import {REFERENCE_MIME, referenceToken} from '../references/codec.js';
import * as conversationGroupsTree from './conversationGroupsTree.js';
import * as conversationTreeSelection from './conversationTreeSelection.js';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const {descriptor} = parse(source);
const walk = nodes => (nodes || []).flatMap(n => n && typeof n === 'object' ? [n, ...walk(Array.isArray(n.children) ? n.children : [])] : []);
const templateNodes = walk(baseParse(descriptor.template.content).children);
const classNode = name => templateNodes.find(n => n.props?.some(p => p.name === 'class' && p.value?.content === name));
const navigationTemplate = '<section>' + ['sidebar-activity','sidebar-tabs','sidebar-panel'].map(name => classNode(name).loc.source).join('\n') + '</section>';
const classes = (n, name) => String(n.props?.class || '').split(/\s+/).includes(name);
const folder = id => ({kind:'folder', id, folderId:id, name:id, path:`目录 ${id}`, conversationCount:1});
const conversation = (id, extra={}) => ({kind:'conversation',id,conversationUuid:id,folderId:'F',title:id,path:'目录 F',lastInteractionAtMs:1000,activityVersion:1,activityReadVersion:1,activityState:'completed',...extra});
const rows = view => view.nodes.filter(n=>classes(n,'tree-row-wrap'));
const ids = view => rows(view).map(n=>n.props['data-tree-id']);
const selected = view => rows(view).filter(n=>walk([n]).some(child=>classes(child,'is-chat-active'))).map(n=>n.props['data-tree-id']);
const settle = async () => {await nextTick();await nextTick();};
function harness(t, {phone=true, storedView='recent', blockedStorage=false}={}) {
  const scope=effectScope(), mounted=[], unmounted=[], intervals=new Map(), events=[], reads=[], requests=[], scrolled=[], stored=new Map([['openbear:conversation-sidebar-view',storedView]]);
  const props=reactive({activeConversationUuid:'c',draftConversation:null});
  const catalog=reactive({connected:true,ready:true,activityReadVersions:new Map()});
  let s;
  const api={
    readConversationActivity:async values=>{reads.push(values);return {};},
    conversationTreeFolders:async()=>({items:[folder('F')]}),
    conversationTreeBootstrap:async ({conversationUuid}={})=>{
      requests.push(['bootstrap',conversationUuid]);
      const item=conversation(conversationUuid||props.activeConversationUuid);
      return {rootFolders:[folder('F')],activeCount:1,selected:{item,folderPath:['F'],folderItems:[folder('F')]},running:{items:[],recentItems:[item]}};
    },
    conversationTreeChildren:async options=>{requests.push(['children',options]);return {items:options.parentId==='F'?[conversation('c')]:[],hasMore:false};},
    locateConversationFolderInTree:async id=>{requests.push(['folder',id]);return {folderPath:[id],folderItems:[folder(id)]};},
    locateConversationInTree:async id=>{requests.push(['locate',id]);return {item:conversation(id),folderPath:['F'],folderItems:[folder('F')]};},
  };
  const ctx=vm.createContext({...conversationGroupsTree,...conversationTreeSelection,userStorage,...promptPolicy,...icons,computed,nextTick,reactive,ref,watch,useRecentConversationRows,rowId,treeItemParent,compareTreeItems,resolveTreeDrop,activityLabel,activityState,activityReadRequests,isActivityWaiting,pendingConversationRows,pendingFolderCounts, folderActivityCounts,
    props,referenceCatalog:catalog,referenceItem:()=>null,acceptActivityReadReceipt(){},REFERENCE_MIME,referenceToken,
    defineLazyView:()=>({}),defineProps:()=>props,defineEmits:()=>(...args)=>events.push(args),defineExpose(){},
    onMounted:fn=>mounted.push(fn),onBeforeUnmount:fn=>unmounted.push(fn),Api:api,apiError:String,ElMessage:{error:value=>assert.fail(String(value))},
    window:{matchMedia:()=>({matches:phone}),localStorage:{getItem:key=>{if(blockedStorage)throw Error('blocked');return stored.get(key);},setItem:(key,value)=>{if(blockedStorage)throw Error('blocked');stored.set(key,value);}},addEventListener(){},removeEventListener(){},setInterval:fn=>{intervals.set(1,fn);return 1;},clearInterval:id=>intervals.delete(id)},
    document:{querySelector:selector=>selector.startsWith('[data-tree-id=')?{scrollIntoView:()=>scrolled.push(selector)}:null},CSS:{escape:s=>s},setTimeout:()=>1,clearTimeout(){},
  });
  scope.run(()=>vm.runInContext(descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm,'')+`
    globalThis.s={selecting,isSelected,selectableRow,toggleSelected,clickConversation,sidebarView,isDirectoryView,sidebarScroll,sidebarScrollKey,switchSidebarView,sidebarTabKeydown,showRecentActivity,revealInFolders,revealDraft,locatingConversation,
      activeConversationRow,recentClock,recentConversationRows,recentUnreadRows,recentUnreadCount,recentWaitingCount,recentRunningCount,recentLabel,displayRows,visibleRows,query,initialized,loading,
      activityItems,recentItems,pendingRows,folderWaitingCount,collapsedFolderActivity,isActivityWaiting,activityReadBusy,titleGenerating,referenceCatalog,rootFolders,stateFor,expanded,selectedFolderId,selectedTargetLabel,emit,menu,listRef,runMenuAction,
      activateRow,toggleRow,dragStart,dropIntent,dragOver,drop,clearDrag,rootDropTarget,drag,moveInFlight,rowId,rowLabel,rowLoading,nodePath,indentation,isExpanded,running,isTitleGenerating,liveConversationTitle,conversationOrigin,
      loadChildren,searchHasMore,searchRows,searchLoading,runSearch,locateAndOpen,openMenu,openRootMenu,openMoreMenu,rowKeydown,moreMenuKeydown,clearDropTarget,closeOverview,enterOverview,leaveOverview,markActivityRead,openActivityConversation};
    initialized.value=true; selectedFolderId.value='chosen';`,ctx));
  t.after(()=>{unmounted.forEach(fn=>fn());scope.stop();});
  s=ctx.s;s.rootFolders.value=[folder('F')];s.stateFor('F').items=[conversation('c')];s.stateFor('F').loaded=true;s.expanded.value=new Set(['F']);s.recentItems.value=[conversation('c')];
  async function render() {
    let vnode;
    const bindings=proxyRefs({...icons,...s,activeConversationUuid:props.activeConversationUuid,activityLabel});
    const draw=compile(navigationTemplate);
    const app=createSSRApp({render(){vnode=draw.call(this,bindings,[]);return vnode;}});
    for(const [name,icon] of Object.entries(icons))app.component(name,icon);
    app.component('AnimatedConversationTitle',{props:['text'],render(){return h('span',this.text);}});
    app.component('el-icon',{render(){return h('i',this.$slots.default?.());}});
    const html=await renderToString(app);return {html,nodes:walk([vnode])};
  }
  return {s,ctx,api,props,catalog,events,reads,requests,scrolled,stored,intervals,mounted,unmounted,render,run:code=>vm.runInContext(code,ctx)};
}

async function renderCreationMenu(x) {
  const items=[], folders=[];
  const bindings=proxyRefs({...x.s,promptFolder:id=>folders.push(id)});
  const draw=compile(templateNodes.find(n=>n.tag==='el-dropdown-menu').loc.source);
  const app=createSSRApp({render(){return draw.call(this,bindings,[]);}});
  app.component('el-dropdown-menu',{render(){return h('ul',this.$slots.default?.());}});
  app.component('el-dropdown-item',{props:{divided:Boolean},render(){
    const children=this.$slots.default?.()||[];
    const node=h('li',this.$attrs,children);
    items.push({node,divided:this.divided,label:walk(children).filter(n=>typeof n.children==='string').map(n=>n.children).join('').trim()});
    return node;
  }});
  app.component('el-icon',{render(){return h('i',this.$slots.default?.());}});
  for(const name of ['ChatLineRound','FolderAdd'])app.component(name,icons[name]);
  await renderToString(app);return {items,folders};
}

for(const folderId of ['F',''])test(`toolbar creation menu separates temporary chat from ${folderId ? 'selected-folder actions' : 'root-folder creation'} without duplicates`,async t=>{
  const x=harness(t);x.s.selectedFolderId.value=folderId;
  const {items,folders}=await renderCreationMenu(x);
  assert.deepEqual(items.map(i=>i.label),folderId?['新建会话 · 目录 F','新建目录 · 目录 F','新建临时会话']:['新建根级目录','新建临时会话']);
  assert.deepEqual(items.map(i=>i.divided),folderId?[false,false,true]:[false,true]);
  await items.at(-1).node.props.onClick();
  assert.deepEqual(x.events.at(-1),['new-conversation','']);
  assert.equal(x.s.selectedFolderId.value,folderId,'creation uses the existing parent handler rather than changing selection ahead of draft confirmation');
  if(folderId){await items[0].node.props.onClick();assert.deepEqual(x.events.at(-1),['new-conversation','F']);}
  await items.at(-2).node.props.onClick();assert.deepEqual(folders,[folderId]);
});

for(const phone of [true,false])test(`${phone?'phone':'desktop'} recent group conversations stay flat even when their directory branch is expanded`,async t=>{
  const x=harness(t,{phone});
  const item=conversation('c',{groupCount:1,expandId:'lead:c'});
  x.s.recentItems.value=[item];x.s.stateFor('F').items=[item];x.s.expanded.value=new Set(['F','lead:c']);
  let view=await x.render();assert.deepEqual(ids(view),['recent:c']);
  assert.equal(rows(view)[0].props['aria-expanded'],undefined);assert.ok(!view.nodes.some(n=>classes(n,'tree-chevron')));
  const recent=x.s.displayRows.value[0];await x.s.toggleRow(recent);
  for(const key of ['ArrowLeft','ArrowRight'])x.s.rowKeydown({key,preventDefault(){assert.fail('recent arrows do not control a branch');}},recent);
  assert.deepEqual([...x.s.expanded.value],['F','lead:c']);assert.equal(x.requests.length,0);
  x.s.rowKeydown({key:'Enter',preventDefault(){}},recent);assert.equal(x.events.at(-1)[0],'open');
});

for(const phone of [true,false])test(`${phone?'phone':'desktop'} shows a flat recent list or the real tree, never duplicate entries`,async t=>{
  const x=harness(t,{phone});let view=await x.render();
  assert.deepEqual(ids(view),['recent:c']);assert.deepEqual(selected(view),['recent:c']);
  assert.equal(view.nodes.find(n=>classes(n,'tree-list')).props.role,'list');
  assert.equal(rows(view)[0].props.role,'listitem');assert.equal(rows(view)[0].props.draggable,true);
  assert.match(view.html,/目录 F/);assert.doesNotMatch(view.html,/__recent|activity-folder/);
  await view.nodes.find(n=>n.props?.['data-sidebar-view']==='folders').props.onClick();
  view=await x.render();assert.deepEqual(ids(view),['F','c','__temporary','__archive']);assert.deepEqual(selected(view),['c']);
  assert.equal(view.nodes.find(n=>classes(n,'tree-list')).props.role,'tree');assert.equal(rows(view)[1].props['aria-level'],2);
  assert.equal(rows(view)[0].props.draggable,true);assert.ok(!x.s.displayRows.value.some(r=>r.recentAlias));
  assert.equal(x.events.length,0);assert.equal(x.requests.length,0);assert.equal(x.props.activeConversationUuid,'c');assert.equal(x.s.selectedFolderId.value,'chosen');
  assert.equal(view.nodes.filter(n=>classes(n,'tree-list')).length,1);
  assert.equal(view.nodes.filter(n=>n.props?.role==='tab'&&n.props['aria-selected']).length,1);
});

test('switching views preserves each scroll offset, folder expansion and conversation without refetching',async t=>{
  const x=harness(t);x.s.listRef.value={scrollTop:120};
  await x.s.switchSidebarView('folders');await settle();assert.equal(x.s.listRef.value.scrollTop,0);
  x.s.listRef.value.scrollTop=360;x.s.expanded.value=new Set();
  await x.s.switchSidebarView('recent');await settle();assert.equal(x.s.listRef.value.scrollTop,120);
  await x.s.switchSidebarView('folders');await settle();assert.equal(x.s.listRef.value.scrollTop,360);assert.equal(x.s.expanded.value.size,0);
  assert.equal(x.events.length,0);assert.equal(x.requests.length,0);assert.equal(x.props.activeConversationUuid,'c');assert.equal(x.s.selectedFolderId.value,'chosen');
  assert.equal(x.stored.get('openbear:conversation-sidebar-view'),'folders');
});

test('last view is restored, while invalid or unavailable browser storage safely uses recent',async t=>{
  assert.equal(harness(t,{storedView:'folders'}).s.sidebarView.value,'folders');
  assert.equal(harness(t,{storedView:'invalid'}).s.sidebarView.value,'recent');
  const x=harness(t,{blockedStorage:true});assert.equal(x.s.sidebarView.value,'recent');await x.s.switchSidebarView('folders');assert.equal(x.s.sidebarView.value,'folders');
});

test('tab keyboard navigation retains its DOM target across asynchronous rendering and does not open a chat',async t=>{
  const x=harness(t),view=await x.render(),focused=[];
  const event={key:'ArrowRight',preventDefault(){},currentTarget:{querySelector:selector=>({focus:()=>focused.push(selector)})}};
  const job=view.nodes.find(n=>n.props?.role==='tablist').props.onKeydown(event);event.currentTarget=null;await job;
  assert.equal(x.s.sidebarView.value,'folders');assert.deepEqual(focused,['[data-sidebar-view="folders"]']);assert.equal(x.events.length,0);
});

for(const phone of [true,false])test(`${phone?'phone':'desktop'} recent activation preserves folder target, while directory activation keeps normal selection`,async t=>{
  const x=harness(t,{phone});
  await (await x.render()).nodes.find(n=>classes(n,'conversation')).props.onClick();
  const opened=x.events.find(e=>e[0]==='open')[1];assert.equal(opened.conversationUuid,'c');assert.equal(opened.folderId,'F');assert.equal('recentAlias' in opened,false);
  assert.equal(x.s.selectedFolderId.value,'chosen');assert.equal(x.s.sidebarView.value,'recent');assert.equal(x.requests.length,0);
  await x.s.switchSidebarView('folders');await (await x.render()).nodes.find(n=>classes(n,'conversation')).props.onClick();
  assert.equal(x.s.selectedFolderId.value,'F');assert.equal(x.s.stateFor('F').items[0].recentAlias,undefined);
  x.s.recentItems.value=[conversation('c',{lastInteractionAtMs:2000})];x.s.stateFor('F').items=[conversation('c',{lastInteractionAtMs:2000})];
  assert.deepEqual(selected(await x.render()),['c']);await x.s.switchSidebarView('recent');assert.deepEqual(selected(await x.render()),['recent:c']);
});

for(const phone of [true,false])test(`${phone?'phone':'desktop'} recent drag inserts an editor reference but cannot reorder or move a conversation`,async t=>{
  const x=harness(t,{phone}),view=await x.render(),row=rows(view)[0],data=new Map();
  const transfer={setData:(key,value)=>data.set(key,value),getData:key=>data.get(key)||'',get types(){return [...data.keys()];}};
  let prevented=0;
  row.props.onDragstart({dataTransfer:transfer,preventDefault(){prevented++;}});
  const reference={kind:'chat',id:'c',label:'c',scope:'full'};
  assert.equal(row.props.draggable,true);assert.equal(prevented,0);
  assert.equal(transfer.effectAllowed,'copy');assert.equal(x.s.drag.value.row,null);
  assert.deepEqual(JSON.parse(data.get(REFERENCE_MIME)),reference);
  assert.equal(data.get('text/plain'),referenceToken(reference));assert.equal(data.has('application/x-openbear-tree'),false);

  // Feed the actual exported payload through the real editor's receiving handlers.
  const editorSource=fs.readFileSync(new URL('../references/ReferenceEditor.vue',import.meta.url),'utf8'),inserted=[];
  const receiver=vm.createContext({...conversationGroupsTree,REFERENCE_MIME,dragHover:ref(false),lastSelection:null,
    editor:ref({view:{posAtCoords:()=>({pos:6})}}),insertReference:(value,range)=>inserted.push(JSON.parse(JSON.stringify({value,range}))),
    event:{dataTransfer:transfer,clientX:30,clientY:40,preventDefault(){prevented++;},stopPropagation(){}}});
  vm.runInContext(editorSource.slice(editorSource.indexOf('function dragOver('),editorSource.indexOf('function externalInsert(')),receiver);
  vm.runInContext('dragOver(event);drop(event)',receiver);
  assert.equal(transfer.dropEffect,'copy');assert.equal(receiver.dragHover.value,false);
  assert.deepEqual(inserted,[{value:reference,range:{from:6,to:6}}]);

  const event={dataTransfer:transfer,preventDefault(){},stopPropagation(){},clientY:50,currentTarget:{getBoundingClientRect:()=>({top:0,height:100})}};
  await x.s.drop(event,x.s.displayRows.value[0]);
  assert.equal(x.requests.length,0);assert.equal(x.events.length,0);
  row.props.onDragstart({dataTransfer:transfer,preventDefault(){assert.fail('reference drag unexpectedly blocked');}});
  await x.s.switchSidebarView('folders');
  assert.equal(x.s.dropIntent(event,folder('F')),null);
  await x.s.drop(event,folder('F'));await x.s.drop(event,x.s.rootDropTarget);
  assert.equal(x.requests.length,0);assert.equal(x.s.stateFor('F').items[0].folderId,'F');
  assert.deepEqual(x.s.recentItems.value.map(item=>item.conversationUuid),['c']);
});

test('recent context menu keeps original identity and directory drop remains available only in directory view',async t=>{
  const x=harness(t),alias=x.s.displayRows.value[0];
  const event={preventDefault(){},stopPropagation(){},currentTarget:{getBoundingClientRect:()=>({right:200,bottom:100})}};
  await x.s.openMoreMenu(event,alias);assert.equal(x.s.menu.value.row.conversationUuid,'c');assert.equal('recentAlias' in x.s.menu.value.row,false);
  const other=conversation('other'),destination=folder('destination');
  for(const ratio of [0,.5,1]){assert.equal(resolveTreeDrop(other,alias,ratio,[]),null);assert.equal(resolveTreeDrop(alias,destination,ratio,[]),null);}
  x.s.drag.value.row=other;assert.equal(x.s.dropIntent({},destination),null);
  await x.s.switchSidebarView('folders');x.s.drag.value.row=other;
  assert.equal(x.s.dropIntent({currentTarget:{getBoundingClientRect:()=>({top:0,height:100})},clientY:50},destination).targetFolderId,'destination');
});

for(const phone of [true,false])test(`${phone?'phone':'desktop'} status packets still render fifteen recent conversations without the redundant footer`,async t=>{
  const x=harness(t,{phone});
  x.ctx.statusPacket={items:[],recentItems:Array.from({length:16},(_,i)=>conversation(`recent${i}`,{lastInteractionAtMs:2000-i}))};
  x.run('applyStatus(statusPacket)');await settle();
  assert.equal(x.s.recentItems.value.length,15);
  const view=await x.render();
  assert.deepEqual(ids(view),Array.from({length:15},(_,i)=>`recent:recent${i}`));
  assert.ok(!templateNodes.some(n=>n.tag==='footer'));
});

test('outstanding work, counts and read-all remain accessible in both views',async t=>{
  const x=harness(t);
  x.s.recentItems.value=Array.from({length:7},(_,i)=>conversation(`recent${i}`,{lastInteractionAtMs:1000+i}));
  x.s.activityItems.value=[conversation('wait',{running:true,activityPending:[{action:'confirm'}]}),conversation('unread',{activityUnread:true,activityReadVersion:0}),conversation('running',{running:true,activityState:'running'})];
  x.s.pendingRows.value=pendingConversationRows({activityItems:x.s.activityItems.value});
  await settle();assert.equal(x.s.recentConversationRows.value.length,10); // All seven recents plus three outstanding conversations.
  let view=await x.render();assert.match(view.html,/待确认/);assert.match(view.html,/完成待查看/);
  await x.s.switchSidebarView('folders');view=await x.render();assert.match(view.html,/待处理 1/);assert.match(view.html,/未读 1/);assert.match(view.html,/运行 1/);
  await view.nodes.find(n=>classes(n,'recent-read-all')).props.onClick();assert.equal(x.reads.length,1);assert.equal(x.reads[0].length,1);assert.equal(x.reads[0][0].conversationUuid,'unread');
  x.s.query.value='needle';await settle();
  await (await x.render()).nodes.find(n=>classes(n,'sidebar-unread-count')).props.onClick();
  assert.equal(x.s.sidebarView.value,'recent');assert.equal(x.s.query.value,'');
  await x.s.switchSidebarView('folders');x.s.query.value='needle';await settle();
  await (await x.render()).nodes.find(n=>classes(n,'sidebar-running-count')).props.onClick();
  assert.equal(x.s.sidebarView.value,'recent');assert.equal(x.s.query.value,'');assert.equal(x.s.recentLabel(conversation('fail',{activityState:'failed'})),'失败');
});

for(const phone of [true,false]) for(const mode of ['recent','folders']) test(`${phone?'phone':'desktop'} ${mode} keeps all counts visible and shows read-all only with unread conversations`,async t=>{
  const x=harness(t,{phone});await x.s.switchSidebarView(mode);
  const states=[
    [[],[0,0,0]],
    [[conversation('job',{running:true,activityState:'running'})],[0,0,1]],
    [[conversation('job',{running:true,activityState:'running',activityUnread:true,activityReadVersion:0})],[0,0,1]],
    [[conversation('job',{running:true,activityPending:[{action:'confirm'}]})],[1,0,0]],
    [[conversation('job',{running:true,activityState:'waiting',activityPending:[{action:'confirm'}],activityUnread:true,activityReadVersion:0})],[1,0,0]],
    [[conversation('job',{activityUnread:true,activityReadVersion:0})],[0,1,0]],
    [[],[0,0,0]],
  ];
  for(const [items,counts] of states){
    x.s.activityItems.value=items;x.s.pendingRows.value=pendingConversationRows({activityItems:items});await settle();const view=await x.render();
    const activity=view.nodes.find(n=>classes(n,'sidebar-activity'));assert.ok(activity,'status row stays mounted even when idle');
    assert.deepEqual(walk([activity]).filter(n=>classes(n,'activity-cell')).map(n=>['recent-waiting-count','sidebar-unread-cell','sidebar-running-count'].find(name=>classes(n,name))),['recent-waiting-count','sidebar-unread-cell','sidebar-running-count'],'three cells in a fixed order; none is set apart');
    const readAllHost=walk([activity]).find(n=>classes(n,'sidebar-unread-cell'));
    assert.equal(walk([readAllHost]).some(n=>classes(n,'recent-read-all')),Boolean(counts[1]),'read-all lives inside the unread cell');
    assert.deepEqual([x.s.recentWaitingCount.value,x.s.recentUnreadCount.value,x.s.recentRunningCount.value],counts);
    assert.match(view.html,new RegExp(`待处理 ${counts[0]}`));assert.match(view.html,new RegExp(`未读 ${counts[1]}`));assert.match(view.html,new RegExp(`运行 ${counts[2]}`));
    const readAll=view.nodes.find(n=>classes(n,'recent-read-all'));
    assert.equal(Boolean(readAll),Boolean(counts[1]),'no invisible button may reserve width when there are no unread conversations');
    if(readAll){
      assert.equal(Boolean(readAll.props.disabled),false);
      x.s.activityReadBusy.value=true;
      assert.equal((await x.render()).nodes.find(n=>classes(n,'recent-read-all')).props.disabled,true);
      x.s.activityReadBusy.value=false;
    }
    assert.equal(x.s.sidebarView.value,mode);assert.equal(x.props.activeConversationUuid,'c');
  }
});

for(const phone of [true,false]) for(const mode of ['recent','folders']) test(`${phone?'phone':'desktop'} ${mode} read-all only acknowledges counted unread rows and retains in-progress results`,async t=>{
  const x=harness(t,{phone});await x.s.switchSidebarView(mode);
  x.s.titleGenerating.value=new Set(['naming']);
  let items=[
    conversation('done',{activityVersion:4,activityReadVersion:3}),
    conversation('working',{running:true,activityState:'running',activityVersion:20,activityReadVersion:18}),
    conversation('waiting',{running:true,activityState:'waiting',activityPending:[{action:'confirm'}],activityReadVersion:0}),
    conversation('naming',{activityReadVersion:0}),
  ];
  const apply=async()=>{
    x.ctx.statusPacket={items:items.filter(row=>row.running),activityItems:items,recentItems:items};
    x.run('applyStatus(statusPacket)');await settle();
  };
  const counts=()=>[x.s.recentWaitingCount.value,x.s.recentUnreadCount.value,x.s.recentRunningCount.value];
  const clickReadAll=async()=>{await (await x.render()).nodes.find(n=>classes(n,'recent-read-all')).props.onClick();};
  const requests=()=>JSON.parse(JSON.stringify(x.reads.at(-1)));
  await apply();
  assert.deepEqual(counts(),[1,1,1]);
  await clickReadAll();assert.deepEqual(requests(),[{conversationUuid:'done',version:4}]);
  for(const id of ['working','waiting','naming']){
    assert.equal(x.s.recentConversationRows.value.find(row=>row.conversationUuid===id).activityUnread,true,'hiding a marker must not consume the result');
  }
  // A server read receipt clears only the completed conversation.
  items=items.map(row=>row.conversationUuid==='done'?{...row,activityReadVersion:4}:row);
  await apply();assert.deepEqual(counts(),[1,0,1]);
  assert.ok(!(await x.render()).nodes.some(n=>classes(n,'recent-read-all')));
  // When the parent run finishes, its new completion becomes visible as unread.
  items=items.map(row=>row.conversationUuid==='working'?{...row,running:false,activityState:'completed',activityVersion:21}:row);
  await apply();assert.deepEqual(counts(),[1,1,0]);
  await clickReadAll();assert.deepEqual(requests(),[{conversationUuid:'working',version:21}]);
  items=items.map(row=>row.conversationUuid==='working'?{...row,activityReadVersion:21}:row);
  await apply();assert.deepEqual(counts(),[1,0,0]);
  assert.ok(!(await x.render()).nodes.some(n=>classes(n,'recent-read-all')));
});

test('status geometry keeps breakpoint heights and splits the row into three equal cells',()=>{
  const css=postcss.parse(descriptor.styles[0].content),heights=[];
  css.walkRules('.sidebar-activity',r=>r.walkDecls('height',d=>heights.push([r.parent.type==='root'?'base':r.parent.params,d.value])));
  assert.deepEqual(heights,[['base','27px'],['(max-width:760px), (hover:none) and (pointer:coarse)','44px']]);
  const decls=selector=>{const out={};css.walkRules(r=>{if(r.parent.type==='root'&&r.selector.split(',').map(s=>s.trim()).includes(selector))r.walkDecls(d=>{out[d.prop]=d.value;});});return out;};
  assert.equal(decls('.sidebar-activity').display,'grid');
  assert.equal(decls('.sidebar-activity')['grid-template-columns'],'repeat(3, minmax(0, 1fr))');
  const cell=decls('.activity-cell');
  assert.equal(cell['justify-content'],'center');assert.equal(cell['white-space'],'nowrap');assert.equal(cell['font-variant-numeric'],'tabular-nums');
  assert.ok(Object.keys(decls('.activity-cell:hover')).includes('background'),'every cell has the same hover surface');
});

test('global search has its own scroll state; clearing restores the chosen view without changing the chat',async t=>{
  const x=harness(t);x.s.listRef.value={scrollTop:70};
  x.s.query.value='needle';x.s.searchRows.value=[conversation('c'),conversation('result')];await settle();
  assert.equal(x.s.listRef.value.scrollTop,0);assert.deepEqual(ids(await x.render()),['c','result']);assert.deepEqual(selected(await x.render()),['c']);
  x.s.listRef.value.scrollTop=25;await x.s.switchSidebarView('folders');await settle();assert.equal(x.s.listRef.value.scrollTop,25);
  x.s.query.value='';await settle();assert.equal(x.s.listRef.value.scrollTop,0);assert.deepEqual(ids(await x.render()),['F','c','__temporary','__archive']);
  await x.s.switchSidebarView('recent');await settle();assert.equal(x.s.listRef.value.scrollTop,70);assert.equal(x.props.activeConversationUuid,'c');
  assert.equal(x.events.length,0);assert.equal(x.requests.length,0);
});

test('search result activation opens the result while remaining in the chosen navigation view',async t=>{
  const x=harness(t);x.s.query.value='other';x.s.searchRows.value=[conversation('other')];await settle();
  await (await x.render()).nodes.find(n=>classes(n,'conversation')).props.onClick();
  assert.equal(x.events.find(e=>e[0]==='open')[1].conversationUuid,'other');assert.equal(x.s.sidebarView.value,'recent');
  x.props.activeConversationUuid='other';assert.deepEqual(selected(await x.render()),['other']);
  x.s.query.value='';x.s.recentItems.value=[conversation('other')];assert.deepEqual(selected(await x.render()),['recent:other']);
  x.props.activeConversationUuid='not-loaded';assert.deepEqual(selected(await x.render()),[]);
});

test('recent menu locate still reveals its conversation in folders without opening a different one',async t=>{
  const x=harness(t),row=x.s.displayRows.value[0];x.s.expanded.value=new Set();x.s.query.value='needle';await settle();
  await x.s.openMenu({preventDefault(){},stopPropagation(){}},row);
  await x.s.runMenuAction('locate');
  assert.equal(x.s.sidebarView.value,'folders');assert.equal(x.s.query.value,'');assert.equal(x.s.expanded.value.has('F'),true);
  assert.deepEqual(x.requests.filter(r=>r[0]==='bootstrap'),[['bootstrap','c']]);assert.equal(x.events.some(e=>e[0]==='open'),false);assert.equal(x.props.activeConversationUuid,'c');
  assert.ok(x.scrolled.includes('[data-tree-id="c"]'));assert.deepEqual(selected(await x.render()),['c']);assert.equal(x.s.locatingConversation.value,false);
});

test('a new local draft is revealed in the real directory rather than disappearing from recents',async t=>{
  const x=harness(t);x.props.draftConversation=conversation('local:new',{local:true});x.props.activeConversationUuid='local:new';await settle();
  await x.s.revealDraft('F');assert.equal(x.s.sidebarView.value,'folders');assert.equal(x.s.selectedFolderId.value,'F');
  assert.ok(ids(await x.render()).includes('local:new'));assert.deepEqual(selected(await x.render()),['local:new']);assert.ok(x.scrolled.includes('[data-tree-id="local:new"]'));
});

test('folder search result opens its directory view, not a fictitious recent branch',async t=>{
  const x=harness(t);x.s.query.value='F';x.s.searchRows.value=[folder('F')];await settle();
  await (await x.render()).nodes.find(n=>classes(n,'tree-node-main')).props.onClick();
  assert.equal(x.s.sidebarView.value,'folders');assert.equal(x.s.query.value,'');assert.equal(x.s.selectedFolderId.value,'F');assert.equal(x.props.activeConversationUuid,'c');assert.equal(x.events.some(e=>e[0]==='open'),false);
});

test('empty recent view, shared clock and lifecycle cleanup work without breakpoint listeners',async t=>{
  const x=harness(t);x.s.recentItems.value=[];assert.match((await x.render()).html,/暂无最近会话/);
  x.run('refreshTree=async()=>{};scheduleStatus=()=>{}');await Promise.all(x.mounted.map(fn=>fn()));assert.equal(x.intervals.size,1);
  x.s.recentClock.value=0;x.intervals.get(1)();assert.ok(x.s.recentClock.value>0);
  x.unmounted.forEach(fn=>fn());assert.equal(x.intervals.size,0);
});

test('compiled navigation keeps one scroller, fixed controls, shared theme tokens and touch targets',()=>{
  assert.deepEqual(compileTemplate({source:descriptor.template.content,filename:'ConversationTree.vue',id:'sidebar-views'}).errors,[]);
  assert.equal(templateNodes.filter(n=>n.props?.some(p=>p.name==='ref'&&p.value?.content==='listRef')).length,1);
  assert.ok(!templateNodes.some(n=>n.tag==='ConversationActivityFolder'));
  const css=postcss.parse(descriptor.styles[0].content);let overflow,minHeight,flex;
  css.walkRules('.tree-list',r=>r.walkDecls('overflow',d=>overflow=d.value));assert.equal(overflow,'auto');
  css.walkRules('.sidebar-panel',r=>{r.walkDecls('min-height',d=>minHeight=d.value);r.walkDecls('flex',d=>flex=d.value);});assert.equal(minHeight,'0');assert.equal(flex,'1');
  const touch=[];css.walkAtRules('media',r=>{if(r.params.includes('max-width:760px'))r.walkRules(rule=>{if(rule.selector.includes('.sidebar-tabs button'))rule.walkDecls('min-height',d=>touch.push(d.value));});});assert.ok(touch.includes('44px'));
  assert.match(descriptor.styles[0].content,/\.sidebar-tabs button\[aria-selected="true"\].*var\(--ob-surface-raised\)/);
});

test('collapsed directories summarize running and unread descendants; expanding, local reads and moves update them',async t=>{
  const x=harness(t,{storedView:'folders'});
  const busy=conversation('busy',{running:true,activityState:'running',folderPath:['F']});
  x.ctx.statusPacket={items:[busy],activityItems:[busy,
    conversation('done',{folderId:'deep',folderPath:['F','deep'],activityVersion:2,activityReadVersion:1}),
    conversation('ask',{running:true,activityPending:[{action:'confirm'}],folderPath:['F']})]};
  x.run('applyStatus(statusPacket)');await settle();
  const row=x.s.rootFolders.value[0];
  assert.equal(x.s.collapsedFolderActivity(row),null); // Expanded: its own rows carry the state.
  x.s.expanded.value=new Set();await settle();
  assert.deepEqual({...x.s.collapsedFolderActivity(row)},{running:1,unread:1}); // The waiting one stays only in "待处理".
  const view=await x.render();
  assert.match(view.html,/待处理 1/);assert.match(view.html,/1 个会话运行中/);assert.match(view.html,/1 个会话有未读结果/);
  x.catalog.activityReadVersions=new Map([['done',2]]);await settle();
  assert.deepEqual({...x.s.collapsedFolderActivity(row)},{running:1,unread:0});
  x.catalog.activityReadVersions=new Map();await settle();
  x.run('rebaseStatusFolderPaths({kind:"folder",folderId:"deep"},["G","deep"])');
  assert.deepEqual(JSON.parse(JSON.stringify(x.run('folderActivity.value'))),{F:{running:1,unread:0},G:{running:0,unread:1},deep:{running:0,unread:1}});
});
