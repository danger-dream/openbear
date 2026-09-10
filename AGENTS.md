# OpenBear 项目约束

## 测试规范（2026-09-10）

本项目禁止浏览器测试，适用于本地开发验收、CI、发行包验证和测试机验证。

- 不新增或运行 Playwright、Puppeteer、Selenium、Chrome/Chromium、CDP 等真实或无头浏览器自动化测试。
- 不以截图、视觉回归或浏览器端到端冒烟作为发布检查，也不通过增加等待或反复重跑恢复这类测试。
- 使用确定性的单元测试、组件逻辑与状态机测试、HTTP/WebSocket 接口集成测试、前端构建，以及发行包内容、SHA256、静态资源和健康检查。
- 改动对应功能时保留关键行为断言；禁止浏览器测试不等于跳过失败检查。
- 只有项目所有者明确改变此要求后，才能重新引入浏览器测试。

常规检查：

```bash
.venv/bin/python -m pytest -q
(cd web && npm test && npm run build)
python3 scripts/bump_version.py --check
```

发行包验证使用 `scripts/release_validation.py` 和 `scripts/smoke_release_login.py`；后者只通过 HTTP/WebSocket 验证解压后的真实后端与构建产物，不启动浏览器。

## 发布边界

- GitHub 公开历史与 Gitea 开发历史独立，不将开发 `main` 强推到 GitHub，不推送全部标签。
- 不把本地配置、数据库、工作区、密钥、真实 skills/MCP 或内部评测报告提交到公开仓库和发行包。
- 不在正在承载当前对话的开发机安装发行包或擅自重启服务；发行验收在指定测试机进行。
