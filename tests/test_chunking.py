"""HTML 分页测试 —— 重点验证代码块跨页标签平衡。"""
from __future__ import annotations

from app.html_chunking import split_html_chunks


def test_short_no_split():
    assert split_html_chunks("hello", 4000) == ["hello"]


def test_plain_text_split():
    text = "A" * 9000
    chunks = split_html_chunks(text, 4000)
    assert len(chunks) >= 3
    assert all(len(c) <= 4000 for c in chunks)
    # 内容无损
    assert "".join(chunks) == text


def test_code_block_across_boundary():
    """★ 关键：代码块正好跨 4000 边界 → 每块都补全 <pre><code>/</code></pre>。"""
    code = "x" * 5000
    html = f"<pre><code>{code}</code></pre>"
    chunks = split_html_chunks(html, 4000)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 4000
        # 每块都必须是闭合的代码块
        assert c.count("<pre>") == c.count("</pre>")
        assert c.count("<code>") == c.count("</code>")
        # 每块都以代码块开头、代码块结尾
        assert c.startswith("<pre><code>")
        assert c.endswith("</code></pre>")
    # 代码内容无损还原（去掉标签后）
    merged = "".join(c.replace("<pre><code>", "").replace("</code></pre>", "") for c in chunks)
    assert merged == code


def test_bold_across_boundary():
    html = "<b>" + ("word " * 1000) + "</b>"
    chunks = split_html_chunks(html, 1000)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.count("<b>") == c.count("</b>")


def test_entity_not_split():
    """HTML 实体 &amp; 不能从中间切断。"""
    # 构造一个实体正好落在边界附近的场景
    html = "A" * 3998 + "&amp;" + "B" * 100
    chunks = split_html_chunks(html, 4000)
    # 任何块里不能出现残缺实体（& 后面紧跟非法）
    for c in chunks:
        # 不能以孤立的 & 或 &am 结尾
        assert not c.endswith("&")
        assert not c.endswith("&am")
        assert not c.endswith("&amp")


def test_nested_tags_across_boundary():
    """嵌套标签跨页：blockquote 里有 code。"""
    html = "<blockquote><code>" + ("y" * 4000) + "</code></blockquote>"
    chunks = split_html_chunks(html, 2000)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.count("<blockquote>") == c.count("</blockquote>")
        assert c.count("<code>") == c.count("</code>")


def test_self_closing_br():
    html = "line1<br>line2<br>" + ("z" * 5000)
    chunks = split_html_chunks(html, 3000)
    assert all(len(c) <= 3000 for c in chunks)
    # br 不应导致未闭合错误（不进 open stack）


def test_content_preserved_overall():
    """整体文字内容（去标签）无损。"""
    html = "<b>开头</b> " + ("中文内容测试 " * 800) + " <i>结尾</i>"
    chunks = split_html_chunks(html, 1500)
    import re

    def strip(s: str) -> str:
        return re.sub(r"</?[a-z]+>", "", s)

    merged = "".join(strip(c) for c in chunks)
    original = strip(html)
    assert merged == original
