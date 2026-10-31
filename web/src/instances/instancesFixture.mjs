// Fake HTTP endpoints of the instances page (and the App / settings / login paths it needs),
// shared by instances.dom.test.mjs (happy-dom) and instances.browser.test.mjs (Chromium).
// handle({path, method, body, query}) -> {status, json}; every request lands in `calls`.
export const fixture = () => ({id:'child-a',name:'实例甲',status:'ready',healthStatus:'healthy',serverId:'srv-1',port:18970,managementUrl:'http://192.0.2.10:18970',workspacePath:'/opt/openbear/instances/' + 'long-alpha-account-'.repeat(3),models:{mode:'copy',primary:'parent/model'},account:{username:'alpha'},gateway:{status:'synced',revision:1,appliedRevision:1},notifications:{loginSuccess:false,loginFailure:true,health:true},createdAtMs:10,updatedAtMs:20,lastSeenAtMs:Date.now()-60000,lastLoginSuccessAtMs:Date.now()-3600000,lastHealthAtMs:Date.now()-120000});

export function createInstancesBackend({mode='controller',loginStatus=401,verifyItemStatus='ready',verifyHealth,changeStatus=200,gatewayOn=true,instances=true,portUsed=false,syncFails=false,itemPatch={},observed={peer:'127.0.0.1',header:'X-Forwarded-For',atMs:Date.now()-120000}}={}) {
  const calls=[];
  let item = {...fixture(),...itemPatch}, operations = [{id:'op-1',kind:'deployment',status:'completed',conversationUuid:'conv-deploy',createdAtMs:1000}], failMaintain = false;
  let gateway = gatewayOn ? {ok:true,enabled:true,connected:true,status:'connected',publicUrl:'http://203.0.113.10:18962',serverId:'local',port:18962,clientIp:{source:'auto',trustedProxies:[]},observed} : {ok:true,enabled:false,connected:false,status:'disabled',publicUrl:'',serverId:'local',port:18962,clientIp:{source:'auto',trustedProxies:[]},observed:null};
  let servers = [{id:'local',name:'本机',local:true,publicIp:'203.0.113.10',status:'ok',usedBy:gatewayOn?['访问网关']:[]},{id:'srv-1',name:'云服务器',host:'192.0.2.10',port:22,username:'root',auth:'key',keyLabel:'deploy / 主密钥',publicIp:'198.51.100.7',docker:'27.3',status:'ok',usedBy:['实例甲']},
    {id:'srv-2',name:'洛杉矶节点机',host:'',port:0,username:'',auth:'none',usedBy:[],node:{online:true,exec:true,address:'192.0.2.7',publicIp:'198.51.100.20'}},
    {id:'srv-3',name:'柏林节点机',host:'',port:0,username:'',auth:'none',usedBy:[],node:{online:false,exec:true,address:'',publicIp:''}}];
  // The servers page list: regions, the host's own name and the saved order.
  const pageServers = [{id:'local',name:'Edge-SG',local:true,region:'SG'},{id:'srv-2',name:'洛杉矶节点机',region:'US'},{id:'srv-1',name:'云服务器',region:'JP'},{id:'srv-3',name:'柏林节点机',region:'DE'}];
  const keys = [{secret:'deploy',field:'主密钥',type:'ED25519',encrypted:true,usable:true,fingerprint:'SHA256:fixture',passphraseFields:['口令','备注'],suggestedPassphraseField:'口令'},{secret:'deploy',field:'备用',type:'RSA 4096',encrypted:false,usable:true,fingerprint:'SHA256:other',passphraseFields:[]},{secret:'site',field:'key',type:'RSA 2048',usable:true,likelyTls:true,passphraseFields:[]},{secret:'old',field:'ppk',usable:false,reason:'putty_format'}];
  function handle({path,method,body,query={}}) {
    calls.push({path,method,body});
    let data={ok:true,items:[]},status=200;
    if(path==='/api/auth/password/login'){status=loginStatus;data=status===200?{ok:true}:{ok:false,error:'DO-NOT-ECHO-password-fixture'};}
    else if(path==='/api/auth/password/change'){status=changeStatus;data=status===200?{ok:true}:{ok:false,error:'DO-NOT-ECHO-password-fixture'};}
    else if(path==='/api/instance-gateway') data=gateway;
    else if(path==='/api/instance-gateway/port') data={ok:true,free:portUsed&&query.port==='18962'?false:true,...(portUsed&&query.port==='18962'?{usedBy:'nginx'}:{})};
    else if(path==='/api/instance-gateway/suggest') data={ok:true,publicIp:query.serverId==='local'?'203.0.113.10':'198.51.100.7',port:Number(query.port)};
    else if(path==='/api/instance-gateway/enable'){gateway={...gateway,enabled:true,connected:true,status:'connected',...body,clientIp:{source:'auto',trustedProxies:[]}};data=gateway;}
    else if(path==='/api/instance-gateway/client-ip'){gateway={...gateway,clientIp:body.trustedProxies.length?{source:'header',trustedProxies:body.trustedProxies}:{source:'auto',trustedProxies:[]},observed:gateway.observed&&'trusted' in gateway.observed?{...gateway.observed,trusted:gateway.observed.peer==='127.0.0.1'||body.trustedProxies.includes(gateway.observed.peer)}:gateway.observed};data=gateway;}
    else if(path==='/api/instance-gateway/disable'){gateway={...gateway,enabled:false,connected:false,status:'disabled'};data=gateway;}
    else if(path==='/api/instance-gateway/rotate-keys') data={...gateway,rotatedInstances:1,pendingInstances:1};
    else if(path==='/api/instance-servers' && method==='GET') data={ok:true,items:servers};
    else if(path==='/api/servers' && method==='GET') data={ok:true,items:pageServers};
    else if(path==='/api/instance-servers/ssh-keys') data={ok:true,items:keys};
    else if(path==='/api/instance-servers/test') data={ok:true,os:'Debian GNU/Linux 13',docker:'',publicIp:'198.51.100.9'};
    else if(path==='/api/instance-servers' && method==='POST'){const created={id:'srv-new',name:body.name,host:body.host,port:body.port,username:body.username,auth:body.auth==='upload'?'key':body.auth,keyLabel:body.keyRef?`${body.keyRef.secret} / ${body.keyRef.field}`:'',status:'ok',usedBy:[]};servers=[...servers,created];data={ok:true,item:created};}
    else if(path==='/api/instance-model-options') data={ok:true,primary:'parent/model',providers:[{name:'parent',models:['model','cheap']},{name:'second',models:['big']}]};
    else if(path==='/api/instance-deployment-folder') data={ok:true,folderUuid:'deploy-folder'};
    else if(path==='/api/instances' && method==='GET') data={ok:true,items:instances?[item]:[]};
    else if(path==='/api/instances' && method==='POST'){item={...fixture(),id:'child-new',name:body.name,serverId:body.serverId,status:'deploying',account:{username:body.account.username},models:body.models};instances=true;data={ok:true,item,conversationUuid:'conv-new',deployment:{ok:true}};}
    else if(/^\/api\/instances\/child-[a-z]+$/.test(path) && method==='GET') data={ok:true,item};
    else if(/^\/api\/instances\/child-[a-z]+$/.test(path) && method==='PATCH'){if(body.notifications)item.notifications=body.notifications;if(body.account)item.gateway=syncFails?{status:'pending_sync',error:'gateway_unreachable'}:{status:'synced'};data={ok:true,item};}
    else if(path.endsWith('/gateway-sync')){item.gateway={status:'synced'};data={ok:true,synced:true,item};}
    else if(path.endsWith('/operations')) data={ok:true,items:operations};
    else if(path.endsWith('/events')) data={ok:true,items:[{id:'evt-1',type:'login_failure',atMs:Date.now()-60000,data:{ip:'198.51.100.23'}},{id:'evt-2',type:'login_success',atMs:Date.now()-3600000,data:{ip:'198.51.100.23'}}]};
    else if(path.endsWith('/maintenance')){
      if(failMaintain){status=409;data={ok:false,error:'operation_running'};}
      else {const op={id:'op-2',kind:'maintenance',status:'running',conversationUuid:'conv-maint',createdAtMs:Date.now()};operations=[op,...operations];data={ok:true,operation:op,conversationUuid:'conv-maint'};}
    }
    else if(path.endsWith('/verify')){item.status=verifyItemStatus;item.healthStatus=verifyHealth || (verifyItemStatus==='ready'?'healthy':'degraded');data={ok:true,item,verification:{ok:verifyItemStatus==='ready'&&!verifyHealth,identity:true,ready:true,persistence:true,publicRoute:true,health:{status:item.healthStatus,issues:[]}}};}
    else if(path.endsWith('/archive')){item.status='archived';data={ok:true,item};}
    else if(path==='/api/instances/child-a/rotate-keys'){item.rotation={status:'pending',attempts:1,error:'child_unreachable'};data={ok:true,rotated:false,error:'child_unreachable',item};}
    else if(path==='/api/instances/child-a/migrate-network'){item={...item,managementUrl:'http://127.0.0.1:18970',network:{bind:['127.0.0.1']},networkMigration:null};data={ok:true,migrated:true,synced:true,item};}
    else if(path==='/api/channels') data={ok:true,primaryModel:'parent/model',providers:[{name:'parent',enabled:true,models:[{id:'model'}]}]};
    else if(path.endsWith('/request')) {
      let child={ok:true};
      if(body.path.startsWith('/api/memory/entries')) child=body.method==='GET'?{ok:true,items:[]}:{ok:true,item:{id:1,...body.body}};
      if(body.path==='/api/channels' && body.method==='GET') child={ok:true,primaryModel:'remote/model',providers:[{name:'remote',baseUrl:'https://provider.example.test',protocol:'chat',enabled:true,models:[{id:'model',name:'Model',contextWindow:128000,maxTokens:8192}]}]};
      if(body.path==='/api/skills') child={ok:true,skillsDir:'/workspace/skills',items:[{name:'readme',description:'测试技能',status:'enabled',configuredEnabled:true}]};
      if(body.path==='/api/skills/readme') child={ok:true,item:{name:'readme',content:'# Skill documentation'}};
      if(body.path==='/api/mcp/status') child={ok:true,enabled:true,servers:[{key:'tools',name:'tools',transport:'stdio',status:'connected',enabled:true,approval:'ask',toolCount:1}],tools:[]};
      if(body.path==='/api/settings') child={ok:true,values:{'agent.maxTurns':30,'memory.accessKey':'SERVER-SECRET-NOT-TO-RENDER'}};
      if(body.path==='/api/settings/specs') child={ok:true,specs:{'agent.maxTurns':{path:'agent.maxTurns',title:'最大轮次',kind:'int',min:1},'memory.accessKey':{path:'memory.accessKey',title:'记忆访问密钥',kind:'string',sensitive:true}}};
      if(body.path==='/api/managed/v1/jobs' && body.method==='POST') child={ok:true,job:{id:'job-1',status:'running',output:'',exitCode:null}};
      if(body.path==='/api/managed/v1/jobs/job-1') child={ok:true,job:{id:'job-1',status:'completed',output:'Python 3.12',exitCode:0}};
      if(body.path.endsWith('/cancel')) child={ok:true,job:{id:'job-1',status:'cancelled',output:'',exitCode:null}};
      data={ok:true,status:body.path==='/api/managed/v1/jobs'?202:200,data:child};
    }
    // Existing App bootstrap read paths, without exercising unrelated managers.
    else if(path==='/api/settings') data={ok:true,providerCount:1,values:{'agent.maxTurns':30,'web.taskNotifications.enabled':true,'web.port':8899},masked:{},revision:1};
    else if(path==='/api/settings/specs') data={ok:true,domains:[{key:'agent',title:'Agent 设置',desc:'常用业务设置',sections:[{key:'agent',title:'执行限制',paths:['agent.maxTurns']}]},{key:'web',title:'Web 服务',desc:'监听设置',sections:[{key:'web',title:'监听设置',paths:['web.port']}]},{key:'notifications',title:'通知',desc:'本设备系统通知与 Telegram 提醒',sections:[{key:'web_notifications',title:'Telegram 通知',paths:['web.taskNotifications.enabled']}]}],specs:{'agent.maxTurns':{path:'agent.maxTurns',title:'最大轮次',kind:'int',group:'agent',effect:'立即生效',desc:'常用轮次限制',min:1},'web.taskNotifications.enabled':{path:'web.taskNotifications.enabled',title:'Telegram 长任务通知',kind:'bool',group:'web_notifications',effect:'立即生效',desc:'通过 Telegram 发送'},'web.port':{path:'web.port',title:'监听端口',kind:'int',group:'web',effect:'需要重启',desc:'服务端口'}}};
    else if(path==='/api/auth/session') data={ok:true,authenticated:true,mode};
    else if(path==='/api/system/version') data={ok:true,version:'test',updateAvailable:true,latest:{version:'next',requiresRestart:true}};
    else if(path==='/api/rath/options') data={ok:true,models:[],agents:[]};
    else if(path.includes('/conversation-tree/')) data={ok:true,items:[],folders:[],conversations:[],root:{id:'root',kind:'folder'},recent:[],revision:1};
    return {status,json:data};
  }
  return {calls,handle,failMaintain:()=>{failMaintain=true;}};
}
