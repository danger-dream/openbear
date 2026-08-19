# OpenBear

单人自用的 Agent Web 控制台。Telegram 只负责登录确认和少量运维命令，日常对话在 Web 里完成。

## 环境

- Debian / Ubuntu 及使用 apt 的同系发行版
- root + systemd
- Python ≥ 3.11（脚本可用 uv 自动装 3.12）
- Node ≥ 18（脚本可自动安装）

## 一键安装

```bash
bash <(curl -Ls https://github.com/danger-dream/openbear/releases/latest/download/install.sh)
```

还没有正式发行版时，可以用仓库里的脚本，它会回退到 `git clone`：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/danger-dream/openbear/main/scripts/install.sh)
```

安装时会询问：

1. 部署目录（默认 `/opt/openbear`）
2. Telegram Bot Token（@BotFather）
3. Admin 的 **Telegram 用户数字 ID**（不是 Bot Token，也不是 @用户名；用 @userinfobot 查）
4. 称呼（回车默认为「老大」）
5. 模型渠道：渠道名称、Base URL、API Key，再选择协议和主模型。脚本会拉模型列表并测试主模型，不通就停止安装。

脚本会创建 venv、构建前端、生成 `openbear.json`、安装 `openbear.service` 并启动。若本机 `ufw` / `firewalld` 处于活动状态，默认会放行 Web 端口（可用 `OPENBEAR_SKIP_FIREWALL=1` 跳过）。云厂商安全组需要你自己放行。

工作目录固定为部署目录下的 `workspace`，例如 `/opt/openbear/workspace`。默认不安装任何 Skill / MCP，也不写入记忆、凭证或文档。

## 第一次登录

安装结束会打印内网地址、外网地址和访问密钥。

1. 用 Admin 的 Telegram 账号给 Bot 发一次 `/start`
2. 浏览器打开打印出来的内网或外网地址
3. 粘贴访问密钥
4. 回 Telegram 点确认

## 升级

正式安装只认 GitHub Release，不跟 `main`。运行中的控制台每 5 分钟检查最新稳定版，左上角版本号可点：有更新时会出标记，窗口里是更新说明，二次确认后由独立更新器换文件；只换了前端就提示刷新，动了后端才重启。失败会回滚代码并写 `data/update-result.json`。

也可以再跑安装脚本，拉最新发行包：

- 下载最新 Release zip 并校验 SHA256
- 更新 Python 依赖；发行包已带 `web/dist` 时不再现场构建
- 需要时重启服务
- **保留** `openbear.json`、`data/`、`workspace/`、`skills/`

安装目录里如果有未提交的源码改动，升级会拒绝执行，避免覆盖本地修改。

```bash
bash /opt/openbear/scripts/install.sh
```

## 卸载

```bash
bash /opt/openbear/scripts/uninstall.sh
```

默认只停服务、删 unit，不删数据和配置。需要连目录一起删时：

```bash
OPENBEAR_PURGE=1 bash /opt/openbear/scripts/uninstall.sh
```

## 常用命令

```bash
systemctl status openbear
journalctl -u openbear -f
curl -fsS http://127.0.0.1:18961/health
```

## 安全

- `openbear.json` 含 Bot Token 和模型密钥，权限为 `600`，不要提交、不要发到群里
- 仓库只包含 `openbear.json.example`，真实配置、数据库、工作区、skills、MCP 安装都不进 git
