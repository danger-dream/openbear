# OpenBear

单人自托管的 Agent 控制台。日常对话、工具调用和任务都在 Web 里完成；Telegram 只负责登录确认和少量运维命令。

[最新发行版](https://github.com/danger-dream/openbear/releases/latest)

## 特性

- 浏览器里使用的 Agent 工作台，支持长任务、工具和会话管理
- 多协议模型渠道：OpenAI Responses / Chat Completions、Anthropic
- 内置文件、命令行、记忆等工具；Skill 与 MCP 可选，默认不预装
- 系统提示词、Agent 提示词可在界面里编辑和切换
- 从 GitHub Release 检查更新：只换前端则刷新即可，后端变化才会重启
- 一份 systemd 服务，数据、配置和工作区都留在本机

## 环境要求

- Debian、Ubuntu 或同系发行版
- root + systemd
- Python 3.11+（安装脚本可用 uv 自动准备）
- 一个 Telegram Bot，以及你自己的 Telegram 数字用户 ID
- 至少一个可用的模型渠道（Base URL + API Key）

发行包已包含前端构建产物，目标机不必再装 Node。

## 安装

```bash
bash <(curl -Ls https://github.com/danger-dream/openbear/releases/latest/download/install.sh)
```

脚本会询问部署目录、Bot Token、Admin 的 Telegram 用户 ID、显示名，以及模型渠道。渠道会先探测并做一次对话测试，不通就不会继续装。

默认目录是 `/opt/openbear`，工作目录是其下的 `workspace`。装完后会生成 `openbear.json`（权限 `600`）、空数据库，并启动 `openbear.service`。

本机若开着 `ufw` / `firewalld`，脚本会放行 Web 端口。云厂商安全组需要自己开。不想改防火墙：

```bash
OPENBEAR_SKIP_FIREWALL=1 bash <(curl -Ls https://github.com/danger-dream/openbear/releases/latest/download/install.sh)
```

## 第一次登录

安装结束会打印内网地址、外网地址和访问密钥。

1. 用 Admin 账号给 Bot 发 `/start`
2. 打开打印出来的地址
3. 粘贴访问密钥
4. 回 Telegram 点确认

Telegram 用户 ID 不是 Bot Token，也不是 `@用户名`。可在 Telegram 里找 `@userinfobot` 查询。

## 更新

控制台左上角的版本号可以点开。有新发行版时会提示，确认后自动下载、校验并替换文件。配置、数据库、工作区和 skills 不会被覆盖。

也可以再跑一次安装脚本，效果相同：

```bash
bash /opt/openbear/scripts/install.sh
```

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

## 配置与安全

- 真实配置在 `openbear.json`，包含 Bot Token 和模型密钥，不要提交或外传
- 仓库里只有 `openbear.json.example`
- 数据库、工作区、skills、MCP 安装目录都不会进入 git
- 一台机器默认只跑一份 `openbear.service`；同一个 Bot 也不要同时被两套实例 polling
