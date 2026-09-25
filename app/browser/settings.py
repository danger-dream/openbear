"""Browser setting explanations consumed by the existing Web/TG editors."""

from app.browser.config import BrowserConfig

# Descriptions explain the user-visible behavior, prerequisites and defaults.
LABELS = {
    "enabled": (
        "启用浏览器",
        "默认关闭。设置连接地址并通过真实 CDP 验证后才允许开启；启用或更换已启用的连接地址时会重新验证，失败不保存。启动时也会验证，连接失败只停用浏览器工具，不影响其他功能。关闭不会关闭已有浏览器或页面。",
        "browser_connection",
    ),
    "mainEndpoint": (
        "浏览器连接地址",
        "连接已配置的浏览器服务，沿用它的登录状态。这是自动化控制连接地址，不是网站地址。可先用“测试连接”验证当前输入；测试不会保存、自动启用、打开或接管页面。保存地址后再开启浏览器。网页由浏览器所在环境访问，本机地址不代表共用网络；不会另起宿主机浏览器。",
        "browser_connection",
    ),
    "mainDownloadHostPath": (
        "已有浏览器下载共享目录（OpenBear 侧）",
        "浏览器在远端或容器中时，填写 OpenBear 能读取的下载共享目录绝对路径，并同时填写下项浏览器侧对应路径。两项仅在浏览器与 OpenBear 确实使用同一文件系统时可以留空。每次新建控制连接使用独立子目录，不会重新请求下载链接。",
        "browser_connection",
    ),
    "mainDownloadBrowserPath": (
        "已有浏览器下载共享目录（浏览器侧）",
        "与上项指向同一共享目录，在浏览器所在机器或容器内的绝对路径，例如 /shared/downloads。两项成对配置，在下一次建立控制连接时生效；不修改已有挂载，不访问或清理其他下载目录。",
        "browser_connection",
    ),
    "connectTimeoutS": (
        "连接浏览器的等待时间",
        "建立浏览器控制连接时，最多等待多久，默认 10 秒。同时受本次调用的总等待预算限制，不会重新获得一份等待时间。",
        "browser_limits",
    ),
    "navigationTimeoutS": (
        "页面加载的默认等待时间",
        "打开或刷新网页时，默认等待页面主体就绪的时间，默认 45 秒。不会一直等广告、视频等请求全部结束；网站主体加载较慢时可适当调大。",
        "browser_limits",
    ),
    "actionTimeoutS": (
        "网页操作的默认等待时间",
        "点击、输入、查找元素等单次操作，默认最多等待多久，默认 20 秒。页面上的按钮或输入框出现较慢时可调大；连接、页面准备和排队都计入本次调用的总预算。",
        "browser_limits",
    ),
    "maxTimeoutS": (
        "单次操作的等待上限",
        "助手可为较慢的操作请求更长等待，但不能超过此值，默认 180 秒。它也会限制上面两个默认等待时间；例如上限为 60 秒时，即使页面加载默认设为 90 秒，也只等待 60 秒。",
        "browser_limits",
    ),
    "queueTimeoutS": (
        "同一页面的排队等待时间",
        "多个操作同时请求同一页面时，后面的操作最多排队多久，默认 10 秒。超过后取消的是尚未开始的操作，不会中断前一个；排队同时受本次调用的总预算限制，不会额外延长总等待时间。",
        "browser_limits",
    ),
    "snapshotMaxChars": (
        "单次读取的页面文字上限",
        "一次最多把多少字符的页面摘要或脚本结果交给助手，默认 12000 字符。调大可让助手一次看到更多内容，也会增加上下文占用；不限制网页本身的长度。",
        "browser_limits",
    ),
    "maxEventEntries": (
        "每页保留的诊断记录数",
        "每个页面分别保留最近多少条控制台日志、网络请求记录，默认各 200 条。超出后淘汰最早记录；通常无需修改，排查复杂网页问题时可调大。",
        "browser_limits",
    ),
    "maxBodyBytes": (
        "网页请求内容的读取上限",
        "检查网页网络请求时，单份返回内容最多读取多大，默认 2 MB。超过后不返回这份内容；不限制网页实际加载的数据量，也不是下载文件的大小限制。",
        "browser_limits",
    ),
    "maxArtifactBytes": (
        "浏览器文件的大小上限",
        "限制单个截图、下载文件、保存的网络内容，以及一次上传的文件总大小，默认 32 MB。处理较大文件时可调大；网络内容同时受上一项读取上限约束。",
        "browser_limits",
    ),
    "maxPagesPerOwner": (
        "每个会话可打开的页面数量",
        "限制同一会话或子任务通过工具新建的页面数，默认 12 页。达到上限后需要先关闭不用的页面，不会自动替你关闭已有页面。",
        "browser_limits",
    ),
    "autoReconnect": (
        "连接断开后自动重连",
        "默认开启。控制连接意外断开后，后续调用可尝试重新建立连接；不会重新点击按钮、重复提交表单，也不会自动关闭网页或重启浏览器。连续初始化失败三次后停止自动尝试。",
        "browser_recovery",
    ),
    "recoveryCooldownS": (
        "重连尝试的最短间隔",
        "同一个浏览器两次恢复连接的尝试之间至少间隔多久，默认 5 秒，避免故障时连续尝试。通常无需修改；不代表系统每隔这段时间就一定执行重连。",
        "browser_recovery",
    ),
    "mainRestartUrl": (
        "已有浏览器的重启管理地址（可选）",
        "通常留空。只有你使用的浏览器管理服务提供“停止指定浏览器、保留登录资料”的接口时，才填写它的 HTTP POST 地址；不是普通网页地址，也不是前面的调试连接地址。助手显式请求重启且你确认后才调用，不用于自动重连。",
        "browser_recovery",
    ),
    "allowEvaluate": (
        "允许助手在网页中运行脚本",
        "默认开启，方便读取复杂页面或完成普通点击无法完成的操作。脚本只在网页中运行，不是服务器命令，但可能修改网页内容或发送请求；关闭后助手仍可使用常规点击、输入和截图。",
        "browser_recovery",
    ),
    "agentAccess": (
        "允许子任务使用浏览器",
        "默认关闭。开启后，主助手可把浏览器能力分配给子任务；子任务只能操作自己的页面，不能代替你确认关闭或重启等恢复操作。",
        "browser_recovery",
    ),
}

DISPLAY = {
    "maxBodyBytes": ("MB", 1024 * 1024),
    "maxArtifactBytes": ("MB", 1024 * 1024),
    "snapshotMaxChars": ("字符", 1),
    "maxEventEntries": ("条", 1),
    "maxPagesPerOwner": ("页", 1),
}


def build_specs(make):
    fields = {field.alias or name: field for name, field in BrowserConfig.model_fields.items()}
    result = {}
    for alias, (title, desc, group) in LABELS.items():
        field = fields[alias]
        default = field.default
        kind = (
            "bool"
            if isinstance(default, bool)
            else "int"
            if isinstance(default, int)
            else "float"
            if isinstance(default, float)
            else "str"
        )
        bounds = {}
        for metadata in field.metadata:
            if hasattr(metadata, "ge"):
                bounds["min_value"] = metadata.ge
            if hasattr(metadata, "le"):
                bounds["max_value"] = metadata.le
        unit, scale = DISPLAY.get(alias, ("秒" if alias.endswith("S") else "", 1))
        path = "browser." + alias
        result[path] = make(
            path,
            title,
            desc,
            kind,
            group,
            "下一轮生效",
            unit=unit,
            display_scale=scale,
            **bounds,
        )
    return result
