"""Names in Telegram notices (server reminders, instance notices) as 「name」, sent as code: a name
may come from a node, an instance or the gateway, and Telegram would turn a bare URL in it into a link."""
from __future__ import annotations

import html
import re

# A quoted name with something Telegram could make a link of; quoted page labels (「续费」) are all Chinese and stay as they are.
_QUOTED = re.compile(r'「([^「」\n]*[!-~][^「」\n]*)」')


def telegram_html(text):
    """text as Telegram HTML with each 「name」 in <code>."""
    return _QUOTED.sub(r'「<code>\1</code>」', html.escape(text, quote=False))


def quoted(name):
    """「name」 for every name a notice text carries, from any module: corner brackets in the name
    become 『』 and line breaks spaces, so the quote cannot end early and telegram_html puts the
    whole name in one <code>."""
    return '「' + re.sub(r'[\r\n]+', ' ', str(name)).replace('「', '『').replace('」', '』') + '」'
