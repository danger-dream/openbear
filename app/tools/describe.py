"""The describe convention of action tools.

describe without params.action is an index: one line per action. describe with
params.action gives that action's contract and only the guide paragraphs marked
for it. An unknown action is an error that lists the actions.
"""
from __future__ import annotations

import re

_MARK = re.compile(r'^<!-- for: ([a-z_ ]+) -->[ \t]*\n', re.M)


def guide_sections(text: str) -> list[tuple[frozenset[str], str]]:
    """Paragraph groups of a guide, each for the actions named by the `<!-- for: a b -->` line above it."""
    parts = _MARK.split(text)
    if parts[0].strip():
        raise ValueError('every guide paragraph needs a <!-- for: actions --> line above it')
    return [(frozenset(names.split()), body.strip()) for names, body in zip(parts[1::2], parts[2::2])]


def guide_for(sections: list[tuple[frozenset[str], str]], action: str) -> str:
    return '\n\n'.join(body for names, body in sections if action in names)


def unknown_action(action, actions) -> dict:
    return {'error': 'unknown_action', 'message': f'describe has no action {action!r}; use one of these', 'actions': list(actions)}
