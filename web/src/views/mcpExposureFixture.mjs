// MCP status with one server of 170 tools over the exposure budget, for mcpExposure.dom.test.mjs
// and mcpExposure.browser.test.mjs. legacy: an older backend without tool selection.
// handle({path, method, body}) answers /api; writes and calls record what the page sent.
function status(legacy = false) {
  return {ok:true,enabled:true,settingsAvailable:true,agentAccessAvailable:false,
    ...(!legacy ? {toolSelectionAvailable:true,thresholds:{toolCount:30,definitionBytes:65536}} : {}),
    servers:[{key:'rea',name:'REA',enabled:true,transport:'stdio',status:legacy?'connected':'awaiting_confirmation',approval:'ask',
      toolCount:legacy?170:0,visibleTools:legacy?170:0,totalTools:170,enabledTools:170,definitionBytes:599952,disabledTools:[],agentAccess:{mode:'disabled',tools:[]}}],
    tools:Array.from({length:170},(_,i)=>({serverKey:'rea',publicName:`mcp__rea__read_${i}`,originalToolName:`read_${i}_${'very_long_name_'.repeat(4)}`,description:`读取工具 ${i} 的简短说明。${'需要展开的详细说明。'.repeat(20)}`,definitionBytes:3500,enabled:true,filtered:!legacy,filterReason:legacy?'allowed':'awaiting_confirmation',parameters:[]})),prompts:[]};
}
export function createMcpBackend({legacy = false} = {}) {
  const current = status(legacy), writes = [], calls = [];
  function handle({path, method, body}) {
    calls.push(path);
    if (legacy) return {status: 200, json: path === '/api/mcp/status' ? current : path === '/api/settings/specs' ? {ok:true,groups:[{key:'mcp',paths:['mcp.installDir']}]} : {ok:true}};
    let json={ok:true},code=200;
    if(path==='/api/mcp/status') json=current;
    else if(path==='/api/settings/specs') json={ok:true,groups:[{key:'mcp',paths:['mcp.installDir','mcp.toolCountThreshold','mcp.definitionSizeThresholdKB']}]};
    else if(method!=='GET') {
      writes.push({path,body});
      const count=170-(body?.disabledTools?.length || 0);
      if(count>30 && !body?.exposureConfirmations){code=409;json={ok:false,error:'mcp_exposure_confirmation_required',confirmationText:'当前已启用 170 / 170 个工具，定义 599,952 字节，数量阈值 30、体积阈值 64 KB。每次请求固定占用约 149,988 tokens，可以停用部分工具。',exposureConfirmations:[{server:'rea',fingerprint:'fixture'}]};}
      else if(body?.disabledTools){current.servers[0].disabledTools=body.disabledTools;current.servers[0].enabledTools=count;current.servers[0].definitionBytes=count?count*3500+count+1:0;current.servers[0].status='connected';current.tools.forEach(tool=>{tool.filtered=body.disabledTools.includes(tool.originalToolName);tool.filterReason=tool.filtered?'tool_disabled':'allowed';});}
    }
    return {status:code,json};
  }
  return {handle, writes, calls};
}
