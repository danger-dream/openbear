"""The controller <-> node protocol samples in node/testdata/protocol.md, read the same way
node/protocol_samples_test.go reads them. The fake node (tests/fake_node.py) checks what the
controller sends against them and builds what it sends from them; the Go test checks the real
node against the same file. See the file itself for the notation."""
from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'node' / 'testdata' / 'protocol.md'
_TYPES = {'<string>', '<int>', '<int?>', '<number>', '<bool>', '<base64>', '<hex64>', '<any>'}


def load(path=PATH):
    """name -> value: each "### name" heading and the ```json block after it (line by line,
    as the Go test reads it)."""
    samples, name, block = {}, None, None
    for line in path.read_text(encoding='utf-8').splitlines():
        if block is not None:
            if line == '```':
                if name in samples:
                    raise ValueError(f'sample {name} appears twice')
                samples[name], name, block = json.loads('\n'.join(block)), None, None
            else:
                block.append(line)
        elif line.startswith('### '):
            if name:
                raise ValueError(f'sample {name} has no json block')
            name = line[4:].split()[0]
        elif line == '```json' and name:
            block = []
    if name or block is not None:
        raise ValueError(f'sample {name} is not finished')
    return samples


SAMPLES = load()


def sample(name):
    return copy.deepcopy(SAMPLES[name])


def subst(value, mapping):
    """value with each token of mapping (like '<dir>') replaced inside strings and keys."""
    if isinstance(value, str):
        for token, real in mapping.items():
            value = value.replace(token, real)
        return value
    if isinstance(value, list):
        return [subst(v, mapping) for v in value]
    if isinstance(value, dict):
        return {subst(k, mapping): subst(v, mapping) for k, v in value.items()}
    return value


def _ref(value):
    return value[5:-1] if isinstance(value, str) and value.startswith('<ref:') and value.endswith('>') else None


def _kind(value):
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, (int, float)):
        return 'number'
    return {str: 'string', list: 'list', dict: 'object', type(None): 'null'}[type(value)]


def problems(value, pattern, *, loose=False, where='$'):
    """What keeps value from matching pattern (an empty list when it matches).

    Placeholders (<string>, <int>, <int?> ...) match by type; "<ref:name>" is sample name; a key
    ending in "?" may be left out; ["<each>", p] is a list of any length whose items match p;
    any other list has exactly its items. Other literals match exactly, or with loose=True only
    by their JSON type (the shape of an example)."""
    ref = _ref(pattern)
    if ref:
        return problems(value, SAMPLES[ref], loose=loose, where=where)
    if isinstance(pattern, str) and pattern in _TYPES:
        ok = {'<string>': isinstance(value, str), '<any>': True,
              '<int>': _kind(value) == 'number' and float(value).is_integer(),
              '<int?>': value is None or _kind(value) == 'number' and float(value).is_integer(),
              '<number>': _kind(value) == 'number', '<bool>': isinstance(value, bool),
              '<hex64>': isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None}.get(pattern)
        if pattern == '<base64>':
            try:
                ok = isinstance(value, str) and base64.b64decode(value, validate=True) is not None
            except ValueError:
                ok = False
        return [] if ok else [f'{where}: {value!r} is not {pattern}']
    if isinstance(pattern, dict):
        if not isinstance(value, dict):
            return [f'{where}: {value!r} is not an object']
        keys = {k.rstrip('?'): k for k in pattern}
        found = [f'{where}: unexpected key {k!r}' for k in value if k not in keys]
        found += [f'{where}: missing key {k!r}' for k, raw in keys.items() if not raw.endswith('?') and k not in value]
        for k, raw in keys.items():
            if k in value:
                found += problems(value[k], pattern[raw], loose=loose, where=f'{where}.{k}')
        return found
    if isinstance(pattern, list):
        if not isinstance(value, list):
            return [f'{where}: {value!r} is not a list']
        if pattern[:1] == ['<each>']:
            return [p for i, v in enumerate(value) for p in problems(v, pattern[1], loose=loose, where=f'{where}[{i}]')]
        if len(value) != len(pattern):
            return [f'{where}: {len(value)} items, the sample has {len(pattern)}']
        return [p for i, (v, s) in enumerate(zip(value, pattern)) for p in problems(v, s, loose=loose, where=f'{where}[{i}]')]
    if loose:
        return [] if _kind(value) == _kind(pattern) else [f'{where}: {value!r} is not a {_kind(pattern)}']
    return [] if value == pattern and _kind(value) == _kind(pattern) else [f'{where}: {value!r} is not {pattern!r}']


def check(value, name_or_pattern, *, loose=False):
    """Raise AssertionError unless value matches the named sample (or a pattern)."""
    pattern = SAMPLES[name_or_pattern] if isinstance(name_or_pattern, str) else name_or_pattern
    found = problems(value, pattern, loose=loose)
    if found:
        label = name_or_pattern if isinstance(name_or_pattern, str) else 'pattern'
        raise AssertionError(f'protocol sample {label}: ' + '; '.join(found[:8]))
    return value


def method_sample(method):
    """The primary call sample of a node method ("### call:exec"), or None."""
    return SAMPLES.get(f'call:{method}')
