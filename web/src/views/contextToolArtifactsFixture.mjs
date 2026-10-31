// Settings endpoints with the compaction section, for contextToolArtifacts.dom.test.mjs and
// contextToolArtifacts.browser.test.mjs. newBackend: the server knows the tool-result setting.
export const TOOL_RESULT_PATH = 'contextManagement.toolResultArtifactTokens';
export function createSettingsBackend({newBackend = true} = {}) {
  const path = TOOL_RESULT_PATH, calls = [];
  const specs={'agent.compactRatio':{path:'agent.compactRatio',title:'压缩触发比例',desc:'模型未设置独立触发阈值时，按模型窗口的这个比例触发当前会话选择的压缩策略。',kind:'float',group:'compaction',effect:'下一轮生效',min:.1,max:.95}};
  const values={'agent.compactRatio':.7};
  if(newBackend){specs[path]={path,title:'工具结果落盘阈值',desc:'仅实际压缩时，把超过阈值的普通工具结果完整保存为文件，上下文保留路径和头尾预览，可用 Read 分段读取全文；包括最新未读结果。滑动窗口和模型摘要共用。默认 16000 token，0 关闭；用户要求、确认决策和 Agent 编排结论不落盘，未授权 Read 时保持原文。',kind:'int',group:'compaction',effect:'下一轮生效',min:0,unit:'token'};values[path]=16000;}
  function handle({path: url, method, body}) {
    let json={ok:true},status=200;
    if(url==='/api/settings/specs')json={ok:true,domains:[{key:'agent',title:'Agent',sections:[{key:'compaction',title:'上下文压缩',paths:Object.keys(specs)}]}],specs};
    else if(url==='/api/settings')json={ok:true,values:{...values}};
    else if(url.startsWith('/api/settings/')&&method==='PATCH'){
      const key=decodeURIComponent(url.slice('/api/settings/'.length));calls.push({path:key,...body});
      if(!specs[key]){status=400;json={ok:false,error:'未知设置项'};}else{values[key]=Number(body.value);json={ok:true,value:values[key]};}
    }else if(url==='/api/rath/options')json={ok:true,models:[]};
    return {status,json};
  }
  return {handle, calls};
}
