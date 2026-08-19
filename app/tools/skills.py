"""Skills 加载、过滤、提示词注入 —— 对齐 OpenClaw 三阶段。

阶段 1 · 加载: 扫 skills/*/SKILL.md，解析 YAML frontmatter（含 metadata.openclaw 块）
阶段 2 · 过滤: 依赖检查（requires.bins / requires.env）、enabled 开关、安全校验
阶段 3 · 注入: 组装 <available_skills> XML（含引导语、XML escape、路径压缩、体积控制）
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.logging import get_logger

log = get_logger("tools.skills")

# ── 阶段 1: 数据结构 + 加载 ──────────────────────────────────────────

# frontmatter 里 metadata.openclaw 能识别的字段
_OPENCLAW_STRING_FIELDS = {"primaryEnv", "emoji", "homepage", "skillKey"}


@dataclass(slots=True)
class SkillRequires:
    """skill 声明的运行时依赖。"""
    bins: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SkillMetadata:
    """从 metadata.openclaw 块解析出的结构化元数据。"""
    primary_env: str | None = None
    requires: SkillRequires = field(default_factory=SkillRequires)
    emoji: str | None = None
    homepage: str | None = None
    skill_key: str | None = None
    always: bool | None = None


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    location: str       # SKILL.md 绝对路径
    base_dir: str       # skill 目录绝对路径
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    enabled: bool = True


# ── frontmatter 解析 ──────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    """Parse a SKILL.md YAML frontmatter mapping with the safe loader.

    Older local skills used unquoted top-level descriptions containing ``: ``,
    which strict YAML rejects. Quote only legacy scalar name/description values;
    nested metadata and block scalars still go through normal YAML semantics.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    end = next((index for index, line in enumerate(lines[1:], start=1) if line in {"---", "..."}), None)
    if end is None:
        return {}
    frontmatter_lines = lines[1:end]
    for index, line in enumerate(frontmatter_lines):
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, separator, raw_value = line.partition(":")
        value = raw_value.strip()
        if key.strip() not in {"name", "description"} or not value:
            continue
        if value.startswith(("\"", "'", "|", ">", "[", "{")):
            continue
        frontmatter_lines[index] = f"{key}{separator} {json.dumps(value, ensure_ascii=False)}"
    loaded = yaml.safe_load("\n".join(frontmatter_lines))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return loaded


def _extract_metadata(fm: dict) -> SkillMetadata:
    """从 frontmatter 提取 metadata.openclaw 块。"""
    meta = SkillMetadata()

    # 兼容两种写法: metadata.openclaw.xxx 和顶层 credentials
    oc = {}
    if isinstance(fm.get("metadata"), dict):
        oc = fm["metadata"].get("openclaw", {})
        if not isinstance(oc, dict):
            oc = {}

    meta.primary_env = _str_or_none(oc.get("primaryEnv"))
    meta.emoji = _str_or_none(oc.get("emoji"))
    meta.homepage = _str_or_none(oc.get("homepage"))
    meta.skill_key = _str_or_none(oc.get("skillKey"))

    if isinstance(oc.get("always"), bool):
        meta.always = oc["always"]

    # requires 可以在 metadata.openclaw.requires 或顶层 (inline JSON)
    req_raw = oc.get("requires") or fm.get("requires")
    if isinstance(req_raw, dict):
        bins = req_raw.get("bins") or req_raw.get("bin") or []
        envs = req_raw.get("env") or []
        if isinstance(bins, str):
            bins = [bins]
        if isinstance(envs, str):
            envs = [envs]
        meta.requires = SkillRequires(
            bins=[b for b in bins if isinstance(b, str) and b.strip()],
            env=[e for e in envs if isinstance(e, str) and e.strip()],
        )

    # credentials 块的 name 字段也算 env 依赖（兼容 anysearch 等 skill）
    creds = fm.get("credentials")
    if isinstance(creds, list):
        for cred in creds:
            if isinstance(cred, dict):
                cname = cred.get("name", "")
                if cname and isinstance(cname, str) and cname not in meta.requires.env:
                    # 只有 required=true 的才作为硬依赖
                    if cred.get("required") is True:
                        meta.requires.env.append(cname)

    return meta


def _str_or_none(v) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


# SKILL.md 文件大小上限 (256KB，防止恶意/误放大文件)
_MAX_SKILL_FILE_BYTES = 256 * 1024


def _is_safe_skill_path(path: Path, root: Path) -> bool:
    """安全校验：目录和文件的真实路径都必须位于 skills root 内。"""
    try:
        real_root = root.resolve(strict=True)
        real_path = path.resolve(strict=True)
        real_path.relative_to(real_root)
        return True
    except (ValueError, OSError):
        return False


def load_skills(skills_dir: str) -> list[Skill]:
    """阶段 1：扫描 skills_dir 下所有子目录，解析 SKILL.md，返回 Skill 列表。"""
    root = Path(skills_dir).expanduser().resolve()
    if not root.is_dir():
        return []

    skills: list[Skill] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith(".") or sub.name == "node_modules":
            continue

        # 安全校验：目录和 SKILL.md 都不能通过符号链接逃逸 root。
        if not _is_safe_skill_path(sub, root):
            log.warning("skill 目录安全检查失败，跳过", 目录=str(sub))
            continue

        md = sub / "SKILL.md"
        if not md.exists():
            continue
        if not _is_safe_skill_path(md, root) or not md.is_file():
            log.warning("SKILL.md 安全检查失败，跳过", 文件=str(md))
            continue

        # 文件大小检查
        try:
            size = md.stat().st_size
            if size > _MAX_SKILL_FILE_BYTES:
                log.warning("SKILL.md 超过大小上限，跳过",
                            skill=sub.name, 大小=size, 上限=_MAX_SKILL_FILE_BYTES)
                continue
        except OSError:
            continue

        try:
            fm = _parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("解析 SKILL.md 失败", 目录=str(sub), 错误=str(e)[:100])
            continue

        name = fm.get("name") or sub.name
        desc = fm.get("description", "")
        if not isinstance(name, str) or not isinstance(desc, str) or not name.strip() or not desc.strip():
            log.warning("skill 的 name/description 必须是非空字符串，跳过", 目录=str(sub))
            continue

        metadata = _extract_metadata(fm)
        location = str(md.resolve())
        base_dir = str(sub.resolve())

        skills.append(Skill(
            name=name.strip(),
            description=desc.strip(),
            location=location,
            base_dir=base_dir,
            metadata=metadata,
        ))

    log.info("阶段1·加载完成", 数量=len(skills),
             名称=[s.name for s in skills] or "无")
    return skills


# ── 阶段 2: 过滤 ──────────────────────────────────────────────────

def _check_bin(name: str) -> bool:
    """检查系统 PATH 里是否有指定二进制。"""
    return shutil.which(name) is not None


def _check_env(name: str) -> bool:
    """检查环境变量是否已设置（非空）。"""
    return bool(os.environ.get(name, "").strip())


def _check_env_or_dotenv(name: str, skill_base_dir: str) -> bool:
    """检查环境变量或 skill 目录 .env 里是否有该 key。"""
    if _check_env(name):
        return True
    # 检查 skill 目录的 .env
    for env_path in [
        os.path.join(skill_base_dir, ".env"),
        os.path.join(skill_base_dir, "scripts", ".env"),
    ]:
        if os.path.isfile(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        if key.strip() == name and value.strip():
                            return True
            except OSError:
                pass
    return False


@dataclass(slots=True)
class FilterResult:
    """过滤结果。"""
    included: list[Skill]
    excluded: list[tuple[Skill, str]]  # (skill, reason)


def filter_skills(
    skills: list[Skill],
    *,
    disabled_names: set[str] | None = None,
) -> FilterResult:
    """阶段 2：依赖检查 + enabled 开关过滤。

    - requires.bins: 检查 PATH 里有没有对应二进制
    - requires.env: 检查环境变量或 .env 里有没有对应 key
    - disabled_names: 显式禁用的 skill 名称集合
    - metadata.always=True: 跳过依赖检查，始终包含
    """
    included: list[Skill] = []
    excluded: list[tuple[Skill, str]] = []

    for skill in skills:
        # 显式禁用
        if disabled_names and skill.name in disabled_names:
            excluded.append((skill, "disabled"))
            continue

        if not skill.enabled:
            excluded.append((skill, "disabled"))
            continue

        # always=True 跳过依赖检查
        if skill.metadata.always is True:
            included.append(skill)
            continue

        # 二进制依赖检查
        missing_bins = [b for b in skill.metadata.requires.bins if not _check_bin(b)]
        if missing_bins:
            excluded.append((skill, f"missing bins: {', '.join(missing_bins)}"))
            continue

        # 环境变量依赖检查（同时检查 .env）
        missing_env = [
            e for e in skill.metadata.requires.env
            if not _check_env_or_dotenv(e, skill.base_dir)
        ]
        if missing_env:
            excluded.append((skill, f"missing env: {', '.join(missing_env)}"))
            continue

        included.append(skill)

    if excluded:
        log.info("阶段2·过滤排除",
                 排除数=len(excluded),
                 详情=[(s.name, r) for s, r in excluded])
    log.info("阶段2·过滤完成", 通过数=len(included),
             名称=[s.name for s in included] or "无")
    return FilterResult(included=included, excluded=excluded)


# ── 阶段 3: 提示词注入 ────────────────────────────────────────────

def _escape_xml(s: str) -> str:
    """XML 特殊字符转义。"""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _compact_home_path(filepath: str) -> str:
    """把 home 目录前缀替换成 ~，省 token。"""
    home = os.path.expanduser("~")
    if not home or home == "~":
        return filepath
    # 确保 home 以 / 结尾再匹配
    prefix = home if home.endswith(os.sep) else home + os.sep
    if filepath.startswith(prefix):
        return "~/" + filepath[len(prefix):]
    # 也试 resolve 后的 home
    try:
        real_home = str(Path(home).resolve())
        real_prefix = real_home if real_home.endswith(os.sep) else real_home + os.sep
        if filepath.startswith(real_prefix):
            return "~/" + filepath[len(real_prefix):]
    except OSError:
        pass
    return filepath


# 引导语（对齐 OpenClaw skill-contract.ts）
_SKILLS_PREAMBLE = (
    "The following skills provide specialized instructions for specific tasks.\n"
    "Use the read tool to load a skill's file when the task matches its description.\n"
    "When a skill file references a relative path, resolve it against the skill "
    "directory (parent of SKILL.md / dirname of the path) and use that absolute "
    "path in tool commands."
)

# 体积上限
_MAX_SKILLS_PROMPT_CHARS = 12000   # full 模式上限
_MAX_SKILLS_COMPACT_CHARS = 6000   # compact 模式上限（去掉 description）


def _render_full(skills: list[Skill]) -> str:
    """完整格式：name + description + location。"""
    lines = [_SKILLS_PREAMBLE, "", "<available_skills>"]
    for s in skills:
        loc = _compact_home_path(s.location)
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(s.name)}</name>")
        lines.append(f"    <description>{_escape_xml(s.description)}</description>")
        lines.append(f"    <location>{_escape_xml(loc)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_compact(skills: list[Skill]) -> str:
    """紧凑格式：只有 name + location（省 token，保持 skill 感知）。"""
    lines = [_SKILLS_PREAMBLE, "", "<available_skills>"]
    for s in skills:
        loc = _compact_home_path(s.location)
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(s.name)}</name>")
        lines.append(f"    <location>{_escape_xml(loc)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def render_skills_block(skills: list[Skill]) -> str:
    """阶段 3：组装 <available_skills> 注入块。

    策略（对齐 OpenClaw applySkillsPromptLimits）:
    1. 先尝试 full 格式（含 description）
    2. 超预算 → 降级 compact 格式（去掉 description）
    3. compact 还超 → 二分搜索最大前缀
    4. 空列表 → 返回空字符串
    """
    if not skills:
        return ""

    # 1. 尝试 full
    full = _render_full(skills)
    if len(full) <= _MAX_SKILLS_PROMPT_CHARS:
        return full

    # 2. 降级 compact
    compact = _render_compact(skills)
    if len(compact) <= _MAX_SKILLS_COMPACT_CHARS:
        warning = (
            f"⚠️ {len(skills)} skills loaded; descriptions omitted to save context space. "
            "Read the SKILL.md for details when a skill matches.\n\n"
        )
        return warning + compact

    # 3. 二分截断
    lo, hi = 0, len(skills)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_render_compact(skills[:mid])) <= _MAX_SKILLS_COMPACT_CHARS:
            lo = mid
        else:
            hi = mid - 1

    truncated = skills[:lo]
    omitted = len(skills) - lo
    warning = (
        f"⚠️ {len(skills)} skills available, showing {lo} (omitted {omitted} to fit context). "
        "Read the SKILL.md for details when a skill matches.\n\n"
    )
    return warning + _render_compact(truncated)
