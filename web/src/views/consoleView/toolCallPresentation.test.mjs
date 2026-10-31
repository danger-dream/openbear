import test from 'node:test';
import assert from 'node:assert/strict';
import {toolDisplayLabel} from './toolCallPresentation.js';
import {serversEditDiff, toolArgumentsSummary, toolArgumentsRawPayload} from './toolArgumentsPresentation.js';

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

test('Servers file transfers have Chinese summaries while raw parameters stay intact', async () => {
  const {rememberServerNames} = await import('./serverNames.js');
  rememberServerNames([{id: 'transfer-node', name: '测试-Windows'}]);
  const cases = [
    ['upload_file', {localPath: '/tmp/check.exe', remotePath: 'C:/Temp/check.exe'}, '上传文件到“测试-Windows” · C:/Temp/check.exe'],
    ['download_file', {remotePath: 'C:/Temp/result.txt', localPath: '/tmp/result.txt'}, '从“测试-Windows”下载文件 · C:/Temp/result.txt'],
    ['transfer_status', {transferId: 'transfer-1', waitSeconds: 60}, '查询“测试-Windows”的文件传输进度 · transfer-1'],
    ['transfer_cancel', {transferId: 'transfer-1'}, '取消“测试-Windows”的文件传输 · transfer-1'],
    ['describe', {topic: 'files'}, '查看文件传输用法'],
  ];
  for (const [action, params, expected] of cases) {
    const args = {action, params: {serverId: 'transfer-node', ...params}};
    const raw = JSON.stringify(args);
    assert.equal(toolArgumentsSummary('Servers', args), expected);
    assert.equal(toolArgumentsSummary('Servers', raw), expected);
    for (const state of ['queued', 'done', 'unknown']) {
      assert.equal(toolArgumentsSummary('Servers', raw, JSON.stringify({ok: true, transfer: {state}})), expected);
    }
    assert.equal(JSON.stringify(args), raw);
    assert.deepEqual(JSON.parse(toolArgumentsRawPayload('Servers', args).content), args);
  }
  for (const [action, expected] of [
    ['upload_file', '上传文件到指定服务器'], ['download_file', '从指定服务器下载文件'],
    ['transfer_status', '查询指定服务器的文件传输进度'], ['transfer_cancel', '取消指定服务器的文件传输'],
  ]) assert.equal(toolArgumentsSummary('Servers', {action}), expected);
});

test('Servers calls read as what they do on which server, without passwords', async () => {
  const {rememberServerNames} = await import('./serverNames.js');
  const {buildToolArgumentsView} = await import('./toolArgumentsPresentation.js');
  rememberServerNames([{id: 'w1', name: '公司-Windows'}, {id: 'm1', name: '公司-Mac mini'}]);
  const say = (action, params) => toolArgumentsSummary('Servers', {action, params});
  assert.equal(toolDisplayLabel('Servers'), '服务器');
  assert.equal(say('list', {}), '查看服务器列表');
  assert.equal(say('get', {serverId: 'w1'}), '查看“公司-Windows”的详情');
  assert.equal(say('get', {serverId: 'unknown-id'}), '查看指定服务器的详情');
  assert.equal(say('metrics', {serverId: 'm1', range: '24h'}), '查看“公司-Mac mini”的监控数据 · 最近 24 小时');
  assert.equal(say('exec', {serverIds: ['w1', 'm1'], command: 'hostname\nwhoami'}), '执行 hostname · 在“公司-Windows”、“公司-Mac mini”上');
  assert.equal(say('exec', {group: '国内', command: 'uptime', purpose: '看负载'}), '看负载 · 在“国内”分组的在线服务器上');
  // Servers no longer asks for confirmation; a stale confirm flag is not shown.
  assert.equal(say('exec', {all: true, command: 'rm -rf /tmp/x', confirm: true}), '执行 rm -rf /tmp/x · 在全部在线服务器上');
  assert.equal(say('list_dir', {serverId: 'w1', path: '/'}), '列出“公司-Windows”的根目录');
  assert.equal(say('list_dir', {serverId: 'm1', path: '/Users'}), '列出“公司-Mac mini”的 /Users');
  assert.equal(say('read_file', {serverId: 'm1', path: '/etc/hosts', encoding: 'base64'}), '下载“公司-Mac mini”上的文件 · /etc/hosts');
  assert.equal(say('write_file', {serverId: 'w1', path: 'C:/a.bin', contentBase64: 'AAAA'}), '上传“公司-Windows”上的文件 · C:/a.bin');
  assert.equal(say('file_op', {serverId: 'w1', op: 'compress', paths: ['C:/d/a', 'C:/d/b'], name: 'pack', format: 'zip'}),
    '在“公司-Windows”上压缩 2 项为“pack.zip” · C:/d/a 等 2 项');
  assert.equal(say('file_op', {serverId: 'm1', op: 'rename', path: '/tmp/a.txt', name: 'b.txt'}), '在“公司-Mac mini”上重命名 · /tmp/a.txt → b.txt');
  assert.equal(say('file_op', {serverId: 'm1', op: 'chmod', paths: ['/srv'], mode: '755', recursive: true}), '在“公司-Mac mini”上把权限改为 755（含子目录） · /srv');
  assert.equal(say('file_op', {serverId: 'w1', op: 'delete', paths: ['C:/x']}), '在“公司-Windows”上删除 · C:/x');
  assert.equal(say('edit', {serverId: 'w1', profile: {group: '国内', region: 'CN'}}), '修改“公司-Windows” · 分组、地区');
  assert.equal(say('add', {name: '公司-Windows', ssh: {host: '192.0.2.3', port: 22, username: 'admin', password: 'pw'}, install: true}),
    '添加服务器“公司-Windows” · SSH admin@192.0.2.3 · 并安装节点');
  assert.equal(say('install', {serverId: 'm1'}), '通过 SSH 在“公司-Mac mini”上安装节点');
  assert.equal(say('batch_install', {servers: [{host: 'a'}, {host: 'b'}], ssh: {username: 'root'}}), '批量添加并安装 2 台服务器 · SSH 用户 root');
  assert.equal(say('set_ssh', {serverId: 'm1', ssh: {host: '192.0.2.53', port: 2222, username: 'dev'}}), '设置“公司-Mac mini”的 SSH 连接 · dev@192.0.2.53:2222');
  assert.equal(say('remove_node', {serverId: 'w1', uninstall: true}), '移除“公司-Windows”的节点并卸载');
  assert.equal(say('delete', {serverId: 'w1'}), '删除服务器“公司-Windows”');
  assert.equal(say('save_settings', {timezone: 'Asia/Tokyo', reminders: {}}), '修改服务器设置 · 时区、提醒规则');
  assert.equal(say('code_create', {name: '新机器', group: '国内', maxUses: 2}), '创建注册码“新机器” · “国内”分组 · 最多用 2 次');
  assert.equal(say('command_save', {name: '磁盘', command: 'df -h /'}), '添加常用命令“磁盘” · df -h /');
  assert.equal(say('test_access', {url: 'https://node.example.com/'}), '测试接入点 · node.example.com');
  assert.equal(say('update_node', {all: true}), '更新全部服务器的节点');
  assert.equal(say('install', {serverId: 'm1', browser: true}), '通过 SSH 在“公司-Mac mini”上安装节点并开启浏览器');
  assert.equal(say('browser', {serverId: 'w1'}), '查看“公司-Windows”的浏览器');
  assert.equal(say('set_browser', {serverId: 'w1', enabled: true}), '开启“公司-Windows”的浏览器');
  assert.equal(say('set_browser', {serverId: 'w1', enabled: true, download: true}), '开启“公司-Windows”的浏览器 · 使用下载的测试版 Chrome');
  assert.equal(say('set_browser', {serverId: 'm1', enabled: true, endpoint: 'http://127.0.0.1:9222'}), '开启“公司-Mac mini”的浏览器 · 连接 http://127.0.0.1:9222');
  assert.equal(say('set_browser', {serverId: 'w1', enabled: false}), '关闭“公司-Windows”的浏览器');
  assert.equal(say('mesh', {}), '查看组网概况');
  assert.equal(say('mesh_draft', {}), '查看组网草稿');
  assert.equal(say('mesh_save_draft', {rules: [], members: [{serverId: 'w1'}]}), '修改组网草稿 · 成员、访问规则');
  assert.equal(say('mesh_save_draft', {}), '修改组网草稿');
  assert.equal(say('mesh_join', {serverIds: ['w1', 'm1']}), '把“公司-Windows”、“公司-Mac mini”加入组网');
  assert.equal(say('mesh_join', {serverId: 'm1'}), '把“公司-Mac mini”加入组网');
  assert.equal(say('mesh_leave', {serverId: 'w1'}), '把“公司-Windows”移出组网');
  assert.equal(say('mesh_apply', {}), '应用组网草稿');
  assert.equal(say('mesh_discard', {}), '放弃组网草稿');
  assert.equal(say('mesh_rollback', {}), '回退到上一个组网版本');
  assert.equal(say('mesh_member', {serverId: 'm1'}), '查看“公司-Mac mini”的组网状态');
  assert.equal(say('mesh_trend', {serverId: 'm1', range: '7d'}), '查看“公司-Mac mini”的组网趋势 · 最近 7 天');
  assert.equal(say('mesh_trend', {serverId: 'w1'}), '查看“公司-Windows”的组网趋势 · 最近 1 小时');
  assert.equal(say('mesh_path', {src: 'w1', dst: 'm1'}), '查看“公司-Windows”到“公司-Mac mini”的组网路径');
  assert.equal(say('mesh_path', {src: 'w1'}), '查看“公司-Windows”到各成员的组网路径');
  const meshDone = (action, params, result) => toolArgumentsSummary('Servers', {action, params}, JSON.stringify(result));
  assert.equal(meshDone('mesh_apply', {}, {apply: {running: false, revision: 67, progress: [{serverId: 'w1', state: 'done'}]}}), '应用组网草稿 · 版本 67');
  assert.equal(meshDone('mesh_apply', {}, {apply: {running: true, revision: 68, progress: [{state: 'failed'}, {state: 'offline'}, {state: 'applying'}]}}),
    '应用组网草稿 · 版本 68 · 仍在下发 · 1 台失败 · 1 台离线，重连后下发');
  assert.equal(meshDone('mesh_rollback', {}, {apply: {running: false, revision: 69, progress: []}}), '回退到上一个组网版本 · 版本 69');
  assert.equal(meshDone('mesh_save_draft', {rules: []}, {ok: false, error: 'mesh_invalid', errors: [{path: 'rules[0]'}, {path: 'rules[1]'}]}),
    '修改组网草稿 · 访问规则 · 未保存：草稿有 2 处错误');
  assert.equal(meshDone('mesh_apply', {}, {ok: false, error: 'mesh_invalid', errors: [{path: 'members[0]'}]}), '应用组网草稿 · 未应用：草稿有 1 处错误');
  assert.equal(meshDone('mesh_join', {serverIds: ['w1', 'm1']}, {items: [{serverId: 'w1', ok: true}, {serverId: 'm1', ok: false, error: 'node_offline'}]}),
    '把“公司-Windows”、“公司-Mac mini”加入组网 · 1 台没有加入');
  // Benchmarks (评测).
  assert.equal(say('describe', {topic: 'bench'}), '查看评测用法');
  assert.equal(say('bench_catalog', {}), '查看评测项目');
  assert.equal(say('bench_runs', {}), '查看评测记录');
  assert.equal(say('bench_runs', {serverId: 'w1', test: 'ipcheck', state: 'done'}), '查看评测记录 · “公司-Windows” · IP.Check.Place · 完成');
  assert.equal(say('bench_start', {serverId: 'w1', test: 'netcheck', options: {mode: 'full', family: '46'}}), '在“公司-Windows”上运行 Net.Check.Place · IPv4 + IPv6、完整模式');
  assert.equal(say('bench_start', {serverId: 'm1', test: 'ipcheck', options: {report: true}}), '在“公司-Mac mini”上运行 IP.Check.Place · 生成在线报告');
  assert.equal(say('bench_start', {serverId: 'm1', test: 'tcpquality', options: {mode: 'route-v4'}}), '在“公司-Mac mini”上运行 TCPQuality · 回程线路 IPv4');
  assert.equal(say('bench_start', {serverId: 'w1', suite: true, options: {family: '4'}}), '在“公司-Windows”上运行综合评测 · 仅 IPv4');
  assert.equal(say('bench_start', {serverId: 'w1', test: 'unlock', options: {region: 'jp'}}), '在“公司-Windows”上运行 Service Unlock · 地区 jp');
  assert.equal(say('bench_wait', {runId: 'abc'}), '等待评测结束');
  assert.equal(say('bench_get', {runId: 'abc'}), '读取评测报告');
  assert.equal(say('bench_get', {runId: 'abc', part: 'json'}), '读取评测结果数据');
  assert.equal(say('bench_get', {runId: 'abc', part: 'log'}), '读取评测运行日志');
  assert.equal(say('bench_stop', {runId: 'abc'}), '停止评测');
  assert.equal(say('bench_delete', {runId: 'abc'}), '删除评测记录');
  const run = {id: 'abc', server: 'Wawo-US', test: 'ipcheck', name: 'IP.Check.Place', state: 'done'};
  assert.equal(meshDone('bench_start', {serverId: 'w1', suite: true}, {ok: true, runs: [run, run, run, run]}), '在“公司-Windows”上运行综合评测 · 排入 4 项');
  assert.equal(meshDone('bench_start', {serverId: 'w1', test: 'ipcheck'}, {ok: true, runs: [run]}), '在“公司-Windows”上运行 IP.Check.Place');
  assert.equal(meshDone('bench_wait', {runId: 'abc'}, {ok: true, reason: 'ended', run}), '等待评测结束 · “Wawo-US”的 IP.Check.Place · 完成');
  assert.equal(meshDone('bench_wait', {runId: 'abc'}, {ok: true, reason: 'timeout', run: {...run, state: 'running'}}), '等待评测结束 · “Wawo-US”的 IP.Check.Place · 仍运行中');
  assert.equal(meshDone('bench_get', {runId: 'abc'}, {ok: true, run: {...run, test: 'geekbench5', name: 'Geekbench 5'}}), '读取评测报告 · “Wawo-US”的 Geekbench 5 · 完成');
  assert.equal(meshDone('bench_stop', {runId: 'abc'}, {ok: true, run: {...run, state: 'running', stopping: true}}), '停止评测 · “Wawo-US”的 IP.Check.Place · 正在停止');
  assert.equal(meshDone('bench_get', {runId: 'x'}, {ok: false, error: 'bench_run_not_found'}), '读取评测报告');
  assert.equal(meshDone('mesh_join', {serverId: 'w1'}, {items: [{serverId: 'w1', ok: true}]}), '把“公司-Windows”加入组网');
  // Exact edits show the path, and once done how it went; the diff gets its own section.
  const edit = {action: 'edit_file', params: {serverId: 'm1', path: '/etc/nginx/site.conf', edits: [{old: 'a', new: 'b'}, {old: 'c', new: 'd'}]}};
  assert.equal(toolArgumentsSummary('Servers', edit), '修改“公司-Mac mini”上的文件 · /etc/nginx/site.conf · 2 处');
  const diff = '--- /etc/nginx/site.conf\n+++ /etc/nginx/site.conf\n@@ -1 +1 @@\n-a\n+b\n';
  assert.equal(toolArgumentsSummary('Servers', edit, JSON.stringify({ok: true, replacements: 2, changed: true, diff})),
    '修改“公司-Mac mini”上的文件 · /etc/nginx/site.conf · 2 处 · 2 处替换');
  assert.equal(toolArgumentsSummary('Servers', edit, JSON.stringify({ok: false, error: 'edit_not_applied', failures: []})),
    '修改“公司-Mac mini”上的文件 · /etc/nginx/site.conf · 2 处 · 未修改：有替换没能匹配');
  assert.equal(serversEditDiff('Servers', edit, JSON.stringify({ok: true, diff})), diff);
  assert.equal(serversEditDiff('Servers', {action: 'read_file', params: {}}, JSON.stringify({diff})), '');
  // Background jobs show the action and the job id.
  assert.equal(say('job_start', {serverId: 'w1', command: 'npm run build\nnpm test'}), '在“公司-Windows”上启动后台任务 · npm run build');
  assert.equal(toolArgumentsSummary('Servers', {action: 'job_start', params: {serverId: 'w1', command: 'make'}}, JSON.stringify({ok: true, job: {id: 'mh2k3j9a1b2c'}})),
    '在“公司-Windows”上启动后台任务 · make · 任务 mh2k3j9a1b2c');
  assert.equal(say('jobs', {serverId: 'w1'}), '查看“公司-Windows”的后台任务');
  assert.equal(say('job_status', {serverId: 'w1', jobId: 'mh2k3j9a1b2c'}), '查看“公司-Windows”的后台任务 · mh2k3j9a1b2c');
  assert.equal(say('job_wait', {serverId: 'm1', jobId: 'mh2k3j9a1b2c', until: 'listening on'}), '等待“公司-Mac mini”的后台任务 · mh2k3j9a1b2c · 直到输出匹配 listening on');
  assert.equal(say('job_stop', {serverId: 'm1', jobId: 'mh2k3j9a1b2c'}), '停止“公司-Mac mini”的后台任务 · mh2k3j9a1b2c');
  assert.equal(say('restart_browser', {serverId: 'm1'}), '重启“公司-Mac mini”的浏览器');
  // The Browser tool names a node by server name or id.
  const page = (params, action = 'page') => toolArgumentsSummary('Browser', {action, params});
  assert.equal(page({op: 'new', node: 'w1', url: 'https://example.com/'}), '在“公司-Windows”的浏览器里新建 example.com 页面');
  assert.equal(page({op: 'new', node: 'edge-fra'}), '在“edge-fra”的浏览器里新建 空白 页面');
  assert.equal(page({op: 'list', node: 'm1'}), '列出“公司-Mac mini”的浏览器里的页面');
  assert.equal(page({op: 'restart', node: 'm1'}, 'recover'), '重启“公司-Mac mini”的浏览器');
  assert.equal(say('frobnicate', {}), 'frobnicate');

  const args = {action: 'add', params: {name: 'x', ssh: {host: 'h', username: 'u', password: 'private-pw', privateKey: 'private-key'}}};
  const before = JSON.stringify(args);
  assert.doesNotMatch(toolArgumentsRawPayload('Servers', args).content, /private-pw|private-key/);
  assert.match(toolArgumentsRawPayload('Servers', args).content, /\[已隐藏\]/);
  assert.doesNotMatch(buildToolArgumentsView('Servers', args).blocks[0].content, /private-pw/);
  assert.equal(JSON.stringify(args), before);
  const upload = toolArgumentsRawPayload('Servers', {action: 'write_file', params: {serverId: 'w1', path: 'C:/a', contentBase64: 'A'.repeat(4096)}}).content;
  assert.match(upload, /二进制内容，约 3 KB/);
  // Arguments still streaming in are not JSON yet; their secrets stay hidden too.
  const partial = '{"action":"add","params":{"ssh":{"host":"h","password":"half-typed-pw","privateKey":"-----BEGIN';
  assert.doesNotMatch(toolArgumentsRawPayload('Servers', partial).content, /half-typed-pw|BEGIN/);
  assert.doesNotMatch(buildToolArgumentsView('Servers', partial).blocks[0].content, /half-typed-pw|BEGIN/);
  assert.match(toolArgumentsRawPayload('Servers', partial).content, /"host":"h"/);
  // A streaming Memory credential is hidden before resource arrives, closed or not.
  for (const memory of ['{"kvJson":"[{\\"key\\":\\"token\\",\\"value\\":\\"half-secret', '{"name":"gh","kvJson":"{\\"token\\":\\"full-secret\\"}","resource":"sec']) {
    assert.doesNotMatch(toolArgumentsRawPayload('Memory', memory).content, /half-secret|full-secret/);
    assert.doesNotMatch(buildToolArgumentsView('Memory', memory).blocks[0].content, /half-secret|full-secret/);
  }
  assert.match(toolArgumentsRawPayload('Memory', '{"name":"gh","kvJson":"{\\"token\\":\\"x\\"}","resou').content, /"name":"gh"/);
});
