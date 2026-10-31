"""Find SSH private keys in vault secrets by value; field names only label them."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from app.instances.contracts import InstanceError
from app.webhooks.repository import many, one

_BLOCK = re.compile(r'-----BEGIN ((?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY)-----(.*?)-----END \1-----', re.S)
_CERT = re.compile(r'-----BEGIN CERTIFICATE-----')
_PUTTY = re.compile(r'^PuTTY-User-Key-File-\d+:', re.M)
_PASS_NAMES = re.compile(r'pass|pwd|口令|密码|phrase', re.I)


def normalize(value):
    """Return the canonical PEM block in a field, repairing common paste damage."""
    if not isinstance(value, str):
        return None
    text = value.replace('\r\n', '\n').replace('\r', '\n')
    if '\\n' in text and '\n' not in text.strip():
        text = text.replace('\\n', '\n')
    match = _BLOCK.search(text)
    if not match:
        return None
    kind, body = match.group(1), match.group(2)
    lines = [line.strip() for line in body.split('\n') if line.strip()]
    headers = [line for line in lines if ':' in line]
    data = ''.join(line for line in lines if ':' not in line).replace(' ', '')
    if not data or not re.fullmatch(r'[A-Za-z0-9+/=]+', data):
        return None
    wrapped = [data[i:i+70 if kind == 'OPENSSH PRIVATE KEY' else i+64] for i in range(0, len(data), 70 if kind == 'OPENSSH PRIVATE KEY' else 64)]
    head = headers + ([''] if headers else [])
    return '\n'.join([f'-----BEGIN {kind}-----', *head, *wrapped, f'-----END {kind}-----', ''])


async def _keygen(*args, stdin=None, env=None):
    try:
        process = await asyncio.create_subprocess_exec('ssh-keygen', *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', **(env or {})})
    except OSError:
        raise InstanceError('ssh_client_unavailable', 503) from None
    out, err = await asyncio.wait_for(process.communicate(stdin), 20)
    return process.returncode, out.decode(errors='replace'), err.decode(errors='replace')


async def inspect(pem, passphrase=''):
    """Parse with the same OpenSSH that will use it. Returns type, fingerprint and lock state."""
    with tempfile.TemporaryDirectory(prefix='openbear-key-') as directory:
        path = Path(directory) / 'key'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(pem)
        if passphrase:
            # Not on the command line, where every local user could read it: ssh-keygen asks the
            # askpass program, which reads it from a file only this user can open.
            secret = Path(directory) / 'passphrase'
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(passphrase)
            askpass = Path(directory) / 'askpass'
            fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(fd, 'w') as handle:
                handle.write(f'#!/bin/sh\nexec cat {shlex.quote(str(secret))}\n')
            code, public, err = await _keygen('-y', '-f', str(path), env={'SSH_ASKPASS': str(askpass), 'SSH_ASKPASS_REQUIRE': 'force',
                                                                       'DISPLAY': 'none'})
        else:
            code, public, err = await _keygen('-y', '-P', '', '-f', str(path))
        if code != 0:
            if 'passphrase' in err.lower():
                return {'usable': True, 'encrypted': True, 'unlocked': False}
            return {'usable': False, 'reason': 'unsupported_key'}
        code, line, _ = await _keygen('-l', '-f', '-', stdin=public.encode())
    parts = line.split()
    if code != 0 or len(parts) < 2:
        return {'usable': False, 'reason': 'unsupported_key'}
    kind = parts[-1].strip('()')
    bits = parts[0]
    label = kind + ('' if kind in {'ED25519', 'ED25519-SK'} else ' ' + bits)
    comment = ' '.join(parts[2:-1])
    return {'usable': True, 'encrypted': bool(passphrase), 'unlocked': True, 'type': label, 'fingerprint': parts[1],
            'comment': '' if comment == 'no comment' else comment}


async def _secrets(db):
    return await many(db.conn, 'SELECT id,name,note,grp,kv_json FROM memory_secrets WHERE archived=0 ORDER BY sort,id')


def _fields(row):
    try:
        items = json.loads(row['kv_json'] or '[]')
    except ValueError:
        return []
    return [(str(i.get('key') or ''), i.get('value')) for i in items if isinstance(i, dict)]


async def catalog(db):
    """Every vault field whose value is a private key; values never leave the server."""
    found = []
    for row in await _secrets(db):
        fields = _fields(row)
        has_cert = any(isinstance(v, str) and _CERT.search(v) for _, v in fields)
        others = [k for k, v in fields if isinstance(v, str) and v and not normalize(v)]
        candidates = sorted(others, key=lambda k: not _PASS_NAMES.search(k))
        for index, (key, value) in enumerate(fields):
            base = {'secret': row['name'], 'secretNote': row['note'] or '', 'field': key, 'index': index}
            if isinstance(value, str) and _PUTTY.search(value):
                found.append({**base, 'usable': False, 'reason': 'putty_format'})
                continue
            pem = normalize(value)
            if not pem:
                continue
            info = await inspect(pem)
            entry = {**base, **info}
            if info.get('usable') and has_cert:
                entry['likelyTls'] = True
            if info.get('encrypted'):
                entry['passphraseFields'] = candidates
                for name in candidates:
                    unlocked = await inspect(pem, next(v for k, v in fields if k == name))
                    if unlocked.get('unlocked'):
                        entry.update({k: unlocked[k] for k in ('type', 'fingerprint', 'comment')}, suggestedPassphraseField=name)
                        break
            found.append(entry)
    return found


async def resolve(db, ref):
    """Read the current value at use time; the server stores only the reference."""
    if not isinstance(ref, dict) or not isinstance(ref.get('secret'), str) or not isinstance(ref.get('field'), str):
        raise InstanceError('ssh_key_reference_invalid')
    row = await one(db.conn, 'SELECT name,kv_json FROM memory_secrets WHERE name=? AND archived=0', (ref['secret'],))
    if not row:
        raise InstanceError('ssh_key_missing', 409)
    fields = _fields(row)
    values = [v for k, v in fields if k == ref['field']]
    pem = normalize(values[0]) if len(values) == 1 else None
    if not pem:
        raise InstanceError('ssh_key_missing', 409)
    passphrase = ''
    if ref.get('passphraseField'):
        secrets = [v for k, v in fields if k == ref['passphraseField']]
        if len(secrets) != 1 or not isinstance(secrets[0], str):
            raise InstanceError('ssh_key_passphrase_missing', 409)
        passphrase = secrets[0]
    info = await inspect(pem, passphrase)
    if not info.get('usable'):
        raise InstanceError('ssh_key_unsupported', 409)
    if not info.get('unlocked'):
        raise InstanceError('ssh_key_passphrase_incorrect', 409)
    return pem, passphrase, info
