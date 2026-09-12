from __future__ import annotations

import pytest

from app.settings.specs import GROUPS, SPECS, WEB_DOMAINS, get_spec, group_specs


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
    spec = get_spec("contextManagement.retainRatio")
    assert spec is not None and spec.parse("0.15") == 0.15
    assert len(group_specs("compaction")) == 10
    with pytest.raises(ValueError):
        spec.parse("0")


def test_agent_and_tool_sections_are_available():
    assert [s.title for s in group_specs("agent")] == [
        "单轮最长运行时间",
        "连续无进展轮数",
    ]
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
        "Bash 落盘上限",
    ]
    assert [s.title for s in group_specs("files")] == [
        "Read 默认行数上限",
        "Read 输出字节上限",
        "Read 单行字节上限",
        "Read 状态缓存上限",
    ]
    assert get_spec("agent.retryJitterRatio") is None
    assert "阶梯" in get_spec("agent.retryBackoffS").desc
    assert "600 秒" in get_spec("agent.retryMaxDelayS").desc
    mcp = get_spec("mcp.installDir")
    assert mcp is not None
    assert mcp.default_value == "./mcp-servers"
    assert [s.title for s in group_specs("mcp")] == ["MCP 安装目录"]
    assert [s.title for s in group_specs("interface")] == [
        "显示本轮统计",
    ]
