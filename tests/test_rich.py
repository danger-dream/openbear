from __future__ import annotations

from app.telegram_ui import USE_RICH_MESSAGES, _preserve_rich_line_breaks, split_rich_html


def test_rich_line_breaks_become_br():
    out = _preserve_rich_line_breaks("a\nb\n\nc")
    assert out == "a<br>b<br><br>c"


def test_rich_line_breaks_preserve_code_blocks():
    src = "before\n<pre><code>a\nb</code></pre>\nafter"
    out = _preserve_rich_line_breaks(src)
    assert "before<br>" in out
    assert "a\nb" in out
    assert "</pre><br>after" in out


def test_split_rich_html_preserves_breaks_for_current_transport():
    chunks = split_rich_html("标题\n• 第一项\n• 第二项")
    expected = "标题<br>• 第一项<br>• 第二项" if USE_RICH_MESSAGES else "标题\n• 第一项\n• 第二项"
    assert chunks == [expected]


class _NoRichBot:
    def __init__(self):
        self.sent: list[str] = []
        self.reply_parameters = []
        self.mid = 1

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self.reply_parameters.append(kwargs.get("reply_parameters"))
        self.mid += 1
        return type("M", (), {"message_id": self.mid})()


async def test_send_rich_fallback_converts_br_back_to_newlines():
    from app.telegram_ui import send_rich

    bot = _NoRichBot()
    await send_rich(bot, 123, "第一行<br><br><b>第二行</b>")

    assert bot.sent == ["第一行\n\n<b>第二行</b>"]
    assert "<br>" not in bot.sent[0]
