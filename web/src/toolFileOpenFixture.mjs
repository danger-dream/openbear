// A finished turn with Read, Write and Bash tool rows and the files behind them, for
// toolFileOpen.dom.test.mjs and toolFileOpen.browser.test.mjs.
// handle({path, query}) answers HTTP: {status, json} or {status, headers, body} (bytes);
// socket(ws) answers the page's sockets; fileRequests records every file read.
import {readFileSync} from 'node:fs';
export const uuid='5b0c7e52-6f0e-4d7a-9a51-2f4b7f0b2c11';
const conversation={kind:'conversation',id:uuid,conversationUuid:uuid,title:'工具读写文件',folderId:'',parentId:'',path:'',origin:'user',lastInteractionAtMs:1000,activityVersion:1,activityReadVersion:1,activityUnread:false,activityState:'completed',running:false};
const picture=readFileSync(new URL('../public/icons/apple-touch-icon.png',import.meta.url));
// Longer than the chat's 8000-character highlight limit: the preview still highlights it.
export const script='#!/usr/bin/env python3\n"""Maintain benchmark-package/."""\n\n\ndef main() -> int:\n    return 0\n'+Array.from({length:400},(_,i)=>`\n\ndef step_${i}(value: int) -> int:\n    return value + ${i}\n`).join('');
const base=(opId,opType,displaySeq,source,payload)=>({conversationId:uuid,conversationUuid:uuid,internalChatId:-1,opId,opType,turnId:'turn-files',turnUuid:'turn-files',parentTurnId:'',runRootTurnId:'turn-files',displaySeq,createdAtMs:1000+displaySeq,updatedAtMs:1000+displaySeq,terminalAtMs:1000+displaySeq,revision:2,status:'completed',lifecycle:'terminal',internal:false,source,transcriptMessageIds:[],targetType:'run',targetId:'turn-files',taskUuid:'',runId:'turn-files',payload});
const tool=(id,name,args,displaySeq)=>({...base(`tool:${id}`,'tool',displaySeq,'tool',{rootToolName:name,toolCallId:id,toolName:name,name,status:'completed',durationMs:3,runId:'turn-files',taskUuid:'',previewArguments:JSON.stringify(args),resultState:'ok'}),detailAvailable:true,detailLoaded:false,detailRevision:2});
const operations=[
  base('run:turn-files','run',10,'user',{turnId:'turn-files',runId:'turn-files',source:'user',status:'completed',internal:false,transcriptMessageIds:[]}),
  base('msg:m1','user_message',20,'user',{role:'user',text:'看看这两个文件',attachments:[],queued:false,interruption:false,status:'',transcriptMessageIds:[]}),
  tool('call-read','Read',{path:'/tmp/fileview/shot.png'},30),
  tool('call-write','Write',{path:'/opt/src-space/openbear/scripts/benchmark_package.py',content:script},40),
  tool('call-bash','Bash',{command:'ls /tmp/fileview',description:'列出目录'},50),
  base('assistant:turn-files:1','assistant_message',60,'assistant',{text:'两个文件都看过了。',complete:true,footer:'',segmentIndex:1,runId:'turn-files',transcriptMessageIds:[]}),
];
const files={
  'tool:call-read':{fileName:'shot.png',mimeType:'image/png',path:'/tmp/fileview/shot.png',fileState:'',body:picture},
  'tool:call-write':{fileName:'benchmark_package.py',mimeType:'text/x-python',path:'/opt/src-space/openbear/scripts/benchmark_package.py',fileState:'changed',body:Buffer.from(script)},
};

export function createToolFileBackend() {
  const fileRequests=[];
  const status={items:[],activityItems:[],recentItems:[conversation],folderConversationCounts:{},folderRunningCounts:{},revision:1};
  function handle({path,query={}}) {
    const file=path.match(/\/operations\/([^/]+)\/file(\/content)?$/);
    if(file){
      const opId=decodeURIComponent(file[1]),meta=files[opId];
      fileRequests.push(`${opId}${file[2]||''}`);
      if(!meta)return {status:404,headers:{'content-type':'text/plain'},body:'file not found'};
      if(file[2])return {status:200,headers:{'content-type':meta.mimeType,'cache-control':'no-store'},body:meta.body};
      const contentUrl=`/api/conversations/${uuid}/operations/${encodeURIComponent(opId)}/file/content`;
      return {status:200,json:{ok:true,artifact:{artifactUuid:opId,conversationUuid:uuid,source:'tool-file',fileName:meta.fileName,mimeType:meta.mimeType,sizeBytes:meta.body.length,sha256:'digest',modifiedAt:Date.UTC(2026,9,30,14,56),path:meta.path,fileState:meta.fileState,contentUrl,downloadUrl:`${contentUrl}?download=1`}}};
    }
    let json={ok:true,items:[]};
    if(path==='/api/conversation-tree/bootstrap')json={ok:true,rootFolders:[],rootItems:[conversation],selected:{item:conversation,folderPath:[],folderItems:[]},initialExpandedPaths:[],locatedFolders:[],running:status,activeCount:1,archivedCount:0,temporaryCount:0};
    else if(path==='/api/conversation-tree/children')json={ok:true,items:query.parentId?[]:[conversation],hasMore:false,nextCursor:''};
    else if(path==='/api/conversation-tree/status')json={ok:true,...status};
    else if(path.includes('/conversation-tree/locate/'))json={ok:true,item:conversation,folderPath:[],folderItems:[]};
    else if(path===`/api/conversations/${uuid}/state`)json={ok:true,conversationUuid:uuid,conversation:{...conversation,status:'idle',running:false},running:false,operations,messages:[],usage:{},frameSeq:0,hasMoreBefore:false};
    else if(path==='/api/auth/session')json={ok:true,authenticated:true,mode:'controller'};
    else if(path==='/api/system/version')json={ok:true,version:'test'};
    return {status:200,json};
  }
  function socket(ws) {
    const url=typeof ws.url==='function'?ws.url():ws.url,send=text=>(ws.receive?ws.receive(text):ws.send(text));
    if(new URL(url).pathname==='/api/events/ws')send(JSON.stringify({type:'snapshot',epoch:'test',seq:1,items:[],includeArchived:false,treeStatus:status}));
    const onMessage=message=>{if(message==='ping')send('pong');};
    if(ws.onMessage)ws.onMessage(onMessage);else ws.onSend=onMessage;
  }
  return {handle,socket,fileRequests};
}
