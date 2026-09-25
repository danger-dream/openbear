import test from 'node:test';
import assert from 'node:assert/strict';
import {toolDisplayLabel} from './toolCallPresentation.js';
import {toolArgumentsSummary, toolArgumentsRawPayload} from './toolArgumentsPresentation.js';

const cases = [
  ['History',{action:'read',scope:'current',from:'end',lastTurns:6},'读取当前对话最近 6 轮内容'],
  ['History',{action:'read',scope:'current',from:'start',turns:2,offset:3},'读取当前对话最早 2 轮内容 · 跳过 3 轮'],
  ['History',{action:'read',conversationUuid:'other',turns:2},'读取指定对话最近 2 轮内容'],
  ['History',{action:'read',scope:'current',opId:'message-1'},'读取当前对话的指定消息'],
  ['History',{action:'search',scope:'current',query:'编译'},'搜索当前对话的历史：“编译”'],
  ['History',{action:'index',source:'execution',currentTask:true,limit:20},'查看当前任务的执行记录索引 · 最多 20 条'],
  ['History',{action:'read_events',source:'execution',eventIds:['1','2']},'读取当前对话 2 条执行记录'],
  ['History',{action:'read_turn',scope:'current',before:0,after:2},'读取当前对话的指定轮次 · 含后 2 轮'],
  ['History',{action:'read_turn',scope:'current',before:1,after:0},'读取当前对话的指定轮次 · 含前 1 轮'],
  ['History',{action:'read_turn',scope:'current',before:0,after:0},'读取当前对话的指定轮次'],
  ['History',{action:'read_turn',scope:'current'},'读取当前对话的指定轮次'],
  ['History',{action:'read_turn',scope:'current',before:1,after:2},'读取当前对话的指定轮次 · 含前 1 轮、后 2 轮'],
  ['AgentHistory',{action:'read_turn',scope:'current',before:'0',after:'2'},'读取当前对话的指定轮次 · 含后 2 轮'],
  ['Memory',{resource:'entry',action:'get',ref:'openbear'},'读取 openbear 记忆内容'],
  ['Memory',{resource:'doc',action:'get',name:'部署说明'},'读取 部署说明 文档内容'],
  ['Memory',{resource:'secret',action:'set',name:'github',kvJson:'{"token":"private"}'},'写入 github 凭证内容'],
  ['Memory',{resource:'identity',action:'list'},'列出身份列表'],
  ['Memory',{resource:'entry',action:'del',id:12},'删除 #12 记忆'],
  ['Browser',{action:'page',params:{op:'new',url:'https://example.com/docs?a=1'}},'新建 example.com/docs 页面'],
  ['Browser',{action:'page',params:{op:'new',mode:'isolated'}},'新建独立 空白 页面'],
  ['Browser',{action:'page',params:{op:'list'}},'列出已打开的页面'],
  ['Browser',{action:'page',params:{op:'close'}},'关闭指定页面'],
  ['Browser',{action:'page',params:{op:'adopt'}},'接管指定页面'],
  ['Browser',{action:'page',params:{op:'release'}},'释放独立浏览器'],
  ['Browser',{action:'navigate',params:{url:'https://example.com/'}},'打开 example.com 页面'],
  ['Browser',{action:'navigate',params:{op:'back'}},'返回上一页'],
  ['Browser',{action:'navigate',params:{op:'reload'}},'刷新页面'],
  ['Browser',{action:'snapshot'},'查看页面内容快照'],
  ['Browser',{action:'capture',params:{fullPage:true}},'截取完整页面图片'],
  ['Browser',{action:'describe',params:{action:'evaluate'}},'查看页面脚本的用法'],
  ['Browser',{action:'act',params:{op:'check',checked:false,target:'r2'}},'取消勾选 r2'],
  ['Browser',{action:'act',params:{op:'press',key:'Enter'}},'按下 Enter'],
  ['Browser',{action:'act',params:{op:'fill',fields:[{target:'r1',value:'secret'},{target:'r2',value:'password'}]}},'填写 2 个表单字段'],
  ['Browser',{action:'wait',params:{timeMs:0}},'等待 0 秒'],
  ['Browser',{action:'wait',params:{text:'加载中',state:'hidden'}},'等待页面不再显示“加载中”'],
  ['Browser',{action:'dialog',params:{op:'handle',accept:false}},'取消页面对话框'],
  ['Browser',{action:'files',params:{op:'save',id:1}},'保存下载文件'],
  ['Browser',{action:'files',params:{op:'cancel',id:1}},'取消文件下载'],
  ['Browser',{action:'network',params:{id:1,part:'response-body'}},'查看指定网络请求 · 响应正文'],
  ['Browser',{action:'recover',params:{op:'restart'}},'重启浏览器实例'],
  ['OpenBearControl',{action:'status'},'查看运行状态'],
  ['OpenBearControl',{action:'think',args:{level:'xhigh'}},'调整思考强度：极高'],
  ['OpenBearControl',{action:'restart',args:{delayS:3}},'3 秒后重启服务'],
  ['OpenBearControl',{action:'skills_status',args:{query:'天气'}},'查看技能状态 · 天气'],
  ['Process',{action:'log',session_id:'s1'},'读取进程日志 · s1'],
  ['AgentInfo',{action:'capabilities'},'查看可委派能力'],
  ['AgentWait',{mode:'review_after',reviewAfterSeconds:30},'等待助手，30 秒后检查进展'],
  ['UserInteraction',{action:'questionnaire',title:'选择布局'},'请用户填写问卷 · 选择布局'],
  ['EditBatch',{path:'a.vue',edits:[{},{}]},'a.vue · 修改 2 处'],
  ['mcp__parrot__web_search',{query:'OpenBear',freshness:'week'},'“OpenBear” · 一周内'],
];
for (const [name,args,expected] of cases) test(`${name} translates ${JSON.stringify(args)}`, () => {
  assert.equal(toolArgumentsSummary(name,args),expected);
  assert.equal(toolArgumentsSummary(name,JSON.stringify(args)),expected);
});

test('tool labels are shared, and unknown tool/action/partial args remain honest', () => {
  assert.equal(toolDisplayLabel('OpenBearControl'),'OpenBear 控制');
  assert.equal(toolDisplayLabel('AgentInfo'),'助手信息');
  assert.equal(toolDisplayLabel('mcp__vendor__new_tool'),'mcp__vendor__new_tool');
  assert.equal(toolArgumentsSummary('Browser',{action:'future_action'}),'future_action');
  assert.equal(toolArgumentsSummary('Browser',{action:'page'}),'管理页面');
  assert.equal(toolArgumentsSummary('Memory',{ref:'openbear'}),'记忆操作');
  assert.equal(toolArgumentsSummary('Browser','{"action":'),'');
  assert.equal(toolArgumentsSummary('Browser',null),'');
  assert.equal(toolArgumentsSummary('Memory',[]),'');
});

test('summaries leave payloads unchanged and never promote secret fields, URL credentials or page scripts', () => {
  const args = {resource:'secret',action:'get',name:'github',kvJson:'{"token":"private-value"}'};
  const before = JSON.stringify(args);
  assert.equal(toolArgumentsSummary('Memory',args),'读取 github 凭证内容');
  assert.equal(JSON.stringify(args),before);
  assert.doesNotMatch(toolArgumentsRawPayload('Memory',args).content,/private-value/);
  const browser = {action:'page',params:{op:'new',url:'https://user:password@example.com/?token=private-value'}};
  assert.equal(toolArgumentsSummary('Browser',browser),'新建 example.com 页面');
  assert.equal(JSON.parse(toolArgumentsRawPayload('Browser',browser).content).params.url,browser.params.url);
  assert.equal(toolArgumentsSummary('Browser',{action:'evaluate',params:{expression:'secret script'}}),'执行页面脚本');
});

test('existing file descriptions and TaskMemory result subjects are preserved', () => {
  assert.equal(toolArgumentsSummary('Bash',{command:'echo 1',description:'检查状态'}),'检查状态');
  assert.equal(toolArgumentsSummary('Read',{path:'/a',description:'读取设计稿'}),'读取设计稿');
  assert.equal(toolArgumentsSummary('Write',{path:'/a',content:'not headline'}),'/a');
  assert.equal(toolArgumentsSummary('TaskMemory',{action:'get',memoryUuid:'mem-x'},{memory:{name:'界面偏好'}}),'读取记忆： 界面偏好');
});
