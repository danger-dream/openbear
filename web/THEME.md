# OpenBear 前端主题

主题颜色由 `src/theme-tokens.css` 的 `:root` / `html.dark` 统一定义。桌面与手机共用色板；响应式 CSS 只决定布局或组件用途，不另建手机色板。

## 接入层

- `theme.js`：继续负责浅色／深色／跟随系统和本地偏好持久化，不负责颜色值。
- `index.html`：预加载主题变量并在应用启动前应用偏好，避免首屏背景跳色。
- `style.css`：应用背景、滚动条和通用基础样式。
- `dark-theme.css`：保留既有入口名称，内容为**两种主题**的 Element Plus 语义桥接，以及独立的代码语法高亮。禁止把页面补丁堆进该文件。
- `tailwind.config.js`：`ob-*` 语义色；旧 `mac*` 是同源兼容别名，支持 `/70` 等透明度修饰。
- `editorTheme.js`：Monaco 不识别 CSS 变量，因此在创建和切换主题时将当前 RGB 通道转换为 Monaco 十六进制颜色；继承编辑器本身的语法／诊断规则。

## 颜色角色

| 用途 | 变量 | 浅色／深色 |
| --- | --- | --- |
| 页面画布 | `--ob-bg` | `#f8f8f8` / `#161616` |
| 侧栏 | `--ob-sidebar` | `#f0f0f0` / `#161616` |
| 顶栏 | `--ob-header` | `#ffffff` / `#202020` |
| 普通面板、输入 | `--ob-surface` | `#ffffff` / `#202020` |
| 浮层、弹框 | `--ob-surface-raised` | `#f6f6f6` / `#2b2b2b` |
| 右侧浮动操作入口 | `--ob-floating-control` | `#f0f0f0` / `#262626` |
| 内嵌底色、表头 | `--ob-surface-soft` | `#f1f1f2` / `#303030` |
| 正文／强调／次要／辅助／禁用 | `--ob-text[-strong/-subtle/-muted/-disabled]` | 由色板定义 |
| 实色按钮文字 | `--ob-text-inverse` | 配合实色强调背景，不可用在浅面板上 |
| 边线 | `--ob-border[-soft/-strong]` | 按主题调整中性墨色和透明度 |
| 悬浮／选中／焦点 | `--ob-hover` / `--ob-selected` / `--ob-focus` | 由色板定义 |
| 遮罩 | `--ob-mask` | 黑色 28% / 55%，深色不使用白色遮罩 |

状态使用 `--ob-blue/success/warning/danger/info/violet/orange`，轻底使用对应 `-soft`。阴影使用 `--ob-shadow-panel/popover/dialog/inset`。`--ob-code-bg/text` 控制代码容器，不抹平语法高亮。

## 组件写法

```css
.panel { background: var(--ob-surface); color: var(--ob-text); border: 1px solid var(--ob-border); }
.popover { background: var(--ob-surface-raised); box-shadow: var(--ob-shadow-popover); }
.glass { background: rgb(var(--ob-surface-rgb) / .92); }
```

```html
<div class="bg-ob-surface text-ob-text border border-ob-border">...</div>
<button class="bg-ob-blue text-ob-inverse">保存</button>
```

对话顶栏与画布连续，使用 `--ob-bg`；侧栏搜索框使用 `--ob-hover` 轻底，不使用白色面板底。图标外框透明，右侧操作入口使用轻面板阴影；悬浮内容仍使用浮层阴影。

组件可以定义表达局部用途的别名（例如 `--rc-text: var(--ob-text)`），但不复制深浅色值。浮层可能传送到 `body`，其依赖必须来自根色板或自身，不能依赖对话页面祖先。

品牌图片、媒体画布、图表系列、语法高亮和 CSS mask 的黑白值有独立含义，不等同于界面主题漏改。保留这些含义，不做机械全替换。

## 验证

`npm test` 包含主题切换、根变量解析、Tailwind 透明度、Monaco 适配、正文／实色控件对比度、表格层次和现有桌面／手机交互契约。构建使用隔离 `--outDir`，在线资源更新沿用项目既有授权流程。
