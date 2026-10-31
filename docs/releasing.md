# OpenBear 固定发布入口

维护者负责审阅改动、确定版本、明确纳入的文件、编写面向用户的说明。**发布是发布服务器上的独立维护脚本，不是 OpenBear 产品工具、Web 接口或 Agent 能力。核心入口仍为 `scripts/release.py`；启动后由脚本连续执行，正常推进不逐阶段干预。**

## 代码与部署边界

- 发布源码可随 GitHub 源码仓库公开，但 `scripts/release.py`、`scripts/release_local.py` 和 `scripts/release_support/` 不进入用户安装 ZIP，也不由应用导入或注册。
- `scripts/release_local.py` 保留原适配层的前台进程、进度脱敏、摘要、重复启动保护及取消清理逻辑，改为可选的独立 CLI；它调用同一核心脚本，不另实现发布链。
- 凭据只由维护进程环境提供，不访问应用记忆库、数据库或配置。源码、测试、注释和文档不得包含真实密钥、个人账号资料、私有主机及路径。本机镜像配置放在已忽略的 `.release-environment.json`；运行状态、日志和授权材料不作为公开源码。提交钩子扫描暂存文本中的既有凭据模式，含 NUL 的二进制文件跳过内容扫描；这不代替对账号、会话和环境资料的人工审阅。
- 根目录 `evals/` 仅保留本机使用：整体忽略、不得跟踪或强制暂存，不进入公开源码或安装 ZIP。取消跟踪只移除当前版本中的文件，不删除本机评测与运行资料，也不重写既有历史。提交钩子允许取消跟踪，但拒绝新增、修改或改名进入 `evals/`。如取消跟踪尚未提交，需将对应的已暂存删除路径明确列入 `--files`；冻结阶段只接纳这些删除，不重新 add 本机文件，续跑也不恢复其跟踪。未审阅删除、仍有暂存内容或重新强制暂存均会拒绝。
- 历史冻结候选若仍包含 `evals/`，导出、续跑复用及发布前校验均拒绝；不会静默删项后沿用旧指纹、提交或验收。需先形成符合当前边界的已审阅候选。源码缓存若额外残留 `evals/` 也不得复用；已公开运行的状态查询／清理不会改写旧公开历史。
- 公开源代码不等于开放服务器上的执行入口。维护者在自己的受控环境执行脚本，使用本地凭据及明确的本轮授权；应用登录用户和 Agent 没有发布工具。
- 发布不更新本机在线前端、不重启正在运行的 OpenBear。公开仓库的源码、用户安装 ZIP 与维护者本地状态是三个不同边界。
- 服务器节点 `node/` 的源码随公开源码导出，但只保留 `.go`、`go.mod`、`go.sum`、`.md`、`VERSION`、`LICENSE`、`node/browser/resources/` 与 `node/mesh/wintun/` 下的内嵌资源，以及 `node/third_party/` 下内嵌模块自己的 `go.mod`、`go.sum`、`LICENSE`；另外精确纳入组网测试资源 `sim-links.json`、`contract_configs.py`、`hostfw_container.sh` 和 WireGuard 上游的 `.gitignore`、`Makefile`、`tests/netns.sh`；`node/bin/` 和任何 `openbear-node*` 构建产物不公开，被跟踪时冻结直接拒绝。**主控安装 ZIP 不含 `node/` 的任何内容，也不含节点程序**；主控运行时在本地没有节点程序时从节点自己的 GitHub Release 下载。节点按下文“服务器节点单独发布”独立发版。

- 网关的 `openbear-push-sw.js` 是编译内嵌资源，随源码导出。`benchmark-package/` 仅允许已列明工具的 recipe、版本 manifest、许可证、补丁及具体脚本/数据路径，禁止二进制归档、缓存和运行报告；这些必要资源也进入安装 ZIP，打包时核对 manifest 摘要，更新器按后端资源更新处理。
- `.gitattributes` 精确列出的固定第三方资源与补丁保持原始字节，不做空白规范化；自研文件仍须通过 `git diff --check`。上游公开 `cookies.txt` 仅在路径和 SHA256 同时匹配时豁免凭据模式扫描，换路径或修改内容不继承豁免；不能全局关闭提交钩子。

## 核心维护 CLI

```bash
.venv/bin/python scripts/release.py start 0.9.4 \
  --notes /absolute/path/release-notes.md \
  --files app/changed.py tests/test_changed.py \
  --approve-container-cgroup
```

示例版本仅用于说明，不是下一版的发布授权。无源码改动、只发布已提交内容时可以使用空的 `--files`。不要提前手工升版：本机平时是开发版号，要发布的 beta、rc 或正式版号由入口写入——它调用现有 `bump_version.py` 同步本机的五处版本文件，保留提交钩子并提交，随后冻结候选。版本号规则见下节。

- 发布脚本只接收维护进程提供的 `GH_TOKEN`（或 `GITHUB_TOKEN`）环境变量，不读取 OpenBear 配置、数据库或密码存储；不得把 token 写进命令参数、说明、状态文件、源码、注释或日志。
- `--approve-container-cgroup` 只表示已取得**本轮专属验收容器**的命名空间内 cgroup remount 授权，不是自动授权或长期许可。未获授权省略，脚本在变更源码之前暂停。恢复时如果还需运行验收，同样只在授权仍覆盖本轮时传入。
- 默认 GitHub 仓库为 `danger-dream/openbear`，可用 `--repository owner/repo` 明确指定。必须从本机 `main` 启动，不把 GitHub main 反向当作本机源码上游。
- 未列入 `--files` 的源码改动会阻止提交；自动纳入五个版本文件，不纳入开发目录原有未跟踪 `build/` 备份；`web/.dist-before-*` 前端备份整体忽略且禁止公开导出。新修复文件用 `resume --files` 显式加入。
- 说明原文在启动时冻结，不会因外部说明文件后来改变而偷偷替换。脚本补上准确 Full Changelog：正式版从上一正式版算起，beta／rc 从这条线上最近发布的版本算起；已有错误跨度会拒绝。Annotated tag 正文是最终权威，Release 使用相同 Markdown。
- 每阶段默认超时 1800 秒，可通过 `start --timeout` 配置。执行工具的总超时应覆盖整条链；不要用短超时反复杀脚本。

## 版本号规则

主控与节点各自编号、各自发布，可以只发其中一个。解析和比较只用 `app/versioning.py`（发布、构建脚本按路径加载它，不依赖 app 包或 `.venv`）；独立运行的 `scripts/updater.py` 内置同一套比较，测试保证两者结果一致。

- 格式：正式版 `X.Y.Z`；预发布版 `X.Y.Z-rc.N`、`X.Y.Z-beta.N`；开发版 `X.Y.Z-dev.N`。主控 tag 为 `vX.Y.Z…`，节点 tag 为 `node-vX.Y.Z…`。
- 先后顺序：先比 X.Y.Z；X.Y.Z 相同时正式版 > rc > beta > dev；同一类再比 N。例：`1.0.1 > 1.0.1-rc.1 > 1.0.1-beta.2 > 1.0.1-beta.1 > 1.0.1-dev.9`。不用标准 semver 排序（它把 dev 排在 beta 前面）。
- 开发版只在本地使用，**永远不发布**：两条线的 `start` 与预检都拒绝 `-dev.N`。目标版本必须比这条线上已发布的全部版本（`v*` 或 `node-v*` tag，含 beta／rc）都新；GitHub 上意外出现的开发版 tag 不计入。本机当前的开发版号不是下限，例如本机已是 `1.3.0-dev.1` 时仍可发布 `1.2.0-beta.2` 或 `1.2.0`。
- beta 和 rc 在 GitHub 上是预发布（prerelease），从不标为 Latest；主控正式版是普通发布并标为 Latest；节点任何版本都不标 Latest。主控 beta／rc 的升级验收仍从最新正式版（`/releases/latest`）开始，即默认用户实际安装的版本。
- 发布公开后，脚本把本机版本改成下一个次版本的 `dev.1`，并单独做一次只含版本文件的本地提交（`release: start <版本> development`／`release: start node <版本> development`），**不推送**。发正式版和发 beta、rc 都一样：发完 `1.1.0` 或 `1.2.0-beta.1`，分别改成 `1.2.0-dev.1`、`1.3.0-dev.1`。开发版号不会出现在 tag 或 Release 中，但会出现在之后推送的公开源码历史里。若版本文件有未提交的改动、本机版本已被改成别的值或 HEAD 在中途移动，脚本不强改，以退出码 2 停下；处理后 `resume` 只补做这一步，不再重新发布。
- 开发期间每次构建，dev 序号自动加 1：节点是每次运行 `scripts/build_node.py`（只改 `node/VERSION`）；主控是每次前端构建 `npm run build`（`web/package.json` 的 `prebuild` 运行 `web/scripts/bump-dev-version.mjs`，只用 Node，同步五处版本文件）。只改后端、没有构建前端时，主控序号不变。beta、rc、正式版构建（包括发布容器里的构建）不加号；五处版本不一致时前端构建报错，只复制了部分文件的构建（如 Dockerfile 的前端阶段）保持原号。从源码安装不算开发构建：`scripts/install.sh` 以 `OPENBEAR_SKIP_DEV_BUMP=1 npm run build` 构建，版本号不变；其他场景的 `npm run build` 照常加号。更新器安装发行包自带的 `web/dist`，不构建前端。
- `uv.lock` 中项目自身版本写 uv 的 PEP 440 规范写法（`1.2.0b1`、`1.2.0rc1`、`1.2.0.dev3`），其余四处写原样；`bump_version.py --check` 与发行包校验按此比较。
- 节点是可选功能：主控预检查看 GitHub 上有没有与这个主控兼容（协议范围与 `REQUIRED_FEATURES`）且主控用户会收到的节点版本（按 `node-v` 前缀读全部 tag，再按 tag 取 Release，和主控挑节点的方式一致），没有时只在输出和 `report.json` 的 `nodeCompatibility` 中给警告，不阻止发布。
- 用户默认只收正式版；`updates.prerelease`（设置里的“接收测试版”）打开后才收 beta 和 rc。主控的更新提示默认读 `/releases/latest`（它不返回预发布）；打开后逐页读完 Release 列表，取主控用户会收到的最新版本（第一页用 ETag 条件请求，没变时不再读后面的页；任何一页失败都保留上次结果）。主控给节点挑版本遵循同一设置。

## 可选的本地前台适配脚本

```bash
.venv/bin/python scripts/release_local.py start 0.9.4 --notes ./release-notes.md --files app/changed.py --approve-container-cgroup
.venv/bin/python scripts/release_local.py status <运行ID>
.venv/bin/python scripts/release_local.py resume <运行ID> --files tests/fix.py --approve-container-cgroup
```

这是原适配代码的独立脚本形态，不是新的发布引擎。进度输出到标准错误，有界 JSON 摘要输出到标准输出；凭据不进入参数或临时说明文件。`status`、重复 `start` 及已完成运行只读取状态，不重新发布。每次真正执行 `start/resume` 都须由本地操作者在已有本轮授权范围内显式提供容器权限参数；脚本不读取旧 Web 会话的确认记录。取消时停止子进程并仅清理已知的该次运行。

## 固定执行链

1. 核实本轮授权、工具链与 GitHub 权限；目标须为 beta、rc 或正式版，且比已有的全部 `v*` 版本新；锁定真实上一正式版、对应 tag 的源码提交和公开历史衔接点；检查有无兼容的已发布节点版本（只警告）。
2. 在本机同步版本、提交审阅过的修改；只从该提交导出允许公开的同源内容。公开提交可以有不同 SHA，但不能包含本机私有开发历史、真实配置或运行数据。
3. 自动准备／复用匹配锁文件的独立 Python 3.11、3.12、3.13 环境和统一 Node 依赖。
4. 按上一正式版源码与候选的实际内容分类。三版后端和前端分别执行或复用匹配的通过证据；证据缺失时自动补测。核对用例集合、通过／跳过／失败及未处理后台异常。既有真实上游和私有数据库条件跳过保留；评测真实环境测试只允许 `test_servers_bench_live` 中原因是 `set OPENBEAR_BENCH_LIVE=1 to run a real benchmark` 的跳过，不在发布矩阵中执行真实外部评测；需要 Go 或 Chrome 的节点集成测试（`test_servers`、`test_servers_transfers`、`test_node_browser`、`test_browser_engine_parity`）在测试镜像里按缺少工具链跳过，需要已构建网关程序的 `test_gateway_update`、`test_instance_hardening` 用例（`gateway/bin/` 是构建产物，不在导出源码中）按缺少网关程序跳过，`test_servers_hardening` 中需要 `ssh-keygen` 的用例按缺少该命令跳过，节点自身的 Go 测试由节点发布在 Go 容器里执行；其他意外跳过停止放行。
5. 前端测试通过后即可构建，不必等独立的后端测试；前端源码、依赖和构建输入未变时复用上一正式包的前端产物。**所有必要测试和构建都通过后**才打包唯一 ZIP 与四项资产。不会向正在使用的 `web/dist` 写入。
6. 包内 HTTP／WebSocket 冒烟、A、B 三者并行，任意失败均阻止公开发布。
7. A：新临时容器运行实际安装器，真实 systemd 启动服务并验证业务与资源。
8. B：另一个容器安装真实上一正式版，建立完整合法数据种子，通过旧 updater 的 transient unit 升级到与 A 相同的 ZIP，验证保留、实际前端标识及刷新／重启分支；重启分支须有可读数据库备份，仅前端更新须保持服务进程身份不变。
9. 快进推送同源公开源码，创建 annotated tag 和草稿 Release；上传四项原始资产，下载核对字节数／SHA256 后公开并验证下载入口。正式版验证已是 Latest；beta／rc 以预发布公开（`make_latest=false`），验证没有变成 Latest。
10. 定向回收本轮临时资源，自动输出状态、阶段耗时和结果报告。
11. 本机版本改为下一个次版本的 `dev.1` 并做一次本地提交（不推送）。

开发期的 `scripts/check.py` 观察 Python 依赖配置及评测资源：实质依赖变更交给 testmon 选择，项目自身版本号变化不触发 Python 重跑；评测资源变化映射到包校验、运行合同与打包测试，不强制跑全集。

没有默认 `rehearse` 前置步骤，不重复在 GitHub 构建发行包，不启动浏览器。安装器、测试等真实命令不替换为假命令。模拟 Bot 与模型只用于验收容器中的受控上游。

## 改动范围与证据复用

分类逻辑在 `scripts/release_support/scope.py`，结果保存在每次运行的 `scope.json` 和 `report.json`：

| 分类 | 执行方式 |
|---|---|
| 仅前端 | 前端全测与构建；后端源码、测试、依赖和工具链均匹配已有通过证据时复用三版矩阵，同时重跑明确列出的跨端／资源 Python 检查 |
| 仅后端 | 三版 Python 测试；符合条件时复用前端测试证据，前端产物不重新编译 |
| 两端、共享接口／依赖、未知路径 | 两端验证；前端通过即可与后端测试并行构建 |
| 仅项目版本元数据 | 按相同规则复用有效证据及前端产物，不把版本字段当依赖变化 |

比较基准是上一正式版 tag 的固定源码提交，不是本次工作区的 `git diff`。只排除项目自身版本字段，依赖版本、真实源码、测试、安装补丁与构建配置变化仍参与判定。共享接口和不认识的路径按两端验证，不自动猜测测试依赖。

测试证据保存在 `.release-runs/cache/tests/`，绑定对应源码／测试指纹、依赖环境、工具链与策略版本，并核对原始日志／JUnit 内容；文件不存在、被修改、测试失败、环境不匹配或没有既有证据时正常执行测试。复用项在汇总中明确列为 `reusedStages`，不会冒充本轮新运行。**首次使用或策略变化可能需要完整测试；快速路径不能用来绕过失败。**

仅后端发版仍保留一个整体版本号和完整发行 ZIP。前端复用时，JS/CSS/HTML及 `buildId` 保持原始字节，只更新包内 `build-info.json` 的发行版本，以兼容既有安装器／旧 updater 的版本一致性校验；不修改旧资产，不放宽校验。没有有效前端身份或源码证明不一致则停止，不盲目搬旧 dist。

## 环境与缓存

公开源码只保留通用镜像名称：`openbear-release-python:3.11`、`:3.12`、`:3.13` 和 `openbear-release-systemd:local`，不包含发布服务器实际使用的镜像标识。

入口优先使用显式 `--environment`；否则自动读取仓库根已忽略的 `.release-environment.json`，没有本机配置时才使用通用名称。续跑默认沿用该次运行冻结的环境，不自动换用新的本机配置。

这些是需由维护者准备的基础镜像，不表示各镜像都带有前端依赖。入口先解析实际 image ID，构建模块按冻结锁文件准备自己的 Python 环境，三版后端的引用集成测试与前端共用脚本准备的 Node 依赖。Vite 在容器内可写副本构建。

本机配置格式示例（不要提交实际配置）：

```json
{
  "pythonImages": {
    "311": "your-python311-baseline:fixed",
    "312": "your-python312-node20-baseline:fixed",
    "313": "your-python313-baseline:fixed"
  },
  "systemdImage": "your-systemd-baseline:fixed"
}
```

通过 `--environment /absolute/path/.release-environment.json` 传入。缺失基础镜像明确报错，不静默换 Python、Docker 安全边界或把宿主改造成测试环境。匹配的依赖缓存放在 `.release-runs/cache/`；仅项目版本字段变化可以复用，实际依赖及安装补丁变化不可冒用旧缓存。当前已知的自包含 npm 安装补丁按 `web/scripts/` 跟踪，普通界面改动不会重新安装同一套 node_modules；未知安装生命周期命令按完整 web 输入保守处理。

源码／原始资产只读挂载到容器，再复制到容器自身工作区。测试默认限制 CPU、内存和进程数；测试网络关闭。systemd 验收只调整其自身 cgroup 子树，核验宿主挂载不变，不使用 privileged、宿主 PID／网络、宿主完整可写 cgroup 或 docker.sock。

## 失败、续跑与清理

```bash
.venv/bin/python scripts/release.py status <运行ID>
.venv/bin/python scripts/release.py resume <运行ID> --approve-container-cgroup
```

使用核心 CLI 的 `status <运行ID> --summary` 或独立适配脚本的 `status <运行ID>` 读取失败摘要。摘要包含失败用例、断言／堆栈摘录、日志位置、已通过阶段和恢复参数；仅在摘要不能回答具体问题时，才读取对应原始日志。核心 CLI 的原 `status` 完整状态仍可用于深入诊断。解决具体问题后恢复同一次运行，不重新 `start` 同一版本，不删除状态绕过门禁。

| 情况 | 行为 |
|---|---|
| 同一候选、同一环境、阶段已通过 | 复用有效完成记录 |
| 只有某个矩阵测试失败 | 其他已通过测试保留；只修正该 Python 环境时不重跑另外两版、前端或构建。共享 Node 环境变化则重做受影响的阶段 |
| 源码或测试需要修改 | 修改本机文件，`resume --files ...` 提交新候选；按阶段真实输入复用，旧证据留档。仅改 Python 测试不重跑输入未变的前端测试和构建；新候选的包及对应验收仍重新生成／执行 |
| A 安装通过，B 升级失败 | 同包 A 证据保留，只恢复 B |
| 上传或公开请求超时／响应丢失 | 查询远端 tag、Release 和资产，确认真实状态后补做缺项，不重复创建 |
| 远端 main、tag、资产与本运行不一致 | 停止，不强推、不移动 tag、不删除或替换未知资产 |
| 已公开但清理失败 | 只恢复清理，不重测、重打包或重新公开 |
| 已公开但开发版号未设置（退出码 2） | 处理所说的本地冲突后 `resume`，只补做开发版号提交；提交结果不明时按父提交、提交标题与版本核实，不重复提交 |
| 已完成的运行再次 resume | 返回既有结果，不再次执行 |

已上传到正式发布链的候选不能再通过修改源码偷偷替换；需要人工处理版本边界。脚本不会自动修业务代码、放宽断言、跳过失败或宣称失败后线上已恢复。

手工取消后可以明确执行：

```bash
.venv/bin/python scripts/release.py cleanup <运行ID>
```

只处理本运行精确进程身份、Docker run ID 标签所归属的资源，保留续跑所需源码、唯一 ZIP、日志和状态。禁止全局 prune。正式发布成功后删除一次性源码副本和重复解压／构建目录，保留原始资产、报告和可复用依赖缓存。

退出码：0 完成；1 失败；2 需要授权或副作用核实；130 被中断。`report.json` 区分整次墙钟、脚本活跃时间和进程外间隔，不把并行测试耗时相加。

## 与本机上线分离

此入口**不会重启承载当前对话的 `openbear.service`，不会把 ZIP 安装到开发目录，不会替换在线前端资源**。本机源码升版、对外 Release 发布、在线资源切换和进程加载是不同状态；报告分别表述。

## 人工准备的发布信息

- 版本、明确纳入的文件和中文发布说明仍由维护者／模型审阅后填写，不让脚本从提交标题拼凑用户说明。
- 面向实际使用者说明相对上一正式版的变化，按有内容的栏目组织 `### ✨ 新功能`、`### 🔧 优化修复`、必要的升级注意和 `### 🧪 质量`；不硬凑栏目、不写 commit 流水。质量部分不声称未经验证的结果。
- 脚本把同一正文写入 annotated tag 与 Release，控制台继续采用这份权威正文；不另维护不同的纯文本说明。
- 不凭路径或手工文案断言“只需刷新／必须重启”；现场 updater 仍按实际差异决定生效方式。已有自定义模板、配置及用户数据不因升级而被默认内容覆盖。

## 按实际改动补充的验收

固定 A/B 覆盖正常安装和升级，不意味着所有发布都证明了冷安装或新 updater 引擎。只有本次改动涉及这些路径时，维护者应在启动发布前明确必要的附加验收并完成它；不要把固定流程的成功扩大解释为未运行场景已通过：

- 安装器／系统依赖变化：缺依赖冷安装及受影响的支持环境。
- updater／服务切换变化：新引擎、真实 launcher、refresh/restart 和切换后回滚；旧 updater 成功不代替新引擎验证。
- 数据库／配置变化：受影响旧结构、WAL 备份、自定义路径及迁移恢复。
- 打包／资源保留变化：受影响的旧资源代际、动态资源与 PWA 确定性验证。

不在每次发布前固定再跑一次通用演练，也不新增浏览器测试。附加验证发现产品问题时回到本机源码处理，不在发布副本里修改产品。

## 服务器节点单独发布

节点（`node/`，Go 模块 `openbear/node`）有自己的版本号 `node/VERSION`（一行，格式与规则见“版本号规则”，平时是开发版号），与主控版本、主控发布互不依赖。入口仍是 `scripts/release.py`，使用 `node` 子命令：

```bash
.venv/bin/python scripts/release.py node start 1.0.1 \
  --notes /absolute/path/node-notes.md \
  --files node/node.go node/browser/cdp.go
.venv/bin/python scripts/release.py node status <节点运行ID>
.venv/bin/python scripts/release.py node resume <节点运行ID> [--files 修正文件 ...]
.venv/bin/python scripts/release.py node cleanup <节点运行ID>
```

示例版本仅用于说明，也可以是 `1.1.0-beta.1`、`1.1.0-rc.2`。运行 ID 形如 `node-1.0.1-<12位十六进制>`／`node-1.1.0-beta.1-<12位十六进制>`，状态、日志与证据放在 `.release-runs/<运行ID>/`。凭据同样只经 `GH_TOKEN`／`GITHUB_TOKEN` 环境变量传入；节点发布不需要容器 cgroup 授权参数。

### 发布物格式（主控按此下载，不得改变）

- tag：`node-v<版本>`，annotated tag，正文为冻结的发布说明。
- GitHub Release：非草稿；正式版是普通发布，beta／rc 是预发布；**永远不标为 Latest**（创建与公开都传 `make_latest=false`，公开后核实 Latest 仍是原来的版本；若被标为 Latest 则停止，需人工恢复后再续跑）。
- 附件恰好五个：`openbear-node-linux-amd64`、`openbear-node-linux-arm64`、`openbear-node-darwin-arm64`、`openbear-node-windows-amd64.exe`、`manifest.json`。
- `manifest.json` 与 `scripts/build_node.py` 输出同格式：`{"schema": 1, "version": "<版本>", "protocol": <node/protocol.go 的 ProtocolVersion>, "protocolMin": <const ProtocolMin，没有时等于 protocol>, "features": [<var Features>], "toolchain": "go version ...", "binaries": {"<附件名>": {"sha256": "...", "bytes": N}}}`。`protocol.go` 没有 `Features` 时构建停止。

### 固定执行链

1. 校验版本格式，拒绝开发版；预检核实 GitHub 推送权限，目标 tag／Release 不存在，且版本**比远端已有的全部 `node-v*` 版本新**（按版本号规则比较，非版本形状的 tag 与开发版 tag 忽略）。有上一节点版本时自动补上 `node-v旧...node-v新` 的 Full Changelog（正式版从上一正式版算起）。
2. 写入 `node/VERSION`，连同 `--files` 明确列出的文件在本机 `main` 提交（`release: prepare node <版本>`）。未列出的改动、非公开路径同样拒绝。首次发布时若已提交的 `node/VERSION` 恰为目标版本且没有改动，直接以当前 HEAD 为候选，不做空提交。
3. 用与主控相同的导出机制生成公开源码（节点源码必须在其中），后续 tag 指向这个公开提交。导出的是本机 `main` 上全部允许公开的已提交内容，不只 `node/`；内容与公开 `main` 完全相同（例如主控刚发布过同一份源码）时不做空提交，直接给公开 `main` 当前提交打 tag。
4. 在与网关构建相同的 Go 基础镜像（`GO_BASE`）中：联网只做 `go mod download`／`go mod verify`，结果按镜像与 `go.mod`／`go.sum` 缓存在 `.release-runs/cache/go-node/`；测试和构建均断网运行，执行 `go test -race ./...`。
5. 断网交叉构建四个目标：`CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w -X openbear/node.Version=<版本>'`。校验文件头与目标平台一致（ELF／Mach-O／PE 及架构），运行 linux-amd64 产物的 `version` 子命令确认报出本版本（因此发布机需为 amd64），再生成 `manifest.json`。
6. 发布前再次确认本机 HEAD 与候选文件未变。沿用主控发布的同一条推送路径：快进推送公开 main、推送 annotated tag、建草稿 Release（beta／rc 为预发布）、上传五个附件、逐个下载核对字节数和 SHA256，然后公开（`make_latest=false`）并检查下载入口。
7. `node/VERSION` 改为下一个次版本的 `dev.1` 并做一次本地提交（不推送），规则同主控。

节点发布**不打 ZIP，也不跑 A/B 安装验收**；也不会改动主控版本文件。主控发布也不改 `node/VERSION`。

### 续跑与边界

- 各阶段与主控相同：结果写入阶段证据，同输入且证据有效时复用；`resume` 继续同一次运行，不重新 `start` 同一版本。只改与节点无关的文件时，节点测试与构建按节点源码指纹复用。
- 创建、上传或公开的响应丢失时，续跑先按 tag、已保存的 Release ID 或 Release 列表（草稿不能按 tag 查询）核实远端真实状态，补做缺项，不重复创建 Release、不重复上传已确认的附件；已公开的运行再次续跑只返回结果。
- 远端 main 已被他人移动、远端 tag 指向其他对象、Release 中有外来附件时停止，不强推、不移动 tag、不删除未知资产。
- 节点发布与主控发布共用仓库锁、本机 `main` 和公开 `main`，不要交错执行：任何一方暂停期间另一方提交或推送，会让暂停的一方在续跑时因本机 HEAD 或公开 main 变化而停止。先完成或明确放弃一方，再开始另一方。
- 主控只在本地没有节点程序时从 GitHub 获取节点 Release。先发主控、再发节点时，两次发布之间升级的主控在服务器页显示“GitHub 上还没有可用的节点版本”，暂时无法安装节点；节点 Release 公开后，安装节点时会重新检查并获取，也可在服务器设置中手动检查。先发节点则会把尚未发版的主控源码一并推到公开 `main`。

## 给模型的简短规范

> 发布能力只保留为独立维护脚本，不注册产品工具。核心入口是 `.venv/bin/python scripts/release.py`；可选 `scripts/release_local.py` 提供前台适配和摘要。凭据仅通过维护进程环境传入，容器权限只按已获得的本轮授权传参。正常阶段连续推进；失败看摘要、修正具体问题并 resume 同一运行，不重新 start，不降低验证门禁，不手工升版、重复全测或另跑演练。发布不更新在线前端、不重启本机服务。公开源码及注释不包含私有维护资料。服务器节点用 `scripts/release.py node start|status|resume|cleanup` 单独发布（tag `node-v<版本>`、五个固定附件、永不 Latest），主控 ZIP 不带节点程序。版本只发 beta、rc 或正式版（`X.Y.Z-beta.N`／`X.Y.Z-rc.N`／`X.Y.Z`，由脚本写入），须比该线已发布的都新，比较一律用 `app/versioning.py`；beta／rc 是 GitHub 预发布；发布后本机自动改为下一个次版本的 `dev.1` 并本地提交，不推送。
