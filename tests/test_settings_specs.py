from __future__ import annotations

import pytest

from app.conversation_titles import DEFAULT_NAMING_PROMPT
from app.settings.specs import GROUPS, SPECS, WEB_DOMAINS, get_spec, group_specs


def test_browser_units_preserve_storage_and_telegram_input():
    from app.admin.settings import parse_setting_value, serialize_spec
    from app.bot.admin import _fmt_value, _input_hint

    spec = get_spec("browser.maxBodyBytes")
    assert spec is not None
    assert serialize_spec(spec)["displayScale"] == 1024 * 1024
    assert spec.display_value(2097152) == 2
    assert spec.parse_display("1.5") == 1572864
    assert parse_setting_value(spec.path, 1572864) == 1572864
    assert _fmt_value(2097152, spec) == "2MB"
    assert "单位：MB" in _input_hint(spec, 2097152)
    with pytest.raises(ValueError, match="16 MB"):
        spec.parse_display("17")
    for invalid in ("nan", "inf", "", "-1"):
        with pytest.raises(ValueError):
            spec.parse_display(invalid)


@pytest.mark.parametrize('path', [
    'tools.bashSpoolMaxBytes', 'tools.fileReadOutputBytes', 'tools.fileReadMaxLineBytes',
    'webhooks.ingress.maxBodyBytes', 'webhooks.queue.maxBytes',
    'webhooks.scripts.maxStdoutBytes', 'webhooks.scripts.maxStderrBytes',
    'webhooks.batching.maxBatchBytes', 'webhooks.telemetry.maxBusinessRecordBytes',
])
def test_capacity_settings_use_mb_without_changing_storage(path):
    from app.admin.settings import parse_setting_value, serialize_spec
    spec = get_spec(path)
    payload = serialize_spec(spec)
    assert payload['unit'] == 'MB' and payload['displayScale'] == 1024 * 1024
    assert spec.parse_display('1.5') == 1572864
    assert spec.display_value(1572864) == 1.5
    assert parse_setting_value(path, 1572864) == 1572864


def test_webhooks_navigation_follows_web_and_security():
    from app.admin.settings import settings_specs_payload
    keys = [item['key'] for item in settings_specs_payload()['domains']]
    assert keys.index('webhooks') == keys.index('web') + 1


def test_notifications_have_a_dedicated_web_domain_without_changing_settings():
    from app.admin.settings import settings_specs_payload
    domains = {item['key']: item for item in settings_specs_payload()['domains']}
    assert domains['notifications']['title'] == '通知'
    assert [section['key'] for section in domains['notifications']['sections']] == ['web_notifications']
    assert [section['key'] for section in domains['web']['sections']] == ['web']
    assert domains['notifications']['sections'][0]['paths'] == GROUPS['web_notifications'][1]


def test_browser_settings_mention_server_nodes_only_on_a_controller():
    from app.admin.settings import settings_specs_payload
    controller, managed = settings_specs_payload()['specs'], settings_specs_payload(managed=True)['specs']
    for path in ('browser.enabled', 'browser.agentAccess'):
        assert '服务器节点的浏览器' in controller[path]['desc'] and '服务器节点' not in managed[path]['desc']
        assert controller[path]['desc'].startswith(managed[path]['desc'])
    assert set(controller) - set(managed) == {'features.instances', 'features.servers', 'updates.prerelease', 'updates.autoUpdateNodes'}
    assert {p for p in managed if controller[p] != managed[p]} == {'browser.enabled', 'browser.agentAccess'}


def test_bool_setting_parse_chinese_values():
    spec = get_spec("rath.enabled")
    assert spec is not None
    assert spec.parse("开启") is True
    assert spec.parse("关闭") is False


def test_number_setting_range_validation():
    spec = get_spec("tools.bashTimeoutS")
    assert spec is not None
    assert spec.parse("1000") == 1000
    with pytest.raises(ValueError):
        spec.parse("0")
    with pytest.raises(ValueError):
        spec.parse("90000")


def test_legacy_settings_are_not_web_editable():
    assert get_spec("agent.interruptOnNew") is None
    assert get_spec("ui.showThinking") is None
    assert get_spec("media.enabled") is None
    assert get_spec("media.keepDays") is None
    assert get_spec("ui.showTurnStats") is not None
    assert get_spec("ui.editThrottleMs") is None
    assert get_spec("session.idleArchiveMinutes") is None
    assert get_spec("web.enabled") is None
    for group in ["telegram", "session"]:
        with pytest.raises(KeyError):
            group_specs(group)


def test_setting_catalog_is_complete_and_unique():
    grouped_paths = [path for _title, paths in GROUPS.values() for path in paths]
    assert sorted(grouped_paths) == sorted(SPECS)
    assert len(grouped_paths) == len(set(grouped_paths))

    section_keys = [key for _title, _desc, keys in WEB_DOMAINS.values() for key in keys]
    assert sorted(section_keys) == sorted(GROUPS)
    assert len(section_keys) == len(set(section_keys))


def test_dual_strategy_settings_preserve_summary_and_remove_only_reminder():
    for key in ("agent.compactTimeoutS", "agent.compactPrompt", "agent.manualCompactMinPercent"):
        assert get_spec(key) is not None
    assert get_spec("agent.memoryReminderPrompt") is None
    assert get_spec("contextManagement.retainRatio") is None
    spec = get_spec("contextManagement.retainTokens")
    assert spec is not None and spec.parse("40000") == 40_000
    assert get_spec("contextManagement.dialogueTokens").parse("0") == 0
    assert len(group_specs("compaction")) == 12
    with pytest.raises(ValueError):
        spec.parse("10000")


def test_agent_and_tool_sections_are_available():
    assert [s.title for s in group_specs("agent")] == [
        "单轮最长运行时间",
        "连续无进展轮数",
        "命名模型",
        "命名单次超时",
        "命名失败重试次数",
        "命名会话最大获取轮数",
        "命名会话最大获取字符数",
        "会话命名提示词",
    ]
    assert "不计首次请求" in get_spec("agent.namingMaxRetries").desc
    assert get_spec("agent.namingPrompt").default_value == DEFAULT_NAMING_PROMPT
    assert [s.title for s in group_specs("retry")] == [
        "模型调用失败重试次数",
        "重试基础等待",
        "重试等待上限",
        "空回复补救次数",
        "只有思考无正文的补救次数",
    ]
    assert [s.title for s in group_specs("bash")] == [
        "Bash 默认超时",
        "Bash 最大超时",
        "Bash 输出回灌上限",
        "Bash 输出文件大小上限",
    ]
    assert [s.title for s in group_specs("files")] == [
        "Read 默认行数上限",
        "Read 返回内容大小上限",
        "Read 单行大小上限",
        "Read 状态缓存上限",
    ]
    assert get_spec("agent.retryJitterRatio") is None
    assert "阶梯" in get_spec("agent.retryBackoffS").desc
    assert "600 秒" in get_spec("agent.retryMaxDelayS").desc
    mcp = get_spec("mcp.installDir")
    assert mcp is not None
    assert mcp.default_value == "./mcp-servers"
    assert [s.title for s in group_specs("mcp")] == ["MCP 安装目录", "MCP 工具数量确认阈值", "MCP 定义体积确认阈值"]
    assert get_spec("mcp.toolCountThreshold").default_value == 30
    assert get_spec("mcp.definitionSizeThresholdKB").default_value == 64
    assert get_spec("mcp.toolCountThreshold").parse("0") == 0
    assert [s.title for s in group_specs("interface")] == [
        "显示本轮统计",
    ]


def test_update_switches_default_off_and_are_controller_settings():
    from app.admin.settings import parse_setting_value, settings_specs_payload
    from app.config import UpdatesConfig
    assert UpdatesConfig().prerelease is False and UpdatesConfig().auto_update_nodes is False
    assert UpdatesConfig.model_validate({'prerelease': True, 'autoUpdateNodes': True}).model_dump(by_alias=True) == {'prerelease': True, 'autoUpdateNodes': True}
    assert get_spec('updates.prerelease').title == '接收测试版' and get_spec('updates.autoUpdateNodes').kind == 'bool'
    assert parse_setting_value('updates.prerelease', '开启') is True
    domains = {d['key']: d for d in settings_specs_payload()['domains']}
    assert [s['key'] for s in domains['updates']['sections']] == ['updates']
    assert 'updates' not in {d['key'] for d in settings_specs_payload(managed=True)['domains']}
