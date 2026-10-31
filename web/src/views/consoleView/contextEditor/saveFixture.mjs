// The context editor's endpoints for one conversation, shared by save.dom.test.mjs and
// save.browser.test.mjs. Every write is recorded in writes; conflict() makes the next save 409.
function snapshot() {
  return {ok:true,baseline:{format:'openbear-context-edit/1',revision:1,origin:{protocol:'chat',modelLabel:'test/model'},runtime:{frozen:true},system:'System context',tools:[],entries:[{entryId:'u',message:{role:'user',content:'Keep this question'}},{entryId:'a',message:{role:'assistant',content:'Remove this answer'}}]},snapshotToken:'initial',targetModel:'test/model',models:[{model:'test/model',label:'Test',protocol:'chat'}],draft:null};
}
export function createContextSaveBackend() {
  let data=snapshot(),conflicting=false;
  const writes=[];
  function handle({path,method,body}) {
    if(!path.startsWith('/api/conversations/original/context-editor'))return {status:200,json:{ok:true,items:[]}};
    let json={ok:true},status=200;
    if(method==='GET')json=data;
    else if(path.endsWith('/preview'))json={ok:true,payload:{model:'model',messages:body.working.entries.map(e=>e.message)},document:body.working,issues:[{code:'reasoning_cleanup',severity:'warning',entryId:'',text:'框架已自动处理 25 组历史思考、签名和密文；正文、工具调用及返回保留'}]};
    else if(path.endsWith('/branch')){writes.push({method:'branch',body});json={ok:true,conversation:{conversationUuid:'copy'}};}
    else if(method==='PUT'&&!path.endsWith('/draft')){
      writes.push({method:'original',body});
      if(conflicting){status=409;json={ok:false,error:'context_source_changed'};}
      else{data={...data,baseline:body.working,snapshotToken:'saved',draft:{baseline:body.working,working:body.working,snapshotToken:'saved',mode:'compatible',targetModel:'test/model',revision:1}};json={...data,conversation:{conversationUuid:'original'}};}
    }
    return {status,json};
  }
  return {handle,writes,conflict(){conflicting=true;}};
}
