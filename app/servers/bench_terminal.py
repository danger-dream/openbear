"""What a terminal would finally show for a tool's output: carriage returns, line erases and
cursor moves applied, colours (SGR) kept, everything else dropped. Progress bars that redraw
one line collapse to their last state."""
from __future__ import annotations

import re

MAX_LINES = 20000
MAX_WIDTH = 2000
CLEAR = re.compile(r'\x1b\[[23]J|\x1bc')
_TOKEN = re.compile(r'\x1b\[([0-9;?]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\r\n\b\t]|[^\x1b\r\n\b\t]+')


def _sgr(state, params):
    """The colours and attributes after one SGR sequence: (attributes, foreground, background)."""
    attrs, fg, bg = set(state[0]), state[1], state[2]
    codes = [int(p) if p.isdigit() else 0 for p in params.split(';')] if params else [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            attrs, fg, bg = set(), None, None
        elif c in (1, 2, 3, 4, 5, 7, 8, 9):
            attrs.add(c)
        elif c in (21, 22):
            attrs -= {1, 2}
        elif 23 <= c <= 29:
            attrs.discard(c - 20)
        elif 30 <= c <= 37 or 90 <= c <= 97:
            fg = str(c)
        elif c == 39:
            fg = None
        elif 40 <= c <= 47 or 100 <= c <= 107:
            bg = str(c)
        elif c == 49:
            bg = None
        elif c in (38, 48):
            if codes[i + 1:i + 2] == [5] and i + 2 < len(codes):
                value, i = f'{c};5;{codes[i + 2]}', i + 2
            elif codes[i + 1:i + 2] == [2] and i + 4 < len(codes):
                value, i = f'{c};2;' + ';'.join(str(v) for v in codes[i + 2:i + 5]), i + 4
            else:
                break
            if c == 38:
                fg = value
            else:
                bg = value
        i += 1
    return frozenset(attrs), fg, bg


def _style(state):
    parts = [str(a) for a in sorted(state[0])] + [v for v in state[1:] if v]
    return f'\x1b[{";".join(parts)}m' if parts else ''


def after_last_clear(text):
    """The output since the screen was last cleared: a tool's final report usually starts there."""
    last = None
    for last in CLEAR.finditer(text):
        pass
    return text[last.end():] if last else text


def screen(text, *, clears='reset'):
    """clears='reset' starts over at each screen clear; 'mark' keeps the earlier output and
    draws a rule where the screen was cleared."""
    lines = [[]]
    row = col = 0
    state, style = (frozenset(), None, None), ''

    def put(ch):
        nonlocal col
        line = lines[row]
        if col >= MAX_WIDTH:
            return
        while len(line) < col:
            line.append(('', ' '))
        cell = (style, ch)
        if col < len(line):
            line[col] = cell
        else:
            line.append(cell)
        col += 1

    def goto(r):
        nonlocal row
        row = max(0, min(r, MAX_LINES - 1))
        while len(lines) <= row:
            lines.append([])

    for m in _TOKEN.finditer(text):
        token = m.group(0)
        if token == '\n':
            goto(row + 1)
            col = 0
        elif token == '\r':
            col = 0
        elif token == '\b':
            col = max(0, col - 1)
        elif token == '\t':
            col = min(MAX_WIDTH, (col // 8 + 1) * 8)
        elif token[0] != '\x1b':
            for ch in token:
                put(ch)
        elif m.group(2):
            params, final = m.group(1), m.group(2)
            if params.startswith('?'):
                continue
            nums = [int(p) if p.isdigit() else 0 for p in params.split(';')] if params else []
            n = nums[0] if nums and nums[0] else 1
            if final == 'm':
                state = _sgr(state, params)
                style = _style(state)
            elif final == 'K':
                mode = nums[0] if nums else 0
                line = lines[row]
                if mode == 0:
                    del line[col:]
                elif mode == 1:
                    line[:min(col + 1, len(line))] = [('', ' ')] * min(col + 1, len(line))
                else:
                    line.clear()
            elif final in 'JH' and (final == 'J' and (nums[:1] in ([2], [3])) or final == 'H' and not params):
                if final == 'J':
                    if clears == 'reset':
                        lines, row, col = [[]], 0, 0
                    else:
                        goto(len(lines))
                        lines[row] = [('', '─')] * 40
                        goto(row + 1)
                        col = 0
                elif clears == 'reset':
                    row = col = 0
            elif final == 'A':
                goto(row - n)
            elif final == 'B':
                goto(row + n)
            elif final == 'C':
                col = min(MAX_WIDTH, col + n)
            elif final == 'D':
                col = max(0, col - n)
            elif final == 'G':
                col = max(0, min(MAX_WIDTH, n - 1))
        elif token == '\x1bc':
            if clears == 'reset':
                lines, row, col = [[]], 0, 0
    out = []
    for line in lines:
        text_line, current = [], ''
        for cell_style, ch in line:
            if cell_style != current:
                text_line.append('\x1b[0m' + cell_style if current else cell_style)
                current = cell_style
            text_line.append(ch)
        if current:
            text_line.append('\x1b[0m')
        out.append(''.join(text_line).rstrip(' '))
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return '\n'.join(out) + '\n' if out else ''
