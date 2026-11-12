"""File operations a node runs as one shell command each, so they are logged
like any other command. Every path and name is quoted; absolute paths start
with '/', so none of them can be read as an option. Windows nodes get the
PowerShell equivalent, with paths written as 'C:/dir' and passed literally."""
from __future__ import annotations

import posixpath
import re
import shlex

from app.instances.contracts import InstanceError
from app.servers.install import ps

EXISTS = 17
NO_TOOL = 127
MAX_PATHS = 200
LONG = 1800
SHORT = 60
FORMATS = {'tar.gz': '.tar.gz', 'tar.xz': '.tar.xz', 'zip': '.zip'}
ARCHIVES = (('.tar.gz', 'tar'), ('.tgz', 'tar'), ('.tar.xz', 'tar'), ('.txz', 'tar'), ('.tar.bz2', 'tar'), ('.tbz2', 'tar'),
            ('.tar.zst', 'tar'), ('.tar', 'tar'), ('.zip', 'zip'))
# Removing or renaming these breaks the system or this node; recursive chmod
# on them is refused for the same reason.
PROTECTED = frozenset({
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib32', '/lib64', '/libx32', '/media', '/mnt', '/opt', '/proc', '/root',
    '/run', '/sbin', '/srv', '/sys', '/tmp', '/usr', '/var', '/usr/bin', '/usr/sbin', '/usr/lib', '/usr/local', '/usr/local/bin',
    '/var/lib', '/var/log', '/etc/ssh', '/etc/systemd', '/etc/openbear-node', '/usr/local/bin/openbear-node',
    '/etc/systemd/system/openbear-node.service', '/etc/init.d/openbear-node',
    # macOS
    '/Applications', '/Library', '/Library/LaunchDaemons', '/Library/LaunchDaemons/com.openbear.node.plist', '/System', '/Users',
    '/Volumes', '/private', '/private/etc', '/private/tmp', '/private/var', '/cores',
})
# On every drive; compared case-insensitively. The drive root itself too.
WINDOWS_PROTECTED = frozenset({
    '', '/windows', '/windows/system32', '/program files', '/program files (x86)', '/programdata', '/users', '/recovery',
    '/program files/openbear', '/program files/openbear/openbear-node.exe', '/programdata/openbear', '/programdata/openbear/node',
})
WINDOWS_FORMATS = ('tar.gz', 'zip')
_DRIVE = re.compile(r'[A-Za-z]:')
_MODE = re.compile(r'[0-7]{3,4}')
q = shlex.quote


def path(value, platform='linux'):
    if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in '\x00\n\r'):
        raise InstanceError('invalid_file_path')
    if platform == 'windows':
        value = value.replace('\\', '/')
        if not _DRIVE.match(value) or value[2:3] not in ('', '/'):
            raise InstanceError('invalid_file_path')
        rest = posixpath.normpath('/' + value[2:].lstrip('/'))
        return value[:2].upper() + ('/' if rest == '/' else rest)
    if not value.startswith('/'):
        raise InstanceError('invalid_file_path')
    return '/' + posixpath.normpath(value).lstrip('/')


def name(value, platform='linux'):
    banned = '/\\:*?"<>|\x00\n\r' if platform == 'windows' else '/\x00\n\r'
    if (not isinstance(value, str) or not value.strip() or value in {'.', '..'} or len(value.encode()) > 255
            or any(c in value for c in banned)):
        raise InstanceError('invalid_file_name')
    return value


def paths(value, platform='linux'):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PATHS:
        raise InstanceError('invalid_file_paths')
    out = list(dict.fromkeys(path(v, platform) for v in value))
    return out


def unprotected(items, platform='linux'):
    for item in items:
        if item in PROTECTED if platform != 'windows' else _windows_key(item) in WINDOWS_PROTECTED:
            raise InstanceError('protected_path', 403)
    return items


def _windows_key(item):
    # Windows drops trailing dots and spaces from each name: "C:/Windows." names C:/Windows.
    return '/'.join(part.rstrip('. ') for part in item[2:].split('/')).rstrip('/').lower()


def archive_kind(target):
    lower = target.lower()
    return next((kind for ext, kind in ARCHIVES if lower.endswith(ext)), None)


# BusyBox tar (OpenWrt, Alpine) neither tells the compression of what it extracts nor has -J or -j:
# gzip goes with -z, the rest through their own program, and without it the tool is missing.
BUSYBOX_TAR = 'tar --help 2>&1 | grep -q BusyBox'
PIPEFAIL = '(set -o pipefail) 2>/dev/null && set -o pipefail; '
COMPRESSORS = {'.tar.gz': 'gz', '.tgz': 'gz', '.tar.xz': 'xz', '.txz': 'xz', '.tar.bz2': 'bz2', '.tbz2': 'bz2', '.tar.zst': 'zst'}
DECOMPRESS = {'xz': ('xz -dc', 'unxz -c'), 'bz2': ('bzip2 -dc', 'bunzip2 -c'), 'zst': ('zstd -dc', 'unzstd -c')}


def compression(target):
    lower = target.lower()
    return next((c for ext, c in COMPRESSORS.items() if lower.endswith(ext)), '')


def extract_tar(archive, dest):
    comp, plain = compression(archive), f'tar -xf {q(archive)} -C {q(dest)}'
    if not comp:
        return plain
    if comp == 'gz':
        return f'if {BUSYBOX_TAR}; then tar -xzf {q(archive)} -C {q(dest)}; else {plain}; fi'
    tools = DECOMPRESS[comp]
    piped = ''.join(f'{"if" if i == 0 else "elif"} command -v {tool.split()[0]} >/dev/null 2>&1; then {tool} {q(archive)} | tar -xf - -C {q(dest)}; '
                    for i, tool in enumerate(tools))
    gnu = f'command -v {tools[0].split()[0]} >/dev/null 2>&1 || exit {NO_TOOL}; {plain}'
    return f'if {BUSYBOX_TAR}; then {PIPEFAIL}{piped}else exit {NO_TOOL}; fi; else {gnu}; fi'


def free(target):
    """Shell test that stops with EXISTS when something is already there."""
    return f'if [ -e {q(target)} ] || [ -L {q(target)} ]; then exit {EXISTS}; fi; '


def command(op, data, platform='linux'):
    """(shell command, timeout seconds) for one operation; raises on bad input."""
    if platform == 'windows':
        return windows_command(op, data)
    if op == 'delete':
        return 'rm -rf -- ' + ' '.join(q(p) for p in unprotected(paths(data.get('paths')))), LONG
    if op == 'rename':
        source = unprotected([path(data.get('path'))])[0]
        target = posixpath.join(posixpath.dirname(source), name(data.get('name')))
        if platform == 'darwin' and target != source and target.lower() == source.lower():
            # Only the case changes, on a file system that ignores it: the target "exists" (it is the
            # source), and mv would put a directory inside itself. Through a temporary name.
            step = posixpath.join(posixpath.dirname(source), f'.{name(data.get("name"))}.obrename')
            return free(step) + f'mv -- {q(source)} {q(step)} && mv -- {q(step)} {q(target)}', SHORT
        # BSD mv has no -T; free() has already ruled out an existing target.
        return free(target) + (f'mv -- {q(source)} {q(target)}' if platform == 'darwin' else f'mv -T -- {q(source)} {q(target)}'), SHORT
    if op == 'chmod':
        mode, recursive = data.get('mode'), data.get('recursive') is True
        if not isinstance(mode, str) or not _MODE.fullmatch(mode):
            raise InstanceError('invalid_file_mode')
        items = paths(data.get('paths'))
        if recursive:
            unprotected(items)
        # BSD chmod takes "--" for a file name; the paths are absolute anyway.
        end = '' if platform == 'darwin' else '-- '
        return f'chmod {"-R " if recursive else ""}{mode} {end}' + ' '.join(q(p) for p in items), LONG if recursive else SHORT
    if op in ('mkdir', 'touch'):
        target = posixpath.join(path(data.get('path')), name(data.get('name')))
        return free(target) + (f'mkdir -- {q(target)}' if op == 'mkdir' else f': > {q(target)}'), SHORT
    if op == 'compress':
        items = paths(data.get('paths'))
        parent = posixpath.dirname(items[0])
        if any(posixpath.dirname(p) != parent for p in items) or '/' in items:
            raise InstanceError('compress_one_folder')
        fmt = data.get('format', 'tar.gz')
        if fmt not in FORMATS:
            raise InstanceError('invalid_archive_format')
        file = name(data.get('name'))
        if not file.lower().endswith(FORMATS[fmt]):
            file += FORMATS[fmt]
        target = posixpath.join(parent, file)
        members = [posixpath.basename(p) for p in items]
        if fmt == 'zip':
            local = ' '.join(q('./' + m) for m in members)
            body = (f'cd {q(parent)} && if command -v zip >/dev/null 2>&1; then zip -qry {q(target)} {local}; '
                    f'elif command -v python3 >/dev/null 2>&1; then python3 -m zipfile -c {q(target)} {local}; else exit {NO_TOOL}; fi')
        elif fmt == 'tar.xz' and platform != 'darwin':
            listed = ' '.join(q(m) for m in members)
            busybox = f'{PIPEFAIL}tar -cf - -C {q(parent)} -- {listed} | xz -c > {q(target)} || {{ rc=$?; rm -f -- {q(target)}; exit $rc; }}'
            body = (f'command -v xz >/dev/null 2>&1 || exit {NO_TOOL}; '
                    f'if {BUSYBOX_TAR}; then {busybox}; else tar -cJf {q(target)} -C {q(parent)} -- {listed}; fi')
        else:
            body = f'tar -c{"z" if fmt == "tar.gz" else "J"}f {q(target)} -C {q(parent)} -- ' + ' '.join(q(m) for m in members)
        return free(target) + body, LONG
    if op == 'extract':
        archive = path(data.get('path'))
        kind = archive_kind(archive)
        if not kind:
            raise InstanceError('not_an_archive')
        dest = path(data.get('dest') or posixpath.dirname(archive))
        if kind == 'zip':
            body = (f'if command -v unzip >/dev/null 2>&1; then unzip -qo {q(archive)} -d {q(dest)}; '
                    f'elif command -v python3 >/dev/null 2>&1; then python3 -m zipfile -e {q(archive)} {q(dest)}; else exit {NO_TOOL}; fi')
        elif platform == 'darwin':
            body = f'tar -xf {q(archive)} -C {q(dest)}'
        else:
            body = extract_tar(archive, dest)
        return f'mkdir -p -- {q(dest)} && ' + body, LONG
    raise InstanceError('invalid_file_op')


def ps_free(target):
    return f'if ([IO.File]::Exists({ps(target)}) -or [IO.Directory]::Exists({ps(target)})) {{ exit {EXISTS} }}\n'


def ps_parent(target):
    parent = posixpath.dirname(target)
    return parent + '/' if len(parent) == 2 else parent


TAR = f"if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {{ exit {NO_TOOL} }}\n"


def windows_command(op, data):
    """PowerShell for one operation; tar.exe (Windows 10 1803+) handles archives."""
    w = 'windows'
    if op == 'delete':
        items = unprotected(paths(data.get('paths'), w), w)
        return 'Remove-Item -LiteralPath ' + ','.join(ps(p) for p in items) + ' -Recurse -Force', LONG
    if op == 'rename':
        source = unprotected([path(data.get('path'), w)], w)[0]
        new = name(data.get('name'), w)
        if new != posixpath.basename(source) and new.lower() == posixpath.basename(source).lower():
            # Only the case changes: NTFS sees the target as the source, and Rename-Item refuses
            # a name it takes for the same one. Through a temporary name.
            step = f'.{new}.obrename'
            return (ps_free(posixpath.join(ps_parent(source), step)) + f'Rename-Item -LiteralPath {ps(source)} -NewName {ps(step)} -ErrorAction Stop\n'
                    f'Rename-Item -LiteralPath {ps(posixpath.join(ps_parent(source), step))} -NewName {ps(new)}'), SHORT
        return ps_free(posixpath.join(ps_parent(source), new)) + f'Rename-Item -LiteralPath {ps(source)} -NewName {ps(new)}', SHORT
    if op == 'chmod':
        raise InstanceError('chmod_unsupported')
    if op in ('mkdir', 'touch'):
        target = posixpath.join(path(data.get('path'), w), name(data.get('name'), w))
        make = f'[void][IO.Directory]::CreateDirectory({ps(target)})' if op == 'mkdir' else f'[IO.File]::Create({ps(target)}).Close()'
        return ps_free(target) + make, SHORT
    if op == 'compress':
        items = paths(data.get('paths'), w)
        parent = ps_parent(items[0])
        if any(ps_parent(p) != parent for p in items) or any(len(p) <= 3 for p in items):
            raise InstanceError('compress_one_folder')
        fmt = data.get('format', 'zip')
        if fmt not in WINDOWS_FORMATS:
            raise InstanceError('invalid_archive_format')
        file = name(data.get('name'), w)
        if not file.lower().endswith(FORMATS[fmt]):
            file += FORMATS[fmt]
        target = posixpath.join(parent, file)
        members = ' '.join(ps(posixpath.basename(p)) for p in items)
        flags = '-a -c' if fmt == 'zip' else '-c -z'
        return ps_free(target) + TAR + f'tar.exe {flags} -f {ps(target)} -C {ps(parent)} -- {members}', LONG
    if op == 'extract':
        archive = path(data.get('path'), w)
        if not archive_kind(archive):
            raise InstanceError('not_an_archive')
        dest = path(data.get('dest') or ps_parent(archive), w)
        return TAR + f'[void][IO.Directory]::CreateDirectory({ps(dest)})\ntar.exe -x -f {ps(archive)} -C {ps(dest)}', LONG
    raise InstanceError('invalid_file_op')


def outcome(result):
    """Map an exec result to the error the page shows; None when it worked."""
    if result.get('ok'):
        return None
    if result.get('error'):
        return result['error'], ''
    if result.get('timedOut'):
        return 'file_op_timeout', ''
    code = result.get('exitCode')
    if code == EXISTS:
        return 'file_exists', ''
    if code == NO_TOOL:
        return 'archive_tool_missing', ''
    detail = (result.get('stderr') or result.get('stdout') or '').strip().splitlines()
    return 'file_op_failed', (detail[-1] if detail else '')[:300]
