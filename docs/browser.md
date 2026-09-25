# 原生 Browser 工具

Browser 是 OpenBear 内置工具，对模型只暴露一个浏览器接口。执行引擎为 **Python + 原生 CDP**，无需 Node/npm、Playwright Python 包或 Playwright MCP。工具入口、页面归属、超时、恢复和产物都由 OpenBear 管理。管理通道独立连接 CDP，不依赖执行 worker 初始化成功。默认关闭，升级不会自动接管或关闭已有浏览器。

## 启用

1. 打开 **设置 → 参数设置 → 浏览器**。设置分为：浏览器连接、等待时间与用量、恢复与权限。
2. 填写已有浏览器服务的 CDP 地址（http(s)/ws(s)）。服务可在本机、容器或远端；OpenBear 不另行启动宿主机 Chrome，也不会在连接失败后换用另一套浏览器。
3. 点击连接地址旁的“测试连接”，验证当前输入是否能完成 CDP 握手。测试只读取浏览器版本和目标列表，不保存配置、不启用工具、不打开或接管页面。
4. 保存连接地址，再开启“启用浏览器”。启用或更换已启用的地址时，后端会重新验证，失败则不保存该次修改。后续轮次获得一个 `Browser` 工具。所有配置都在 `browser` 对象内，不读取或修改任何 MCP 服务配置。

浏览器默认关闭。启动时，已有的启用配置也必须通过连接验证后才注册工具；空地址、超时或不可用地址只使浏览器工具保持停用，不阻止 OpenBear 其他功能启动，也不改写用户配置。连接恢复后可关闭再开启浏览器开关。验证只在明确的配置操作和启动时执行，不增加后台轮询。

worker 使用 OpenBear 自身的 Python 解释器、已有的 aiohttp 依赖以及随包分发的浏览器内辅助脚本。辅助脚本复用 Playwright 的成熟选择器、可访问性快照和动作可执行性算法，只在 Chromium 内执行，不启动 Playwright 驱动或 Node 子进程。来源固定为 `playwright-core 1.64.0-alpha-1789764292000`，版本、SHA-256 与许可证保存在 `app/browser/pyworker/resources/`，不会跟随 `latest` 静默升级。

安装/升级脚本仅校验随包资源，不联网安装 worker 依赖、不安装 MCP、不下载或启动 Chromium。源码开发环境也可执行 `python3 scripts/install_browser_worker.py`。旧配置中的 `nodeCommand` 会被兼容忽略，不再作为设置项。前端开发构建及其他 MCP 服务是否需要 Node，与此浏览器执行引擎无关。

**服务已经运行旧源码时，安装新后端代码仍需按正常流程重启 OpenBear。修改默认参数不等于部署新代码。**

## 单一浏览器服务

连接已配置服务，保留它的 profile、Cookie、指纹参数和已有页面，不改变部署或访问控制。不同会话分别拥有标签页，但共享该浏览器的登录状态与故障范围，不承诺独立进程隔离。每调用者默认最多通过工具创建 12 页；不会因空闲自动关闭浏览器。

网页 URL 始终由浏览器访问和解释。URL 中的 localhost 指浏览器所在环境，不是 OpenBear 所在环境；即使 CDP 地址是本机地址，也可能经过端口映射或隧道，不能用 OpenBear 侧连通性证明浏览器可达。工具不自动换地址、不猜网关、不修改监听或防火墙，也不额外发送 HTTP 预检请求。网络不可达仍需按实际部署解决，本功能不是跨网络代理。

### 旧配置与调用

- 历史 `mode: main` 参数兼容接收，但不再向模型展示模式选择；`mode: isolated` 在连接和建页之前明确拒绝，不会偷偷换成共享登录。原 `page release` 操作已移除，不会改成关闭共享浏览器。
- 旧配置中的 `defaultMode`、`executablePath`、`headless`、`noSandbox`、`locale`、`timezone`、`viewportWidth`、`viewportHeight`、`maxIsolatedInstances`、`idleTimeoutS` 在加载时兼容移除，下次保存不再写回。未知的其他字段仍校验报错。现有登录资料不迁移、不清理。
- 连接地址保留存储名 `mainEndpoint`。若旧配置只有临时浏览器启动参数而没有此地址，浏览器工具保持停用，不推测地址；补充可用地址并重新开启后才注册工具。原 main 页面的归属记录沿用；旧临时实例的失效页面记录不恢复。
- 按任务显式执行 `act resize/emulate` 的能力保留；不再自动覆盖语言、时区或视口。

## 常用配置

设置页保留连接、下载共享目录、等待预算、用量、恢复与权限配置；不再提供模式选择或本机浏览器启动参数。

界面中的文件和网络内容大小以 **MB** 显示、输入，程序自动换算；例如 `2 MB` 对应原配置的 `2097152` 字节。保留字段的存储值及设置 API 单位不变。

也可由维护者在 `openbear.json` 的 `browser` 对象中配置以下原始字段：

```json
{
  "enabled": false,
  "mainEndpoint": "http://127.0.0.1:9222?fingerprint=openbear-main",
  "mainDownloadHostPath": "",
  "mainDownloadBrowserPath": "",
  "connectTimeoutS": 10,
  "actionTimeoutS": 20,
  "navigationTimeoutS": 45,
  "maxTimeoutS": 180,
  "queueTimeoutS": 10,
  "snapshotMaxChars": 12000,
  "maxEventEntries": 200,
  "maxBodyBytes": 2097152,
  "maxArtifactBytes": 33554432,
  "maxPagesPerOwner": 12,
  "autoReconnect": true,
  "recoveryCooldownS": 5,
  "allowEvaluate": true,
  "agentAccess": false,
  "mainRestartUrl": ""
}
```

超时和输出上限用于后续调用；修改限额不会重新执行已发出的操作。连接、建页、worker 绑定、排队和导航共用一次请求的总预算。连接地址改变后旧页不自动迁移。

`mainRestartUrl` 是可选、由用户配置的 HTTP POST 接口：其语义应是停止**指定浏览器实例**并保留持久 profile，随后 CDP 重连可以重新启动实例。例如兼容的 CloakServe 可提供 `/fingerprint/<seed>/close`。必须先确认该提供者版本实际保留 profile；不能填写清空资料或重启整个容器的接口。工具仅在 `recover restart` 经确认后调用一次，不自动重试。未配置时不提供外部实例重启。

## 调用示例

```json
{"action":"page","params":{"op":"new","url":"https://example.com"}}
{"action":"snapshot","page":"p...","params":{"interactive":true,"maxChars":6000}}
{"action":"act","page":"p...","params":{"op":"click","target":"s12345678:e7"}}
{"action":"act","page":"p...","params":{"op":"fill","target":"css=input[name=q]","text":"查询"}}
{"action":"capture","page":"p...","params":{"view":true}}
{"action":"describe","params":{"action":"files"}}
```

`page new` 先创建自有标签，再初始化 worker、仅绑定该页并执行只读就绪验证，有 URL 时随后导航。绑定失败会返回已创建页的句柄、失败阶段和 `pageCreated=true, navigationStarted=false`，不是“什么都没发生”；不要重复建页。不会为验证新页而初始化其他标签。

所有页面操作都明确携带 page handle，没有全局“当前标签”。原有未登记页面可由主控 `page list` 看见，`page adopt` 经确认后接管；不能接管另一会话已拥有的页面。由已拥有页面打开的 popup 继承归属。标签索引、标题和 URL 都不能代替身份。

快照 ref 绑定页面和快照版本，导航/重连后不能继续使用旧 ref。可用 `css=...` 指定唯一 CSS 定位器；不唯一时保留严格匹配行为，不自动猜另一个元素。

能力包括：页面、前进导航/后退/重载、快照、文字查找、点击/填写/逐字输入/按键/勾选/选择/悬停/拖动/滚动、视口和媒体模拟、等待、截图、弹窗、上传/拖放/下载、console、network 和页面 JavaScript。

- `evaluate` 只在网页执行，没有 Node/服务器任意代码执行入口，但网页脚本仍可能产生外部副作用。
- 浏览器捕获的网络、控制台和下载事件从当前 worker 绑定该页开始；重连不会声称找回已经丢失的事件。
- console/network 带 cursor 与 dropped 标记，旧请求详情可能过期。重定向各跳的已捕获请求正文和响应头独立保留；CDP 无法取得的中间响应正文明确报不可用，不会将最终响应错配给前一跳。
- 截图、下载、网络正文和大脚本结果保存至 `workspace/artifacts/browser/`。
- `capture view=true` 在工具批次结束后添加结构化图像输入，普通工具文本和持久审计不保存 base64。需要所用模型支持图片输入。
- 上传与文件拖放将本机文件按块通过 CDP 传入网页，可跨机器/容器使用，无需浏览器访问 OpenBear 的原始上传路径。
- 下载保存浏览器已取得的原始文件，绝不重新请求 URL。远程或跨容器浏览器需将同一共享目录分别配置为 `mainDownloadHostPath`（OpenBear 所见绝对路径）和 `mainDownloadBrowserPath`（浏览器所见绝对路径）；worker 会在其下建立独立子目录。浏览器与 OpenBear 确实共享同一文件系统时可不配置。只填一个路径时连接会明确报错；没有可用共享文件时保存会明确失败，不伪装成功。

## 故障、确认与结果语义

- `status` 只读缓存状态，不启动浏览器。
- `page list`、`recover probe`、定向关闭走独立 CDP；即使 worker 初始化失败仍可诊断。
- 操作按页排队，恢复/管理通道不排在卡死的网页队列后面。dialog 可以越过页面锁来解除已打开的模态框。
- worker 连接故障可限次恢复；连续初始化失败三次后熔断，冷却后显式 `recover connection` 可重置。
- 动作触发 JavaScript 弹窗时及时返回 `pendingDialog=true, completed=false`，保留原始弹窗供 `dialog inspect/handle` 显式处理，不自动接受，也不重复点击。
- 任何恢复都不重放旧操作。提交、上传等超时后返回 `outcome=unknown`，意味着可能已经执行；页面进入 suspect，必须检查或恢复后再修改。
- 网络拒绝、DNS 错误返回浏览器实际的网络错误；HTTP 404 是已收到响应，不是浏览器挂死，不自动重启或改为根路径。导航失败后的快照快速返回前次失败，不重复卡住；健康页面上的脚本异常仍允许快照观察。
- 连接恢复或 probe 成功都不能证明之前的提交结果。
- `recover terminate/close/restart` 与接管旧页面使用工具内置确认。附带文字的反馈、拒绝、超时都不算授权；不能通过参数伪造 confirmed。
- 子 Agent 默认无 Browser 能力。开启 agentAccess 后还须在该 Agent 的本轮工具白名单中显式授予，页面仍独立；Agent 无权代替用户确认破坏性恢复。
- 不提供静默重启容器的动作。

## 实现与验证

入口：`app/tools/browser.py`。业务服务：`app/browser/service.py`。独立管理 CDP：`cdp.py`。Python worker 生命周期与 IPC：`worker.py`。执行引擎：`pyworker/engine.py`，定位/输入/快照/iframe/网络/文件分别实现于同目录模块。浏览器内静态辅助脚本及许可证位于 `pyworker/resources/`。

测试遵守 OpenBear 项目约定，不启动实际浏览器：

```sh
.venv/bin/python -m pytest tests/test_browser_native.py tests/test_browser_lifecycle.py tests/test_browser_python_*.py tests/test_browser_failure_recovery.py tests/test_browser_single_service.py
python3 scripts/install_browser_worker.py
```

引擎回归同时覆盖元素重建/遮挡等待、按键和拖拽中断、弹窗期间的释放、iframe 会话切换与销毁、导航生命周期、文件传输取消、重定向归属、并发断连与清理，不只测试成功分支。

测试通过不等于已经在生产接管现有浏览器。启用后仍应在授权的实际使用中确认网站兼容性、登录状态及远程文件传输条件。
