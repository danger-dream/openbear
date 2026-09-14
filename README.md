# OpenBear

单人自托管的 Agent 控制台。日常对话、工具调用和任务在 Web 里完成；Telegram 提供登录确认、运维命令、交互作答和任务通知，也可通过回复任务通知继续原 Web 会话。

[最新发行版](https://github.com/danger-dream/openbear/releases/latest)

## 特性

- 浏览器里使用的 Agent 工作台，支持长任务、工具和会话管理
- 多协议模型渠道：OpenAI Responses / Chat Completions、Anthropic
- 内置文件、命令行、记忆等工具；Skill 与 MCP 可选，默认不预装
- 系统提示词、Agent 提示词可在界面里编辑和切换
- 从 GitHub Release 检查更新：只换前端则刷新即可，后端变化才会重启
- 一份 systemd 服务，数据、配置和工作区都留在本机

### Telegram 任务续聊

- 开启长任务通知后，完成信息与最终回答合并发送；超长回答分页，每一段都可以回复。
- 直接用文字回复通知，或点击「回复继续」后回复输入提示，即可继续**原 Web 会话**；点击按钮本身不会执行任务。普通 TG 文本不作为 Agent 输入。
- 浏览器无需在线。上下文与当前会话设置沿用 Web；任务运行中收到的回复进入同一主控的插话队列，不会另起并行任务。
- Web 发起的任务继续遵守通知阈值；TG 续聊的完成或失败结果始终回 TG，不受时长限制。图片、文件和语音请在 Web 中发送。
- 仅限白名单本人私聊；已删除、已归档的会话及超过 90 天的通知不能续聊。重复投递不会重复执行；重启前提交结果不确定的指令会提示核实，不自动重放。

### 行内引用与实时目录

- 在消息中输入 `@`，按类型选择记忆、文档、凭证、会话或历史问答／消息；支持中文、英文、拼音全拼与首字母搜索。行首、换行后、中文或标点后可直接输入 `@`，无需补空格；邮箱与代码块不误触发。桌面也可把左侧会话或导航悬浮目录中的条目拖入编辑区。
- 引用以行内胶囊展示，发送后的消息保持相同样式；复制粘贴、草稿恢复和「从此处重来」保留真实引用 ID 与范围。复制到纯文本环境时保留可读的引用链接，不复制材料正文。
- 发送前由后端直接读取所选材料并交给模型，不再让模型先查名称、再读取详情。前后端按 Markdown 语法识别有效引用；代码块（反引号／波浪号围栏、缩进代码）、行内代码及转义示例不展开材料。会话使用 History 的可见对话口径，默认全文；可点击编辑中的会话胶囊改为最近若干轮。超过 History 输出限制或模型输入预算时明确报错，不静默截断。
- 普通材料保留发送时版本，历史胶囊可查看当时引用的内容；重新编辑发送则重新校验来源。来源删除或停用时，编辑中的胶囊会提示失效。
- **选择凭证并发送，会把该凭证交给当前模型服务。** 目录只展示凭证名称及字段名（key），不包含值；拖拽、剪贴板和草稿只保存引用，凭证预览不显示值。凭证材料快照单独加密保存；备份／迁移数据库时，也必须保存数据库同目录的 `reference-materials.key`（权限 `600`），否则旧的受保护快照无法解密。
- 全局元数据 WebSocket 与聊天流分离，工具和网页修改都会同步到引用目录；连接中断后自动重连并重新校准。记忆／文档候选附带约 30 字正文短摘要，凭证候选只列字段名；完整正文与凭证值不进入全局目录，归档内容需显式开启后查询。

### 会话统计概览

- 桌面鼠标停留在已保存的会话节点上，可查看会话模型、思考强度、Fast 状态，以及状态、本次已运行时间、累计 Token（含缓存）、消息条数、累计耗时、模型／工具调用次数及总花费；不必切换当前会话。长耗时以天／小时／分／秒显示。配置与输入区一致，运行中修改的设置在下一轮生效。
- 浮层只通过现有全局 WebSocket 订阅当前查看的会话汇总，不加载聊天正文或工具明细；关闭后取消订阅，重新打开及断线恢复会重新校准。消息条数沿用用户／助手消息计数，不等同于问答轮数。
- 运行中的会话图标带旋转边框，结束后自动恢复；保持原节点尺寸及点击、右键、拖拽规则，并适配深色。旋转边框作为必要的运行状态提示，在“减少动态效果”模式下仍保留；其他动画继续遵守该偏好。目录与本地未发送草稿不显示统计浮层，已归档会话仍需手动展开后查看。

## 环境要求

- Debian、Ubuntu 或同系发行版
- root + systemd
- `curl`（用于首次下载安装器）
- Python 3.11+（目标机没有合适版本时，安装器会通过 uv 自动准备）
- 一个 Telegram Bot，以及你自己的 Telegram 数字用户 ID
- 至少一个可用的模型渠道（Base URL + API Key）

发行包已包含前端构建产物，目标机不必再装 Node。只有强制走 git/源码且缺少构建产物时，安装器才会准备 Node。

## 安装

先完整下载最新安装器，下载成功后再执行；临时文件会自动清理：

```bash
bash -c '
  installer="$(mktemp)" || exit 1
  trap "rm -f \"$installer\"" EXIT
  curl -fSL --retry 3 -o "$installer" \
    https://github.com/danger-dream/openbear/releases/latest/download/install.sh || exit $?
  bash "$installer"
'
```

脚本会先补齐 CA 证书和 Python 等最小安装引导，再询问部署目录、Bot Token、Admin 的 Telegram 用户 ID、显示名，以及模型渠道。渠道会先探测并做一次对话测试，不通就不会继续装。

默认目录是 `/opt/openbear`，工作目录是其下的 `workspace`。装完后会生成 `openbear.json`（权限 `600`）、空数据库，并启动 `openbear.service`。

本机若开着 `ufw` / `firewalld`，脚本会放行 Web 端口。云厂商安全组需要自己开。不想改防火墙，可把环境变量传给同一下载命令：

```bash
OPENBEAR_SKIP_FIREWALL=1 bash -c '
  installer="$(mktemp)" || exit 1
  trap "rm -f \"$installer\"" EXIT
  curl -fSL --retry 3 -o "$installer" \
    https://github.com/danger-dream/openbear/releases/latest/download/install.sh || exit $?
  bash "$installer"
'
```

## 第一次登录

安装结束会打印内网地址、外网地址和访问密钥。

1. 用 Admin 账号给 Bot 发 `/start`
2. 打开打印出来的地址
3. 粘贴访问密钥
4. 回 Telegram 点确认

Telegram 用户 ID 不是 Bot Token，也不是 `@用户名`。可在 Telegram 里找 `@userinfobot` 查询。

## 安装为应用（PWA）

已有用户升级到包含此功能的版本后，从**自己的可信 HTTPS 地址**打开 OpenBear，在「设置 → 安装应用 (PWA)」检查当前入口并按浏览器指引安装；无需按域名重新构建或额外安装脚本。地址自动使用当前 `location.origin`，不会绑定会话链接。

Chrome / Edge 只有在浏览器实际提供安装事件时才显示系统安装按钮；HTTPS 并不保证能弹安装框。iPhone / iPad 可在 Safari 分享菜单选择「添加到主屏幕」，内置浏览器建议改用系统浏览器。HTTP 页面允许输入自己的 HTTPS 入口，核对后手动前往（仅 origin，不复制路径、查询参数、片段、会话或密钥），随时可取消。

安装后仍使用同源 API、WebSocket 和现有登录认证；首期不包含 Service Worker、离线业务缓存或 Web Push。网页不能可靠判断设备是否已经安装，检测结果只说明当前窗口模式、资源与浏览器提供的能力。

## 更新

### 公开 v0.1.2 用户首次升级到 v0.2.0

等待 GitHub 的 `latest` 已显示 `v0.2.0` 后，按以下顺序做一次桥接升级：

1. 先结束正在运行的主控/Agent 任务和待回答交互；这些进程内工作不能跨后端重启继续执行。
2. **不要**先点 v0.1.2 控制台里的更新，也不要运行本地旧版 `/opt/openbear/scripts/install.sh`。第一次升级仍会执行机器上已有的旧更新逻辑，下载包里的修复不能反向保护已经启动的旧 updater。
3. 重新完整下载 `latest` 的新 installer 并执行：

```bash
bash -c '
  installer="$(mktemp)" || exit 1
  trap "rm -f \"$installer\"" EXIT
  curl -fSL --retry 3 -o "$installer" \
    https://github.com/danger-dream/openbear/releases/latest/download/install.sh || exit $?
  bash "$installer"
'
```

4. 升级完成后手动刷新原来的 Web 页面。完成这次桥接后，后续版本可正常使用网页更新。

新 installer 会保留 `openbear.json`、`data`、`workspace`、`skills` 和 MCP 安装目录。后端重启型升级会在旧服务停止后、切换新代码前，对配置中实际 `storage.dbPath` 创建 SQLite 一致性备份；路径会打印并保留在 `data/backups`。只有前端变化或旧数据库不存在时不会凭空创建备份。

代码/依赖自动回滚**不会**自动用旧快照覆盖数据库，以免抹掉新版本已经写入的数据。如需人工恢复数据库备份，必须先停服务，并接受备份时间点之后的数据会丢失；使用过新版功能后也不能承诺旧代码理解所有新状态，降级前应先确认边界。

升级不会覆盖已有的激活模板或用户自定义模板。希望采用新版模板时，请升级后在 Web 模板页显式导入/合并和激活，并在新会话中验证。v0.2.0 新增的 Telegram 待答交互通知默认开启，可在 Web 设置中关闭通知或 TG 回复能力。

### 后续更新

完成上述 v0.1.2 桥接后，控制台左上角的版本号会提示新发行版。确认后会自动下载、校验并替换文件；后端重启升级前会创建上述数据库备份。更新前仍应先结束运行中的任务和待回答交互，更新后刷新页面。

安装目录里如果有未提交的源码改动，升级会拒绝执行。

## 卸载

```bash
bash /opt/openbear/scripts/uninstall.sh
```

默认只停止服务、删除 systemd unit，保留数据和配置。连目录一起删：

```bash
OPENBEAR_PURGE=1 bash /opt/openbear/scripts/uninstall.sh
```

## 运维

```bash
systemctl status openbear
journalctl -u openbear -f
curl -fsS http://127.0.0.1:18961/health
```

默认 Web 端口是 `18961`。

## 前端开发验证

`web` 目录执行 `npm ci && npm test && npm run build`，运行 Node 单元、组件逻辑和 HTTP/WebSocket 接口测试并构建前端。

发布 CI 使用相同命令，发行包登录验证通过 HTTP/WebSocket 接口完成，无需安装或启动浏览器。

## 配置与安全

- 真实配置在 `openbear.json`，包含 Bot Token 和模型密钥，不要提交或外传
- 仓库里只有 `openbear.json.example`
- 数据库、工作区、skills、MCP 安装目录都不会进入 git
- 一台机器默认只跑一份 `openbear.service`；同一个 Bot 也不要同时被两套实例 polling
