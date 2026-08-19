"""skills 三阶段测试：加载 → 过滤 → 注入。"""
from __future__ import annotations

import os
import textwrap

from app.tools.skills import (
    Skill,
    SkillMetadata,
    SkillRequires,
    _compact_home_path,
    _escape_xml,
    filter_skills,
    load_skills,
    render_skills_block,
)

# ── helpers ──────────────────────────────────────────────────────

def _make_skill(root, name, desc, extra_fm=""):
    d = root / name
    d.mkdir()
    fm = f"---\nname: {name}\ndescription: {desc}\n{extra_fm}---\n\n# {name}\n正文内容"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")


def _make_skill_full(root, name, content):
    d = root / name
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def _quick_skill(name, desc="test skill", **kwargs) -> Skill:
    return Skill(
        name=name, description=desc,
        location=f"/skills/{name}/SKILL.md",
        base_dir=f"/skills/{name}",
        **kwargs,
    )


# ── 阶段 1: 加载 ────────────────────────────────────────────────

def test_load_and_render(tmp_path):
    _make_skill(tmp_path, "weather", "查天气用")
    _make_skill(tmp_path, "search", "搜索用")
    skills = load_skills(str(tmp_path))
    assert len(skills) == 2
    names = {s.name for s in skills}
    assert names == {"weather", "search"}
    block = render_skills_block(skills)
    assert "<available_skills>" in block
    assert "<name>weather</name>" in block
    assert "查天气用" in block
    assert "SKILL.md" in block


def test_empty_dir(tmp_path):
    assert load_skills(str(tmp_path)) == []
    assert render_skills_block([]) == ""


def test_multiline_description(tmp_path):
    d = tmp_path / "multi"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: multi\ndescription: >-\n  Use when: 第一行\n  第二行续\n---\n正文", encoding="utf-8")
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].description == "Use when: 第一行 第二行续"


def test_legacy_unquoted_description_with_colon(tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: legacy\ndescription: Use when: legacy trigger\n---\n", encoding="utf-8"
    )
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].description == "Use when: legacy trigger"


def test_invalid_frontmatter_type_skips_only_bad_skill(tmp_path):
    _make_skill(tmp_path, "good", "valid")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: [123]\ndescription: invalid name type\n---\n", encoding="utf-8")
    skills = load_skills(str(tmp_path))
    assert [skill.name for skill in skills] == ["good"]


def test_missing_skill_md_ignored(tmp_path):
    (tmp_path / "notaskill").mkdir()
    (tmp_path / "notaskill" / "readme.txt").write_text("x")
    assert load_skills(str(tmp_path)) == []


def test_hidden_dir_ignored(tmp_path):
    _make_skill(tmp_path, ".hidden", "hidden skill")
    assert load_skills(str(tmp_path)) == []


def test_no_description_skipped(tmp_path):
    d = tmp_path / "nodesc"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: nodesc\n---\n# no desc", encoding="utf-8")
    assert load_skills(str(tmp_path)) == []


def test_oversized_skill_skipped(tmp_path):
    d = tmp_path / "huge"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: huge\ndescription: big\n---\n" + "x" * 300_000,
                                encoding="utf-8")
    assert load_skills(str(tmp_path)) == []


def test_metadata_requires_bins(tmp_path):
    _make_skill_full(tmp_path, "tmux", textwrap.dedent("""\
        ---
        name: tmux
        description: tmux controller
        metadata:
          openclaw:
            requires: { "bins": ["tmux"] }
        ---
        # tmux
    """))
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].metadata.requires.bins == ["tmux"]


def test_base_dir_populated(tmp_path):
    _make_skill(tmp_path, "test-skill", "test desc")
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].base_dir == str((tmp_path / "test-skill").resolve())


# ── 阶段 2: 过滤 ────────────────────────────────────────────────

def test_filter_passes_all_by_default():
    skills = [_quick_skill("a"), _quick_skill("b")]
    result = filter_skills(skills)
    assert len(result.included) == 2
    assert len(result.excluded) == 0


def test_filter_disabled_names():
    skills = [_quick_skill("a"), _quick_skill("b")]
    result = filter_skills(skills, disabled_names={"b"})
    assert [s.name for s in result.included] == ["a"]
    assert result.excluded[0][1] == "disabled"


def test_filter_disabled_flag():
    s = _quick_skill("x", enabled=False)
    result = filter_skills([s])
    assert len(result.included) == 0


def test_filter_missing_bin():
    s = _quick_skill("needgit",
                     metadata=SkillMetadata(requires=SkillRequires(bins=["__nonexistent_bin_xyz__"])))
    result = filter_skills([s])
    assert len(result.included) == 0
    assert "missing bins" in result.excluded[0][1]


def test_filter_present_bin():
    # 'python3' should exist in test env
    s = _quick_skill("needpy",
                     metadata=SkillMetadata(requires=SkillRequires(bins=["python3"])))
    result = filter_skills([s])
    assert len(result.included) == 1


def test_filter_always_skips_checks():
    s = _quick_skill("force",
                     metadata=SkillMetadata(
                         always=True,
                         requires=SkillRequires(bins=["__nonexistent__"]),
                     ))
    result = filter_skills([s])
    assert len(result.included) == 1


def test_filter_env_from_dotenv(tmp_path):
    """skill 目录有 .env 文件时，env 依赖检查应通过。"""
    d = tmp_path / "myskill"
    d.mkdir()
    (d / ".env").write_text("MY_SECRET_KEY=abc123\n")
    s = Skill(
        name="myskill", description="test",
        location=str(d / "SKILL.md"), base_dir=str(d),
        metadata=SkillMetadata(requires=SkillRequires(env=["MY_SECRET_KEY"])),
    )
    # 确保进程 env 里没有
    os.environ.pop("MY_SECRET_KEY", None)
    result = filter_skills([s])
    assert len(result.included) == 1


# ── 阶段 3: 注入 ────────────────────────────────────────────────

def test_xml_escape():
    assert _escape_xml("a<b>c&d") == "a&lt;b&gt;c&amp;d"
    assert _escape_xml('"hello\'') == "&quot;hello&apos;"


def test_compact_home_path():
    home = os.path.expanduser("~")
    assert _compact_home_path(f"{home}/skills/x/SKILL.md") == "~/skills/x/SKILL.md"
    assert _compact_home_path("/opt/other/SKILL.md") == "/opt/other/SKILL.md"


def test_render_has_preamble():
    skills = [_quick_skill("weather", "查天气")]
    block = render_skills_block(skills)
    assert "Use the read tool to load" in block
    assert "resolve it against the skill directory" in block


def test_render_xml_escaped():
    s = _quick_skill("test<skill", "desc with <b>html</b> & 'quotes'")
    block = render_skills_block([s])
    assert "test&lt;skill" in block
    assert "&lt;b&gt;html&lt;/b&gt;" in block
    assert "&amp;" in block


def test_render_compact_fallback():
    """超长 description 触发 compact 降级。"""
    skills = [_quick_skill(f"s{i}", "x" * 800) for i in range(20)]
    block = render_skills_block(skills)
    # compact 模式不含 description
    assert "descriptions omitted" in block or "<description>" not in block


def test_render_truncation():
    """极端数量触发二分截断。"""
    skills = [_quick_skill(f"skill-with-long-name-{i:04d}") for i in range(200)]
    block = render_skills_block(skills)
    assert "omitted" in block
    # 输出不应超过 compact 上限 + warning 头部
    assert len(block) < 8000


# ── 阶段 1 补充: 真实 frontmatter 格式 ────────────────────────────

def test_json_metadata_block(tmp_path):
    """metadata 为多行 JSON 块时（tmux/weather 格式）能正确解析 requires.bins。"""
    _make_skill_full(tmp_path, "tmux-like", textwrap.dedent('''\
        ---
        name: tmux-like
        description: test json metadata
        metadata:
          {
            "openclaw":
              {
                "emoji": "🧵",
                "requires": { "bins": ["tmux"] },
              },
          }
        ---
        # test
    '''))
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].metadata.requires.bins == ["tmux"]
    assert skills[0].metadata.emoji == "🧵"


def test_inline_json_requires(tmp_path):
    """requires 在顶层以 inline JSON 写时也能解析。"""
    _make_skill_full(tmp_path, "inline", textwrap.dedent('''\
        ---
        name: inline
        description: test inline requires
        requires: { "bins": ["curl", "jq"], "env": ["MY_KEY"] }
        ---
        # test
    '''))
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].metadata.requires.bins == ["curl", "jq"]
    assert skills[0].metadata.requires.env == ["MY_KEY"]


def test_credentials_required_true(tmp_path):
    """credentials 块的 required=true 项被当作 env 依赖。"""
    _make_skill_full(tmp_path, "creds", textwrap.dedent('''\
        ---
        name: creds
        description: test credentials
        credentials:
          - name: MY_API_KEY
            required: true
            description: needed
        ---
        # test
    '''))
    skills = load_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].metadata.requires.env == ["MY_API_KEY"]


def test_symlink_escape(tmp_path):
    """符号链接逃逸到 root 外的 skill 被拒绝。"""
    # 创建 root 外的目录
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_md = outside / "SKILL.md"
    outside_md.write_text(
        "---\nname: evil\ndescription: escaped\n---\n", encoding="utf-8")
    # 在 skills root 里创建目录链接和文件链接，两种都必须拒绝。
    root = tmp_path / "skills"
    root.mkdir()
    (root / "evil-dir-link").symlink_to(outside)
    file_link_dir = root / "evil-file-link"
    file_link_dir.mkdir()
    (file_link_dir / "SKILL.md").symlink_to(outside_md)
    skills = load_skills(str(root))
    assert len(skills) == 0


# ── 阶段 2 补充: .env 在 scripts/ 子目录 ────────────────────────────

def test_filter_env_from_scripts_dotenv(tmp_path):
    """skill 的 scripts/.env 也能被检测到。"""
    d = tmp_path / "myskill"
    d.mkdir()
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / ".env").write_text("DEEP_KEY=yes\n")
    s = Skill(
        name="myskill", description="test",
        location=str(d / "SKILL.md"), base_dir=str(d),
        metadata=SkillMetadata(requires=SkillRequires(env=["DEEP_KEY"])),
    )
    os.environ.pop("DEEP_KEY", None)
    result = filter_skills([s])
    assert len(result.included) == 1


def test_filter_env_missing_entirely(tmp_path):
    """env 依赖既不在环境也不在 .env 时被排除。"""
    d = tmp_path / "myskill"
    d.mkdir()
    s = Skill(
        name="myskill", description="test",
        location=str(d / "SKILL.md"), base_dir=str(d),
        metadata=SkillMetadata(requires=SkillRequires(env=["TOTALLY_MISSING_VAR_XYZ"])),
    )
    os.environ.pop("TOTALLY_MISSING_VAR_XYZ", None)
    result = filter_skills([s])
    assert len(result.included) == 0
    assert "missing env" in result.excluded[0][1]


# ── 端到端: 真实 skills 目录 ────────────────────────────────────────

def test_real_skills_load():
    """确保真实 skills 目录能完整加载（不崩溃）。"""
    import pathlib
    real_dir = pathlib.Path("/opt/src-space/openbear/skills")
    if not real_dir.is_dir():
        return  # CI 环境可能没有
    skills = load_skills(str(real_dir))
    assert len(skills) > 0
    # 每个 skill 都有 name、description、location、base_dir
    for s in skills:
        assert s.name
        assert s.description
        assert s.location.endswith("SKILL.md")
        assert os.path.isabs(s.base_dir)


def test_real_skills_filter():
    """真实 skills 过滤不崩溃，tmux/weather 的 bins 依赖能被检测到。"""
    import pathlib
    real_dir = pathlib.Path("/opt/src-space/openbear/skills")
    if not real_dir.is_dir():
        return
    skills = load_skills(str(real_dir))
    result = filter_skills(skills)
    # tmux 和 weather 声明了 bins 依赖，在本机应该都装了
    names = {s.name for s in result.included}
    assert "tmux" in names  # tmux 本机有
    assert "weather" in names  # curl 本机有


def test_real_skills_render():
    """真实 skills 渲染不崩溃，输出合法 XML。"""
    import pathlib
    real_dir = pathlib.Path("/opt/src-space/openbear/skills")
    if not real_dir.is_dir():
        return
    skills = load_skills(str(real_dir))
    result = filter_skills(skills)
    block = render_skills_block(result.included)
    assert "<available_skills>" in block
    assert "</available_skills>" in block
    assert "Use the read tool" in block
    # 确保 XML 标签内的 & 都已转义
    # （preamble 是纯文本不在标签内，不需检查）
